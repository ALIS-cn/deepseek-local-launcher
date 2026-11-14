# -*- coding: utf-8 -*-
"""生成应用图标 assets/app.ico（构建时运行一次即可）"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "app.ico"
SIZE = 256
RADIUS = 58


def gradient(size: int):
    """蓝紫渐变背景"""
    img = Image.new("RGB", (size, size))
    top = (77, 107, 254)
    bottom = (124, 92, 255)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            img.putpixel((x, y), color)
    return img


def main():
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), RADIUS, fill=255)
    base.paste(gradient(SIZE), (0, 0), mask)

    draw = ImageDraw.Draw(base)
    # 白色聊天气泡
    draw.rounded_rectangle((52, 58, 204, 178), 30, fill=(255, 255, 255, 235))
    draw.polygon([(92, 170), (92, 208), (134, 172)], fill=(255, 255, 255, 235))

    # 气泡里的字母 D
    font = None
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 84)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "D"
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(
        (128 - tw / 2 - box[0], 118 - th / 2 - box[1]),
        text,
        font=font,
        fill=(77, 107, 254, 255),
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    base.save(
        OUT,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("icon written:", OUT)


if __name__ == "__main__":
    main()
