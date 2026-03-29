"""
一键：YouTube 链接 → 下载 → 英译简中字幕 → 烧录双语画面字幕 →（可选）上传哔哩哔哩。

依赖：pip install -r requirements.txt，系统 PATH 中有 ffmpeg；B 站投稿需配置 Cookie（见 upload_bilibili.py）。

用法:
  python youtube_to_bilibili.py "https://www.youtube.com/watch?v=xxxx"
  python youtube_to_bilibili.py "URL" --title "B站投稿标题"
  python youtube_to_bilibili.py "URL" --no-upload   # 只生成本地双语视频，不上传
  python youtube_to_bilibili.py "URL" --from-step 3  # 从烧录字幕起（需已有本地视频与英/简中 VTT）

说明：
- 下载：**仅 1080p**（`height=1080` 的视频轨 + 音频，或单文件 1080p）。若当前环境下列不出 1080p 流，将报错退出，**不会**降级为 720p/360p。
- 视频与字幕保存在 video_subs/，文件名以 yt_<视频ID> 为前缀。
- 翻译默认使用免费引擎（deep-translator），长视频可能较慢；可配置 Google 翻译 API（见 translate_subs_to_zh_hans.py）。
- 转载投稿须自行确保有权使用素材，并遵守哔哩哔哩社区规范。
- YouTube 登录态：不要用 bilibili_cookie.env（那是 B 站 KEY=value）。将浏览器导出的 Netscape 文件放在项目根目录，命名为 youtube_cookies.txt 或扩展默认名 www.youtube.com_cookies（.txt 可无），或传 --cookies / 环境变量 YOUTUBE_COOKIES_FILE。
- 若下载仍慢：先 yt-dlp -U；再配合 cookies 常能缓解限速。
- B 站投稿标题：默认会先去掉 YouTube 原标题末尾「| The China Show M/D/YYYY」（及可选尾随 |），再格式化为「清理后标题或 --title | YYYY/MM/DD」（YouTube upload_date）；无上传日期元数据时仅用标题。
- 上传成功后会轮询创作中心审核：若「已退回」且稿件问题中含【HH:MM:SS-HH:MM:SS】，则剪除对应片段并替换稿件后结束（不再轮询）。可用 --no-review-wait 关闭。环境变量见 bilibili_review.py。
- 流水线最后一步会清理 **video_subs/** 下当前视频的中间文件（下载的 mp4、vtt、srt 等），**仅保留** `yt_<视频ID>_bilingual.mp4`；其它视频 ID 的文件不受影响。
- 若链接含 &list=（播放列表），脚本已默认 noplaylist，只处理当前 watch?v= 视频；也可手动改成仅 https://www.youtube.com/watch?v=视频ID 。
- 若使用 cookies 后出现「Requested format is not available」：cookie 已生效，但带登录态时需通过 YouTube 验证，本机须安装 Deno/Node 等（见 https://github.com/yt-dlp/yt-dlp/wiki/EJS ）。脚本会先带 cookie 下载，失败则自动去掉 cookie 重试。可选环境变量：YTDLP_DENO_PATH / YTDLP_NODE_PATH 指向 deno.exe、node.exe（未加入 PATH 时）。
- 云服务器常见 IPv6 不通导致连接失败：默认启用 yt-dlp 的 force_ipv4（等同 --force-ipv4）。若需走 IPv6，设置环境变量 YTDLP_FORCE_IPV4=0。
- 机房 IP / 新版 YouTube：默认 `player_client=android_vr`（多数环境下不要求 GVS PO Token；旧版 `android` 常需 PO Token，见 yt-dlp PO-Token-Guide）。可用环境变量 `YTDLP_YOUTUBE_PLAYER_CLIENT` 覆盖（如 `android,web`）；设为 `none` 则不用。仍 403 时请在服务器放置 **youtube_cookies.txt**（浏览器导出 Netscape）。
- 断点续跑：`--from-step 2` 需已有 `yt_<ID>.mp4`（或 mkv/webm）与英文字幕 VTT；`3` 另需 `yt_<ID>.zh-Hans.vtt`；`4` 需已有 `yt_<ID>_bilingual.mp4`。仍会请求同一 URL 以解析视频 ID 与标题（不重复下载视频）。
"""

