"""
将本地视频投稿到哔哩哔哩（使用 bilibili-api，需浏览器 Cookie）。

凭证不要写在代码里，请用环境变量或本地 bilibili_cookie.env（勿提交到 Git）。

环境变量（或 bilibili_cookie.env 每行 KEY=value）：
  BILIBILI_SESSDATA   必填，Cookie 中的 SESSDATA
  BILIBILI_BILI_JCT   必填，Cookie 中的 bili_jct
  BILIBILI_BUVID3     可选，Cookie 中的 BUVID3
  BILIBILI_DEDEUSERID 可选，Cookie 中的 DedeUserID

可选：
  BILIBILI_TID        分区 ID，默认 138（财经商业），见创作中心分区说明
  BILIBILI_COVER      封面图片路径（仅当项目根目录不存在 bloomberg.jpg 时作为备选）

封面：若项目根目录存在 **bloomberg.jpg**，投稿时固定使用该图；否则再尝试环境变量 BILIBILI_COVER，最后才从视频首帧截取。

用法:
  pip install -r requirements.txt
  python upload_bilibili.py <视频.mp4> [标题]
  不传参数时优先上传 video_subs/preview_bilingual_90s.mp4，否则尝试 video_subs/1_subs.mp4

Cookie 获取：浏览器登录 bilibili.com → F12 → 应用 → Cookie → 复制对应字段。
项目根目录新建 bilibili_cookie.env（勿提交 Git，已加入 .gitignore），例如:
  BILIBILI_SESSDATA=你的值
  BILIBILI_BILI_JCT=你的值

投稿须遵守哔哩哔哩社区规范与版权要求；转载类请如实填写来源说明（脚本内默认已写）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from paths_config import VIDEO_SUBS_DIR, ensure_video_subs_dir

# 默认分区：138 财经商业（可按需改环境变量 BILIBILI_TID）
DEFAULT_TID = 138

# 存在则优先作为投稿封面（与项目根目录 youtube_to_bilibili 同级）
DEFAULT_COVER_NAME = "bloomberg.jpg"


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _resolve_cover_path(video_path: Path) -> tuple[Path, bool]:
    """返回 (封面路径, 是否为需删除的临时截图文件)。"""
    fixed = _project_root() / DEFAULT_COVER_NAME
    if fixed.is_file():
        return fixed, False
    env_cover = os.environ.get("BILIBILI_COVER", "").strip()
    if env_cover:
        p = Path(env_cover).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"封面文件不存在: {p}")
        return p, False
    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", prefix="bili_cover_")
    os.close(fd)
    tmp = Path(tmp_name)
    _extract_cover_from_video(video_path, tmp)
    return tmp, True


def _load_local_env() -> None:
    """从项目根目录 bilibili_cookie.env 加载 KEY=value。
    若环境变量未设置或为空，则用文件中的值填充。
    """
    env_path = Path(__file__).resolve().parent / "bilibili_cookie.env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not k:
                continue
            cur = os.environ.get(k, "").strip()
            if not cur:
                os.environ[k] = v


def _extract_cover_from_video(video_path: Path, out_path: Path) -> None:
    """用 ffmpeg 截取首帧作为封面。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "0.5",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"ffmpeg 截取封面失败: {r.stderr or r.stdout}")


async def _upload_async(
    video_path: Path,
    title: str,
    desc: str,
    tags: list[str],
    cover_path: Path,
    *,
    source: str,
) -> dict:
    from bilibili_api import Credential
    from bilibili_api.video_uploader import VideoMeta, VideoUploader, VideoUploaderPage

    sess = os.environ.get("BILIBILI_SESSDATA", "").strip()
    jct = os.environ.get("BILIBILI_BILI_JCT", "").strip()
    if not sess or not jct:
        raise RuntimeError(
            "请设置环境变量 BILIBILI_SESSDATA 与 BILIBILI_BILI_JCT，"
            "或创建 bilibili_cookie.env（见脚本注释）。"
        )

    buvid = os.environ.get("BILIBILI_BUVID3", "").strip() or None
    dede = os.environ.get("BILIBILI_DEDEUSERID", "").strip() or None
    credential = Credential(sessdata=sess, bili_jct=jct, buvid3=buvid, dedeuserid=dede)

    ok = await credential.check_valid()
    if not ok:
        raise RuntimeError("Cookie 无效或已过期，请重新登录后复制 Cookie。")

    tid = int(os.environ.get("BILIBILI_TID", str(DEFAULT_TID)))

    # 转载：非原创需填写来源
    meta = VideoMeta(
        tid=tid,
        title=title[:80],
        desc=desc[:2000],
        cover=str(cover_path),
        tags=tags,
        original=False,
        source=source[:200],
    )

    page = VideoUploaderPage(
        path=str(video_path),
        title=title[:80],
        description=desc[:2000],
    )
    uploader = VideoUploader(
        pages=[page],
        meta=meta,
        credential=credential,
    )
    return await uploader.start()


def upload_video_to_bilibili(
    video_path: str | Path,
    *,
    title: str | None = None,
    desc: str | None = None,
    tags: list[str] | None = None,
    source: str = "YouTube",
) -> dict:
    """
    供流水线或其它脚本调用：上传单个视频。
    需已配置 Cookie（环境变量或 bilibili_cookie.env）。
    """
    _load_local_env()
    ensure_video_subs_dir()
    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到视频: {video_path}")

    title = (title or video_path.stem)[:80]
    desc = desc or (
        "转载自网络。\n"
        "仅供个人学习交流，如有侵权请联系删除。"
    )
    tags = tags or ["YouTube"]
    cover_path, cover_is_temp = _resolve_cover_path(video_path)

    try:
        return asyncio.run(
            _upload_async(video_path, title, desc, tags, cover_path, source=source)
        )
    finally:
        if cover_is_temp and cover_path.is_file():
            try:
                cover_path.unlink()
            except OSError:
                pass


def main() -> None:
    _load_local_env()
    ensure_video_subs_dir()

    if len(sys.argv) >= 2:
        video_path = Path(sys.argv[1]).resolve()
        title_arg = sys.argv[2] if len(sys.argv) >= 3 else None
    else:
        preview = VIDEO_SUBS_DIR / "preview_bilingual_90s.mp4"
        fallback = VIDEO_SUBS_DIR / "1_subs.mp4"
        video_path = preview if preview.is_file() else fallback
        title_arg = None

    if not video_path.is_file():
        print("用法: python upload_bilibili.py <视频.mp4> [标题]")
        print(f"示例: python upload_bilibili.py {VIDEO_SUBS_DIR / 'preview_bilingual_90s.mp4'} \"China Show 标题\"")
        sys.exit(1)

    title = title_arg or video_path.stem
    desc = (
        "转载自 YouTube Bloomberg Television / The China Show。\n"
        "仅供个人学习交流，如有侵权请联系删除。"
    )
    tags = ["财经", "Bloomberg", "China", "The China Show"]

    try:
        cover_path, cover_is_temp = _resolve_cover_path(video_path)
    except Exception as e:
        print(e)
        sys.exit(1)

    try:
        result = asyncio.run(
            _upload_async(
                video_path,
                title,
                desc,
                tags,
                cover_path,
                source="YouTube Bloomberg Television",
            )
        )
        print("投稿成功:", result)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    finally:
        if cover_is_temp and cover_path.is_file():
            try:
                cover_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
