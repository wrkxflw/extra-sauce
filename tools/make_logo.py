"""Draw the Extra Sauce logo and splash.

Logo: line-art takeout box (thick dark outline, flat fills), a strip of film being pulled out
of it by a pair of chopsticks, red badge on the front. Laid out after the user's sketch.
Splash: the logo large inside a soft circle on white, wordmark and "powered by WRK×FLW" below.

Writes app/web/logo.png, app/web/icon.png, app/assets/icon.ico (Windows), app/assets/icon.icns (macOS)
and app/assets/splash.png.
Everything is drawn at 4x and downsampled.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LINE = (14, 12, 12, 255)
BOX = (242, 225, 216, 255)
INSIDE = (216, 193, 179, 255)
RED = (252, 94, 62, 255)
STICK = (252, 94, 62, 255)
STICK_TIP = (250, 168, 140, 255)
FILM = (154, 95, 88, 255)
FILM_DARK = (230, 160, 40, 255)
HOLE = (255, 255, 255, 255)
GOLD = (213, 166, 58, 255)
SS = 4
N = 1024 * SS
LW = 24
ROOT = Path(__file__).resolve().parents[1] / "app"
FONTS = ROOT / "web" / "fonts"


def P(pts):
    return [(x * SS, y * SS) for x, y in pts]


def S(*v):
    return [round(x * SS) for x in v]


def shape(d, pts, fill, lw=LW):
    """Filled polygon with a thick outline and round joins."""
    d.polygon(P(pts), fill=fill)
    closed = pts + pts[:1]
    d.line(P(closed), fill=LINE, width=lw * SS, joint="curve")
    r = lw * SS / 2
    for x, y in pts:
        d.ellipse([x * SS - r, y * SS - r, x * SS + r, y * SS + r], fill=LINE)


def rrect(d, box, radius, fill, lw=LW):
    d.rounded_rectangle(S(*box), radius=radius * SS, fill=fill, outline=LINE, width=lw * SS)


K = 1024 / 1200          # the logo is laid out on a 1200 grid (the user's sketch) and drawn at 1024


def Q(pts):
    return [(x * K * SS, y * K * SS) for x, y in pts]


def stroke(d, pts, lw=22, closed=False):
    """Dark outline through the points, round joins and caps."""
    pts = pts + pts[:1] if closed else pts
    d.line(Q(pts), fill=LINE, width=round(lw * K * SS), joint="curve")
    r = lw * K * SS / 2
    for x, y in Q(pts):
        d.ellipse([x - r, y - r, x + r, y + r], fill=LINE)


def chopstick(d, x0, y0, w0, x1, y1, w1, gap=None):
    """Near-horizontal stick, thin end at (x0,y0) on the left, thick end at (x1,y1).
    gap=(xa, xb) leaves a break in the top outline, the style's highlight."""
    top = [(x0, y0 - w0), (x1, y1 - w1)]
    pts = [top[0], top[1], (x1, y1 + w1), (x0, y0 + w0)]
    d.polygon(Q(pts), fill=STICK)
    if gap:
        xa, xb = gap
        ya = y0 - w0 + (y1 - w1 - y0 + w0) * (xa - x0) / (x1 - x0)
        yb = y0 - w0 + (y1 - w1 - y0 + w0) * (xb - x0) / (x1 - x0)
        stroke(d, [(xb, yb), top[1], (x1, y1 + w1), (x0, y0 + w0), top[0], (xa, ya)])
    else:
        stroke(d, pts, closed=True)


