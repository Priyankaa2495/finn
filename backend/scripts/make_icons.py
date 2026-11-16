"""Generate FINN's app icons with the standard library only (no Pillow).

    python scripts/make_icons.py     # writes into frontend/

Mark: a bold "F" with a small rising tick, white on the app's accent green. Shapes are
rectangles rasterised with 4x4 supersampling for clean edges. Outputs:
  icon-192.png, icon-512.png           purpose "any"  (rounded square, transparent corners)
  icon-maskable-512.png                purpose "maskable" (full bleed, glyph inside the safe zone)
  apple-touch-icon.png (180)           full bleed; iOS applies its own mask
  favicon.ico (16/32/48 PNG-in-ICO), favicon.svg
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "frontend"
BG = (31, 95, 74)          # --accent (light theme)
FG = (255, 255, 255)
TICK = (108, 196, 161)     # --accent (dark theme), used for the tick

# Glyph in unit coordinates (x0, y0, x1, y1). Kept inside the maskable safe zone (r = 0.4).
F_RECTS = [(0.31, 0.24, 0.43, 0.76), (0.31, 0.24, 0.70, 0.35), (0.31, 0.46, 0.62, 0.56)]
TICK_RECTS = [(0.58, 0.64, 0.70, 0.76)]


def _in_round_rect(x: float, y: float, r: float) -> bool:
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int, radius: float | None) -> bytes:
    """RGBA rows. radius=None -> full-bleed square background."""
    ss = 4
    rows = bytearray()
    for py in range(size):
        rows.append(0)                                   # PNG filter: none
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) / size
                    y = (py + (sy + 0.5) / ss) / size
                    if radius is not None and not _in_round_rect(x, y, radius):
                        continue
                    col = BG
                    if any(a <= x <= c and b <= y <= d for a, b, c, d in F_RECTS):
                        col = FG
                    elif any(a <= x <= c and b <= y <= d for a, b, c, d in TICK_RECTS):
                        col = TICK
                    acc[0] += col[0]; acc[1] += col[1]; acc[2] += col[2]; acc[3] += 255
            n = ss * ss
            alpha = acc[3] / n
            if alpha:
                k = 255 / acc[3]                          # un-premultiply
                rows += bytes((round(acc[0] * k), round(acc[1] * k), round(acc[2] * k), round(alpha)))
            else:
                rows += b"\0\0\0\0"
    return bytes(rows)


def png(size: int, radius: float | None) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(render(size, radius), 9)) + chunk(b"IEND", b""))


def ico(sizes: list[int]) -> bytes:
    images = [png(s, 0.22) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return header + entries + blobs


SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="22" fill="rgb{BG}"/>
<path fill="#fff" d="M31 24h39v11H43v11h19v10H43v20H31z"/>
<rect x="58" y="64" width="12" height="12" fill="rgb{TICK}"/>
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon-192.png").write_bytes(png(192, 0.22))
    (OUT / "icon-512.png").write_bytes(png(512, 0.22))
    (OUT / "icon-maskable-512.png").write_bytes(png(512, None))
    touch = png(180, None)
    (OUT / "apple-touch-icon.png").write_bytes(touch)
    (OUT / "apple-touch-icon-precomposed.png").write_bytes(touch)   # older iOS probes this path
    (OUT / "favicon.ico").write_bytes(ico([16, 32, 48]))
    (OUT / "favicon.svg").write_text(SVG, encoding="utf-8")
    for p in sorted(OUT.glob("*icon*")) + [OUT / "favicon.svg"]:
        print(f"{p.name:34} {p.stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()
