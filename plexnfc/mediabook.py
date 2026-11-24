"""Printable books of albums and audiobooks, in the movie book's style (book.py).

A page per album (or playlist) or audiobook: its cover, what it is, and the gold circle
where its NFC card or sticker goes. Hold the circle on a Movie Remote and it plays on the
card's speaker; an audiobook picks up where it was left. Pages for things with a card can
carry a QR code too, which plays the card from a phone (``/c/<card ID>``).
"""
from __future__ import annotations

from html import escape
from string import Template

from .book import CONTENTS_PER_PAGE, CSS, FONTS, LAYOUTS, MARGIN, PAPER, qr_svg, tap_target

WORDS = {
    "music": {"title": "Music", "caps": "Now spinning", "one": "album", "many": "albums",
              "contents": "Track List", "sub": "On the shelf",
              "how": ("Find something you want to hear.",
                      "Hold the gold circle beside its cover on the Movie Remote.",
                      "It plays on its speaker.")},
    "books": {"title": "Story Time", "caps": "Now reading", "one": "book", "many": "books",
              "contents": "Contents", "sub": "On the shelf",
              "how": ("Find a book you want to hear.",
                      "Hold the gold circle beside its cover on the Movie Remote.",
                      "It picks up where you left off.")},
}

# Covers are square (albums) or near it (audiobooks), not 2:3 posters
EXTRA_CSS = """
.no-poster { aspect-ratio: 1 / 1; width: 80%; }
.speaker { font-size: 10px; color: var(--muted); margin-top: 4px; font-style: italic; }
"""


def duration_text(seconds) -> str:
    minutes = round((seconds or 0) / 60)
    if not minutes:
        return ""
    return f"{minutes // 60} h {minutes % 60:02d} m" if minutes >= 60 else f"{minutes} m"


def meta_line(item: dict, kind: str) -> str:
    """``The Beatles · 1969`` or ``Ada Author · read by Sam · 7 h 12 m``"""
    parts = [item.get("artist") or ""]
    if kind == "books":
        if item.get("narrator"):
            parts.append(f"read by {item['narrator']}")
        parts.append(duration_text(item.get("duration")))
    elif item.get("year"):
        parts.append(str(item["year"]))
    return " · ".join(p for p in parts if p)


def render(items: list[dict], kind: str = "music", layout: str = "full", title: str = "", qr: bool = True,
           page_size: str = "letter", art_src=None, cards: dict | None = None, base_url: str = "") -> str:
    """The book as one HTML page. ``art_src(item)`` is each cover's <img> src (or None);
    ``cards`` maps an item ID to its card ``{"uid", "speaker_name"}``, for the QR code
    and the speaker line."""
    words = WORDS[kind]
    spec = LAYOUTS[layout]
    per_sheet = spec["cols"] * spec["rows"]
    cards = cards or {}
    title = title or words["title"]
    contents_pages = max(1, -(-len(items) // CONTENTS_PER_PAGE))
    first_sheet = 2 + contents_pages

    def page_of(i: int) -> int:
        return first_sheet + i // per_sheet

    pages = []
    for item in items:
        src = art_src(item) if art_src else None
        cover = (f'<img class="poster" src="{escape(src)}" alt="">' if src
                 else f'<div class="no-poster">{escape(item["name"])}</div>')
        card = cards.get(item["id"])
        meta = meta_line(item, kind)
        series = item.get("album") if kind == "books" else ""
        summary = (item.get("description") or "") if spec["summary"] and kind == "books" else ""
        qr_url = f"{base_url}/c/{card['uid']}" if card else ""
        marks = (f'<div class="qr-box">{qr_svg(qr_url)}NO NFC? SCAN</div>' if qr and card else "") + tap_target()
        pages.append(
            f'<div class="card"><div class="poster-box">{cover}</div><div class="band"><div class="info">'
            f'<h3 class="title">{escape(item["name"])}</h3>'
            + (f'<div class="meta caps">{escape(meta)}</div>' if meta else "")
            + (f'<div class="speaker">{escape(series)}</div>' if series else "")
            + (f'<div class="speaker">Plays on {escape(card["speaker_name"])}</div>'
               if card and card.get("speaker_name") else "")
            + (f'<div class="summary">{escape(summary)}</div>' if summary else "")
            + f'</div><div class="marks">{marks}</div></div></div>'
        )

    def folio(n: int) -> str:
        return f'<div class="folio caps">&#9670; {n} &#9670;</div>'

    count = f"{len(items)} {words['one'] if len(items) == 1 else words['many']}"
    how = "".join(f"<div><b>{n}</b>{escape(step)}</div>" for n, step in enumerate(words["how"], 1))
    cover_page = (
        f'<section class="cover"><div class="frame"></div><div class="sign">'
        f'<div class="caps">{escape(words["caps"])}</div><h1 class="display">{escape(title)}</h1>'
        f'<div class="count">{count}</div></div><div class="how">{how}</div></section>'
    )
    ordered = sorted(enumerate(items), key=lambda im: (im[1]["name"].casefold(), im[0]))
    contents = "".join(
        f'<section class="contents"><div class="frame"></div>'
        f'<h2 class="display">{escape(words["contents"])}</h2><div class="sub caps">{escape(words["sub"])}</div><ol>'
        + "".join(f'<li><span class="t">{escape(m["name"])}'
                  + (f' <i>({escape(m["artist"])})</i>' if m.get("artist") else "")
                  + f'</span><span class="dots"></span><span class="n">{page_of(i)}</span></li>'
                  for i, m in ordered[p * CONTENTS_PER_PAGE:(p + 1) * CONTENTS_PER_PAGE])
        + f"</ol>{folio(2 + p)}</section>"
        for p in range(contents_pages)
    )
    sheets = "".join(
        f'<section class="sheet">{"".join(pages[i:i + per_sheet])}{folio(page_of(i))}</section>'
        for i in range(0, len(pages), per_sheet)
    )
    width, height = PAPER.get(page_size.casefold(), PAPER["letter"])
    css = Template(CSS).substitute({k: v for k, v in spec.items() if k != "summary"},
                                   page_width=width, page_height=height, margin=MARGIN)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<link rel='stylesheet' href='{FONTS}'><style>{css}{EXTRA_CSS}</style></head><body>"
        + cover_page + contents + sheets + "</body></html>"
    )
