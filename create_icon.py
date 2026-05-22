#!/usr/bin/env python3
"""
Run this once to generate interpro_icon.ico
python create_icon.py
"""
import struct, zlib, os

def make_png(size, bg=(9,13,19), accent=(0,200,255)):
    """Generate a minimal InterPro logo PNG at given size."""
    import math
    w = h = size
    pixels = []
    cx = cy = size / 2
    r_outer = size / 2 - 1
    r_inner = size / 2 - size * 0.04

    num_bars = 9
    bar_w = max(1, int(size * 0.05))
    max_bar_h = size * 0.55

    for y in range(h):
        row = []
        for x in range(w):
            dx = x - cx; dy = y - cy
            dist = math.sqrt(dx*dx + dy*dy)

            if dist > r_outer:
                row += [0, 0, 0, 0]
                continue

            # Background
            r, g, b = bg

            # Draw waveform bars
            bar_heights = [0.45, 0.60, 0.75, 0.88, 1.0, 0.88, 0.75, 0.60, 0.45]
            total_w = num_bars * bar_w + (num_bars - 1) * bar_w
            start_x = cx - total_w / 2

            in_bar = False
            for i, bh_ratio in enumerate(bar_heights):
                bx = start_x + i * (bar_w * 2)
                bar_h = max_bar_h * bh_ratio
                by_top = cy - bar_h / 2
                by_bot = cy + bar_h / 2
                if bx <= x <= bx + bar_w and by_top <= y <= by_bot:
                    alpha = int(255 * (0.5 + 0.5 * bh_ratio))
                    row += [accent[0], accent[1], accent[2], alpha]
                    in_bar = True
                    break

            if not in_bar:
                # Subtle ring
                if abs(dist - r_outer * 0.78) < 0.8 or abs(dist - r_outer * 0.98) < 1.0:
                    row += [accent[0], accent[1], accent[2], 30]
                else:
                    row += [r, g, b, 255]

        pixels.append(bytes(row))

    def chunk(name, data):
        c = zlib.crc32(name + data) & 0xffffffff
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', c)

    raw = b''
    for row in pixels:
        raw += b'\x00' + row

    compressed = zlib.compress(raw, 9)
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', ihdr)
    png += chunk(b'IDAT', compressed)
    png += chunk(b'IEND', b'')
    return png

# Generate PNGs at multiple sizes for ICO
sizes = [256, 128, 64, 48, 32, 16]
pngs = [(s, make_png(s)) for s in sizes]

# Build ICO file
num = len(pngs)
# ICO header: 6 bytes
# Directory entries: 16 bytes each
# Then PNG data

header = struct.pack('<HHH', 0, 1, num)  # reserved, type=1(icon), count

offset = 6 + num * 16
entries = b''
data = b''
for size, png_data in pngs:
    sz = size if size < 256 else 0
    entries += struct.pack('<BBBBHHII',
        sz, sz,     # width, height
        0, 0,       # color count, reserved
        1, 32,      # planes, bit count
        len(png_data),
        offset
    )
    offset += len(png_data)
    data += png_data

ico = header + entries + data

with open('interpro_icon.ico', 'wb') as f:
    f.write(ico)

print(f"Created interpro_icon.ico ({len(ico)} bytes)")
print("Sizes:", [s for s, _ in pngs])
