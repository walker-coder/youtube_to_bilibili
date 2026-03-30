"""
从 youtube_to_bilibili 流水线的中间步骤继续执行（无需重新下载）。

前提：`video_subs/` 下已有对应文件（见各 --from 说明）。

示例：
  python resume_youtube_pipeline.py --from translate --vid JOU5iy56FjY --url "https://www.youtube.com/watch?v=JOU5iy56FjY"
  python resume_youtube_pipeline.py --from burn --vid JOU5iy56FjY --url "https://..."
  python resume_youtube_pipeline.py --from upload --vid JOU5iy56FjY --url "https://..."
  python resume_youtube_pipeline.py --from translate --vid JOU5iy56FjY --no-upload   # 无需 --url
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from paths_config import PROJECT_ROOT, VIDEO_SUBS_DIR, ensure_video_subs_dir
from translate_subs_to_zh_hans import translate_vtt_to_zh_hans
from zh_sensitive_replace import apply_zh_sensitive_replacements_to_vtt
from upload_bilibili import upload_video_to_bilibili
from vtt_to_srt import vtt_to_srt
from youtube_to_bilibili import (
    _find_en_vtt,
    _js_runtimes_from_env,
    _resolve_youtube_cookiefile,
    _strip_china_show_title_suffix,
    _youtube_extractor_args,
    _youtube_upload_date_ymd_slash,
)


def _zh_vtt_from_en(en_vtt: Path) -> Path:
    stem = en_vtt.stem
    if stem.endswith(".en"):
        base = en_vtt.parent / stem[:-3]
    else:
        base = en_vtt.parent / stem
    return base.parent / (base.name + ".zh-Hans.vtt")


def _find_video_for_vid(vid: str) -> Path:
    for ext in ("mp4", "mkv", "webm"):
        p = VIDEO_SUBS_DIR / f"yt_{vid}.{ext}"
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        f"未找到视频文件（预期 {VIDEO_SUBS_DIR / f'yt_{vid}.mp4'} 或 .mkv / .webm）"
    )


def _fetch_youtube_metadata(
    url: str,
    *,
    cookies_file: str | None,
    no_youtube_cookies: bool,
) -> tuple[str, str | None, str]:
    """返回 (YouTube 标题, 上传日期 YYYY/MM/DD 或 None, 解析到的视频 id)。"""
    ensure_video_subs_dir()
    cookie_path = None if no_youtube_cookies else _resolve_youtube_cookiefile(cookies_file)

    def build(with_cookie: bool) -> dict:
        opts: dict = {
            "quiet": True,
            "noplaylist": True,
        }
        if os.environ.get("YTDLP_FORCE_IPV4", "1").strip().lower() not in ("0", "false", "no"):
            opts["force_ipv4"] = True
        ex = _youtube_extractor_args()
        if ex:
            opts["extractor_args"] = ex
        js_rt = _js_runtimes_from_env()
        if js_rt:
            opts["js_runtimes"] = js_rt
        if with_cookie and cookie_path:
            p = Path(cookie_path).expanduser().resolve()
            if p.is_file():
                opts["cookiefile"] = str(p)
        return opts

    attempts: list[bool] = []
    if cookie_path:
        cp = Path(cookie_path).expanduser().resolve()
        if cp.is_file():
            attempts.append(True)
    attempts.append(False)

    last_err: Exception | None = None
    info = None
    for with_cookie in attempts:
        try:
            with yt_dlp.YoutubeDL(build(with_cookie)) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except DownloadError as e:
            last_err = e
            continue
    if info is None:
        if last_err:
            raise last_err
        raise RuntimeError("yt-dlp 未返回视频信息")

    title = (info.get("title") or "video").strip()
    date_ymd = _youtube_upload_date_ymd_slash(info)
    got_id = str(info.get("id") or "").strip()
    return title, date_ymd, got_id


def _burn_bilingual(vid: str, video_path: Path, en_vtt: Path, zh_vtt: Path) -> Path:
    print("  转为 SRT 并烧录双语画面字幕…")
    en_srt = vtt_to_srt(en_vtt)
    zh_srt = vtt_to_srt(zh_vtt)
    out_bilingual = VIDEO_SUBS_DIR / f"yt_{vid}_bilingual.mp4"
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "bilingual_subs_to_video.py"),
        "--video",
        str(video_path),
        "--en",
        str(en_srt),
        "--zh",
        str(zh_srt),
        "-o",
        str(out_bilingual),
    ]
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if r.returncode != 0:
        raise RuntimeError("烧录字幕失败（bilingual_subs_to_video.py 退出码非 0）")
    print(f"  已生成: {out_bilingual}")
    return out_bilingual


def _upload_and_maybe_review(
    *,
    url: str,
    out_bilingual: Path,
    yt_title: str,
    yt_date_ymd: str | None,
    bilibili_title: str | None,
    no_review_wait: bool,
) -> None:
    review_after = not no_review_wait
    total_steps = 5 if review_after else 4
    print(f"步骤 4/{total_steps}：上传哔哩哔哩…")
    base_title = (
        bilibili_title.strip()
        if bilibili_title
        else _strip_china_show_title_suffix(yt_title)
    )
    if yt_date_ymd:
        title = f"{base_title} | {yt_date_ymd}"
    else:
        title = base_title
    title = title[:80]
    desc = (
        f"转载自 YouTube。\n原链接: {url}\n\n"
        "仅供个人学习交流，如有侵权请联系删除。"
    )
    result = upload_video_to_bilibili(
        out_bilingual,
        title=title,
        desc=desc,
        tags=["YouTube", "中英字幕", "转载", "Bloomberg", "The China Show"],
        source=f"YouTube: {url[:180]}",
    )
    print("投稿成功:", result)
    if review_after:
        bvid = result.get("bvid")
        if not bvid:
            raise RuntimeError("上传返回结果中缺少 bvid，无法轮询审核。")
        print("步骤 5/5：轮询审核状态（退回则按时间轴剪片并替换稿件）…")
        from bilibili_review import run_review_flow_sync

        run_review_flow_sync(bvid, out_bilingual)


def run_from(
    *,
    resume_from: str,
    vid: str,
    url: str | None,
    bilibili_title: str | None,
    no_upload: bool,
    cookies_file: str | None,
    no_youtube_cookies: bool,
    no_review_wait: bool,
) -> Path:
    ensure_video_subs_dir()
    vid = vid.strip()
    if not vid:
        raise ValueError("视频 ID 不能为空")

    need_url = not no_upload
    if need_url and not (url and url.strip()):
        raise ValueError("上传 B 站需要 --url（用于简介中的原链接与拉取标题/日期）")

    yt_title = ""
    yt_date_ymd: str | None = None
    if need_url:
        assert url is not None
        yt_title, yt_date_ymd, got_id = _fetch_youtube_metadata(
            url.strip(),
            cookies_file=cookies_file,
            no_youtube_cookies=no_youtube_cookies,
        )
        if got_id and got_id != vid:
            print(
                f"  警告：--vid={vid} 与链接解析出的 id={got_id} 不一致，将仍以 --vid 查找本地文件。",
                file=sys.stderr,
            )
        print(f"  YouTube 标题: {yt_title}")
        if yt_date_ymd:
            print(f"  上传日期: {yt_date_ymd}（B 站标题：原标题 | 此日期）")

    out_bilingual: Path | None = None

    if resume_from == "translate":
        video_path = _find_video_for_vid(vid)
        en_vtt = _find_en_vtt(vid)
        print(f"步骤 2：翻译为简体中文…")
        print(f"  视频: {video_path}")
        print(f"  英文字幕: {en_vtt}")
        zh_vtt = translate_vtt_to_zh_hans(en_vtt)
        zh_vtt = apply_zh_sensitive_replacements_to_vtt(zh_vtt)
        print(f"步骤 3：烧录双语字幕…")
        out_bilingual = _burn_bilingual(vid, video_path, en_vtt, zh_vtt)

    elif resume_from == "burn":
        video_path = _find_video_for_vid(vid)
        en_vtt = _find_en_vtt(vid)
        zh_vtt = _zh_vtt_from_en(en_vtt)
        if not zh_vtt.is_file():
            raise FileNotFoundError(
                f"未找到简体中文字幕（请先完成翻译步骤）: {zh_vtt}"
            )
        zh_vtt = apply_zh_sensitive_replacements_to_vtt(zh_vtt)
        print(f"步骤 3：烧录双语字幕…")
        print(f"  视频: {video_path}")
        print(f"  英文: {en_vtt}")
        print(f"  中文: {zh_vtt}")
        out_bilingual = _burn_bilingual(vid, video_path, en_vtt, zh_vtt)

    elif resume_from == "upload":
        out_bilingual = VIDEO_SUBS_DIR / f"yt_{vid}_bilingual.mp4"
        if not out_bilingual.is_file():
            raise FileNotFoundError(
                f"未找到双语成片: {out_bilingual}（请先完成烧录步骤）"
            )
        print(f"  使用成片: {out_bilingual}")

    else:
        raise ValueError(f"未知 --from: {resume_from}")

    assert out_bilingual is not None

    if no_upload:
        print("已跳过上传（--no-upload）。")
        return out_bilingual

    assert url is not None
    _upload_and_maybe_review(
        url=url.strip(),
        out_bilingual=out_bilingual,
        yt_title=yt_title,
        yt_date_ymd=yt_date_ymd,
        bilibili_title=bilibili_title,
        no_review_wait=no_review_wait,
    )
    return out_bilingual


def main() -> None:
    ap = argparse.ArgumentParser(
        description="从中间步骤继续 YouTube → 双语字幕 → 哔哩哔哩（不重新下载）"
    )
    ap.add_argument(
        "--from",
        dest="resume_from",
        choices=("translate", "burn", "upload"),
        required=True,
        help="从哪一步继续：translate=翻译→烧录→上传；burn=已有中英 vtt 仅烧录→上传；upload=仅上传已有双语 mp4",
    )
    ap.add_argument(
        "--vid",
        required=True,
        help="YouTube 视频 ID（与 video_subs/yt_<vid>.* 文件名一致）",
    )
    ap.add_argument(
        "--url",
        default=None,
        help="原 YouTube 链接（上传时需要；仅本地处理且加 --no-upload 时可省略）",
    )
    ap.add_argument(
        "--title",
        dest="bilibili_title",
        default=None,
        help="B 站投稿标题（默认用 YouTube 原标题）",
    )
    ap.add_argument("--no-upload", action="store_true", help="只生成本地双语视频，不上传")
    ap.add_argument(
        "--cookies",
        metavar="FILE",
        default=None,
        help="YouTube Netscape cookies（与 youtube_to_bilibili.py 相同）",
    )
    ap.add_argument(
        "--no-youtube-cookies",
        action="store_true",
        help="拉取元数据时不使用 YouTube cookies",
    )
    ap.add_argument(
        "--no-review-wait",
        action="store_true",
        help="上传成功后不轮询审核",
    )
    args = ap.parse_args()
    try:
        run_from(
            resume_from=args.resume_from,
            vid=args.vid,
            url=args.url,
            bilibili_title=args.bilibili_title,
            no_upload=args.no_upload,
            cookies_file=args.cookies,
            no_youtube_cookies=args.no_youtube_cookies,
            no_review_wait=args.no_review_wait,
        )
    except (RuntimeError, FileNotFoundError, DownloadError, TimeoutError, ValueError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
