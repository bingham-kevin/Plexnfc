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
  /appletv/...         pair Apple TVs so plexnfc opens Marquee on them itself (needs pyatv)
  /phone/...           the Marquee Remote phone app: which player this phone's taps go to
  /poster/<key>        poster image (proxied so your Plex token stays private)
                       ?w=200&h=300 returns a resized baseline JPEG (for the ESP32 screen)

For the ESP32 remote (see esphome/):
  /m/<key>?async=1     start playback in the background and return immediately
  /now                 JSON: what's playing, poster path, position, duration
  /toggle              play/pause
  /seek?by=30          jump forwards (or backwards with a negative number) in seconds
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from html import escape
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, abort, jsonify, request

from . import plexauth
from .appletv import AppleTVError
from .config import Config
from .plex import Plex, PlexNFCError, movie_key

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


def create_app(config: Config | None = None, plex: Plex | None = None) -> Flask:
    config = config or Config.from_env()
    plex = plex or Plex(config)
    app = Flask(__name__)

    def resume_param():
        value = request.args.get("resume")
        return None if value is None else value.lower() in ("1", "true", "yes")

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
        try:
            movie, client = plex.play(key, tap_client(), resume_param())
        except PlexNFCError as e:
            return page("Can't play", f'<h1 class="err">Can\'t play that</h1><p>{escape(str(e))}</p>', 404)
        return page(
            f"Playing {movie.title}",
            f'<img src="/poster/{escape(key)}" alt=""><h1>{escape(movie.title)}</h1>'
            f"<p>Now playing on {escape(client.title)}</p>",
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

    def paired_device() -> str | None:
        """The paired Apple TV making this request, from its ``Authorization: Bearer`` secret."""
        auth = request.headers.get("Authorization", "")
        return plex.app_pairings.device(auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None)

    # Home Assistant's ingress, and this machine itself. On the host network (needed for
    # Bonjour) ingress requests come from the host's own address, the tag address's host.
    # (A host name isn't looked up: the tag address is a fixed IP in practice.)
    panel_addresses = {"172.30.32.2", "172.30.32.1", "127.0.0.1", "::1"}
    panel_addresses.add(urlsplit(config.public_base_url).hostname or "")

    def from_panel() -> bool:
        """Pairings are approved only from the panel: through Home Assistant's ingress,
        or on the machine plexnfc runs on, never from another device on the network."""
        return request.remote_addr in panel_addresses

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
        return jsonify(plex.app_pairings.start(body.get("name", ""), body.get("kind", "tv")))

    @app.get("/tv/pair/<pair_id>")
    def tv_pair_status(pair_id):
        result = plex.app_pairings.poll(pair_id)
        return jsonify(result) if result else (jsonify(status="expired"), 404)

    @app.post("/tv/pair/<pair_id>/approve")
    def tv_pair_approve(pair_id):
        if not from_panel():
            abort(403)
        plex.app_pairings.approve(pair_id, request.form.get("name"))
        return page("Paired", '<h1>Apple TV paired</h1><p><a href="../../../">Back</a></p>')

    @app.post("/tv/devices/<device_id>/rename")
    def tv_device_rename(device_id):
        if not from_panel():
            abort(403)
        name = plex.app_pairings.rename(device_id, request.form.get("name", ""))
        if name is None:
            abort(400)
        return page("Renamed", f'<h1>Renamed to {escape(name)}</h1><p>If <b>Marquee player</b> or a '
                               f'get-ready script names it, change those to <code>app:{escape(name)}</code> '
                               f'too.</p><p><a href="../../../">Back</a></p>')

    @app.post("/tv/devices/<device_id>/remove")
    def tv_device_remove(device_id):
        if not from_panel():
            abort(403)
        plex.app_pairings.remove(device_id)
        return page("Removed", '<h1>Apple TV removed</h1><p><a href="../../../">Back</a></p>')

    # ---- opening Marquee on Apple TVs without Home Assistant ---------------------------

    def appletv_error(e: AppleTVError, back: str) -> Response:
        return page("Error", f'<h1 class="err">{escape(str(e))}</h1><p><a href="{back}">Back</a></p>', 400)

    @app.get("/appletv/scan")
    def appletv_scan():
        if not from_panel():
            abort(403)
        try:
            found = plex.apple_tvs.scan()
        except AppleTVError as e:
            return appletv_error(e, "../")
        players = "".join(f'<option value="{escape(d["name"])}">' for d in plex.app_pairings.devices()
                          if d["kind"] == "tv")
        rows = "".join(
            f'<tr><td>{escape(tv["name"])}<br><small>{escape(tv["address"])}</small></td>'
            f'<td><form action="pair" method="post"><input type="hidden" name="identifier" '
            f'value="{escape(tv["identifier"])}"><input name="player" list="players" required maxlength="40" '
            f'value="{escape(tv["linked"] or tv["name"])}" aria-label="Marquee player"> '
            f'<button>{"Pair again" if tv["linked"] else "Pair"}</button></form></td></tr>'
            for tv in found)
        table = f"<table>{rows}</table>" if rows else "<p>No Apple TVs found. Are they on and on this network?</p>"
        return page("Apple TVs", f"<h1>Apple TVs on this network</h1><p>Pair one with the Marquee player it "
                                 f"runs (the name you gave Marquee on it). The TV will show a PIN.</p>"
                                 f'<datalist id="players">{players}</datalist>{table}'
                                 f'<p><a href="../">Back</a></p>')

    @app.post("/appletv/pair")
    def appletv_pair():
        if not from_panel():
            abort(403)
        try:
            pair_id = plex.apple_tvs.start_pairing(request.form.get("identifier", ""), request.form.get("player", ""))
        except AppleTVError as e:
            return appletv_error(e, "scan")
        return page("Enter the PIN", f'<h1>Enter the PIN on the TV</h1>'
                                     f'<form action="pair/{escape(pair_id)}" method="post">'
                                     f'<input name="pin" inputmode="numeric" autocomplete="one-time-code" '
                                     f'maxlength="4" required autofocus aria-label="PIN"> <button>Pair</button></form>')

    @app.post("/appletv/pair/<pair_id>")
    def appletv_pair_finish(pair_id):
        if not from_panel():
            abort(403)
        try:
            player = plex.apple_tvs.finish_pairing(pair_id, request.form.get("pin", ""))
        except AppleTVError as e:
            return appletv_error(e, "../scan")
        return page("Paired", f"<h1>Paired</h1><p>Taps for <code>app:{escape(player)}</code> now switch that "
                              f"Apple TV on and open Marquee.</p><p><a href=\"../../\">Back</a></p>")

    @app.post("/appletv/open")
    def appletv_open():
        if not from_panel():
            abort(403)
        player = request.form.get("player", "")
        try:
            plex.apple_tvs.open_marquee(player)
        except AppleTVError as e:
            return appletv_error(e, "../")
        return page("Opened", f"<h1>Opened Marquee on {escape(player)}</h1><p><a href=\"../\">Back</a></p>")

    @app.post("/appletv/remove")
    def appletv_remove():
        if not from_panel():
            abort(403)
        plex.apple_tvs.remove(request.form.get("player", ""))
        return page("Removed", '<h1>Apple TV unpaired</h1><p><a href="../">Back</a></p>')

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

    @app_route("/tv/play", methods=["POST"])
    def tv_play(device):
        body = request.get_json(silent=True) or {}
        resume = body.get("resume")
        return jsonify(plex.app_play_details(device, body.get("key", ""), resume=resume))

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

    @app.post("/plex/logout")
    def plex_logout():
        plexauth.forget_token(config.auth_file)
        plex.set_token("")
        return page("Signed out", '<h1>Signed out of Plex</h1><p><a href="../">Back</a></p>')

    def device_status(device: dict) -> str:
        if device["kind"] != "phone":
            return "Apple TV, paired"
        return f"phone, taps go to {device['player'] or 'the default player'}"

    def marquee_section() -> str:
        """Pairing requests, and the paired Apple TVs and phones, for the Marquee apps."""
        server = config.public_base_url.split("://", 1)[-1]
        rows = "".join(
            f'<tr><td>{escape(p["name"])} ({"phone" if p["kind"] == "phone" else "Apple TV"}) wants to pair</td>'
            f'<td><b style="font-size:1.4em">{p["code"]}</b></td>'
            f'<td><form action="tv/pair/{escape(p["pair_id"])}/approve" method="post">'
            f'<input name="name" value="{escape(p["name"])}" maxlength="40" aria-label="Name, e.g. the room"> '
            f'<button>Approve</button></form></td></tr>'
            for p in plex.app_pairings.pending())
        rows += "".join(
            f'<tr><td><form action="tv/devices/{escape(d["id"])}/rename" method="post">'
            f'<input name="name" value="{escape(d["name"])}" maxlength="40" required aria-label="Name"> '
            f'<button>Rename</button></form></td><td>{escape(device_status(d))}</td>'
            f'<td><form action="tv/devices/{escape(d["id"])}/remove" method="post">'
            f'<button>Remove</button></form></td></tr>'
            for d in plex.app_pairings.devices())
        table = f"<table>{rows}</table>" if rows else "<p>Nothing paired yet.</p>"
        return (f"<h1>Marquee Apple TVs and phones</h1><p>In Marquee (Apple TV) or Marquee Remote (iPhone, iPad), "
                f"connect to <code>{escape(server)}</code>, "
                f"then approve the code it shows here. Name each Apple TV after its room: tags pick one with "
                f"<code>?client=app:NAME</code>. Reload this page to see new requests.</p>{table}")

    def apple_tv_section() -> str:
        """Apple TVs plexnfc opens Marquee on itself, instead of a get-ready script."""
        if not plex.apple_tvs.available:
            return ""
        rows = "".join(
            f'<tr><td>{escape(player)}</td><td>{escape(link["name"])}</td>'
            f'<td><form action="appletv/open" method="post" style="display:inline">'
            f'<input type="hidden" name="player" value="{escape(player)}"><button>Open Marquee</button></form> '
            f'<form action="appletv/remove" method="post" style="display:inline">'
            f'<input type="hidden" name="player" value="{escape(player)}"><button>Unpair</button></form></td></tr>'
            for player, link in plex.apple_tvs.linked().items())
        table = f"<table>{rows}</table>" if rows else ""
        return (f"<h1>Open Marquee automatically</h1><p>Pair an Apple TV here and a tap switches it on and opens "
                f"Marquee, with no Home Assistant script.</p>{table}"
                f'<p><a class="btn" href="appletv/scan">Find Apple TVs</a></p>')

    @app.get("/")
    def index():
        # Links are relative so this page works inside the Home Assistant sidebar (ingress).
        # The book is the exception: it opens in a new tab, and ingress turns away any
        # request that isn't from inside the panel, so it goes to the tag address instead.
        warn = ""
        if "localhost" in config.public_base_url or "127.0.0.1" in config.public_base_url:
            warn = ('<p class="err">public_base_url is set to localhost. Phones can\'t reach that: '
                    "set it to this machine's IP address, e.g. http://192.168.1.20:8080</p>")
        # The same order play() picks a player in
        player_label = (f"the Marquee app ({config.app_player})" if config.app_player
                        else f"Jellyfin player {config.jellyfin_player}" if config.jellyfin_player
                        else config.ha_player or config.client or "(no player set)")
        apple_tvs = marquee_section()
        if not config.plex_token:
            signin = ('<p class="err">plexnfc isn\'t signed in to Plex yet.</p>'
                      '<p><a class="btn" href="plex/login">Sign in with Plex</a></p>')
        elif plexauth.saved_token(config.auth_file) == config.plex_token:
            signin = ('<form action="plex/logout" method="post"><p>Signed in to Plex '
                      '<button>Sign out</button></p></form>')
        else:
            signin = ""  # token set by hand in the settings
        return page("plexnfc", f"""
          <h1>plexnfc</h1>
          {signin}
          <p>Tags point at <code>{escape(config.public_base_url)}</code>, playing on
             <b>{escape(player_label)}</b></p>{warn}
          <h1>Make the book</h1>
          <form action="{escape(config.public_base_url)}/book" target="_blank"
                style="display:inline-grid;gap:8px;text-align:left">
            <label>Title <input name="title" value="Movie Night"></label>
            <label>Plex collection (blank = whole library) <input name="collection"></label>
            <label>Layout <select name="layout"><option value="full">1 per page</option>
              <option value="grid">4 per page</option></select></label>
            <label>Paper <select name="page_size"><option>letter</option><option>A4</option></select></label>
            <label><input type="checkbox" name="qr" value="1" checked> QR code fallback</label>
            <button>Open printable book</button>
          </form>
          <p>Wait for all the posters to load, then print with margins <i>None</i> and background graphics on.</p>
          <h1>Write the tags</h1>
          <p><a style="color:#8ab4f8" href="tags">Tag URLs</a> &middot;
             <a style="color:#8ab4f8" href="tags.csv">Download tags.csv</a></p>
          {apple_tvs}
          {apple_tv_section()}""")

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
