"""Matching folder movies with TMDB, and the changes made to them in the control panel.

A folder movie is named from its file (``Alien (1979).mkv``) or a ``.nfo`` beside it.
The control panel can also match it with a movie on TMDB (The Movie Database, which
Plex and Jellyfin use too), which gives it TMDB's title, year, summary, IDs, collection
and poster, or change its details by hand. Those changes are kept here, in
``folder_metadata.json`` in Marquee Server's data folder, never written to the share.

TMDB needs an API key (free from themoviedb.org, under Settings › API), which the user
pastes into the panel; it's kept in ``sources.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata

import requests

log = logging.getLogger(__name__)

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGES = "https://image.tmdb.org/t/p"
# What an edit can change; "matched" says how it was matched ("manual" or "auto")
EDIT_FIELDS = ("title", "year", "summary", "duration", "ids", "collections", "labels", "poster_path", "matched")


class MetadataError(Exception):
    """TMDB couldn't be reached or refused; the message says why."""


def plain_title(title: str) -> str:
    """Letters and digits only, lowercase: "Alien: Covenant" -> "aliencovenant"."""
    plain = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", plain.replace("&", "and"))


class Tmdb:
    """TMDB's movie search and details. Takes a v3 API key or a v4 read access token."""

    def __init__(self, key: str):
        self.key = key.strip()

    def _get(self, path: str, **params) -> dict:
        headers = {"Accept": "application/json"}
        if self.key.startswith("eyJ"):  # a v4 read access token (a JWT)
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        try:
            r = requests.get(f"{TMDB_API}{path}", params=params, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise MetadataError(f"Couldn't reach TMDB: {e}") from e
        if r.status_code == 401:
            raise MetadataError("TMDB didn't accept the API key")
        if r.status_code == 429:
            raise MetadataError("TMDB says too many requests; try again in a moment")
        if not r.ok:
            raise MetadataError(f"TMDB answered {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise MetadataError("TMDB's answer couldn't be read") from e

    def search(self, query: str, year: int | None = None) -> list[dict]:
        """Movies matching a title (and year), best first: id, title, year, summary, poster_path."""
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        results = self._get("/search/movie", **params).get("results") or []
        if not results and year:  # the file's year can be off by one
            results = self._get("/search/movie", query=query, include_adult="false").get("results") or []
        return [{"id": r["id"], "title": r.get("title") or r.get("original_title") or "",
                 "year": _year(r.get("release_date")), "summary": r.get("overview") or "",
                 "poster_path": r.get("poster_path")} for r in results if r.get("id")]

    def details(self, tmdb_id: int | str) -> dict:
        """What a match gives a movie, as an edit: title, year, summary, duration (ms), IDs,
        collections and poster_path."""
        d = self._get(f"/movie/{int(tmdb_id)}")
        ids = {"tmdb": str(d["id"])}
        if d.get("imdb_id"):
            ids["imdb"] = d["imdb_id"]
        collection = (d.get("belongs_to_collection") or {}).get("name")
        return {"title": d.get("title") or d.get("original_title") or "", "year": _year(d.get("release_date")),
                "summary": d.get("overview") or "", "duration": int(d.get("runtime") or 0) * 60_000,
                "ids": ids, "collections": [collection] if collection else [],
                "poster_path": d.get("poster_path")}

    def best_match(self, title: str, year: int | None) -> dict | None:
        """The one search result that's surely this movie: the same title and a year within one
        (or, without a year, the only movie with that title); else None."""
        plain = plain_title(title)
        if not plain:
            return None
        same = [r for r in self.search(title, year) if plain_title(r["title"]) == plain]
        if year:
            close = [r for r in same if r["year"] and abs(r["year"] - year) <= 1]
            exact = [r for r in close if r["year"] == year]
            return (exact or close)[0] if len(exact) == 1 or (not exact and len(close) == 1) else None
        return same[0] if len(same) == 1 else None


def poster_url(poster_path: str, width: int | None = None) -> str:
    size = "w342" if width and width <= 342 else "w780"
    return f"{TMDB_IMAGES}/{size}{poster_path}"


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


class MovieEdits:
    """The changes made to folder movies, by their ``file-…`` key."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "folder_metadata.json")
        self._lock = threading.Lock()
        try:
            with open(self.path) as f:
                self._edits: dict[str, dict] = json.load(f)
        except (OSError, ValueError):
            self._edits = {}

    def get(self, source_key: str) -> dict | None:
        return self._edits.get(source_key)

    def set(self, source_key: str, edit: dict) -> dict:
        edit = {k: v for k, v in edit.items() if k in EDIT_FIELDS}
        with self._lock:
            self._edits[source_key] = edit
            self._save()
        return edit

    def remove(self, source_key: str) -> bool:
        with self._lock:
            if self._edits.pop(source_key, None) is None:
                return False
            self._save()
        return True

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self._edits, f, indent=1)
        os.replace(tmp, self.path)
