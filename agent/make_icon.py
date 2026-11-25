"""Draws the WorkLog clock icon (purple circle, white clock hands) as worklog.ico.
Pure Python, no extra libraries. Run once before building the .exe."""
import math
import os
import struct
import zlib


def draw(size):
    px = bytearray(size * size * 4)
    c = (size - 1) / 2
    r_out, ring = size * 0.47, size * 0.36
    lw = max(1.2, size * 0.065)

    def seg_dist(x, y, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - ax - t * dx, y - ay - t * dy)

    hands = [(c, c, c, c - size * 0.24), (c, c, c + size * 0.17, c + size * 0.08)]
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c)
            a = max(0.0, min(1.0, r_out - d + 0.5))          # anti-aliased disc
            if a == 0:
                continue
            white = max(0.0, min(1.0, lw / 2 - abs(d - ring) + 0.5))  # clock ring
            for h in hands:
                white = max(white, min(1.0, max(0.0, lw / 2 - seg_dist(x, y, *h) + 0.5)))
            r = int(91 + (255 - 91) * white)
            g = int(79 + (255 - 79) * white)
            b = int(224 + (255 - 224) * white)
            i = (y * size + x) * 4
            px[i:i + 4] = bytes((r, g, b, int(255 * a)))
    return px


def png(size, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


sizes = [16, 24, 32, 48, 64, 128, 256]
images = [png(s, draw(s)) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for s, data in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worklog.ico")
with open(out, "wb") as f:
    f.write(header + entries + b"".join(images))
print("Saved", out)
