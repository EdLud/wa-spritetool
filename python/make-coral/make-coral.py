#!/usr/bin/env python3
"""Coral texture prototypes for the Coral Reef objects. Pure PIL, no numpy.

Two generators, both chosen because they model how the real structure forms
rather than trying to imitate a photo:

  sea_fan()    -- anastomosing DLA. Plain DLA (the dendrite build) grows a
                  TREE: branches only ever split, never rejoin, which is why
                  it reads as frost/fern. Real gorgonian sea fans ANASTOMOSE:
                  neighbouring branches fuse into a lacy NET. We get that by
                  letting a walker stick to more than one existing branch,
                  plus a strong lateral bias so growth spreads sideways into
                  a fan rather than shooting straight up.

  brain_coral() -- Gray-Scott reaction-diffusion. This is the standard model
                  for meandering labyrinth patterns (Turing patterns), which
                  is literally what brain coral grooves are. Two chemicals
                  diffuse at different rates; the feed/kill rates decide
                  whether you get spots, stripes, or the maze we want.

Both return tileable 'L' masks so they can be applied like the dendrite mask.
"""
import math
import os
import random
from PIL import Image, ImageFilter, ImageChops


# ---------------------------------------------------------------------------
# 1. sea fan: anastomosing DLA
# ---------------------------------------------------------------------------
def sea_fan(size=200, coverage=0.22, seed=11, lateral=0.55, fuse=2,
            n_seeds=3, max_steps=6_000_000):
    """Grow a lacy, net-like fan.

    lateral : 0..1 sideways drift. High values spread the fan wide instead of
              growing a tall spindly tree.
    fuse    : how many separate existing neighbours a walker may touch and
              still stick. 1 = classic DLA (pure tree, no loops). >=2 lets
              branches REJOIN, which is what creates the net/mesh look.
    """
    rnd = random.Random(seed)
    occupied = set()
    for i in range(n_seeds):
        occupied.add(((i + 1) * size // (n_seeds + 1), size - 1))

    NB = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    target = int(size * size * coverage)
    steps = 0
    top_y = size - 1
    GAP = max(4, size // 14)

    while len(occupied) < target and steps < max_steps:
        wx = rnd.randrange(size)
        wy = max(0, top_y - GAP)
        for _ in range(size * 14):
            steps += 1
            if steps >= max_steps:
                break
            # count DISTINCT touching neighbours (proxy for how many separate
            # branches meet here) -- fusing needs >=fuse of them
            touch = sum(1 for dx, dy in NB
                        if ((wx + dx) % size, (wy + dy) % size) in occupied)
            if touch:
                # stick if it's a normal tip contact, OR if enough branches
                # meet here to justify fusing them into a net
                if touch >= fuse or rnd.random() < 0.35:
                    occupied.add((wx, wy))
                    top_y = min(top_y, wy)
                    break
            r = rnd.random()
            if r < lateral:                      # sideways spread -> fan
                wx += rnd.choice((-1, 1))
            elif r < lateral + 0.30:             # drift toward the base
                wy += 1
            else:
                dx, dy = NB[rnd.randrange(8)]
                wx += dx
                wy += dy
            wx %= size
            if wy < 0 or wy >= size or wy < top_y - 2 * GAP:
                break
    return occupied


def sea_fan_mask(size=200, thicken=1, **kw):
    occ = sea_fan(size=size, **kw)
    im = Image.new("L", (size, size), 0)
    p = im.load()
    for (x, y) in occ:
        p[x, y] = 255
    for _ in range(thicken):
        im = im.filter(ImageFilter.MaxFilter(3))
    return im


# ---------------------------------------------------------------------------
# 2. brain coral: Gray-Scott reaction-diffusion
# ---------------------------------------------------------------------------
def brain_coral(size=128, steps=3000, feed=0.055, kill=0.062,
                dA=0.2, dB=0.1, seed=5, dt=1.0):
    """Gray-Scott on a torus (so the result tiles). Returns an 'L' mask.

    feed/kill pick the regime; ~(0.055, 0.062) is the classic 'maze'/
    labyrinth setting, which is the brain-coral groove pattern.

    STABILITY: with an unscaled 5-point Laplacian the explicit Euler step
    needs dA*dt <= 0.25. The textbook dA=1.0 assumes a pre-scaled Laplacian;
    using it here saturates the grid to a checkerboard on the FIRST step.
    Hence dA=0.2 / dB=0.1.
    Implemented on flat lists -- slow-ish but dependency free.
    """
    rnd = random.Random(seed)
    n = size * size
    A = [1.0] * n
    B = [0.0] * n
    # Seed blobs with the STANDARD Gray-Scott perturbation (A=0.5, B=0.25).
    # Setting B=1.0 while A stays 1.0 makes the a*b*b reaction term explode at
    # the seeds and the whole grid saturates into a checkerboard instead of
    # forming a maze.
    for _ in range(max(6, size // 12)):
        cx, cy = rnd.randrange(size), rnd.randrange(size)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                j = ((cy + dy) % size) * size + (cx + dx) % size
                A[j] = 0.5
                B[j] = 0.25

    idx = range(size)
    for _ in range(steps):
        nA = A[:]
        nB = B[:]
        for y in idx:
            yn = ((y - 1) % size) * size
            ys = ((y + 1) % size) * size
            yc = y * size
            for x in idx:
                xw = (x - 1) % size
                xe = (x + 1) % size
                i = yc + x
                a = A[i]
                b = B[i]
                # 5-point Laplacian on the torus
                lapA = A[yc + xw] + A[yc + xe] + A[yn + x] + A[ys + x] - 4 * a
                lapB = B[yc + xw] + B[yc + xe] + B[yn + x] + B[ys + x] - 4 * b
                abb = a * b * b
                # dt < 1 keeps the explicit Euler step stable; at dt=1 with
                # dA=1.0 the diffusion term overshoots and the grid collapses
                # into a pixel-checkerboard instead of a maze.
                na = a + dt * (dA * lapA - abb + feed * (1 - a))
                nb = b + dt * (dB * lapB + abb - (kill + feed) * b)
                nA[i] = 0.0 if na < 0 else (1.0 if na > 1 else na)
                nB[i] = 0.0 if nb < 0 else (1.0 if nb > 1 else nb)
        A, B = nA, nB

    # normalise the FIELD (0..~0.4) to full 0-255 before rasterising
    lo, hi = min(B), max(B)
    rng = (hi - lo) or 1.0
    im = Image.new("L", (size, size))
    p = im.load()
    for y in idx:
        for x in idx:
            p[x, y] = int(255 * (B[y * size + x] - lo) / rng)
    return im


# ---------------------------------------------------------------------------
# cached mask loader
# ---------------------------------------------------------------------------
def cached_mask(path, kind="brain", size=96, seed=5, force=False, **kw):
    """Return a coral mask, generating it only once and caching to `path`.

    Gray-Scott takes ~15s, which is far too slow to pay on every build, and
    the pattern never changes for fixed parameters -- so it is written to a
    PNG next to the other art and reloaded thereafter. Delete the file (or
    pass force=True) to regrow it.
    """
    if not force and os.path.exists(path):
        return Image.open(path).convert("L")
    if kind == "brain":
        m = brain_coral(size=size, seed=seed, **kw)
    elif kind == "seafan":
        m = sea_fan_mask(size=size, seed=seed, **kw)
    else:
        raise ValueError(f"unknown coral kind: {kind}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    m.save(path)
    return m
