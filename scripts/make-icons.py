"""Render the PWA icons (static/icons/*.png). Needs Pillow and a Japanese bold font (Windows: Yu Gothic Bold).

    python scripts/make-icons.py

The PNGs are committed, so this only has to be run when the design changes.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "icons")
BG = (255, 40, 0)        # #FF2800, Ferrari red
FG = (255, 255, 255)
FONTS = [r"C:\Windows\Fonts\YuGothB.ttc", r"C:\Windows\Fonts\meiryob.ttc", r"C:\Windows\Fonts\msgothic.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"]
TEXT = "G醫t"


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    sys.exit("No Japanese bold font found; edit FONTS in this script.")


def draw(size: int, *, maskable: bool, rounded: bool) -> Image.Image:
    """maskable: full-bleed square with the text inside the 80% safe zone; otherwise a rounded square."""
    scale = 4  # draw large, then downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable or not rounded:
        d.rectangle([0, 0, s, s], fill=BG)
    else:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)
    target = s * (0.62 if maskable else 0.74)  # text width as a share of the icon
    px = 10
    while font(px + 4).getbbox(TEXT)[2] - font(px + 4).getbbox(TEXT)[0] < target:
        px += 4
    f = font(px)
    x0, y0, x1, y1 = d.textbbox((0, 0), TEXT, font=f)
    d.text(((s - (x1 - x0)) / 2 - x0, (s - (y1 - y0)) / 2 - y0), TEXT, font=f, fill=FG)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = {
        "icon-192.png": (192, False, True),
        "icon-512.png": (512, False, True),
        "icon-maskable-192.png": (192, True, False),
        "icon-maskable-512.png": (512, True, False),
        "apple-touch-icon.png": (180, True, False),   # iOS applies its own rounding, so no transparency
    }
    for name, (size, maskable, rounded) in jobs.items():
        draw(size, maskable=maskable, rounded=rounded).save(os.path.join(OUT, name), optimize=True)
        print("wrote", name)


if __name__ == "__main__":
    main()