def draw_logo() -> Image.Image:
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # upper chopstick first: it runs behind the leader
    chopstick(d, 150, 122, 20, 1150, 117, 33)

    # film strip rising out of the box; the leader narrows to a tongue on the left
    film = [(376, 470), (376, 92), (382, 81), (452, 81), (465, 92), (465, 128), (474, 152), (500, 170),
            (528, 170), (548, 180), (558, 200), (558, 470)]
    d.polygon(Q(film), fill=FILM)
    stroke(d, film)
    for y in range(90, 460, 28):                     # sprocket holes
        d.rectangle(Q([(389, y), (403, y + 14)]), fill=LINE)
        if y > 190:
            d.rectangle(Q([(528, y), (542, y + 14)]), fill=LINE)

    # lower chopstick in front, pinching the tongue against the upper one
    chopstick(d, 150, 228, 21, 1150, 268, 34, gap=(985, 1065))

    # box: front face, the inside showing above it, the flaps as open lines
    tl, tr, il, ir, bl, br = (130, 480), (820, 480), (235, 665), (710, 665), (230, 1155), (715, 1155)
    d.polygon(Q([tl, il, ir, tr, br, bl]), fill=BOX)
    d.polygon(Q([tl, tr, ir, il]), fill=INSIDE)
    stroke(d, [tl, tr])
    stroke(d, [tl, il, ir, tr])
    stroke(d, [tl, (40, 668)])
    stroke(d, [tr, (905, 668)])
    stroke(d, [tr, br, bl, (203, 970)])
    stroke(d, [tl, (187, 878)])                      # left edge with a break and a dot, like the reference
    stroke(d, [(195, 922), (195, 922)])

    # badge: red disc, two strokes (chopsticks) running out past its edge
    cx, cy, r = 473, 920, 116
    d.ellipse(Q([(cx - r, cy - r), (cx + r, cy + r)]), fill=RED)
    d.ellipse(Q([(cx - r, cy - r), (cx + r, cy + r)]), outline=LINE, width=round(22 * K * SS))
    stroke(d, [(446, 946), (555, 752)])
    stroke(d, [(500, 960), (622, 792)])
    return im.resize((1024, 1024), Image.LANCZOS)


def tile(logo: Image.Image) -> Image.Image:
    im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    m = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(m).rounded_rectangle(S(24, 24, 1000, 1000), radius=228 * SS, fill=(255, 255, 255, 255))
    im.alpha_composite(m.resize((1024, 1024), Image.LANCZOS))
    im.alpha_composite(logo.resize((900, 900), Image.LANCZOS), (62, 50))
    return im


def font(name, size, weight):
    """Nunito Sans is a variable font: pick the weight on its wght axis."""
    f = ImageFont.truetype(str(FONTS / name), size * SS)
    f.set_variation_by_axes([weight if a["name"] in (b"Weight", "Weight") else a["default"] for a in f.get_variation_axes()])
    return f


def splash(logo: Image.Image) -> Image.Image:
    W, H = 720, 520
    im = Image.new("RGBA", (W * SS, H * SS), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    cx, cy, r = W // 2, 196, 150
    d.ellipse(S(cx - r, cy - r, cx + r, cy + r), fill=(250, 244, 236, 255), outline=(240, 228, 212, 255), width=3 * SS)
    big = logo.resize((250 * SS, 250 * SS), Image.LANCZOS)
    im.alpha_composite(big, ((cx - 125) * SS, (cy - 132) * SS))
    heavy = font("NunitoSans.ttf", 40, 900)
    italic = font("NunitoSans-Italic.ttf", 36, 400)
    small_i = font("NunitoSans-Italic.ttf", 13, 400)
    small_b = font("NunitoSans.ttf", 13, 900)
    # wordmark: EXTRA sauce, centred
    parts = [("EXTRA", heavy, LINE[:3]), ("sauce", italic, GOLD[:3])]
    gaps = [0, 8 * SS]
    widths = [d.textlength(t, font=f) for t, f, _ in parts]
    x = cx * SS - (sum(widths) + sum(gaps)) / 2
    y = 382 * SS
    for (t, f, c), w, g in zip(parts, widths, gaps):
        x += g
        d.text((x, y + (3 * SS if f is italic else 0)), t, font=f, fill=c)
        x += w
    # powered by WRK×FLW
    seg = [("powered by  ", small_i, (176, 180, 188)), ("WRK", small_b, (154, 158, 166)), ("×", small_b, GOLD[:3]),
           ("FLW", small_b, (154, 158, 166))]
    ws = [d.textlength(t, font=f) for t, f, _ in seg]
    x = cx * SS - sum(ws) / 2
    for (t, f, c), w in zip(seg, ws):
        d.text((x, 446 * SS), t, font=f, fill=c)
        x += w
    return im.resize((W, H), Image.LANCZOS).convert("RGB")


if __name__ == "__main__":
    (ROOT / "web").mkdir(parents=True, exist_ok=True)
    (ROOT / "assets").mkdir(parents=True, exist_ok=True)
    logo = draw_logo()
    logo.save(ROOT / "web" / "logo.png")
    icon = tile(logo)
    icon.save(ROOT / "web" / "icon.png")
    icon.save(ROOT / "assets" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.save(ROOT / "assets" / "icon.icns")
    splash(logo).save(ROOT / "assets" / "splash.png")
    print("ok")
