"""Music cards: NFC cards that play an album, playlist, artist, track or radio station
through Music Assistant, on a speaker chosen when the card was linked.

There are two ways to reach Music Assistant, and either will do:

- **Directly** (``MusicAssistant``): its own API on port 8095, with a long-lived token
  made in Music Assistant. Home Assistant isn't needed at all.
- **Through Home Assistant** (``MusicBridge``): Marquee Server needs no Home Assistant
  token for this. The Marquee integration in Home Assistant (``custom_components/marquee``)
  keeps a request open to ``/ha/music/poll``, sending the Music Assistant speakers it can
  see each time; the server answers with jobs (``search`` or ``play``), the integration
  runs them through Music Assistant's own actions and posts each result back to
  ``/ha/music/jobs/<id>``.

``Music`` picks one: the direct connection when it's set up and working, else Home
Assistant. A card keeps its speaker's Music Assistant player ID, which both know.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
from urllib.parse import quote, urlsplit

import requests

log = logging.getLogger(__name__)

# A Home Assistant that polled this recently is connected (it polls every 25 s at most)
CONNECTED_FOR = 60
MEDIA_TYPES = ("album", "playlist", "artist", "track", "radio", "audiobook", "podcast")


class MusicError(Exception):
    pass


class MusicSlow(MusicError):
    """Home Assistant hasn't answered yet; the job is still waiting for it."""


def music_item(item: dict) -> dict:
    """The parts of a Music Assistant item a card keeps: ``uri``, ``media_type``, ``name``,
    ``artist`` and ``image``. Raises ValueError if it isn't one."""
    if not isinstance(item, dict):
        raise ValueError("Choose some music")
    uri, media_type = str(item.get("uri") or ""), str(item.get("media_type") or "")
    name = str(item.get("name") or "").strip()
    if not uri or "://" not in uri or len(uri) > 500 or media_type not in MEDIA_TYPES or not name:
        raise ValueError("That isn't a Music Assistant item")
    image = item.get("image")
    return {"uri": uri, "media_type": media_type, "name": name[:200],
            "artist": str(item.get("artist") or "")[:200],
            "image": image if isinstance(image, str) and image.startswith(("http://", "https://")) else None}


