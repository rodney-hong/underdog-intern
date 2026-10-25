"""
A synthetic pick'em-style screenshot of player cards, drawn with Pillow at test time.
Not a real Underdog screenshot and never written to the repo: tests build it in memory.
Each card: player name, matchup, line, stat, and Higher / Lower multipliers (optional).
"""
import io

from PIL import Image, ImageDraw, ImageFont

CARDS = [
    {"player_name": "Giannis Antetokounmpo", "matchup": "MIA vs CHA - 6:00PM", "stat": "Points",
     "line": 27.5, "higher": 1.03, "lower": 0.88},
    {"player_name": "Brandon Miller", "matchup": "CHA vs MIA - 6:00PM", "stat": "3PM",
     "line": 2.5, "higher": None, "lower": None},
    {"player_name": "Jalen Williams", "matchup": "OKC @ DEN - 9:00PM", "stat": "Rebounds",
     "line": 5.5, "higher": 0.92, "lower": 1.05},
    {"player_name": "Cade Cunningham", "matchup": "DET vs NYK - 7:30PM", "stat": "Assists",
     "line": 8.5, "higher": 1.0, "lower": 1.0},
]

_STAT_LABEL = {"Points": "Points", "Rebounds": "Rebounds", "Assists": "Assists", "3PM": "3-Pointers Made"}


def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


GRID_CARDS = CARDS + [
    {"player_name": "Shai Gilgeous-Alexander", "matchup": "OKC @ DEN - 9:00PM", "stat": "Points",
     "line": 31.5, "higher": 1.04, "lower": 0.87},
    {"player_name": "Nikola Jokic", "matchup": "DEN vs OKC - 9:00PM", "stat": "Rebounds",
     "line": 12.5, "higher": None, "lower": None},
]


