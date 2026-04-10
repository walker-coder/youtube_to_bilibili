"""
投稿后轮询哔哩哔哩审核状态：若「已退回」则按时间轴剪片并替换稿件；退回说明里若含**多段**时间，`extract_time_ranges_from_text` 会全部解析，`ffmpeg_remove_time_ranges` 一次剪除多段。**可再次退回**，则对最新本地成片重复解析→剪片→替换，直到「审核通过」或单轮超时或达到最大轮数。

依赖 upload_bilibili 的 Cookie 配置、ffmpeg、与 bilibili-api。

环境变量（可选）：
  BILIBILI_REVIEW_POLL_INTERVAL_SEC  轮询间隔秒数，默认 30
  BILIBILI_REVIEW_MAX_WAIT_SEC       每一轮（从轮询到通过/退回/超时）最长等待秒数，默认 7200；替换后会开启新一轮轮询
  BILIBILI_REVIEW_MAX_REPLACE_ROUNDS  最多剪片替换次数（含多轮退回），默认 20，防止无限循环
  解析不到时间段时：终端会打印接口摘要（顶层键、可能含退回说明的嵌套字段节选、合并 JSON 的首尾截断），并写入 logs/bilibili_reject_raw_<BV>_<时间>.txt 全文。
  解析区间时优先从疑似退回正文字段提取，并对整段 JSON 使用 strict 匹配，避免裸「时:分:秒-时:分:秒」误命中无关字段（与网页不一致）。
  退回时会在终端输出「哔哩哔哩审核内容」（整理自 archive 与优先字段；过长截断）。
  退回时**必定**将 upload_args 与 page_state 的完整 JSON 写入 logs/bilibili_review_api_<BV>_<时间>.json，便于对照网页扩展正则。
  BILIBILI_REVIEW_PRINT_FULL_JSON=1 时同时将完整 JSON 打印到终端（体积大）。
  BILIBILI_REVIEW_RECUT_PAD_SEC  对解析出的每段删除区间左右各扩展的秒数（默认 1.0）；设为 0 关闭。
  BILIBILI_UPLOAD_LINE  替换稿件时走 UPOS 上传，若报「获取 upload_id 错误」可指定线路：bda2 / qn / ws / bldsa（小写）。
                          不设时依次尝试：自动测速、bda2、qn、ws。

单独补跑（已上传过、未走流水线步骤 5 时）：
  python bilibili_review.py BV1DhX1BVESJ
  python bilibili_review.py BV1DhX1BVESJ video_subs/yt_xxx_bilingual.mp4
  第二参数省略时自动选 video_subs 下最新 *_bilingual.mp4

仅替换视频（本地已剪好 recut / 成片，只上传覆盖分 P）：
  python bilibili_review.py BV1xxxxxxxxxx video_subs/yt_xxx_bilingual_recut.mp4 --replace-only
  # 或：python bilibili_review.py BV1xxxxxxxxxx --replace-only --video video_subs/yt_xxx_bilingual_recut.mp4
  # 勿写成 BV号 --replace-only 路径（若脚本未识别 --replace-only，路径会被当成多余参数报错）

推荐流程（先查原因 → 本地改片 → 再上传）：
  1) python bilibili_review.py BV1xxxxxxxxxx --query-reject
     （只查当前审核状态与退回说明、解析时间段；不剪片、不上传）
  2) 按说明在本地修改/重编码成片后
  3) python bilibili_review.py BV1xxxxxxxxxx video_subs/你的成片.mp4 --replace-only
     需要替换后继续自动轮询再加: --resume-review

仅「替换并接着轮询」一条命令（不先查原因时）：
  python bilibili_review.py BV1xx video_subs/yt_xxx_bilingual_recut.mp4 --replace-only --resume-review

第一个参数必须是哔哩哔哩「稿件 BV 号」（创作中心或视频页地址里的 BVxxxxxxxx，共 12 位），
不要使用 YouTube 视频 ID（如 yt_xxxx_bilingual 里的 11 位 ID），否则接口会返回 -400。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 退回说明里常见：【01:29:19-01:31:06】、无括号、全角冒号/破折号、仅分:秒 等（见 extract_time_ranges_from_text）
_DASH = r"[-–—~～]"

# 标准 BV 号为 BV + 10 位（共 12 字符）；YouTube 视频 id 常为 11 位 [A-Za-z0-9_-]
_RE_YOUTUBE_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


def _looks_like_youtube_video_id(s: str) -> bool:
    return bool(_RE_YOUTUBE_ID.fullmatch(s.strip()))


def normalize_bvid_cli_arg(raw: str) -> str:
    """
    解析命令行第一个参数为合法 BV 号。
    若误传 YouTube 11 位 id，给出明确错误，避免被拼成 BVxxxxxxxxxxx 导致接口 -400。
    """
    s = raw.strip()
    if not s.upper().startswith("BV"):
        if _looks_like_youtube_video_id(s):
            raise ValueError(
                "第一个参数看起来像 YouTube 视频 ID（11 位），不是哔哩哔哩稿件 BV 号。\n"
                "请到创作中心打开该稿件，复制完整 BV 号（BV + 10 位，共 12 位），"
                "不要使用本地文件名里的 yt_xxxx 中的那段 ID。"
            )
        s = "BV" + s
    if len(s) != 12:
        raise ValueError(
            f"BV 号长度应为 12（例如 BV1xxxxxxxxxx），当前为 {len(s)} 位：{s!r}。\n"
            "请从哔哩哔哩视频页或创作中心复制完整 BV。"
        )
    return s


def _parse_time_token(hms: str) -> float:
    """
    解析 ASS/退回说明里的时间：支持 H:MM:SS、HH:MM:SS，以及无小时的 MM:SS（按 分:秒）。
    """
    s = hms.strip().replace("\uFF1A", ":").replace("：", ":")
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    raise ValueError(f"无法解析时间: {hms!r}")


def _ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {r.stderr or r.stdout}")
    return float((r.stdout or "").strip())


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not ranges:
        return []
    srt = sorted((a, b) for a, b in ranges if b > a)
    out: list[tuple[float, float]] = []
    for a, b in srt:
        if not out or a > out[-1][1]:
            out.append((a, b))
        else:
            out[-1] = (out[-1][0], max(out[-1][1], b))
    return out


def _expand_remove_ranges_with_padding(
    ranges: list[tuple[float, float]],
    *,
    pad_sec: float,
    duration_sec: float,
) -> list[tuple[float, float]]:
    """
    对每段删除区间左右各扩展 pad_sec 秒（应对退回里时间过短），再合并重叠，并限制在 [0, duration_sec]。
    """
    merged = _merge_ranges(ranges)
    if pad_sec <= 0:
        return merged
    padded: list[tuple[float, float]] = []
    for a, b in merged:
        a2 = max(0.0, a - pad_sec)
        b2 = min(float(duration_sec), b + pad_sec)
        if b2 > a2:
            padded.append((a2, b2))
    return _merge_ranges(padded)


def _format_seconds_as_hms(seconds: float) -> str:
    """终端展示用：秒 → 与退回说明相近的 H:MM:SS 或 M:SS。"""
    t = max(0.0, float(seconds))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    if h > 0:
        return f"{h:d}:{m:02d}:{s:06.3f}".rstrip("0").rstrip(".")
    return f"{m:d}:{s:06.3f}".rstrip("0").rstrip(".")


def ffmpeg_remove_time_ranges(input_path: Path, output_path: Path, ranges: list[tuple[float, float]]) -> None:
    """删除视频中若干时间段（秒），保留其余部分；多段用 concat demuxer。"""
    merged = _merge_ranges(ranges)
    if not merged:
        shutil.copy2(input_path, output_path)
        return
    dur = _ffprobe_duration(input_path)
    keep: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in merged:
        if cur < a:
            keep.append((cur, a))
        cur = max(cur, b)
    if cur < dur:
        keep.append((cur, dur))
    if not keep:
        raise RuntimeError("根据退回区间计算后无可保留片段")

    if len(keep) == 1:
        s, e = keep[0]
        t = e - s
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(s),
            "-i",
            str(input_path),
            "-t",
            str(t),
            "-c",
            "copy",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 剪切失败: {r.stderr or r.stdout}")
        return

    tmpd = Path(tempfile.mkdtemp(prefix="bili_recut_"))
    try:
        parts: list[Path] = []
        for i, (s, e) in enumerate(keep):
            t = e - s
            p = tmpd / f"part{i}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(s),
                "-i",
                str(input_path),
                "-t",
                str(t),
                "-c",
                "copy",
                str(p),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg 分段失败: {r.stderr or r.stdout}")
            parts.append(p)
        list_file = tmpd / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{x.as_posix()}'" for x in parts),
            encoding="utf-8",
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg concat 失败: {r.stderr or r.stdout}")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _reject_text_key_hint(k: str) -> bool:
    """字段名是否像退回/审核说明（用于优先从接口里摘出与网页一致的文案）。"""
    kl = str(k).lower()
    for h in (
        "reject",
        "reason",
        "remark",
        "message",
        "fail",
        "audit",
        "退回",
        "意见",
        "说明",
        "违规",
        "problem",
        "violation",
        "issue",
        "审核",
        "问题",
    ):
        if h.lower() in kl or h in str(k):
            return True
    return False


def _value_looks_like_violation_notice(s: str) -> bool:
    """正文是否像「违规时间点」类退回说明（避免把整段 JSON 当正文乱匹配）。"""
    if not s or not isinstance(s, str) or len(s) < 8:
        return False
    if ("违规" in s or "退回" in s or "问题" in s or "片段" in s or "时间点" in s) and (
        "P1(" in s or "【" in s or re.search(r"\d{1,2}:\d{2}:\d{2}", s)
    ):
        return True
    if "P1(" in s and re.search(r"\d{1,2}:\d{2}:\d{2}", s):
        return True
    return False


def _collect_priority_reject_text(
    archive_json: dict, page_state: dict | None
) -> str:
    """
    从接口 JSON 中优先抽出与创作中心网页相近的退回说明字符串（不整段 dump JSON）。
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = (s or "").strip()
        if len(s) < 5:
            return
        if s in seen:
            return
        seen.add(s)
        parts.append(s)

    def walk(obj, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                prefer = _reject_text_key_hint(str(k))
                if isinstance(v, str):
                    if (prefer and _might_contain_time_hint(v)) or _value_looks_like_violation_notice(
                        v
                    ):
                        add(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:100]:
                walk(v, depth + 1)

    walk(archive_json, 0)
    if page_state is not None:
        walk(page_state, 0)
    return "\n".join(parts)


def _gather_strings_for_review_display(
    obj, out: list[str], path: str = "", depth: int = 0
) -> None:
    """收集嵌套 JSON 中含审核/退回/违规等关键词的字符串，供终端展示。"""
    if depth > 16 or len(out) >= 40:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and len(v) > 12:
                if any(
                    x in v
                    for x in (
                        "审核",
                        "退回",
                        "违规",
                        "稿件",
                        "锁定",
                        "开放浏览",
                        "未通过",
                        "问题",
                        "时间点",
                    )
                ):
                    out.append(f"{p}: {v.strip()[:4000]}")
            else:
                _gather_strings_for_review_display(v, out, p, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            _gather_strings_for_review_display(v, out, f"{path}[{i}]", depth + 1)


def format_bilibili_review_content(
    archive_json: dict, page_state: dict | None
) -> str:
    """将接口返回整理为可读多行文本（不含整段 JSON dump），供终端或日志。"""
    lines: list[str] = []
    arc = archive_json.get("archive")
    if isinstance(arc, dict):
        st = arc.get("state")
        if st is not None:
            lines.append(f"archive.state = {st!r}")
        for k in (
            "reject_reason",
            "reject_reason_v2",
            "reject_reason_str",
            "reject_reason_web",
            "reason",
            "reject_message",
            "message",
            "audit_msg",
            "audit_reason",
        ):
            v = arc.get(k)
            if isinstance(v, str) and v.strip():
                lines.append(f"archive.{k}:\n{v.strip()}")
    pri = _collect_priority_reject_text(archive_json, page_state)
    if pri.strip():
        if lines:
            lines.append("---")
        lines.append("【退回说明（接口优先字段汇总）】")
        lines.append(pri.strip())
    if not lines:
        nested: list[str] = []
        _gather_strings_for_review_display(archive_json, nested, "archive_json", 0)
        if page_state is not None:
            _gather_strings_for_review_display(page_state, nested, "page_state", 0)
        if nested:
            lines.extend(nested[:30])
    if not lines:
        return (
            "(未从接口中解析出可读的审核说明；请查看 logs/bilibili_reject_raw_*.txt）"
        )
    return "\n".join(lines)


def print_bilibili_review_content(archive_json: dict, page_state: dict | None) -> None:
    """在终端打印哔哩哔哩返回的可读审核内容（过长时截断）。"""
    block = format_bilibili_review_content(archive_json, page_state)
    print("  【哔哩哔哩审核内容】")
    all_lines = block.splitlines()
    max_lines = 120
    for i, line in enumerate(all_lines):
        if i >= max_lines:
            print(
                f"  … 共 {len(all_lines)} 行，此处仅展示前 {max_lines} 行；"
                "完整接口见 logs/bilibili_reject_raw_*.txt"
            )
            break
        print(f"  {line}")


def extract_time_ranges_from_text(
    text: str, *, strict: bool = False
) -> list[tuple[float, float]]:
    """
    从退回说明 / API JSON 文本中提取「需删除」时间段（秒）。
    支持：【HH:MM:SS-HH:MM:SS】、B 站常见 **P1(01:26:33-01:27:10)**（分 P + 圆括号）、
    半角 []、无括号、全角冒号、多种破折号、MM:SS、可选毫秒、从…到/至、纯「秒」区间、HTML 等。
    若起止时刻相同（如 P1(01:26:33-01:26:33)），按剪除 1 秒处理。
    多条重叠或相邻区间（如多条 P1(01:26:32-01:26:33)、P1(01:26:32-01:26:34)）在返回前会合并为一条再剪除。

    strict=True：不启用「正文里无括号的两段 HMS-HMS / 无括号 MM:SS-MM:SS」，避免对整段 json.dumps
    误匹配到无关数字串（与网页展示的删除区间不一致）。
    """
    # 创作中心文案里偶见 <br>；折行可能导致「时刻-时刻」被拆开，先规整
    t = re.sub(r"<[^>]+>", " ", text)
    t = t.replace("\uFF1A", ":").replace("：", ":")
    t = re.sub(r"\s+", " ", t)

    out: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()

    def add_pair(a: str, b: str) -> None:
        try:
            sa, sb = _parse_time_token(a), _parse_time_token(b)
            if sb <= sa:
                # B 站退回里常见 P1(01:26:33-01:26:33) 起止相同，按该时刻起 1 秒剪除
                sb = sa + 1.0
            key = (round(sa, 3), round(sb, 3))
            if key not in seen:
                seen.add(key)
                out.append((sa, sb))
        except (ValueError, IndexError, OSError):
            return

    def add_seconds_pair(a: str, b: str) -> None:
        try:
            sa, sb = float(a.strip()), float(b.strip())
            if sb <= sa or sa < 0:
                return
            key = (round(sa, 3), round(sb, 3))
            if key not in seen:
                seen.add(key)
                out.append((sa, sb))
        except (ValueError, TypeError):
            return

    # 时:分:秒 可带小数秒；分:秒 可带小数
    HMS = r"\d{1,2}:\d{2}:\d{2}(?:\.\d+)?"
    MMSS = r"\d{1,2}:\d{2}(?:\.\d+)?"
    # 连接符含全角横线、波浪线（与 _DASH 一致）
    conn = _DASH

    patterns: list[re.Pattern[str]] = [
        # 您的视频【P1(01:26:33-01:27:10)】【内容】… — 分 P + 半角圆括号包裹
        re.compile(rf"P\d+\(\s*({HMS})\s*{conn}\s*({HMS})\s*\)"),
        # 【01:29:19-01:31:06】、带毫秒
        re.compile(rf"[【\[]({HMS}){conn}({HMS})[】\]]"),
        # 正文里无括号，两段完整时刻（整段 JSON 时易误匹配，strict 时跳过）
        re.compile(rf"({HMS})\s*{conn}\s*({HMS})"),
        # 仅 分:秒（短片段）
        re.compile(rf"[【\[]({MMSS}){conn}({MMSS})[】\]]"),
        re.compile(
            rf"(?<![\d:])({MMSS})\s*{conn}\s*({MMSS})(?![\d:])"
        ),
        # 01:29:19 至 / 到 01:31:06
        re.compile(rf"({HMS})\s*至\s*({HMS})"),
        re.compile(rf"({HMS})\s*到\s*({HMS})"),
        re.compile(rf"({MMSS})\s*至\s*({MMSS})"),
        re.compile(rf"({MMSS})\s*到\s*({MMSS})"),
        # 从 01:29:19 到 01:31:06
        re.compile(rf"(?:从|自)\s*({HMS})\s*(?:到|至)\s*({HMS})"),
        re.compile(rf"(?:从|自)\s*({MMSS})\s*(?:到|至)\s*({MMSS})"),
        # 中间点号连接（少数模板）
        re.compile(rf"({HMS})\s*[·•]\s*({HMS})"),
    ]
    if strict:
        # 去掉索引 2、4：裸 HMS-HMS、裸 MM:SS-MM:SS
        patterns = [p for i, p in enumerate(patterns) if i not in (2, 4)]

    for pat in patterns:
        for m in pat.finditer(t):
            add_pair(m.group(1), m.group(2))

    # 纯秒数区间（须带「秒」字，避免误匹配 JSON 里其它数字）
    sec_patterns = [
        re.compile(rf"(\d+(?:\.\d+)?)\s*秒\s*{conn}\s*(\d+(?:\.\d+)?)\s*秒"),
        re.compile(rf"(\d+(?:\.\d+)?)\s*秒\s*至\s*(\d+(?:\.\d+)?)\s*秒"),
        re.compile(rf"(\d+(?:\.\d+)?)\s*秒\s*到\s*(\d+(?:\.\d+)?)\s*秒"),
    ]
    for pat in sec_patterns:
        for m in pat.finditer(t):
            add_seconds_pair(m.group(1), m.group(2))

    return _merge_ranges(out)


def extract_time_ranges_for_review(
    archive_json: dict, page_state: dict | None
) -> tuple[list[tuple[float, float]], str]:
    """
    供轮询退回使用：对「优先退回字段」与「整段接口 JSON」分别做 strict 解析后**合并**，
    避免某一字段只含部分 P1、另一字段含完整「违规时间点」时提前返回漏段。
    若无结果再对整段 blob 做宽松兜底。
    """
    priority = _collect_priority_reject_text(archive_json, page_state)
    blob = _review_text_blob(archive_json, page_state)
    rp = (
        extract_time_ranges_from_text(priority, strict=True)
        if priority.strip()
        else []
    )
    rb = extract_time_ranges_from_text(blob, strict=True)
    merged = _merge_ranges(rp + rb)
    if merged:
        src = "优先字段 + 合并接口 JSON（strict 合并）"
        return merged, src
    r = extract_time_ranges_from_text(blob, strict=False)
    if r:
        return (
            r,
            "合并接口 JSON（宽松匹配，可能与网页不一致；请核对 logs/bilibili_reject_raw_*.txt）",
        )
    return [], "无"


def _review_text_blob(archive_json: dict, page_state: dict | None) -> str:
    parts = [json.dumps(archive_json, ensure_ascii=False)]
    if page_state is not None:
        parts.append(json.dumps(page_state, ensure_ascii=False))
    return "\n".join(parts)


def _write_review_api_json(
    bvid: str, archive_json: dict, page_state: dict | None
) -> Path:
    """退回时写入完整接口响应（pretty JSON），供对照网页与扩展正则。"""
    from paths_config import PROJECT_ROOT

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = log_dir / f"bilibili_review_api_{bvid}_{stamp}.json"
    payload = {
        "bvid": bvid,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "upload_args_response": archive_json,
        "page_state_initial": page_state,
    }
    fp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return fp


def _maybe_print_full_review_json(fp: Path) -> None:
    if os.environ.get("BILIBILI_REVIEW_PRINT_FULL_JSON", "").strip() not in (
        "1",
        "true",
        "yes",
    ):
        return
    try:
        print("  ----- BILIBILI_REVIEW_PRINT_FULL_JSON -----")
        print(fp.read_text(encoding="utf-8"))
        print("  ----- end -----")
    except OSError as e:
        print(f"  （读取 {fp} 失败: {e}）")


def _write_reject_debug_text(bvid: str, text: str) -> Path:
    """解析不到时间段时写出接口合并文本，便于对照创作中心改正则。"""
    from paths_config import PROJECT_ROOT

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = log_dir / f"bilibili_reject_raw_{bvid}_{stamp}.txt"
    fp.write_text(text, encoding="utf-8")
    return fp


def _might_contain_time_hint(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    return bool(
        re.search(r"\d{1,2}\s*[:：]\s*\d{2}", s)
        or "秒" in s
        or "退回" in s
        or "问题" in s
        or "删除" in s
        or "片段" in s
    )


def _gather_nested_string_fields(obj, out: list[str], prefix: str = "", depth: int = 0) -> None:
    """收集嵌套 JSON 里可能含退回说明/时间轴的字符串，便于终端展示。"""
    if depth > 14 or len(out) >= 40:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, str) and _might_contain_time_hint(v):
                snippet = v.strip().replace("\n", " ")
                if len(snippet) > 800:
                    snippet = snippet[:800] + "…"
                out.append(f"  {p}: {snippet}")
            else:
                _gather_nested_string_fields(v, out, p, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:30]):
            _gather_nested_string_fields(v, out, f"{prefix}[{i}]", depth + 1)


def _print_reject_response_preview(
    bvid: str,
    archive_json: dict,
    page_state: dict | None,
    blob: str,
) -> None:
    """
    解析时间段失败时打印 B 站返回摘要，便于对照创作中心或把终端片段发给他人扩展正则。
    """
    head_n, tail_n = 4500, 4500
    print()
    print("=" * 72)
    print(f"【退回说明 · 接口预览】 稿件 {bvid}")
    print(f"合并文本总长度: {len(blob)} 字符（完整内容已写入 logs 下 txt）")
    arc = archive_json.get("archive")
    if isinstance(arc, dict):
        st = arc.get("state")
        print(f"archive.state（若存在）: {st!r}")
    # 优先列出常见顶层键，便于判断结构是否变化
    print(f"archive_json 顶层键: {list(archive_json.keys())}")
    if page_state is not None and isinstance(page_state, dict):
        print(f"page_state 顶层键: {list(page_state.keys())[:30]}{'…' if len(page_state) > 30 else ''}")

    nested: list[str] = []
    _gather_nested_string_fields(archive_json, nested, "archive_json", 0)
    if page_state is not None:
        _gather_nested_string_fields(page_state, nested, "page_state", 0)
    if nested:
        print("\n--- 嵌套字段中可能含时间/退回说明的字符串（节选）---")
        for line in nested[:25]:
            print(line)
        if len(nested) > 25:
            print(f"  … 共 {len(nested)} 条匹配，其余见完整日志文件 …")

    print("\n--- 合并 JSON 字符串（首尾截断，用于搜时间格式）---")
    if len(blob) <= head_n + tail_n + 120:
        print(blob)
    else:
        print(blob[:head_n])
        print(
            f"\n... （中间省略 {len(blob) - head_n - tail_n} 字符；完整见 logs/bilibili_reject_raw_*.txt）...\n"
        )
        print(blob[-tail_n:])
    print("=" * 72)
    print()


def classify_review(archive_json: dict, page_state: dict | None) -> str:
    """返回 passed | rejected | pending"""
    text = _review_text_blob(archive_json, page_state)
    if "已退回" in text:
        return "rejected"
    if "退回" in text and ("稿件" in text or "审核" in text or "问题" in text):
        return "rejected"
    if "审核通过" in text or "开放浏览" in text:
        return "passed"
    arc = archive_json.get("archive") or {}
    if arc.get("state") == -40:
        return "rejected"
    if arc.get("state") == 0 and "通过" in text:
        return "passed"
    return "pending"


async def _fetch_review_data(bvid: str, credential) -> tuple[dict, dict | None]:
    from bilibili_api.utils.initial_state import get_initial_state
    from bilibili_api.utils.network import Api
    from bilibili_api.utils.utils import get_api

    api = get_api("video_uploader")["upload_args"]
    archive_json = await Api(**api, credential=credential).update_params(bvid=bvid).result
    url = f"https://member.bilibili.com/platform/upload/video/frame?type=edit&bvid={bvid}"
    try:
        page_state, _ = await get_initial_state(url, credential=credential, strict=False)
    except Exception:
        page_state = None
    return archive_json, page_state


def _replace_upload_line_attempts() -> list[tuple[str, object]]:
    """(日志标签, Lines|None)。None 表示 _choose_line 内联测速。"""
    from bilibili_api.video_uploader import Lines

    raw = (os.environ.get("BILIBILI_UPLOAD_LINE") or "").strip().lower()
    m = {
        "bda2": Lines.BDA2,
        "qn": Lines.QN,
        "ws": Lines.WS,
        "bldsa": Lines.BLDSA,
    }
    if raw:
        if raw not in m:
            print(
                f"  警告: BILIBILI_UPLOAD_LINE={raw!r} 无效（应为 bda2/qn/ws/bldsa），改用自动测速"
            )
            return [("probe", None)]
        return [(raw, m[raw])]
    return [("probe", None), ("bda2", Lines.BDA2), ("qn", Lines.QN), ("ws", Lines.WS)]


def _parse_tags(tag_field) -> list[str]:
    if tag_field is None:
        return ["转载"]
    if isinstance(tag_field, list):
        return [str(x).strip() for x in tag_field if str(x).strip()][:10] or ["转载"]
    s = str(tag_field).strip()
    if not s:
        return ["转载"]
    return [t.strip() for t in s.split(",") if t.strip()][:10]


class _ReplaceVideoUploader:
    """上传新分 P 后走编辑 submit（/x/vu/web/edit），不新建稿件。"""

    def __init__(self, bvid: str, credential, meta, page, line):
        from bilibili_api.video_uploader import VideoUploader

        self.bvid = bvid
        self.credential = credential
        self.page = page
        self.line = line
        self._vu = VideoUploader([page], meta=meta, credential=credential, cover="")

    async def start(self) -> dict:
        from copy import deepcopy

        import time

        from bilibili_api.utils.aid_bvid_transformer import bvid2aid
        from bilibili_api.utils.network import Api
        from bilibili_api.utils.utils import get_api
        from bilibili_api.video_uploader import VideoMeta, VideoUploaderEvents

        self._vu.line = self.line
        data = await self._vu._upload_page(self.page)
        cover_url = await self._vu._upload_cover()
        m = self._vu.meta
        meta = deepcopy(m.__dict__() if isinstance(m, VideoMeta) else dict(m))
        meta["cover"] = cover_url
        meta["videos"] = [
            {
                "title": self.page.title,
                "desc": self.page.description,
                "filename": data["filename"],
                "cid": data["cid"],
            }
        ]
        meta["csrf"] = self.credential.bili_jct
        meta["aid"] = bvid2aid(self.bvid)
        meta["bvid"] = self.bvid
        meta["new_web_edit"] = 1
        api = get_api("video_uploader")["edit"]
        params = {"csrf": self.credential.bili_jct, "t": int(time.time())}
        headers = {
            "content-type": "application/json;charset=UTF-8",
            "referer": "https://member.bilibili.com",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        resp = await Api(**api, credential=self.credential, no_csrf=True, json_body=True).update_params(
            **params
        ).update_data(**meta).update_headers(**headers).result
        self._vu.dispatch(VideoUploaderEvents.COMPLETED.value, resp)
        return resp


async def _replace_video_edit(
    bvid: str,
    new_video_path: Path,
    credential,
) -> dict:
    from bilibili_api.exceptions.ApiException import ApiException
    from bilibili_api.utils.network import Api
    from bilibili_api.utils.utils import get_api
    from bilibili_api.video_uploader import VideoMeta, VideoUploaderPage, _choose_line

    from upload_bilibili import _resolve_cover_path

    api = get_api("video_uploader")["upload_args"]
    old = await Api(**api, credential=credential).update_params(bvid=bvid).result
    arc = old["archive"]
    v0 = old["videos"][0]
    cover_path, _ = _resolve_cover_path(new_video_path)

    meta = VideoMeta(
        tid=int(arc["tid"]),
        title=str(arc["title"])[:80],
        desc=str(arc.get("desc", ""))[:2000],
        cover=str(cover_path),
        tags=_parse_tags(arc.get("tag")),
        original=int(arc.get("copyright", 2)) == 1,
        source=(arc.get("source") or "")[:200] if int(arc.get("copyright", 2)) != 1 else None,
    )

    page = VideoUploaderPage(
        str(new_video_path),
        title=str(v0.get("title") or arc.get("title", ""))[:80],
        description=str(v0.get("desc") or arc.get("desc", ""))[:2000],
    )

    attempts = _replace_upload_line_attempts()
    for idx, (label, line_enum) in enumerate(attempts):
        line = await _choose_line(line_enum)
        print(f"  替换稿件视频：上传线路={label}")
        uploader = _ReplaceVideoUploader(bvid, credential, meta, page, line)
        try:
            return await uploader.start()
        except ApiException as e:
            err_s = str(e)
            is_uid = "upload_id" in err_s or "获取 upload_id" in err_s
            if is_uid and idx < len(attempts) - 1:
                print(f"  UPOS 预上传失败，换线重试… ({err_s[:160]})")
                await asyncio.sleep(3.0 + idx * 2.0)
                continue
            raise


def _credential():
    from bilibili_api import Credential

    sess = os.environ.get("BILIBILI_SESSDATA", "").strip()
    jct = os.environ.get("BILIBILI_BILI_JCT", "").strip()
    if not sess or not jct:
        raise RuntimeError("缺少 BILIBILI_SESSDATA / BILIBILI_BILI_JCT")
    buvid = os.environ.get("BILIBILI_BUVID3", "").strip() or None
    dede = os.environ.get("BILIBILI_DEDEUSERID", "").strip() or None
    return Credential(sessdata=sess, bili_jct=jct, buvid3=buvid, dedeuserid=dede)


async def poll_and_repair_rejected(
    bvid: str,
    bilingual_mp4: Path,
) -> None:
    """
    轮询至通过；若退回则剪片替换，并可能对替换后的稿件再次轮询（多轮退回直到通过或达上限）。
    每轮剪片输入为「当前线上稿件对应的本地文件」：首次为 bilingual_mp4，之后为上一轮生成的 *_recut.mp4。
    每轮成功写出新的 *_recut.mp4 后，会删除上一轮的中间 recut 文件（保留最初的 *_bilingual.mp4）。
    """
    from upload_bilibili import _load_local_env

    _load_local_env()
    credential = _credential()
    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期")

    interval = float(os.environ.get("BILIBILI_REVIEW_POLL_INTERVAL_SEC", "30"))
    max_wait = float(os.environ.get("BILIBILI_REVIEW_MAX_WAIT_SEC", "7200"))
    max_rounds = int(os.environ.get("BILIBILI_REVIEW_MAX_REPLACE_ROUNDS", "20"))
    if max_rounds < 1:
        max_rounds = 1

    loop = asyncio.get_running_loop()
    original_bilingual: Path = Path(bilingual_mp4).resolve()
    current: Path = original_bilingual

    round_idx = 0
    while True:
        round_idx += 1
        if round_idx > max_rounds:
            raise RuntimeError(
                f"已超过最多审核/替换轮数 {max_rounds}（环境变量 BILIBILI_REVIEW_MAX_REPLACE_ROUNDS），"
                "请手动在创作中心处理。"
            )
        if round_idx > 1:
            print(
                f"\n第 {round_idx} 轮：轮询稿件 {bvid}（本地基准视频: {current.name}）…"
            )

        deadline = loop.time() + max_wait
        print(
            f"开始轮询稿件 {bvid}：每 {interval:.0f} 秒查一次，本轮最长 {max_wait:.0f} 秒。"
            " 审核中时会持续打印进度（并非卡住）。"
        )
        n = 0
        while loop.time() < deadline:
            n += 1
            archive_json, page_state = await _fetch_review_data(bvid, credential)
            status = classify_review(archive_json, page_state)
            text = _review_text_blob(archive_json, page_state)

            if status == "passed":
                print("  哔哩哔哩审核：已通过。")
                arc = archive_json.get("archive")
                if isinstance(arc, dict) and arc.get("state") is not None:
                    print(f"  archive.state = {arc.get('state')!r}")
                return

            if status == "rejected":
                api_fp = _write_review_api_json(bvid, archive_json, page_state)
                print(
                    f"  完整接口 JSON 已写入: {api_fp}"
                    "（对照网页「违规时间点」、扩展正则时请附此文件）"
                )
                _maybe_print_full_review_json(api_fp)
                print_bilibili_review_content(archive_json, page_state)
                print("  哔哩哔哩审核：已退回，解析需删除的时间段…")
                ranges, range_source = extract_time_ranges_for_review(
                    archive_json, page_state
                )
                if range_source != "无":
                    print(f"  区间解析来源: {range_source}")
                if not ranges:
                    _print_reject_response_preview(
                        bvid, archive_json, page_state, text
                    )
                    fp = _write_reject_debug_text(bvid, text)
                    print(f"  未解析到时间段，完整接口原文已写入: {fp}")
                    raise RuntimeError(
                        "退回稿件中未解析到可识别的起止时间（已支持【HH:MM:SS-HH:MM:SS】、毫秒、"
                        "从/至/到、纯秒区间带「秒」字等）。请对照上方终端预览与日志文件；"
                        "把「嵌套字段节选」或合并字符串里含时间的片段发开发者即可扩展正则。"
                    )
                merged_in = _merge_ranges(ranges)
                dur = _ffprobe_duration(current)
                try:
                    pad = float(
                        os.environ.get("BILIBILI_REVIEW_RECUT_PAD_SEC", "1.0").strip()
                        or "0"
                    )
                except ValueError:
                    pad = 1.0
                ranges_for_cut = _expand_remove_ranges_with_padding(
                    merged_in,
                    pad_sec=pad,
                    duration_sec=dur,
                )
                parts = [
                    f"{_format_seconds_as_hms(a)}–{_format_seconds_as_hms(b)}"
                    for a, b in merged_in
                ]
                print(
                    f"  从退回说明解析到 {len(merged_in)} 段需删除区间（合并后）: "
                    + "；".join(parts)
                )
                if len(merged_in) != len(ranges):
                    print(
                        f"  （原始解析 {len(ranges)} 段，重叠/相邻合并为 {len(merged_in)} 段）"
                    )
                if pad > 0 and ranges_for_cut:
                    parts_cut = [
                        f"{_format_seconds_as_hms(a)}–{_format_seconds_as_hms(b)}"
                        for a, b in ranges_for_cut
                    ]
                    print(
                        f"  每段左右各扩展 {pad:g}s 后实际剪除（共 {len(ranges_for_cut)} 段）: "
                        + "；".join(parts_cut)
                    )
                elif pad <= 0:
                    print("  （BILIBILI_REVIEW_RECUT_PAD_SEC=0，未做左右扩展）")
                out = current.with_name(current.stem + "_recut.mp4")
                ffmpeg_remove_time_ranges(current, out, ranges_for_cut)
                print(f"  已剪除指定片段，输出: {out}")
                if current.resolve() != original_bilingual.resolve():
                    try:
                        current.unlink()
                        print(f"  已删除上一轮中间文件: {current.name}")
                    except OSError as e:
                        print(f"  警告: 删除中间文件失败（可手动删）: {current} — {e}")
                print("  正在替换稿件视频并重新提交…")
                await _replace_video_edit(bvid, out, credential)
                current = out
                print("  已提交修改，继续轮询审核…")
                break

            left = max(0.0, deadline - loop.time())
            print(
                f"  [{datetime.now().strftime('%H:%M:%S')}] 第 {n} 次：仍为审核中/待判定，"
                f"约 {left:.0f} 秒后本轮超时；{interval:.0f} 秒后再查…"
            )
            await asyncio.sleep(interval)
        else:
            raise TimeoutError(
                f"{max_wait} 秒内本轮未等到审核通过或退回，请稍后在创作中心查看。"
            )


def run_review_flow_sync(bvid: str, bilingual_mp4: str | Path) -> None:
    asyncio.run(poll_and_repair_rejected(bvid, Path(bilingual_mp4).resolve()))


async def _replace_video_only_async(bvid: str, mp4: Path) -> None:
    """仅调用 UPOS 上传并走编辑接口替换分 P，不轮询审核。"""
    from upload_bilibili import _load_local_env

    _load_local_env()
    credential = _credential()
    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期，请重新导出 bilibili_cookie.env")
    print(f"仅替换稿件视频: {bvid} ← {mp4}")
    await _replace_video_edit(bvid, mp4, credential)
    print("替换已提交，请在哔哩哔哩创作中心查看审核状态。")


def run_replace_video_only_sync(bvid: str, mp4: str | Path) -> None:
    asyncio.run(_replace_video_only_async(bvid, Path(mp4).resolve()))


async def _query_reject_reason_only_async(bvid: str) -> None:
    """只查询创作中心接口：状态、退回说明、可解析时间段；不剪片、不替换。"""
    from upload_bilibili import _load_local_env

    _load_local_env()
    credential = _credential()
    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期，请重新导出 bilibili_cookie.env")

    archive_json, page_state = await _fetch_review_data(bvid, credential)
    status = classify_review(archive_json, page_state)
    api_fp = _write_review_api_json(bvid, archive_json, page_state)
    print(f"稿件 {bvid} 接口快照已写入: {api_fp}")
    _maybe_print_full_review_json(api_fp)

    if status == "passed":
        print("\n当前状态：审核通过（或接口文案判定为已通过）。")
        arc = archive_json.get("archive")
        if isinstance(arc, dict):
            print(f"  archive.state = {arc.get('state')!r}")
        print("无需剪片替换。")
        return

    if status == "pending":
        print(
            "\n当前状态：审核中 / 待判定（未从接口合并文案中识别明确「已退回」或「审核通过」）。"
        )
        print_bilibili_review_content(archive_json, page_state)
        print(
            "\n未执行剪片、未上传。若之后被退回，可再运行本命令（加 --query-reject）查看说明。"
        )
        return

    print("\n当前状态：已退回。以下为退回说明（未执行任何剪片或替换分 P）。\n")
    print_bilibili_review_content(archive_json, page_state)
    print("  哔哩哔哩审核：已退回，解析需删除的时间段（仅供参考）…")
    ranges, range_source = extract_time_ranges_for_review(archive_json, page_state)
    if range_source != "无":
        print(f"  区间解析来源: {range_source}")
    blob = _review_text_blob(archive_json, page_state)
    if not ranges:
        _print_reject_response_preview(bvid, archive_json, page_state, blob)
        fp = _write_reject_debug_text(bvid, blob)
        print(f"  未解析到可自动剪片的时间段，原文已写入: {fp}")
    else:
        merged_in = _merge_ranges(ranges)
        parts = [
            f"{_format_seconds_as_hms(a)}–{_format_seconds_as_hms(b)}"
            for a, b in merged_in
        ]
        print(f"  解析到需处理时间段（合并后）: " + "；".join(parts))

    print(
        "\n---\n"
        "未执行剪片、未替换分 P。请根据说明在本地修改成片后执行：\n"
        f"  python bilibili_review.py {bvid} video_subs/你的成片.mp4 --replace-only\n"
        "  若替换后需继续自动轮询（再退回再剪再传）: 再加 --resume-review"
    )


def run_query_reject_reason_only_sync(bvid: str) -> None:
    asyncio.run(_query_reject_reason_only_async(bvid))


async def _recut_rejected_video_only_async(bvid: str, mp4: Path) -> Path:
    """按当前 BV 的退回说明裁剪本地视频，输出 *_recut.mp4；不替换分 P。"""
    from upload_bilibili import _load_local_env

    _load_local_env()
    credential = _credential()
    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期，请重新导出 bilibili_cookie.env")

    current = Path(mp4).resolve()
    if not current.is_file():
        raise FileNotFoundError(f"找不到文件: {current}")

    archive_json, page_state = await _fetch_review_data(bvid, credential)
    status = classify_review(archive_json, page_state)
    api_fp = _write_review_api_json(bvid, archive_json, page_state)
    print(f"稿件 {bvid} 接口快照已写入: {api_fp}")
    _maybe_print_full_review_json(api_fp)

    if status == "passed":
        print("当前状态：审核通过，无需剪片。")
        return current
    if status != "rejected":
        raise RuntimeError("当前未识别为“已退回”，不会执行剪片。可先用 --query-reject 查看状态。")

    print("当前状态：已退回。以下按退回说明裁剪本地视频（不上传）。")
    print_bilibili_review_content(archive_json, page_state)
    print("  哔哩哔哩审核：已退回，解析需删除的时间段…")
    ranges, range_source = extract_time_ranges_for_review(archive_json, page_state)
    if range_source != "无":
        print(f"  区间解析来源: {range_source}")
    blob = _review_text_blob(archive_json, page_state)
    if not ranges:
        _print_reject_response_preview(bvid, archive_json, page_state, blob)
        fp = _write_reject_debug_text(bvid, blob)
        print(f"  未解析到时间段，完整接口原文已写入: {fp}")
        raise RuntimeError("退回说明中未解析到可自动剪片的时间段，无法执行 --recut-only。")

    merged_in = _merge_ranges(ranges)
    dur = _ffprobe_duration(current)
    try:
        pad = float(os.environ.get("BILIBILI_REVIEW_RECUT_PAD_SEC", "1.0").strip() or "0")
    except ValueError:
        pad = 1.0
    ranges_for_cut = _expand_remove_ranges_with_padding(
        merged_in,
        pad_sec=pad,
        duration_sec=dur,
    )
    parts = [f"{_format_seconds_as_hms(a)}–{_format_seconds_as_hms(b)}" for a, b in merged_in]
    print(f"  从退回说明解析到 {len(merged_in)} 段需删除区间（合并后）: " + "；".join(parts))
    if len(merged_in) != len(ranges):
        print(f"  （原始解析 {len(ranges)} 段，重叠/相邻合并为 {len(merged_in)} 段）")
    if pad > 0 and ranges_for_cut:
        parts_cut = [
            f"{_format_seconds_as_hms(a)}–{_format_seconds_as_hms(b)}"
            for a, b in ranges_for_cut
        ]
        print(
            f"  每段左右各扩展 {pad:g}s 后实际剪除（共 {len(ranges_for_cut)} 段）: "
            + "；".join(parts_cut)
        )
    elif pad <= 0:
        print("  （BILIBILI_REVIEW_RECUT_PAD_SEC=0，未做左右扩展）")

    out = current.with_name(current.stem + "_recut.mp4")
    ffmpeg_remove_time_ranges(current, out, ranges_for_cut)
    print(f"已剪除指定片段，输出: {out}")
    print(
        "未执行上传。若要替换稿件，请执行：\n"
        f"  python bilibili_review.py {bvid} \"{out}\" --replace-only"
    )
    return out


def run_recut_rejected_video_only_sync(bvid: str, mp4: str | Path) -> Path:
    return asyncio.run(_recut_rejected_video_only_async(bvid, Path(mp4).resolve()))


def _default_bilingual_mp4() -> Path | None:
    from paths_config import VIDEO_SUBS_DIR

    if not VIDEO_SUBS_DIR.is_dir():
        return None
    cands = list(VIDEO_SUBS_DIR.glob("*_bilingual.mp4"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def main() -> None:
    import argparse
    import sys

    from paths_config import VIDEO_SUBS_DIR

    ap = argparse.ArgumentParser(description="轮询 B 站审核；退回则按时间轴剪片并替换稿件")
    ap.add_argument("bvid", help="稿件 BV 号，如 BV1DhX1BVESJ")
    ap.add_argument(
        "video",
        nargs="?",
        default=None,
        help=f"本地 MP4；省略则用 {VIDEO_SUBS_DIR.name} 下最新 *_bilingual.mp4（--replace-only 时须指定本参数或 --video）",
    )
    ap.add_argument(
        "--video",
        dest="video_flag",
        metavar="PATH",
        default=None,
        help="本地视频路径（与第二位置参数二选一；推荐与 --replace-only 连用，避免歧义）",
    )
    ap.add_argument(
        "--replace-only",
        action="store_true",
        help="只上传并替换该 BV 的分 P；默认不轮询，可加 --resume-review",
    )
    ap.add_argument(
        "--resume-review",
        action="store_true",
        help="与 --replace-only 连用：替换成功后继续轮询审核；再退回则剪片替换（多轮）",
    )
    ap.add_argument(
        "--query-reject",
        action="store_true",
        help="只查询审核状态与退回说明（含解析时间段），不剪片、不上传；改完片后再用 --replace-only",
    )
    ap.add_argument(
        "--recut-only",
        action="store_true",
        help="按当前退回说明裁剪你指定的本地视频，生成 *_recut.mp4；不上传",
    )
    args = ap.parse_args()
    try:
        bvid = normalize_bvid_cli_arg(args.bvid)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    if args.resume_review and not args.replace_only:
        print(
            "错误: --resume-review 仅可与 --replace-only 连用；"
            "若只需轮询/自动剪片不替换，请: "
            f"python bilibili_review.py {bvid} video_subs/yt_xxx_bilingual.mp4",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.query_reject:
        if args.replace_only or args.resume_review or args.recut_only or args.video or args.video_flag:
            print(
                "错误: --query-reject 只需 BV 号，请勿加视频路径或 --replace-only / --resume-review / --recut-only",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            run_query_reject_reason_only_sync(bvid)
        except RuntimeError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.recut_only:
        raw_vp = args.video_flag or args.video
        if not raw_vp:
            print(
                "错误: --recut-only 须指定视频路径。示例：\n"
                f"  python bilibili_review.py {bvid} video_subs/yt_xxx_bilingual.mp4 --recut-only\n"
                f"  或: python bilibili_review.py {bvid} --recut-only --video video_subs/yt_xxx_bilingual.mp4",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.video_flag and args.video:
            print(
                "错误: 不要同时指定第二位置参数与 --video，请只保留其一。",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.replace_only or args.resume_review:
            print(
                "错误: --recut-only 不可与 --replace-only / --resume-review 同时使用。",
                file=sys.stderr,
            )
            sys.exit(2)
        vp = Path(raw_vp).resolve()
        if not vp.is_file():
            print(f"错误: 找不到文件: {vp}", file=sys.stderr)
            sys.exit(1)
        try:
            run_recut_rejected_video_only_sync(bvid, vp)
        except (RuntimeError, TimeoutError, FileNotFoundError) as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.replace_only:
        raw_vp = args.video_flag or args.video
        if not raw_vp:
            print(
                "错误: --replace-only 须指定视频路径。推荐：\n"
                f"  python bilibili_review.py {bvid} video_subs/xxx.mp4 --replace-only\n"
                f"  或: python bilibili_review.py {bvid} --replace-only --video video_subs/xxx.mp4",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.video_flag and args.video:
            print(
                "错误: 不要同时指定第二位置参数与 --video，请只保留其一。",
                file=sys.stderr,
            )
            sys.exit(2)
        vp = Path(raw_vp).resolve()
        if not vp.is_file():
            print(f"错误: 找不到文件: {vp}", file=sys.stderr)
            sys.exit(1)
        try:
            run_replace_video_only_sync(bvid, vp)
        except (RuntimeError, FileNotFoundError) as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            from bilibili_api.exceptions.ApiException import ApiException

            if isinstance(e, ApiException):
                print(f"错误: B 站接口: {e}", file=sys.stderr)
                sys.exit(1)
            raise
        if args.resume_review:
            print(
                "\n继续轮询审核（退回则按说明剪片并替换，通过则结束；多轮上限见 BILIBILI_REVIEW_MAX_REPLACE_ROUNDS）…"
            )
            try:
                run_review_flow_sync(bvid, vp)
            except (RuntimeError, TimeoutError, FileNotFoundError) as e:
                print(f"错误: {e}", file=sys.stderr)
                sys.exit(1)
        return

    if args.video_flag:
        vp = Path(args.video_flag).resolve()
    elif args.video:
        vp = Path(args.video).resolve()
    else:
        fp = _default_bilingual_mp4()
        if not fp:
            print(
                f"错误: 未指定视频且未在 {VIDEO_SUBS_DIR} 找到 *_bilingual.mp4",
                file=sys.stderr,
            )
            sys.exit(1)
        vp = fp
        print(f"使用本地视频: {vp}")
    if not vp.is_file():
        print(f"错误: 找不到文件: {vp}", file=sys.stderr)
        sys.exit(1)
    try:
        run_review_flow_sync(bvid, vp)
    except (RuntimeError, TimeoutError, FileNotFoundError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
