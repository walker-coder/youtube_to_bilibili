"""
将 VTT 字幕转换为 SRT 格式。
用法: python vtt_to_srt.py <输入.vtt> [输出.srt]
不指定输出时，生成同名的 .srt 文件。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _parse_vtt(path: Path) -> list[tuple[str, str]]:
    """解析 VTT：返回 [(时间行, 文本块), ...]。时间行含 --> 与可选的 position 等。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    cues = []
    i = 0
    while i < len(lines) and not re.match(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*", lines[i]):
        i += 1
    while i < len(lines):
        time_line = lines[i].strip()
        if not re.match(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*", time_line):
            i += 1
            continue
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        cues.append((time_line, "\n".join(text_lines)))
        while i < len(lines) and not lines[i].strip():
            i += 1
    return cues


def _vtt_time_to_srt(time_line: str) -> str:
    """将 VTT 时间行转为 SRT：只保留 00:00:00.000 --> 00:00:00.000，点改逗号。"""
    # 匹配 HH:MM:SS.mmm --> HH:MM:SS.mmm，忽略 position 等
    m = re.match(r"(\d{2}:\d{2}:\d{2})\.(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2})\.(\d{3})", time_line)
    if not m:
        return time_line.replace(".", ",")
    return f"{m.group(1)},{m.group(2)} --> {m.group(3)},{m.group(4)}"


def vtt_to_srt(vtt_path: str | Path, srt_path: str | Path | None = None) -> Path:
    vtt_path = Path(vtt_path)
    if not vtt_path.is_file():
        raise FileNotFoundError(f"找不到文件: {vtt_path}")

    if srt_path is None:
        srt_path = vtt_path.with_suffix(".srt")
    else:
        srt_path = Path(srt_path)

    cues = _parse_vtt(vtt_path)
    srt_lines = []
    for seq, (time_line, text_block) in enumerate(cues, start=1):
        srt_lines.append(str(seq))
        srt_lines.append(_vtt_time_to_srt(time_line))
        srt_lines.append(text_block)
        srt_lines.append("")

    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    print(f"已生成: {srt_path}（共 {len(cues)} 条）")
    return srt_path


def main():
    if len(sys.argv) < 2:
        print("用法: python vtt_to_srt.py <输入.vtt> [输出.srt]")
        sys.exit(1)
    vtt_path = Path(sys.argv[1]).resolve()
    srt_path = Path(sys.argv[2]).resolve() if len(sys.argv) >= 3 else None
    vtt_to_srt(vtt_path, srt_path)


if __name__ == "__main__":
    main()