from __future__ import annotations

import argparse
import os
import re
import signal
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

# 仅 1080p：DASH 合并（最佳 1080p 视频轨 + 最佳音频）或极少数单文件 1080p；无匹配则 yt-dlp 失败。
YOUTUBE_FORMAT_1080P_ONLY = "bestvideo[height=1080]+bestaudio/best[height=1080]"

# 供 SIGINT/SIGTERM 时终止子进程（如 bilingual_subs_to_video）
_pipeline_child: subprocess.Popen | None = None


def _set_pipeline_child(p: subprocess.Popen | None) -> None:
    global _pipeline_child
    _pipeline_child = p


def _on_pipeline_signal(signum: int, frame) -> None:
    print(
        f"\n[{datetime.now().isoformat()}] 流水线被中断（signal {signum}）",
        flush=True,
    )
    c = _pipeline_child
    if c is not None and c.poll() is None:
        try:
            c.terminate()
        except OSError:
            pass
    raise SystemExit(130)


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


def _youtube_extractor_args() -> dict | None:
    """缓解 YouTube 403 / SABR；默认 android_vr（通常不需 GVS PO Token）。YTDLP_YOUTUBE_PLAYER_CLIENT=none 关闭。"""
    raw = (os.environ.get("YTDLP_YOUTUBE_PLAYER_CLIENT") or "android_vr").strip().lower()
    if raw in ("none", "off", "0", "false", "no"):
        return None
    clients = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    if not clients:
        return None
    return {"youtube": {"player_client": clients}}


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


def _zh_hans_vtt_path_from_en_vtt(en_vtt: Path) -> Path:
    """与 translate_subs_to_zh_hans.translate_vtt_to_zh_hans 输出路径一致。"""
    stem = en_vtt.stem
    if stem.endswith(".en"):
        base = en_vtt.parent / stem[:-3]
    else:
        base = en_vtt.parent / stem
    return base.parent / (base.name + ".zh-Hans.vtt")


def _find_local_video_for_id(video_id: str) -> Path:
    for ext in ("mp4", "mkv", "webm"):
        p = VIDEO_SUBS_DIR / f"yt_{video_id}.{ext}"
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        f"未找到本地视频 video_subs/yt_{video_id}.mp4（或 .mkv/.webm）。请先完成步骤 1。"
    )


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


def _cleanup_video_subs_keep_bilingual_only(vid: str) -> None:
    """删除 video_subs 下与当前视频 ID 相关的中间文件，仅保留 yt_{vid}_bilingual.mp4（不删其它视频前缀）。"""
    keep_name = f"yt_{vid}_bilingual.mp4"
    ensure_video_subs_dir()
    removed: list[str] = []
    for p in sorted(VIDEO_SUBS_DIR.glob(f"yt_{vid}*")):
        if not p.is_file():
            continue
        if p.name == keep_name:
            continue
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as e:
            print(f"  警告：无法删除 {p.name}: {e}", file=sys.stderr)
    if removed:
        if len(removed) <= 15:
            print(f"  已删除中间文件（{len(removed)} 个）: {', '.join(removed)}")
        else:
            print(
                f"  已删除中间文件（{len(removed)} 个）: {', '.join(removed[:15])} …"
            )
    else:
        print(f"  无其它 yt_{vid}* 文件需删除。")


def _is_unavailable_format_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return (
        "requested format is not available" in msg
        or "no video formats" in msg
    )


def _raise_no_1080_stream(err: BaseException) -> None:
    raise RuntimeError(
        "未找到 1080p 视频流，已按要求停止（不下载更低清晰度）。"
        "请尝试：更新 yt-dlp；若日志出现 PO Token / GVS：默认已用 android_vr，也可试 "
        "YTDLP_YOUTUBE_PLAYER_CLIENT=tv 或按 https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide 配置；"
        "并放置 youtube_cookies.txt。"
    ) from err


