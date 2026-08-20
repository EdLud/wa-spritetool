#!/usr/bin/env python3

import sys
import struct
from PIL import Image


def write_os2_bmp(img: Image.Image, output_path: str) -> None:
    if img.mode != "P":
        raise ValueError("Image must be mode 'P'.")

    width, height = img.size
    palette = img.getpalette()

    row_stride = (width + 3) & ~3
    pixel_bytes = row_stride * height

    palette_bytes = 256 * 3
    offset = 14 + 12 + palette_bytes
    file_size = offset + pixel_bytes

    with open(output_path, "wb") as f:
        # BITMAPFILEHEADER
        f.write(struct.pack("<2sIHHI",
            b"BM", file_size, 0, 0, offset))

        # BITMAPCOREHEADER
        f.write(struct.pack("<IHHHH",
            12, width, height, 1, 8))

        # Palette (RGBTRIPLE = B,G,R)
        for i in range(256):
            r = palette[i * 3]
            g = palette[i * 3 + 1]
            b = palette[i * 3 + 2]
            f.write(bytes((b, g, r)))

        # Pixels
        pixels = img.load()
        pad = b"\0" * (row_stride - width)

        for y in range(height - 1, -1, -1):
            f.write(bytes(pixels[x, y] for x in range(width)))
            f.write(pad)


def main():
    if len(sys.argv) != 3:
        print("Usage:")
        print("  python3 convert_to_bmp.py input.png output.img.bmp")
        sys.exit(1)

    src, dst = sys.argv[1:]

    img = Image.open(src)

    if img.mode != "P":
        raise RuntimeError(
            "Input image must already be indexed (mode='P'). "
            "Run your palette conversion first."
        )

    write_os2_bmp(img, dst)


if __name__ == "__main__":
    main()