"""
定时任务：按搜索词与 upload_date 筛选 The China Show（默认「今天」；可用 --search-keyword-date 指定同一天），对匹配到的视频调用 youtube_to_bilibili.run_pipeline。

去重：仅 logs/china_show_daily_YYYYMMDD.log —— 成功解析到当日候选视频（YouTube video id）后写入；存在则当日后续 cron 直接跳过（省 yt 搜索与负载）。
若同一天还要再跑，请用 --force 或删除当日 log，或使用 --no-daily-log。

依赖与 youtube_to_bilibili 相同。

Windows 任务计划程序示例（每天 18:30）:
  程序: python
  参数: D:\\path\\to\\bloombreg\\china_show_daily_to_bilibili.py
  起始于: D:\\path\\to\\bloombreg

Linux cron 示例（工作日每 10 分钟）:
  */10 * * * 1-5 cd /path/to/bloombreg && /usr/bin/python3 china_show_daily_to_bilibili.py >> logs/china_show_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import date, datetime
from pathlib import Path

from paths_config import LOGS_DIR, ensure_logs_dir

from download_bloomberg_china_show import (
    SEARCH_COUNT,
    entry_watch_url,
    fetch_china_show_entries,
    filter_entries_by_upload_dates,
)
from youtube_to_bilibili import run_pipeline

DAILY_LOG_PREFIX = "china_show_daily_"
DAILY_LOG_SUFFIX = ".log"


def daily_success_log_path(d: date | None = None) -> Path:
    """当日已成功锁定候选视频的标记（与 download_bloomberg 的 china_show_*.log 区分命名）。"""
    day = d or date.today()
    return LOGS_DIR / f"{DAILY_LOG_PREFIX}{day.strftime('%Y%m%d')}{DAILY_LOG_SUFFIX}"


def already_ran_daily_success() -> bool:
    ensure_logs_dir()
    return daily_success_log_path().exists()


def _write_daily_success_log(note: str = "") -> None:
    ensure_logs_dir()
    p = daily_success_log_path()
    line = f"completed_at={datetime.now().isoformat(timespec='seconds')}"
    if note:
        line += f" {note}"
    p.write_text(line + "\n", encoding="utf-8")


def _video_id(entry: dict) -> str | None:
    vid = entry.get("id")
    if vid is None:
        return None
    s = str(vid).strip()
    return s if s else None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="今日 China Show → youtube_to_bilibili（可选 china_show_daily 日志去重）"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将处理的 URL，不调用流水线、不写 daily log",
    )
    ap.add_argument(
        "--search-count",
        type=int,
        default=SEARCH_COUNT,
        metavar="N",
        help=f"ytsearch 条数（默认 {SEARCH_COUNT}）",
    )
    ap.add_argument(
        "--search-keyword-date",
        metavar="YYYY-MM-DD",
        default=None,
        help=(
            "搜索关键词里的日期（Bloomberg Television China Show M/D/YYYY），"
            "并作为 upload_date 筛选日（YYYYMMDD）；省略则搜索词与筛选日均为今天"
        ),
    )
    ap.add_argument("--no-upload", action="store_true", help="同 youtube_to_bilibili")
    ap.add_argument("--cookies", metavar="FILE", default=None, help="YouTube cookies 文件")
    ap.add_argument(
        "--no-youtube-cookies",
        action="store_true",
        help="不使用 YouTube cookies",
    )
    ap.add_argument(
        "--no-review-wait",
        action="store_true",
        help="上传后不轮询审核",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="忽略 logs 下当日 china_show_daily_YYYYMMDD.log",
    )
    ap.add_argument(
        "--no-daily-log",
        action="store_true",
        help="不写入、不检测 china_show_daily_YYYYMMDD.log（每次匹配到的今日视频都会跑流水线）",
    )
    args = ap.parse_args()

    search_keyword_day: date | None = None
    if args.search_keyword_date:
        try:
            search_keyword_day = datetime.strptime(
                args.search_keyword_date, "%Y-%m-%d"
            ).date()
        except ValueError:
            ap.error(
                f"--search-keyword-date 需为 YYYY-MM-DD，收到: {args.search_keyword_date!r}"
            )

    if not args.no_daily_log and not args.force and already_ran_daily_success():
        p = daily_success_log_path()
        print(
            f"今日 china_show_daily 已有标记（存在 {p}），跳过。"
            " 若仍需跑请用 --force 或删除该文件。"
        )
        sys.exit(0)

    filter_day = search_keyword_day if search_keyword_day is not None else date.today()
    dates = [filter_day.strftime("%Y%m%d")]

    entries = fetch_china_show_entries(
        search_count=args.search_count,
        search_keyword_day=search_keyword_day,
    )
    if not entries:
        print("未搜到任何视频，退出。")
        sys.exit(0)
    if args.dry_run:
        print(f"搜索命中 {len(entries)} 个视频（未筛选）：")
        for e in entries:
            vid = _video_id(e) or "unknown"
            title = e.get("title") or ""
            upload_date = e.get("upload_date") or "unknown"
            url = entry_watch_url(e) or "unknown"
            print(f"  - {vid} | upload_date={upload_date} | title={title}")
            print(f"    url={url}")

    today_entries = filter_entries_by_upload_dates(entries, dates)
    if not today_entries:
        print(f"搜索前 {args.search_count} 条中，无 upload_date 为 {dates} 的视频，退出。")
        sys.exit(0)
    today_entries = [
        e for e in today_entries if "The China Show" in str(e.get("title") or "")
    ]
    if not today_entries:
        print(
            f"搜索前 {args.search_count} 条中，无标题包含 'The China Show' 且 "
            f"upload_date 为 {dates} 的视频，退出。"
        )
        sys.exit(0)

    pending: list[tuple[str, str, dict]] = []
    for e in today_entries:
        vid = _video_id(e)
        url = entry_watch_url(e)
        if not vid or not url:
            print(f"跳过无 ID/URL 的条目: {e.get('title', '')!r}")
            continue
        pending.append((vid, url, e))

    if not pending:
        print("今日匹配条目中无有效 ID/URL，退出。")
        sys.exit(0)

    print(f"待处理 {len(pending)} 个视频（日期 {dates}）。")
    for vid, url, e in pending:
        title = e.get("title") or ""
        upload_date = e.get("upload_date") or "unknown"
        print(f"  - {vid} | upload_date={upload_date} | title={title}")

    if args.dry_run:
        for vid, url, e in pending:
            print(
                f"  [dry-run] {vid} {url}\n"
                f"    upload_date={e.get('upload_date', 'unknown')} | title={e.get('title', '')}"
            )
        sys.exit(0)

    if not args.no_daily_log:
        vids = ",".join(vid for vid, _, _ in pending)
        _write_daily_success_log(note=f"video_ids={vids}")
        print(f"已写入当日完成标记: {daily_success_log_path()}")

    old_int = signal.signal(signal.SIGINT, signal.SIG_DFL)
    old_term = (
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if hasattr(signal, "SIGTERM")
        else None
    )
    try:
        for vid, url, e in pending:
            title = e.get("title") or ""
            print(f"\n>>> 开始流水线: {vid} | {title!r}\n")
            try:
                run_pipeline(
                    url,
                    bilibili_title=None,
                    no_upload=args.no_upload,
                    cookies_file=args.cookies,
                    no_youtube_cookies=args.no_youtube_cookies,
                    no_review_wait=args.no_review_wait,
                    from_step=1,
                )
            except Exception as ex:
                print(f"错误: {vid} 流水线失败: {ex}", file=sys.stderr)
                continue
    finally:
        signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    main()
