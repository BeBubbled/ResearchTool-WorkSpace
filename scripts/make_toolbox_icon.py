"""Generate static/toolbox.ico (multi-size) for the desktop / taskbar shortcut."""

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # draw large, then downsample for clean edges
OUT = Path(__file__).resolve().parent.parent / "static" / "toolbox.ico"


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # handle (behind lid)
    d.rounded_rectangle((330, 120, 694, 360), radius=90, fill=(70, 78, 92))
    d.rounded_rectangle((400, 190, 624, 360), radius=40, fill=(0, 0, 0, 0))

    # shadow
    d.rounded_rectangle((110, 330, 934, 930), radius=90, fill=(0, 0, 0, 60))

    # body
    d.rounded_rectangle((90, 300, 934, 900), radius=90, fill=(214, 58, 46))
    # lid band
    d.rounded_rectangle((90, 300, 934, 520), radius=90, fill=(236, 84, 62))
    d.rectangle((90, 430, 934, 520), fill=(236, 84, 62))
    # seam
    d.rectangle((90, 515, 934, 545), fill=(150, 34, 28))
    # highlight on lid
    d.rounded_rectangle((150, 340, 874, 380), radius=20, fill=(255, 140, 115))

    # latches
    for x in (230, 694):
        d.rounded_rectangle((x, 470, x + 100, 610), radius=22, fill=(255, 196, 48))
        d.rounded_rectangle((x + 25, 530, x + 75, 575), radius=10, fill=(196, 140, 20))

    # bottom shading
    d.rounded_rectangle((90, 820, 934, 900), radius=60, fill=(176, 44, 36))
    d.rectangle((90, 800, 934, 850), fill=(214, 58, 46))
    return img


def main() -> None:
    big = draw()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(s, s) for s in sizes])
    print(OUT)


if __name__ == "__main__":
    main()
