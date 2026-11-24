"""Generate static/toolbox.ico (multi-size) for the desktop / taskbar shortcut."""

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # draw large, then downsample for clean edges
OUT = Path(__file__).resolve().parent.parent / "static" / "toolbox.ico"
PNG_OUT = OUT.with_suffix(".png")  # Linux 桌面启动器（.desktop）使用


STEEL = (146, 157, 172)  # 偏深，浅色任务栏上才有对比
STEEL_DARK = (104, 115, 132)
WRENCH = (226, 62, 48)  # 灰色扳手在任务栏上看不清，改用红色
WRENCH_DARK = (176, 40, 32)


def _layer() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _tilt(layer: Image.Image, angle: float) -> Image.Image:
    # 工具都竖着画，再绕箱口附近的支点倾斜
    return layer.rotate(angle, resample=Image.BICUBIC, center=(512, 700))


def _wrench() -> Image.Image:
    img, d = _layer()
    d.rounded_rectangle((427, 260, 597, 780), radius=30, fill=WRENCH)
    d.rectangle((548, 300, 597, 780), fill=WRENCH_DARK)
    d.ellipse((332, 30, 692, 390), fill=WRENCH)
    # 开口
    d.rectangle((437, 0, 587, 215), fill=(0, 0, 0, 0))
    return _tilt(img, 26)


def _screwdriver() -> Image.Image:
    img, d = _layer()
    d.rectangle((457, 380, 567, 780), fill=STEEL)
    d.rectangle((532, 380, 567, 780), fill=STEEL_DARK)
    d.rounded_rectangle((397, 30, 627, 400), radius=100, fill=(246, 166, 35))
    d.rounded_rectangle((560, 60, 627, 370), radius=40, fill=(214, 132, 16))
    d.rectangle((397, 330, 627, 400), fill=(70, 78, 92))
    return _tilt(img, -26)


def draw() -> Image.Image:
    img, d = _layer()

    # 只放两件大工具（扳手、螺丝刀），任务栏的小尺寸下也认得出来
    for tool in (_wrench(), _screwdriver()):
        img = Image.alpha_composite(img, tool)
    d = ImageDraw.Draw(img)

    # shadow
    d.rounded_rectangle((110, 620, 934, 1010), radius=80, fill=(0, 0, 0, 60))

    # 敞口箱体（蓝色，避免红箱子被认成急救包）
    d.rounded_rectangle((80, 590, 944, 990), radius=80, fill=(37, 99, 201))
    # 箱口边沿
    d.rounded_rectangle((50, 550, 974, 690), radius=55, fill=(66, 133, 235))
    d.rounded_rectangle((120, 578, 904, 610), radius=16, fill=(140, 186, 255))
    d.rectangle((80, 690, 944, 714), fill=(24, 72, 156))

    # 正面搭扣
    d.rounded_rectangle((422, 640, 602, 830), radius=30, fill=(255, 196, 48))
    d.rounded_rectangle((467, 725, 557, 790), radius=14, fill=(196, 140, 20))

    # bottom shading
    d.rounded_rectangle((80, 900, 944, 990), radius=60, fill=(27, 80, 170))
    d.rectangle((80, 880, 944, 930), fill=(37, 99, 201))
    return img


def main() -> None:
    big = draw()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(s, s) for s in sizes])
    print(OUT)
    big.resize((256, 256), Image.LANCZOS).save(PNG_OUT)
    print(PNG_OUT)


if __name__ == "__main__":
    main()