class MusicBridge:
    """Jobs for the Marquee integration in Home Assistant, and the speakers it reported."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._jobs: list[dict] = []  # waiting to be picked up
        self._results: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._seen = 0.0
        self._speakers: list[dict] = []
        self._assistant = False  # whether Home Assistant has Music Assistant

    # ---- the integration's side ---------------------------------------------------

    def poll(self, speakers: list, assistant: bool, wait: float) -> list[dict]:
        """Note what Home Assistant has, then return the jobs waiting, waiting up to ``wait``
        seconds for one."""
        clean = [{"entity_id": str(s["entity_id"]), "name": str(s.get("name") or s["entity_id"]),
                  "player_id": str(s.get("player_id") or "")}
                 for s in speakers if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("media_player.")]
        deadline = time.monotonic() + max(0.0, min(wait, 25))
        with self._cond:
            self._seen = time.monotonic()
            self._speakers = sorted(clean, key=lambda s: s["name"].lower())
            self._assistant = bool(assistant)
            while not self._jobs and (remaining := deadline - time.monotonic()) > 0:
                self._cond.wait(remaining)
                self._seen = time.monotonic()
            jobs, self._jobs = self._jobs, []
            return jobs

    def finish(self, job_id: int, ok: bool, result=None, message: str | None = None) -> bool:
        with self._cond:
            if job_id in self._results:
                return False  # answered already, or nobody's waiting any more
            self._results[job_id] = {"ok": bool(ok), "result": result, "message": message}
            self._cond.notify_all()
            return True

    # ---- Marquee Server's side ----------------------------------------------------

    def status(self) -> dict:
        with self._cond:
            connected = time.monotonic() - self._seen < CONNECTED_FOR if self._seen else False
            return {"connected": connected, "music_assistant": connected and self._assistant,
                    "speakers": list(self._speakers) if connected else []}

    def speaker_name(self, entity_id: str) -> str | None:
        with self._cond:
            return next((s["name"] for s in self._speakers if s["entity_id"] == entity_id), None)

    def entity_for(self, speaker: str) -> str | None:
        """Home Assistant's entity for a speaker (a Music Assistant player ID, or an entity)."""
        with self._cond:
            for s in self._speakers:
                if speaker in (s["entity_id"], s["player_id"]):
                    return s["entity_id"]
        return speaker if speaker.startswith("media_player.") else None

    def player_for(self, speaker: str) -> str | None:
        """Music Assistant's player ID for a speaker (an entity, or a player ID already)."""
        if not speaker.startswith("media_player."):
            return speaker
        with self._cond:
            return next((s["player_id"] for s in self._speakers if s["entity_id"] == speaker and s["player_id"]), None)

    def speakers(self) -> list[dict]:
        """``[{"id", "name"}]``, ``id`` being the Music Assistant player ID where known."""
        with self._cond:
            return [{"id": s["player_id"] or s["entity_id"], "name": s["name"]} for s in self._speakers]

    def request(self, kind: str, data: dict, timeout: float = 10, keep: bool = False):
        """Have Home Assistant run a job and return its result. Raises MusicError, or with
        ``keep``, MusicSlow if it hasn't answered in time (the job still runs)."""
        status = self.status()
        if not status["connected"]:
            raise MusicError("Home Assistant isn't connected. Add the Marquee integration in Home Assistant.")
        if not status["music_assistant"]:
            raise MusicError("Home Assistant doesn't have Music Assistant set up.")
        deadline = time.monotonic() + timeout
        with self._cond:
            job_id = next(self._ids)
            self._jobs.append({"id": job_id, "kind": kind, "data": data})
            self._cond.notify_all()
            while job_id not in self._results and (remaining := deadline - time.monotonic()) > 0:
                self._cond.wait(remaining)
            answer = self._results.pop(job_id, None)
            if answer is None:
                if keep:
                    raise MusicSlow("Home Assistant hasn't answered yet.")
                self._jobs = [j for j in self._jobs if j["id"] != job_id]
                self._results[job_id] = {}  # a late answer is dropped
                raise MusicError("Home Assistant didn't answer in time.")
        if not answer["ok"]:
            raise MusicError(answer.get("message") or "Music Assistant couldn't do that.")
        return answer.get("result")

    def search(self, query: str) -> list[dict]:
        found = self.request("search", {"query": query}, timeout=15) or []
        items = []
        for item in found if isinstance(found, list) else []:
            try:
                items.append(music_item(item))
            except ValueError:
                continue
        return items

    def play(self, item: dict, speaker: str, timeout: float = 4) -> bool:
        """Start the music on a speaker. Returns True once it's playing, False if Home
        Assistant is still on it after ``timeout`` (the box only waits a few seconds)."""
        entity = self.entity_for(speaker)
        if entity is None:
            raise MusicError("Home Assistant doesn't have that speaker.")
        try:
            self.request("play", {"uri": item["uri"], "media_type": item["media_type"], "speaker": entity},
                         timeout=timeout, keep=True)
            return True
        except MusicSlow:
            return False


