#!/usr/bin/env python3
"""Generate dendrite (DLA) texture tiles for the Coral Reef objects.

Diffusion-Limited Aggregation is the standard model for the manganese
dendrites in Dendrites01.jpg: a seed sticks to a substrate, then random
walkers wander in and freeze on contact. Because a walker is far more likely
to hit an exposed tip than to reach a sheltered inner crevice, growth
concentrates at the tips -- which is exactly what produces the fern/frost
branching.

Two knobs matter for the look:
  STICK      probability a walker freezes on contact. <1 lets walkers slide
             along the surface first, filling crevices, giving THICKER, more
             fern-like arms (the Wikipedia photo). 1.0 gives spindly, open,
             lightning-like growth.
  BIAS       downward/directional drift on the walk. The photo's dendrites
             grew along a plane with a clear "up" -- a slight bias makes the
             arms lean consistently instead of growing as a round blob.

The output is a TILEABLE grayscale mask (walkers wrap around the edges and
the seed row wraps too), so it can be applied across an object of any size
without seams.
"""
import os
import random
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))


def grow_dla(size=128, coverage=0.18, stick=0.55, bias=0.35, seed=7,
             seed_mode="bottom", max_steps=4_000_000):
    """Grow a DLA cluster on a `size`x`size` torus. Returns a set of (x,y).

    coverage  : fraction of cells to fill before stopping
    stick     : 0..1 chance to freeze on contact (lower = denser/fernier)
    bias      : 0..1 downward drift (walkers pushed toward the seed row)
    seed_mode : "bottom" = seed the bottom row (upward fern growth, like the
                photo); "center" = single central seed (radial snowflake)
    """
    rnd = random.Random(seed)
    occupied = set()

    # Seeds must be SPARSE: seeding a solid row means a descending walker
    # freezes on first contact and you get a flat crust instead of branches.
    # A few isolated seeds let tips compete and screen each other, which is
    # what actually produces the fern structure.
    if seed_mode == "bottom":
        n_seeds = max(1, size // 32)
        for i in range(n_seeds):
            occupied.add(((i * size // n_seeds + size // (2 * n_seeds)) % size,
                          size - 1))
    else:
        occupied.add((size // 2, size // 2))

    target = int(size * size * coverage)
    steps = 0

    # 8-neighbourhood contact test
    NB = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

    # track the growth front so walkers launch just above it: launching at a
    # fixed y=0 on a torus would let them wrap directly onto the seeds from
    # below (no branching, and very slow).
    top_y = min(y for _, y in occupied)
    LAUNCH_GAP = max(4, size // 16)

    while len(occupied) < target and steps < max_steps:
        # release a walker just above the current front
        if seed_mode == "bottom":
            wx = rnd.randrange(size)
            wy = max(0, top_y - LAUNCH_GAP)
        else:
            wx, wy = rnd.randrange(size), rnd.randrange(size)
            if (wx, wy) in occupied:
                continue

        for _ in range(size * 12):          # walker lifetime
            steps += 1
            if steps >= max_steps:
                break

            # contact?
            touching = False
            for dx, dy in NB:
                if ((wx + dx) % size, (wy + dy) % size) in occupied:
                    touching = True
                    break
            if touching and rnd.random() < stick:
                occupied.add((wx, wy))
                if wy < top_y:
                    top_y = wy
                break

            # random step, with optional drift toward the seed
            r = rnd.random()
            if seed_mode == "bottom" and r < bias:
                wy += 1
            else:
                dx, dy = NB[rnd.randrange(8)]
                wx += dx
                wy += dy
            wx %= size
            # wrap horizontally (keeps the tile seamless) but do NOT wrap
            # vertically -- a walker that escapes upward is abandoned rather
            # than reappearing under the seeds.
            if seed_mode == "bottom":
                if wy < 0 or wy >= size:
                    break
                if wy < top_y - 2 * LAUNCH_GAP:
                    break
            else:
                wy %= size

    return occupied


def dla_mask(size=128, blur=0.6, **kw):
    """Grow a cluster and return it as a tileable 'L' mask (255 = dendrite)."""
    occ = grow_dla(size=size, **kw)
    im = Image.new("L", (size, size), 0)
    p = im.load()
    for (x, y) in occ:
        p[x, y] = 255
    if blur:
        # wrap-aware blur so the mask stays tileable
        pad = 8
        big = Image.new("L", (size + 2 * pad, size + 2 * pad))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                big.paste(im, (pad + dx * size, pad + dy * size))
        big = big.filter(ImageFilter.GaussianBlur(blur))
        im = big.crop((pad, pad, pad + size, pad + size))
    return im


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dla.png"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 128
    m = dla_mask(size=size)
    m.save(out)
    print(f"wrote {out} ({size}x{size})")
