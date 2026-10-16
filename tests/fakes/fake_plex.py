"""A small Plex Media Server stand-in on pytest-httpserver.

Serves the JSON shapes Setlistarr reads: identity, sections, paged /all listings with
updatedAt>> / lastViewedAt>> filters, batched /library/metadata/k1,k2, and playlist
creation. Records every request so tests can assert on traffic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from werkzeug.wrappers import Request, Response

TOKEN = "test-token"
MACHINE_ID = "fake-machine-id"


@dataclass
class FakePlex:
    sections: list[dict] = field(default_factory=lambda: [{"key": "1", "title": "Music", "type": "artist"}])
    items: dict[str, dict] = field(default_factory=dict)  # ratingKey -> item (artists, albums, tracks)
    playlists: dict[str, dict] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    bulk_clear: bool = True  # False = server without DELETE /playlists/{id}/items
    now: int = 1_700_000_000
    _next_key: int = 9000
    _next_item: int = 5000

    # ------------------------------------------------------------------ data
    def add_artist(self, key, title, *, genres=(), styles=(), moods=(), section=1, updated=None):
        self.items[str(key)] = {
            "ratingKey": str(key), "type": "artist", "title": title, "_section": section,
            "Genre": [{"tag": g} for g in genres], "Style": [{"tag": s} for s in styles],
            "Mood": [{"tag": m} for m in moods], "updatedAt": updated or self.now,
        }

    def add_album(self, key, artist_key, title, year, *, genres=(), styles=(), section=1, updated=None):
        self.items[str(key)] = {
            "ratingKey": str(key), "type": "album", "title": title, "year": year,
            "parentRatingKey": str(artist_key), "_section": section,
            "Genre": [{"tag": g} for g in genres], "Style": [{"tag": s} for s in styles],
            "updatedAt": updated or self.now,
        }

    def add_track(self, key, album_key, title, *, duration=200_000, track_artist=None, moods=(),
                  views=0, rating=None, section=1, updated=None, index=1):
        album = self.items[str(album_key)]
        artist = self.items[album["parentRatingKey"]]
        t = {
            "ratingKey": str(key), "type": "track", "title": title, "index": index, "parentIndex": 1,
            "parentRatingKey": album["ratingKey"], "parentTitle": album["title"], "parentYear": album["year"],
            "grandparentRatingKey": artist["ratingKey"], "grandparentTitle": artist["title"],
            "duration": duration, "viewCount": views, "addedAt": self.now - 86400, "_section": section,
            "updatedAt": updated or self.now, "_full": {"Mood": [{"tag": m} for m in moods]},
            "Media": [{"audioCodec": "flac", "bitrate": 900, "Part": [{"file": f"/music/{key}.flac"}]}],
        }
        if track_artist:
            t["originalTitle"] = track_artist
        if rating is not None:
            t["userRating"] = rating
        self.items[str(key)] = t
        return t

    def play(self, key, at):
        t = self.items[str(key)]
        t["viewCount"] = t.get("viewCount", 0) + 1
        t["lastViewedAt"] = at  # note: Plex does not bump updatedAt for plays

    # ------------------------------------------------------------------ http
    def install(self, httpserver) -> str:
        httpserver.expect_request(re.compile(r".*")).respond_with_handler(self._handle)
        return httpserver.url_for("/").rstrip("/")

    def _json(self, mc: dict, status: int = 200) -> Response:
        return Response(json.dumps({"MediaContainer": mc}), status, content_type="application/json")

    @staticmethod
    def _public(item: dict, full: bool) -> dict:
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        if full:
            out.update(item.get("_full", {}))
        return out

    def _handle(self, req: Request) -> Response:
        self.log.append(f"{req.method} {req.full_path}")
        if req.headers.get("X-Plex-Token") != TOKEN:
            return Response("unauthorized", 401)
        path = req.path

        if path == "/" and req.method == "GET":
            return self._json({"machineIdentifier": MACHINE_ID, "friendlyName": "FakePlex", "version": "1.41.0"})
        if path == "/library/sections":
            return self._json({"Directory": self.sections + [{"key": "2", "title": "Movies", "type": "movie"}]})

        m = re.fullmatch(r"/library/sections/(\d+)/all", path)
        if m:
            section = int(m.group(1))
            libtype = {"8": "artist", "9": "album", "10": "track"}[req.args["type"]]
            rows = [i for i in self.items.values() if i["type"] == libtype and i["_section"] == section]
            for fld in ("updatedAt", "lastViewedAt"):
                since = req.args.get(f"{fld}>>")
                if since is not None:
                    rows = [i for i in rows if (i.get(fld) or 0) > int(since)]
            rows.sort(key=lambda i: int(i["ratingKey"]))
            start = int(req.args.get("X-Plex-Container-Start", 0))
            size = int(req.args.get("X-Plex-Container-Size", 100))
            page = rows[start : start + size]
            return self._json({"size": len(page), "totalSize": len(rows),
                               "Metadata": [self._public(i, False) for i in page]})

        m = re.fullmatch(r"/library/metadata/([\d,]+)", path)
        if m:
            keys = m.group(1).split(",")
            return self._json({"Metadata": [self._public(self.items[k], True) for k in keys if k in self.items]})

        if path == "/playlists" and req.method == "POST":
            uri = req.args["uri"]
            assert uri.startswith(f"server://{MACHINE_ID}/com.plexapp.plugins.library/library/metadata/"), uri
            keys = uri.rsplit("/", 1)[1].split(",")
            self._next_key += 1
            pl = {"ratingKey": str(self._next_key), "title": req.args["title"], "leafCount": len(keys),
                  "_keys": keys, "_ids": [self._item_id() for _ in keys], "type": "playlist", "playlistType": "audio"}
            self.playlists[pl["ratingKey"]] = pl
            return self._json({"Metadata": [self._public(pl, False)]})

        m = re.fullmatch(r"/playlists/(\d+)", path)
        if m:
            pl = self.playlists.get(m.group(1))
            if pl is None:
                return Response("not found", 404)
            if req.method == "GET":
                return self._json({"Metadata": [{**self._public(pl, False), "leafCount": len(pl["_keys"])}]})
            if req.method == "PUT":
                for f in ("title", "summary"):
                    if f"{f}.value" in req.args:
                        pl[f] = req.args[f"{f}.value"]
                return Response("", 200)

        m = re.fullmatch(r"/playlists/(\d+)/items(?:/(\d+))?", path)
        if m:
            pl = self.playlists.get(m.group(1))
            if pl is None:
                return Response("not found", 404)
            item_id = m.group(2)
            if req.method == "GET":
                return self._json({"Metadata": [
                    {"ratingKey": k, "playlistItemID": str(i)} for k, i in zip(pl["_keys"], pl["_ids"])]})
            if req.method == "DELETE" and item_id is None:
                if not self.bulk_clear:
                    return Response("not found", 404)
                pl["_keys"], pl["_ids"] = [], []
                return Response("", 200)
            if req.method == "DELETE":
                if int(item_id) not in pl["_ids"]:
                    return Response("not found", 404)
                idx = pl["_ids"].index(int(item_id))
                del pl["_keys"][idx], pl["_ids"][idx]
                return Response("", 200)
            if req.method == "PUT":
                new = req.args["uri"].rsplit("/", 1)[1].split(",")
                pl["_keys"] += new
                pl["_ids"] += [self._item_id() for _ in new]
                return Response("", 200)

        return Response("not found", 404)

    def _item_id(self) -> int:
        self._next_item += 1
        return self._next_item

    def requests_matching(self, pattern: str) -> list[str]:
        return [r for r in self.log if re.search(pattern, r)]


def seed_basic(fp: FakePlex) -> FakePlex:
    """Two artists, three albums, six tracks (one compilation track with a guest artist)."""
    fp.add_artist(1, "Slowdive", genres=["Rock"], styles=["Shoegaze"], moods=["Dreamy"])
    fp.add_artist(2, "Pixies", genres=["Rock"], styles=["Alternative Rock"], moods=["Energetic"])
    fp.add_album(10, 1, "Souvlaki", 1993, genres=["Rock"], styles=["Dream Pop"])
    fp.add_album(11, 2, "Doolittle", 1989, genres=["Rock"])
    fp.add_album(12, 2, "Trompe le Monde", 1991)
    fp.add_track(100, 10, "Alison", moods=["Melancholy"], views=3, rating=10)
    fp.add_track(101, 10, "Machine Gun", index=2)
    fp.add_track(102, 11, "Debaser", views=12, rating=8)
    fp.add_track(103, 11, "Hey", index=2)
    fp.add_track(104, 12, "Planet of Sound")
    fp.add_track(105, 12, "Head On", track_artist="Pixies feat. Guest", index=2)
    return fp
