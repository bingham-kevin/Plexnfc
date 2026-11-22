"""Text for the Movie Remote's screen.

The box draws with LVGL's built-in Montserrat fonts, which have only plain ASCII
letters (and a few symbols). Any other character shows as an empty box: "8½" came out
as "8☐". So everything sent to a box is turned into the nearest ASCII here, on the
server, where Python does it properly: "8½" -> "8 1/2", "Amélie" -> "Amelie",
"Alien³" -> "Alien3", curly quotes and dashes -> straight ones.
"""
from __future__ import annotations

import re
import unicodedata

# Characters NFKD doesn't take apart into ASCII
_REPLACE = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", "×": "x", "⁄": "/", "∕": "/", "•": "-", "·": "-",
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O",
    "ß": "ss", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ł": "l",
    "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "¡": "!", "¿": "?",
    "©": "(c)", "®": "(R)", "™": "TM",
}
_REPLACE_RE = re.compile("|".join(map(re.escape, _REPLACE)))
# Vulgar fractions (½, ¼, ⅓…); after a digit they need a space: 8½ -> 8 1/2
_FRACTION_RE = re.compile(r"(\d?)([¼-¾⅐-⅟↉])")


def _fraction(m: re.Match) -> str:
    text = unicodedata.normalize("NFKD", m.group(2)).replace("⁄", "/")
    return f"{m.group(1)} {text}" if m.group(1) else text


def box_text(text: str) -> str:
    """The nearest plain-ASCII form of ``text``, for the box's screen."""
    if text.isascii():
        return text
    text = _FRACTION_RE.sub(_fraction, text)
    text = _REPLACE_RE.sub(lambda m: _REPLACE[m.group(0)], text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if c.isascii() and (c.isprintable() or c in "\n\t"))
    return re.sub(r"[ \t]{2,}", " ", text).strip(" \t")


def box_json(value):
    """``value`` (a JSON body) with every string in it made safe for the box's screen."""
    if isinstance(value, str):
        return box_text(value)
    if isinstance(value, list):
        return [box_json(v) for v in value]
    if isinstance(value, dict):
        return {k: box_json(v) for k, v in value.items()}
    return value
