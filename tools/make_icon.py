"""Generate the AppImage/desktop icon without any image libraries."""

import struct
import zlib
from pathlib import Path

SIZE = 256
RADIUS = 48
CENTER = (128, 128)


def inside_rounded(x: int, y: int) -> bool:
    cx = min(max(x, RADIUS), SIZE - 1 - RADIUS)
    cy = min(max(y, RADIUS), SIZE - 1 - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2


def in_triangle(px: int, py: int, a, b, c) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def pixel(x: int, y: int):
    if not inside_rounded(x, y):
        return (0, 0, 0, 0)
    t = y / SIZE
    bg = (
        int(0x17 + (0x0B - 0x17) * t),
        int(0x1B + (0x0D - 0x1B) * t),
        int(0x24 + (0x12 - 0x24) * t),
        255,
    )
    dist = ((x - CENTER[0]) ** 2 + (y - CENTER[1]) ** 2) ** 0.5
    if 74 <= dist <= 86:
        return (0xE8, 0xA3, 0x3D, 255)
    if in_triangle(x, y, (107, 88), (107, 168), (178, 128)):
        return (0xF6, 0xC8, 0x77, 255)
    return bg


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def main() -> None:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows.extend(pixel(x, y))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    target = Path(__file__).resolve().parents[1] / "icon.png"
    target.write_bytes(png)
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
