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


# Underdog's iPhone game page (Players tab), 1185 x 2576 like an iPhone screenshot, laid out in
# units of 1/721 of the width (where it was measured). Every tile / row carries two marker
# squares of its own colours, one by the name and one by the line, so a stubbed reader can
# tell exactly which item a crop holds and whether the name and line are both in it.
PHONE_W, PHONE_H = 1185, 2576
PHONE_PAGE, PHONE_PILL, PHONE_PILL_ON = (1, 7, 14), (28, 30, 38), (230, 230, 235)
PHONE_HEADER_MARK = (255, 128, 0)
PHONE_TABS = ["Popular", "Points", "Rebounds", "Assists", "3-Pointers"]
PHONE_CONTENT_TOP = 521        # units: the sticky header ends here; items scroll under it
ROW_PITCH, ROW_H, TILE_PITCH, TILE_H = 107.5, 86, 465, 446

PHONE_ROWS = [{"player_name": "J. Tatum", "line": 24.5}, {"player_name": "M. Porter Jr.", "line": 19.5},
              {"player_name": "P. George", "line": 19.5}, {"player_name": "D. White", "line": 14.5}]
PHONE_TILES = [{"player_name": "M. PORTER JR.", "stat": "Points", "line": 19.5},
               {"player_name": "P. GEORGE", "stat": "Rebounds", "line": 3.5},
               {"player_name": "J. TATUM", "stat": "Points", "line": 24.5},
               {"player_name": "P. GEORGE", "stat": "Points", "line": 19.5}]


def name_mark(i: int) -> tuple:
    return (250, 40 + 8 * i, 200)


def line_mark(i: int) -> tuple:
    return (200, 40 + 8 * i, 250)


def render_phone(layout: str = "list", items=None, tab: str = "Points", first_y: float = 595,
                 header: bool = True, tabs_y: float = 430, height: int = PHONE_H) -> bytes:
    """layout 'list' (a stat tab: rows with two line buttons) or 'tiles' (Popular). first_y:
    units from the top to the first item's top (above PHONE_CONTENT_TOP it is cut by the
    sticky header); height: px, a smaller one cuts the last items at the bottom. header=False
    leaves the team header blank; tabs_y moves the tab pills (and the content below them)."""
    items = items if items is not None else (PHONE_ROWS if layout == "list" else PHONE_TILES)
    u = PHONE_W / 721
    shift = tabs_y - 430
    img = Image.new("RGB", (PHONE_W, height), PHONE_PAGE)
    d = ImageDraw.Draw(img)
    big, mid, small = _font(int(30 * u)), _font(int(22 * u)), _font(int(17 * u))

    def box(x0, y0, x1, y1):
        return [int(x0 * u), int(y0 * u), int(x1 * u), int(y1 * u)]

    for i, c in enumerate(items):
        if layout == "list":
            y = first_y + shift + i * ROW_PITCH
            d.ellipse(box(28, y + 6, 100, y + 78), fill=(60, 140, 70))
            d.text((int(115 * u), int((y + 14) * u)), c["player_name"], font=mid, fill=(245, 245, 245))
            d.text((int(115 * u), int((y + 46) * u)), "20.0 avg", font=small, fill=(170, 170, 180))
            d.rectangle(box(300, y + 18, 318, y + 36), fill=name_mark(i))
            for j, arrow in enumerate(("^", "v")):
                x = 368 + j * 169
                d.rounded_rectangle(box(x, y, x + 155, y + ROW_H), radius=int(14 * u), fill=PHONE_PILL)
                d.text((int((x + 40) * u), int((y + 12) * u)), f"{arrow} {c['line']:g}", font=mid, fill=(245, 245, 245))
                d.text((int((x + 50) * u), int((y + 48) * u)), "1.62x", font=small, fill=(200, 200, 205))
            d.rectangle(box(378, y + 14, 394, y + 30), fill=line_mark(i))
        else:
            x, y = 28 + (i % 2) * 340, first_y + shift + (i // 2) * TILE_PITCH
            d.rounded_rectangle(box(x, y, x + 320, y + TILE_H), radius=int(24 * u), fill=(12, 16, 22))
            d.rectangle(box(x, y + 20, x + 320, y + 0.465 * TILE_H), fill=(20, 160, 70))
            d.rounded_rectangle(box(x, y, x + 320, y + 40), radius=int(24 * u), fill=(20, 160, 70))
            d.text((int((x + 60) * u), int((y + 0.04 * TILE_H) * u)), c["player_name"], font=big, fill=(255, 255, 255))
            d.rectangle(box(x + 280, y + 0.05 * TILE_H, x + 298, y + 0.09 * TILE_H), fill=name_mark(i))
            d.text((int((x + 120) * u), int((y + 0.56 * TILE_H) * u)), f"{c['line']:g}", font=big, fill=(255, 255, 255))
            d.text((int((x + 110) * u), int((y + 0.67 * TILE_H) * u)), c["stat"], font=small, fill=(200, 200, 205))
            d.rectangle(box(x + 230, y + 0.58 * TILE_H, x + 248, y + 0.62 * TILE_H), fill=line_mark(i))
            for j, side in enumerate(("Higher", "Lower")):
                bx = x + 22 + j * 144
                d.rounded_rectangle(box(bx, y + 0.79 * TILE_H, bx + 130, y + 0.95 * TILE_H), radius=int(12 * u),
                                    fill=(30, 34, 42))
                d.text((int((bx + 30) * u), int((y + 0.81 * TILE_H) * u)), side, font=small, fill=(245, 245, 245))
    # the sticky header over whatever scrolled under it
    d.rectangle(box(0, 0, 721, PHONE_CONTENT_TOP + shift), fill=PHONE_PAGE)
    d.rounded_rectangle(box(185, 22, 530, 92), radius=int(35 * u), fill=(0, 0, 0))
    if header:
        d.text((int(46 * u), int(240 * u)), "BKN", font=big, fill=(160, 160, 165))
        d.text((int(595 * u), int(240 * u)), "BOS", font=big, fill=(40, 170, 80))
        d.text((int(315 * u), int(245 * u)), "6:30pm", font=mid, fill=(245, 245, 245))
        d.rectangle(box(350, 290, 370, 310), fill=PHONE_HEADER_MARK)
        d.text((int(185 * u), int(350 * u)), "Game     Players     Entries", font=mid, fill=(200, 200, 205))
    x = 28
    for label, wd in zip(PHONE_TABS, (167, 116, 157, 125, 125)):
        d.rounded_rectangle(box(x, tabs_y, x + wd, tabs_y + 66), radius=int(33 * u),
                            fill=PHONE_PILL_ON if label == tab else PHONE_PILL)
        d.text((int((x + 20) * u), int((tabs_y + 18) * u)), label, font=mid,
               fill=(10, 10, 10) if label == tab else (220, 220, 225))
        x += wd + 14
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