class MusicAssistant:
    """Music Assistant's own API: ``POST <url>/api`` with ``{"command", "args"}`` and a
    long-lived token (made in Music Assistant, under your profile)."""

    def __init__(self, url: str = "", token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def call(self, command: str, timeout: float = 10, **args):
        try:
            r = requests.post(f"{self.url}/api", json={"command": command, "args": args}, timeout=timeout,
                              headers={"Authorization": f"Bearer {self.token}"})
        except requests.RequestException as e:
            raise MusicError(f"Couldn't reach Music Assistant at {self.url}") from e
        if r.status_code in (401, 403):
            raise MusicError("Music Assistant didn't accept the token. Make a new one in Music Assistant.")
        if r.status_code != 200:
            raise MusicError(f"Music Assistant couldn't do that: {r.text.strip()[:200] or r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise MusicError("Music Assistant sent something unexpected") from e

    def players(self) -> list[dict]:
        """``[{"id", "name"}]``: the speakers Music Assistant can play on now."""
        found = self.call("players/all", timeout=6) or []
        out = []
        for p in found if isinstance(found, list) else []:
            if not isinstance(p, dict) or not p.get("player_id") or p.get("enabled") is False:
                continue
            if p.get("available") is False or p.get("hidden") or p.get("hide_in_ui"):
                continue
            out.append({"id": str(p["player_id"]), "name": str(p.get("display_name") or p.get("name") or p["player_id"])})
        return sorted(out, key=lambda s: s["name"].lower())

    def image_url(self, item: dict) -> str | None:
        """An item's picture, as a URL a browser can load."""
        image = item.get("image")
        if isinstance(image, str):
            return image
        # Prefer a thumbnail on the open web: the proxy can't fetch every provider's own paths
        thumbs = [i for i in ([image] if isinstance(image, dict) else []) + list((item.get("metadata") or {}).get("images") or [])
                  if isinstance(i, dict) and i.get("type", "thumb") == "thumb" and i.get("path")]
        image = next((i for i in thumbs if i.get("remotely_accessible") and str(i["path"]).startswith(("http://", "https://"))),
                     thumbs[0] if thumbs else None)
        if not image or not image.get("path"):
            return None
        path = str(image["path"])
        if image.get("remotely_accessible") and path.startswith(("http://", "https://")):
            return path
        if image.get("proxy_id"):  # Music Assistant 2.10 and later
            return f"{self.url}/imageproxy/{quote(str(image['proxy_id']), safe='')}?size=256"
        return f"{self.url}/imageproxy?path={quote(quote(path, safe=''), safe='')}" \
               f"&provider={quote(str(image.get('provider') or ''), safe='')}&size=256"

    def search(self, query: str) -> list[dict]:
        found = self.call("music/search", timeout=15, search_query=query, media_types=list(SEARCH_TYPES), limit=6)
        items = []
        for kind in ("albums", "playlists", "artists", "tracks", "radio"):
            for item in (found or {}).get(kind) or [] if isinstance(found, dict) else []:
                if not isinstance(item, dict):
                    continue
                artists = item.get("artists") or []
                try:
                    items.append(music_item({
                        "uri": item.get("uri"), "media_type": item.get("media_type"),
                        # "Abbey Road (2019 Mix)": albums often come in several versions
                        "name": f"{item.get('name')} ({item['version']})" if item.get("version") and item.get("name")
                                else item.get("name"),
                        "artist": ", ".join(a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")),
                        "image": self.image_url(item),
                    }))
                except ValueError:
                    continue
        return items

    def play(self, item: dict, player_id: str, timeout: float = 4) -> bool:
        """Start the music on a player. Returns True once Music Assistant says it's started,
        False if it's still on it after ``timeout``; it carries on either way."""
        done = threading.Event()
        outcome: dict = {}

        def run():
            try:
                self.call("player_queues/play_media", timeout=60, queue_id=player_id, media=item["uri"], option="replace")
            except MusicError as e:
                outcome["error"] = e
                if not done.is_set():
                    log.warning("Music Assistant couldn't play %s: %s", item.get("uri"), e)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(timeout):
            return False
        if "error" in outcome:
            raise outcome["error"]
        return True


SEARCH_TYPES = ("album", "playlist", "artist", "track", "radio")
CHECK_EVERY = 30  # how often the direct connection is checked again


class Music:
    """Music cards' way to Music Assistant: directly if that's set up and works, else
    through Home Assistant."""

    def __init__(self, bridge: MusicBridge, settings_path: str, url: str = "", token: str = "") -> None:
        self.bridge = bridge
        self.path = settings_path
        self._lock = threading.Lock()
        saved = self._load()
        self.assistant = MusicAssistant(saved.get("url", url), saved.get("token", token))
        self._checked = 0.0
        self._players: list[dict] = []
        self._error: str | None = None

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # ---- the direct connection ----------------------------------------------------

    def set_assistant(self, url: str, token: str | None) -> None:
        """Connect to Music Assistant at ``url`` (blank: don't), with ``token`` (None keeps
        the one there is). Raises MusicError, keeping the old settings, if it doesn't work."""
        url = url.strip().rstrip("/")
        if url and "://" not in url:
            url = f"http://{url}"
        if url:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise MusicError("That isn't a web address, like http://homeassistant.local:8095")
            if not urlsplit(url).port and parts.scheme == "http":
                url = f"{url}:8095"
        token = self.assistant.token if token is None else token.strip()
        trial = MusicAssistant(url, token)
        players: list[dict] = []
        if url:
            if not token:
                raise MusicError("Paste a token made in Music Assistant")
            players = trial.players()
        with self._lock:
            self.assistant = trial
            self._players, self._error, self._checked = players, None, time.monotonic()
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                json.dump({"url": url, "token": token} if url else {}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def _direct(self, fresh: bool = False) -> MusicAssistant | None:
        """The direct connection, if it's set up and answered lately."""
        with self._lock:
            assistant = self.assistant
            due = fresh or time.monotonic() - self._checked > CHECK_EVERY
        if not assistant.configured:
            return None
        if due:
            try:
                players, error = assistant.players(), None
            except MusicError as e:
                players, error = [], str(e)
            with self._lock:
                if self.assistant is assistant:
                    self._players, self._error, self._checked = players, error, time.monotonic()
        with self._lock:
            return assistant if self._error is None and self.assistant is assistant else None

    # ---- what the cards use -------------------------------------------------------

    def status(self) -> dict:
        direct = self._direct()
        ha = self.bridge.status()
        with self._lock:
            assistant = {"url": self.assistant.url, "token": bool(self.assistant.token),
                         "connected": direct is not None, "message": self._error}
            players = list(self._players)
        if direct is not None:
            return {"connected": True, "music_assistant": True, "via": "music_assistant",
                    "speakers": players, "assistant": assistant, "home_assistant": ha["connected"]}
        return {"connected": ha["connected"], "music_assistant": ha["music_assistant"],
                "via": "home_assistant" if ha["music_assistant"] else None,
                "speakers": self.bridge.speakers() if ha["connected"] else [],
                "assistant": assistant, "home_assistant": ha["connected"]}

    def speaker_name(self, speaker: str) -> str | None:
        with self._lock:
            name = next((p["name"] for p in self._players if p["id"] == speaker), None)
        return name or self.bridge.speaker_name(self.bridge.entity_for(speaker) or speaker)

    def search(self, query: str) -> list[dict]:
        if direct := self._direct():
            return direct.search(query)
        if self.assistant.configured and not self.bridge.status()["connected"]:
            raise MusicError(self._error or "Music Assistant isn't connected")
        return self.bridge.search(query)

    def play(self, item: dict, speaker: str, timeout: float = 4) -> bool:
        """Start a card's music. True once it's playing, False if it's still starting."""
        if direct := self._direct():
            with self._lock:  # a player ID can look like an entity ("media_player.x", from Home Assistant's player provider)
                known = any(p["id"] == speaker for p in self._players)
            player = speaker if known else self.bridge.player_for(speaker)
            if player:
                return direct.play(item, player, timeout)
        if self.assistant.configured and not self.bridge.status()["connected"]:
            raise MusicError(self._error or "Music Assistant isn't connected")
        return self.bridge.play(item, speaker, timeout)

    def plays_here(self) -> bool:
        """Whether this server plays music cards itself (rather than Home Assistant)."""
        return self._direct() is not None
