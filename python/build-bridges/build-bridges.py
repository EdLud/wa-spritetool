#!/usr/bin/env python3
"""Build the three bridge source PNGs from a red-marked plate.

W:A bridges come in three pieces, each 64px WIDE with variable height:
  bridge-l.img  left  end cap
  bridge.img    middle (repeats to span the gap)
  bridge-r.img  right end cap

Workflow (mirrors process_marked.py): the user draws ONE closed red loop
around the figure they want as the bridge in the MARKED plate, and the clean
full-res JP2 sits in raw/. This script:

 1. finds the red loop, floods "outside", takes the one enclosed region
 2. removes the plate background inside it (colour-distance alpha, sampled
    locally just inside the loop) -- pixels come from the CLEAN raw plate
 3. rotates the cutout 90 deg CCW (a vertical figure becomes a horizontal
    bridge strip)
 4. splits the strip into three equal columns  Cut_1 | Cut_2 | Cut_3
 5. composes:
        left   = Cut_1 + Cut_2
        middle = Cut_3 + mirror(Cut_3)
        right  = mirror(left)
 6. scales all three to 64px wide (height derived from the cutout's own
    proportions, shared across the three so they line up) and saves:
        Bridge/bridge_l_src.png
        Bridge/bridge_mid_src.png
        Bridge/bridge_r_src.png

build_terrain.py then quantises these to the terrain palette and emits the
three .img files.

Usage:
  python3 build_bridges.py                 # default plate below
  python3 build_bridges.py 0283            # pick a plate by its 4-digit number
"""
import os, sys, re, collections, statistics
from PIL import Image, ImageFilter

HERE    = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(HERE, "Distant Planet")
MARKED_DIR = os.path.join(PROJECT, "Bridge", "marked_bridges")  # red-loop plates
RAW_DIR    = os.path.join(PROJECT, "raw")                       # clean full-res JP2s
OUT_DIR    = os.path.join(PROJECT, "Bridge")

DEFAULT_PLATE = "0283"
BRIDGE_W = 64            # required output width (px); height is free

# same knobs as process_marked.py
ANALYZE_SCALE = 4        # region analysis downscale
BG_THRESH = 60           # colour-distance alpha threshold
BG_SOFT   = 60           # soft ramp width above threshold
RED_CLOSE = 25           # full-res px the red line is thickened to bridge gaps
PAD       = 8


def find_plate(directory, num, exts):
    for f in os.listdir(directory):
        if f.lower().endswith(exts) and num in f:
            return os.path.join(directory, f)
    return None


def largest_region(im):
    """Red-loop detection -> single largest enclosed region (analysis grid)."""
    w0, h0 = im.size
    r, g, b = im.split()
    rp, gp, bp = r.load(), g.load(), b.load()
    redfull = Image.new("L", (w0, h0), 0)
    rf = redfull.load()
    for y in range(h0):
        for x in range(w0):
            if rp[x, y] > 170 and gp[x, y] < 100 and bp[x, y] < 100:
                rf[x, y] = 255
    redfull = redfull.filter(ImageFilter.MaxFilter(RED_CLOSE))
    w, h = w0 // ANALYZE_SCALE, h0 // ANALYZE_SCALE
    small_red = redfull.resize((w, h), Image.BOX).point(lambda v: 255 if v > 20 else 0)
    srp = small_red.load()
    red = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if srp[x, y]:
                red[y * w + x] = 1
    outside = bytearray(w * h)
    dq = collections.deque()
    for x in range(w):
        for i in (x, (h - 1) * w + x):
            if not red[i] and not outside[i]:
                outside[i] = 1; dq.append(i)
    for y in range(h):
        for i in (y * w, y * w + w - 1):
            if not red[i] and not outside[i]:
                outside[i] = 1; dq.append(i)
    while dq:
        i = dq.popleft(); x = i % w; y = i // w
        for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if not red[j] and not outside[j]:
                    outside[j] = 1; dq.append(j)
    seen = bytearray(w * h)
    regions = []
    for start in range(w * h):
        if not red[start] and not outside[start] and not seen[start]:
            comp = []
            seen[start] = 1; dq.append(start)
            while dq:
                i = dq.popleft(); comp.append(i); x = i % w; y = i // w
                for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if not red[j] and not outside[j] and not seen[j]:
                            seen[j] = 1; dq.append(j)
            regions.append(comp)
    if not regions:
        return None, w, h
    return max(regions, key=len), w, h


