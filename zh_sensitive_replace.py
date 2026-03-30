"""
简体字幕敏感词替换：按对照表对 .vtt 中字幕文本做就地替换（不改时间轴）。

对照表：项目根目录 zh_sensitive_word_map.json（JSON 对象，键为原文片段，值为替换文）。
也可用环境变量 ZH_SENSITIVE_WORD_MAP_JSON 指向其它路径；文件不存在或为空对象时不替换。

同键多次出现时均替换；多键重叠时按「键长度从长到短」依次替换，减少短词抢先破坏长词的情况。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from paths_config import PROJECT_ROOT

_TIME_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*")


def default_zh_sensitive_map_path() -> Path:
    env = (os.environ.get("ZH_SENSITIVE_WORD_MAP_JSON") or "").strip()
    if env:
        return Path(env)
    return PROJECT_ROOT / "zh_sensitive_word_map.json"


def load_zh_sensitive_map(path: Path | None = None) -> dict[str, str]:
    p = path or default_zh_sensitive_map_path()
    if not p.is_file():
        return {}
    raw = p.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"敏感词表须为 JSON 对象（键值对字符串）: {p}")
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        out[k] = v
    return out


def apply_zh_sensitive_map_to_text(text: str, mapping: dict[str, str]) -> str:
    if not mapping or not text:
        return text
    for old in sorted(mapping.keys(), key=len, reverse=True):
        new = mapping[old]
        if old:
            text = text.replace(old, new)
    return text


def _parse_vtt_header_and_cues(lines: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    i = 0
    while i < len(lines) and not _TIME_LINE_RE.match(lines[i].strip()):
        i += 1
    header = lines[:i]
    cues: list[tuple[str, str]] = []
    while i < len(lines):
        if not _TIME_LINE_RE.match(lines[i].strip()):
            i += 1
            continue
        raw_time = lines[i]
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        cues.append((raw_time, "\n".join(text_lines)))
        while i < len(lines) and not lines[i].strip():
            i += 1
    return header, cues


def _write_vtt(path: Path, header: list[str], cues: list[tuple[str, str]]) -> None:
    out: list[str] = []
    out.extend(header)
    for raw_time, block in cues:
        out.append(raw_time.rstrip("\r\n"))
        if block:
            out.extend(block.split("\n"))
        out.append("")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_zh_sensitive_replacements_to_vtt(
    vtt_path: str | Path,
    *,
    mapping: dict[str, str] | None = None,
) -> Path:
    """对简体 VTT 就地写入并返回路径；无规则或文件无字幕块时原样返回。"""
    vtt_path = Path(vtt_path)
    if not vtt_path.is_file():
        raise FileNotFoundError(f"找不到字幕: {vtt_path}")
    m = load_zh_sensitive_map() if mapping is None else mapping
    if not m:
        return vtt_path

    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    lines = raw.splitlines()
    header, cues = _parse_vtt_header_and_cues(lines)
    if not cues:
        return vtt_path

    new_cues: list[tuple[str, str]] = []
    for raw_time, block in cues:
        new_cues.append((raw_time, apply_zh_sensitive_map_to_text(block, m)))
    _write_vtt(vtt_path, header, new_cues)
    print(f"  已对简体字幕做敏感词替换（{len(m)} 条规则）。")
    return vtt_path
