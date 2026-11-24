"""Web server the NFC tags point at.

Tapping a tag opens e.g. ``http://192.168.1.20:8080/m/tmdb-603`` on the phone,
which sends that movie to the Marquee app on the chosen Apple TV (it streams from
Plex, Jellyfin or a folder).

Routes
  /m/<key>             play a movie   (?client=NAME to pick a Marquee player, ?resume=0 to start over)
  /pause /play /stop   control tags for the back cover of the book
  /off                 run the Home Assistant HA_OFF_SCRIPT (e.g. TV off)
  /tags                list every movie with the URL to write onto its tag (/tags.csv to download)
  /book                the printable book (?layout=grid&collection=NAME&title=...)
  /plex/login          Sign in with Plex (saves the token, so nobody has to find it by hand)
  /plex/server         JSON: the Plex server in use (found from the sign-in when PLEX_URL isn't set)
  /tv/...               the Marquee Apple TV app: pairing, inbox, status, library, search and play
                       (/tv/expected: Marquee pairs by itself on an Apple TV paired below)
  /panel/api/...       the control panel (/): overview, movies, pairing, Apple TVs (needs pyatv),
                       and cards linked to movies by their ID (see cards.py)
  /phone/...           the Marquee Remote phone app: which player this phone's taps go to
  /ha/...              the Marquee integration for Home Assistant: players, what they're playing, controls
  /poster/<key>        poster image (proxied so your Plex token stays private)
                       ?w=200&h=300 returns a resized baseline JPEG (for the ESP32 screen)
  /files/<key>/<token>/...  a folder source's movie file, or ffmpeg's HLS stream of it, for
                       Marquee (the address is signed for that movie; see filestream.py)

For the ESP32 remote (see esphome/):
  /m/<key>?async=1     start playback in the background and return immediately
  /now                 JSON: what's playing, poster path, position, duration
  /toggle              play/pause
  /seek?by=30          jump forwards (or backwards with a negative number) in seconds
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import logging
import os
import socket
import threading
import time
from html import escape
from urllib.parse import parse_qs, urlsplit

import requests
from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory

from . import plexauth
from .appletv import AppleTVError
from .config import Config
from .cards import music_title, normalize_uid
from .music import Music, MusicBridge, MusicError, music_item
from .musiclib import AUDIO_TYPES, MusicLibrary, MusicSourceError, split_id
from .readers import ReaderError, Readers
from .lyrics import Lyrics
from .speakers import MarqueeSpeakers, MusicPlayer, SpeakerError
from .plex import Plex, PlexNFCError, app_movie, movie_key
from .screen_text import box_json
from .filestream import DIRECT_TYPES
from .sources import FolderSource, SourceError, SourceMovie, connect_plex_server, source_name

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body{{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee;text-align:center;padding:24px}}
  img{{max-width:70vw;max-height:60vh;border-radius:8px;box-shadow:0 8px 32px #000}}
  h1{{font-size:1.4rem;margin:20px 0 4px}} p{{color:#aaa;margin:4px}} .err{{color:#ff8a80}}
  table{{margin:auto;border-collapse:collapse;text-align:left}} td{{padding:6px 10px;border-bottom:1px solid #333}}
  code{{user-select:all}} button{{font:inherit;padding:4px 10px}}
  .btn{{display:inline-block;background:#e5a00d;color:#111;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600}}
</style></head><body>{body}</body></html>"""


def page(title: str, body: str, status: int = 200) -> Response:
    return Response(PAGE.format(title=escape(title), body=body), status=status, mimetype="text/html")


def resize_jpeg(data: bytes, width: int, height: int) -> bytes:
    """Crop/scale to exactly width x height and save as a baseline JPEG.

    Small microcontroller JPEG decoders (like ESPHome's) can't read progressive
    JPEGs, and an exact size means no scaling work on the device.
    """
    from PIL import Image, ImageOps

    img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (width, height), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, progressive=False, optimize=False)
    return out.getvalue()



@functools.lru_cache(maxsize=64)
def _own_address(address: str) -> bool:
    try:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((address, 0))  # only an address of this machine's can be bound
        return True
    except (OSError, ValueError):
        return False


def is_own_address(address: str) -> bool:
    """Whether a request from ``address`` comes from this machine, by any of its network
    addresses: the control panel opened at marquee.local on the Mac itself, say."""
    return bool(address) and _own_address(address)


