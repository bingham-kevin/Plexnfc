"""Build the printable poster book.

    python -m plexnfc.book                       # every movie, one per page
    python -m plexnfc.book --collection "Book"   # only a Plex collection
    python -m plexnfc.book --layout grid         # four posters per page

Writes into ./out:
  book.html  open in a browser and Print -> Save as PDF (or print directly)
  tags.csv   title, year, URL to write onto each tag (handy for NFC Tools)

The book opens with a marquee-sign cover and a contents page, and ends with the
control tags as ticket stubs. Every poster has a gold circle showing where to
stick its NFC tag, in the same spot on every page so the phone always finds it.
The QR code is a fallback for phones without NFC; leave it out with --no-qr.
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

# The look follows the Marquee app: a vintage movie theater, in print. Colors are
# ink-friendly on white paper; print with "Background graphics" on.
FONTS = ("https://fonts.googleapis.com/css2?family=Limelight&family=Oswald:wght@500;600"
         "&family=Lora:ital,wght@0,400;1,400&display=swap")

CSS = """
@page { size: $page; margin: 0; }
* { box-sizing: border-box; }
:root { --ink: #1d1b1a; --red: #a3241c; --gold: #b0852f; --teal: #124a4c; --paper: #fbf7ee; --muted: #6d655c; }
html, body { margin: 0; background: #fff; color: var(--ink); }
body { font-family: Lora, Georgia, 'Times New Roman', serif; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
section { position: relative; height: 100vh; overflow: hidden; page-break-after: always; break-after: page; }
.display { font-family: Limelight, 'Didot', Georgia, serif; font-weight: normal; }
.caps { font-family: Oswald, 'Arial Narrow', Arial, sans-serif; text-transform: uppercase; letter-spacing: .14em; }

/* A thin double gold frame inside every page */
.frame { position: absolute; inset: 0.3in; border: 1.5px solid var(--gold); pointer-events: none; }
.frame::after { content: ""; position: absolute; inset: 4px; border: 0.75px solid var(--gold); }
.folio { position: absolute; bottom: 0.12in; left: 0; right: 0; text-align: center; font-size: 9px; color: var(--gold); }

/* Cover: a marquee sign with a border of bulbs */
.cover { background: var(--paper); display: flex; flex-direction: column; align-items: center; justify-content: center;
         text-align: center; padding: 1in; }
.sign { background: var(--red); color: #fff; border-radius: 22px; padding: 0.9in 0.7in 0.8in; position: relative;
        width: 100%; max-width: 6in; box-shadow: 0 0 0 6px var(--paper), 0 0 0 8px var(--gold); }
.sign::before { content: ""; position: absolute; inset: 11px; border: 7px dotted #ffe9a8; border-radius: 14px; }
.sign .caps { font-size: 13px; color: #ffe9a8; letter-spacing: .4em; }
.sign h1 { font-size: 64px; line-height: 1.02; margin: 14px 0 10px; overflow-wrap: anywhere; }
.sign .count { font-style: italic; font-size: 15px; color: #fbe3d6; }
.how { display: flex; gap: 0.45in; margin-top: 0.75in; max-width: 6.4in; }
.how div { flex: 1; font-size: 12.5px; line-height: 1.45; color: var(--muted); }
.how b { display: block; font-family: Limelight, Georgia, serif; font-weight: normal; font-size: 26px; color: var(--gold); margin-bottom: 4px; }

/* Contents */
.contents { padding: 0.75in 0.8in; }
.contents h2 { text-align: center; font-size: 40px; margin: 0 0 4px; color: var(--teal); }
.contents .sub { text-align: center; margin-bottom: 0.3in; color: var(--gold); font-size: 11px; }
.contents ol { columns: 2; column-gap: 0.45in; list-style: none; padding: 0; margin: 0; font-size: 12px; }
.contents li { display: flex; align-items: baseline; gap: 6px; padding: 2.5px 0; break-inside: avoid; }
.contents li .t { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.contents li .dots { flex: 1; border-bottom: 1px dotted #b9ad9b; transform: translateY(-3px); min-width: 12px; }
.contents li .n { font-family: Oswald, Arial, sans-serif; color: var(--red); }

/* Poster pages */
.sheet { display: grid; padding: 0.55in 0.55in 0.45in; gap: 0.3in;
         grid-template-columns: repeat($cols, 1fr); grid-template-rows: repeat($rows, minmax(0, 1fr)); }
.card { display: flex; flex-direction: column; min-height: 0; }
.poster-box { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; }
.poster { max-width: 100%; max-height: 100%; object-fit: contain; padding: 5px; background: #fff;
          border: 1px solid var(--gold); box-shadow: 0 3px 14px rgba(0,0,0,.28); }
.no-poster { width: 66%; aspect-ratio: 2 / 3; border: 1px solid var(--gold); background: var(--paper);
             display: flex; align-items: center; justify-content: center; padding: 12px; text-align: center;
             font-family: Limelight, Georgia, serif; font-size: 22px; color: var(--teal); }
.band { height: $band; flex: none; display: flex; align-items: center; gap: 14px; margin-top: 12px;
        border-top: 2px solid var(--red); padding-top: 10px; }
.info { flex: 1; min-width: 0; align-self: stretch; overflow: hidden; }
.title { font-family: Oswald, 'Arial Narrow', Arial, sans-serif; font-weight: 600; text-transform: uppercase;
         letter-spacing: .04em; font-size: $title_size; line-height: 1.1; margin: 0;
         display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.meta { font-size: $meta_size; color: var(--gold); margin-top: 4px; letter-spacing: .18em; }
.summary { font-size: 11.5px; line-height: 1.45; color: var(--muted); margin-top: 6px; font-style: italic;
           display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.marks { display: flex; align-items: center; gap: 12px; flex: none; }
.qr-box { display: flex; flex-direction: column; align-items: center; gap: 3px; font-size: 7px; color: var(--muted); }
.qr { width: $qr; height: $qr; display: block; }

/* Where the NFC sticker goes: the same spot on every page */
.tap { width: $tap; height: $tap; flex: none; border-radius: 50%; border: 2px dashed var(--gold);
       display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
       color: var(--red); background: radial-gradient(circle, #fff 58%, var(--paper) 60%); }
.tap svg { width: 38%; height: auto; }
.tap span { font-family: Oswald, Arial, sans-serif; font-size: $tap_label; letter-spacing: .2em; }

/* Back cover: the control tags as ticket stubs */
.remote { background: var(--paper); display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 1in; }
.remote h2 { font-size: 44px; margin: 0; color: var(--teal); }
.remote .sub { color: var(--muted); font-style: italic; margin: 6px 0 0.6in; font-size: 13px; }
.tickets { display: grid; grid-template-columns: repeat(2, 2.6in); gap: 0.35in; }
.ticket { position: relative; background: #fff; border: 1.5px solid var(--gold); border-radius: 10px; padding: 16px 18px;
          display: flex; align-items: center; gap: 14px; }
.ticket::before, .ticket::after { content: ""; position: absolute; top: 50%; width: 16px; height: 16px; margin-top: -8px;
          border-radius: 50%; background: var(--paper); border: 1.5px solid var(--gold); }
.ticket::before { left: -9px; clip-path: inset(0 0 0 50%); } .ticket::after { right: -9px; clip-path: inset(0 50% 0 0); }
.ticket .tap { width: 0.95in; height: 0.95in; }
.ticket .label { font-family: Oswald, Arial, sans-serif; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; font-size: 18px; }
.ticket small { display: block; font-size: 8px; color: var(--muted); margin-top: 4px; overflow-wrap: anywhere; }
"""

LAYOUTS = {
    "full": dict(cols=1, rows=1, title_size="30px", meta_size="12px", tap="1.15in", tap_label="10px",
                 qr="0.8in", band="1.45in", summary=True),
    "grid": dict(cols=2, rows=2, title_size="15px", meta_size="9px", tap="0.85in", tap_label="7px",
                 qr="0.6in", band="0.95in", summary=False),
}

CONTENTS_PER_PAGE = 84  # two columns of titles

# Radio waves: "hold your phone here"
NFC_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
            '<circle cx="6" cy="12" r="1.6" fill="currentColor" stroke="none"/><path d="M10 8.5a5 5 0 0 1 0 7"/>'
            '<path d="M13.5 5.5a9.5 9.5 0 0 1 0 13"/><path d="M17 2.5a14 14 0 0 1 0 19"/></svg>')


def tap_target(label: str = "TAP") -> str:
    return f'<div class="tap">{NFC_ICON}<span>{label}</span></div>'


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


def control_actions(plex: Plex) -> list[tuple[str, str]]:
    actions = [("pause", "Pause"), ("play", "Resume"), ("stop", "Stop")]
    if plex.config.ha_off_script:
        actions.append(("off", "TV off"))
    return actions


def tag_rows(plex: Plex, base_url: str, movies) -> list[tuple]:
    """(title, year, url) for every tag: one per movie, then the control tags."""
    rows = [(m.title, m.year or "", f"{base_url}/m/{movie_key(m)}") for m in movies]
    rows += [(f"[{action}]", "", f"{base_url}/{action}") for action, _ in control_actions(plex)]
    return rows


def write_csv(rows, f) -> None:
    w = csv.writer(f)
    w.writerow(["title", "year", "url"])
    w.writerows(rows)


def meta_line(movie) -> str:
    """e.g. 2008 · 1 h 36 m · PG-13"""
    parts = [str(movie.year)] if getattr(movie, "year", None) else []
    minutes = round((getattr(movie, "duration", 0) or 0) / 60000)
    if minutes:
        parts.append(f"{minutes // 60} h {minutes % 60:02d} m" if minutes >= 60 else f"{minutes} m")
    if rating := getattr(movie, "contentRating", None):
        parts.append(str(rating))
    return " · ".join(parts)


def render_html(plex: Plex, base_url: str, movies, layout: str = "full", title: str = "Movie Night",
                qr: bool = True, page_size: str = "letter", poster_src=None) -> str:
    """The printable book as one HTML page.

    ``poster_src(movie)`` gives each <img> src: a data: URI for a standalone
    file, or a URL when the page is served by plexnfc (Home Assistant app).
    """
    from string import Template

    spec = LAYOUTS[layout]
    per_sheet = spec["cols"] * spec["rows"]
    movies = list(movies)
    contents_pages = max(1, -(-len(movies) // CONTENTS_PER_PAGE))
    first_sheet = 2 + contents_pages  # page 1 is the cover

    def page_of(i: int) -> int:
        return first_sheet + i // per_sheet

    cards = []
    for movie in movies:
        url = f"{base_url}/m/{movie_key(movie)}"
        src = poster_src(movie) if poster_src and getattr(movie, "thumb", None) else ""
        poster = (f'<img class="poster" src="{escape(src)}" alt="">' if src
                  else f'<div class="no-poster">{escape(movie.title)}</div>')
        meta = meta_line(movie)
        summary = (getattr(movie, "summary", "") or "") if spec["summary"] else ""
        marks = (f'<div class="qr-box">{qr_svg(url)}NO NFC? SCAN</div>' if qr else "") + tap_target()
        cards.append(
            f'<div class="card"><div class="poster-box">{poster}</div><div class="band"><div class="info">'
            f'<h3 class="title">{escape(movie.title)}</h3>'
            + (f'<div class="meta caps">{escape(meta)}</div>' if meta else "")
            + (f'<div class="summary">{escape(summary)}</div>' if summary else "")
            + f'</div><div class="marks">{marks}</div></div></div>'
        )

    def folio(n: int) -> str:
        return f'<div class="folio caps">&#9670; {n} &#9670;</div>'

    count = f"{len(movies)} feature{'s' if len(movies) != 1 else ''}"
    cover = (
        f'<section class="cover"><div class="frame"></div><div class="sign">'
        f'<div class="caps">Now showing</div><h1 class="display">{escape(title)}</h1>'
        f'<div class="count">{count}</div></div>'
        '<div class="how"><div><b>1</b>Unlock your phone and find a film you want to watch.</div>'
        '<div><b>2</b>Hold the top of the phone on the gold circle beside its poster.</div>'
        '<div><b>3</b>Open the link that pops up. The movie starts on the TV.</div></div></section>'
    )
    ordered = sorted(enumerate(movies), key=lambda im: im[1].title.casefold())
    contents = "".join(
        f'<section class="contents"><div class="frame"></div>'
        f'<h2 class="display">Program</h2><div class="sub caps">Feature presentations</div><ol>'
        + "".join(f'<li><span class="t">{escape(m.title)}</span><span class="dots"></span>'
                  f'<span class="n">{page_of(i)}</span></li>'
                  for i, m in ordered[p * CONTENTS_PER_PAGE:(p + 1) * CONTENTS_PER_PAGE])
        + f"</ol>{folio(2 + p)}</section>"
        for p in range(contents_pages)
    )
    sheets = "".join(
        f'<section class="sheet">{"".join(cards[i:i + per_sheet])}{folio(page_of(i))}</section>'
        for i in range(0, len(cards), per_sheet)
    )
    tickets = "".join(
        f'<div class="ticket">{tap_target()}<div><div class="label">{label}</div>'
        f'<small>{escape(base_url)}/{action}</small></div></div>'
        for action, label in control_actions(plex)
    )
    remote = (
        f'<section class="remote"><div class="frame"></div><h2 class="display">The Remote</h2>'
        f'<div class="sub">Tap a ticket to control whatever is playing.</div>'
        f'<div class="tickets">{tickets}</div></section>'
    )
    css = Template(CSS).substitute({k: v for k, v in spec.items() if k != "summary"}, page=page_size)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<link rel='stylesheet' href='{FONTS}'><style>{css}</style></head><body>"
        + cover + contents + sheets + remote + "</body></html>"
    )


def build(plex: Plex, base_url: str, layout: str, title: str, collection: str | None,
          qr: bool, page_size: str, out_dir: str) -> tuple[str, int]:
    """Write a standalone book.html (posters embedded) and tags.csv into out_dir."""
    movies = list(plex.movies(collection))
    os.makedirs(out_dir, exist_ok=True)
    count = iter(range(1, len(movies) + 1))

    def embed(movie):
        print(f"[{next(count)}/{len(movies)}] {movie.title}")
        return data_uri(plex.poster_url(movie, 1200, 1800))

    html = render_html(plex, base_url, movies, layout, title, qr, page_size, poster_src=embed)
    book_path = os.path.join(out_dir, "book.html")
    with open(book_path, "w") as f:
        f.write(html)
    with open(os.path.join(out_dir, "tags.csv"), "w", newline="") as f:
        write_csv(tag_rows(plex, base_url, movies), f)
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
