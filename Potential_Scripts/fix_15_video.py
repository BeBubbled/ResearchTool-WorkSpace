import sys
import os
import shutil
import subprocess
import tempfile
import cv2

def run(cmd: list[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\nSTDOUT:\n" + p.stdout
            + "\n\nSTDERR:\n" + p.stderr
        )

def main():
    if len(sys.argv) != 3:
        print("Usage: python repeat_frame16_from_frame15_safe.py input.mp4 output.mp4")
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]

    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {in_path}")

    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()

    if len(frames) < 16:
        raise RuntimeError(f"Video has only {len(frames)} frames, need at least 16.")

    # 统一尺寸，取前16帧
    frames = [cv2.resize(f, (512, 512), interpolation=cv2.INTER_LINEAR) for f in frames[:16]]

    # 第16帧(索引15) = 第15帧(索引14)
    frames[15] = frames[14].copy()

    # 写出为 PNG 序列（BGR->PNG 不会变色）
    tmpdir = tempfile.mkdtemp(prefix="vid_frames_")
    try:
        for i, f in enumerate(frames):
            fp = os.path.join(tmpdir, f"{i:05d}.png")
            ok = cv2.imwrite(fp, f)
            if not ok:
                raise RuntimeError(f"Failed to write image: {fp}")

        # 用 ffmpeg 编码：严格 8fps、16帧、512x512、yuv420p
        # -frames:v 16 强制输出16帧
        # -r 8 控制输出fps
        run([
            "ffmpeg", "-y",
            "-framerate", "8",
            "-i", os.path.join(tmpdir, "%05d.png"),
            "-vf", "scale=512:512,fps=8",
            "-frames:v", "16",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", "8",
            "-an",
            out_path
        ])

        print(f"[OK] Wrote {out_path} (512x512, 16 frames, 8fps).")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    main()
