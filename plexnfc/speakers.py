"""Speakers music plays on, and what's playing on them.

Three kinds, all with IDs like ``kind:ref``:

- ``ma:<player_id>``: Music Assistant's players (AirPlay, Cast, Sonos, Snapcast...).
  Music Assistant keeps their queues and groups them in sync.
- ``airplay:<identifier>``: AirPlay speakers, streamed to by Marquee itself with pyatv.
- ``cast:<uuid>``: Chromecast and Google Cast speakers, and the speaker groups made in
  Google Home, streamed to by Marquee itself with pychromecast.

For AirPlay and Cast, Marquee keeps the queue (a ``Session``): it sends one track at a
time and moves on when the first speaker finishes it. Playing to several of them at once
sends each the same track; they aren't in sync to the millisecond the way a Cast group
or a Music Assistant group is.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlencode

from .musiclib import MusicSourceError, item_id, split_id

log = logging.getLogger(__name__)

try:
    import pyatv
    from pyatv.const import Protocol
except ImportError:
    pyatv = None

try:
    import pychromecast
    from pychromecast.discovery import CastBrowser, SimpleCastListener
except ImportError:
    pychromecast = None

# What pyatv can stream as it is (it decodes with miniaudio); anything else goes through ffmpeg
AIRPLAY_DIRECT = (".mp3", ".flac", ".wav", ".ogg")
AIRPLAY_DIRECT_TYPES = ("audio/mpeg", "audio/flac", "audio/wav", "audio/ogg")
CAST_TYPES = ("audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/ogg", "audio/wav")
SCAN_EVERY = 120
PAIRING_TTL = 300  # seconds a PIN on the TV stays good for


class SpeakerError(Exception):
    pass


def kind_of(speaker_id: str) -> str:
    return speaker_id.split(":", 1)[0] if ":" in speaker_id else ""


def ref_of(speaker_id: str) -> str:
    return speaker_id.split(":", 1)[1] if ":" in speaker_id else speaker_id


# ---- AirPlay ------------------------------------------------------------------------

class AirPlay:
    """AirPlay speakers through pyatv (RAOP): found on the network, streamed to one track
    at a time. HomePods and Apple TVs need "Allow speaker access" set to everyone on the
    same network. Apple TVs (and Macs) that ask for pairing are paired once with the PIN
    they show; the credentials are kept in ``airplay_pairings.json``."""

    def __init__(self, path: str | None = None, linked_credentials=None):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._devices: dict[str, object] = {}  # identifier -> pyatv config
        self._scanned = 0.0
        self._scanning = False
        self._playing: dict[str, dict] = {}  # identifier -> {"atv", "task", "proc"}
        self.path = path
        # identifiers -> credentials of an Apple TV paired in the Apple TVs section (appletv.py)
        self._linked = linked_credentials
        self._credentials: dict[str, dict] = {}  # identifier -> {"name", "credentials"}
        self._pairings: dict[str, dict] = {}  # pair ID -> {"ident", "pairing", "created"}
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self._credentials = json.load(f).get("speakers") or {}
            except (OSError, ValueError) as e:
                log.warning("Couldn't read %s: %s", path, e)

    def _save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"speakers": self._credentials}, f)
        os.replace(tmp, self.path)

    def _needs_pairing(self, conf) -> bool:
        service = conf.get_service(Protocol.RAOP)
        return service is not None and service.pairing.name == "Mandatory"

    def _linked_credentials(self, ident: str) -> str | None:
        conf = self._devices.get(ident)
        if self._linked is None or conf is None:
            return None
        try:
            return self._linked(getattr(conf, "all_identifiers", None) or [ident])
        except Exception:  # noqa: BLE001
            return None

    def credentials(self, ident: str) -> str | None:
        """Credentials paired here, else those from pairing the Apple TV for opening Marquee."""
        saved = self._credentials.get(ident)
        return saved["credentials"] if saved else self._linked_credentials(ident)

    def pairing_state(self, ident: str) -> str | None:
        """"paired" (here), "linked" (paired in the Apple TVs section), "needed" (it won't
        play until paired), or None (no pairing needed)."""
        if ident in self._credentials:
            return "paired"
        conf = self._devices.get(ident)
        if conf is None or not self._needs_pairing(conf):
            return None
        return "linked" if self._linked_credentials(ident) else "needed"

    def start_pairing(self, ident: str) -> str:
        """Ask the speaker to show a PIN; returns a pairing ID for ``finish_pairing``."""
        if not self.available:
            raise SpeakerError("AirPlay needs pyatv (pip install pyatv)")
        if ident not in self._devices:
            self.refresh(wait=True)
        conf = self._devices.get(ident)
        if conf is None:
            raise SpeakerError("That AirPlay speaker isn't on the network")

        async def begin():
            pairing = await pyatv.pair(conf, Protocol.RAOP, asyncio.get_running_loop(), name="Marquee Server")
            await pairing.begin()
            return pairing

        try:
            pairing = self._run(begin())
        except SpeakerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SpeakerError(f"{conf.name} didn't start pairing: {e or type(e).__name__}") from None
        pair_id = secrets.token_urlsafe(16)
        with self._lock:
            old = [k for k, v in self._pairings.items() if time.monotonic() - v["created"] > PAIRING_TTL]
            stale = [self._pairings.pop(k)["pairing"] for k in old]
            self._pairings[pair_id] = {"ident": ident, "pairing": pairing, "created": time.monotonic()}
        for p in stale:
            self._close(p)
        return pair_id

    def _close(self, pairing) -> None:
        try:
            self._run(pairing.close(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def finish_pairing(self, pair_id: str, pin: str) -> str:
        """Pair with the PIN shown on the TV; returns the speaker's name."""
        with self._lock:
            entry = self._pairings.pop(pair_id, None)
        if not entry:
            raise SpeakerError("That pairing has expired. Start again.")
        pairing, ident = entry["pairing"], entry["ident"]
        digits = "".join(str(pin).split())
        if not digits.isdigit():
            self._close(pairing)
            raise SpeakerError("The PIN is the 4 digits on the TV")

        async def finish():
            try:
                pairing.pin(int(digits))
                await pairing.finish()
                if not pairing.has_paired:
                    raise SpeakerError("That PIN wasn't accepted")
                return pairing.service.credentials
            finally:
                await pairing.close()

        try:
            credentials = self._run(finish())
        except SpeakerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SpeakerError(f"Pairing didn't work: {e or type(e).__name__}") from None
        conf = self._devices.get(ident)
        name = conf.name if conf else ident
        with self._lock:
            self._credentials[ident] = {"name": name, "credentials": credentials}
            self._save()
        log.info("Paired AirPlay speaker %s", name)
        return name

    def unpair(self, ident: str) -> None:
        with self._lock:
            if self._credentials.pop(ident, None) is not None:
                self._save()

    @property
    def available(self) -> bool:
        return pyatv is not None

    def _run(self, coro, timeout: float | None = 20):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="airplay", daemon=True).start()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future if timeout is None else future.result(timeout)

    def refresh(self, wait: bool = False) -> None:
        if not self.available or self._scanning or (time.monotonic() - self._scanned < SCAN_EVERY and not wait):
            return
        self._scanning = True

        async def scan():
            return await pyatv.scan(asyncio.get_running_loop(), timeout=4, protocol=Protocol.RAOP)

        def run():
            try:
                found = self._run(scan(), timeout=15)
                self._devices = {c.identifier: c for c in found if c.identifier}
                self._scanned = time.monotonic()
            except Exception as e:  # noqa: BLE001 - network trouble mustn't break the panel
                log.info("AirPlay scan failed: %s", e)
            finally:
                self._scanning = False

        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()

    def speakers(self) -> list[dict]:
        self.refresh()
        return sorted(({"id": f"airplay:{ident}", "name": conf.name, "kind": "airplay", "group": False,
                        "pairing": self.pairing_state(ident)}
                       for ident, conf in self._devices.items()), key=lambda s: s["name"].lower())

    def name(self, ident: str) -> str | None:
        conf = self._devices.get(ident)
        return conf.name if conf else None

    def play(self, ident: str, source, metadata: dict) -> concurrent.futures.Future:
        """Stream ``source`` (a file path, a URL pyatv can decode, or an ffmpeg command
        whose output is MP3) to a speaker. The future is done when the track ends."""
        if not self.available:
            raise SpeakerError("AirPlay needs pyatv (pip install pyatv)")
        if ident not in self._devices:
            self.refresh(wait=True)
        conf = self._devices.get(ident)
        if conf is None:
            raise SpeakerError("That AirPlay speaker isn't on the network")
        credentials = self.credentials(ident)
        if credentials:
            conf.set_credentials(Protocol.RAOP, credentials)
        elif self._needs_pairing(conf):
            raise SpeakerError(f"Pair {conf.name} first (Music › Speakers › Pair)")
        self.stop(ident)

        async def stream():
            loop = asyncio.get_running_loop()
            atv = await pyatv.connect(conf, loop)
            entry = self._playing.setdefault(ident, {})
            entry["atv"] = atv
            proc = None
            try:
                file = source
                if isinstance(source, list):  # ffmpeg, writing MP3 to its output
                    proc = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    entry["proc"] = proc
                    file = proc.stdout
                from pyatv.interface import MediaMetadata
                meta = MediaMetadata(title=metadata.get("name"), artist=metadata.get("artist") or None,
                                     album=metadata.get("album") or None, duration=metadata.get("duration"))
                await atv.stream.stream_file(file, metadata=meta, override_missing_metadata=True)
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
                atv.close()

        async def start():
            task = asyncio.ensure_future(stream())
            self._playing.setdefault(ident, {})["task"] = task
            return task

        task = self._run(start())
        done: concurrent.futures.Future = concurrent.futures.Future()

        def finished(t):
            if done.done():
                return
            if t.cancelled():
                done.set_result(False)
            elif t.exception():
                log.warning("AirPlay stream to %s failed: %r", ident, t.exception())
                done.set_exception(SpeakerError(f"AirPlay: {t.exception() or type(t.exception()).__name__}"))
            else:
                done.set_result(True)
        self._loop.call_soon_threadsafe(task.add_done_callback, finished)
        return done

    def stop(self, ident: str) -> None:
        entry = self._playing.pop(ident, None)
        if not entry:
            return
        if (proc := entry.get("proc")) and proc.poll() is None:
            proc.kill()
        if (task := entry.get("task")) and self._loop:
            self._loop.call_soon_threadsafe(task.cancel)

    def volume(self, ident: str, level: int) -> None:
        entry = self._playing.get(ident)
        if not entry or "atv" not in entry:
            raise SpeakerError("Volume can be changed while it's playing")

        async def set_volume():
            await entry["atv"].audio.set_volume(float(level))
        self._run(set_volume(), timeout=10)


