"""
定时任务：查询「今天上传」的 The China Show（Bloomberg 搜索），对尚未处理过的视频调用 youtube_to_bilibili.run_pipeline。

去重：项目根目录 china_show_processed_video_ids.json 记录已成功的 YouTube 视频 ID，不会重复投稿。

启动时若 logs 下已有当日文件 china_show_daily_YYYYMMDD.log（本轮待处理视频全部成功跑完流水线后写入），则直接退出；加 --force 可忽略该检查。

依赖与 youtube_to_bilibili 相同；建议在视频上线后每日运行一次。

Windows 任务计划程序示例（每天 18:30）:
  程序: python
  参数: D:\\path\\to\\bloombreg\\china_show_daily_to_bilibili.py
  起始于: D:\\path\\to\\bloombreg

Linux cron 示例:
  30 18 * * * cd /path/to/bloombreg && /path/to/python china_show_daily_to_bilibili.py >> logs/china_show_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from paths_config import LOGS_DIR, PROJECT_ROOT, ensure_logs_dir

from download_bloomberg_china_show import (
    SEARCH_COUNT,
    entry_watch_url,
    fetch_china_show_entries,
    filter_entries_by_upload_dates,
)
from youtube_to_bilibili import run_pipeline

PROCESSED_IDS_FILE = PROJECT_ROOT / "china_show_processed_video_ids.json"

DAILY_LOG_PREFIX = "china_show_daily_"
DAILY_LOG_SUFFIX = ".log"


def today_daily_log_path(d: date | None = None) -> Path:
    day = d or date.today()
    return LOGS_DIR / f"{DAILY_LOG_PREFIX}{day.strftime('%Y%m%d')}{DAILY_LOG_SUFFIX}"


def already_ran_daily_success() -> bool:
    ensure_logs_dir()
    return today_daily_log_path().exists()


def _write_daily_success_log() -> None:
    ensure_logs_dir()
    p = today_daily_log_path()
    p.write_text(
        f"completed_at={datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )


def _load_processed_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, list):
        return {str(x).strip() for x in data if x}
    if isinstance(data, dict) and "video_ids" in data:
        return {str(x).strip() for x in data["video_ids"] if x}
    return set()


def _save_processed_ids(path: Path, ids: set[str]) -> None:
    path.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _video_id(entry: dict) -> str | None:
    vid = entry.get("id")
    if vid is None:
        return None
    s = str(vid).strip()
    return s if s else None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="今日 China Show → youtube_to_bilibili（按视频 ID 去重）"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将处理的 URL，不调用流水线、不写出去重文件",
    )
    ap.add_argument(
        "--search-count",
        type=int,
        default=SEARCH_COUNT,
        metavar="N",
        help=f"ytsearch 条数（默认 {SEARCH_COUNT}）",
    )
    ap.add_argument(
        "--include-yesterday",
        action="store_true",
        help="除今天外，仍包含「日历昨天」上传（时区/上架延迟时可开）",
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
        "--state-file",
        type=Path,
        default=PROCESSED_IDS_FILE,
        help=f"去重 JSON 路径（默认 {PROCESSED_IDS_FILE.name}）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="忽略 logs 下当日 china_show_daily_YYYYMMDD.log 已存在时的退出",
    )
    args = ap.parse_args()

    if not args.force and not args.dry_run and already_ran_daily_success():
        p = today_daily_log_path()
        print(
            f"今日已成功跑过本脚本（存在 {p}），跳过。需要重跑请加 --force"
        )
        sys.exit(0)

    dates = [date.today().strftime("%Y%m%d")]
    if args.include_yesterday:
        dates.append((date.today() - timedelta(days=1)).strftime("%Y%m%d"))

    entries = fetch_china_show_entries(search_count=args.search_count)
    if not entries:
        print("未搜到任何视频，退出。")
        sys.exit(0)

    today_entries = filter_entries_by_upload_dates(entries, dates)
    if not today_entries:
        print(f"搜索前 {args.search_count} 条中，无 upload_date 为 {dates} 的视频，退出。")
        sys.exit(0)

    processed = _load_processed_ids(args.state_file)
    pending: list[tuple[str, str, dict]] = []
    for e in today_entries:
        vid = _video_id(e)
        url = entry_watch_url(e)
        if not vid or not url:
            print(f"跳过无 ID/URL 的条目: {e.get('title', '')!r}")
            continue
        if vid in processed:
            print(f"已处理过，跳过: {vid} {e.get('title', '')!r}")
            continue
        pending.append((vid, url, e))

    if not pending:
        print("今日条目均已处理过，无需运行流水线。")
        sys.exit(0)

    print(
        f"待处理 {len(pending)} 个视频（日期 {dates}）；"
        f"状态文件: {args.state_file}"
    )

    if args.dry_run:
        for vid, url, e in pending:
            print(f"  [dry-run] {vid} {url}\n    {e.get('title', '')}")
        sys.exit(0)

    old_int = signal.signal(signal.SIGINT, signal.SIG_DFL)
    old_term = (
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if hasattr(signal, "SIGTERM")
        else None
    )
    try:
        ok = 0
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
            processed.add(vid)
            _save_processed_ids(args.state_file, processed)
            print(f"已记录成功: {vid} -> {args.state_file}")
            ok += 1
        if ok == len(pending) and pending:
            _write_daily_success_log()
            print(f"\n当日全部待处理视频已成功，已写入: {today_daily_log_path()}")
    finally:
        signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    main()
