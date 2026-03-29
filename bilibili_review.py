"""
投稿后轮询哔哩哔哩审核状态：若「已退回」则解析稿件问题中的时间轴，剪除后通过编辑接口替换视频并再次提交；不再次轮询。

依赖 upload_bilibili 的 Cookie 配置、ffmpeg、与 bilibili-api。

环境变量（可选）：
  BILIBILI_REVIEW_POLL_INTERVAL_SEC  轮询间隔秒数，默认 30
  BILIBILI_REVIEW_MAX_WAIT_SEC       最长等待秒数，默认 7200
  BILIBILI_REVIEW_DUMP_REJECT_TEXT=1  若退回但解析不到时间轴，将 API 合并原文写入 logs/bilibili_reject_raw_<BV>_<时间>.txt 便于核对格式

单独补跑（已上传过、未走流水线步骤 5 时）：
  python bilibili_review.py BV1DhX1BVESJ
  python bilibili_review.py BV1DhX1BVESJ video_subs/yt_xxx_bilingual.mp4
  第二参数省略时自动选 video_subs 下最新 *_bilingual.mp4

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


def extract_time_ranges_from_text(text: str) -> list[tuple[float, float]]:
    """
    从退回说明 / API JSON 文本中提取「需删除」时间段（秒）。
    支持：【HH:MM:SS-HH:MM:SS】、半角 []、无括号、全角冒号、多种破折号、MM:SS-MM:SS（无小时）等。
    """
    t = text.replace("\uFF1A", ":").replace("：", ":")
    out: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()

    def add_pair(a: str, b: str) -> None:
        try:
            sa, sb = _parse_time_token(a), _parse_time_token(b)
            if sb <= sa:
                return
            key = (round(sa, 3), round(sb, 3))
            if key not in seen:
                seen.add(key)
                out.append((sa, sb))
        except (ValueError, IndexError, OSError):
            return

    patterns = [
        # 【01:29:19-01:31:06】
        re.compile(
            rf"[【\[](\d{{1,2}}:\d{{2}}:\d{{2}}){_DASH}(\d{{1,2}}:\d{{2}}:\d{{2}})[】\]]"
        ),
        # 正文里无括号，两段完整时刻
        re.compile(
            rf"(\d{{1,2}}:\d{{2}}:\d{{2}})\s*{_DASH}\s*(\d{{1,2}}:\d{{2}}:\d{{2}})"
        ),
        # 仅 分:秒（短片段）
        re.compile(rf"[【\[](\d{{1,2}}:\d{{2}}){_DASH}(\d{{1,2}}:\d{{2}})[】\]]"),
        re.compile(
            rf"(?<![\d:])(\d{{1,2}}:\d{{2}})\s*{_DASH}\s*(\d{{1,2}}:\d{{2}})(?![\d:])"
        ),
        # 01:29:19 至 01:31:06
        re.compile(
            rf"(\d{{1,2}}:\d{{2}}:\d{{2}})\s*至\s*(\d{{1,2}}:\d{{2}}:\d{{2}})"
        ),
        re.compile(rf"(\d{{1,2}}:\d{{2}})\s*至\s*(\d{{1,2}}:\d{{2}})"),
    ]
    for pat in patterns:
        for m in pat.finditer(t):
            add_pair(m.group(1), m.group(2))

    return out


def _review_text_blob(archive_json: dict, page_state: dict | None) -> str:
    parts = [json.dumps(archive_json, ensure_ascii=False)]
    if page_state is not None:
        parts.append(json.dumps(page_state, ensure_ascii=False))
    return "\n".join(parts)


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
    line = await _choose_line(None)
    uploader = _ReplaceVideoUploader(bvid, credential, meta, page, line)
    return await uploader.start()


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
    """轮询至通过或退回；退回则剪片并编辑重传一次后结束。"""
    from upload_bilibili import _load_local_env

    _load_local_env()
    credential = _credential()
    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期")

    interval = float(os.environ.get("BILIBILI_REVIEW_POLL_INTERVAL_SEC", "30"))
    max_wait = float(os.environ.get("BILIBILI_REVIEW_MAX_WAIT_SEC", "7200"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait

    print(
        f"开始轮询稿件 {bvid}：每 {interval:.0f} 秒查一次，最长 {max_wait:.0f} 秒。"
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
            return

        if status == "rejected":
            print("  哔哩哔哩审核：已退回，解析需删除的时间段…")
            ranges = extract_time_ranges_from_text(text)
            if not ranges:
                dump = (os.environ.get("BILIBILI_REVIEW_DUMP_REJECT_TEXT") or "").strip().lower()
                if dump in ("1", "true", "yes", "on", "y"):
                    from paths_config import PROJECT_ROOT

                    log_dir = PROJECT_ROOT / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fp = log_dir / f"bilibili_reject_raw_{bvid}_{stamp}.txt"
                    fp.write_text(text, encoding="utf-8")
                    print(f"  已写出退回相关 API 原文（便于核对时间格式）: {fp}")
                raise RuntimeError(
                    "退回稿件中未解析到可识别的起止时间（支持【HH:MM:SS-HH:MM:SS】、无括号两段时刻、"
                    "「至」连接、MM:SS 等）。请上创作中心查看审核说明；需要看接口原文时可设 "
                    "BILIBILI_REVIEW_DUMP_REJECT_TEXT=1 后重跑，在 logs/ 下查看 bilibili_reject_raw_*.txt。"
                )
            out = bilingual_mp4.with_name(bilingual_mp4.stem + "_recut.mp4")
            ffmpeg_remove_time_ranges(bilingual_mp4, out, ranges)
            print(f"  已剪除指定片段，输出: {out}")
            print("  正在替换稿件视频并重新提交…")
            await _replace_video_edit(bvid, out, credential)
            print("  已提交修改，不再轮询审核状态。")
            return

        left = max(0.0, deadline - loop.time())
        print(
            f"  [{datetime.now().strftime('%H:%M:%S')}] 第 {n} 次：仍为审核中/待判定，"
            f"约 {left:.0f} 秒后超时；{interval:.0f} 秒后再查…"
        )
        await asyncio.sleep(interval)

    raise TimeoutError(f"{max_wait} 秒内未等到审核通过或退回，请稍后在创作中心查看。")


def run_review_flow_sync(bvid: str, bilingual_mp4: str | Path) -> None:
    asyncio.run(poll_and_repair_rejected(bvid, Path(bilingual_mp4).resolve()))


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
        help=f"本地双语 MP4（与首次投稿一致）；省略则用 {VIDEO_SUBS_DIR.name} 下最新 *_bilingual.mp4",
    )
    args = ap.parse_args()
    try:
        bvid = normalize_bvid_cli_arg(args.bvid)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    if args.video:
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
