"""Movie cards: blank NFC cards linked to movies by their ID, in place of written tags.

A card's ID (its UID, such as ``04-F3-21-B8-49-02-89``) is fixed at the factory, so
nothing has to be written onto it. The Movie Remote sends the ID of every card it
reads, and Marquee Server looks the movie up here.

Cards are linked in the control panel: choose movies (a collection, a label, or by
hand) and start a scan. The panel shows the first movie; hold its card on a Movie
Remote, and it's linked and the next movie comes up.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

UID_RE = re.compile(r"^[0-9A-F]{2}(-[0-9A-F]{2}){3,9}$")


def normalize_uid(uid: str) -> str | None:
    """``04:f3:21:b8`` or ``04f321b8`` -> ``04-F3-21-B8``; None if it isn't a card ID."""
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", uid or "").upper()
    if len(hex_only) % 2 or not 8 <= len(hex_only) <= 20:
        return None
    uid = "-".join(hex_only[i:i + 2] for i in range(0, len(hex_only), 2))
    return uid if UID_RE.match(uid) else None


def music_title(item: dict) -> str:
    """``Abbey Road - The Beatles``"""
    return item["name"] + (f" - {item['artist']}" if item.get("artist") else "")


class Cards:
    """Which movie each card plays, saved in ``path``; and the scan in progress, if any."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        try:
            with open(path) as f:
                self._cards: dict[str, dict] = json.load(f).get("cards", {})
        except (OSError, ValueError):
            self._cards = {}
        self._scan: dict | None = None
        # The last card read that isn't linked (``{"uid", "at"}``), to link from the panel
        self.unknown: dict | None = None

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"cards": self._cards}, f, indent=1)
        os.replace(tmp, self.path)

    # ---- the cards ------------------------------------------------------------

    def card(self, uid: str) -> dict | None:
        """What a card plays: a movie (``{"key", "title"}``) or music or a book (with
        ``music``; an old Music Assistant card also has ``speaker`` and ``speaker_name``), or None."""
        with self._lock:
            card = self._cards.get(normalize_uid(uid) or "")
            return dict(card) if card else None

    def movie(self, uid: str) -> dict | None:
        """The movie a card plays: ``{"key", "title"}``, or None (for music too)."""
        card = self.card(uid)
        return card if card and "music" not in card else None

    def all(self) -> list[dict]:
        with self._lock:
            return [{"uid": uid, **card} for uid, card in sorted(self._cards.items(), key=lambda c: c[1]["title"])]

    def by_movie(self) -> dict[str, list[str]]:
        """Movie key -> the IDs of its cards."""
        out: dict[str, list[str]] = {}
        with self._lock:
            for uid, card in self._cards.items():
                if "music" not in card:
                    out.setdefault(card["key"], []).append(uid)
        return out

    def link(self, uid: str, key: str, title: str, year: int | None = None) -> str | None:
        """Make the card play this movie. Returns the title it played before, if another.
        The title and year find the movie again if its key changes (in another source)."""
        uid = normalize_uid(uid)
        if uid is None:
            raise ValueError("That isn't a card ID")
        with self._lock:
            before = self._cards.get(uid)
            self._cards[uid] = {"key": key, "title": title, "linked": int(time.time()),
                                **({"year": int(year)} if year else {})}
            self._save()
        return before["title"] if before and before["key"] != key else None

    def link_music(self, uid: str, item: dict, speaker: str | None = None,
                   speaker_name: str | None = None) -> str | None:
        """Make the card play music or a book (a ``music_item``). It plays on the speakers of
        the reader it's held to; only an old Music Assistant card (no ``id``) keeps a speaker
        of its own. Returns the title it played before, if another."""
        uid = normalize_uid(uid)
        if uid is None:
            raise ValueError("That isn't a card ID")
        speaker = str(speaker or "").strip() if not item.get("id") else ""
        if not item.get("id") and not speaker:  # a Music Assistant player ID (or, from before, an entity)
            raise ValueError("Choose a speaker")
        title = music_title(item)
        with self._lock:
            before = self._cards.get(uid)
            self._cards[uid] = {"key": f"music:{item.get('id') or item['uri']}", "title": title, "music": item,
                                **({"speaker": speaker, "speaker_name": speaker_name or speaker} if speaker else {}),
                                "linked": int(time.time())}
            self._save()
        return before["title"] if before and before["title"] != title else None

    def remove(self, uid: str) -> bool:
        with self._lock:
            if self._cards.pop(normalize_uid(uid) or "", None) is None:
                return False
            self._save()
            return True

    # ---- scanning a batch of cards ------------------------------------------------

    def start_scan(self, movies: list[dict]) -> dict:
        """Link the next cards read to these movies (``{"key", "title"}``), in order. An item
        can be music or a book instead: ``{"music": <music_item>}`` (with ``speaker`` and
        ``speaker_name`` for an old Music Assistant item)."""
        movies = [{"key": f"music:{m['music'].get('id') or m['music']['uri']}", "title": music_title(m["music"]), "music": m["music"],
                   **({"speaker": str(m["speaker"]), "speaker_name": str(m.get("speaker_name") or m["speaker"])}
                      if m.get("speaker") else {})}
                  if "music" in m else
                  {"key": str(m["key"]), "title": str(m.get("title") or m["key"]),
                   **({"year": m["year"]} if isinstance(m.get("year"), int) else {})}
                  for m in movies if m.get("key") or "music" in m]
        if not movies:
            raise ValueError("Choose at least one movie")
        with self._lock:
            self._scan = {"movies": movies, "index": 0, "linked": 0, "last": None, "started": time.time()}
        return self.scan()

    def stop_scan(self) -> None:
        with self._lock:
            self._scan = None

    def move(self, by: int) -> dict | None:
        """Skip ahead (1) or go back (-1) in the scan."""
        with self._lock:
            if self._scan:
                self._scan["index"] = max(0, min(len(self._scan["movies"]), self._scan["index"] + by))
                self._scan["last"] = None
        return self.scan()

    def scan(self) -> dict | None:
        """The scan in progress: where it is, the movie whose card is wanted next, and
        what the last card did; None if there's no scan."""
        with self._lock:
            if self._scan is None:
                return None
            s = self._scan
            index, movies = s["index"], s["movies"]
            return {"index": index, "total": len(movies), "linked": s["linked"],
                    "current": movies[index] if index < len(movies) else None,
                    "next": movies[index + 1] if index + 1 < len(movies) else None,
                    "done": index >= len(movies), "last": s["last"]}

    def scanned(self, uid: str) -> dict | None:
        """A card read during a scan: link it to the current movie and move on.

        Returns what to show on the box (``{"message", "result"}``), or None if no scan is
        going. ``result`` is ``linked``, ``repeat`` (the card just linked, read again),
        ``done`` (every movie has its card) or ``unreadable``.
        """
        uid = normalize_uid(uid)
        with self._lock:
            s = self._scan
            if s is None:
                return None
            if s["index"] >= len(s["movies"]):
                return {"message": "All the cards are linked.\nStop the scan in the control panel.",
                        "result": "done"}
            last = s["last"]
            if uid is None:
                return {"message": "Couldn't read that card's ID.\nTry it again.", "result": "unreadable"}
            # The card that was just linked, read again (it was held there): don't link it to
            # the next movie as well
            if last and last.get("uid") == uid and last.get("result") == "linked":
                return {"message": f"That card is {last['title']}.\nUse the next card.", "result": "repeat"}
            movie = s["movies"][s["index"]]
        if "music" in movie:
            moved_from = self.link_music(uid, movie["music"], movie.get("speaker"), movie.get("speaker_name"))
        else:
            moved_from = self.link(uid, movie["key"], movie["title"], movie.get("year"))
        with self._lock:
            if self._scan is not s:
                return None  # stopped meanwhile; the card is linked all the same
            s["index"] += 1
            s["linked"] += 1
            s["last"] = {"uid": uid, "title": movie["title"], "key": movie["key"], "result": "linked",
                         "moved_from": moved_from, "at": time.time()}
            upcoming = s["movies"][s["index"]]["title"] if s["index"] < len(s["movies"]) else None
        message = f"Linked: {movie['title']}"
        if moved_from:
            message += f"\n(was {moved_from})"
        message += f"\n\nNext: {upcoming}" if upcoming else "\n\nThat's the last one."
        return {"message": message, "result": "linked"}
