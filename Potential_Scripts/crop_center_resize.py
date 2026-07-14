#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
from pathlib import Path
from PIL import Image


IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def get_resample_filter():
    # Pillow new: Image.Resampling.LANCZOS
    if hasattr(Image, "Resampling"):
        return Image.Resampling.LANCZOS
    # Pillow old: Image.LANCZOS (or Image.ANTIALIAS)
    if hasattr(Image, "LANCZOS"):
        return Image.LANCZOS
    return Image.ANTIALIAS


def center_crop(img: Image.Image, crop_w: int, crop_h: int, offset_y: int = -80) -> Image.Image:
    w, h = img.size
    if w < crop_w or h < crop_h:
        raise ValueError(f"Image too small for crop: got {w}x{h}, need >= {crop_w}x{crop_h}")

    # 以图像中心为基准，裁剪中心沿 y 方向偏移 offset_y（向上是负数）
    cx = w // 2
    cy = h // 2 + offset_y

    left = cx - crop_w // 2
    top = cy - crop_h // 2
    right = left + crop_w
    bottom = top + crop_h

    # 边界裁剪：保证窗口落在图像内
    if left < 0:
        left, right = 0, crop_w
    if right > w:
        right, left = w, w - crop_w
    if top < 0:
        top, bottom = 0, crop_h
    if bottom > h:
        bottom, top = h, h - crop_h

    return img.crop((left, top, right, bottom))



def process_one(in_path: Path, out_path: Path, crop_size: int = 256, out_size: int = 512) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resample = get_resample_filter()

    with Image.open(in_path) as im:
        # 你的输入是 512x512，直接做中心裁剪与缩放
        im = im.convert("RGB")
        cropped = center_crop(im, crop_size, crop_size, offset_y=-80)
        resized = cropped.resize((out_size, out_size), resample=resample)
        resized.save(out_path, quality=95)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=str, required=True)
    parser.add_argument("--crop", type=int, default=256)
    parser.add_argument("--out", type=int, default=512)
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args()

    in_dir = Path(args.input_dir).expanduser().resolve()
    if not in_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {in_dir}")

    out_dir = in_dir.parent / f"{in_dir.name}_resized"
    out_dir.mkdir(parents=True, exist_ok=True)

    pattern = "**/*" if args.recursive else "*"
    files = [p for p in in_dir.glob(pattern) if p.is_file() and p.suffix.lower() in IMG_EXTS]

    ok, fail = 0, 0
    for p in files:
        rel = p.relative_to(in_dir)
        out_path = out_dir / rel
        try:
            process_one(p, out_path, crop_size=args.crop, out_size=args.out)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"[FAIL] {p} -> {e}")

    print(f"Done. Output: {out_dir}")
    print(f"Processed: {ok}, Failed: {fail}, Total: {len(files)}")


if __name__ == "__main__":
    main()
