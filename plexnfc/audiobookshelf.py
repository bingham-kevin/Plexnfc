"""Audiobooks from Audiobookshelf, for the Books section.

Connected in the panel with Audiobookshelf's address and an API key (Settings › Users ›
your account › API Keys in Audiobookshelf 2.26 and later), saved in ``audiobookshelf.json``.

Books are items like the music's (musiclib.make_item), from the source ``abs``:

- ``abs|book|<library item ID>``: an audiobook. Its tracks are its audio files, each
  with ``offset`` (where it starts in the book) so a position in the book can be found.
- ``abs|author|<author ID>`` and ``abs|series|<series ID>``: their books.

Playing a book starts where Audiobookshelf says you left off, and the position is
reported back as it plays (speakers.BookProgress), so its apps stay in step.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote, urlsplit

import requests

from .musiclib import MusicSource, MusicSourceError, art_url, item_id, make_item, split_id

log = logging.getLogger(__name__)

KINDS = ("book", "author", "series")
LIBRARIES_EVERY = 300


class AudiobookshelfError(MusicSourceError):
    pass


def _bare(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise AudiobookshelfError("That isn't a web address, like http://nas.local:13378")
    return url


class Audiobookshelf:
    """The connection: its address, the API key, and the calls Marquee makes."""

    def __init__(self, path: str, url: str = "", token: str = ""):
        self.path = path
        self._lock = threading.Lock()
        saved = {}
        try:
            with open(path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            pass
        self.url = saved.get("url") or url.strip().rstrip("/")
        self.token = saved.get("token") or token.strip()
        self._libraries: tuple[float, list[dict]] | None = None
        self.error: str | None = None
        self.user: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    # ---- the panel ----

    def public(self) -> dict:
        out = {"configured": self.configured, "url": self.url, "user": self.user, "message": None, "libraries": []}
        if self.configured:
            try:
                out["libraries"] = [{"id": lib["id"], "name": lib["name"]} for lib in self.libraries()]
                out["user"] = self.user
            except AudiobookshelfError as e:
                out["message"] = str(e)
        return out

    def set(self, url: str, token: str | None) -> None:
        """Connect to ``url`` with ``token`` (None keeps the one there is); blank ``url``
        disconnects. Raises AudiobookshelfError, keeping the old settings, if it fails."""
        url = _bare(url)
        token = self.token if token is None else token.strip()
        if url:
            if not token:
                raise AudiobookshelfError("Paste an API key made in Audiobookshelf")
            user = self._try(url, token)
        with self._lock:
            self.url, self.token = (url, token) if url else ("", "")
            self.user = user if url else None
            self._libraries = None
            self.error = None
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump({"url": url, "token": token} if url else {}, f)
            os.replace(tmp, self.path)

    @staticmethod
    def _try(url: str, token: str) -> str:
        try:
            r = requests.get(f"{url}/api/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        except requests.RequestException:
            raise AudiobookshelfError(f"Couldn't reach Audiobookshelf at {url}") from None
        if r.status_code in (401, 403):
            raise AudiobookshelfError("Audiobookshelf didn't accept that API key")
        if not r.ok:
            raise AudiobookshelfError(f"Audiobookshelf answered {r.status_code}. Is that its address?")
        try:
            return r.json().get("username") or "?"
        except ValueError:
            raise AudiobookshelfError("That doesn't look like Audiobookshelf") from None

    # ---- calls ----

    def request(self, method: str, path: str, *, raw: bool = False, timeout: float = 15, **kwargs):
        if not self.configured:
            raise AudiobookshelfError("Connect Audiobookshelf under Books › Sources")
        try:
            r = requests.request(method, f"{self.url}{path}", headers={"Authorization": f"Bearer {self.token}"},
                                 timeout=timeout, **kwargs)
        except requests.RequestException:
            raise AudiobookshelfError("Couldn't reach Audiobookshelf") from None
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise AudiobookshelfError("Audiobookshelf didn't accept the API key. Connect it again.")
        if not r.ok:
            raise AudiobookshelfError(f"Audiobookshelf answered {r.status_code}")
        if raw:
            return r
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    def get(self, path: str, **params):
        return self.request("GET", path, params=params)

    def libraries(self) -> list[dict]:
        """The audiobook libraries (podcast libraries are left out)."""
        cached = self._libraries
        if cached and time.monotonic() - cached[0] < LIBRARIES_EVERY:
            return cached[1]
        found = (self.get("/api/libraries") or {}).get("libraries") or []
        books = [lib for lib in found if lib.get("mediaType") == "book"]
        if self.user is None:
            self.user = (self.get("/api/me") or {}).get("username")
        self._libraries = (time.monotonic(), books)
        return books

    def progress(self, book_id: str) -> dict | None:
        """``{"currentTime", "duration", "isFinished"}`` for a book, or None if not started."""
        found = self.get(f"/api/me/progress/{quote(book_id)}")
        return found or None

    def save_progress(self, book_id: str, position: float, duration: float, finished: bool = False) -> None:
        position = max(0.0, min(float(position), float(duration or position)))
        self.request("PATCH", f"/api/me/progress/{quote(book_id)}",
                     json={"currentTime": round(position, 1), "duration": duration,
                           "progress": (position / duration) if duration else 0, "isFinished": finished})

    def file_url(self, book_id: str, ino: str) -> str:
        """An address a speaker can play an audio file from (the key rides along, as
        Audiobookshelf's own apps do for casting)."""
        return f"{self.url}/api/items/{quote(book_id)}/file/{quote(str(ino))}?token={quote(self.token)}"


# ---- the source ---------------------------------------------------------------------

def locate(tracks: list[dict], position: float) -> tuple[int, float]:
    """``(track index, seconds into it)`` for a position in a book."""
    for i, t in enumerate(tracks):
        start = t.get("offset") or 0
        if position < start + (t.get("duration") or 0):
            return i, max(0.0, position - start)
    return max(0, len(tracks) - 1), 0.0


def _sequence(item: dict) -> tuple:
    series = ((item.get("media") or {}).get("metadata") or {}).get("series")
    seq = series.get("sequence") if isinstance(series, dict) else None
    try:
        return (0, float(seq), "")
    except (TypeError, ValueError):
        return (1, 0.0, ((item.get("media") or {}).get("metadata") or {}).get("title") or "")


class AudiobookshelfSource(MusicSource):
    kind = "audiobookshelf"

    def __init__(self, abs_: Audiobookshelf):
        super().__init__("abs", "Audiobookshelf")
        self.abs = abs_
        self._items: dict[str, tuple[float, dict]] = {}  # book ID -> (when, expanded item)

    def public(self) -> dict:
        return {**super().public(), "section": "books", "kinds": list(KINDS)}

    # ---- items ----

    def _book(self, raw: dict) -> dict | None:
        media = raw.get("media") or {}
        meta = media.get("metadata") or {}
        if not raw.get("id") or raw.get("mediaType", "book") != "book":
            return None
        author = meta.get("authorName") or ", ".join(a.get("name", "") for a in meta.get("authors") or [])
        narrator = meta.get("narratorName") or ", ".join(meta.get("narrators") or [])
        series = meta.get("seriesName")
        if not series and isinstance(meta.get("series"), list):
            series = ", ".join(s["name"] + (f" #{s['sequence']}" if s.get("sequence") else "") for s in meta["series"])
        elif not series and isinstance(meta.get("series"), dict):
            s = meta["series"]
            series = s.get("name", "") + (f" #{s['sequence']}" if s.get("sequence") else "")
        year = meta.get("publishedYear")
        return make_item(self.id, "book", raw["id"], meta.get("title"), author, series or "",
                         art_url(item_id(self.id, "book", raw["id"])) if media.get("coverPath") else None,
                         media.get("duration"), narrator=narrator or None, subtitle=meta.get("subtitle") or None,
                         year=int(year) if str(year or "").isdigit() else None,
                         description=meta.get("description") or None)

    def _author(self, raw: dict) -> dict:
        return make_item(self.id, "author", raw["id"], raw.get("name"),
                         image=art_url(item_id(self.id, "author", raw["id"])) if raw.get("imagePath") else None,
                         count=raw.get("numBooks"))

    def _series(self, raw: dict) -> dict:
        books = raw.get("books") or []
        first = min(books, key=_sequence) if books else None
        cover = first and (first.get("media") or {}).get("coverPath")
        return make_item(self.id, "series", raw["id"], raw.get("name"),
                         artist=((first or {}).get("media") or {}).get("metadata", {}).get("authorName") or "",
                         image=art_url(item_id(self.id, "book", first["id"])) if cover else None,
                         count=len(books) or None)

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        if kind not in KINDS:
            return []
        out: list[dict] = []
        for lib in self.abs.libraries():
            lid = quote(lib["id"])
            if query:
                found = self.abs.get(f"/api/libraries/{lid}/search", q=query, limit=max(limit, 25)) or {}
                if kind == "book":
                    out += [b for x in found.get("book") or [] if (b := self._book(x.get("libraryItem") or {}))]
                elif kind == "author":
                    out += [self._author(a) for a in found.get("authors") or []]
                else:
                    out += [self._series({**(x.get("series") or {}), "books": x.get("books") or []})
                            for x in found.get("series") or [] if (x.get("series") or {}).get("id")]
                continue
            page = offset // limit if limit else 0
            if kind == "book":
                found = self.abs.get(f"/api/libraries/{lid}/items", limit=limit, page=page,
                                     sort="media.metadata.title", minified=1) or {}
                out += [b for raw in found.get("results") or [] if (b := self._book(raw))]
            elif kind == "author":
                found = self.abs.get(f"/api/libraries/{lid}/authors") or {}
                authors = sorted(found.get("authors") or [], key=lambda a: (a.get("lastFirst") or a.get("name") or "").casefold())
                out += [self._author(a) for a in authors[offset:offset + limit]]
            else:
                found = self.abs.get(f"/api/libraries/{lid}/series", limit=limit, page=page, sort="name") or {}
                out += [self._series(s) for s in found.get("results") or []]
        return out[:limit] if query else out

    def search(self, query: str, limit: int = 8) -> list[dict]:
        out = []
        for kind in KINDS:
            out += self.browse(kind, query, 0, limit)[:limit]
        return out

    def _expanded(self, book_id: str) -> dict:
        cached = self._items.get(book_id)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        raw = self.abs.get(f"/api/items/{quote(book_id)}", expanded=1)
        if not raw:
            raise AudiobookshelfError("Audiobookshelf doesn't have that book any more")
        if len(self._items) > 200:
            self._items.clear()
        self._items[book_id] = (time.monotonic(), raw)
        return raw

    def _tracks(self, raw: dict, book: dict) -> list[dict]:
        media = raw.get("media") or {}
        files = media.get("tracks") or sorted((f for f in media.get("audioFiles") or [] if not f.get("exclude")),
                                              key=lambda f: f.get("index") or 0)
        out, offset = [], 0.0
        single = len(files) == 1
        for n, f in enumerate(files):
            start = f.get("startOffset")
            start = float(start) if start is not None else offset
            name = book["name"] if single else (f.get("title") or (f.get("metaTags") or {}).get("tagTitle")
                                                or (f.get("metadata") or {}).get("filename") or f"Part {n + 1}")
            name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.I)
            out.append(make_item(self.id, "file", f"{raw['id']}/{f.get('ino')}", name, book["artist"], book["name"],
                                 book["image"], f.get("duration"), n + 1, offset=start, book=raw["id"],
                                 book_duration=book.get("duration"), mime=f.get("mimeType") or "audio/mpeg"))
            offset = start + float(f.get("duration") or 0)
        return out

    def open(self, kind: str, ref: str) -> dict:
        if kind == "book":
            raw = self._expanded(ref)
            book = self._book(raw) or make_item(self.id, "book", ref, ref)
            chapters = [{"title": c.get("title") or f"Chapter {i + 1}", "start": float(c.get("start") or 0),
                         "end": float(c.get("end") or 0)} for i, c in enumerate((raw.get("media") or {}).get("chapters") or [])]
            progress = self.abs.progress(ref)
            return {"item": book, "tracks": self._tracks(raw, book), "chapters": chapters,
                    "progress": {"position": progress.get("currentTime") or 0,
                                 "finished": bool(progress.get("isFinished"))} if progress else None}
        if kind == "author":
            raw = self.abs.get(f"/api/authors/{quote(ref)}", include="items")
            if not raw:
                raise AudiobookshelfError("Audiobookshelf doesn't have that author any more")
            books = [b for x in sorted(raw.get("libraryItems") or [], key=_sequence) if (b := self._book(x))]
            return {"item": self._author(raw), "albums": books}
        if kind == "series":
            books_raw = []
            for lib in self.abs.libraries():
                token = base64.b64encode(ref.encode()).decode()
                found = self.abs.get(f"/api/libraries/{quote(lib['id'])}/items", filter=f"series.{token}",
                                     minified=1, limit=500) or {}
                books_raw += found.get("results") or []
            books_raw.sort(key=_sequence)
            books = [b for x in books_raw if (b := self._book(x))]
            series_name = ""
            if books_raw:
                s = ((books_raw[0].get("media") or {}).get("metadata") or {}).get("series")
                series_name = s.get("name", "") if isinstance(s, dict) else ""
            item = make_item(self.id, "series", ref, series_name or "Series",
                             books[0]["artist"] if books else "", image=books[0]["image"] if books else None,
                             count=len(books))
            return {"item": item, "albums": books}
        if kind == "file":
            return {"item": self.track(ref), "tracks": [self.track(ref)]}
        raise AudiobookshelfError("That isn't a book")

    def tracks(self, kind: str, ref: str) -> list[dict]:
        if kind == "file":
            return [self.track(ref)]
        if kind == "book":
            return self.book_tracks(ref)
        opened = self.open(kind, ref)
        out = []
        for book in opened.get("albums") or []:
            out += self.tracks("book", split_id(book["id"])[2])
        return out

    def book_tracks(self, book_id: str) -> list[dict]:
        raw = self._expanded(book_id)
        return self._tracks(raw, self._book(raw) or make_item(self.id, "book", book_id, book_id))

    def track(self, ref: str) -> dict:
        book_id, _, ino = ref.partition("/")
        for t in self.book_tracks(book_id):
            if t["id"].endswith(f"/{ino}"):
                return t
        raise AudiobookshelfError("That part of the book isn't there any more")

    def stream(self, track: dict) -> tuple[str, str]:
        book_id, _, ino = split_id(track["id"])[2].partition("/")
        return self.abs.file_url(book_id, ino), track.get("mime") or "audio/mpeg"

    def art(self, kind: str, ref: str) -> tuple[bytes, str] | None:
        if kind == "file":
            kind, ref = "book", ref.partition("/")[0]
        path = {"book": f"/api/items/{quote(ref)}/cover", "author": f"/api/authors/{quote(ref)}/image"}.get(kind)
        if kind == "series":
            books = self.open("series", ref).get("albums") or []
            return self.art("book", split_id(books[0]["id"])[2]) if books else None
        if not path:
            return None
        try:
            r = self.abs.request("GET", path, raw=True, params={"width": 400})
        except AudiobookshelfError:
            return None
        return (r.content, r.headers.get("Content-Type", "image/jpeg")) if r is not None else None

    # ---- where you are in a book ----

    def resume_point(self, book_id: str) -> float:
        """Where to start: where you left off, or the start if it's finished or new."""
        try:
            progress = self.abs.progress(book_id)
        except AudiobookshelfError as e:
            log.info("Couldn't read where you are in %s: %s", book_id, e)
            return 0.0
        if not progress or progress.get("isFinished"):
            return 0.0
        return float(progress.get("currentTime") or 0)