def _log_youtube_download_quality(info: dict) -> None:
    """打印 yt-dlp 返回的已下载视频清晰度与编码（stdout，便于 nohup/重定向进日志）。"""
    w = info.get("width")
    h = info.get("height")
    res = (info.get("resolution") or "").strip()
    fid = (info.get("format_id") or "").strip()
    vc = (info.get("vcodec") or "").strip() or None
    ac = (info.get("acodec") or "").strip() or None

    bits: list[str] = []
    if w and h:
        bits.append(f"{w}x{h}")
    elif h:
        bits.append(f"{h}p")
    if res and res not in bits and not (w and h):
        bits.append(res)
    if fid:
        bits.append(f"format_id={fid}")
    if vc and vc != "none":
        bits.append(f"vcodec={vc}")
    if ac and ac != "none":
        bits.append(f"acodec={ac}")

    summary = "; ".join(bits) if bits else "（顶层未含分辨率，见分轨）"
    print(f"  拉取到的清晰度: {summary}")

    rd = info.get("requested_downloads") or []
    if rd:
        print("  分轨详情:")
        for i, r in enumerate(rd, 1):
            rf = (r.get("format_id") or "?").strip()
            rw = r.get("width")
            rh = r.get("height")
            rvc = (r.get("vcodec") or "").strip()
            rac = (r.get("acodec") or "").strip()
            rsize = r.get("filesize") or r.get("filesize_approx")
            dim = f"{rw}x{rh}" if rw and rh else (f"{rh}p" if rh else "")
            line = f"    轨 {i}: format_id={rf}"
            if dim:
                line += f", {dim}"
            if rvc and rvc != "none":
                line += f", vcodec={rvc}"
            if rac and rac != "none":
                line += f", acodec={rac}"
            if rsize is not None:
                try:
                    line += f", ~{int(rsize) // (1024 * 1024)}MiB"
                except (TypeError, ValueError):
                    pass
            print(line)


_RE_CHINA_SHOW_TITLE_SUFFIX = re.compile(
    r"\s*\|\s*The China Show\s+\d{1,2}/\d{1,2}/\d{4}\s*\|?\s*$",
    re.IGNORECASE,
)


def _strip_china_show_title_suffix(title: str) -> str:
    """去掉 Bloomberg 节目名单独出现在末尾时的「| The China Show M/D/YYYY」（及可选尾随 |）。"""
    raw = title.strip()
    stripped = _RE_CHINA_SHOW_TITLE_SUFFIX.sub("", raw).strip()
    return stripped if stripped else raw


def _youtube_upload_date_ymd_slash(info: dict) -> str | None:
    """从 yt-dlp 信息解析上传日期，格式 YYYY/MM/DD（用于 B 站标题后缀）。"""
    ud = (info.get("upload_date") or info.get("release_date") or "").strip()
    if len(ud) == 8 and ud.isdigit():
        try:
            dt = datetime.strptime(ud, "%Y%m%d")
            return f"{dt.year}/{dt.month:02d}/{dt.day:02d}"
        except ValueError:
            pass
    return None


