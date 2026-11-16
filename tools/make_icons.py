"""Draw the app icon as PNG files with nothing but the standard library.

    python tools/make_icons.py

Writes static/icons/icon-192.png and static/icons/icon-512.png: a green
rounded square with a white location pin, the same mark as icon.svg.
"""

import math
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "static", "icons")

GREEN = (15, 107, 79)
WHITE = (255, 255, 255)


def _png_chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_png(path, width, height, pixel):
    """pixel(x, y) -> (r, g, b, a) for integer pixel coordinates."""
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            row.extend(pixel(x, y))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(raw, 9))
           + _png_chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(png)


def _in_rounded_square(u, v, radius=0.22):
    """u, v in 0..1. True inside a rounded square with the given corner radius."""
    cx = min(max(u, radius), 1 - radius)
    cy = min(max(v, radius), 1 - radius)
    return (u - cx) ** 2 + (v - cy) ** 2 <= radius ** 2


def _in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _in_pin(u, v):
    """The white pin: a circle at (0.5, 0.41) joined to a point at (0.5, 0.84)."""
    head = (u - 0.5) ** 2 + (v - 0.41) ** 2 <= 0.25 ** 2
    tail = _in_triangle(u, v, (0.31, 0.55), (0.69, 0.55), (0.5, 0.84))
    hole = (u - 0.5) ** 2 + (v - 0.41) ** 2 <= 0.09 ** 2
    return (head or tail) and not hole


def icon_pixel(size, samples=3):
    def pixel(x, y):
        bg = fg = 0
        total = samples * samples
        for sy in range(samples):
            for sx in range(samples):
                u = (x + (sx + 0.5) / samples) / size
                v = (y + (sy + 0.5) / samples) / size
                if _in_rounded_square(u, v):
                    bg += 1
                    if _in_pin(u, v):
                        fg += 1
        if bg == 0:
            return (0, 0, 0, 0)
        mix = fg / bg
        r = round(GREEN[0] + (WHITE[0] - GREEN[0]) * mix)
        g = round(GREEN[1] + (WHITE[1] - GREEN[1]) * mix)
        b = round(GREEN[2] + (WHITE[2] - GREEN[2]) * mix)
        return (r, g, b, round(255 * bg / total))
    return pixel


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (192, 512):
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        write_png(path, size, size, icon_pixel(size))
        print("wrote", os.path.relpath(path, ROOT), os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
