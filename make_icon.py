#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a terminal-green ">_" app icon as PNG, .ico (Windows) and an
.iconset folder (for `iconutil` -> .icns on macOS). Pure stdlib (zlib)."""

from __future__ import annotations

import os
import struct
import zlib

# palette
BG = (8, 20, 12)        # near-black green
BORDER = (43, 255, 106)  # phosphor green
FG = (90, 247, 142)      # prompt green

MASTER = 1024


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def render_master(N=MASTER) -> bytearray:
    buf = bytearray(N * N * 4)  # RGBA, transparent

    def setpx(x, y, c):
        i = (y * N + x) * 4
        buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = c[0], c[1], c[2], 255

    m = 70
    x0, y0, x1, y1 = m, m, N - m, N - m
    rad = 180
    bt = 26  # border thickness
    # chevron ">" points + cursor block ("_") -> ">_"
    cx1, cy1 = 320, 360
    cx2, cy2 = 520, 512
    cx3, cy3 = 320, 664
    th = 46  # chevron thickness
    ux0, uy0, ux1, uy1 = 560, 600, 730, 656  # underscore/cursor block

    for y in range(N):
        for x in range(N):
            # rounded-rect membership (distance to inner core)
            qx = min(max(x, x0 + rad), x1 - rad)
            qy = min(max(y, y0 + rad), y1 - rad)
            d = ((x - qx) ** 2 + (y - qy) ** 2) ** 0.5
            inside = (x0 <= x <= x1 and y0 <= y <= y1) and d <= rad
            if not inside:
                continue
            # border ring
            if d >= rad - bt or x <= x0 + bt or x >= x1 - bt \
                    or y <= y0 + bt or y >= y1 - bt:
                if d > rad - bt or (x0 + bt < x < x1 - bt
                                    and y0 + bt < y < y1 - bt):
                    # corner ring handled by d; straight edges by the elifs
                    pass
                setpx(x, y, BORDER)
                continue
            # glyph ">_"
            d1 = _dist_seg(x, y, cx1, cy1, cx2, cy2)
            d2 = _dist_seg(x, y, cx2, cy2, cx3, cy3)
            if d1 <= th or d2 <= th or (ux0 <= x <= ux1 and uy0 <= y <= uy1):
                setpx(x, y, FG)
                continue
            setpx(x, y, BG)
    return buf


def downsample(src: bytearray, N: int, t: int) -> bytearray:
    f = N // t
    out = bytearray(t * t * 4)
    for oy in range(t):
        for ox in range(t):
            r = g = b = a = 0
            for yy in range(f):
                base = ((oy * f + yy) * N + ox * f) * 4
                for xx in range(f):
                    i = base + xx * 4
                    r += src[i]; g += src[i + 1]; b += src[i + 2]; a += src[i + 3]
            n = f * f
            j = (oy * t + ox) * 4
            out[j] = r // n; out[j + 1] = g // n
            out[j + 2] = b // n; out[j + 3] = a // n
    return out


def png_bytes(w: int, h: int, buf: bytearray) -> bytes:
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += buf[y * w * 4:(y + 1) * w * 4]
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
            + chunk(b'IEND', b''))


def write_ico(path: str, pngs: list[tuple[int, bytes]]) -> None:
    n = len(pngs)
    out = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
    data = b''
    for size, png in pngs:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    with open(path, 'wb') as f:
        f.write(out + data)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    print("rendering master 1024…")
    master = render_master(MASTER)

    sizes = {}
    for t in (1024, 512, 256, 128, 64, 32, 16):
        sizes[t] = master if t == MASTER else downsample(master, MASTER, t)
        print(f"  size {t} ok")

    # Linux / general PNG
    with open(os.path.join(here, 'app_icon.png'), 'wb') as f:
        f.write(png_bytes(512, 512, sizes[512]))

    # Windows .ico (PNG-compressed entries)
    write_ico(os.path.join(here, 'app_icon.ico'),
              [(s, png_bytes(s, s, sizes[s])) for s in (16, 32, 64, 128, 256)])

    # macOS .iconset (iconutil turns this into .icns)
    iconset = os.path.join(here, 'app_icon.iconset')
    os.makedirs(iconset, exist_ok=True)
    icns_map = [
        ('icon_16x16', 16), ('icon_16x16@2x', 32),
        ('icon_32x32', 32), ('icon_32x32@2x', 64),
        ('icon_128x128', 128), ('icon_128x128@2x', 256),
        ('icon_256x256', 256), ('icon_256x256@2x', 512),
        ('icon_512x512', 512), ('icon_512x512@2x', 1024),
    ]
    for name, s in icns_map:
        with open(os.path.join(iconset, name + '.png'), 'wb') as f:
            f.write(png_bytes(s, s, sizes[s]))
    print("done: app_icon.png, app_icon.ico, app_icon.iconset/")


if __name__ == '__main__':
    main()
