#!/usr/bin/env python3
"""Make a photographic texture tile seamlessly, without numpy.

Strategy (image-quilting style, the standard approach for grainy material):

1. Take a source crop LARGER than the target tile by an overlap margin.
2. Build the tile by wrapping the crop onto itself so the tile's own opposite
   edges come from CONTIGUOUS source pixels -- i.e. the right edge of the tile
   and the left edge of the tile are neighbours in the original photo. That
   makes them intrinsically compatible instead of arbitrary.
3. Where the wrapped copies overlap, don't blend along a straight line (that
   leaves a visible soft band and, in directional material like sand ripples,
   an obvious "restart"). Instead find the MIN-ERROR BOUNDARY: a 1-px-wide
   irregular path through the overlap that follows wherever the two candidate
   images already agree. Cut along that path. In grain/noise the path hides
   completely.
4. Feather only +-1px around the cut to kill single-pixel aliasing.

Seam quality is then verified numerically: the wrap-around difference should be
statistically indistinguishable from a normal interior pixel step.
"""
import os
import sys
from PIL import Image, ImageFilter


def _px_diff(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _min_error_path_vertical(imgA, imgB, x0, w, h):
    """Find a top->bottom min-error cut through a vertical overlap band.

    imgA/imgB are loaded pixel accessors covering the same band geometry.
    The band is `w` px wide starting at x0, `h` px tall.
    Returns a list of length h: for each row, the band-relative x where we
    switch from imgA (left of cut) to imgB (right of cut).
    """
    INF = float("inf")
    # cost[y][x] = squared-ish difference between the two candidates
    cost = [[_px_diff(imgA[x0 + x, y], imgB[x0 + x, y]) for x in range(w)]
            for y in range(h)]

    # DP accumulate downward, allowing the path to step -1/0/+1 in x
    acc = [cost[0][:]]
    back = [[0] * w]
    for y in range(1, h):
        prev = acc[-1]
        row = [0.0] * w
        brow = [0] * w
        for x in range(w):
            best = INF
            bestk = x
            for k in (x - 1, x, x + 1):
                if 0 <= k < w and prev[k] < best:
                    best = prev[k]
                    bestk = k
            row[x] = cost[y][x] + best
            brow[x] = bestk
        acc.append(row)
        back.append(brow)

    # walk back from the cheapest endpoint
    last = acc[-1]
    x = min(range(w), key=lambda i: last[i])
    path = [0] * h
    for y in range(h - 1, -1, -1):
        path[y] = x
        x = back[y][x]
    return path


def _min_error_path_horizontal(imgA, imgB, y0, h, w):
    """Same as above but for a horizontal overlap band: left->right cut.
    Returns list of length w giving the band-relative y per column."""
    INF = float("inf")
    cost = [[_px_diff(imgA[x, y0 + y], imgB[x, y0 + y]) for y in range(h)]
            for x in range(w)]
    acc = [cost[0][:]]
    back = [[0] * h]
    for x in range(1, w):
        prev = acc[-1]
        col = [0.0] * h
        bcol = [0] * h
        for y in range(h):
            best = INF
            bestk = y
            for k in (y - 1, y, y + 1):
                if 0 <= k < h and prev[k] < best:
                    best = prev[k]
                    bestk = k
            col[y] = cost[x][y] + best
            bcol[y] = bestk
        acc.append(col)
        back.append(bcol)
    last = acc[-1]
    y = min(range(h), key=lambda i: last[i])
    path = [0] * w
    for x in range(w - 1, -1, -1):
        path[x] = y
        y = back[x][y]
    return path


def make_seamless(src_path, size=256, overlap_frac=0.25, seed_crop="center",
                  region=None):
    """Return a `size`x`size` seamlessly tiling RGB Image from src_path.

    `region` (px) crops that square out of the source photo at native
    resolution BEFORE scaling down to the tile, i.e. it sets the zoom level.
    Smaller region = more zoomed in = finer grain and less recognisable
    large-scale pattern when the tile repeats. None = use the whole photo.
    """
    src = Image.open(src_path).convert("RGB")
    if region:
        W, H = src.size
        need = int(region * (1 + overlap_frac))
        need = min(need, W, H)
        cx, cy = (W - need) // 2, (H - need) // 2
        tgt = int(size * (1 + overlap_frac))
        src = src.crop((cx, cy, cx + need, cy + need)).resize((tgt, tgt),
                                                              Image.LANCZOS)
    SW, SH = src.size

    ov = max(8, int(size * overlap_frac))
    need_w, need_h = size + ov, size + ov
    if SW < need_w or SH < need_h:
        # upscale source so we have room for the overlap margin
        s = max(need_w / SW, need_h / SH)
        src = src.resize((max(need_w, int(SW * s) + 1),
                          max(need_h, int(SH * s) + 1)), Image.LANCZOS)
        SW, SH = src.size

    # crop a (size+ov) block; centre by default
    if seed_crop == "center":
        cx, cy = (SW - need_w) // 2, (SH - need_h) // 2
    else:
        cx, cy = 0, 0
    block = src.crop((cx, cy, cx + need_w, cy + need_h))

    # --- horizontal wrap ---------------------------------------------------
    # base = leftmost `size` columns.  shifted = the block's RIGHT part, i.e.
    # the pixels that in the photo continue past base's right edge, brought
    # around to overlap base's LEFT edge. Because they are photo-contiguous
    # with base's right edge, cutting between them yields a true wrap.
    base = block.crop((0, 0, size, need_h))
    tailA = block.crop((size, 0, size + ov, need_h))       # continues past right edge

    canvas = Image.new("RGB", (size, need_h))
    canvas.paste(base, (0, 0))
    # place the tail over the left overlap region
    over = Image.new("RGB", (size, need_h))
    over.paste(base, (0, 0))
    over.paste(tailA, (0, 0))                              # tail sits on left band

    a = canvas.load()
    b = over.load()
    pathv = _min_error_path_vertical(b, a, 0, ov, need_h)   # b(left/tail) -> a(base)

    merged = canvas.copy()
    mp = merged.load()
    op = over.load()
    for y in range(need_h):
        cut = pathv[y]
        for x in range(0, cut):
            mp[x, y] = op[x, y]

    # feather 1px along the cut to remove stair aliasing
    soft = merged.filter(ImageFilter.GaussianBlur(0.6))
    sp = soft.load()
    for y in range(need_h):
        cut = pathv[y]
        for x in (cut - 1, cut, cut + 1):
            if 0 <= x < size:
                mp[x, y] = sp[x, y]

    # --- vertical wrap -----------------------------------------------------
    stageA = merged                                        # size x need_h
    top = stageA.crop((0, 0, size, size))
    tailB = stageA.crop((0, size, size, size + ov))        # continues past bottom

    canvas2 = Image.new("RGB", (size, size))
    canvas2.paste(top, (0, 0))
    over2 = Image.new("RGB", (size, size))
    over2.paste(top, (0, 0))
    over2.paste(tailB, (0, 0))                             # tail on top band

    a2 = canvas2.load()
    b2 = over2.load()
    pathh = _min_error_path_horizontal(b2, a2, 0, ov, size)

    out = canvas2.copy()
    outp = out.load()
    o2p = over2.load()
    for x in range(size):
        cut = pathh[x]
        for y in range(0, cut):
            outp[x, y] = o2p[x, y]

    soft2 = out.filter(ImageFilter.GaussianBlur(0.6))
    s2p = soft2.load()
    for x in range(size):
        cut = pathh[x]
        for y in (cut - 1, cut, cut + 1):
            if 0 <= y < size:
                outp[x, y] = s2p[x, y]

    return out


def seam_report(im, label=""):
    """Compare wrap-around edge difference against interior adjacency."""
    w, h = im.size
    px = im.load()

    def d(a, b):
        return sum(abs(a[i] - b[i]) for i in range(3)) / 3.0

    wrap_v = sum(d(px[0, y], px[w - 1, y]) for y in range(h)) / h
    wrap_h = sum(d(px[x, 0], px[x, h - 1]) for x in range(w)) / w
    # interior baseline = mean adjacent-step difference over many lines
    iv = sum(d(px[x, y], px[x + 1, y])
             for y in range(0, h, 4) for x in range(0, w - 1, 4))
    nv = len(range(0, h, 4)) * len(range(0, w - 1, 4))
    ih = sum(d(px[x, y], px[x, y + 1])
             for y in range(0, h - 1, 4) for x in range(0, w, 4))
    nh = len(range(0, h - 1, 4)) * len(range(0, w, 4))
    base_v, base_h = iv / nv, ih / nh
    print(f"{label}")
    print(f"  L/R wrap {wrap_v:6.2f}   interior {base_v:5.2f}   ratio {wrap_v/base_v:4.2f}x")
    print(f"  T/B wrap {wrap_h:6.2f}   interior {base_h:5.2f}   ratio {wrap_h/base_h:4.2f}x")
    return wrap_v / base_v, wrap_h / base_h


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "Coral Reef/Texture/seabed.png"
    outp = sys.argv[2] if len(sys.argv) > 2 else "/tmp/seamless_out.png"
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 256
    # region: zoom level in source px. Coral Reef's seabed.png uses 340.
    region = int(sys.argv[4]) if len(sys.argv) > 4 else None
    im = make_seamless(src, size=size, region=region)
    im.save(outp)
    seam_report(im, f"{os.path.basename(outp)} ({size}x{size})")
    # tiled preview
    t = Image.new("RGB", (size * 3, size * 3))
    for y in range(3):
        for x in range(3):
            t.paste(im, (x * size, y * size))
    t.save(outp.replace(".png", "_3x3.png"))
    print(f"  wrote {outp} and 3x3 preview")
