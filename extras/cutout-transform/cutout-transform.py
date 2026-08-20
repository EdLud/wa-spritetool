#!/usr/bin/env python3
"""Split cutouts.png into individual object PNGs.

cutouts.png holds ~20 black tree/branch silhouettes on a transparent
background, each isolated from its neighbors by fully-transparent space.
We label connected components of non-transparent pixels (8-connected, via
BFS over a row-run representation for speed) and crop each one out into its
own PNG under /cutouts, preserving transparency.
"""
import os
import sys
from collections import deque
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "cutouts.png")
OUT = os.path.join(HERE, "cutouts")

ALPHA_THRESHOLD = 10   # pixels with alpha above this are foreground
PAD = 4                # transparent padding kept around each crop
MIN_PIXELS = 500      # discard dust specks/noise smaller than this


def find_components(alpha):
    """8-connected components of alpha > ALPHA_THRESHOLD, via BFS on row runs."""
    w, h = alpha.size
    px = alpha.load()

    # Precompute per-row runs of foreground pixels: row -> list of (x0, x1) exclusive
    row_runs = []
    for y in range(h):
        runs = []
        x = 0
        while x < w:
            if px[x, y] > ALPHA_THRESHOLD:
                x0 = x
                while x < w and px[x, y] > ALPHA_THRESHOLD:
                    x += 1
                runs.append([x0, x, False])  # start, end, visited
            else:
                x += 1
        row_runs.append(runs)

    components = []
    for y in range(h):
        for run in row_runs[y]:
            if run[2]:
                continue
            # BFS over runs, 8-connected across adjacent rows
            queue = deque([(y, run)])
            run[2] = True
            min_x, max_x, min_y, max_y = run[0], run[1], y, y
            pixel_count = 0
            while queue:
                cy, crun = queue.popleft()
                pixel_count += crun[1] - crun[0]
                for ny in (cy - 1, cy + 1):
                    if ny < 0 or ny >= h:
                        continue
                    for orun in row_runs[ny]:
                        if orun[2]:
                            continue
                        # 8-connected overlap: touch if ranges overlap when
                        # expanded by 1 on each side
                        if orun[0] - 1 < crun[1] and crun[0] - 1 < orun[1]:
                            orun[2] = True
                            min_x = min(min_x, orun[0])
                            max_x = max(max_x, orun[1])
                            min_y = min(min_y, ny)
                            max_y = max(max_y, ny)
                            queue.append((ny, orun))
            components.append((min_x, min_y, max_x, max_y, pixel_count))
    return components


def main():
    img = Image.open(SRC).convert("RGBA")
    w, h = img.size
    alpha = img.split()[-1]

    print(f"Loaded {SRC} ({w}x{h})")
    components = find_components(alpha)
    components = [c for c in components if c[4] >= MIN_PIXELS]
    print(f"Found {len(components)} objects")

    os.makedirs(OUT, exist_ok=True)
    # remove any stale pngs from a previous run
    for f in os.listdir(OUT):
        if f.lower().endswith(".png"):
            os.remove(os.path.join(OUT, f))

    # order roughly left-to-right, top-to-bottom (row band then x)
    components.sort(key=lambda c: (c[1] // 200, c[0]))

    for i, (x0, y0, x1, y1, count) in enumerate(components, start=1):
        cx0 = max(0, x0 - PAD)
        cy0 = max(0, y0 - PAD)
        cx1 = min(w, x1 + PAD)
        cy1 = min(h, y1 + PAD)
        crop = img.crop((cx0, cy0, cx1, cy1))
        name = f"cutout_{i:02d}.png"
        crop.save(os.path.join(OUT, name), "PNG", optimize=True)
        print(f"  {name}: {cx1-cx0}x{cy1-cy0} px, {count} fg pixels")

    print(f"\nSaved {len(components)} PNGs to {OUT}")


if __name__ == "__main__":
    main()
