"""The TeamSync mark: the old quill, standing over a ring of two arrows.

The ring says what the program does - work travels both ways, on its own. The
quill is the one that was there before, redrawn at full size: the 32-pixel
original could not be enlarged without turning to mush.

Everything is drawn at four times the final size and reduced with Lanczos, so
the curves are smooth at every size Windows asks for, not just the big one.
"""
from PIL import Image, ImageDraw
import math, os

ACCENT     = (79, 140, 255, 255)
ACCENT_TOP = (110, 163, 255, 255)
WHITE      = (255, 255, 255, 255)
INK        = (16, 26, 48, 255)     # the quill: dark navy
INK_EDGE   = (7, 11, 22, 255)      # its outline, nearly black
SHEEN      = (128, 172, 244, 255)  # the barb streaks, as on the original

K = 4
S = 1024 * K
cx = cy = S // 2

# No tile: the mark stands on whatever is behind it. That means it has to read on
# a dark taskbar and on a light desktop alike, so the ring is the accent blue
# rather than white, and the dark quill carries a light rim.
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- the ring -----------------------------------------------------------------
R, w = 296*K, 60*K
def arrow(start, end):
    d.arc([cx-R, cy-R, cx+R, cy+R], start, end, fill=ACCENT, width=w)
    a = math.radians(end)
    px, py = cx + R*math.cos(a), cy + R*math.sin(a)
    tx, ty = -math.sin(a), math.cos(a)
    nx, ny = math.cos(a), math.sin(a)
    d.polygon([(px + tx*w*1.6, py + ty*w*1.6),
               (px + nx*w*1.2, py + ny*w*1.2),
               (px - nx*w*1.2, py - ny*w*1.2)], fill=ACCENT)
arrow(203, 337)
arrow(23, 157)

# --- the quill, standing over the ring ----------------------------------------
# Slender and near-upright, leaning right, as the original is: a pointed top, a
# belly a little above the middle, and a bare shaft running out below to a point.
BASE = (cx - 58*K, cy + 352*K)      # where the nib touches
TOP  = (cx + 104*K, cy - 392*K)     # the tip of the plume, above the ring
BOW  = 40*K

ang = math.atan2(TOP[1]-BASE[1], TOP[0]-BASE[0])
sx, sy = math.cos(ang), math.sin(ang)
nx, ny = -math.sin(ang), math.cos(ang)
L = math.hypot(TOP[0]-BASE[0], TOP[1]-BASE[1])

def spine(t):
    return (BASE[0] + sx*L*t - nx*BOW*math.sin(math.pi*t*0.86),
            BASE[1] + sy*L*t - ny*BOW*math.sin(math.pi*t*0.86))

VSTART = 0.26                        # bare shaft below this - that is the nib
def vane(t, side):
    if t <= VSTART:
        return 0.0
    u = (t - VSTART) / (1.0 - VSTART)
    base = 108*K * math.sin(math.pi * u**0.62) ** 0.86
    return base * (1.0 if side > 0 else 0.74)

steps = 320
left  = [(spine(i/steps)[0] + nx*vane(i/steps, 1),
          spine(i/steps)[1] + ny*vane(i/steps, 1)) for i in range(steps+1)]
right = [(spine(i/steps)[0] - nx*vane(i/steps, -1),
          spine(i/steps)[1] - ny*vane(i/steps, -1)) for i in range(steps, -1, -1)]
outline = left + right
d.line(outline + [outline[0]], fill=SHEEN, width=22*K, joint="curve")
d.polygon(outline, fill=INK)
d.line(outline + [outline[0]], fill=INK_EDGE, width=7*K, joint="curve")

# the shaft below the plume, tapering to the writing point
for i in range(40):
    t0, t1 = i/40, (i+1)/40
    if t1 > VSTART + 0.02:
        break
    thick = 26*K * (0.30 + 0.70*(t1/VSTART))
    d.line([spine(t0), spine(t1)], fill=SHEEN, width=max(4, int(thick*1.5)))
    d.line([spine(t0), spine(t1)], fill=INK_EDGE, width=max(3, int(thick)))

# barbs, leaning off the shaft towards the tip, as on the original
for i in range(16):
    t = VSTART + 0.03 + i*0.045
    if t > 0.97:
        break
    bx, by = spine(t)
    lean = math.radians(30)
    for side in (1, -1):
        a2 = ang + side*(math.pi/2) - lean
        reach = vane(t, side) * 0.88
        if reach < 6*K:
            continue
        d.line([(bx, by), (bx + math.cos(a2)*reach, by + math.sin(a2)*reach)],
               fill=SHEEN, width=7*K)
# and the shaft line itself, light, running up the middle
d.line([spine(VSTART), spine(0.95)], fill=SHEEN, width=8*K)

master = img.resize((1024, 1024), Image.LANCZOS)
out = os.path.join("ui", "assets"); os.makedirs(out, exist_ok=True)
ico = os.path.join(out, "teamsync.ico")
master.save(ico, sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
master.resize((256,256), Image.LANCZOS).save(os.path.join(out, "teamsync-preview.png"))
for name, bg in (("teamsync-on-dark.png", (32, 34, 40, 255)),
                 ("teamsync-on-light.png", (245, 246, 248, 255))):
    plate = Image.new("RGBA", (320, 320), bg)
    plate.alpha_composite(master.resize((256, 256), Image.LANCZOS), (32, 32))
    plate.save(os.path.join(out, name))

strip = Image.new("RGBA", (16+24+32+48+64 + 5*12, 64), (24,26,32,255))
x = 0
for sz in (16,24,32,48,64):
    strip.paste(master.resize((sz,sz), Image.LANCZOS), (x, (64-sz)//2)); x += sz+12
strip.resize((strip.width*3, strip.height*3), Image.NEAREST).save(
    os.path.join(out, "teamsync-sizes.png"))
print("wrote", ico, os.path.getsize(ico), "bytes")
