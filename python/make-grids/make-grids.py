#!/usr/bin/env python3
"""Overlay a labeled pixel-coordinate grid on each plate so the user can read off
bounding boxes (x0,y0,x1,y1) for each figure. Saves to /grids."""
import os
from PIL import Image, ImageDraw, ImageFont

SRC, OUT = "objects", "grids"
STEP = 100  # grid spacing in original pixels

def font():
    for p in ["/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, 22)
            except: pass
    return ImageFont.load_default()

def main():
    os.makedirs(OUT, exist_ok=True)
    F = font()
    for pf in sorted(f for f in os.listdir(SRC) if f.lower().endswith(".jpg")):
        img = Image.open(os.path.join(SRC,pf)).convert("RGB")
        d = ImageDraw.Draw(img)
        w,h = img.size
        for x in range(0,w,STEP):
            d.line([(x,0),(x,h)], fill=(255,0,0), width=1)
            d.text((x+2,2), str(x), fill=(255,0,0), font=F)
            d.text((x+2,h-26), str(x), fill=(255,0,0), font=F)
        for y in range(0,h,STEP):
            d.line([(0,y),(w,y)], fill=(255,0,0), width=1)
            d.text((2,y+2), str(y), fill=(0,120,255), font=F)
            d.text((w-52,y+2), str(y), fill=(0,120,255), font=F)
        name = os.path.splitext(pf)[0] + "_grid.png"
        img.save(os.path.join(OUT,name))
        print(f"{name}: {w}x{h}")

if __name__=="__main__":
    main()
