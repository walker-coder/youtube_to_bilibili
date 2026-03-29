#!/usr/bin/env python3
"""
在 Linux/macOS/Windows 上快速验证：ffmpeg + libass 能否用当前字体烧录中文（与 bilingual_subs_to_video 同源逻辑）。

用法（在项目根目录）:
  export BLOOMBREG_ASS_FONTNAME="Noto Sans CJK SC"   # 可选，与烧录流水线一致
  python3 test_cjk_font_subtitle.py
  python3 test_cjk_font_subtitle.py /tmp/out.mp4

输出默认: video_subs/_font_test_cjk.mp4
用播放器打开：若中文为方框，说明 fontconfig 未解析到该 Fontname；请 fc-list 核对名称后重设 BLOOMBREG_ASS_FONTNAME。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from paths_config import PROJECT_ROOT, ensure_video_subs_dir


def _fontname() -> str:
    env = (os.environ.get("BLOOMBREG_ASS_FONTNAME") or "").strip()
    if env:
        return env
    if sys.platform == "win32":
        return "Microsoft YaHei"
    return "Noto Sans CJK SC"


def _build_ass(font: str) -> str:
    # Style 行与 bilingual_subs_to_video 一致，仅保留一条测试字幕
    return "\n".join(
        [
            "[Script Info]",
            "Title: font test",
            "ScriptType: v4.00+",
            "PlayResX: 1920",
            "PlayResY: 1080",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font},64,&H00000000,&H00000000,&H0000FFFF,&H0000FFFF,-1,0,0,0,100,100,0,0,3,8,0,2,80,80,100,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            r"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{\an2\pos(960,1040)\fs64\c&H000000&}中文显示测试 简体\N{\fs56\c&H000000&}English line (if Chinese is tofu, font missing)",
            "",
        ]
    )


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if out is None:
        ensure_video_subs_dir()
        out = PROJECT_ROOT / "video_subs" / "_font_test_cjk.mp4"

    font = _fontname()
    ass_content = _build_ass(font)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        suffix=".ass",
        delete=False,
    ) as f:
        f.write(ass_content)
        ass_path = Path(f.name)

    try:
        out_abs = out.resolve()
        ap = ass_path.resolve().as_posix()
        vf = f"subtitles={ap}:charenc=UTF-8"
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#1a1a1a:s=1280x720:d=5",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            "5",
            str(out_abs),
        ]

        print(f"测试字体名（BLOOMBREG_ASS_FONTNAME 或默认）: {font!r}")
        print(f"ASS 临时文件: {ass_path}")
        print(f"输出: {out_abs}")
        print("执行 ffmpeg …")
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if r.returncode != 0:
            print("ffmpeg 失败。", file=sys.stderr)
            sys.exit(r.returncode)
        print("完成。请用播放器打开输出文件：中文应清晰，不应为方框。")
        print("提示: fc-list | grep -i noto   # 核对字体在系统中的名称")
    finally:
        ass_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
