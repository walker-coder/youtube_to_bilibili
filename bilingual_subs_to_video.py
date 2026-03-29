"""
将 1.srt 与 1.zh-Hans.srt 按时间对齐，在画面底部烧录中英双行字幕（上中文、下英文，黄底黑字框）。

说明：
- ffmpeg 烧录字幕时需要一种字幕格式；本脚本在内部使用「临时 ASS」，默认不写 1_bilingual.ass。
- 需要长期保存 ASS 调试时，请加 --keep-ass 路径。

依赖：ffmpeg 在 PATH；可选 Pillow（仅 --export-pngs）。

用法:
  python bilingual_subs_to_video.py --seconds 90 -o video_subs/preview_bilingual_90s.mp4
  python bilingual_subs_to_video.py --video video_subs/输出.mp4 -o video_subs/输出_双语.mp4
  python bilingual_subs_to_video.py --keep-ass video_subs/1_bilingual.ass
  烧录时输出进度百分比到 stdout（由上层 nohup 重定向时写入同一日志文件）；SIGINT/SIGTERM 会记录中断时间。
  低内存：ffmpeg stderr 仅保留末尾若干行供报错用，不随时长无限增长。可选环境变量 BLOOMBREG_FFMPEG_BURN_ARGS（空格分隔，见 shlex），例如 `-threads 1` 降低并行与峰值内存。
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from paths_config import PROJECT_ROOT, VIDEO_SUBS_DIR, ensure_video_subs_dir


def _parse_srt(path: Path) -> list[dict]:
    """返回 [{\"index\": int, \"start\": float, \"end\": float, \"text\": str}, ...]"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    lines = raw.splitlines()
    cues = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.isdigit():
            idx = int(line)
            i += 1
            if i >= len(lines) or "-->" not in lines[i]:
                continue
            time_line = lines[i].strip()
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i])
                i += 1
            start_s, end_s = _parse_srt_time_range(time_line)
            text = "\n".join(text_lines).strip()
            cues.append({"index": idx, "start": start_s, "end": end_s, "text": text})
            continue
        i += 1
    cues.sort(key=lambda c: (c["start"], c["index"]))
    return cues


def _parse_srt_time_range(line: str) -> tuple[float, float]:
    parts = line.split("-->")
    if len(parts) != 2:
        return 0.0, 0.0
    return _srt_ts_to_sec(parts[0].strip()), _srt_ts_to_sec(parts[1].strip())


