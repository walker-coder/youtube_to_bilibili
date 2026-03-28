"""
将中英文字幕嵌入视频，生成带双字幕轨的 MP4。
默认从 video_subs 目录读取视频与字幕，结果也保存到 video_subs。
用法: python embed_subs.py [视频] [英文字幕.vtt] [中文字幕.zh-Hans.vtt] [输出.mp4]
默认: video_subs/1.mp4 + 1.vtt + 1.zh-Hans.vtt → video_subs/1_subs.mp4

需要已安装 ffmpeg 并加入 PATH。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from paths_config import VIDEO_SUBS_DIR, ensure_video_subs_dir


def embed_subtitles(
    video_path: str | Path,
    en_vtt_path: str | Path,
    zh_vtt_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """将英文字幕、中文字幕嵌入视频，输出新 MP4。"""
    video_path = Path(video_path)
    en_vtt_path = Path(en_vtt_path)
    zh_vtt_path = Path(zh_vtt_path)

    for p, name in [(video_path, "视频"), (en_vtt_path, "英文字幕"), (zh_vtt_path, "中文字幕")]:
        if not p.is_file():
            raise FileNotFoundError(f"找不到{name}: {p}")

    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_subs{video_path.suffix}"
    else:
        output_path = Path(output_path)

    # 双字幕轨：先英后中，均转为 mov_text 以兼容 MP4
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-i", str(en_vtt_path),
        "-i", str(zh_vtt_path),
        "-map", "0:v", "-map", "0:a",
        "-map", "1:0", "-map", "2:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng",
        "-metadata:s:s:1", "language=chi",
        "-disposition:s:0", "default",
        str(output_path),
    ]
    print("执行:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stderr or result.stdout)
        raise RuntimeError(f"ffmpeg 退出码 {result.returncode}")
    print(f"已生成: {output_path}")
    return output_path


def main():
    ensure_video_subs_dir()
    if len(sys.argv) >= 4:
        video_path = Path(sys.argv[1])
        en_vtt = Path(sys.argv[2])
        zh_vtt = Path(sys.argv[3])
        out = Path(sys.argv[4]) if len(sys.argv) >= 5 else None
    else:
        video_path = VIDEO_SUBS_DIR / "1.mp4"
        en_vtt = VIDEO_SUBS_DIR / "1.vtt"
        zh_vtt = VIDEO_SUBS_DIR / "1.zh-Hans.vtt"
        out = VIDEO_SUBS_DIR / "1_subs.mp4"
    embed_subtitles(video_path, en_vtt, zh_vtt, out)


if __name__ == "__main__":
    main()
