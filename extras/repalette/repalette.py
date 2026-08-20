#!/usr/bin/env python3

from PIL import Image
import numpy as np
import sys

if len(sys.argv) != 4:
    print("Usage:")
    print("  python repalette.py palette.bmp target.bmp output.bmp")
    sys.exit(1)

palette_path, target_path, output_path = sys.argv[1:]

# ------------------------------------------------------------
# Load palette image
# ------------------------------------------------------------

palette_img = Image.open(palette_path)

if palette_img.mode != "P":
    raise RuntimeError("Palette image must be an indexed (mode 'P') BMP.")

palette = palette_img.getpalette()[:768]  # first 256 RGB entries
palette = np.array(palette, dtype=np.uint8).reshape((256, 3))

# ------------------------------------------------------------
# Load target image as RGB
# ------------------------------------------------------------

target = Image.open(target_path).convert("RGB")
pixels = np.array(target, dtype=np.uint8)

h, w, _ = pixels.shape
pixels_flat = pixels.reshape((-1, 3)).astype(np.int16)

palette16 = palette.astype(np.int16)

# ------------------------------------------------------------
# Compute nearest palette colour
# ------------------------------------------------------------

# Squared Euclidean distance
dist = ((pixels_flat[:, None, :] - palette16[None, :, :]) ** 2).sum(axis=2)

indices = np.argmin(dist, axis=1).astype(np.uint8)

# ------------------------------------------------------------
# Create indexed output
# ------------------------------------------------------------

out = Image.fromarray(indices.reshape((h, w)), mode="P")
out.putpalette(palette.flatten().tolist())

out.save(output_path)

print("Saved", output_path)