#!/usr/bin/env python3
import argparse
from pathlib import Path
import cv2


def extract_frames(video_path: Path, output_dir: Path):
    print(f"[INFO] 正在处理: {video_path}")

    cap = cv2.VideoCapture(str(video_path))

    if not cap.isOpened():
        print(f"[ERROR] 无法打开视频: {video_path}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps          = cap.get(cv2.CAP_PROP_FPS)

    print(f"[INFO] 视频信息: {total_frames} 帧, 分辨率 {width}x{height}, FPS={fps}")

    output_dir.mkdir(parents=True, exist_ok=True)

    frame_idx = 0
    saved = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        out_path = output_dir / f"{frame_idx}.png"

        # 保存失败时给出提示
        ok = cv2.imwrite(str(out_path), frame)
        if not ok:
            print(f"[ERROR] 无法写入文件: {out_path}")
            break

        frame_idx += 1
        saved += 1

    cap.release()

    print(f"[INFO] 完成: 输出 {saved} 张图片\n")


def process_folder(root_dir: Path):
    mp4_files = list(root_dir.rglob("*.mp4"))
    if not mp4_files:
        print(f"[WARN] 在 {root_dir} 下未找到 mp4 文件")
        return

    for video in mp4_files:
        output_dir = video.with_suffix("")  # same name folder
        extract_frames(video, output_dir)


def main():
    parser = argparse.ArgumentParser(
        description="遍历文件夹并逐帧拆解 mp4 为 PNG"
    )
    parser.add_argument("input_dir", type=str, help="输入文件夹路径")
    args = parser.parse_args()

    root_dir = Path(args.input_dir).expanduser().resolve()

    if not root_dir.is_dir():
        print(f"[ERROR] 输入路径不是目录: {root_dir}")
        return

    process_folder(root_dir)


if __name__ == "__main__":
    main()
