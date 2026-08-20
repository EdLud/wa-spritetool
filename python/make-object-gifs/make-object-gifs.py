"""
Generate a preview GIF for each obj-*.bmp in Distant Planet/build/dist_planet/.
Layout rules from objects.txt:
  floor   -> bottom edge, horizontally centered
  ceiling -> top edge, horizontally centered
  side    -> left or right edge (alternates), vertically centered
Frame size: square determined by the largest BMP's max dimension (cladonema = 420px).
Background: #fefefe (from example.gif).
FPS: 2 (500ms delay, from example.gif).
"""

from PIL import Image
import os, re

BMP_DIR = "Distant Planet/build/dist_planet"
OBJ_TXT = "Distant Planet/objects.txt"
OUT_DIR = "Distant Planet/Misc/gif"
BG_COLOR = (68, 8, 33, 255)
FRAME_DELAY_MS = 500  # 2 FPS from example.gif

# --- Parse objects.txt ---
objects = {}
with open(OBJ_TXT) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # skip header line
        if line.startswith("name"):
            continue
        parts = re.split(r'\s+', line)
        if len(parts) < 16:
            continue
        name = parts[0]
        objects[name] = {
            "rotate": float(parts[5]),
            "mirror": int(parts[6]),
            "where":  parts[15],
        }

# --- Find all obj- BMPs ---
bmp_files = sorted(
    f for f in os.listdir(BMP_DIR)
    if f.startswith("obj-") and f.endswith(".bmp")
)

# --- Determine frame size from largest BMP ---
max_dim = 0
for bmp in bmp_files:
    img = Image.open(os.path.join(BMP_DIR, bmp))
    max_dim = max(max_dim, img.width, img.height)

FRAME_SIZE = max_dim  # square
print(f"Frame size: {FRAME_SIZE}x{FRAME_SIZE} (largest BMP max dim)")

frames = []

for bmp in bmp_files:
    name = bmp.replace("obj-", "").replace(".img.bmp", "")
    meta = objects.get(name)
    if meta is None:
        print(f"  WARNING: {name} not in objects.txt, skipping")
        continue

    # Load BMP, make index 0 transparent
    src = Image.open(os.path.join(BMP_DIR, bmp)).copy()
    src.info["transparency"] = 0
    src = src.convert("RGBA")

    # Apply mirror
    mirror = meta["mirror"]
    if mirror == 1 or mirror == 3:
        src = src.transpose(Image.FLIP_LEFT_RIGHT)
    if mirror == 2 or mirror == 3:
        src = src.transpose(Image.FLIP_TOP_BOTTOM)

    # Apply rotation (PIL rotates CCW, objects.txt uses +ccw/-cw, so same sign)
    angle = meta["rotate"]
    if angle != 0:
        src = src.rotate(angle, expand=True, resample=Image.BICUBIC)

    obj_w, obj_h = src.size

    x = (FRAME_SIZE - obj_w) // 2
    y = (FRAME_SIZE - obj_h) // 2

    # Flatten onto bg then quantize to P mode
    bg_flat = Image.new("RGB", (FRAME_SIZE, FRAME_SIZE), BG_COLOR[:3])
    bg_flat.paste(src, (x, y), mask=src)
    frame_p = bg_flat.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    frames.append(frame_p)
    print(f"  {name}")

out_path = os.path.join(OUT_DIR, "objects.gif")
os.makedirs(OUT_DIR, exist_ok=True)
frames[0].save(
    out_path,
    format="GIF",
    save_all=True,
    append_images=frames[1:],
    duration=FRAME_DELAY_MS,
    loop=0,
)
print(f"\nDone. {len(frames)} frames -> {out_path}")
