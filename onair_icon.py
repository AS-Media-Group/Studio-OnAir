"""The On-Air Bridge icon: a dark tile with a glowing broadcast dot and signal arcs.

Drawn in code (Pillow only) so the window icon, the tray icon and the .exe icon always match.
    python onair_icon.py            writes onair.ico (used by the build) and prints nothing else
The dot changes colour with the state: red = ON AIR, green = signs off, grey = stopped.
"""
from __future__ import annotations

import io
import struct
import sys

from PIL import Image, ImageDraw

RED, GREEN, GREY = "#e53935", "#43a047", "#8a8a93"
TILE = "#17171d"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _rgba(hex_colour: str, alpha: int = 255) -> tuple:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def draw_logo(size: int, colour: str = RED) -> Image.Image:
    """A crisp RGBA icon at `size` px. Drawn 8x larger and shrunk so the edges are smooth."""
    k = 8
    big = size * k
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=_rgba(TILE))

    cx = cy = big / 2
    small = size <= 24  # at tray sizes: a bigger dot and one chunky pair of arcs
    r_dot = big * (0.27 if small else 0.2)
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i, a in ((1.9, 40), (1.55, 70), (1.25, 110)):
        r = r_dot * i
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_rgba(colour, a))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot), fill=_rgba(colour))
    if not small:  # a little highlight so the dot looks like a lit lamp
        hr = r_dot * 0.42
        hx, hy = cx - r_dot * 0.34, cy - r_dot * 0.38
        shine = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(shine).ellipse((hx - hr, hy - hr, hx + hr, hy + hr), fill=(255, 255, 255, 80))
        img = Image.alpha_composite(img, shine)  # blend, don't overwrite
        d = ImageDraw.Draw(img)

    arc_col = _rgba("#f2f2f5", 235 if colour != GREY else 120)
    radii = ((0.40, 0.085),) if small else ((0.31, 0.045), (0.41, 0.045))
    for rad, width in radii:
        r = big * rad
        w = max(1, int(big * width))
        box = (cx - r, cy - r, cx + r, cy + r)
        d.arc(box, -38, 38, fill=arc_col, width=w)
        d.arc(box, 142, 218, fill=arc_col, width=w)
    return img.resize((size, size), Image.LANCZOS)


def png_bytes(size: int, colour: str = RED) -> bytes:
    buf = io.BytesIO()
    draw_logo(size, colour).save(buf, "PNG")
    return buf.getvalue()


def write_ico(path: str, colour: str = RED, sizes=ICO_SIZES) -> None:
    """A multi-size .ico, each size drawn for itself (Pillow would only shrink the biggest one)."""
    blobs = [(s, png_bytes(s, colour)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    table, data = b"", b""
    for s, blob in blobs:
        table += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    with open(path, "wb") as f:
        f.write(head + table + data)


if __name__ == "__main__":
    write_ico(sys.argv[1] if len(sys.argv) > 1 else "onair.ico")