def render_grid(cards=GRID_CARDS, cols: int = 3, fmt: str = "PNG", header: bool = True) -> bytes:
    """Cards in a grid (desktop layout) with a nav/header bar above it, each card a filled
    rounded rectangle on a darker page: name, matchup, big line, stat label, two buttons."""
    cw, ch, gap, top = 380, 300, 24, (110 if header else 24)
    rows = -(-len(cards) // cols)
    W, H = gap + cols * (cw + gap), top + rows * (ch + gap)
    img = Image.new("RGB", (W, H), (14, 14, 18))
    d = ImageDraw.Draw(img)
    if header:   # full-width bar with text: must not be taken for a card
        d.rectangle([0, 0, W, 80], fill=(28, 28, 34))
        d.text((24, 22), "Pick'em  ·  NBA  ·  Higher / Lower", font=_font(30), fill=(235, 235, 240))
    big, mid, small = _font(46), _font(28), _font(24)
    for i, c in enumerate(cards):
        x, y = gap + (i % cols) * (cw + gap), top + (i // cols) * (ch + gap)
        d.rounded_rectangle([x, y, x + cw, y + ch], radius=16, fill=(32, 32, 40))
        d.text((x + 20, y + 18), c["player_name"], font=mid, fill=(245, 245, 245))
        d.text((x + 20, y + 56), c["matchup"], font=small, fill=(150, 150, 160))
        d.text((x + 20, y + 104), f"{c['line']:g}", font=big, fill=(245, 245, 245))
        d.text((x + 20, y + 162), _STAT_LABEL[c["stat"]], font=mid, fill=(200, 200, 210))
        for j, side in enumerate(("Higher", "Lower")):
            bx = x + 20 + j * 175
            d.rounded_rectangle([bx, y + 214, bx + 160, y + 278], radius=10, fill=(54, 54, 66))
            mult = c["higher" if side == "Higher" else "lower"]
            d.text((bx + 14, y + 222), side + (f"  {mult:.2f}x" if mult is not None else ""),
                   font=small, fill=(230, 230, 235))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def render(cards=CARDS, fmt: str = "PNG") -> bytes:
    """The screenshot as image bytes (PNG by default), one card per row."""
    w, card_h, pad = 900, 210, 20
    img = Image.new("RGB", (w, pad + len(cards) * (card_h + pad)), (18, 18, 22))
    d = ImageDraw.Draw(img)
    big, mid, small = _font(40), _font(30), _font(26)
    for i, c in enumerate(cards):
        y = pad + i * (card_h + pad)
        d.rounded_rectangle([pad, y, w - pad, y + card_h], radius=18, fill=(34, 34, 42))
        d.text((pad + 24, y + 18), c["player_name"], font=big, fill=(245, 245, 245))
        d.text((pad + 24, y + 70), c["matchup"], font=small, fill=(150, 150, 160))
        line = f"{c['line']:g}"
        d.text((pad + 24, y + 120), line, font=big, fill=(245, 245, 245))
        d.text((pad + 24 + 30 * len(line), y + 128), _STAT_LABEL[c["stat"]], font=mid, fill=(200, 200, 210))
        for j, side in enumerate(("Higher", "Lower")):
            mult = c["higher" if side == "Higher" else "lower"]
            x0 = w - pad - 330 + j * 165
            d.rounded_rectangle([x0, y + 110, x0 + 150, y + 180], radius=12, fill=(52, 52, 64))
            d.text((x0 + 14, y + 116), side, font=small, fill=(230, 230, 235))
            if mult is not None:
                d.text((x0 + 14, y + 146), f"{mult:.2f}x", font=small, fill=(250, 210, 90))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# Underdog's dark theme: outlined cards whose fill is almost the page colour (only the
# outline and the buttons stand out), line arrows on some cards, and one or two pick buttons.
VARIANT_CARDS = [
    {"player_name": "Daniss Jenkins", "matchup": "DET @ PHI - 6:30PM", "stat": "3PM", "line": 0.5,
     "arrows": True, "sides": "both", "higher": 1.55, "lower": 2.11},
    {"player_name": "Paul Reed", "matchup": "DET @ PHI - 6:30PM", "stat": "3PM", "line": 0.5,
     "arrows": True, "sides": "both", "higher": 2.55, "lower": 1.37},
    {"player_name": "Ausar Thompson", "matchup": "DET @ PHI - 6:30PM", "stat": "3PM", "line": 0.5,
     "arrows": False, "sides": "both", "higher": 4.44, "lower": 1.15},
    {"player_name": "Adem Bona", "matchup": "PHI vs DET - 6:30PM", "stat": "3PM", "line": 0.5,
     "arrows": False, "sides": "higher", "higher": 5.82, "lower": None},
]


def render_underdog(cards=VARIANT_CARDS, cols: int = 2, crop_top: int = 0) -> bytes:
    """Cards as on Underdog's dark grid (crop_top cuts the first row's top, as a scrolled
    screenshot does). sides: 'both', 'higher' (one wide Higher button) or 'lower'."""
    cw, ch, gap, pad = 432, 154, 10, 8
    rows = -(-len(cards) // cols)
    W, H = pad + cols * (cw + gap), pad + rows * (ch + gap)
    page, fill, outline, button = (4, 6, 10), (6, 8, 12), (44, 48, 60), (20, 23, 30)
    img = Image.new("RGB", (W, H), page)
    d = ImageDraw.Draw(img)
    name, small, big = _font(14), _font(12), _font(15)
    for i, c in enumerate(cards):
        x, y = pad + (i % cols) * (cw + gap), pad + (i // cols) * (ch + gap)
        d.rounded_rectangle([x, y, x + cw, y + ch], radius=12, fill=fill, outline=outline, width=2)
        d.ellipse([x + 16, y + 16, x + 72, y + 72], fill=(60, 50, 50))
        d.text((x + 88, y + 26), c["player_name"], font=name, fill=(245, 245, 245))
        d.text((x + 88, y + 50), c["matchup"], font=small, fill=(200, 200, 205))
        lx = x + 34 if c["arrows"] else x + 14
        if c["arrows"]:
            d.text((x + 14, y + 104), "^", font=small, fill=(220, 220, 220))
            d.text((x + 14, y + 122), "v", font=small, fill=(220, 220, 220))
        d.text((lx, y + 104), f"{c['line']:g}", font=big, fill=(245, 245, 245))
        d.text((lx, y + 124), _STAT_LABEL[c["stat"]], font=small, fill=(170, 170, 180))
        sides = ["Higher", "Lower"] if c["sides"] == "both" else [c["sides"].capitalize()]
        bw = 96 if len(sides) == 2 else 196
        for j, side in enumerate(sides):
            bx = x + 232 + j * 100
            d.rounded_rectangle([bx, y + 96, bx + bw, y + 144], radius=6, fill=button)
            mult = c["higher" if side == "Higher" else "lower"]
            d.text((bx + bw // 2 - 20, y + 102), side, font=small, fill=(240, 240, 240))
            if mult is not None:
                d.text((bx + bw // 2 - 14, y + 122), f"{mult:g}x", font=small, fill=(220, 220, 225))
    if crop_top:
        img = img.crop((0, crop_top, W, H))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