def _srt_ts_to_sec(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _sec_to_ass_time(t: float) -> str:
    """ASS: H:MM:SS.cc（厘秒两位）"""
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    whole = int(s)
    cs = int(round((s - whole) * 100))
    if cs >= 100:
        whole += 1
        cs = 0
    return f"{h}:{m:02d}:{whole:02d}.{cs:02d}"


def _ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _cue_to_ass_text(s: str) -> str:
    """
    将一条 SRT 字幕块内的多行，转为 ASS 单行内的 \\N 换行。
    若直接把 Python 的 \\n 写进 .ass 文件，会截断 Dialogue，导致只显示第一行（常见为只剩英文）。
    """
    parts = [p.strip() for p in s.splitlines() if p.strip()]
    if not parts:
        return ""
    return r"\N".join(_ass_escape(p) for p in parts)


def _merge_to_ass(en_cues: list[dict], zh_cues: list[dict]) -> str:
    """生成 ASS：底部水平居中，上行中文、下行英文；黄底黑字（BorderStyle 3 不透明框）。"""
    n = min(len(en_cues), len(zh_cues))
    if len(en_cues) != len(zh_cues):
        print(f"警告: 英 {len(en_cues)} 条、中 {len(zh_cues)} 条，仅合并前 {n} 条。")

    # \\an2 底中；\\pos 的 Y 为底边锚点，数值越大越贴近画面下缘（1080p 下缘为 1080）。
    # BorderStyle=3 时 OutlineColour 勿用纯黑，Outline 与字号成比例以免黑边盖黄底。
    line_tag = r"{\an2\pos(960,1040)\fs64\c&H000000&}"

    lines = [
        "[Script Info]",
        "Title: bilingual",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # BorderStyle=3：Back=亮黄；Primary=黑字；OutlineColour 勿用纯黑（见上行注释）。StrikeOut 后须接 ScaleX=100,ScaleY=100。
        "Style: Default,Microsoft YaHei,64,&H00000000,&H00000000,&H0000FFFF,&H0000FFFF,-1,0,0,0,100,100,0,0,3,8,0,2,80,80,100,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for i in range(n):
        en = en_cues[i]["text"]
        zh = zh_cues[i]["text"]
        start = _sec_to_ass_time(en_cues[i]["start"])
        end = _sec_to_ass_time(en_cues[i]["end"])
        en_ass = _cue_to_ass_text(en)
        zh_ass = _cue_to_ass_text(zh)
        body = zh_ass + r"\N{\fs56\c&H000000&}" + en_ass
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{line_tag}{body}")

    return "\n".join(lines) + "\n"


def _write_ass(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")


def _export_pngs(
    en_cues: list[dict],
    zh_cues: list[dict],
    out_dir: Path,
    width: int = 1920,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("导出 PNG 需要: pip install Pillow")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    n = min(len(en_cues), len(zh_cues))
    try:
        font = ImageFont.truetype("msyh.ttc", 28)
    except OSError:
        font = ImageFont.load_default()

    for i in range(n):
        en = en_cues[i]["text"]
        zh = zh_cues[i]["text"]
        lines = (en + "\n" + zh).split("\n")
        img = Image.new("RGBA", (width, 200), (0, 0, 0, 200))
        draw = ImageDraw.Draw(img)
        y = 10
        for line in lines:
            draw.text((40, y), line, font=font, fill=(255, 255, 255, 255))
            y += 34
        img.save(out_dir / f"{i+1:05d}.png")
    print(f"已导出 {n} 张 PNG 到: {out_dir}")


def _ffprobe_duration_sec(video: Path) -> float | None:
    """返回视频时长（秒），失败时 None。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video.resolve()),
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        return None
    try:
        v = float((r.stdout or "").strip())
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_ffmpeg_stderr_time_sec(line: str) -> float | None:
    """从 ffmpeg 进度行解析已处理时长，例如 time=00:05:36.44。"""
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    h, m_, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + m_ * 60 + s


def _burn_ffmpeg(
    video: Path,
    ass_path: Path,
    output: Path,
    *,
    duration_sec: float | None = None,
) -> None:
    """在项目根目录执行 ffmpeg，使用相对路径，避免 Windows 下 subtitles= 绝对路径解析错误。"""
    root = PROJECT_ROOT
    vid = video.resolve()
    ass = ass_path.resolve()
    out = output.resolve()
    try:
        vid_rel = vid.relative_to(root)
        ass_rel = ass.relative_to(root)
        out_rel = out.relative_to(root)
    except ValueError:
        vid_rel, ass_rel, out_rel = vid, ass, out
    sub_path = str(ass_rel).replace("\\", "/")
    vf = f"subtitles={sub_path}:charenc=UTF-8"
    extra = shlex.split(os.environ.get("BLOOMBREG_FFMPEG_BURN_ARGS", "").strip())
    cmd = ["ffmpeg", "-y", *extra]
    cmd += [
        "-i",
        str(vid_rel).replace("\\", "/"),
    ]
    clip_sec = duration_sec
    if clip_sec is not None and clip_sec > 0:
        cmd += ["-ss", "0", "-t", str(clip_sec)]
    cmd += [
        "-vf",
        vf,
        "-c:a",
        "copy",
        str(out_rel).replace("\\", "/"),
    ]
    total_sec = clip_sec if (clip_sec is not None and clip_sec > 0) else _ffprobe_duration_sec(vid)
    ts0 = datetime.now().isoformat()
    print(f"[{ts0}] 开始烧录 → {out.name}")
    print("执行 (cwd=%s):" % root, " ".join(cmd))
    if total_sec:
        print(f"  预计总时长: {total_sec:.1f}s（用于进度百分比）")

    st: dict = {"proc": None, "interrupted": False}

    def _on_sig(signum: int, frame) -> None:
        st["interrupted"] = True
        print(
            f"\n[{datetime.now().isoformat()}] 烧录被中断（signal {signum}），正在终止 ffmpeg…",
            flush=True,
        )
        p = st["proc"]
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    old_int = signal.signal(signal.SIGINT, _on_sig)
    old_term = signal.signal(signal.SIGTERM, _on_sig) if hasattr(signal, "SIGTERM") else None

    proc = None
    stderr_tail: deque[str] = deque(maxlen=60)
    last_int_pct = -1
    tty = sys.stdout.isatty()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        st["proc"] = proc
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line)
            t = _parse_ffmpeg_stderr_time_sec(line)
            if t is None or not total_sec or total_sec <= 0:
                continue
            pct = min(100.0, 100.0 * t / total_sec)
            ip = int(pct)
            if ip > last_int_pct:
                last_int_pct = ip
                msg = f"  烧录进度: {pct:.1f}%"
                if tty:
                    print(f"\r{msg}", end="", flush=True)
                else:
                    print(msg, flush=True)
        code = proc.wait()
        if st["interrupted"]:
            print(
                f"\n[{datetime.now().isoformat()}] 烧录已中止（未完成输出）。",
                flush=True,
            )
            raise SystemExit(130)
        if code != 0:
            tail = "".join(stderr_tail)
            raise RuntimeError(f"ffmpeg 失败，退出码 {code}\n{tail}")
        if total_sec:
            if tty:
                print(f"\r  烧录进度: 100.0%      ")
            else:
                print("  烧录进度: 100.0%", flush=True)
        else:
            print("  烧录完成（未解析到时长，无百分比）")
        print(f"[{datetime.now().isoformat()}] 烧录结束 退出码=0 输出={out}", flush=True)
    finally:
        signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if proc is not None and proc.poll() is None:
            proc.kill()


def main() -> None:
    ensure_video_subs_dir()
    ap = argparse.ArgumentParser(description="中英 SRT 合并为 ASS 并烧录到视频")
    ap.add_argument("--en", type=Path, default=VIDEO_SUBS_DIR / "1.srt", help="英文字幕")
    ap.add_argument("--zh", type=Path, default=VIDEO_SUBS_DIR / "1.zh-Hans.srt", help="简体中文字幕")
    ap.add_argument("--video", type=Path, default=None, help="输入视频（默认 video_subs/输出.mp4 或 1.mp4）")
    ap.add_argument("-o", "--output", type=Path, default=None, help="输出视频")
    ap.add_argument("--ass-only", type=Path, default=None, help="只生成 ASS 到该路径，不跑 ffmpeg")
    ap.add_argument("--export-pngs", type=Path, default=None, help="导出每句双语 PNG 到目录（不烧录）")
    ap.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="N",
        help="只处理视频前 N 秒（预览用，避免整片重编码过久）",
    )
    ap.add_argument(
        "--keep-ass",
        type=Path,
        default=None,
        metavar="PATH",
        help="同时把合并后的 ASS 保存到该路径（默认不保存，仅用临时文件）",
    )
    args = ap.parse_args()

    en_path = args.en.resolve()
    zh_path = args.zh.resolve()
    if not en_path.is_file() or not zh_path.is_file():
        print(f"找不到字幕: {en_path} 或 {zh_path}")
        sys.exit(1)

    en_cues = _parse_srt(en_path)
    zh_cues = _parse_srt(zh_path)
    if not en_cues or not zh_cues:
        print("字幕解析为空")
        sys.exit(1)

    if args.export_pngs:
        _export_pngs(en_cues, zh_cues, args.export_pngs.resolve())
        return

    ass_content = _merge_to_ass(en_cues, zh_cues)

    if args.ass_only:
        ass_path = Path(args.ass_only).resolve()
        _write_ass(ass_path, ass_content)
        print(f"已写入 ASS: {ass_path}")
        return

    if args.keep_ass:
        keep = Path(args.keep_ass).resolve()
        _write_ass(keep, ass_content)
        print(f"已保存 ASS 副本: {keep}")

    vid = args.video
    if vid is None:
        for cand in (VIDEO_SUBS_DIR / "输出.mp4", VIDEO_SUBS_DIR / "1.mp4", VIDEO_SUBS_DIR / "1_subs.mp4"):
            if cand.is_file():
                vid = cand
                break
    if vid is None or not Path(vid).is_file():
        print("请用 --video 指定输入视频，或在 video_subs 下放置 输出.mp4 / 1.mp4 / 1_subs.mp4")
        sys.exit(1)
    vid = Path(vid).resolve()

    out = args.output or (vid.parent / f"{vid.stem}_bilingual{vid.suffix}")
    out = Path(out).resolve()

    # 临时 ASS 放在 video_subs 下，便于用相对路径调用 ffmpeg（系统 Temp 盘符路径会触发解析错误）
    ass_path = VIDEO_SUBS_DIR / f".tmp_bilingual_{uuid.uuid4().hex}.ass"
    try:
        ass_path.write_text(ass_content, encoding="utf-8-sig")
        _burn_ffmpeg(vid, ass_path, out, duration_sec=args.seconds)
    finally:
        try:
            ass_path.unlink(missing_ok=True)
        except OSError:
            pass

    print(f"已生成: {out}")


if __name__ == "__main__":
    main()
