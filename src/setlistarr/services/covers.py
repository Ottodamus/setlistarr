"""Playlist covers: uploads (cropped in the browser) and AI-generated covers.

Two ways to generate:
  * image model (OpenAI-compatible provider whose model is gpt-image-* etc.) -> real artwork
  * text model (Claude, OpenAI chat, Ollama) -> the model writes SVG vector art, which we render
    to PNG with resvg. The SVG is sanitized first and never served to browsers.

Every cover is stored as a square JPEG (max 1024 px) in /config/covers.
"""

from __future__ import annotations

import io
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import bootstrap
from ..llm.base import ProviderError
from ..llm.registry import ProviderConfig, build_provider, cost_usd, get_provider, list_providers
from ..logs import get_logger

log = get_logger(__name__)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SIDE = 1024
MIN_SIDE = 300
SVG_SIZE = 1024
Image.MAX_IMAGE_PIXELS = 60_000_000  # refuse decompression bombs


class CoverError(Exception):
    pass


def covers_dir() -> Path:
    d = bootstrap().config_dir / "covers"
    d.mkdir(exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def normalize(data: bytes) -> bytes:
    """Any supported image -> square RGB JPEG, max 1024 px. Center-crops if not square
    (the browser already crops uploads; this is the safety net)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise CoverError("That image is larger than 15 MB")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # honor phone rotation, then EXIF is dropped
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise CoverError("That file isn't an image Setlistarr can read (use JPEG, PNG or WebP)") from e
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    else:
        img = img.convert("RGB")
    side = min(img.size)
    if side < MIN_SIDE:
        raise CoverError(f"That image is too small ({img.width}x{img.height}); use at least {MIN_SIDE}x{MIN_SIDE}")
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > MAX_SIDE:
        img = img.resize((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def store(conn: sqlite3.Connection, image: bytes, *, kind: str, draft_id: int | None = None,
          recipe_id: int | None = None, prompt: str | None = None, provider_id: int | None = None,
          model: str | None = None, cost: float | None = None) -> int:
    jpeg = normalize(image)
    name = f"{uuid.uuid4().hex}.jpg"
    (covers_dir() / name).write_bytes(jpeg)
    cid = conn.execute(
        "INSERT INTO covers(kind, file, draft_id, recipe_id, prompt, provider_id, model, cost_usd, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, name, draft_id, recipe_id, prompt, provider_id, model, cost, _now()),
    ).lastrowid
    conn.commit()
    return cid


def get_cover(conn: sqlite3.Connection, cover_id: int | None) -> dict[str, Any] | None:
    if not cover_id:
        return None
    row = conn.execute("SELECT * FROM covers WHERE id = ?", (cover_id,)).fetchone()
    return dict(row) if row else None


def cover_bytes(conn: sqlite3.Connection, cover_id: int) -> bytes | None:
    c = get_cover(conn, cover_id)
    if not c:
        return None
    path = covers_dir() / c["file"]
    return path.read_bytes() if path.exists() else None


def history(conn: sqlite3.Connection, *, draft_id: int | None = None, recipe_id: int | None = None,
            limit: int = 8) -> list[dict[str, Any]]:
    """Covers made for this draft / recipe (newest first), for switching back."""
    col, val = ("draft_id", draft_id) if draft_id else ("recipe_id", recipe_id)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM covers WHERE {col} = ? ORDER BY id DESC LIMIT ?", (val, limit))]


def set_cover(conn: sqlite3.Connection, cover_id: int | None, *, draft_id: int | None = None,
              recipe_id: int | None = None) -> None:
    with conn:
        if draft_id:
            conn.execute("UPDATE drafts SET cover_id = ? WHERE id = ?", (cover_id, draft_id))
        if recipe_id:
            conn.execute("UPDATE recipes SET cover_id = ? WHERE id = ?", (cover_id, recipe_id))


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
def default_prompt(title: str | None, description: str | None = None, request: str | None = None) -> str:
    parts = [f'Square album-cover artwork for a music playlist titled "{(title or "Untitled").strip()}".']
    vibe = (description or request or "").strip()
    if vibe:
        parts.append(f"Mood and music: {vibe[:300]}")
    parts.append("Bold, striking composition that reads well as a small thumbnail. "
                 "No text, no logos, no real people or band likenesses.")
    return " ".join(parts)


SVG_SYSTEM = """\
You are a graphic designer who makes album covers as SVG code.
Output ONLY one complete SVG document - no explanation, no markdown fences.
Rules: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">;
use shapes, paths, gradients, patterns and filters; fill the whole canvas (no transparent background);
no <image>, no external references, no scripts, no <foreignObject>; at most one short line of text
(only if it truly adds to the design). Keep it under about 12 KB.
"""


# --------------------------------------------------------------------------- #
# SVG sanitizing + rendering
# --------------------------------------------------------------------------- #
_SVG_NS = "{http://www.w3.org/2000/svg}"
_BANNED = {"script", "foreignObject", "image", "feImage", "iframe", "audio", "video"}
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?(?!#)|@import", re.I)  # url(#grad) is fine
_HREF_ATTRS = ("href", "{http://www.w3.org/1999/xlink}href")


def extract_svg(text: str) -> str:
    m = re.search(r"<svg\b.*?</svg>", text or "", re.S | re.I)
    if not m:
        raise CoverError("The model didn't return SVG artwork")
    return m.group(0)


def sanitize_svg(svg: str) -> str:
    """Drop anything that could reach outside the document (files, URLs, scripts). resvg would
    otherwise resolve <image href="/config/..."> from the local disk."""
    if re.search(r"<!(DOCTYPE|ENTITY)", svg, re.I):
        raise CoverError("SVG with DOCTYPE/ENTITY declarations isn't allowed")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        raise CoverError(f"The model's SVG wasn't valid XML ({e})") from e
    if root.tag not in ("svg", f"{_SVG_NS}svg"):
        raise CoverError("Not an SVG document")

    def clean(el: ET.Element) -> None:
        for child in list(el):
            local = child.tag.split("}")[-1]
            if local in _BANNED or (local == "style" and _EXTERNAL_URL.search(child.text or "")):
                el.remove(child)
                continue
            for attr in list(child.attrib):
                low = attr.split("}")[-1].lower()
                val = child.attrib[attr].strip()
                if low.startswith("on") or (attr in _HREF_ATTRS and not val.startswith("#")) \
                        or _EXTERNAL_URL.search(val):
                    del child.attrib[attr]
            clean(child)

    clean(root)
    for attr in list(root.attrib):
        if attr.lower().startswith("on"):
            del root.attrib[attr]
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def render_svg(svg: str) -> bytes:
    import resvg_py

    # resvg maps generic families to Arial/Times by default; the container ships DejaVu
    # (fonts-dejavu-core), so name it explicitly or text silently disappears.
    png = resvg_py.svg_to_bytes(
        svg_string=svg, width=SVG_SIZE, height=SVG_SIZE, background="#000000",
        font_family="DejaVu Sans", sans_serif_family="DejaVu Sans", serif_family="DejaVu Serif",
        monospace_family="DejaVu Sans Mono", cursive_family="DejaVu Sans", fantasy_family="DejaVu Sans",
    )
    return bytes(png)


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
def cover_providers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every provider, labelled with how it makes covers."""
    out = []
    for p in list_providers(conn):
        out.append({"cfg": p, "id": p.id, "method": "image" if p.is_image_model else "art",
                    "label": f"{p.name} · {p.model}"})
    return sorted(out, key=lambda x: (x["method"] != "image", x["label"].lower()))


def default_cover_provider(conn: sqlite3.Connection) -> ProviderConfig | None:
    """An image model if one is configured, else the default playlist provider."""
    image = [p for p in list_providers(conn) if p.is_image_model]
    return image[0] if image else get_provider(conn)


def generate(conn: sqlite3.Connection, *, prompt: str, provider_id: int | None = None,
             draft_id: int | None = None, recipe_id: int | None = None, inject: dict | None = None) -> int:
    """Make an AI cover and store it. Raises CoverError / ProviderError with UI-ready messages."""
    cfg = get_provider(conn, provider_id) if provider_id else default_cover_provider(conn)
    if cfg is None:
        raise CoverError("No AI provider configured - add one under AI providers")
    prompt = prompt.strip()
    if not prompt:
        raise CoverError("Describe the cover you want")
    provider = (inject or {}).get("provider") or build_provider(cfg, **(inject or {}))

    if cfg.is_image_model:
        if not hasattr(provider, "generate_image"):
            raise CoverError(f"{cfg.name} can't generate images")
        image, usage, model = provider.generate_image(prompt)
        cost = cost_usd(conn, cfg, model, usage) if (usage.input_tokens or usage.output_tokens) else None
        kind = "ai_image"
    else:
        messages: list[dict[str, Any]] = [{"role": "user", "content": f"Design this cover:\n{prompt}"}]
        cost, image, model, last_err = 0.0, None, cfg.model, None
        for _attempt in range(2):
            resp = provider.chat(system=SVG_SYSTEM, messages=messages, max_tokens=12000)
            c = cost_usd(conn, cfg, resp.model or cfg.model, resp.usage)
            cost = None if (c is None or cost is None) else cost + c
            model = resp.model or cfg.model
            try:
                image = render_svg(sanitize_svg(extract_svg(resp.text)))
                break
            except CoverError as e:
                last_err = e
                messages += [resp.as_message(),
                             {"role": "user", "content": f"That didn't work: {e}. Reply with only a valid SVG document."}]
            except Exception as e:  # resvg parse/render errors
                last_err = CoverError(f"The SVG couldn't be rendered ({e})")
                messages += [resp.as_message(),
                             {"role": "user", "content": "That SVG couldn't be rendered. Reply with a simpler, valid SVG."}]
        if image is None:
            raise last_err or CoverError("The model couldn't produce cover art")
        kind = "ai_art"

    cid = store(conn, image, kind=kind, draft_id=draft_id, recipe_id=recipe_id, prompt=prompt,
                provider_id=cfg.id, model=model, cost=cost)
    log.info("cover.generated", cover_id=cid, kind=kind, provider=cfg.name, model=model, cost_usd=cost)
    return cid


def generate_safely(conn: sqlite3.Connection, **kw) -> tuple[int | None, str | None]:
    """generate() for background use (recipes): returns (cover_id, error) instead of raising."""
    try:
        return generate(conn, **kw), None
    except (CoverError, ProviderError) as e:
        return None, str(e)
    except Exception as e:
        log.exception("cover.failed")
        return None, f"{type(e).__name__}: {e}"
