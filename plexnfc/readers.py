"""NFC readers and where they play.

A reader is a Movie Remote box (``remote:<its pairing ID>``), a paired Home Assistant
(``home:<its pairing ID>``, kind ``assistant``: every NFC reader in it, phones with the
Home Assistant app included), or one NFC reader in Home Assistant, such as a Tessera screen
(``ha:<its Home Assistant device ID>``). A reader in Home Assistant uses its Home
Assistant's settings for whatever it hasn't been given its own. Each has:

- a **default output**, chosen in the control panel: the speakers (one, or several played
  together) that music and book cards play on when they're held to it;
- its **current speakers**, which start as the default and change when they're chosen on
  the reader itself. They're remembered (the "last settings used") until they're chosen
  again, put back to the default, or made the new default;
- for Home Assistant readers, the Marquee player its movie cards go to, and the page of a
  Tessera screen that shows its controls (opened when a card is read there);
- the Marquee screen (an Apple TV) in its room, which shows Now Playing (the cover, the
  timeline, the lyrics) when a music or book card is read; ``wake`` turns the TV on for it,
  else it only shows when Marquee is already open.

What a reader controls is whatever is playing on its current speakers.
"""
from __future__ import annotations

import json
import os
import threading
import time

KINDS = ("remote", "assistant", "homeassistant")


class ReaderError(Exception):
    """Something the panel or the reader should be told."""


def _speakers(value) -> list[str]:
    if not isinstance(value, list):
        raise ReaderError("Speakers are a list")
    out = []
    for s in value:
        if isinstance(s, str) and s and s not in out:
            out.append(s)
    return out


class Readers:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._readers: dict[str, dict] = {}
        self.version = 0  # goes up with every change, so Home Assistant hears of it
        try:
            with open(path) as f:
                data = json.load(f)
            self._readers = {k: v for k, v in (data.get("readers") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.version += 1
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"readers": self._readers}, f, indent=1)
        os.replace(tmp, self.path)

    # ---- which readers there are ----

    def seen(self, reader_id: str, name: str | None, kind: str, touch: bool = True) -> dict:
        """A reader read a card (or asked for its state): know it, under its latest name.
        ``touch`` notes when it was last heard from."""
        if kind not in KINDS:
            raise ReaderError("Unknown kind of reader")
        with self._lock:
            reader = self._readers.get(reader_id)
            changed = False
            if reader is None:
                reader = self._readers[reader_id] = {"kind": kind, "name": name or reader_id, "default": [],
                                                     "speakers": None, "movie_player": None, "page": None,
                                                     "screen": None, "wake": False}
                changed = True
            elif name and reader.get("name") != name:
                reader["name"] = name
                changed = True
            if touch:
                reader["seen"] = time.time()
            if changed:
                self._save()
            return self._public(reader_id, reader)

    def get(self, reader_id: str) -> dict | None:
        with self._lock:
            reader = self._readers.get(reader_id)
            return self._public(reader_id, reader) if reader else None

    def all(self) -> list[dict]:
        with self._lock:
            return sorted((self._public(k, v) for k, v in self._readers.items()),
                          key=lambda r: (KINDS.index(r["kind"]), r["name"].casefold()))

    def remove(self, reader_id: str) -> None:
        with self._lock:
            if self._readers.pop(reader_id, None) is not None:
                self._save()

    @staticmethod
    def _public(reader_id: str, reader: dict) -> dict:
        default = list(reader.get("default") or [])
        current = reader.get("speakers")
        current = list(default if current is None else current)
        return {"id": reader_id, "kind": reader["kind"], "name": reader.get("name") or reader_id,
                "default": default, "speakers": current, "is_default": current == default,
                "movie_player": reader.get("movie_player"), "page": reader.get("page"),
                "screen": reader.get("screen"), "wake": bool(reader.get("wake")), "seen": reader.get("seen")}

    # ---- the control panel: the default output ----

    def configure(self, reader_id: str, body: dict) -> dict:
        """Set ``default`` (speakers), ``movie_player``, ``page`` (a Tessera page number, or
        None), ``screen`` (a Marquee's name, or None) and ``wake``, whichever are given. A new
        default is also what the reader plays on now."""
        with self._lock:
            reader = self._readers.get(reader_id)
            if reader is None:
                raise ReaderError("Marquee doesn't know that reader")
            if "default" in body:
                reader["default"] = _speakers(body["default"])
                reader["speakers"] = None
            if "movie_player" in body:
                player = body["movie_player"]
                if player is not None and not (isinstance(player, str) and player):
                    raise ReaderError("Choose a Marquee player, or none for the default one")
                reader["movie_player"] = player
            if "page" in body:
                page = body["page"]
                if page is not None and not (isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 99):
                    raise ReaderError("A Tessera page is a number from 1 to 99")
                reader["page"] = page
            if "screen" in body:
                screen = body["screen"]
                if screen is not None and not (isinstance(screen, str) and screen):
                    raise ReaderError("Choose a Marquee screen, or none")
                reader["screen"] = screen
            if "wake" in body:
                reader["wake"] = bool(body["wake"])
            self._save()
            return self._public(reader_id, reader)

    # ---- the reader itself ----

    def choose(self, reader_id: str, speakers: list[str]) -> dict:
        """Play on these speakers from now on (remembered)."""
        speakers = _speakers(speakers)
        if not speakers:
            raise ReaderError("Choose at least one speaker")
        with self._lock:
            reader = self._reader(reader_id)
            reader["speakers"] = speakers
            self._save()
            return self._public(reader_id, reader)

    def restore(self, reader_id: str) -> dict:
        """Back to the default output."""
        with self._lock:
            reader = self._reader(reader_id)
            if not reader.get("default"):
                raise ReaderError("This reader has no default speakers yet. Choose them in the control panel, "
                                  "or make the ones it uses now the default.")
            reader["speakers"] = None
            self._save()
            return self._public(reader_id, reader)

    def make_default(self, reader_id: str) -> dict:
        """The speakers it uses now become its default."""
        with self._lock:
            reader = self._reader(reader_id)
            current = reader.get("speakers")
            if current is None:
                return self._public(reader_id, reader)
            if not current:
                raise ReaderError("Choose at least one speaker first")
            reader["default"] = list(current)
            reader["speakers"] = None
            self._save()
            return self._public(reader_id, reader)

    def _reader(self, reader_id: str) -> dict:
        reader = self._readers.get(reader_id)
        if reader is None:
            raise ReaderError("Marquee doesn't know that reader")
        return reader
