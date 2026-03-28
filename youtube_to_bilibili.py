"""
一键：YouTube 链接 → 下载 → 英译简中字幕 → 烧录双语画面字幕 →（可选）上传哔哩哔哩。

依赖：pip install -r requirements.txt，系统 PATH 中有 ffmpeg；B 站投稿需配置 Cookie（见 upload_bilibili.py）。

用法:
  python youtube_to_bilibili.py "https://www.youtube.com/watch?v=xxxx"
  python youtube_to_bilibili.py "URL" --title "B站投稿标题"
  python youtube_to_bilibili.py "URL" --no-upload   # 只生成本地双语视频，不上传

说明：
- 视频与字幕保存在 video_subs/，文件名以 yt_<视频ID> 为前缀。
- 翻译默认使用免费引擎（deep-translator），长视频可能较慢；可配置 Google 翻译 API（见 translate_subs_to_zh_hans.py）。
- 转载投稿须自行确保有权使用素材，并遵守哔哩哔哩社区规范。
- YouTube 登录态：不要用 bilibili_cookie.env（那是 B 站 KEY=value）。将浏览器导出的 Netscape 文件放在项目根目录，命名为 youtube_cookies.txt 或扩展默认名 www.youtube.com_cookies（.txt 可无），或传 --cookies / 环境变量 YOUTUBE_COOKIES_FILE。
- 若下载仍慢：先 yt-dlp -U；再配合 cookies 常能缓解限速。
- B 站投稿标题：默认在开头附加 YouTube 上传日期（M/D/YYYY，与站点元数据 upload_date 一致），再接原标题或 --title。
- 上传成功后会轮询创作中心审核：若「已退回」且稿件问题中含【HH:MM:SS-HH:MM:SS】，则剪除对应片段并替换稿件后结束（不再轮询）。可用 --no-review-wait 关闭。环境变量见 bilibili_review.py。
- 若链接含 &list=（播放列表），脚本已默认 noplaylist，只处理当前 watch?v= 视频；也可手动改成仅 https://www.youtube.com/watch?v=视频ID 。
- 若使用 cookies 后出现「Requested format is not available」：cookie 已生效，但带登录态时需通过 YouTube 验证，本机须安装 Deno/Node 等（见 https://github.com/yt-dlp/yt-dlp/wiki/EJS ）。脚本会先带 cookie 下载，失败则自动去掉 cookie 重试。可选环境变量：YTDLP_DENO_PATH / YTDLP_NODE_PATH 指向 deno.exe、node.exe（未加入 PATH 时）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from paths_config import PROJECT_ROOT, VIDEO_SUBS_DIR, ensure_video_subs_dir
from translate_subs_to_zh_hans import translate_vtt_to_zh_hans
from upload_bilibili import upload_video_to_bilibili
from vtt_to_srt import vtt_to_srt

# 浏览器扩展导出的 Netscape cookies，与 bilibili_cookie.env（B 站 KEY=value）不是同一种文件
# 按顺序尝试：自定义名 → 扩展默认导出名（如 Get cookies.txt LOCALLY）
_YOUTUBE_COOKIE_NAMES = (
    "youtube_cookies.txt",
    "www.youtube.com_cookies.txt",
    "www.youtube.com_cookies",
)


def _find_project_root_youtube_cookies() -> Path | None:
    for name in _YOUTUBE_COOKIE_NAMES:
        p = PROJECT_ROOT / name
        if p.is_file():
            return p
    return None


def _js_runtimes_from_env() -> dict | None:
    """若设置 YTDLP_DENO_PATH 或 YTDLP_NODE_PATH，显式指定可执行文件路径（未在 PATH 时）。"""
    out: dict = {}
    deno = (os.environ.get("YTDLP_DENO_PATH") or os.environ.get("YTDLP_JS_RUNTIME_PATH") or "").strip()
    if deno:
        out["deno"] = {"path": deno}
    node = (os.environ.get("YTDLP_NODE_PATH") or "").strip()
    if node:
        out["node"] = {"path": node}
    return out if out else None


def _resolve_youtube_cookiefile(explicit: str | None) -> str | None:
    """优先：命令行 --cookies；其次环境变量 YOUTUBE_COOKIES_FILE；再次项目根目录常见文件名。"""
    if explicit:
        return explicit
    env_p = (os.environ.get("YOUTUBE_COOKIES_FILE") or "").strip()
    if env_p:
        return env_p
    found = _find_project_root_youtube_cookies()
    if found:
        return str(found.resolve())
    return None


def _resolve_downloaded_video(info: dict, video_id: str) -> Path:
    fp = info.get("filepath")
    if fp and Path(fp).is_file():
        return Path(fp).resolve()
    for r in info.get("requested_downloads") or []:
        p = r.get("filepath")
        if p and Path(p).is_file():
            return Path(p).resolve()
    for ext in ("mp4", "mkv", "webm"):
        p = VIDEO_SUBS_DIR / f"yt_{video_id}.{ext}"
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(f"未找到已下载视频文件（预期 yt_{video_id}.mp4 等）")


def _find_en_vtt(video_id: str) -> Path:
    cands = sorted(VIDEO_SUBS_DIR.glob(f"yt_{video_id}*.vtt"))
    for p in cands:
        low = p.name.lower()
        if "zh" in low or "hans" in low:
            continue
        if ".en." in low or low.endswith(".en.vtt") or "-en" in low or ".en-" in low:
            return p
    for p in cands:
        if "zh" not in p.name.lower():
            return p
    if cands:
        return cands[0]
    raise FileNotFoundError(
        f"未找到英文字幕 yt_{video_id}*.vtt。该视频可能没有英文字幕或未开放字幕。"
    )


def _youtube_upload_date_label(info: dict) -> str | None:
    """从 yt-dlp 信息解析上传日期，格式与 YouTube 页常见展示一致：M/D/YYYY（如 3/27/2026）。"""
    ud = (info.get("upload_date") or info.get("release_date") or "").strip()
    if len(ud) == 8 and ud.isdigit():
        try:
            dt = datetime.strptime(ud, "%Y%m%d")
            return f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            pass
    return None


def download_youtube(
    url: str,
    *,
    cookies_file: str | None = None,
    no_youtube_cookies: bool = False,
) -> tuple[Path, Path, str, str, str | None]:
    """返回 (视频路径, 英文字幕 vtt 路径, 视频 ID, YouTube 标题, 上传日期标签或 None)。"""
    ensure_video_subs_dir()
    cookie_path = None if no_youtube_cookies else _resolve_youtube_cookiefile(cookies_file)

    base_opts: dict = {
        # 链接里常带 &list=...，只下载当前视频，不展开整个播放列表
        "noplaylist": True,
        # bv*+ba：更宽松的可合并音视频；再回退到原逻辑与单文件 best
        "format": "bv*+ba/bestvideo+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(VIDEO_SUBS_DIR / "yt_%(id)s.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "embed_subs": False,
        "concurrent_fragment_downloads": 8,
    }
    js_rt = _js_runtimes_from_env()
    if js_rt:
        base_opts["js_runtimes"] = js_rt
        print(f"  使用 JS 运行环境: {list(js_rt.keys())}（来自环境变量 YTDLP_DENO_PATH / YTDLP_NODE_PATH）")

    def _extract(with_cookie: bool):
        opts = {**base_opts}
        if with_cookie and cookie_path:
            p = Path(cookie_path).expanduser().resolve()
            if not p.is_file():
                raise FileNotFoundError(f"找不到 YouTube cookies 文件: {p}")
            opts["cookiefile"] = str(p)
            print(f"  使用 YouTube cookies: {p}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    if cookie_path:
        p = Path(cookie_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"找不到 YouTube cookies 文件: {p}")
        try:
            info = _extract(True)
        except DownloadError as e:
            msg = str(e)
            if "Requested format is not available" in msg or "Only images are available" in msg:
                print(
                    "  提示：使用 cookies 时未能解析出可用视频流（常见于 YouTube 验证未通过、"
                    "本机未配置 JS 运行环境，见 https://github.com/yt-dlp/yt-dlp/wiki/EJS ）。"
                    "将不使用 cookies 重试下载…"
                )
                info = _extract(False)
            else:
                raise
    else:
        info = _extract(False)

    if not info:
        raise RuntimeError("yt-dlp 未返回视频信息")
    vid = str(info.get("id") or "")
    if not vid:
        raise RuntimeError("无法解析视频 ID")
    title = (info.get("title") or "video").strip()
    date_label = _youtube_upload_date_label(info)
    video_path = _resolve_downloaded_video(info, vid)
    en_vtt = _find_en_vtt(vid)
    return video_path, en_vtt, vid, title, date_label


def run_pipeline(
    url: str,
    *,
    bilibili_title: str | None,
    no_upload: bool,
    cookies_file: str | None = None,
    no_youtube_cookies: bool = False,
    no_review_wait: bool = False,
) -> Path:
    total_steps = (
        5
        if (not no_upload and not no_review_wait)
        else (3 if no_upload else 4)
    )
    print(f"步骤 1/{total_steps}：下载 YouTube 视频与英文字幕…")
    video_path, en_vtt, vid, yt_title, yt_date_label = download_youtube(
        url,
        cookies_file=cookies_file,
        no_youtube_cookies=no_youtube_cookies,
    )
    print(f"  视频: {video_path}")
    if yt_date_label:
        print(f"  上传日期: {yt_date_label}（将用于 B 站标题前缀）")
    print(f"  英文字幕: {en_vtt}")

    print(f"步骤 2/{total_steps}：翻译为简体中文（可能较久）…")
    zh_vtt = translate_vtt_to_zh_hans(en_vtt)

    print(f"步骤 3/{total_steps}：转为 SRT 并烧录双语画面字幕…")
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

    if no_upload:
        print("已跳过上传（--no-upload）。")
        return out_bilingual

    review_after = not no_review_wait
    print(f"步骤 4/{total_steps}：上传哔哩哔哩…")
    base_title = bilibili_title or yt_title
    if yt_date_label:
        title = f"{yt_date_label} {base_title}"
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
        tags=["YouTube", "中英字幕", "转载"],
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
    return out_bilingual


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube → 中英字幕烧录 → 哔哩哔哩")
    ap.add_argument("url", help="YouTube 视频链接（watch 或 youtu.be）")
    ap.add_argument(
        "--title",
        dest="bilibili_title",
        default=None,
        help="B 站投稿标题（默认用 YouTube 原标题）",
    )
    ap.add_argument(
        "--no-upload",
        action="store_true",
        help="只生成本地双语视频，不上传",
    )
    ap.add_argument(
        "--cookies",
        metavar="FILE",
        default=None,
        help="YouTube Netscape cookies（默认自动使用项目根目录 youtube_cookies.txt 或 www.youtube.com_cookies*）",
    )
    ap.add_argument(
        "--no-youtube-cookies",
        action="store_true",
        help="下载 YouTube 时不使用 cookies（忽略项目内 cookies 文件）",
    )
    ap.add_argument(
        "--no-review-wait",
        action="store_true",
        help="上传成功后不轮询审核、不自动按退回剪片替换",
    )
    args = ap.parse_args()
    try:
        run_pipeline(
            args.url,
            bilibili_title=args.bilibili_title,
            no_upload=args.no_upload,
            cookies_file=args.cookies,
            no_youtube_cookies=args.no_youtube_cookies,
            no_review_wait=args.no_review_wait,
        )
    except (RuntimeError, FileNotFoundError, DownloadError, TimeoutError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
