"""FINN app icons without PIL: ink square, white 'F', blue-pen dot (the network motif).
Usage: python backend/scripts/make_icons.py frontend
Glyph kept inside the central 60% so the icon is also safe as a maskable icon."""
import struct, sys, zlib
from pathlib import Path

INK, WHITE, PEN = (0x18, 0x20, 0x2E), (0xFF, 0xFF, 0xFF), (0x23, 0x40, 0xE8)

def shapes(n):
    u = n / 100.0  # draw on a 100-unit grid
    r = lambda x0, y0, x1, y1: (int(x0 * u), int(y0 * u), int(x1 * u), int(y1 * u))
    return [(r(32, 26, 44, 74), WHITE),   # F stem
            (r(32, 26, 68, 37), WHITE),   # top bar
            (r(32, 46, 60, 56), WHITE)], (int(66 * u), int(66 * u), int(8 * u))  # mid bar; dot (cx, cy, radius)

def png(n):
    rects, (cx, cy, rad) = shapes(n)
    rows = []
    for y in range(n):
        row = bytearray(b"\x00")
        for x in range(n):
            c = INK
            for (x0, y0, x1, y1), col in rects:
                if x0 <= x < x1 and y0 <= y < y1:
                    c = col
            if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                c = PEN
            row += bytes(c)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")

out = Path(sys.argv[1])
for n in (192, 512):
    (out / f"icon-{n}.png").write_bytes(png(n))
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#18202E"/>'
       '<path fill="#fff" d="M32 26h36v11H44v9h16v10H44v18H32z"/><circle cx="66" cy="66" r="8" fill="#2340E8"/></svg>\n')
(out / "icon.svg").write_text(SVG, encoding="utf-8")
print("ok")