def plate_bg(im):
    """Dominant colour of the printed area (mode of coarse histogram)."""
    W, H = im.size
    inner = im.crop((int(W*0.12), int(H*0.12), int(W*0.88), int(H*0.88)))
    small = inner.resize((inner.width // 8, inner.height // 8))
    px = small.load()
    hist = collections.Counter()
    for y in range(0, small.height, 2):
        for x in range(0, small.width, 2):
            r, g, b = px[x, y][:3]
            hist[(r//16, g//16, b//16)] += 1
    (br, bg, bb), _ = hist.most_common(1)[0]
    return br*16+8, bg*16+8, bb*16+8


def local_bg(im, comp, w, h, fallback):
    """Background colour sampled just inside the drawn loop."""
    S = ANALYZE_SCALE
    inset = set(comp)
    samples = []
    for i in comp:
        x, y = i % w, i // w
        if any(not (0 <= nx < w and 0 <= ny < h) or ny*w+nx not in inset
               for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1))):
            fx = min(im.width-1, x*S + S//2)
            fy = min(im.height-1, y*S + S//2)
            r, g, b = im.getpixel((fx, fy))[:3]
            if not (r > 170 and g < 100 and b < 100):
                samples.append((r, g, b))
    if len(samples) < 25:
        return fallback
    return (statistics.median(s[0] for s in samples),
            statistics.median(s[1] for s in samples),
            statistics.median(s[2] for s in samples))


def cut(im, comp, w, h, bg, raw):
    """RGBA cutout of the enclosed region, pixels sampled from the clean raw."""
    bg = local_bg(im, comp, w, h, bg)
    minx = min(i % w for i in comp); maxx = max(i % w for i in comp)
    miny = min(i // w for i in comp); maxy = max(i // w for i in comp)
    S = ANALYZE_SCALE
    sx = raw.width / im.width; sy = raw.height / im.height
    padx = int(PAD * sx); pady = int(PAD * sy)
    x0 = max(0, int(minx*S*sx) - padx); y0 = max(0, int(miny*S*sy) - pady)
    x1 = min(raw.width,  int((maxx+1)*S*sx) + padx)
    y1 = min(raw.height, int((maxy+1)*S*sy) + pady)
    sub = raw.crop((x0, y0, x1, y1)).convert("RGBA")
    m = Image.new("L", (w, h), 0)
    mp = m.load()
    for i in comp:
        mp[i % w, i // w] = 255
    m = m.resize(raw.size, Image.NEAREST).crop((x0, y0, x1, y1))
    br, bgc, bb = bg
    px = sub.load(); mpx = m.load()
    for y in range(sub.height):
        for x in range(sub.width):
            r, g, b, _ = px[x, y]
            if not mpx[x, y]:
                px[x, y] = (r, g, b, 0)
                continue
            d = abs(r-br) + abs(g-bgc) + abs(b-bb)
            if d <= BG_THRESH:
                a = 0
            elif d >= BG_THRESH + BG_SOFT:
                a = 255
            else:
                a = int((d - BG_THRESH) / BG_SOFT * 255)
            if r > 180 and g < 90 and b < 90:
                a = 0
            px[x, y] = (r, g, b, a)
    bbox = sub.getchannel("A").getbbox()
    return sub.crop(bbox) if bbox else sub


def scale_to_width(im, width):
    h = max(1, round(im.height * width / im.width))
    return im.resize((width, h), Image.LANCZOS)


def main():
    num = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLATE)
    m = re.search(r"(\d{4})", num)
    num = m.group(1) if m else num

    marked_path = find_plate(MARKED_DIR, num, (".png", ".jpg", ".jpeg", ".jp2"))
    raw_path    = find_plate(RAW_DIR,    num, (".jp2", ".jpg", ".jpeg", ".png"))
    if not marked_path:
        sys.exit(f"no marked plate matching {num} in {MARKED_DIR}")
    if not raw_path:
        sys.exit(f"no clean raw plate matching {num} in {RAW_DIR}")

    im  = Image.open(marked_path).convert("RGB")
    raw = Image.open(raw_path).convert("RGB")
    if abs(im.width/im.height - raw.width/raw.height) > 0.01:
        print(f"WARNING marked {im.size} vs raw {raw.size} aspect mismatch")

    comp, w, h = largest_region(im)
    if comp is None:
        sys.exit("no enclosed region -- is the red loop closed?")

    obj = cut(im, comp, w, h, plate_bg(im), raw)
    print(f"cutout: {obj.size}")

    # vertical figure -> horizontal bridge strip (CCW: original top -> left)
    strip = obj.rotate(90, expand=True)
    bbox = strip.getchannel("A").getbbox()
    if bbox:
        strip = strip.crop(bbox)
    print(f"rotated strip: {strip.size}")

    # split into three equal columns
    sw, sh = strip.size
    t1 = sw // 3
    t2 = 2 * sw // 3
    cut1 = strip.crop((0,  0, t1, sh))
    cut2 = strip.crop((t1, 0, t2, sh))
    cut3 = strip.crop((t2, 0, sw, sh))

    # compose the three pieces (full res, then scaled to 64px wide together)
    left  = Image.new("RGBA", (cut1.width + cut2.width, sh), (0, 0, 0, 0))
    left.paste(cut1, (0, 0))
    left.paste(cut2, (cut1.width, 0))

    cut3m = cut3.transpose(Image.FLIP_LEFT_RIGHT)
    middle = Image.new("RGBA", (cut3.width + cut3m.width, sh), (0, 0, 0, 0))
    middle.paste(cut3,  (0, 0))
    middle.paste(cut3m, (cut3.width, 0))

    right = left.transpose(Image.FLIP_LEFT_RIGHT)

    out_l = scale_to_width(left,   BRIDGE_W)
    out_m = scale_to_width(middle, BRIDGE_W)
    out_r = scale_to_width(right,  BRIDGE_W)

    out_l.save(os.path.join(OUT_DIR, "bridge_l_src.png"))
    out_m.save(os.path.join(OUT_DIR, "bridge_mid_src.png"))
    out_r.save(os.path.join(OUT_DIR, "bridge_r_src.png"))
    print(f"saved bridge_l_src.png  {out_l.size}")
    print(f"saved bridge_mid_src.png {out_m.size}")
    print(f"saved bridge_r_src.png  {out_r.size}")


if __name__ == "__main__":
    main()
