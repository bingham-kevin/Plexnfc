"""Web server the NFC tags point at.

Tapping a tag opens e.g. ``http://192.168.1.20:8080/m/tmdb-603`` on the phone,
which tells Plex to start that movie on the configured player.

Routes
  /m/<key>             play a movie   (?client=NAME to pick a player, ?resume=0 to start over)
  /pause /play /stop   control tags for the back cover of the book
  /tags                list every movie with the URL to write onto its tag
  /poster/<key>        poster image (proxied so your Plex token stays private)
"""
from __future__ import annotations

import logging
from html import escape

import requests
from flask import Flask, Response, abort, request

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
</style></head><body>{body}</body></html>"""


def page(title: str, body: str, status: int = 200) -> Response:
    return Response(PAGE.format(title=escape(title), body=body), status=status, mimetype="text/html")


def create_app(config: Config | None = None, plex: Plex | None = None) -> Flask:
    config = config or Config.from_env()
    plex = plex or Plex(config)
    app = Flask(__name__)

    def resume_param():
        value = request.args.get("resume")
        return None if value is None else value.lower() in ("1", "true", "yes")

    @app.get("/m/<key>")
    def play(key):
        try:
            movie, client = plex.play(key, request.args.get("client"), resume_param())
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
            client = plex.control(action, request.args.get("client"))
        except PlexNFCError as e:
            return page("Error", f'<h1 class="err">{escape(str(e))}</h1>', 404)
        label = {"pause": "Paused", "play": "Resumed", "stop": "Stopped"}[action]
        return page(label, f"<h1>{label}</h1><p>{escape(client.title)}</p>")

    @app.get("/poster/<key>")
    def poster(key):
        try:
            movie = plex.find_movie(key)
        except PlexNFCError:
            abort(404)
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
            for a in ("pause", "play", "stop")
        )
        # Web NFC only works in Chrome on Android, and only over HTTPS.
        script = """<script>
          if (!('NDEFReader' in window)) document.querySelectorAll('.nfc').forEach(b => b.remove());
          async function writeTag(url) {
            try { await new NDEFReader().write({records: [{recordType: 'url', data: url}]}); alert('Written: ' + url); }
            catch (e) { alert('Write failed: ' + e); }
          }</script>"""
        return page("NFC tags", f"<h1>NFC tag URLs</h1><table>{''.join(rows)}{controls}</table>{script}")

    @app.get("/")
    def index():
        return page("plexnfc", '<h1>plexnfc is running</h1><p><a style="color:#8ab4f8" href="/tags">Tag list</a></p>')

    return app


def main():
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Serve the NFC tag endpoints")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    create_app().run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
