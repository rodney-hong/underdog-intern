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
