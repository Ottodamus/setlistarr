"""Command-line entry point: serve / sync / enrich / generate / stats / migrate / mcp."""

from __future__ import annotations

import argparse
import json

from .config import bootstrap
from .core.db import connect, migrate
from .logs import setup_logging


def _conn():
    conn = connect(bootstrap().db_path)
    migrate(conn)
    return conn


def cmd_serve(args) -> None:
    import uvicorn

    b = bootstrap()
    # workers=1 is load-bearing: the scheduler runs in-process.
    uvicorn.run(
        "setlistarr.web.app:create_app",
        factory=True,
        host=args.host or b.host,
        port=args.port or b.port,
        workers=1,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def cmd_sync(args) -> None:
    from .services.jobs import run_job
    from .services.library import do_sync

    stats = run_job("sync-full" if args.full else "sync", "cli", lambda c: do_sync(c, full=args.full))
    print(json.dumps(stats, indent=2, default=str))


def cmd_enrich(args) -> None:
    from .services.jobs import run_job
    from .services.library import do_enrich

    stats = run_job("enrich", "cli", lambda c: do_enrich(c, limit=args.limit))
    print(json.dumps(stats, indent=2, default=str))


def cmd_stats(args) -> None:
    from .core.tools import ToolContext, list_facets

    print(json.dumps(list_facets(ToolContext(conn=_conn())), indent=2))


def cmd_generate(args) -> None:
    from .generation.constraints import Constraints
    from .generation.runner import default_constraints, manager
    from .llm.registry import get_provider_by_name
    from .services.generations import get_generation, load_draft

    conn = _conn()
    provider_id = None
    if args.provider:
        cfg = get_provider_by_name(conn, args.provider)
        if not cfg:
            raise SystemExit(f"no provider named {args.provider!r}")
        provider_id = cfg.id
    base = default_constraints(conn)
    c = Constraints.from_form(
        {"target_minutes": args.minutes, "max_per_artist": args.max_per_artist},
        default_target=base.default_target_minutes, default_cap=base.default_max_per_artist,
    )
    gen_id = manager.start(conn, args.prompt, provider_id=provider_id, constraints=c, mode=args.mode)
    log = manager.events(gen_id)
    seq = 0
    while True:
        events, done = log.wait_after(seq, 5)
        for e in events:
            seq = e["seq"]
            print(f"{e['t']:6.1f}s {e['kind']:<11} {e['msg']}")
        if done and seq >= len(log.events):
            break
    g = get_generation(conn, gen_id)
    if g["status"] != "ok":
        raise SystemExit(f"{g['status']}: {g['error']}")
    d = load_draft(conn, g["draft_id"])
    print(f"\n{d['title']}  ({len(d['tracks'])} tracks, {d['total_ms'] // 60000} min)")
    if d["description"]:
        print(d["description"])
    for i, t in enumerate(d["tracks"], 1):
        print(f"{i:3}. {t['artist']} - {t['title']} ({t['year'] or '?'})" + ("  [backfill]" if t["source"] == "backfill" else ""))
    for a in d["adjustments"]:
        print("  ! " + a)
    cost = "unknown" if g["cost_usd"] is None else f"${g['cost_usd']:.4f}"
    print(f"\nmode {g['mode_used']} · {g['llm_calls']} calls · {g['input_tokens'] + g['cache_read_tokens'] + g['cache_write_tokens']:,} in / {g['output_tokens']:,} out · cost {cost}")
    print(f"draft: /generate/{gen_id}")


def cmd_migrate(args) -> None:
    print("schema version", migrate(connect(bootstrap().db_path)))


def cmd_mcp(args) -> None:
    from .mcp_server import main as mcp_main

    mcp_main()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="setlistarr")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sync", help="sync enabled Plex music sections into the local DB")
    s.add_argument("--full", action="store_true", help="full resync (detects deletions)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("enrich", help="add Last.fm / MusicBrainz tags and similar artists")
    s.add_argument("--limit", type=int, default=None, help="only process the first N artists")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("generate", help="generate a draft playlist from a prompt")
    s.add_argument("prompt")
    s.add_argument("--provider", help="provider name (default: the default provider)")
    s.add_argument("--mode", choices=["auto", "tools", "two_stage"])
    s.add_argument("--minutes", type=float)
    s.add_argument("--max-per-artist", type=int)
    s.set_defaults(func=cmd_generate)

    sub.add_parser("stats", help="library summary as JSON").set_defaults(func=cmd_stats)
    sub.add_parser("migrate", help="apply DB migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("mcp", help="run the MCP stdio server").set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    if args.cmd != "mcp":  # stdout belongs to the MCP protocol
        b = bootstrap()
        setup_logging(b.log_level, b.log_format)
    args.func(args)


if __name__ == "__main__":
    main()