# ---- Cast ---------------------------------------------------------------------------

class Cast:
    """Chromecast and Google Cast speakers (and their groups) through pychromecast."""

    def __init__(self):
        self._lock = threading.Lock()
        self._browser = None
        self._zconf = None
        self._casts: dict[str, object] = {}  # uuid -> connected Chromecast

    @property
    def available(self) -> bool:
        return pychromecast is not None

    def _start(self) -> None:
        if not self.available:
            return
        with self._lock:
            if self._browser is not None:
                return
            import zeroconf
            self._zconf = zeroconf.Zeroconf()
            self._browser = CastBrowser(SimpleCastListener(), self._zconf)
            self._browser.start_discovery()

    def speakers(self) -> list[dict]:
        self._start()
        if self._browser is None:
            return []
        out = []
        for uuid, info in list(self._browser.devices.items()):
            kind = getattr(info, "cast_type", None)  # None until it's been connected to
            if kind not in ("audio", "group", "cast", None):
                continue
            out.append({"id": f"cast:{uuid}", "name": info.friendly_name, "kind": "cast", "group": kind == "group"})
        return sorted(out, key=lambda s: s["name"].lower())

    def name(self, uuid: str) -> str | None:
        info = self._browser.devices.get(self._uuid(uuid)) if self._browser else None
        return info.friendly_name if info else None

    def _uuid(self, uuid: str):
        if self._browser is None:
            return None
        return next((u for u in self._browser.devices if str(u) == uuid), None)

    def _cast(self, uuid: str):
        if not self.available:
            raise SpeakerError("Cast needs pychromecast (pip install pychromecast)")
        self._start()
        with self._lock:
            cast = self._casts.get(uuid)
        if cast is not None:
            return cast
        key = self._uuid(uuid)
        if key is None:
            time.sleep(2)  # just started looking
            key = self._uuid(uuid)
        if key is None:
            raise SpeakerError("That Cast speaker isn't on the network")
        cast = pychromecast.get_chromecast_from_cast_info(self._browser.devices[key], self._zconf, tries=2, timeout=10)
        cast.wait(timeout=10)
        with self._lock:
            self._casts[uuid] = cast
        return cast

    def play(self, uuid: str, url: str, content_type: str, metadata: dict, seek: float = 0.0) -> None:
        cast = self._cast(uuid)
        mc = cast.media_controller
        meta = {"metadataType": 3, "title": metadata.get("name"), "artist": metadata.get("artist"),
                "albumName": metadata.get("album")}
        if metadata.get("public_image"):
            meta["images"] = [{"url": metadata["public_image"]}]
        mc.play_media(url, content_type, title=metadata.get("name"), thumb=metadata.get("public_image"),
                      stream_type="BUFFERED", metadata=meta, current_time=seek or None)
        mc.block_until_active(timeout=10)

    def status(self, uuid: str) -> dict:
        status = self._cast(uuid).media_controller.status
        return {"state": status.player_state, "idle_reason": status.idle_reason,
                "position": status.adjusted_current_time, "duration": status.duration}

    def control(self, uuid: str, action: str) -> None:
        mc = self._cast(uuid).media_controller
        {"pause": mc.pause, "play": mc.play, "stop": mc.stop}[action]()

    def volume(self, uuid: str, level: int) -> None:
        self._cast(uuid).set_volume(max(0, min(100, level)) / 100)

    def get_volume(self, uuid: str) -> int | None:
        with self._lock:
            cast = self._casts.get(uuid)
        status = getattr(cast, "status", None) if cast else None
        return round(status.volume_level * 100) if status else None


