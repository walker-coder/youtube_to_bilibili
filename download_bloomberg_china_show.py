"""
从 YouTube 搜索 "china show Bloomberg Television"，并下载第一条视频。
（Bloomberg 频道无公开「搜索」标签，故用全站搜索并带频道名。）

- 视频与英文字幕保存到 video_subs 目录。
- 优先下载「今天」上传的视频；若没有，则下载搜索到的「最新一期」。
- 画质：优先「最高清晰度」（需安装 ffmpeg 才能合并出有声音）；无 ffmpeg 时自动改用单文件（约 1080p，有声音）。

供定时任务复用：fetch_china_show_entries()、filter_entries_by_upload_dates()、entry_watch_url()。
定时任务：项目根目录 logs 下若已有当天文件 china_show_YYYYMMDD.log（下载成功后写入），则再次运行会直接跳过；加 --force 可强制重跑。

python版本大于3.10
使用前请安装:
  pip install -r requirements.txt
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yt_dlp

from paths_config import LOGS_DIR, VIDEO_SUBS_DIR, ensure_logs_dir, ensure_video_subs_dir

# 定时任务用：logs 下存在当天此文件则表示已成功跑过，避免重复下载
CHINA_SHOW_LOG_PREFIX = "china_show_"
CHINA_SHOW_LOG_SUFFIX = ".log"


# 全站搜索词（带上频道名，第一条多为该频道节目）
SEARCH_QUERY = "The china show bloomberg"
# 文件名模板（会存到 video_subs 目录下）
OUTPUT_TEMPLATE = "Bloomberg_China_Show_%(title)s.%(ext)s"
# 搜索条数，用于从中筛选今天上传的（取第一条匹配）
SEARCH_COUNT = 10


class _YdlQuietLogger:
    """Filter noisy yt-dlp warnings while keeping hard errors visible."""

    _SUPPRESS_SUBSTRINGS = (
        "No supported JavaScript runtime could be found",
        "YouTube extraction without a JS runtime has been deprecated",
    )

    def debug(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        if any(s in msg for s in self._SUPPRESS_SUBSTRINGS):
            return
        print(msg)

    def error(self, msg: str) -> None:
        print(msg)


def fetch_china_show_entries(
    *,
    search_count: int | None = None,
    current_week_only: bool = False,
    upload_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    仅拉取元数据，不下载。返回 yt_dlp 的 entry 字典列表（可能含 id、title、upload_date 等）。
    current_week_only=True 时，仅返回本周（周一到今天）上传的视频。
    upload_dates:
      - 传入 YYYYMMDD 列表，按指定上传日期精确筛选（优先级高于 current_week_only）
    """
    n = search_count if search_count is not None else SEARCH_COUNT
    search_string = f"ytsearch{n}:{SEARCH_QUERY}"
    extract_opts = {
        "quiet": True,
        "extract_flat": False,
        "logger": _YdlQuietLogger(),
    }
    with yt_dlp.YoutubeDL(extract_opts) as ydl:
        info = ydl.extract_info(search_string, download=False)
    if not info or not info.get("entries"):
        return []
    entries = [e for e in info["entries"] if e]
    if upload_dates:
        return filter_entries_by_upload_dates(entries, upload_dates)
    if current_week_only:
        return filter_entries_by_current_week(entries)
    return entries


def filter_entries_by_upload_dates(
    entries: list[dict[str, Any]],
    dates_yyyymmdd: list[str],
) -> list[dict[str, Any]]:
    """只保留 upload_date 在给定 YYYYMMDD 列表中的条目（顺序与搜索一致）。"""
    want = set(dates_yyyymmdd)
    return [e for e in entries if (e.get("upload_date") or "") in want]


def filter_entries_by_current_week(
    entries: list[dict[str, Any]],
    *,
    ref_day: date | None = None,
) -> list[dict[str, Any]]:
    """只保留 upload_date 在本周（周一到今天）范围内的条目。"""
    today = ref_day or date.today()
    start_of_week = today - timedelta(days=today.weekday())
    kept: list[dict[str, Any]] = []
    for e in entries:
        s = e.get("upload_date") or ""
        if not s:
            continue
        try:
            d = datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            continue
        if start_of_week <= d <= today:
            kept.append(e)
    return kept


def entry_watch_url(entry: dict[str, Any]) -> str | None:
    vid = entry.get("id") or entry.get("url")
    if not vid:
        return None
    s = str(vid)
    if len(s) <= 15 and not s.startswith("http"):
        return f"https://www.youtube.com/watch?v={s}"
    return s if s.startswith("http") else None


def today_success_log_path(d: date | None = None) -> Path:
    """当天「已成功执行」标记日志路径（仅成功结束时创建）。"""
    day = d or date.today()
    return LOGS_DIR / f"{CHINA_SHOW_LOG_PREFIX}{day.strftime('%Y%m%d')}{CHINA_SHOW_LOG_SUFFIX}"


def already_ran_today() -> bool:
    ensure_logs_dir()
    return today_success_log_path().exists()


def _write_success_log() -> None:
    ensure_logs_dir()
    p = today_success_log_path()
    p.write_text(
        f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )


def _get_ydl_opts():
    ensure_video_subs_dir()
    outtmpl = str(VIDEO_SUBS_DIR / OUTPUT_TEMPLATE)
    return {
        # 优先最高画质+音质（需 ffmpeg 合并）；无 ffmpeg 时回退为单文件 best
        "format": "bestvideo+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": False,
        "no_warnings": False,
        "sleep_interval": 1,
        "max_sleep_interval": 2,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "embed_subs": True,
    }


def download_first_video(*, force: bool = False) -> None:
    """
    force=True 时忽略当天是否已有成功日志（调试用或手动补跑）。
    """
    if not force and already_ran_today():
        p = today_success_log_path()
        print(f"今日任务已执行过（存在 {p}），跳过。需要重跑请加 --force")
        return

    today_list = [
        date.today().strftime("%Y%m%d"),
        (date.today() - timedelta(days=1)).strftime("%Y%m%d"),
    ]
    entries = fetch_china_show_entries()
    if not entries:
        print("未搜到任何视频。")
        return

    today_videos = filter_entries_by_upload_dates(entries, today_list)
    if today_videos:
        chosen = today_videos[0]
        print(f"找到今天上传的视频，准备下载：{chosen.get('title', '')}\n")
    else:
        chosen = entries[0]
        print(f"今天暂无新视频，改为下载最新一期：{chosen.get('title', '')}\n")

    video_url = entry_watch_url(chosen)
    if not video_url:
        print("无法获取视频 ID。")
        return

    with yt_dlp.YoutubeDL(_get_ydl_opts()) as ydl:
        ydl.download([video_url])

    _write_success_log()
    print(f"\n下载完成，文件在目录: {VIDEO_SUBS_DIR}")
    print(f"已写入今日完成标记: {today_success_log_path()}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="下载 Bloomberg China Show（YouTube）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使 logs 下已有当天成功日志也重新下载",
    )
    args = parser.parse_args()
    download_first_video(force=args.force)
