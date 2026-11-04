"""Web server the NFC tags point at.

Tapping a tag opens e.g. ``http://192.168.1.20:8080/m/tmdb-603`` on the phone,
which tells Plex to start that movie on the configured player.

Routes
  /m/<key>             play a movie   (?client=NAME to pick a player, ?resume=0 to start over)
  /pause /play /stop   control tags for the back cover of the book
  /off                 run the Home Assistant HA_OFF_SCRIPT (e.g. TV off)
  /tags                list every movie with the URL to write onto its tag (/tags.csv to download)
  /book                the printable book (?layout=grid&collection=NAME&title=...)
  /plex/login          Sign in with Plex (saves the token, so nobody has to find it by hand)
  /plex/server         JSON: the Plex server in use (found from the sign-in when PLEX_URL isn't set)
  /tv/...               the Marquee Apple TV app: pairing, inbox, status, library, search and play
                       (/tv/expected: Marquee pairs by itself on an Apple TV paired below)
  /panel/api/...       the control panel (/): overview, movies, pairing, Apple TVs (needs pyatv)
  /phone/...           the Marquee Remote phone app: which player this phone's taps go to
  /ha/...              the Marquee integration for Home Assistant: players, what they're playing, controls
  /poster/<key>        poster image (proxied so your Plex token stays private)
                       ?w=200&h=300 returns a resized baseline JPEG (for the ESP32 screen)

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
import logging
import os
import socket
import threading
import time
from html import escape
from urllib.parse import parse_qs, urlsplit

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import plexauth
from .appletv import AppleTVError
from .config import Config
from .plex import Plex, PlexNFCError, app_movie, movie_key

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

    # Home Assistant's ingress, and this machine itself. On the host network (needed for
    # Bonjour) ingress requests come from the host's own address, the tag address's host.
    panel_addresses = {"172.30.32.2", "172.30.32.1", "127.0.0.1", "::1"}
    panel_addresses.add(urlsplit(config.public_base_url).hostname or "")

    def from_panel() -> bool:
        """Pairings are approved only from the panel: through Home Assistant's ingress,
        or on the machine plexnfc runs on, never from another device on the network."""
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
            remaining = deadline - time.monotonic()
            if tag != request.args.get("tag") or remaining <= 0:
                return jsonify(players=players, tag=tag)
            plex.app_inbox.wait_for_change(min(remaining, 2))  # also notices a Marquee that went quiet

    @ha_route("/ha/control", methods=["POST"])
    def ha_control():
        body = request.get_json(silent=True) or {}
        if body.get("action") not in ("pause", "play", "stop") or not isinstance(body.get("player"), str):
            return jsonify(ok=False, message="Send a player and an action: pause, play or stop"), 400
        plex.control(body["action"], body["player"])
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
                    return fn(remote=remote, **kwargs)
                except PlexNFCError as e:
                    return jsonify(ok=False, message=str(e)), 404
            wrapped.__name__ = fn.__name__
            return app.route(rule, **options)(wrapped)
        return decorator

    def player_label(player: str | None) -> str:
        player = player or plex.default_player() or ""
        return player.split(":", 1)[1] if player.startswith(("app:", "jellyfin:")) else player

    @remote_route("/remote/tap", methods=["POST"])
    def remote_tap(remote):
        """A tag the box read: its URL, from whichever address it was written with (only the
        /m/KEY path and the tag's own ?client= and ?resume= count). Returns at once."""
        url = urlsplit(str((request.get_json(silent=True) or {}).get("url", "")))
        parts = url.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "m" or not parts[1]:
            return jsonify(ok=False, message="Not a movie book tag"), 400
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
            return jsonify(state="error", message=f"Can't reach Plex: {e}")
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
                                   position=0, duration=0, player=client or config.app_player or config.jellyfin_player or config.client)
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
            movie = plex.find_movie(key)
        except PlexNFCError:
            abort(404)
        w, h = request.args.get("w", type=int), request.args.get("h", type=int)
        if w and h and 16 <= w <= 2000 and 16 <= h <= 3000:
            r = requests.get(plex.poster_url(movie, w, h), timeout=15)
            return Response(resize_jpeg(r.content, w, h), mimetype="image/jpeg",
                            headers={"Cache-Control": "max-age=86400"})
        r = requests.get(plex.poster_url(movie), timeout=15)
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/tags")
    def tags():
        rows = []
        for movie in plex.movies(request.args.get("collection")):
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
            movies = list(plex.movies(request.args.get("collection") or None))
        except Exception as e:
            return page("Book", f'<h1 class="err">Couldn\'t load movies</h1><p>{escape(str(e))}</p>', 500)
        html = render_html(
            plex, config.public_base_url, movies, layout,
            title=request.args.get("title") or "Movie Night",
            qr=request.args.get("qr", "1") == "1",
            page_size=request.args.get("page_size") or "letter",
            # Relative, so it also works behind Home Assistant's ingress proxy
            poster_src=lambda m: f"poster/{movie_key(m)}?w=1200&h=1800",
        )
        return Response(html, mimetype="text/html")

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
        # Relative URLs so this works behind Home Assistant's ingress proxy.
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
    # Anyone on the network can look; only the panel itself (Home Assistant's ingress, or
    # this machine) can change anything. Its addresses are relative, for ingress.

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
            devices.append({**d, "status": status if status and status.get("state", "idle") != "idle" else None})
        return jsonify(
            can_manage=from_panel(),
            tag_address=config.public_base_url,
            tag_address_is_local="localhost" in config.public_base_url or "127.0.0.1" in config.public_base_url,
            default_player=plex.default_player(),
            # The same order play() picks a player in
            default_player_label=(f"Marquee on {config.app_player}" if config.app_player
                                  else f"Jellyfin player {config.jellyfin_player}" if config.jellyfin_player
                                  else config.ha_player or config.client or ""),
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
        """Every player a movie can go to (asks Plex and Jellyfin, so it's slower)."""
        return jsonify(players=plex.players(), default=plex.default_player())

    @panel_route("/movies")
    def panel_movies():
        movies = [{"key": movie_key(m), "title": m.title, "year": m.year,
                   "url": f"{config.public_base_url}/m/{movie_key(m)}"}
                  for m in plex.movies(request.args.get("collection") or None)]
        controls = [{"action": a, "url": f"{config.public_base_url}/{a}"}
                    for a in ("pause", "play", "stop") + (("off",) if config.ha_off_script else ())]
        return jsonify(movies=movies, controls=controls)

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
    create_app(config).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
