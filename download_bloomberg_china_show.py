"""
从 YouTube 搜索 "china show Bloomberg Television"，并下载第一条视频。
（Bloomberg 频道无公开「搜索」标签，故用全站搜索并带频道名。）

- 视频与英文字幕保存到 video_subs 目录。
- 优先下载「今天」上传的视频；若没有，则下载搜索到的「最新一期」。
- 画质：优先「最高清晰度」（需安装 ffmpeg 才能合并出有声音）；无 ffmpeg 时自动改用单文件（约 1080p，有声音）。

供定时任务复用：fetch_china_show_entries()、filter_entries_by_upload_dates()、entry_watch_url()。

python版本大于3.10
使用前请安装:
  pip install -r requirements.txt
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import yt_dlp

from paths_config import VIDEO_SUBS_DIR, ensure_video_subs_dir


# 全站搜索词（带上频道名，第一条多为该频道节目）
SEARCH_QUERY = "china show Bloomberg Television"
# 文件名模板（会存到 video_subs 目录下）
OUTPUT_TEMPLATE = "Bloomberg_China_Show_%(title)s.%(ext)s"
# 搜索条数，用于从中筛选今天上传的（取第一条匹配）
SEARCH_COUNT = 10


def fetch_china_show_entries(*, search_count: int | None = None) -> list[dict[str, Any]]:
    """
    仅拉取元数据，不下载。返回 yt_dlp 的 entry 字典列表（可能含 id、title、upload_date 等）。
    """
    n = search_count if search_count is not None else SEARCH_COUNT
    search_string = f"ytsearch{n}:{SEARCH_QUERY}"
    extract_opts = {
        "quiet": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(extract_opts) as ydl:
        info = ydl.extract_info(search_string, download=False)
    if not info or not info.get("entries"):
        return []
    return [e for e in info["entries"] if e]


def filter_entries_by_upload_dates(
    entries: list[dict[str, Any]],
    dates_yyyymmdd: list[str],
) -> list[dict[str, Any]]:
    """只保留 upload_date 在给定 YYYYMMDD 列表中的条目（顺序与搜索一致）。"""
    want = set(dates_yyyymmdd)
    return [e for e in entries if (e.get("upload_date") or "") in want]


def entry_watch_url(entry: dict[str, Any]) -> str | None:
    vid = entry.get("id") or entry.get("url")
    if not vid:
        return None
    s = str(vid)
    if len(s) <= 15 and not s.startswith("http"):
        return f"https://www.youtube.com/watch?v={s}"
    return s if s.startswith("http") else None


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


def download_first_video():
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

    print(f"\n下载完成，文件在目录: {VIDEO_SUBS_DIR}")


if __name__ == "__main__":
    download_first_video()