def download_youtube(
    url: str,
    *,
    cookies_file: str | None = None,
    no_youtube_cookies: bool = False,
) -> tuple[Path, Path, str, str, str | None]:
    """返回 (视频路径, 英文字幕 vtt 路径, 视频 ID, YouTube 标题, 上传日期 YYYY/MM/DD 或 None)。"""
    ensure_video_subs_dir()
    cookie_path = None if no_youtube_cookies else _resolve_youtube_cookiefile(cookies_file)

    base_opts: dict = {
        # 链接里常带 &list=...，只下载当前视频，不展开整个播放列表
        "noplaylist": True,
        "format": YOUTUBE_FORMAT_1080P_ONLY,
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
    if (os.environ.get("YTDLP_FORCE_IPV4", "1").strip().lower() not in ("0", "false", "no")):
        base_opts["force_ipv4"] = True
    ex_args = _youtube_extractor_args()
    if ex_args:
        base_opts["extractor_args"] = ex_args
        pc = ex_args.get("youtube", {}).get("player_client", [])
        print(f"  YouTube player_client: {pc}（可用 YTDLP_YOUTUBE_PLAYER_CLIENT 覆盖，none=关闭）")
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
            if "Only images are available" in msg or _is_unavailable_format_error(e):
                print(
                    "  提示：使用 cookies 时未能解析出可用视频流（常见于 YouTube 验证未通过、"
                    "本机未配置 JS 运行环境，见 https://github.com/yt-dlp/yt-dlp/wiki/EJS ）。"
                    "将不使用 cookies 重试下载…"
                )
                try:
                    info = _extract(False)
                except DownloadError as e2:
                    if _is_unavailable_format_error(e2):
                        _raise_no_1080_stream(e2)
                    raise
            else:
                raise
    else:
        try:
            info = _extract(False)
        except DownloadError as e:
            if _is_unavailable_format_error(e):
                _raise_no_1080_stream(e)
            raise

    if not info:
        raise RuntimeError("yt-dlp 未返回视频信息")
    vid = str(info.get("id") or "")
    if not vid:
        raise RuntimeError("无法解析视频 ID")
    _log_youtube_download_quality(info)
    title = (info.get("title") or "video").strip()
    date_ymd = _youtube_upload_date_ymd_slash(info)
    video_path = _resolve_downloaded_video(info, vid)
    en_vtt = _find_en_vtt(vid)
    return video_path, en_vtt, vid, title, date_ymd


def _youtube_extract_info_no_download(
    url: str,
    *,
    cookies_file: str | None = None,
    no_youtube_cookies: bool = False,
) -> dict:
    """仅拉取元数据，不下载视频/字幕（用于 --from-step 2/3/4）。"""
    ensure_video_subs_dir()
    cookie_path = None if no_youtube_cookies else _resolve_youtube_cookiefile(cookies_file)

    base_opts: dict = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if (os.environ.get("YTDLP_FORCE_IPV4", "1").strip().lower() not in ("0", "false", "no")):
        base_opts["force_ipv4"] = True
    ex_args = _youtube_extractor_args()
    if ex_args:
        base_opts["extractor_args"] = ex_args
    js_rt = _js_runtimes_from_env()
    if js_rt:
        base_opts["js_runtimes"] = js_rt

    def _extract(with_cookie: bool):
        opts = {**base_opts}
        if with_cookie and cookie_path:
            p = Path(cookie_path).expanduser().resolve()
            if not p.is_file():
                raise FileNotFoundError(f"找不到 YouTube cookies 文件: {p}")
            opts["cookiefile"] = str(p)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    if cookie_path:
        p = Path(cookie_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"找不到 YouTube cookies 文件: {p}")
        try:
            return _extract(True)
        except DownloadError as e:
            msg = str(e)
            if "Only images are available" in msg or _is_unavailable_format_error(e):
                print(
                    "  提示：使用 cookies 时未能解析出元数据（常见于 YouTube 验证未通过、"
                    "本机未配置 JS 运行环境）。将不使用 cookies 重试…"
                )
                try:
                    return _extract(False)
                except DownloadError as e2:
                    raise
            else:
                raise
    else:
        return _extract(False)


def run_pipeline(
    url: str,
    *,
    bilibili_title: str | None,
    no_upload: bool,
    cookies_file: str | None = None,
    no_youtube_cookies: bool = False,
    no_review_wait: bool = False,
    from_step: int = 1,
) -> Path:
    # 最后一步：清理 video_subs 内本视频中间文件，仅保留 yt_{vid}_bilingual.mp4
    total_steps = (
        6
        if (not no_upload and not no_review_wait)
        else (4 if no_upload else 5)
    )

    vid: str
    yt_title: str
    yt_date_ymd: str | None
    video_path: Path | None = None
    en_vtt: Path | None = None
    zh_vtt: Path | None = None
    out_bilingual: Path | None = None

    if from_step <= 1:
        print(f"步骤 1/{total_steps}：下载 YouTube 视频与英文字幕…")
        video_path, en_vtt, vid, yt_title, yt_date_ymd = download_youtube(
            url,
            cookies_file=cookies_file,
            no_youtube_cookies=no_youtube_cookies,
        )
        print(f"  视频: {video_path}")
        if yt_date_ymd:
            print(f"  上传日期: {yt_date_ymd}（B 站标题：原标题 | 此日期）")
        print(f"  英文字幕: {en_vtt}")
    else:
        info = _youtube_extract_info_no_download(
            url,
            cookies_file=cookies_file,
            no_youtube_cookies=no_youtube_cookies,
        )
        if not info:
            raise RuntimeError("yt-dlp 未返回视频信息")
        vid = str(info.get("id") or "")
        if not vid:
            raise RuntimeError("无法解析视频 ID")
        yt_title = (info.get("title") or "video").strip()
        yt_date_ymd = _youtube_upload_date_ymd_slash(info)
        if from_step <= 3:
            video_path = _find_local_video_for_id(vid)
            en_vtt = _find_en_vtt(vid)
            print(f"  视频: {video_path}")
            if yt_date_ymd:
                print(f"  上传日期: {yt_date_ymd}（B 站标题：原标题 | 此日期）")
            print(f"  英文字幕: {en_vtt}")
        if from_step == 4:
            out_bilingual = VIDEO_SUBS_DIR / f"yt_{vid}_bilingual.mp4"
            if not out_bilingual.is_file():
                raise FileNotFoundError(
                    f"未找到 {out_bilingual}，无法从步骤 4 继续。请先完成步骤 3。"
                )
        elif from_step == 3:
            assert en_vtt is not None
            zh_vtt = _zh_hans_vtt_path_from_en_vtt(en_vtt)
            if not zh_vtt.is_file():
                raise FileNotFoundError(
                    f"未找到简体字幕 {zh_vtt}。请先完成步骤 2，或确认文件名与 translate_subs_to_zh_hans 输出一致。"
                )

    if from_step <= 2:
        assert en_vtt is not None
        print(f"步骤 2/{total_steps}：翻译为简体中文（可能较久）…")
        zh_vtt = translate_vtt_to_zh_hans(en_vtt)

    if from_step <= 3:
        assert video_path is not None and en_vtt is not None and zh_vtt is not None
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
        p = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))
        _set_pipeline_child(p)
        try:
            code = p.wait()
        finally:
            _set_pipeline_child(None)
        if code == 130:
            raise SystemExit(130)
        if code != 0:
            raise RuntimeError("烧录字幕失败（bilingual_subs_to_video.py 退出码非 0）")
        print(f"  已生成: {out_bilingual}")
    else:
        assert out_bilingual is not None and out_bilingual.is_file()

    if no_upload:
        print("已跳过上传（--no-upload）。")
        print(f"步骤 {total_steps}/{total_steps}：清理 video_subs（仅保留双语成片）…")
        _cleanup_video_subs_keep_bilingual_only(vid)
        return out_bilingual

    review_after = not no_review_wait
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
        print(f"步骤 5/{total_steps}：轮询审核状态（退回则按时间轴剪片并替换稿件）…")
        from bilibili_review import run_review_flow_sync

        run_review_flow_sync(bvid, out_bilingual)
    print(f"步骤 {total_steps}/{total_steps}：清理 video_subs（仅保留双语成片）…")
    _cleanup_video_subs_keep_bilingual_only(vid)
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
    ap.add_argument(
        "--from-step",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        metavar="N",
        help="从第 N 步开始：1=下载（默认）2=翻译 3=烧录 4=上传；2–4 需同一 URL 且 video_subs 内已有对应中间文件",
    )
    args = ap.parse_args()
    old_int = signal.signal(signal.SIGINT, _on_pipeline_signal)
    old_term = signal.signal(signal.SIGTERM, _on_pipeline_signal) if hasattr(signal, "SIGTERM") else None
    try:
        run_pipeline(
            args.url,
            bilibili_title=args.bilibili_title,
            no_upload=args.no_upload,
            cookies_file=args.cookies,
            no_youtube_cookies=args.no_youtube_cookies,
            no_review_wait=args.no_review_wait,
            from_step=args.from_step,
        )
    except (RuntimeError, FileNotFoundError, DownloadError, TimeoutError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n[{datetime.now().isoformat()}] 流水线被中断（KeyboardInterrupt）",
            flush=True,
        )
        sys.exit(130)
    finally:
        signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    main()