def create_app(config: Config | None = None, plex: Plex | None = None) -> Flask:
    config = config or Config.from_env()
    plex = plex or Plex(config)
    app = Flask(__name__)

    def resume_param():
        """Tags start movies from the beginning, unless the tag asks with ``?resume=1``."""
        return request.args.get("resume", "").lower() in ("1", "true", "yes")

    def tap_client():
        """The player for this request: ``?client=``, else the one chosen in Marquee Remote
        on the phone that tapped (recognized by its address), else the default."""
        return request.args.get("client") or plex.app_pairings.player_for(request.remote_addr)

    # Playback started with ?async=1. Shown by /now until Plex reports the movie playing.
    pending: dict = {}
    pending_lock = threading.Lock()
    # Music cards play through Music Assistant: directly, or through the Marquee
    # integration in Home Assistant (see music.py)
    bridge = MusicBridge()
    music = Music(bridge, os.path.join(os.path.dirname(plex.config.auth_file) or ".", "music_assistant.json"),
                  os.environ.get("MUSIC_ASSISTANT_URL", ""), os.environ.get("MUSIC_ASSISTANT_TOKEN", ""))
    # The Music tab: music from every source, played on AirPlay, Cast and Music Assistant speakers
    music_library = MusicLibrary(plex, music, os.path.dirname(plex.config.auth_file) or ".", plex.library.files,
                                 config.public_base_url)

    def public_art(track: dict) -> str | None:
        image = track.get("image") or ""
        return image if image.startswith(("http://", "https://")) else music_library.signed("art", track["id"])

    player = MusicPlayer(music_library, music, public_art,
                         os.path.join(os.path.dirname(plex.config.auth_file) or ".", "airplay_pairings.json"),
                         plex.apple_tvs.credentials_for)
    app.extensions["marquee_music"] = player
    # Marquee on each Apple TV is a speaker too: it plays music and books through the TV
    player.marquee = MarqueeSpeakers(plex.app_inbox, lambda: marquee_screens(), lambda name: plex.prepare_tv(name))
    lyrics = Lyrics(music_library)
    app.extensions["marquee_lyrics"] = lyrics

    def marquee_screens() -> list[str]:
        """The paired Marquees on Apple TVs: speakers, and screens for Now Playing."""
        return [d["name"] for d in plex.app_pairings.devices() if d["kind"] == "tv"]

    # ---- NFC readers and where they play (see readers.py) ----------------------------
    readers = Readers(os.path.join(os.path.dirname(plex.config.auth_file) or ".", "readers.json"))
    app.extensions["marquee_readers"] = readers

    def speaker_choices() -> list[dict]:
        """The speakers a reader can play on (a Music Assistant group counts as one)."""
        try:
            found = player.speakers()
        except Exception as e:  # noqa: BLE001 - a reader still shows what it has
            logging.getLogger(__name__).info("Couldn't list speakers: %s", e)
            return []
        return [{"id": s["id"], "name": s["name"], "kind": s["kind"], "group": bool(s.get("group"))}
                for s in found if not s.get("synced_to") and not s.get("limited")]

    def zone_summary(zone: dict | None) -> dict | None:
        """What a reader's speakers are playing, for its screen and Home Assistant."""
        if not zone:
            return None
        track = zone.get("track") or {}
        image = track.get("image") or ""
        if not image.startswith(("http://", "https://")):
            image = public_art(track) if track.get("id") else None
        book = zone.get("book") or {}
        position = book.get("position") if book.get("position") is not None else zone.get("position")
        return {"zone": zone["id"], "state": zone["state"], "title": track.get("name") or "",
                "artist": track.get("artist") or "", "album": track.get("album") or "", "image": image or None,
                "position": int(position or 0), "duration": int(book.get("duration") or track.get("duration") or 0),
                "book": bool(book), "volume": zone.get("volume"), "speakers": zone.get("names") or []}

    def reader_zone(reader: dict, zones: list[dict] | None = None) -> dict | None:
        wanted = set(reader["speakers"])
        if not wanted:
            return None
        try:
            zones = player.now() if zones is None else zones
        except Exception:  # noqa: BLE001
            return None
        return next((z for z in zones if wanted & set(z["speakers"])), None)

    def with_movie_player(reader: dict) -> dict:
        """A Movie Remote's movie player is the one chosen for the box under Devices."""
        if reader["kind"] == "remote":
            device = next((d for d in plex.app_pairings.devices() if f"remote:{d['id']}" == reader["id"]), None)
            return {**reader, "movie_player": device.get("player") if device else None}
        return reader

    def reader_state(reader: dict, choices: list[dict] | None = None, zones: list[dict] | None = None) -> dict:
        """A reader, with its speakers by name, every speaker it could use, and what's playing."""
        choices = speaker_choices() if choices is None else choices
        names = {c["id"]: c["name"] for c in choices}
        named = lambda ids: [names.get(s) or player.speaker_name(s) for s in ids]  # noqa: E731
        return {**with_movie_player(reader), "speaker_names": named(reader["speakers"]),
                "default_names": named(reader["default"]),
                "choices": [{**c, "on": c["id"] in reader["speakers"]} for c in choices],
                "now": zone_summary(reader_zone(reader, zones))}

    def reader_action(reader_id: str, body: dict) -> dict:
        """Something done on a reader (a Movie Remote's screen, or Home Assistant for a Tessera
        screen): choose its speakers, put back or replace its default, or control what plays.
        Changing the speakers moves what's playing there. Raises ReaderError."""
        reader = readers.get(reader_id)
        if reader is None:
            raise ReaderError("Marquee doesn't know that reader")
        action = body.get("action")
        zone = reader_zone(reader)
        message = None
        try:
            if action == "speaker":
                speaker, on = body.get("speaker"), bool(body.get("on"))
                if not isinstance(speaker, str) or not speaker:
                    raise ReaderError("Which speaker?")
                speakers = [s for s in reader["speakers"] if s != speaker] + ([speaker] if on else [])
                if not speakers:
                    raise ReaderError("Keep at least one speaker on")
                reader = readers.choose(reader_id, speakers)
            elif action == "speakers":
                reader = readers.choose(reader_id, body.get("speakers"))
            elif action == "restore":
                reader = readers.restore(reader_id)
            elif action == "make_default":
                reader = readers.make_default(reader_id)
                message = "These speakers are now the default"
            elif action in ("pause", "play", "toggle", "next", "previous", "stop"):
                if zone is None:
                    raise ReaderError("Nothing is playing on this reader's speakers")
                if action == "toggle":
                    action = "pause" if zone["state"] == "playing" else "play"
                player.control(zone["id"], action)
                return reader_state(readers.get(reader_id))
            elif action == "volume":
                level = body.get("level")
                if not isinstance(level, (int, float)) or isinstance(level, bool):
                    raise ReaderError("Volume is a number from 0 to 100")
                for speaker in reader["speakers"]:
                    player.volume(speaker, int(level))
                return reader_state(readers.get(reader_id))
            else:
                raise ReaderError("Send an action: speaker, speakers, restore, make_default, pause, play, "
                                  "toggle, next, previous, stop or volume")
            if zone and reader["speakers"] and set(reader["speakers"]) != set(zone["speakers"]):
                try:
                    player.move(zone["id"], reader["speakers"])
                except (SpeakerError, MusicSourceError) as e:
                    message = str(e)
        except (SpeakerError, MusicSourceError) as e:
            raise ReaderError(str(e)) from None
        state = reader_state(reader)
        if message:
            state["message"] = message
        return state

    def box_reader(state: dict) -> dict:
        """A reader's state as small as the Movie Remote needs it (it reads up to 4 KB)."""
        now = state.get("now")
        return {"id": state["id"], "name": state["name"], "default": state["default"], "speakers": state["speakers"],
                "is_default": state["is_default"], "speaker_names": state["speaker_names"],
                "choices": [{"id": c["id"], "name": c["name"], "on": c["on"]} for c in state["choices"]],
                "now": now and {"state": now["state"], "title": now["title"], "artist": now["artist"],
                                "book": now["book"]},
                **({"message": state["message"]} if state.get("message") else {})}

    # ---- Now Playing on Marquee: the room's music or book on the TV ---------------------
    # A Marquee shows what it plays itself (as a speaker), or what the speakers of the reader
    # whose card was just read play (the reader's "screen").

    showing: dict[str, str] = {}  # a Marquee's name (casefolded) -> the reader it shows
    detail_cache: dict[str, tuple[float, object]] = {}

    def cached(key: str, seconds: float, make):
        hit = detail_cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = make()
        detail_cache[key] = (time.monotonic(), value)
        if len(detail_cache) > 200:
            for old in sorted(detail_cache, key=lambda k: detail_cache[k][0])[:100]:
                detail_cache.pop(old, None)
        return value

    def show_on_screen(reader: dict | None) -> None:
        """A music or book card was read: show it on the reader's Marquee screen, if it has
        one and it's open (or the reader may wake its TV)."""
        screen = reader and reader.get("screen")
        if not screen or f"marquee:{screen}" in reader["speakers"]:
            return  # no screen; or Marquee plays it, and shows it anyway
        is_open = plex.app_inbox.status(screen) is not None
        if not is_open and not reader.get("wake"):
            return
        showing[screen.casefold()] = reader["id"]
        plex.app_inbox.send(screen, "nowplaying", reader=reader["id"])
        if not is_open:
            threading.Thread(target=plex.prepare_tv, args=(screen,), daemon=True).start()

    def marquee_zone(device: str) -> tuple[dict | None, dict | None]:
        """What a Marquee shows: what it plays itself, else its reader's speakers' music."""
        try:
            zones = player.now()
        except Exception:  # noqa: BLE001
            zones = []
        zone = next((z for z in zones if f"marquee:{device}" in z["speakers"]), None)
        if zone:
            return zone, None
        reader = readers.get(showing.get(device.casefold(), ""))
        return (reader_zone(reader, zones), reader) if reader else (None, None)

    def assistant_extras(zone_id: str) -> dict:
        """Music Assistant's queue and its lyrics for the current track."""
        queue = player.queue(zone_id)
        own = None
        try:
            current = (player._call("player_queues/get", queue_id=zone_id.split(":", 1)[1]) or {}).get("current_item") or {}
            metadata = (current.get("media_item") or {}).get("metadata") or {}
            from .lyrics import parse
            own = parse(metadata.get("lrc_lyrics")) or parse(metadata.get("lyrics"))
        except Exception:  # noqa: BLE001
            pass
        return {"queue": queue, "lyrics": own}

    def book_details(book_id: str) -> dict:
        opened = music_library.open(book_id)
        item = opened["item"]
        return {"title": item["name"], "author": item.get("artist") or "", "image": public_art(item) if item.get("image") else None,
                "chapters": opened.get("chapters") or []}

    def now_playing(device: str) -> dict:
        """Everything Marquee's Now Playing screen shows."""
        zone, reader = marquee_zone(device)
        if zone is None:
            return {"state": "idle", "reader": reader and reader["name"]}
        summary = zone_summary(zone)
        track = zone.get("track") or {}
        out = {**{k: summary[k] for k in ("state", "title", "artist", "album", "image", "position", "duration")},
               "zone": zone["id"], "speakers": summary["speakers"], "here": f"marquee:{device}" in zone["speakers"],
               "reader": reader and reader["name"], "track": track.get("id") or summary["title"],
               "queue": None, "index": None, "book": None, "lyrics": None}
        own_lyrics = None
        try:
            if zone["kind"] == "music_assistant":
                extras = cached(f"ma|{zone['id']}|{summary['title']}", 10, lambda: assistant_extras(zone["id"]))
                out["queue"], out["index"] = extras["queue"]["items"], extras["queue"]["index"]
                own_lyrics = extras["lyrics"]
            else:
                out["queue"], out["index"] = zone.get("queue"), zone.get("index")
        except Exception as e:  # noqa: BLE001 - the rest still shows
            logging.getLogger(__name__).debug("Now Playing queue: %s", e)
        book = player.progress.book_of(zone["id"])
        if book:
            try:
                details = cached(f"book|{book['id']}", 600, lambda: book_details(book["id"]))
                out["book"] = {**details, "position": book["position"], "duration": book["duration"]}
                if details.get("image"):
                    out["image"] = details["image"]
            except (MusicSourceError, Exception) as e:  # noqa: BLE001
                logging.getLogger(__name__).debug("Now Playing book: %s", e)
        else:
            known, found = lyrics.lookup({**track, "name": summary["title"], "artist": summary["artist"],
                                          "album": summary["album"], "duration": summary["duration"] or None,
                                          **({"source": split_id(track["id"])[0]} if track.get("id") else {})},
                                         own_lyrics)
            out["lyrics"] = found
            out["lyrics_loading"] = not known
        return out

    def tap_reader(body: dict) -> tuple[dict | None, dict | None]:
        """For a card read in Home Assistant: the reader it was read on (``reader_id``, if Home
        Assistant said), and the settings it plays with. A reader without speakers, a movie
        player or a screen of its own uses its Home Assistant's (the paired one sending this)."""
        reader = None
        if isinstance(body.get("reader_id"), str) and body["reader_id"]:
            reader = readers.seen(f"ha:{body['reader_id']}", str(body.get("reader") or "") or None, "homeassistant")
        hub = plex.app_pairings.integration_device(bearer())
        hub = readers.seen(f"home:{hub['id']}", hub["name"], "assistant") if hub else None
        if reader is None or hub is None:
            return reader, reader or hub
        settings = dict(reader)
        if not reader["speakers"] and hub["speakers"]:
            settings.update(id=hub["id"], speakers=hub["speakers"])  # its Now Playing follows the hub's speakers
        if not reader["movie_player"]:
            settings["movie_player"] = hub["movie_player"]
        if not reader["screen"]:
            settings.update(screen=hub["screen"], wake=hub["wake"])
        return reader, settings

    def card_speakers(card: dict, reader: dict | None) -> list[str]:
        """Where a music or book card plays: the speakers of the reader it's held to. Raises
        MusicError if it has none."""
        if reader and reader["speakers"]:
            return list(reader["speakers"])
        if reader:
            raise MusicError(f"{reader['name']} has no speakers yet. Choose its default output in Marquee, "
                             "under Devices › Readers.")
        raise MusicError("Hold the card to a reader to play it")

    def house_speakers() -> list[str]:
        """For a card started from its QR code (no reader): a paired Home Assistant's speakers,
        else the first reader's that has some."""
        with_speakers = [r for r in all_readers() if r["speakers"]]
        for reader in sorted(with_speakers, key=lambda r: r["kind"] != "assistant"):
            return list(reader["speakers"])
        raise MusicError("Hold the card to a reader to play it. Choose the readers' speakers in Marquee, "
                         "under Devices › Readers.")

    def play_in_background(key, client, resume):
        try:
            plex.play(key, client, resume)
            update = {"done": time.monotonic()}
        except Exception as e:  # report anything to the screen rather than dying silently
            logging.getLogger(__name__).warning("Background play of %s failed: %s", key, e)
            update = {"done": time.monotonic(), "error": str(e)}
        with pending_lock:
            if pending.get("key") == key:
                pending.update(update)

    @app.get("/m/<key>")
    def play(key):
        if request.args.get("async") in ("1", "true"):
            try:
                movie = plex.find_movie(key)
            except PlexNFCError as e:
                return jsonify(state="error", message=str(e)), 404
            with pending_lock:
                pending.clear()
                pending.update(key=key, title=movie.title, year=movie.year, started=time.monotonic())
            threading.Thread(target=play_in_background, daemon=True,
                             args=(key, tap_client(), resume_param())).start()
            return jsonify(state="starting", key=key, title=movie.title), 202
        client_name = tap_client()
        try:
            movie, client = plex.play(key, client_name, resume_param())
        except PlexNFCError as e:
            return page("Can't play", f'<h1 class="err">Can\'t play that</h1><p>{escape(str(e))}</p>', 404)
        # Tapped with an iPhone or iPad that plays it itself: the tap opened Safari, so go
        # back to Marquee, which collects the movie when it's open (Safari asks first)
        own = plex.app_pairings.handheld_at(request.remote_addr)
        back = ('<p><a class="btn" href="moviebook://open">Open Marquee</a></p>'
                '<script>location.href = "moviebook://open"</script>'
                if own and (client_name or "").casefold() == f"app:{own}".casefold() else "")
        return page(
            f"Playing {movie.title}",
            f'<img src="/poster/{escape(key)}" alt=""><h1>{escape(movie.title)}</h1>'
            f"<p>Now playing on {escape(client.title)}</p>{back}",
        )

    @app.get("/<any(pause, play, stop):action>")
    def control(action):
        try:
            client = plex.control(action, tap_client())
        except PlexNFCError as e:
            return page("Error", f'<h1 class="err">{escape(str(e))}</h1>', 404)
        label = {"pause": "Paused", "play": "Resumed", "stop": "Stopped"}[action]
        return page(label, f"<h1>{label}</h1><p>{escape(client.title)}</p>")

    @app.get("/off")
    def off():
        try:
            plex.tv_off()
        except PlexNFCError as e:
            return page("Error", f'<h1 class="err">{escape(str(e))}</h1>', 404)
        return page("Off", "<h1>Good night</h1>")

    # ---- the Marquee Apple TV app ----------------------------------------------------

    def bearer() -> str | None:
        auth = request.headers.get("Authorization", "")
        return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None

    def paired_device() -> str | None:
        """The paired Apple TV making this request, from its ``Authorization: Bearer`` secret."""
        return plex.app_pairings.device(bearer())

    # This machine itself, by any of its addresses (including the tag address's host)
    panel_addresses = {"127.0.0.1", "::1"}
    panel_addresses.add(urlsplit(config.public_base_url).hostname or "")

    def from_panel() -> bool:
        """Pairings are approved only from the panel on the machine Marquee Server runs on,
        never from another device on the network."""
        address = (request.remote_addr or "").removeprefix("::ffff:")
        return address in panel_addresses or is_own_address(address)

    def app_route(rule, **options):
        """A /tv route for paired Apple TVs: passes the Apple TV's name as ``device``."""
        def decorator(fn):
            def wrapped(**kwargs):
                device = paired_device()
                if device is None:
                    return jsonify(ok=False, message="This Apple TV isn't paired"), 401
                try:
                    return fn(device=device, **kwargs)
                except PlexNFCError as e:
                    return jsonify(ok=False, message=str(e)), 404
            wrapped.__name__ = fn.__name__
            return app.route(rule, **options)(wrapped)
        return decorator

    @app.post("/tv/pair")
    def tv_pair():
        body = request.get_json(silent=True) or {}
        return jsonify(plex.app_pairings.start(body.get("name", ""), body.get("kind", "tv"), request.remote_addr))

    @app.get("/tv/expected")
    def tv_expected():
        """Whether this server is expecting Marquee on the Apple TV asking: Marquee asks
        every server it finds when it opens, and pairs with one that is."""
        name = plex.app_pairings.expected(request.remote_addr)
        return jsonify(name=name) if name else ("", 204)

    @app.get("/tv/pair/<pair_id>")
    def tv_pair_status(pair_id):
        result = plex.app_pairings.poll(pair_id)
        return jsonify(result) if result else (jsonify(status="expired"), 404)

    # ---- the Marquee Remote phone app ------------------------------------------------

    def phone_route(rule, **options):
        """A /phone route for paired phones: passes the phone as ``phone``, and notes the
        address it's using, which is how its taps (opened in Safari) are recognized."""
        def decorator(fn):
            def wrapped(**kwargs):
                auth = request.headers.get("Authorization", "")
                phone = plex.app_pairings.phone(
                    auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None,
                    request.remote_addr)
                if phone is None:
                    return jsonify(ok=False, message="This phone isn't paired"), 401
                return fn(phone=phone, **kwargs)
            wrapped.__name__ = fn.__name__
            return app.route(rule, **options)(wrapped)
        return decorator

    @phone_route("/phone/players")
    def phone_players(phone):
        return jsonify(players=plex.players(), default=plex.default_player(), selected=phone["player"],
                       name=phone["name"])

    @phone_route("/phone/player", methods=["POST"])
    def phone_player(phone):
        player = (request.get_json(silent=True) or {}).get("player")
        if player is not None and not (isinstance(player, str) and 0 < len(player) <= 200):
            return jsonify(ok=False, message="player must be a player ID, or null for the default"), 400
        plex.app_pairings.set_player(phone["id"], player)
        return jsonify(ok=True, selected=player)

    # ---- the Marquee integration for Home Assistant (custom_components/marquee) ------

    def ha_route(rule, **options):
        """A /ha route for a paired Home Assistant."""
        def decorator(fn):
            def wrapped(**kwargs):
                if plex.app_pairings.integration(bearer()) is None:
                    return jsonify(ok=False, message="This Home Assistant isn't paired"), 401
                try:
                    return fn(**kwargs)
                except PlexNFCError as e:
                    return jsonify(ok=False, message=str(e)), 404
            wrapped.__name__ = fn.__name__
            return app.route(rule, **options)(wrapped)
        return decorator

    def marquee_players() -> tuple[list[dict], str]:
        """Every paired Marquee and what it's playing, with a tag that changes when one
        starts, pauses or stops something (not as the position moves on)."""
        players = []
        for d in plex.app_pairings.devices():
            if d["kind"] != "tv":
                continue
            status = plex.app_inbox.status(d["name"]) or {}
            player = {"id": f"app:{d['name']}", "device_id": d["id"], "name": d["name"],
                      "state": status.get("state") or "idle"}
            if player["state"] != "idle" and status.get("key"):
                player.update(key=status["key"], title=status.get("title") or "", poster=f"/poster/{status['key']}",
                              position=status.get("position") or 0, duration=status.get("duration") or 0)
            players.append(player)
        tag = repr([(p["device_id"], p["name"], p["state"], p.get("key")) for p in players])
        return players, hashlib.sha256(tag.encode()).hexdigest()[:16]

    @ha_route("/ha/players")
    def ha_players():
        """Marquee players and what they're playing. With ``?tag=`` from the last answer, waits
        (up to ``wait`` seconds, 25 at most) for something to change, so Home Assistant hears at once."""
        deadline = time.monotonic() + min(request.args.get("wait", 25, type=float), 25)
        while True:
            players, tag = marquee_players()
            ha_readers = [r for r in readers.all() if r["kind"] == "homeassistant"]
            zones = None
            if ha_readers:
                # A reader's controls change with its speakers and what plays there (not as it plays on)
                try:
                    zones = player.now()
                except Exception:  # noqa: BLE001
                    zones = []
                summary = [(r["id"], r["name"], r["speakers"], r["default"], r["page"],
                            (lambda z: z and (z["id"], z["state"], (z.get("track") or {}).get("name")))(
                                reader_zone(r, zones))) for r in ha_readers]
                tag = hashlib.sha256(f"{tag}{readers.version}{summary!r}".encode()).hexdigest()[:16]
            remaining = deadline - time.monotonic()
            if tag != request.args.get("tag") or remaining <= 0:
                choices = speaker_choices() if ha_readers else []
                return jsonify(players=players, tag=tag,
                               readers=[reader_state(r, choices, zones) for r in ha_readers])
            plex.app_inbox.wait_for_change(min(remaining, 2))  # also notices a Marquee that went quiet

    @ha_route("/ha/control", methods=["POST"])
    def ha_control():
        body = request.get_json(silent=True) or {}
        if body.get("action") not in ("pause", "play", "stop") or not isinstance(body.get("player"), str):
            return jsonify(ok=False, message="Send a player and an action: pause, play or stop"), 400
        plex.control(body["action"], body["player"])
        return jsonify(ok=True)

    @ha_route("/ha/tap", methods=["POST"])
    def ha_tap():
        """A card read by an NFC reader in Home Assistant (a Tessera screen, a phone...), sent
        on by the integration with the reader's device (``reader_id``) and name (``reader``):
        link it during a scan, or say what it plays. Music plays on the reader's speakers if it
        has any, else on its Home Assistant's (Devices › Readers), else the card's; for old Music Assistant cards the integration plays it itself
        (``play``). A movie starts on the reader's Marquee player (or its Home Assistant's), else the default one. The
        answer carries the reader (``reader``: its ID and Tessera ``page``), to show its controls."""
        body = request.get_json(silent=True) or {}
        uid = str(body.get("uid") or "")
        reader, settings = tap_reader(body)
        about = {"reader": {"id": reader["id"], "page": reader["page"]}} if reader else {}

        def answer(**fields):
            return jsonify(**fields, **about)

        if (scanned := plex.cards.scanned(uid)) is not None:
            return jsonify(ok=True, scan=True, message=scanned["message"], result=scanned["result"])
        card = plex.cards.card(uid)
        if card and "music" in card:
            item = card["music"]
            reply = dict(ok=True, result="music", title=music_title(item), message=f"Playing {music_title(item)}")
            if "id" in item:  # linked on the Music tab: Marquee plays it
                try:
                    reply["message"] = play_card_music(card, speakers=card_speakers(card, settings)).replace("\n", " ")
                    show_on_screen(settings)
                except MusicError as e:
                    return answer(ok=False, result="error", title=music_title(item),
                                  message=f"Couldn't play {music_title(item)}: {e}")
                return answer(**reply)
            speaker = music.speaker_name(card["speaker"]) or card.get("speaker_name") or card["speaker"]
            reply["message"] += f" on {speaker}"
            if music.plays_here():  # connected to Music Assistant directly: play it from here
                try:
                    music.play(item, card["speaker"])
                except MusicError as e:
                    return answer(ok=False, result="error", title=music_title(item),
                                  message=f"Couldn't play {music_title(item)}: {e}")
                return answer(**reply)
            entity = bridge.entity_for(card["speaker"]) or card["speaker"]
            return answer(**reply, play={"uri": item["uri"], "media_type": item["media_type"], "speaker": entity})
        if card:
            movie = plex.find_library_movie(card["key"], card.get("title"), card.get("year"))
            where = settings["movie_player"] if settings else None
            threading.Thread(target=play_in_background, daemon=True, args=(movie.key, where, False)).start()
            return answer(ok=True, result="movie", title=movie.title, message=f"Playing {movie.title}")
        if normalize_uid(uid):
            plex.cards.unknown = {"uid": normalize_uid(uid), "at": time.time()}
        return answer(ok=False, result="unknown", message="This card isn't linked. Link it in Marquee, under Cards.")

    @ha_route("/ha/readers/<reader_id>", methods=["POST"])
    def ha_reader(reader_id):
        """Home Assistant's controls for a reader (its media player, speaker switches and
        default buttons): see reader_action."""
        try:
            return jsonify(ok=True, reader=reader_action(reader_id, request.get_json(silent=True) or {}))
        except ReaderError as e:
            return jsonify(ok=False, message=str(e)), 400

    @ha_route("/ha/music/poll", methods=["POST"])
    def ha_music_poll():
        """The integration's open request: it sends the Music Assistant speakers it can see,
        and gets the music jobs waiting (``search`` or ``play``), waiting up to 25 s for one."""
        body = request.get_json(silent=True) or {}
        speakers = body.get("speakers") if isinstance(body.get("speakers"), list) else []
        jobs = bridge.poll(speakers, bool(body.get("music_assistant")), request.args.get("wait", 25, type=float))
        return jsonify(jobs=jobs)

    @ha_route("/ha/music/jobs/<int:job_id>", methods=["POST"])
    def ha_music_done(job_id):
        body = request.get_json(silent=True) or {}
        bridge.finish(job_id, bool(body.get("ok")), body.get("result"), body.get("message"))
        return jsonify(ok=True)

    @ha_route("/ha/play", methods=["POST"])
    def ha_play():
        """Start a movie (``key``, e.g. tmdb-603) on a player, from the beginning unless ``resume``."""
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get("player"), str):
            return jsonify(ok=False, message="Choose a player"), 400
        movie = plex.find_movie(body.get("key", ""))
        threading.Thread(target=play_in_background, daemon=True,
                         args=(body["key"], body["player"], bool(body.get("resume")))).start()
        return jsonify(ok=True, title=movie.title, player=body["player"]), 202

    @app_route("/tv/inbox")
    def tv_inbox(device):
        command = plex.app_inbox.next(device, request.args.get("after", 0, type=int))
        return jsonify(command) if command else ("", 204)

    @app_route("/tv/status", methods=["POST"])
    def tv_status(device):
        status = request.get_json(silent=True) or {}
        plex.app_inbox.set_status(device, {**status, "player": device})
        return jsonify(ok=True)

    @app_route("/tv/library")
    def tv_library(device):
        return jsonify(rows=plex.app_library())

    @app_route("/tv/movies")
    def tv_movies(device):
        return jsonify(plex.app_movies(request.args.get("start", 0, type=int),
                                       min(request.args.get("size", 60, type=int), 200)))

    @app_route("/tv/search")
    def tv_search(device):
        return jsonify(items=plex.app_search(request.args.get("q", "")))

    # Marquee on an iPhone or iPad: where it plays (itself, or another player), like Marquee
    # Remote's choice. Asking notes its address, so its taps opened in Safari play there too.

    @app_route("/tv/players")
    def tv_players(device):
        me = plex.app_pairings.handheld(bearer(), request.remote_addr)
        own = f"app:{device}".casefold()
        return jsonify(players=[p for p in plex.players() if p["id"].casefold() != own],
                       selected=me and me["player"], name=device)

    @app_route("/tv/player", methods=["POST"])
    def tv_player(device):
        player = (request.get_json(silent=True) or {}).get("player")
        if player is not None and not (isinstance(player, str) and 0 < len(player) <= 200):
            return jsonify(ok=False, message="player must be a player ID, or null for this device"), 400
        me = plex.app_pairings.handheld(bearer(), request.remote_addr)
        plex.app_pairings.set_player(me["id"], player)
        return jsonify(ok=True, selected=player)

    @app_route("/tv/start", methods=["POST"])
    def tv_start(device):
        """Start a movie on another player, e.g. a scanned book code while that player is chosen.
        Returns at once: switching on an Apple TV and opening Marquee there takes a while."""
        body = request.get_json(silent=True) or {}
        player = body.get("player")
        if not (isinstance(player, str) and 0 < len(player) <= 200):
            return jsonify(ok=False, message="Choose a player"), 400
        movie = plex.find_movie(body.get("key", ""))
        threading.Thread(target=play_in_background, daemon=True,
                         args=(body["key"], player, body.get("resume"))).start()
        return jsonify(ok=True, title=movie.title, player=player), 202

    @app_route("/tv/movie/<key>")
    def tv_movie(device, key):
        """A movie's details and how far it's been watched (``watched``, seconds), so
        Marquee can ask whether to resume a scanned movie."""
        movie = plex.find_movie(key)
        return jsonify(**app_movie(movie, key), watched=int((getattr(movie, "viewOffset", 0) or 0) / 1000))

    @app_route("/tv/play", methods=["POST"])
    def tv_play(device):
        body = request.get_json(silent=True) or {}
        resume = body.get("resume")
        return jsonify(plex.app_play_details(device, body.get("key", ""), resume=resume))

    @app_route("/tv/nowplaying")
    def tv_nowplaying(device):
        """The music or book Marquee shows: what it plays itself, or what the speakers of the
        reader whose card was just read play. With the cover, the queue, a book's chapters
        and the lyrics."""
        return jsonify(now_playing(device))

    @app_route("/tv/nowplaying", methods=["POST"])
    def tv_nowplaying_control(device):
        """Now Playing's controls: ``toggle``, ``pause``, ``play``, ``next``, ``previous``,
        ``stop``, ``seek`` (``value``: seconds into the track), ``book`` (seconds into the
        book), ``jump`` (a queue index), and ``hide`` (stop showing a reader's music)."""
        body = request.get_json(silent=True) or {}
        action, value = body.get("action"), body.get("value")
        if action == "hide":
            showing.pop(device.casefold(), None)
            return jsonify(ok=True)
        zone, _ = marquee_zone(device)
        if zone is None:
            return jsonify(ok=False, message="Nothing is playing"), 400
        if action == "toggle":
            action = "pause" if zone["state"] == "playing" else "play"
        if action not in ("pause", "play", "next", "previous", "stop", "seek", "book", "jump"):
            return jsonify(ok=False, message="Send an action: toggle, pause, play, next, previous, stop, "
                                             "seek, book, jump or hide"), 400
        try:
            player.control(zone["id"], action, value)
        except (SpeakerError, MusicSourceError, ValueError, TypeError) as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(now_playing(device))

    # ---- the Movie Remote: an ESP32 box with an NFC reader and a touchscreen -----------
    # It pairs like Marquee (showing its code on its screen), then sends the tags it reads
    # and its buttons with its secret. Nothing here works for a box that isn't paired.

    remote_pending: dict[str, dict] = {}  # remote ID -> the movie it just started, until it plays

    def remote_route(rule, **options):
        def decorator(fn):
            def wrapped(**kwargs):
                remote = plex.app_pairings.remote(bearer())
                if remote is None:
                    return jsonify(ok=False, message="This Movie Remote isn't paired"), 401
                try:
                    result = fn(remote=remote, **kwargs)
                except PlexNFCError as e:
                    result = jsonify(ok=False, message=str(e)), 404
                return for_screen(result)
            wrapped.__name__ = fn.__name__
            return app.route(rule, **options)(wrapped)
        return decorator

    def for_screen(result):
        """A remote route's answer, with its text in characters the box's fonts have."""
        response = app.make_response(result)
        if response.is_json:
            response.set_data(json.dumps(box_json(response.get_json()), separators=(",", ":")))
        return response

    def player_label(player: str | None) -> str:
        player = player or plex.default_player() or ""
        return player.removeprefix("app:")

    @remote_route("/remote/tap", methods=["POST"])
    def remote_tap(remote):
        """A card or tag the box read: its ID (``uid``) and, if it has one written on it, its
        URL. Returns at once.

        During a card scan (started in the panel), the card is linked to the next movie
        instead of playing anything. Otherwise a card linked to a movie plays it, and a
        written tag plays the movie in its URL, from whichever address it was written with
        (only the /m/KEY path and the tag's own ?client= and ?resume= count).
        """
        body = request.get_json(silent=True) or {}
        uid = str(body.get("uid") or "")
        if (scanned := plex.cards.scanned(uid)) is not None:
            return jsonify(ok=True, scan=True, message=scanned["message"], result=scanned["result"])
        query: dict = {}
        card = plex.cards.card(uid)
        reader = readers.seen(f"remote:{remote['id']}", remote["name"], "remote")
        if card and "music" in card:
            # Music Assistant plays it on the card's speaker, directly or through Home Assistant
            item = card["music"]
            if "id" in item:  # linked on the Music tab: any source, on the box's speakers if it has some
                try:
                    message = play_card_music(card, speakers=card_speakers(card, reader))
                    show_on_screen(reader)
                except MusicError as e:
                    return jsonify(ok=False, message=f"Couldn't play {item['name']}.\n{e}"), 502
                return jsonify(ok=True, result="music", title=item["name"], message=message)
            try:
                playing = music.play(item, card["speaker"])
            except MusicError as e:
                return jsonify(ok=False, message=f"Couldn't play {item['name']}.\n{e}"), 502
            speaker = music.speaker_name(card["speaker"]) or card.get("speaker_name") or card["speaker"]
            return jsonify(ok=True, result="music", title=item["name"],
                           message=f"{'Playing' if playing else 'Starting'} {music_title(item)}\non {speaker}")
        if card:
            # A card is linked to a movie, whichever source has it: found by its key, else its title
            key = plex.find_library_movie(card["key"], card.get("title"), card.get("year")).key
        else:
            url = urlsplit(str(body.get("url") or ""))
            parts = url.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "m" or not parts[1]:
                plex.cards.unknown = {"uid": normalize_uid(uid), "at": time.time()} if normalize_uid(uid) else None
                return jsonify(ok=False, message="This card isn't linked to a movie.\n"
                                                 "Link it in the control panel, under Cards."), 404
            key, query = parts[1], parse_qs(url.query)
        movie = plex.find_movie(key)
        player = (query.get("client") or [remote["player"]])[0]
        resume = (query.get("resume") or [""])[0].lower() in ("1", "true", "yes")
        with pending_lock:
            remote_pending[remote["id"]] = {"key": key, "title": movie.title, "started": time.monotonic()}

        def run():
            try:
                plex.play(key, player, resume)
                update = {"done": time.monotonic()}
            except Exception as e:
                logging.getLogger(__name__).warning("Movie Remote couldn't play %s: %s", key, e)
                update = {"done": time.monotonic(), "error": str(e)}
            with pending_lock:
                if remote_pending.get(remote["id"], {}).get("key") == key:
                    remote_pending[remote["id"]].update(update)

        threading.Thread(target=run, daemon=True).start()
        return jsonify(ok=True, title=movie.title, player=player_label(player)), 202

    @remote_route("/remote/now")
    def remote_now(remote):
        """What the box's player is playing, for its screen."""
        player = remote["player"]
        try:
            status = dict(plex.now_playing(player))
        except Exception as e:
            status = {"state": "error", "message": f"Can't reach the player: {e}"}
        status["player"] = player_label(player)
        status["name"] = remote["name"]
        with pending_lock:
            started = remote_pending.get(remote["id"])
            if started:
                done = started.get("done")
                if status.get("key") == started["key"] and status.get("state") not in (None, "idle"):
                    remote_pending.pop(remote["id"], None)  # it's playing
                elif done and time.monotonic() - done > 30:
                    remote_pending.pop(remote["id"], None)  # the player never said so: give up
                elif "error" in started:
                    return jsonify(state="error", message=started["error"], title=started["title"],
                                   player=status["player"], name=remote["name"])
                else:
                    return jsonify(state="starting", title=started["title"], key=started["key"],
                                   poster=f"/poster/{started['key']}", position=0, duration=0,
                                   player=status["player"], name=remote["name"])
        return jsonify(status)

    @remote_route("/remote/reader")
    def remote_reader(remote):
        """The box's speakers screen: the speakers it plays music and book cards on, every
        speaker it could add, its default, and what's playing on them."""
        return jsonify(box_reader(reader_state(readers.seen(f"remote:{remote['id']}", remote["name"], "remote"))))

    @remote_route("/remote/reader", methods=["POST"])
    def remote_reader_action(remote):
        """The box's speaker toggles, Restore default, Make default and music controls (see
        reader_action)."""
        reader_id = readers.seen(f"remote:{remote['id']}", remote["name"], "remote")["id"]
        try:
            return jsonify(box_reader(reader_action(reader_id, request.get_json(silent=True) or {})))
        except ReaderError as e:
            return jsonify(ok=False, message=str(e)), 400

    @remote_route("/remote/unpair", methods=["POST"])
    def remote_unpair(remote):
        """The box is being reset: forget it, so the panel doesn't keep a box that's gone."""
        plex.app_pairings.remove(remote["id"])
        return jsonify(ok=True)

    @remote_route("/remote/control", methods=["POST"])
    def remote_control(remote):
        """The box's buttons: toggle, pause, play, stop, seek (with ``by`` seconds) and off."""
        body = request.get_json(silent=True) or {}
        action, player = body.get("action"), remote["player"]
        if action == "toggle":
            plex.toggle(player)
        elif action in ("pause", "play", "stop"):
            plex.control(action, player)
        elif action == "seek" and isinstance(body.get("by"), int):
            plex.seek(body["by"], player)
        elif action == "off":
            plex.tv_off()
        else:
            return jsonify(ok=False, message="Send an action: toggle, pause, play, stop, seek (with by) or off"), 400
        return jsonify(ok=True)

    @app.get("/now")
    def now():
        client = tap_client()
        try:
            status = plex.now_playing(client)
        except Exception as e:
            return jsonify(state="error", message=str(e))
        with pending_lock:
            if pending:
                done = pending.get("done")
                if status.get("key") == pending["key"] or (done and time.monotonic() - done > 30):
                    pending.clear()  # Plex has caught up (or gave up waiting)
                elif "error" in pending:
                    return jsonify(state="error", message=pending["error"], title=pending["title"])
                else:
                    return jsonify(state="starting", title=pending["title"], year=pending["year"],
                                   key=pending["key"], poster=f"/poster/{pending['key']}",
                                   position=0, duration=0, player=(client or config.app_player).removeprefix("app:"))
        return jsonify(status)

    def json_action(fn):
        try:
            client = fn()
        except PlexNFCError as e:
            return jsonify(ok=False, message=str(e)), 404
        return jsonify(ok=True, player=client.title)

    @app.get("/toggle")
    def toggle():
        return json_action(lambda: plex.toggle(tap_client()))

    @app.get("/seek")
    def seek():
        by = request.args.get("by", type=int)
        if by is None:
            return jsonify(ok=False, message="add ?by=SECONDS"), 400
        return json_action(lambda: plex.seek(by, tap_client()))

    @app.get("/poster/<key>")
    def poster(key):
        try:
            movie = plex.find_library_movie(key)
        except PlexNFCError:
            abort(404)
        w, h = request.args.get("w", type=int), request.args.get("h", type=int)
        sized = bool(w and h and 16 <= w <= 2000 and 16 <= h <= 3000)
        # The first source's poster that loads (Plex's, then Jellyfin's, a folder's), or a placeholder
        data, mime = plex.library.poster(movie, w if sized else None, h if sized else None)
        if sized:
            data, mime = resize_jpeg(data, w, h), "image/jpeg"
        return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=86400"})

    @app.get("/files/<key>/<token>/movie<ext>")
    def movie_file(key, token, ext):
        """A folder movie's file, for Marquee (AVPlayer asks for it in ranges)."""
        movie = plex.library.file_movie(key, token)
        if movie is None or ext.lower() not in DIRECT_TYPES:
            abort(404)
        return send_file(movie.ref, mimetype=DIRECT_TYPES[ext.lower()], conditional=True, max_age=0)

    @app.get("/files/<key>/<token>/hls/<stream_id>/<name>")
    def movie_stream(key, token, stream_id, name):
        """ffmpeg's HLS stream of a folder movie Marquee can't play as it is (MKV, AVI...)."""
        if plex.library.file_movie(key, token) is None:
            abort(404)
        found = plex.library.files.hls_file(key, stream_id, name)
        if found is None:
            abort(404)
        data, mime = found
        return Response(data, mimetype=mime, headers={"Cache-Control": "no-cache" if name.endswith(".m3u8")
                                                      else "max-age=3600"})

    # ---- music for speakers: signed addresses AirPlay, Cast and Music Assistant fetch ----

    @app.get("/music/file/<source_id>/<ref>/<token>/track<ext>")
    def music_file(source_id, ref, token, ext):
        """A track in a music folder (speakers ask for it in ranges)."""
        path = music_library.folder_file(source_id, ref, token)
        if not path or os.path.splitext(path)[1].lower() != ext.lower():
            abort(404)
        return send_file(path, mimetype=AUDIO_TYPES.get(ext.lower(), "application/octet-stream"),
                         conditional=True, max_age=0)

    @app.get("/music/art/<token>")
    def music_art_public(token):
        """A track's picture, for a Cast speaker's screen."""
        ident = request.args.get("id", "")
        if not music_library.check("art", ident, token):
            abort(404)
        try:
            found = music_library.art(ident)
        except MusicSourceError:
            found = None
        if not found:
            abort(404)
        return Response(found[0], mimetype=found[1], headers={"Cache-Control": "max-age=86400"})

    @app.get("/music/mp3/<token>")
    def music_mp3(token):
        """A track as MP3, made by ffmpeg, for speakers that can't play its own format."""
        ident = request.args.get("id", "")
        if not music_library.check("mp3", ident, token):
            abort(404)
        try:
            source = music_library.source_for_ffmpeg(ident)
            command = MusicPlayer._ffmpeg(source)
        except (MusicSourceError, SpeakerError) as e:
            return jsonify(ok=False, message=str(e)), 404
        import subprocess
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def chunks():
            try:
                while chunk := proc.stdout.read(65536):
                    yield chunk
            finally:
                if proc.poll() is None:
                    proc.kill()
        return Response(chunks(), mimetype="audio/mpeg")

    @app.get("/jf/<source_id>/<token>/Videos/<path:path>")
    def jellyfin_stream(source_id, token, path):
        """A Jellyfin source's HLS stream (playlists and segments), passed on to Marquee from the
        Jellyfin address this server uses, which the Apple TV may not be able to reach."""
        from .sources import JellyfinSource

        source = plex.library.source(source_id)
        if not (isinstance(source, JellyfinSource) and plex.library.files.check(f"jf:{source_id}", token)):
            abort(404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        try:
            upstream = requests.get(f"{source.url}/Videos/{path}", params=request.args.to_dict(flat=False),
                                    headers=headers, stream=True, timeout=(10, 120))
        except requests.RequestException as e:
            logging.getLogger(__name__).warning("Couldn't reach %s for Marquee: %s", source.name, e)
            abort(502)
        passed = {k: v for k, v in upstream.headers.items()
                  if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control")}
        return Response(upstream.iter_content(256 * 1024), status=upstream.status_code, headers=passed,
                        direct_passthrough=True)

    @app.get("/tags")
    def tags():
        rows = []
        for movie in plex.movies(request.args.get("collection") or None):
            url = f"{config.public_base_url}/m/{movie_key(movie)}"
            rows.append(
                f"<tr><td>{escape(movie.title)} ({movie.year or '?'})</td>"
                f"<td><code>{escape(url)}</code></td>"
                f'<td><button onclick="writeTag(\'{escape(url)}\')" class="nfc">Write tag</button></td></tr>'
            )
        controls = "".join(
            f"<tr><td><i>{a.title()} (control tag)</i></td><td><code>{config.public_base_url}/{a}</code></td>"
            f'<td><button onclick="writeTag(\'{config.public_base_url}/{a}\')" class="nfc">Write tag</button></td></tr>'
            for a in ("pause", "play", "stop") + (("off",) if config.ha_off_script else ())
        )
        # Web NFC only works in Chrome on Android, and only over HTTPS.
        script = """<script>
          if (!('NDEFReader' in window)) document.querySelectorAll('.nfc').forEach(b => b.remove());
          async function writeTag(url) {
            try { await new NDEFReader().write({records: [{recordType: 'url', data: url}]}); alert('Written: ' + url); }
            catch (e) { alert('Write failed: ' + e); }
          }</script>"""
        return page("NFC tags", f"<h1>NFC tag URLs</h1><table>{''.join(rows)}{controls}</table>{script}")

    @app.get("/book")
    def book_page():
        """The printable book, built on the fly (used from the Home Assistant sidebar)."""
        from .book import LAYOUTS, render_html

        layout = request.args.get("layout", "full")
        if layout not in LAYOUTS:
            abort(400)
        try:
            movies = list(plex.movies(request.args.get("collection") or None, request.args.get("label") or None,
                                      request.args.get("source") or None))
            if request.args.get("cards") == "1":  # only movies with a card linked
                with_cards = plex.cards.by_movie()
                movies = [m for m in movies if movie_key(m) in with_cards]
            # Reprinting one page: the page a movie is on (found by any of its keys), or a page number
            reprint = request.args.get("movie") or None
            if reprint:
                reprint = plex.find_library_movie(reprint).key
        except Exception as e:
            return page("Book", f'<h1 class="err">Couldn\'t load movies</h1><p>{escape(str(e))}</p>', 500)
        try:
            html = render_html(
                plex, config.public_base_url, movies, layout,
                title=request.args.get("title") or "Movie Night",
                qr=request.args.get("qr", "1") == "1",
                page_size=request.args.get("page_size") or "letter",
                # Relative, so it works under any address
                poster_src=lambda m: f"poster/{movie_key(m)}?w=1200&h=1800",
                page=request.args.get("page", type=int), page_of_movie=reprint,
            )
        except ValueError as e:
            return page("Book", f'<h1 class="err">Can\'t reprint that page</h1><p>{escape(str(e))}</p>', 404)
        return Response(html, mimetype="text/html")

    def music_cards_by_item() -> dict[str, dict]:
        """Item ID -> its (first) card, for music and audiobook cards."""
        out: dict[str, dict] = {}
        for card in plex.cards.all():
            item = card.get("music") or {}
            if item.get("id") and item["id"] not in out:
                out[item["id"]] = {"uid": card["uid"]}
        return out

    @app.get("/book/<any(music, books):kind>")
    def media_book_page(kind):
        """The printable book of albums (``kind`` music) or audiobooks (books).

        Music: a ``source``'s albums or playlists (``what``), or, with ``cards=1`` and no
        source, everything with a card. Books: every audiobook, or an author's or a series'
        (``what`` = ``author:<ID>`` or ``series:<ID>``)."""
        from .book import LAYOUTS
        from .mediabook import render

        args = request.args
        layout = args.get("layout", "full")
        if layout not in LAYOUTS:
            abort(400)
        cards = music_cards_by_item()
        only_cards = args.get("cards") == "1"
        what = args.get("what", "")
        try:
            if kind == "books":
                if what.startswith(("author:", "series:")):
                    kind_, ref = what.split(":", 1)
                    items = music_library.books.open(kind_, ref).get("albums") or []
                else:
                    items = music_library.books.browse("book", limit=2000)
            elif args.get("source"):
                items = music_library.browse(args["source"], what if what in ("album", "playlist") else "album",
                                             limit=2000)
            else:
                items = [c["music"] for c in plex.cards.all() if c.get("music", {}).get("id")
                         and not c["music"]["id"].startswith("abs|")]
                only_cards = True
        except (MusicSourceError, SpeakerError) as e:
            return page("Book", f'<h1 class="err">Couldn\'t load {kind}</h1><p>{escape(str(e))}</p>', 500)
        if only_cards:
            items = [i for i in items if i["id"] in cards]
        seen: set = set()
        items = [i for i in items if not (i["id"] in seen or seen.add(i["id"]))]
        html = render(items, kind, layout, title=args.get("title") or "", qr=args.get("qr", "1") == "1",
                      page_size=args.get("page_size") or "letter", cards=cards, base_url=config.public_base_url,
                      art_src=lambda i: public_art(i) if i.get("image") else None)
        return Response(html, mimetype="text/html")

    @app.get("/c/<uid>")
    def play_card_page(uid):
        """What a card plays, started from its QR code in a printed book."""
        card = plex.cards.card(uid)
        if card is None:
            return page("Unknown card", '<h1 class="err">That card isn\'t linked to anything</h1>', 404)
        if "music" not in card:
            return redirect(f"/m/{card['key']}")
        if "id" not in card["music"]:
            return page("Can't play", '<h1 class="err">Link this card again on the Music page</h1>', 400)
        try:
            message = play_card_music(card, house_speakers())
        except MusicError as e:
            return page("Can't play", f'<h1 class="err">Can\'t play that</h1><p>{escape(str(e))}</p>', 502)
        title, _, where = message.partition("\n")
        return page(title, f"<h1>{escape(music_title(card['music']))}</h1><p>{escape(title.split(' ')[0])} "
                           f"{escape(where)}</p>")

    @app.get("/tags.csv")
    def tags_csv():
        from .book import tag_rows, write_csv

        buf = io.StringIO()
        write_csv(tag_rows(plex, config.public_base_url, plex.movies(request.args.get("collection") or None)), buf)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tags.csv"})

    # PINs this server asked plex.tv for, so /plex/check only polls our own.
    pins: set[int] = set()

    @app.get("/plex/login")
    def plex_login():
        try:
            pin_id, auth_url = plexauth.start_pin(config.auth_file)
        except requests.RequestException as e:
            return page("Sign in", f'<h1 class="err">Couldn\'t reach plex.tv</h1><p>{escape(str(e))}</p>', 502)
        pins.add(pin_id)
        # Relative URLs, so this works under any address.
        return page("Sign in with Plex", f"""
          <h1>Sign in with Plex</h1>
          <p><a class="btn" href="{escape(auth_url)}" target="_blank" rel="noopener">Open plex.tv to sign in</a></p>
          <p id="status">Sign in on the tab that opens, then come back here.</p>
          <script>
            const started = Date.now();
            async function poll() {{
              if (Date.now() - started > 10 * 60 * 1000) {{
                document.getElementById('status').textContent = 'That took too long. Reload this page to try again.';
                return;
              }}
              try {{
                const r = await (await fetch('check?id={pin_id}')).json();
                if (r.signed_in) {{
                  document.getElementById('status').textContent = 'Signed in.';
                  location.href = '../';
                  return;
                }}
              }} catch (e) {{}}
              setTimeout(poll, 2000);
            }}
            poll();
          </script>""")

    @app.get("/plex/check")
    def plex_check():
        pin_id = request.args.get("id", type=int)
        if pin_id not in pins:
            return jsonify(signed_in=False), 404
        try:
            token = plexauth.check_pin(config.auth_file, pin_id)
        except requests.RequestException:
            return jsonify(signed_in=False)
        if token:
            pins.discard(pin_id)
            plex.set_token(token)
        return jsonify(signed_in=bool(token))

    @app.get("/plex/server")
    def plex_server():
        """The Plex server in use: PLEX_URL's, or the one found from the Plex sign-in."""
        try:
            return jsonify(name=plex.server.friendlyName, url=plex.plex_url)
        except PlexNFCError as e:
            return jsonify(error=str(e)), 503

    # ---- the control panel (panel/, built into plexnfc/static/panel) -----------------
    # Anyone on the network can look; only the panel on this machine can change anything.

    panel_dir = os.path.join(os.path.dirname(__file__), "static", "panel")

    @app.get("/")
    def index():
        if not os.path.exists(os.path.join(panel_dir, "index.html")):
            return page("plexnfc", '<h1 class="err">The control panel isn\'t built</h1>'
                                   "<p>Run <code>npm run build</code> in <code>panel/</code>.</p>", 500)
        return send_from_directory(panel_dir, "index.html", max_age=0)

    @app.get("/assets/<path:name>")
    def panel_assets(name):
        return send_from_directory(os.path.join(panel_dir, "assets"), name, max_age=31536000)

    def panel_route(rule, **options):
        """A /panel/api route. Changes are refused (403) unless they come from the panel itself."""
        def decorator(fn):
            def wrapped(**kwargs):
                if request.method != "GET" and not from_panel():
                    return jsonify(ok=False, message="Open the control panel on the server to change this"), 403
                try:
                    return fn(**kwargs)
                except (PlexNFCError, AppleTVError) as e:
                    return jsonify(ok=False, message=str(e)), 400
            wrapped.__name__ = fn.__name__
            return app.route(f"/panel/api{rule}", **options)(wrapped)
        return decorator

    def panel_body() -> dict:
        return request.get_json(silent=True) or {}

    @panel_route("/overview")
    def panel_overview():
        """Everything the panel shows that changes on its own: sign-in, pairing requests,
        paired devices and what each Marquee is playing, and linked Apple TVs."""
        devices = []
        for d in plex.app_pairings.devices():
            status = plex.app_inbox.status(d["name"]) if d["kind"] == "tv" else None
            if status and status.get("state", "idle") != "idle":
                status = {**status, "source": plex.playing_source(f"app:{d['name']}", status.get("key"))}
            else:
                status = None
            devices.append({**d, "status": status})
        return jsonify(
            can_manage=from_panel(),
            tag_address=config.public_base_url,
            tag_address_is_local="localhost" in config.public_base_url or "127.0.0.1" in config.public_base_url,
            default_player=plex.default_player(),
            default_player_label=f"Marquee on {config.app_player}" if config.app_player else "",
            off_tag=bool(config.ha_off_script),
            plex={"signed_in": bool(config.plex_token),
                  "signed_in_here": bool(config.plex_token) and plexauth.saved_token(config.auth_file) == config.plex_token},
            pairing_requests=plex.app_pairings.pending(),
            devices=devices,
            apple_tvs={"available": plex.apple_tvs.available,
                       "linked": [{"player": p, **link} for p, link in plex.apple_tvs.linked().items()]},
        )

    @panel_route("/players")
    def panel_players():
        """Every player a movie can go to: the paired Marquee Apple TVs."""
        return jsonify(players=plex.players(), default=plex.default_player())

    @panel_route("/movies")
    def panel_movies():
        cards = plex.cards.by_movie()
        found = plex.movies(request.args.get("collection") or None, request.args.get("label") or None,
                            request.args.get("source") or None)
        movies = []
        for m in found:
            key = movie_key(m)
            merged = plex.library.movie_for(m) if plex.library.sources else None
            sources = list(dict.fromkeys(source_name(c) for c in merged.copies)) if merged else [source_name(m)]
            files = [{"id": c.source_key, "source": c.source.name, "matched": c.matched}
                     for c in (merged.copies if merged else [m])
                     if isinstance(c, SourceMovie) and isinstance(c.source, FolderSource)]
            movies.append({"key": key, "title": m.title, "year": m.year, "url": f"{config.public_base_url}/m/{key}",
                           "cards": cards.get(key, []), "source": ", ".join(sources), "sources": sources,
                           "files": files})
        controls = [{"action": a, "url": f"{config.public_base_url}/{a}"}
                    for a in ("pause", "play", "stop") + (("off",) if config.ha_off_script else ())]
        return jsonify(movies=movies, controls=controls)

    @panel_route("/filters")
    def panel_filters():
        """Collections and labels to choose movies by."""
        return jsonify(plex.movie_filters())

    # ---- where movies come from: Plex, Jellyfin servers and folders (see sources.py) ----

    @panel_route("/sources")
    def panel_sources():
        return jsonify(plex.library.public())

    @panel_route("/sources", methods=["POST"])
    def panel_source_add():
        """Add a Jellyfin server (address, and an API key or a user name and password, which is
        only passed on to Jellyfin to sign in) or a folder (its path on this server)."""
        from .jellyfin import JellyfinError, sign_in

        body = panel_body()
        kind = body.get("kind")
        settings = {"name": body.get("name") or None}
        try:
            if kind == "folder":
                settings["path"] = str(body.get("path") or "")
            elif kind == "plex":
                settings["library"] = str(body.get("library") or "")
                if body.get("machine"):  # one of the Plex account's servers
                    if not config.plex_token:
                        return jsonify(ok=False, message="Sign in with Plex first to choose its servers"), 400
                    settings.update(connect_plex_server(config.plex_token, str(body["machine"])))
                else:  # an address and a token typed in
                    settings.update(url=str(body.get("url") or "").strip().rstrip("/"),
                                    token=str(body.get("token") or "").strip())
            elif kind == "jellyfin":
                url = str(body.get("url") or "").strip().rstrip("/")
                settings.update(url=url, library=str(body.get("library") or ""))
                if body.get("username"):
                    settings.update(sign_in(url, str(body["username"]), str(body.get("password") or "")))
                else:
                    settings["token"] = str(body.get("api_key") or "")
            source = plex.library.add(kind, **settings)
        except (SourceError, JellyfinError) as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, source=source.public(), **plex.library.public())

    @panel_route("/plex/servers")
    def panel_plex_servers():
        """The Plex servers the signed-in Plex account can reach, to add as sources."""
        from .sources import plex_account_servers

        if not config.plex_token:
            return jsonify(ok=False, message="Sign in with Plex first to list its servers"), 400
        try:
            servers = plex_account_servers(config.plex_token)
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 502
        added = {s.settings.get("machine") for s in plex.library.sources}
        try:
            signed_in = plex.server.machineIdentifier if plex.library.use_plex else None
        except Exception:  # noqa: BLE001 - only to mark it
            signed_in = None
        return jsonify(ok=True, servers=[
            {"machine": r.clientIdentifier, "name": r.name, "owned": bool(r.owned),
             "added": r.clientIdentifier in added, "signed_in": r.clientIdentifier == signed_in}
            for r in sorted(servers, key=lambda r: (not r.owned, r.name.casefold()))])

    @panel_route("/browse")
    def panel_browse():
        """The folders in a folder on this server, to choose a folder source from. Only the
        panel itself can look (it shows the server's files), even just to look."""
        from .sources import browse_folder

        if not from_panel():
            return jsonify(ok=False, message="Open the control panel on the server to browse its folders"), 403
        try:
            return jsonify(ok=True, **browse_folder(request.args.get("path") or None))
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400

    @panel_route("/sources/tmdb", methods=["POST"])
    def panel_tmdb_key():
        """Keep a TMDB API key (blank forgets it), for matching folder movies."""
        try:
            plex.library.set_tmdb_key(str(panel_body().get("key") or ""))
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, **plex.library.public())

    @panel_route("/sources/<source_id>/match", methods=["POST"])
    def panel_match_all(source_id):
        """Match a folder's unmatched movies with TMDB, in the background."""
        try:
            job = plex.library.start_match_all(source_id)
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, job=job, **plex.library.public())

    # ---- a folder movie's details and its match with TMDB ----

    def folder_movie_json(copy) -> dict:
        return {"id": copy.source_key, "key": plex.library.canonical(copy.key), "source": copy.source.name,
                "path": os.path.relpath(copy.ref, copy.source.path), "title": copy.title, "year": copy.year,
                "summary": copy.summary, "collections": list(copy.collections), "labels": list(copy.labels),
                "ids": copy.ids, "matched": copy.matched, "edited": plex.library.edits.get(copy.source_key) is not None,
                "file": copy.base or {}}

    def folder_movie_or_404(source_key):
        copy = plex.library.folder_copy(source_key)
        if copy is None:
            raise PlexNFCError("That movie file isn't in any folder source")
        return copy

    @panel_route("/files/<source_key>")
    def panel_folder_movie(source_key):
        return jsonify(ok=True, movie=folder_movie_json(folder_movie_or_404(source_key)))

    @panel_route("/files/<source_key>/edit", methods=["POST"])
    def panel_folder_movie_edit(source_key):
        """Change a folder movie's title, year, summary, collections or labels by hand."""
        copy = folder_movie_or_404(source_key)
        body = panel_body()
        edit = dict(plex.library.edits.get(source_key) or {})
        if "title" in body:
            title = str(body["title"] or "").strip()
            if not title:
                return jsonify(ok=False, message="A movie needs a title"), 400
            edit["title"] = title[:200]
        if "year" in body:
            year = str(body["year"] or "").strip()
            if year and not (year.isdigit() and 1880 <= int(year) <= 2100):
                return jsonify(ok=False, message="The year should be like 1979"), 400
            edit["year"] = int(year) if year else None
        if "summary" in body:
            edit["summary"] = str(body["summary"] or "")[:5000]
        for field in ("collections", "labels"):
            if field in body:
                values = body[field] if isinstance(body[field], list) else str(body[field] or "").split(",")
                edit[field] = list(dict.fromkeys(v for v in (str(x).strip()[:100] for x in values) if v))
        if "tmdb" in body:  # an ID typed by hand, or blank to drop it
            ids = dict(edit.get("ids", copy.ids))
            tmdb = str(body["tmdb"] or "").strip()
            if tmdb and not tmdb.isdigit():
                return jsonify(ok=False, message="A TMDB ID is a number, like 348"), 400
            if tmdb:
                ids["tmdb"] = tmdb
            else:
                ids.pop("tmdb", None)
            edit["ids"] = ids
        plex.library.edit_movie(copy, edit)
        return jsonify(ok=True, movie=folder_movie_json(copy))

    @panel_route("/files/<source_key>/match", methods=["POST"])
    def panel_folder_movie_match(source_key):
        """Match a folder movie with a TMDB movie (from the search)."""
        copy = folder_movie_or_404(source_key)
        tmdb_id = str(panel_body().get("tmdb_id") or "")
        if not tmdb_id.isdigit():
            return jsonify(ok=False, message="Choose a TMDB movie"), 400
        try:
            plex.library.match_movie(copy, tmdb_id)
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, movie=folder_movie_json(copy))

    @panel_route("/files/<source_key>/reset", methods=["POST"])
    def panel_folder_movie_reset(source_key):
        """Forget a folder movie's match and changes: back to what its file says."""
        copy = folder_movie_or_404(source_key)
        plex.library.edit_movie(copy, None)
        return jsonify(ok=True, movie=folder_movie_json(copy))

    @panel_route("/match/search")
    def panel_match_search():
        """TMDB's movies for a title (and year), with their posters."""
        from .metadata import poster_url

        query = (request.args.get("q") or "").strip()
        if not query:
            return jsonify(ok=False, message="Type a title to look for"), 400
        year = request.args.get("year") or ""
        try:
            results = plex.library.search_tmdb(query, int(year) if year.isdigit() else None)
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, results=[{**r, "poster": poster_url(r["poster_path"], 342) if r["poster_path"] else None}
                                         for r in results[:12]])

    @panel_route("/sources/<source_id>/refresh", methods=["POST"])
    def panel_source_refresh(source_id):
        """Read a source's movies again now (after adding files, say)."""
        source = plex.library.source(source_id)
        if source is None:
            return jsonify(ok=False, message="That source isn't there"), 404
        try:
            count = len(source.movies(fresh=True))
        except SourceError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, count=count, **plex.library.public())

    @panel_route("/sources/<source_id>/remove", methods=["POST"])
    def panel_source_remove(source_id):
        if not plex.library.remove(source_id):
            return jsonify(ok=False, message="That source isn't there"), 404
        return jsonify(ok=True, **plex.library.public())

    @panel_route("/sources/order", methods=["POST"])
    def panel_source_order():
        """The order to use sources in: a movie plays from the first one that has it."""
        order = panel_body().get("order")
        if not (isinstance(order, list) and all(isinstance(i, str) for i in order)):
            return jsonify(ok=False, message="Send the order as a list of source IDs"), 400
        plex.library.set_order(order)
        return jsonify(ok=True, **plex.library.public())

    @panel_route("/sources/plex", methods=["POST"])
    def panel_source_plex():
        """Use Plex's movies or not (when there are other sources to use instead)."""
        plex.library.set_plex(bool(panel_body().get("enabled")))
        return jsonify(ok=True, **plex.library.public())

    # ---- movie cards (see cards.py) ----

    def cards_state():
        unknown = plex.cards.unknown
        if unknown and (time.time() - unknown["at"] > 600 or plex.cards.card(unknown["uid"])):
            unknown = plex.cards.unknown = None
        return {"cards": plex.cards.all(), "scan": plex.cards.scan(), "unknown": unknown}

    @panel_route("/cards")
    def panel_cards():
        """Every linked card, the scan in progress, and the last card read that isn't linked."""
        return jsonify(cards_state())

    @panel_route("/cards/scan", methods=["POST"])
    def panel_scan_start():
        """Start linking cards: the next cards read go to these movies, in order."""
        try:
            items = panel_body().get("movies") or []
            plex.cards.start_scan([checked_music(m) if isinstance(m, dict) and "music" in m else m
                                   for m in items if isinstance(m, dict)])
        except ValueError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, **cards_state())

    @panel_route("/cards/scan/<any(skip, back, stop):action>", methods=["POST"])
    def panel_scan_step(action):
        if action == "stop":
            plex.cards.stop_scan()
        else:
            plex.cards.move(1 if action == "skip" else -1)
        return jsonify(ok=True, **cards_state())

    def checked_music(body: dict) -> dict:
        """``{"music"}`` from the panel, checked. The music (or book) is an item from the Music
        or Books tab; it plays on the speakers of the reader the card is held to. As before, a
        Music Assistant item for a Music Assistant player also needs its ``speaker``."""
        raw = body.get("music")
        if isinstance(raw, dict) and raw.get("id"):
            try:
                source, kind, ref = split_id(str(raw["id"]))
            except MusicSourceError as e:
                raise ValueError(str(e)) from None
            item = {"id": str(raw["id"]), "kind": kind, "media_type": kind, "name": str(raw.get("name") or "")[:200],
                    "artist": str(raw.get("artist") or "")[:200],
                    "image": raw["image"] if isinstance(raw.get("image"), str) and len(raw["image"]) < 2000 else None}
            if not item["name"]:
                raise ValueError("Choose some music")
            if source == "ma":
                item["uri"] = ref
            return {"music": item}
        speaker = str(body.get("speaker") or "").strip()
        if not speaker or len(speaker) > 200:
            raise ValueError("Choose a speaker")
        item = music_item(raw)
        return {"music": item, "speaker": speaker, "speaker_name": music.speaker_name(speaker) or speaker}

    def play_card_music(card: dict, speakers: list[str], wait: float = 4) -> str:
        """Start a music or book card from the Music or Books tab on ``speakers`` (a reader's):
        the box's message. Raises MusicError."""
        item = card["music"]
        name = " + ".join(player.speaker_name(s) or s for s in speakers)
        done = threading.Event()
        failed: list = []

        def run():
            try:
                player.play([item["id"]], speakers)
            except (MusicSourceError, SpeakerError, MusicError) as e:
                failed.append(e)
                if not done.is_set():
                    logging.getLogger(__name__).warning("Card music failed: %s", e)
            finally:
                done.set()
        threading.Thread(target=run, daemon=True).start()
        finished = done.wait(wait)
        if failed:
            raise MusicError(str(failed[0]))
        return f"{'Playing' if finished else 'Starting'} {music_title(item)}\non {name}"

    @panel_route("/music")
    def panel_music():
        """Whether Music Assistant is connected (directly, or through Home Assistant), and
        its speakers."""
        return jsonify(music.status())

    @panel_route("/music/assistant", methods=["POST"])
    def panel_music_assistant():
        """Connect to Music Assistant directly: ``url`` (blank to stop) and ``token`` (left
        out to keep the one there is). It's tried before it's kept."""
        body = panel_body()
        token = body.get("token")
        try:
            music.set_assistant(str(body.get("url") or ""), None if token in (None, "") else str(token))
        except MusicError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, **music.status())

    # ---- the Music tab ----

    def music_reply(fn):
        """Run a music call, answering its errors as a message."""
        try:
            return fn()
        except (MusicSourceError, SpeakerError, MusicError) as e:
            return jsonify(ok=False, message=str(e)), 400

    @panel_route("/music/sources")
    def panel_music_sources():
        """Where music comes from, the music folders added, and which kinds of speaker work."""
        return jsonify(sources=music_library.public(), folders=[f.public() for f in music_library.folders],
                       speakers=player.status())

    # ---- the Books section: audiobooks from Audiobookshelf ----
    # (browsing, pictures and playing go through the music routes, with source ``abs``)

    @panel_route("/books")
    def panel_books():
        """Whether Audiobookshelf is connected, and its libraries."""
        return jsonify(audiobookshelf=music_library.audiobookshelf.public(),
                       sources=music_library.public("books"), speakers=player.status())

    @panel_route("/books/audiobookshelf", methods=["POST"])
    def panel_books_audiobookshelf():
        """Connect Audiobookshelf: ``url`` (blank to disconnect) and ``token`` (an API key; left
        out to keep the one there is). It's tried before it's kept."""
        body = panel_body()
        token = body.get("token")
        return music_reply(lambda: (music_library.audiobookshelf.set(
            str(body.get("url") or ""), None if token in (None, "") else str(token)), panel_books())[1])

    @panel_route("/music/folders", methods=["POST"])
    def panel_music_folder_add():
        body = panel_body()
        return music_reply(lambda: (music_library.add_folder(str(body.get("path") or ""), str(body.get("name") or "")),
                                    panel_music_sources())[1])

    @panel_route("/music/folders/<source_id>/remove", methods=["POST"])
    def panel_music_folder_remove(source_id):
        music_library.remove_folder(source_id)
        return panel_music_sources()

    @panel_route("/music/folders/<source_id>/rescan", methods=["POST"])
    def panel_music_folder_rescan(source_id):
        return music_reply(lambda: (music_library.rescan(source_id), panel_music_sources())[1])

    @panel_route("/music/browse")
    def panel_music_browse():
        """A source's artists, albums, playlists or tracks (``kind``), optionally filtered (``q``)."""
        args = request.args
        return music_reply(lambda: jsonify(items=music_library.browse(
            args.get("source", ""), args.get("kind", "album"), (args.get("q") or "").strip()[:200],
            args.get("offset", 0, type=int), min(args.get("limit", 100, type=int), 500))))

    @panel_route("/music/search")
    def panel_music_search():
        """Every source's matches (or one ``source``'s)."""
        query = (request.args.get("q") or "").strip()[:200]
        if not query:
            return jsonify(items=[])
        return music_reply(lambda: jsonify(items=music_library.search(query, request.args.get("source") or None)))

    @panel_route("/music/item")
    def panel_music_item():
        """An item with what's in it: an artist's albums, an album's or playlist's tracks."""
        return music_reply(lambda: jsonify(music_library.open(request.args.get("id", ""))))

    @panel_route("/music/art")
    def panel_music_art():
        try:
            found = music_library.art(request.args.get("id", ""))
        except MusicSourceError:
            found = None
        if not found:
            abort(404)
        return Response(found[0], mimetype=found[1], headers={"Cache-Control": "max-age=86400"})

    @panel_route("/music/speakers")
    def panel_music_speakers():
        return jsonify(speakers=player.speakers(), kinds=player.status())

    @panel_route("/music/now")
    def panel_music_now():
        """What's playing where."""
        return jsonify(zones=player.now())

    @panel_route("/music/queue")
    def panel_music_queue():
        return music_reply(lambda: jsonify(player.queue(request.args.get("zone", ""))))

    @panel_route("/music/play", methods=["POST"])
    def panel_music_play():
        """Play items (``ids``) on speakers (``speakers``): ``option`` replace, next or add."""
        body = panel_body()
        ids = [str(i) for i in body.get("ids") or [] if i]
        speakers = [str(s) for s in body.get("speakers") or [] if s]
        position = body.get("position")
        return music_reply(lambda: jsonify(ok=True, zones=player.play(
            ids, speakers, str(body.get("option") or "replace"), int(body.get("start") or 0),
            float(position) if isinstance(position, (int, float)) else None)))

    @panel_route("/music/control", methods=["POST"])
    def panel_music_control():
        body = panel_body()
        return music_reply(lambda: (player.control(str(body.get("zone") or ""), str(body.get("action") or ""),
                                                   body.get("value")), jsonify(ok=True, zones=player.now()))[1])

    @panel_route("/music/volume", methods=["POST"])
    def panel_music_volume():
        body = panel_body()
        return music_reply(lambda: (player.volume(str(body.get("speaker") or ""), int(body.get("level") or 0)),
                                    jsonify(ok=True))[1])

    @panel_route("/music/airplay/pair", methods=["POST"])
    def panel_music_airplay_pair():
        """Ask an AirPlay speaker (an Apple TV) to show a PIN for pairing."""
        speaker = str(panel_body().get("speaker") or "")
        if not speaker.startswith("airplay:"):
            return jsonify(ok=False, message="Only AirPlay speakers pair"), 400
        return music_reply(lambda: jsonify(ok=True, pair_id=player.airplay.start_pairing(speaker[8:])))

    @panel_route("/music/airplay/pair/finish", methods=["POST"])
    def panel_music_airplay_pair_finish():
        body = panel_body()
        return music_reply(lambda: jsonify(ok=True, name=player.airplay.finish_pairing(
            str(body.get("pair_id") or ""), str(body.get("pin") or "")), speakers=player.speakers()))

    @panel_route("/music/airplay/unpair", methods=["POST"])
    def panel_music_airplay_unpair():
        speaker = str(panel_body().get("speaker") or "")
        player.airplay.unpair(speaker.removeprefix("airplay:"))
        return jsonify(ok=True, speakers=player.speakers())

    @panel_route("/music/group", methods=["POST"])
    def panel_music_group():
        """Music Assistant speakers in sync: ``leader`` and its ``members`` (none to ungroup)."""
        body = panel_body()
        return music_reply(lambda: (player.group(str(body.get("leader") or ""),
                                                 [str(m) for m in body.get("members") or []]),
                                    jsonify(ok=True, speakers=player.speakers()))[1])


    @panel_route("/cards/link", methods=["POST"])
    def panel_card_link():
        """Link one card (by its ID, such as the last unknown card read) to a movie, or to
        music or a book (``music``)."""
        body = panel_body()
        if "music" in body:
            try:
                m = checked_music(body)
                plex.cards.link_music(str(body.get("uid", "")), m["music"], m.get("speaker"), m.get("speaker_name"))
            except ValueError as e:
                return jsonify(ok=False, message=str(e)), 400
            return jsonify(ok=True, **cards_state())
        movie = plex.find_library_movie(str(body.get("key", "")))
        try:
            plex.cards.link(str(body.get("uid", "")), movie.key, movie.title, movie.year)
        except ValueError as e:
            return jsonify(ok=False, message=str(e)), 400
        return jsonify(ok=True, **cards_state())

    @panel_route("/cards/<uid>/remove", methods=["POST"])
    def panel_card_remove(uid):
        if not plex.cards.remove(uid):
            return jsonify(ok=False, message="That card isn't linked"), 404
        return jsonify(ok=True, **cards_state())

    @panel_route("/play", methods=["POST"])
    def panel_play():
        """Try a tag: start a movie on a player, as tapping it would."""
        body = panel_body()
        movie = plex.find_movie(body.get("key", ""))
        player = body.get("player") or None
        threading.Thread(target=play_in_background, daemon=True,
                         args=(body["key"], player, bool(body.get("resume")))).start()
        return jsonify(ok=True, title=movie.title), 202

    @panel_route("/control", methods=["POST"])
    def panel_control():
        body = panel_body()
        if body.get("action") not in ("pause", "play", "stop"):
            return jsonify(ok=False, message="Pause, play or stop"), 400
        plex.control(body["action"], body.get("player") or None)
        return jsonify(ok=True)

    @panel_route("/pairings/<pair_id>/approve", methods=["POST"])
    def panel_approve(pair_id):
        if not plex.app_pairings.approve(pair_id, panel_body().get("name")):
            return jsonify(ok=False, message="That request has expired. Ask again from the device."), 404
        return jsonify(ok=True)

    def all_readers() -> list[dict]:
        """Every reader: each paired Movie Remote and Home Assistant, and the readers in Home
        Assistant that have read a card."""
        as_reader = {"remote": ("remote", "remote"), "homeassistant": ("home", "assistant")}  # pairing kind: prefix, kind
        paired = {f"{as_reader[d['kind']][0]}:{d['id']}": (d, as_reader[d["kind"]][1])
                  for d in plex.app_pairings.devices() if d["kind"] in as_reader}
        for reader_id, (device, kind) in paired.items():
            readers.seen(reader_id, device["name"], kind, touch=False)
        for reader in readers.all():
            if reader["kind"] in ("remote", "assistant") and reader["id"] not in paired:
                readers.remove(reader["id"])  # its box or Home Assistant was removed
        return readers.all()

    @panel_route("/readers")
    def panel_readers():
        """The NFC readers, each with its default output, for Devices › Readers."""
        choices = speaker_choices()
        return jsonify(readers=[reader_state(r, choices) for r in all_readers()], speakers=choices,
                       players=plex.players(), default_player=plex.default_player(), screens=marquee_screens())

    @panel_route("/readers/<reader_id>", methods=["POST"])
    def panel_reader(reader_id):
        """Set a reader's default speakers (``default``), Marquee player (``movie_player``),
        Tessera page (``page``), and the Marquee screen that shows its Now Playing (``screen``,
        and ``wake`` to turn the TV on for it)."""
        body = panel_body()
        try:
            reader = readers.get(reader_id)
            if reader is None:
                raise ReaderError("Marquee doesn't know that reader")
            if reader["kind"] == "remote" and "movie_player" in body:
                plex.app_pairings.set_player(reader_id.removeprefix("remote:"), body.pop("movie_player") or None)
            readers.configure(reader_id, body)
        except ReaderError as e:
            return jsonify(ok=False, message=str(e)), 400
        return panel_readers()

    @panel_route("/readers/<reader_id>/remove", methods=["POST"])
    def panel_reader_remove(reader_id):
        """Forget a Home Assistant reader (it comes back if it reads a card again)."""
        readers.remove(reader_id)
        return panel_readers()

    @panel_route("/devices/<device_id>/rename", methods=["POST"])
    def panel_rename(device_id):
        name = plex.app_pairings.rename(device_id, panel_body().get("name", ""))
        if name is None:
            return jsonify(ok=False, message="Give it a name"), 400
        return jsonify(ok=True, name=name)

    @panel_route("/devices/<device_id>/player", methods=["POST"])
    def panel_device_player(device_id):
        """Where a Movie Remote's (or a phone's) taps and buttons go: a player ID, or null for the default."""
        player = panel_body().get("player")
        if player is not None and not (isinstance(player, str) and 0 < len(player) <= 200):
            return jsonify(ok=False, message="Choose a player"), 400
        if not any(d["id"] == device_id for d in plex.app_pairings.devices()):
            return jsonify(ok=False, message="That device isn't paired"), 404
        plex.app_pairings.set_player(device_id, player)
        return jsonify(ok=True)

    @panel_route("/devices/<device_id>/remove", methods=["POST"])
    def panel_remove(device_id):
        plex.app_pairings.remove(device_id)
        return jsonify(ok=True)

    @panel_route("/plex/login", methods=["POST"])
    def panel_plex_login():
        """Start signing in to Plex from an app: the plex.tv page to open, and the PIN to check
        (with /plex/check) until it's signed in."""
        try:
            pin_id, auth_url = plexauth.start_pin(config.auth_file)
        except requests.RequestException as e:
            return jsonify(ok=False, message=f"Couldn't reach plex.tv: {e}"), 502
        pins.add(pin_id)
        return jsonify(ok=True, pin_id=pin_id, auth_url=auth_url)

    @panel_route("/plex/logout", methods=["POST"])
    def panel_plex_logout():
        plexauth.forget_token(config.auth_file)
        plex.set_token("")
        return jsonify(ok=True)

    # Opening Marquee on Apple TVs without Home Assistant

    @panel_route("/appletvs/scan", methods=["POST"])  # POST: it looks around the network for a few seconds
    def panel_appletv_scan():
        return jsonify(found=plex.apple_tvs.scan())

    @panel_route("/appletvs/pair", methods=["POST"])
    def panel_appletv_pair():
        body = panel_body()
        return jsonify(ok=True, pair_id=plex.apple_tvs.start_pairing(body.get("identifier", ""), body.get("player", "")))

    def connect_marquee(player: str) -> str:
        """Open Marquee on ``player``'s Apple TV, expecting it to pair with this server as
        ``player``. Returns how that went."""
        server = config.public_base_url.split("://", 1)[-1]
        if link := plex.apple_tvs.link_for(player):
            plex.app_pairings.expect(link["address"], player)
        try:
            plex.apple_tvs.open_marquee(player)
            if link := plex.apple_tvs.link_for(player):
                plex.app_pairings.expect(link["address"], player)  # its address, if it moved
        except AppleTVError as e:
            return (f"Couldn't open Marquee on it ({e}). In Marquee, connect to {server} "
                    f"and approve its code here instead.")
        return f"Marquee on that Apple TV is connecting to this server as {player}."

    @panel_route("/appletvs/pair/<pair_id>", methods=["POST"])
    def panel_appletv_pair_finish(pair_id):
        player = plex.apple_tvs.finish_pairing(pair_id, str(panel_body().get("pin", "")))
        return jsonify(ok=True, player=player, message=connect_marquee(player))

    @panel_route("/appletvs/connect", methods=["POST"])
    def panel_appletv_connect():
        player = panel_body().get("player", "")
        if not plex.apple_tvs.link_for(player):
            return jsonify(ok=False, message=f"No Apple TV is paired for {player}"), 404
        return jsonify(ok=True, message=connect_marquee(player))

    @panel_route("/appletvs/open", methods=["POST"])
    def panel_appletv_open():
        plex.apple_tvs.open_marquee(panel_body().get("player", ""))
        return jsonify(ok=True)

    @panel_route("/appletvs/remove", methods=["POST"])
    def panel_appletv_remove():
        plex.apple_tvs.remove(panel_body().get("player", ""))
        return jsonify(ok=True)

    return app


def main():
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Serve the NFC tag endpoints")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    config = Config.from_env()
    from .discovery import advertise

    # SERVER_NAME tells servers apart in the apps' lists, e.g. Home Assistant's and a Mac's
    announcement = advertise(config.public_base_url, os.environ.get("SERVER_NAME") or "Movie Book")  # noqa: F841
    plex = Plex(config)
    # Read every source's movies now, so the first tap doesn't wait for it
    threading.Thread(target=lambda: plex.library.index() if plex.library.plex_in_use or plex.library.sources
                     else None, daemon=True).start()
    create_app(config, plex).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
