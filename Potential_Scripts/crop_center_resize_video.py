#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffmpeg failed")
    return p

def process_one_video_ffmpeg(in_path: Path, out_path: Path, crop: int, out: int, offset_y: int, crf: int):
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # crop x,y: center crop then shift y by offset_y
    # x = (in_w - crop)/2
    # y = (in_h - crop)/2 + offset_y  (offset_y=-80 means upward)
    vf = (
        f"crop={crop}:{crop}:(in_w-{crop})/2:(in_h-{crop})/2+({offset_y}),"
        f"scale={out}:{out}:flags=lanczos,"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", "medium",
        "-an",  # no audio; remove this if you want to keep audio
        str(out_path),
    ]
    run(cmd)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", type=str, required=True)
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--out", type=int, default=512)
    ap.add_argument("--offset_y", type=int, default=-80)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args()

    in_dir = Path(args.input_dir).expanduser().resolve()
    if not in_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {in_dir}")

    out_dir = in_dir.parent / f"{in_dir.name}_resized"
    out_dir.mkdir(parents=True, exist_ok=True)

    pattern = "**/*" if args.recursive else "*"
    files = [p for p in in_dir.glob(pattern) if p.is_file() and p.suffix.lower() in VIDEO_EXTS]

    ok, fail = 0, 0
    for p in files:
        rel = p.relative_to(in_dir)
        out_path = (out_dir / rel).with_suffix(".mp4")  # 统一输出 mp4
        try:
            process_one_video_ffmpeg(p, out_path, args.crop, args.out, args.offset_y, args.crf)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"[FAIL] {p} -> {e}")

    print(f"Done. Output: {out_dir}")
    print(f"Processed: {ok}, Failed: {fail}, Total: {len(files)}")

if __name__ == "__main__":
    main()