# ---- Marquee's own queues (AirPlay and Cast) ---------------------------------------

class Session:
    """A queue Marquee plays on one or more AirPlay and Cast speakers."""

    _ids = itertools.count(1)

    def __init__(self, player: "MusicPlayer", speakers: list[str], tracks: list[dict], start: int = 0,
                 seek: float = 0.0):
        self.id = f"session:{next(self._ids)}"
        self._seek = seek  # seconds into the first track (a book picked up where it was left)
        self.player = player
        self.speakers = speakers
        self.tracks = tracks
        self.index = start
        self.state = "playing"
        self.error: str | None = None
        self.started = 0.0
        self.paused_at: float | None = None
        self._jump = threading.Event()
        self._stopped = threading.Event()
        self._resume = threading.Event()
        self._thread = threading.Thread(target=self._run, name=self.id, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def current(self) -> dict | None:
        return self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None

    @property
    def position(self) -> float:
        """Seconds into the current track."""
        return max(0.0, (self.paused_at or time.monotonic()) - self.started) if self.started else 0.0

    def _run(self) -> None:
        while not self._stopped.is_set() and 0 <= self.index < len(self.tracks):
            track = self.tracks[self.index]
            self._jump.clear()
            seek, self._seek = self._seek, 0.0
            try:
                waits = self.player._start_track(self.speakers, track, seek)
            except (SpeakerError, MusicSourceError) as e:
                log.warning("Couldn't play %s: %s", track.get("name"), e)
                self.error = str(e)
                self.index += 1
                continue
            self.error = None
            self.started = time.monotonic() - seek
            self.paused_at = None
            try:
                finished = self._wait(waits)
            except SpeakerError as e:
                log.warning("%s stopped playing %s: %s", self.speakers[0], track.get("name"), e)
                self.error = str(e)
                self.index += 1
                continue
            if self._stopped.is_set():
                break
            if self.state == "paused":
                self._resume.wait()
                self._resume.clear()
                if self._stopped.is_set():
                    break
                if self.player._resume(self.speakers):
                    finished = self._wait(self.player._waits(self.speakers))
                    if self._stopped.is_set():
                        break
                else:
                    # AirPlay can't pause: start the track again, where it was (with ffmpeg)
                    self._seek = self._paused_position
                    continue
            if finished and not self._jump.is_set():
                self.index += 1
        if not self._stopped.is_set():
            self.state = "stopped"
            self.player._stop_speakers(self.speakers)
        self.player._ended(self)

    def _wait(self, waits) -> bool:
        """Wait for the first speaker to finish the track, a skip, a pause or a stop."""
        while not self._stopped.is_set() and not self._jump.is_set() and self.state != "paused":
            if waits():
                return True
            self._jump.wait(1)
        return False

    # ---- controls ----

    def skip(self, step: int) -> None:
        if self.state == "paused":
            self.state = "playing"
            self._resume.set()
        self.index = max(0, self.index + step)
        self._jump.set()

    def jump(self, index: int) -> None:
        self.index = max(0, min(index, len(self.tracks) - 1))
        if self.state == "paused":
            self.state = "playing"
            self._resume.set()
        self._jump.set()

    _paused_position = 0.0

    def pause(self) -> None:
        if self.state == "playing":
            self.state = "paused"
            self.paused_at = time.monotonic()
            self._paused_position = self.position
            self.player._pause(self.speakers)
            self._jump.set()

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "playing"
            self.started += time.monotonic() - (self.paused_at or time.monotonic())
            self._resume.set()

    def stop(self) -> None:
        self.state = "stopped"
        self._stopped.set()
        self._resume.set()
        self._jump.set()
        self.player._stop_speakers(self.speakers)

    def add(self, tracks: list[dict], next_up: bool) -> None:
        if next_up:
            self.tracks[self.index + 1:self.index + 1] = tracks
        else:
            self.tracks += tracks


# ---- where you are in an audiobook --------------------------------------------------

class BookProgress:
    """Reports where each playing audiobook has got to back to Audiobookshelf: every
    ``EVERY`` seconds while it plays, and when it's paused, stopped or replaced."""

    EVERY = 15

    def __init__(self, player: "MusicPlayer"):
        self.player = player
        self._lock = threading.Lock()
        self._zones: dict[str, dict] = {}  # zone ID -> {"book", "tracks", "duration", "last", "sent"}
        self._thread: threading.Thread | None = None

    def watch(self, zone_id: str, book_id: str, tracks: list[dict]) -> None:
        duration = sum(t.get("duration") or 0 for t in tracks) or tracks[0].get("book_duration") or 0
        with self._lock:
            self._zones[zone_id] = {"book": book_id, "tracks": tracks, "duration": duration,
                                    "last": None, "sent": None}
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="book-progress", daemon=True)
                self._thread.start()

    def book_of(self, zone_id: str) -> dict | None:
        """``{"id", "position", "duration"}`` of the book a zone is playing, for the panel."""
        with self._lock:
            z = self._zones.get(zone_id)
        if not z:
            return None
        position = self._position(zone_id, z)
        return {"id": item_id("abs", "book", z["book"]), "duration": z["duration"],
                "position": position if position is not None else z["last"]}

    def _position(self, zone_id: str, z: dict) -> float | None:
        """Where in the book a zone is, or None if it isn't playing the book any more."""
        if kind_of(zone_id) == "ma":
            try:
                queue = self.player._call("player_queues/get", queue_id=ref_of(zone_id)) or {}
            except SpeakerError:
                return None
            current = queue.get("current_item") or {}
            uri = str((current.get("media_item") or {}).get("uri") or current.get("uri") or "")
            track = next((t for t in z["tracks"]
                          if f"/api/items/{z['book']}/file/{split_id(t['id'])[2].partition('/')[2]}" in uri), None)
            if track is None or queue.get("state") not in ("playing", "paused"):
                return None
            return (track.get("offset") or 0) + float(queue.get("elapsed_time") or 0)
        session = next((s for s in self.player.sessions if s.id == zone_id), None)
        if session is None or session.state == "stopped":
            return None
        track = session.current or {}
        if track.get("book") != z["book"]:
            return None
        return (track.get("offset") or 0) + session.position

    def report(self, zone_id: str) -> None:
        """Send where a zone is in its book now (before it's paused, stopped or replaced)."""
        with self._lock:
            z = self._zones.get(zone_id)
        if z:
            position = self._position(zone_id, z)
            if position is not None:
                z["last"] = position
            self._send(z)

    def _send(self, z: dict) -> None:
        if z["last"] is None or (z["sent"] is not None and abs(z["sent"] - z["last"]) < 3):
            return
        finished = bool(z["duration"]) and z["last"] >= z["duration"] - 5
        try:
            self.player.library.audiobookshelf.save_progress(z["book"], z["last"], z["duration"], finished)
            z["sent"] = z["last"]
        except MusicSourceError as e:
            log.info("Couldn't tell Audiobookshelf where %s got to: %s", z["book"], e)

    def _run(self) -> None:
        while True:
            time.sleep(self.EVERY)
            with self._lock:
                zones = list(self._zones.items())
            if not zones:
                with self._lock:
                    if not self._zones:
                        self._thread = None
                        return
                continue
            for zone_id, z in zones:
                position = self._position(zone_id, z)
                if position is None:
                    # It ended, or something else plays there now: the last place is final
                    self._send(z)
                    with self._lock:
                        if self._zones.get(zone_id) is z:
                            del self._zones[zone_id]
                    continue
                z["last"] = position
                self._send(z)


