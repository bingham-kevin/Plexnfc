"""Build the printable poster book.

    python -m plexnfc.book                       # every movie, one per page
    python -m plexnfc.book --collection "Book"   # only a Plex collection
    python -m plexnfc.book --layout grid         # four posters per page

Writes into ./out:
  book.html  open in a browser and Print -> Save as PDF (or print directly)
  tags.csv   title, year, URL to write onto each tag (handy for NFC Tools)

Every page has a circle showing where to stick the NFC tag, in the same spot on
every page so the phone always finds it. The QR code is a fallback for phones
without NFC; leave it out with --no-qr.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import os
from html import escape

import requests

from .config import Config
from .plex import Plex, movie_key

CSS = """
@page { size: %(page)s; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Georgia, 'Times New Roman', serif; }
.sheet { width: 100%%; height: 100vh; page-break-after: always; display: grid; padding: 0.4in;
         grid-template-columns: repeat(%(cols)d, 1fr); grid-template-rows: repeat(%(rows)d, 1fr); gap: 0.3in; }
.card { position: relative; display: flex; flex-direction: column; align-items: center; min-height: 0; }
.card img.poster { flex: 1; min-height: 0; max-width: 100%%; object-fit: contain; border-radius: 4px;
                   box-shadow: 0 2px 10px rgba(0,0,0,.35); }
.caption { width: 100%%; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 10px; }
.title { font-size: %(title_size)s; font-weight: bold; text-align: left; }
.year { font-size: 0.8em; color: #666; font-weight: normal; }
.tap { width: %(tap)s; height: %(tap)s; flex: none; border: 2px dashed #999; border-radius: 50%%;
       display: flex; align-items: center; justify-content: center; font: 10px sans-serif; color: #999; }
.marks { display: flex; gap: 10px; align-items: center; }
.qr { width: %(tap)s; height: %(tap)s; flex: none; }
.cover { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh;
         page-break-after: always; text-align: center; }
.cover h1 { font-size: 48px; margin: 0 0 12px; } .cover p { color: #666; }
.controls { display: flex; gap: 0.6in; margin-top: 0.6in; }
.controls div { display: flex; flex-direction: column; align-items: center; gap: 8px; font: 14px sans-serif; }
.controls .tap { width: 1.1in; height: 1.1in; }
"""

LAYOUTS = {
    "full": dict(cols=1, rows=1, title_size="28px", tap="1.1in"),
    "grid": dict(cols=2, rows=2, title_size="15px", tap="0.85in"),
}


def qr_svg(url: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=1)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):].replace("<svg", '<svg class="qr"', 1)


def data_uri(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return f"data:{mime};base64,{base64.b64encode(r.content).decode()}"


def build(plex: Plex, base_url: str, layout: str, title: str, collection: str | None,
          qr: bool, page_size: str, out_dir: str) -> tuple[str, int]:
    actions = [("pause", "Pause"), ("play", "Resume"), ("stop", "Stop")]
    if plex.config.ha_off_script:
        actions.append(("off", "TV off"))
    spec = LAYOUTS[layout]
    per_sheet = spec["cols"] * spec["rows"]
    movies = list(plex.movies(collection))
    os.makedirs(out_dir, exist_ok=True)

    cards, rows = [], []
    for i, movie in enumerate(movies, 1):
        url = f"{base_url}/m/{movie_key(movie)}"
        rows.append((movie.title, movie.year or "", url))
        print(f"[{i}/{len(movies)}] {movie.title}")
        poster = f'<img class="poster" src="{data_uri(plex.poster_url(movie, 1200, 1800))}" alt="">' if movie.thumb else ""
        year = f' <span class="year">({movie.year})</span>' if movie.year else ""
        cards.append(
            f'<div class="card">{poster}<div class="caption">'
            f'<div class="title">{escape(movie.title)}{year}</div>'
            f'<div class="marks">{qr_svg(url) if qr else ""}<div class="tap">NFC</div></div></div></div>'
        )

    sheets = ["".join(cards[i:i + per_sheet]) for i in range(0, len(cards), per_sheet)]
    controls = "".join(
        f'<div><div class="tap">NFC</div>{label}<small>{base_url}/{action}</small></div>'
        for action, label in actions
    )
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<style>{CSS % dict(spec, page=page_size)}</style></head><body>"
        f'<section class="cover"><h1>{escape(title)}</h1><p>Tap a poster with your phone to watch it.</p></section>'
        + "".join(f'<section class="sheet">{s}</section>' for s in sheets)
        + f'<section class="cover"><h1>Remote</h1><p>Control tags for the back cover</p>'
          f'<div class="controls">{controls}</div></section>'
        "</body></html>"
    )
    book_path = os.path.join(out_dir, "book.html")
    with open(book_path, "w") as f:
        f.write(html)
    with open(os.path.join(out_dir, "tags.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["title", "year", "url"])
        w.writerows(rows)
        for action, _ in actions:
            w.writerow([f"[{action}]", "", f"{base_url}/{action}"])
    return book_path, len(movies)


def main():
    parser = argparse.ArgumentParser(description="Build a printable NFC movie poster book from Plex")
    parser.add_argument("--collection", help="only include movies in this Plex collection")
    parser.add_argument("--layout", choices=LAYOUTS, default="full", help="full = 1 per page, grid = 4 per page")
    parser.add_argument("--title", default="Movie Night")
    parser.add_argument("--page-size", default="letter", help="CSS page size, e.g. letter, A4, 'A5'")
    parser.add_argument("--no-qr", action="store_true", help="leave out the QR code fallback")
    parser.add_argument("--out", default="out")
    args = parser.parse_args()

    config = Config.from_env()
    path, count = build(Plex(config), config.public_base_url, args.layout, args.title,
                        args.collection, not args.no_qr, args.page_size, args.out)
    print(f"\nWrote {count} movies to {path} and tags.csv. Open book.html in a browser and print it.")


if __name__ == "__main__":
    main()
