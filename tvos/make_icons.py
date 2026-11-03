"""Draw layered tvOS icon designs (back + front) for the app name options.

Layers are drawn at 1280x768 (the App Store size) with 4x supersampling, then
scaled for the other sizes. Output: <out>/<slug>/{back,front}.png and a
preview sheet with every option flattened.
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 768
SS = 4  # supersampling


def canvas(color=(0, 0, 0, 0)):
    return Image.new("RGBA", (W * SS, H * SS), color)


def done(img):
    return img.resize((W, H), Image.LANCZOS)


def gradient(top, bottom):
    img = Image.new("RGBA", (W * SS, H * SS))
    d = ImageDraw.Draw(img)
    for y in range(H * SS):
        t = y / (H * SS - 1)
        d.line([(0, y), (W * SS, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return img


def radial_glow(img, center, radius, color):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
                                 fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(radius * 0.45))
    return Image.alpha_composite(img, glow)


def s(v):
    return int(v * SS)


def waves(d, cx, cy, start, count, gap, width, color, span=50):
    """NFC-style arcs, opening to the right."""
    for i in range(count):
        r = s(start + i * gap)
        d.arc([cx - r, cy - r, cx + r, cy + r], -span, span, fill=color, width=s(width))


def play_triangle(d, cx, cy, size, color):
    h = size * math.sqrt(3) / 2
    d.polygon([(cx - s(h / 3), cy - s(size / 2)), (cx - s(h / 3), cy + s(size / 2)), (cx + s(2 * h / 3), cy)],
              fill=color)


# ---- 1. Movie Book: an open book whose pages are film frames ----------------
def movie_book():
    back = gradient((24, 20, 48), (8, 6, 18))
    back = radial_glow(back, (s(640), s(400)), s(380), (120, 70, 220, 150))
    front = canvas()
    d = ImageDraw.Draw(front)
    cx, cy = s(640), s(400)
    page_w, page_h = s(300), s(380)
    gold = (245, 196, 82, 255)
    for side in (-1, 1):
        # each page is a slightly tilted film frame
        x0 = cx if side > 0 else cx - page_w
        poly = [(x0, cy - page_h // 2 + (s(18) if side < 0 else 0)),
                (x0 + page_w, cy - page_h // 2 + (s(18) if side > 0 else 0)),
                (x0 + page_w, cy + page_h // 2), (x0, cy + page_h // 2)]
        if side < 0:
            poly = [(cx - page_w, cy - page_h // 2), (cx, cy - page_h // 2 + s(24)),
                    (cx, cy + page_h // 2 + s(24)), (cx - page_w, cy + page_h // 2)]
        else:
            poly = [(cx, cy - page_h // 2 + s(24)), (cx + page_w, cy - page_h // 2),
                    (cx + page_w, cy + page_h // 2), (cx, cy + page_h // 2 + s(24))]
        d.polygon(poly, fill=(250, 246, 236, 255))
        # sprocket holes along the outer edge
        ex = cx + side * (page_w - s(30))
        for k in range(6):
            y = cy - page_h // 2 + s(40) + k * s(58)
            d.rounded_rectangle([ex - s(12), y, ex + s(12), y + s(30)], radius=s(6), fill=(40, 34, 70, 255))
    # a gold play mark on the right page
    play_triangle(d, cx + s(140), cy + s(10), 130, gold)
    d.line([cx, cy - page_h // 2 + s(24), cx, cy + page_h // 2 + s(24)], fill=(200, 192, 176, 255), width=s(6))
    return back, front


# ---- 2. Poster Tap: a movie poster with NFC waves -----------------------------
def poster_tap():
    back = gradient((10, 40, 70), (4, 12, 26))
    back = radial_glow(back, (s(700), s(380)), s(420), (30, 160, 220, 140))
    front = canvas()
    d = ImageDraw.Draw(front)
    px, py, pw, ph = s(430), s(150), s(300), s(450)
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=s(26), fill=(255, 255, 255, 255))
    d.rounded_rectangle([px + s(22), py + s(22), px + pw - s(22), py + ph - s(110)], radius=s(16),
                        fill=(255, 106, 76, 255))
    # a little skyline "poster art"
    for i, hgt in enumerate([120, 190, 150, 230, 170]):
        x = px + s(40) + i * s(46)
        d.rectangle([x, py + ph - s(110) - s(hgt), x + s(36), py + ph - s(110)], fill=(140, 40, 60, 255))
    d.ellipse([px + s(180), py + s(60), px + s(240), py + s(120)], fill=(255, 220, 120, 255))
    for i, wdt in enumerate([200, 140]):
        d.rounded_rectangle([px + s(40), py + ph - s(80) + i * s(34), px + s(40) + s(wdt), py + ph - s(62) + i * s(34)],
                            radius=s(8), fill=(40, 52, 80, 255))
    waves(d, px + pw + s(20), py + ph // 2, 70, 3, 60, 22, (120, 220, 255, 255))
    return back, front


# ---- 3. Tap & Play: a play button inside NFC rings ----------------------------
def tap_and_play():
    back = gradient((60, 14, 40), (18, 4, 14))
    back = radial_glow(back, (s(640), s(384)), s(400), (255, 60, 120, 140))
    front = canvas()
    d = ImageDraw.Draw(front)
    cx, cy = s(640), s(384)
    for i, alpha in enumerate([255, 170, 95]):
        r = s(150 + i * 70)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 214, 230, alpha), width=s(16))
    d.ellipse([cx - s(120), cy - s(120), cx + s(120), cy + s(120)], fill=(255, 255, 255, 255))
    play_triangle(d, cx + s(6), cy, 120, (220, 40, 100, 255))
    return back, front


# ---- 4. Marquee: a cinema sign with bulbs -------------------------------------
def marquee():
    back = gradient((40, 8, 8), (12, 2, 2))
    back = radial_glow(back, (s(640), s(384)), s(420), (255, 120, 40, 120))
    front = canvas()
    d = ImageDraw.Draw(front)
    x0, y0, x1, y1 = s(280), s(220), s(1000), s(548)
    d.rounded_rectangle([x0, y0, x1, y1], radius=s(40), fill=(250, 200, 70, 255))
    d.rounded_rectangle([x0 + s(36), y0 + s(36), x1 - s(36), y1 - s(36)], radius=s(22), fill=(22, 10, 10, 255))
    # bulbs around the border
    n_x, n_y = 13, 6
    for i in range(n_x):
        for y in (y0 + s(18), y1 - s(18)):
            x = x0 + s(40) + i * (x1 - x0 - s(80)) // (n_x - 1)
            d.ellipse([x - s(9), y - s(9), x + s(9), y + s(9)], fill=(255, 250, 220, 255))
    for j in range(1, n_y - 1):
        for x in (x0 + s(18), x1 - s(18)):
            y = y0 + s(18) + j * (y1 - y0 - s(36)) // (n_y - 1)
            d.ellipse([x - s(9), y - s(9), x + s(9), y + s(9)], fill=(255, 250, 220, 255))
    play_triangle(d, s(640) + s(10), s(384), 150, (255, 210, 90, 255))
    return back, front


DESIGNS = {
    "movie-book": ("Movie Book", movie_book),
    "poster-tap": ("Poster Tap", poster_tap),
    "tap-and-play": ("Tap & Play", tap_and_play),
    "marquee": ("Marquee", marquee),
}


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for slug, (name, draw) in DESIGNS.items():
        back, front = (done(layer) for layer in draw())
        folder = out / slug
        folder.mkdir(exist_ok=True)
        back.convert("RGB").save(folder / "back.png")
        front.save(folder / "front.png")
        tiles.append((name, Image.alpha_composite(back, front)))

    # Preview sheet: each option as it would sit on the Home screen, with its name
    tw, th, pad = 640, 384, 60
    sheet = Image.new("RGB", (2 * tw + 3 * pad, 2 * (th + 70) + 2 * pad), (28, 28, 30))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, img) in enumerate(tiles):
        x = pad + (i % 2) * (tw + pad)
        y = pad + (i // 2) * (th + 70 + pad // 2)
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, tw, th], radius=40, fill=255)
        sheet.paste(img.resize((tw, th), Image.LANCZOS), (x, y), mask)
        d.text((x + tw // 2, y + th + 34), f"{i + 1}. {name}", fill=(235, 235, 240), font=font, anchor="mm")
    sheet.save(out / "options.png")
    print(out / "options.png")


if __name__ == "__main__":
    main(Path(sys.argv[1]))


def asset_catalog(slug: str, out: Path):
    """Write a tvOS asset catalog (layered app icon + top shelf images) for one design."""
    import json

    name, draw = DESIGNS[slug]
    back_full, front_full = (done(layer) for layer in draw())
    brand = out / "App Icon & Top Shelf Image.brandassets"

    def write(path: Path, contents: dict):
        path.mkdir(parents=True, exist_ok=True)
        (path / "Contents.json").write_text(json.dumps({**contents, "info": {"author": "xcode", "version": 1}}, indent=2))

    write(out, {})

    def stack(folder: str, sizes):  # sizes: [(scale, (w, h))]
        root = brand / folder
        write(root, {"layers": [{"filename": "Front.imagestacklayer"}, {"filename": "Back.imagestacklayer"}]})
        for layer, img in (("Front", front_full), ("Back", back_full)):
            lroot = root / f"{layer}.imagestacklayer"
            write(lroot, {})
            images = []
            iset = lroot / "Content.imageset"
            iset.mkdir(parents=True, exist_ok=True)
            for scale, size in sizes:
                fn = f"{layer.lower()}-{size[0]}x{size[1]}.png"
                resized = img.resize(size, Image.LANCZOS)
                (resized.convert("RGB") if layer == "Back" else resized).save(iset / fn)
                images.append({"idiom": "tv", "filename": fn, "scale": scale})
            write(iset, {"images": images})

    stack("App Icon.imagestack", [("1x", (400, 240)), ("2x", (800, 480))])
    stack("App Icon - App Store.imagestack", [("1x", (1280, 768))])

    flat = Image.alpha_composite(back_full, front_full)

    def shelf(folder: str, w: int, h: int):
        iset = brand / folder
        images = []
        for scale, mult in (("1x", 1), ("2x", 2)):
            bw, bh = w * mult, h * mult
            # the icon's background stretched wide, with the icon art centered
            banner = back_full.resize((bw, bh), Image.LANCZOS)
            art = flat.resize((int(bh * W / H), bh), Image.LANCZOS)
            banner.paste(art, ((bw - art.width) // 2, 0))
            fn = f"shelf-{bw}x{bh}.png"
            iset.mkdir(parents=True, exist_ok=True)
            banner.convert("RGB").save(iset / fn)
            images.append({"idiom": "tv", "filename": fn, "scale": scale})
        write(iset, {"images": images})

    shelf("Top Shelf Image.imageset", 1920, 720)
    shelf("Top Shelf Image Wide.imageset", 2320, 720)

    write(brand, {"assets": [
        {"filename": "App Icon - App Store.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "1280x768"},
        {"filename": "App Icon.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "400x240"},
        {"filename": "Top Shelf Image Wide.imageset", "idiom": "tv", "role": "top-shelf-image-wide", "size": "2320x720"},
        {"filename": "Top Shelf Image.imageset", "idiom": "tv", "role": "top-shelf-image", "size": "1920x720"},
    ]})
    print(f"{name}: {out}")


# ---- Marquee, take 2: a modern flat take on a vintage theater front ----------
TEAL_TOP, TEAL_BOTTOM = (18, 70, 78), (6, 30, 36)
CREAM, RED, RED_DARK, BULB, BRASS = (246, 236, 214, 255), (206, 52, 58, 255), (150, 30, 40, 255), (255, 247, 222, 255), (222, 186, 120, 255)


def marquee_vintage():
    back = gradient(TEAL_TOP, TEAL_BOTTOM)
    # faint art-deco sunburst rising from below the sign
    rays = Image.new("RGBA", back.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    ox, oy = s(640), s(760)
    for i in range(-9, 10):
        a0, a1 = math.radians(-90 + i * 9 - 2.2), math.radians(-90 + i * 9 + 2.2)
        far = s(1400)
        rd.polygon([(ox, oy), (ox + far * math.cos(a0), oy + far * math.sin(a0)),
                    (ox + far * math.cos(a1), oy + far * math.sin(a1))], fill=(255, 255, 255, 14))
    back = Image.alpha_composite(back, rays)

    front = canvas()
    d = ImageDraw.Draw(front)
    cx = s(640)
    # art-deco crown: stepped fan above the canopy
    for i, (w, h) in enumerate([(260, 150), (190, 120), (120, 92)]):
        color = (BRASS, CREAM, RED)[i]
        d.pieslice([cx - s(w), s(250) - s(h), cx + s(w), s(250) + s(h)], 180, 360, fill=color)
    d.rectangle([cx - s(270), s(244), cx + s(270), s(262)], fill=BRASS)

    # canopy: a wide red band with two rows of bulbs
    cx0, cx1, cy0, cy1 = s(250), s(1030), s(262), s(392)
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=s(26), fill=RED)
    d.rounded_rectangle([cx0, cy1 - s(26), cx1, cy1], radius=s(14), fill=RED_DARK)
    step = (cx1 - cx0 - s(100)) // 14
    for row, y in enumerate((s(300), s(342))):
        # the second row sits between the first row's bulbs
        for i in range(15 - row):
            x = cx0 + s(50) + i * step + (step // 2 if row else 0)
            d.ellipse([x - s(11), y - s(11), x + s(11), y + s(11)], fill=BULB)

    # letter board: cream panel with rows of letter tiles
    bx0, bx1, by0, by1 = s(300), s(980), s(392), s(578)
    d.rectangle([bx0, by0, bx1, by1], fill=CREAM)
    d.rectangle([bx0, by1 - s(12), bx1, by1], fill=BRASS)
    import random
    rnd = random.Random(7)
    for row, (y, words) in enumerate(((s(424), [3, 5, 4]), (s(494), [4, 6]))):
        widths = [w * 30 + (w - 1) * 8 for w in words]
        total = sum(widths) + 44 * (len(words) - 1)
        x = cx - s(total) // 2
        for w in words:
            for _ in range(w):
                tile_h = s(50)
                d.rounded_rectangle([x, y, x + s(30), y + tile_h], radius=s(5),
                                    fill=(40, 40, 46, 255) if rnd.random() > 0.15 else RED)
                x += s(38)
            x += s(36)

    # posts under the board, then a row of bulbs along the soffit
    for px in (s(330), s(950)):
        d.rectangle([px - s(14), by1, px + s(14), s(668)], fill=RED_DARK)
    for i in range(12):
        x = bx0 + s(30) + i * (bx1 - bx0 - s(60)) // 11
        d.ellipse([x - s(9), by1 + s(22) - s(9), x + s(9), by1 + s(22) + s(9)], fill=BULB)
    return back, front


DESIGNS["marquee"] = ("Marquee", marquee_vintage)


def demo_assets(out: Path):
    """The welcome screen's Marquee art and simple posters for the demo films (drawn here, not the films' own art)."""
    import json

    def imageset(name: str, img: Image.Image):
        folder = out / f"{name}.imageset"
        folder.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(folder / f"{name}.png")
        (folder / "Contents.json").write_text(json.dumps(
            {"images": [{"idiom": "tv", "filename": f"{name}.png", "scale": "1x"}],
             "info": {"author": "xcode", "version": 1}}, indent=2))

    back, front = (done(layer) for layer in marquee_vintage())
    imageset("Marquee", Image.alpha_composite(back, front))

    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 64)
        small_font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 30)
    except OSError:
        title_font = small_font = ImageFont.load_default()

    def poster(name, title, subtitle, top, bottom, draw_art):
        pw, ph = 600, 900
        img = Image.new("RGB", (pw, ph))
        d = ImageDraw.Draw(img)
        for y in range(ph):
            t = y / (ph - 1)
            d.line([(0, y), (pw, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
        draw_art(d, pw, ph)
        d.text((pw // 2, ph - 170), title, fill=(255, 255, 255), font=title_font, anchor="mm")
        d.text((pw // 2, ph - 100), subtitle, fill=(230, 230, 230), font=small_font, anchor="mm")
        imageset(name, img)

    def bunny(d, w, h):  # a big round hill and a sun
        d.ellipse([w * 0.62, h * 0.12, w * 0.86, h * 0.28], fill=(255, 226, 120))
        d.ellipse([-w * 0.4, h * 0.45, w * 1.4, h * 1.3], fill=(92, 168, 72))
        d.ellipse([w * 0.32, h * 0.30, w * 0.68, h * 0.62], fill=(240, 240, 236))  # a round, bunny-ish shape
        d.ellipse([w * 0.38, h * 0.16, w * 0.46, h * 0.36], fill=(240, 240, 236))
        d.ellipse([w * 0.54, h * 0.16, w * 0.62, h * 0.36], fill=(240, 240, 236))

    def steel(d, w, h):  # a city skyline and a glowing robot eye
        for i, bh in enumerate([300, 420, 260, 480, 340, 380]):
            x = i * w / 6
            d.rectangle([x + 8, h * 0.72 - bh, x + w / 6 - 8, h * 0.72], fill=(30, 40, 56))
        d.ellipse([w * 0.40, h * 0.20, w * 0.60, h * 0.33], fill=(255, 80, 60))
        d.ellipse([w * 0.46, h * 0.24, w * 0.54, h * 0.29], fill=(255, 220, 200))

    def sintel(d, w, h):  # snowy peaks and a small dragon-like silhouette
        d.polygon([(0, h * 0.7), (w * 0.3, h * 0.35), (w * 0.55, h * 0.7)], fill=(220, 230, 240))
        d.polygon([(w * 0.35, h * 0.7), (w * 0.7, h * 0.28), (w, h * 0.7)], fill=(200, 212, 228))
        d.polygon([(w * 0.45, h * 0.18), (w * 0.62, h * 0.12), (w * 0.55, h * 0.2), (w * 0.7, h * 0.22),
                   (w * 0.52, h * 0.26)], fill=(120, 40, 50))

    def jellyfish(d, w, h):  # a jellyfish bell with trailing tentacles
        d.pieslice([w * 0.25, h * 0.15, w * 0.75, h * 0.55], 180, 360, fill=(255, 170, 220))
        for i in range(6):
            x = w * 0.3 + i * w * 0.08
            d.line([(x, h * 0.35), (x + 18, h * 0.5), (x - 10, h * 0.62)], fill=(255, 190, 230), width=6)

    poster("DemoBigBuckBunny", "Big Buck Bunny", "Blender Foundation · 2008", (120, 196, 240), (230, 246, 255), bunny)
    poster("DemoSintel", "Sintel", "Preview · Blender Foundation", (60, 90, 140), (150, 170, 200), sintel)
    poster("DemoJellyfish", "Jellyfish", "Preview", (10, 40, 90), (20, 110, 160), jellyfish)
    print("demo assets:", out)