# ---- all of them --------------------------------------------------------------------

class MusicPlayer:
    """Plays library items (musiclib.MusicLibrary) on any speakers, and says what's playing."""

    def __init__(self, library, music, art_url, airplay_pairings: str | None = None, linked_credentials=None):
        self.library = library
        self.music = music  # music.Music: Music Assistant, directly or through Home Assistant
        self.public_art = art_url  # item ID -> a picture address speakers can fetch
        self.airplay = AirPlay(airplay_pairings, linked_credentials)
        self.cast = Cast()
        self._lock = threading.Lock()
        self.sessions: list[Session] = []
        self.progress = BookProgress(self)

    # ---- speakers ----

    def _assistant(self):
        return self.music._direct()

    def _assistant_players(self) -> list[dict]:
        direct = self._assistant()
        if direct is None:
            return []
        try:
            found = direct.call("players/all", timeout=6) or []
        except Exception as e:  # noqa: BLE001
            log.info("Music Assistant's players: %s", e)
            return []
        return [p for p in found if isinstance(p, dict) and p.get("player_id") and p.get("enabled", True) is not False
                and p.get("available", True) is not False and not p.get("hide_in_ui")]

    def speakers(self) -> list[dict]:
        out = []
        for p in self._assistant_players():
            out.append({
                "id": f"ma:{p['player_id']}", "name": str(p.get("display_name") or p.get("name") or p["player_id"]),
                "kind": "music_assistant", "group": p.get("type") == "group",
                "volume": p.get("volume_level"), "state": p.get("playback_state") if self._own_queue(p) else None,
                "members": [f"ma:{m}" for m in p.get("group_members") or [] if m != p["player_id"]],
                "can_group": [f"ma:{m}" for m in p.get("can_group_with") or []],
                "synced_to": f"ma:{p['synced_to']}" if p.get("synced_to") else None,
            })
        if not out and (bridge := self.music.bridge).status()["connected"]:
            # Home Assistant knows Music Assistant's speakers, but can only play its music
            out += [{"id": f"ma:{s['id']}", "name": s["name"], "kind": "music_assistant", "group": False,
                     "volume": None, "state": None, "members": [], "can_group": [], "synced_to": None,
                     "limited": True} for s in bridge.speakers()]
        busy = {s: z for z in self.sessions for s in z.speakers}
        for s in self.airplay.speakers() + self.cast.speakers():
            ref = ref_of(s["id"])
            volume = self.cast.get_volume(ref) if s["kind"] == "cast" else None
            session = busy.get(s["id"])
            out.append({**s, "volume": volume, "state": session.state if session else None,
                        "members": [], "can_group": [], "synced_to": None})
        return out

    def speaker_name(self, speaker_id: str) -> str:
        kind, ref = kind_of(speaker_id), ref_of(speaker_id)
        name = None
        if kind == "airplay":
            name = self.airplay.name(ref)
        elif kind == "cast":
            name = self.cast.name(ref) if self.cast.available else None
        elif kind == "ma":
            name = self.music.speaker_name(ref)
        return name or ref

    def status(self) -> dict:
        return {"airplay": self.airplay.available, "cast": self.cast.available,
                "music_assistant": self._assistant() is not None}

    # ---- playing ----

    def play(self, ids: list[str], speaker_ids: list[str], option: str = "replace", start: int = 0,
             position: float | None = None) -> dict:
        """Play items (albums, playlists, tracks...) on speakers. ``option`` is ``replace``,
        ``next`` (after what's playing) or ``add`` (at the end).

        An audiobook on its own starts at ``position`` (seconds into the book), or where
        Audiobookshelf says it was left, and where it's got to is reported back as it plays."""
        if not ids:
            raise SpeakerError("Choose something to play")
        if not speaker_ids:
            raise SpeakerError("Choose a speaker")
        if option not in ("replace", "next", "add"):
            raise SpeakerError("Play, play next or add to the queue")
        assistant = [s for s in speaker_ids if kind_of(s) == "ma"]
        own = [s for s in speaker_ids if kind_of(s) in ("airplay", "cast")]
        if len(assistant) + len(own) != len(speaker_ids):
            raise SpeakerError("That isn't a speaker")
        if own and any(split_id(i)[0] == "ma" for i in ids):
            raise SpeakerError("Music Assistant's music plays on Music Assistant's speakers. "
                               "Choose one of those, or music from another source.")
        if len(ids) == 1 and option == "replace" and split_id(ids[0])[:2] == ("abs", "book"):
            self._play_book(split_id(ids[0])[2], assistant, own, position)
            return self.now()
        if assistant:
            self._play_assistant(ids, assistant, option, start)
        if own:
            tracks = []
            for ident in ids:
                tracks += self.library.tracks(ident)
            if start and len(ids) == 1:
                tracks = tracks[start:]
            if not tracks:
                raise SpeakerError("There's nothing in that to play")
            self._play_own(tracks, own, option)
        return self.now()

    def _play_book(self, book_id: str, assistant: list[str], own: list[str], position: float | None) -> None:
        from .audiobookshelf import locate
        books = self.library.books
        tracks = books.book_tracks(book_id)
        if not tracks:
            raise SpeakerError("That book has no audio")
        if position is None:
            position = books.resume_point(book_id)
        index, seek = locate(tracks, max(0.0, float(position)))
        if seek < 2:
            seek = 0.0
        if assistant:
            leader = ref_of(assistant[0])
            self._play_assistant([item_id("abs", "book", book_id)], assistant, "replace", index)
            if seek:
                threading.Thread(target=self._assistant_seek, args=(leader, seek), daemon=True).start()
            self.progress.watch(f"ma:{leader}", book_id, tracks)
        if own:
            session = self._play_own(tracks[index:], own, "replace", seek)
            self.progress.watch(session.id, book_id, tracks)

    def _assistant_seek(self, queue_id: str, seek: float) -> None:
        """Music Assistant starts at the top of a file: move into it once it's playing."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                queue = self._call("player_queues/get", queue_id=queue_id) or {}
                if queue.get("state") == "playing":
                    self._call("player_queues/seek", queue_id=queue_id, position=int(seek))
                    return
            except SpeakerError:
                pass
            time.sleep(0.5)
        log.info("Couldn't move %s into the book: it didn't start playing", queue_id)

    def _media(self, ident: str) -> list[str]:
        """What Music Assistant is given for an item: its URI, or the tracks' addresses."""
        source, kind, ref = split_id(ident)
        if source == "ma":
            return [ref]
        return [self.library.stream(t)[0] for t in self.library.tracks(ident)]

    def _play_assistant(self, ids: list[str], speakers: list[str], option: str, start: int) -> None:
        players = [ref_of(s) for s in speakers]
        direct = self._assistant()
        if direct is None:
            # Through Home Assistant: one Music Assistant item on one speaker, nothing more
            if len(ids) != 1 or len(players) != 1 or split_id(ids[0])[0] != "ma" or option != "replace":
                raise SpeakerError("Connect Music Assistant directly (under Speakers) to do that")
            source, kind, ref = split_id(ids[0])
            from .music import MusicError
            try:
                self.music.play({"uri": ref, "media_type": kind, "name": ref}, players[0])
            except MusicError as e:
                raise SpeakerError(str(e)) from None
            return
        leader = players[0]
        if len(players) > 1:
            self._call("players/cmd/set_members", target_player=leader, player_ids_to_add=players[1:])
        media = []
        for ident in ids:
            media += self._media(ident)
        if start and len(media) > 1:
            media = media[start:]
        self._call("player_queues/play_media", timeout=30, queue_id=leader, media=media, option=option)

    def _call(self, command: str, timeout: float = 15, **args):
        direct = self._assistant()
        if direct is None:
            raise SpeakerError("Music Assistant isn't connected directly")
        from .music import MusicError
        try:
            return direct.call(command, timeout=timeout, **args)
        except MusicError as e:
            raise SpeakerError(str(e)) from None

    def _play_own(self, tracks: list[dict], speakers: list[str], option: str, seek: float = 0.0) -> Session:
        with self._lock:
            current = next((s for s in self.sessions if set(speakers) & set(s.speakers)), None)
            if current and option != "replace" and current.state != "stopped":
                current.add(tracks, option == "next")
                return current
            stopping = [s for s in self.sessions if set(speakers) & set(s.speakers)]
            session = Session(self, speakers, tracks, seek=seek)
            self.sessions = [s for s in self.sessions if not set(speakers) & set(s.speakers)] + [session]
        for s in stopping:
            self.progress.report(s.id)
            s.stop()
        session.start()
        return session

    def _ended(self, session: Session) -> None:
        with self._lock:
            if session in self.sessions and session.state == "stopped":
                self.sessions.remove(session)

    # The session's way to its speakers

    def _start_track(self, speakers: list[str], track: dict, seek: float = 0.0):
        """Start a track on every speaker, ``seek`` seconds in; returns a function saying
        whether the first has finished it."""
        url, content_type = self.library.stream(track)
        local = None
        if track["source"].startswith("mf-"):
            local = self.library.source(track["source"]).file(split_id(track["id"])[2])
        meta = {**track, "public_image": self.public_art(track) if track.get("image") else None}
        waits = []
        errors = []
        for speaker in speakers:
            kind, ref = kind_of(speaker), ref_of(speaker)
            try:
                if kind == "airplay":
                    waits.append(self._airplay_track(ref, url, content_type, local, meta, seek))
                else:
                    waits.append(self._cast_track(ref, url, content_type, local, track, meta, seek))
            except SpeakerError as e:
                errors.append(f"{self.speaker_name(speaker)}: {e}")
        if not waits:
            raise SpeakerError("; ".join(errors) or "No speaker could play it")
        first = waits[0]
        return first

    def _airplay_track(self, ref, url, content_type, local, meta, seek: float = 0.0):
        ext = os.path.splitext(local or "")[1].lower()
        if seek and self._ffmpeg_path():
            source = self._ffmpeg(local or url, seek)  # only ffmpeg can start partway in
        elif local and ext in AIRPLAY_DIRECT:
            source = local
        elif content_type in AIRPLAY_DIRECT_TYPES:
            source = url
        else:
            source = self._ffmpeg(local or url)
        future = self.airplay.play(ref, source, meta)

        def finished():
            if not future.done():
                return False
            if (error := future.exception()) is not None:
                raise SpeakerError(str(error))
            return True
        return finished

    def _cast_track(self, ref, url, content_type, local, track, meta, seek: float = 0.0):
        if content_type not in CAST_TYPES:
            url, content_type = self.library.transcoded(track), "audio/mpeg"
        self.cast.play(ref, url, content_type, meta, seek)
        seen = {"playing": False}

        def finished():
            try:
                status = self.cast.status(ref)
            except SpeakerError:
                return False
            if status["state"] in ("PLAYING", "BUFFERING"):
                seen["playing"] = True
            return seen["playing"] and status["state"] == "IDLE"
        return finished

    @staticmethod
    def _ffmpeg_path() -> str | None:
        return shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                               if os.path.exists(p)), None)

    def _ffmpeg(self, source: str, seek: float = 0.0) -> list[str]:
        ffmpeg = self._ffmpeg_path()
        if not ffmpeg:
            raise SpeakerError("This file needs ffmpeg to play over AirPlay (brew install ffmpeg)")
        start = ["-ss", f"{seek:.1f}"] if seek else []
        return [ffmpeg, "-nostdin", "-loglevel", "error", *start, "-i", source, "-vn", "-f", "mp3", "-b:a", "320k",
                "pipe:1"]

    def _waits(self, speakers: list[str]):
        ref = ref_of(speakers[0])
        if kind_of(speakers[0]) == "cast":
            seen = {"playing": False}

            def finished():
                status = self.cast.status(ref)
                if status["state"] in ("PLAYING", "BUFFERING"):
                    seen["playing"] = True
                return seen["playing"] and status["state"] == "IDLE"
            return finished
        return lambda: True

    def _pause(self, speakers: list[str]) -> None:
        for s in speakers:
            try:
                if kind_of(s) == "cast":
                    self.cast.control(ref_of(s), "pause")
                else:
                    self.airplay.stop(ref_of(s))  # AirPlay streams can't pause: the track starts again
            except SpeakerError:
                continue

    def _resume(self, speakers: list[str]) -> bool:
        """Carry on after a pause. False if the track has to start again (AirPlay)."""
        if all(kind_of(s) == "cast" for s in speakers):
            for s in speakers:
                try:
                    self.cast.control(ref_of(s), "play")
                except SpeakerError:
                    continue
            return True
        return False

    def _stop_speakers(self, speakers: list[str]) -> None:
        for s in speakers:
            try:
                if kind_of(s) == "cast":
                    self.cast.control(ref_of(s), "stop")
                else:
                    self.airplay.stop(ref_of(s))
            except (SpeakerError, Exception) as e:  # noqa: BLE001 - stopping is best effort
                log.debug("Stopping %s: %s", s, e)

    # ---- what's playing, and controls ----

    def now(self) -> list[dict]:
        zones = []
        for p in self._assistant_players():
            if p.get("synced_to") or p.get("playback_state") not in ("playing", "paused") or not self._own_queue(p):
                continue
            media = p.get("current_media") or {}
            members = [m for m in p.get("group_members") or [] if m != p["player_id"]]
            zones.append({
                "id": f"ma:{p['player_id']}", "kind": "music_assistant",
                "speakers": [f"ma:{p['player_id']}"] + [f"ma:{m}" for m in members],
                "names": [p.get("display_name") or p.get("name")] + [self.music.speaker_name(m) or m for m in members],
                "state": p["playback_state"], "can_pause": True,
                "track": {"name": media.get("title") or "", "artist": media.get("artist") or "",
                          "album": media.get("album") or "", "image": media.get("image_url"),
                          "duration": media.get("duration")},
                "position": self._elapsed(p), "volume": p.get("volume_level"),
            })
        for s in list(self.sessions):
            if s.state == "stopped":
                continue
            track = s.current or {}
            zones.append({
                "id": s.id, "kind": "marquee", "speakers": list(s.speakers),
                "names": [self.speaker_name(x) for x in s.speakers], "state": s.state,
                "can_pause": True,
                "restarts_on_resume": any(kind_of(x) == "airplay" for x in s.speakers) and not self._ffmpeg_path(),
                "track": {k: track.get(k) for k in ("id", "name", "artist", "album", "image", "duration")},
                "position": s.position, "book": self.progress.book_of(s.id),
                "index": s.index, "error": s.error,
                "queue": [{k: t.get(k) for k in ("id", "name", "artist", "duration")} for t in s.tracks],
            })
        return zones

    @staticmethod
    def _elapsed(p: dict) -> float | None:
        """Where a Music Assistant player is in its track: its elapsed time is as of when it
        was last updated, so the time since counts too while it plays."""
        elapsed = p.get("elapsed_time")
        updated = p.get("elapsed_time_last_updated")
        if elapsed is None:
            return None
        if p.get("playback_state") == "playing" and updated:
            elapsed += max(0.0, time.time() - updated)
        duration = (p.get("current_media") or {}).get("duration")
        return min(elapsed, duration) if duration else elapsed

    @staticmethod
    def _own_queue(p: dict) -> bool:
        """Whether a Music Assistant player is playing Music Assistant's queue, not something
        of its own (an Apple TV's Plex app, say), which Marquee mustn't show or control."""
        source = p.get("active_source")
        return source in (None, p["player_id"]) or (source is not None and source == p.get("active_group")) or \
            bool((p.get("current_media") or {}).get("queue_id"))

    def queue(self, zone_id: str) -> dict:
        """A zone's queue: ``{"index", "items"}``."""
        if kind_of(zone_id) == "ma":
            pid = ref_of(zone_id)
            queue = self._call("player_queues/get", queue_id=pid) or {}
            items = self._call("player_queues/items", queue_id=pid, limit=200) or []
            return {"index": queue.get("current_index") or 0,
                    "items": [{"name": i.get("name"), "duration": i.get("duration"),
                               "artist": ", ".join(a.get("name", "") for a in ((i.get("media_item") or {}).get("artists") or [])
                                                   if isinstance(a, dict))}
                              for i in items if isinstance(i, dict)]}
        session = self._session(zone_id)
        return {"index": session.index, "items": [{k: t.get(k) for k in ("id", "name", "artist", "duration")}
                                                  for t in session.tracks]}

    def _session(self, zone_id: str) -> Session:
        session = next((s for s in self.sessions if s.id == zone_id), None)
        if session is None:
            raise SpeakerError("That isn't playing any more")
        return session

    def control(self, zone_id: str, action: str, value=None) -> None:
        """``pause``, ``play``, ``next``, ``previous``, ``stop`` or ``jump`` (to queue index ``value``)."""
        if kind_of(zone_id) == "ma":
            pid = ref_of(zone_id)
            commands = {"pause": ("player_queues/pause", {"queue_id": pid}),
                        "play": ("player_queues/play", {"queue_id": pid}),
                        "next": ("player_queues/next", {"queue_id": pid}),
                        "previous": ("player_queues/previous", {"queue_id": pid}),
                        "stop": ("player_queues/stop", {"queue_id": pid})}
            if action == "jump":
                self._call("player_queues/play_index", queue_id=pid, index=int(value))
                return
            if action not in commands:
                raise SpeakerError("Pause, play, next, previous or stop")
            if action in ("pause", "stop"):
                self.progress.report(zone_id)
            command, args = commands[action]
            self._call(command, **args)
            return
        session = self._session(zone_id)
        if action == "pause":
            session.pause()
        elif action == "play":
            session.resume()
        elif action == "next":
            session.skip(1)
        elif action == "previous":
            session.skip(-1)
        elif action == "stop":
            self.progress.report(session.id)
            session.stop()
            self._ended(session)
        elif action == "jump":
            session.jump(int(value))
        else:
            raise SpeakerError("Pause, play, next, previous or stop")

    def move(self, zone_id: str, speaker_ids: list[str]) -> None:
        """Carry on what a zone plays on other speakers (a reader's speakers were changed while
        it played): a book from where it's got to, Marquee's own queue from the track it's on,
        and Music Assistant's queue on other Music Assistant speakers."""
        if not speaker_ids:
            raise SpeakerError("Choose a speaker")
        if any(kind_of(s) not in ("ma", "airplay", "cast") for s in speaker_ids):
            raise SpeakerError("That isn't a speaker")
        book = self.progress.book_of(zone_id)
        if book and book.get("position") is not None:
            self.control(zone_id, "stop")  # tells Audiobookshelf where it got to
            self.play([book["id"]], speaker_ids, position=book["position"])
            return
        if kind_of(zone_id) == "ma":
            if any(kind_of(s) != "ma" for s in speaker_ids):
                raise SpeakerError("Music playing through Music Assistant can only move to Music Assistant's "
                                   "speakers. The next card plays on the new ones.")
            leader = speaker_ids[0]
            if leader != zone_id:
                self._call("player_queues/transfer", source_queue_id=ref_of(zone_id),
                           target_queue_id=ref_of(leader), auto_play=True)
            self.group(leader, speaker_ids[1:])
            return
        session = self._session(zone_id)
        tracks, seek = session.tracks[session.index:], session.position
        if not tracks:
            raise SpeakerError("That isn't playing any more")
        own = [s for s in speaker_ids if kind_of(s) != "ma"]
        if len(own) == len(speaker_ids):
            with self._lock:
                if session in self.sessions and not set(own) & set(session.speakers):
                    self.sessions.remove(session)
                    session.stop()
            self._play_own(tracks, own, "replace", seek)
            return
        session.stop()
        self._ended(session)
        ids = [t["id"] for t in tracks if t.get("id")]
        self.play(ids, speaker_ids, "replace")

    def zone_for(self, speaker_ids: list[str]) -> dict | None:
        """What's playing on any of these speakers (a reader's), if anything."""
        wanted = set(speaker_ids)
        return next((z for z in self.now() if wanted & set(z["speakers"])), None) if wanted else None

    def volume(self, speaker_id: str, level: int) -> None:
        level = max(0, min(100, int(level)))
        kind, ref = kind_of(speaker_id), ref_of(speaker_id)
        if kind == "ma":
            self._call("players/cmd/volume_set", player_id=ref, volume_level=level)
        elif kind == "cast":
            self.cast.volume(ref, level)
        elif kind == "airplay":
            self.airplay.volume(ref, level)
        else:
            raise SpeakerError("That isn't a speaker")

    def group(self, leader: str, members: list[str]) -> None:
        """Music Assistant only: play ``members`` in sync with ``leader`` (none to ungroup)."""
        if kind_of(leader) != "ma" or any(kind_of(m) != "ma" for m in members):
            raise SpeakerError("Only Music Assistant's speakers group in sync. For Cast speakers, make a "
                               "speaker group in Google Home.")
        pid = ref_of(leader)
        current = next((p for p in self._assistant_players() if p["player_id"] == pid), None)
        if current is None:
            raise SpeakerError("Music Assistant doesn't have that speaker")
        now = {m for m in current.get("group_members") or [] if m != pid}
        wanted = {ref_of(m) for m in members}
        self._call("players/cmd/set_members", target_player=pid,
                   player_ids_to_add=sorted(wanted - now), player_ids_to_remove=sorted(now - wanted))
