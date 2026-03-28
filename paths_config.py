"""
视频与字幕统一存放目录配置，供下载、翻译、嵌入脚本读写。
"""

from pathlib import Path

# 项目根目录（本文件所在目录）
PROJECT_ROOT = Path(__file__).resolve().parent

# 视频与字幕存放目录（相对项目根）
VIDEO_SUBS_DIR_NAME = "video_subs"
VIDEO_SUBS_DIR = PROJECT_ROOT / VIDEO_SUBS_DIR_NAME


def ensure_video_subs_dir() -> Path:
    """确保 video_subs 目录存在，返回该路径。"""
    VIDEO_SUBS_DIR.mkdir(parents=True, exist_ok=True)
    return VIDEO_SUBS_DIR
