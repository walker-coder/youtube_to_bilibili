#!/usr/bin/env bash
# 后台运行 youtube_to_bilibili.py，只需传入 YouTube 视频 ID（watch?v= 后面的 11 位）。
# 用法:
#   chmod +x run_youtube_to_bilibili_bg.sh
#   ./run_youtube_to_bilibili_bg.sh JOU5iy56FjY
#   ./run_youtube_to_bilibili_bg.sh JOU5iy56FjY --no-upload
# 日志在 logs/ 目录（含步骤 3 烧录进度与中断记录，均写入下方 LOG）；进程为 nohup 后台任务，断开 SSH 后仍继续跑。
# 默认使用 python3.11；可通过环境变量 PYTHON 覆盖，例如：PYTHON=python3 ./run_youtube_to_bilibili_bg.sh ...
# 默认 YTDLP_YOUTUBE_PLAYER_CLIENT=android_vr（避免 android 客户端缺 PO Token）；若已在外部 export 则沿用。
# 默认 BLOOMBREG_FFMPEG_BURN_ARGS=-threads 1（烧录字幕时降低 ffmpeg 并行与峰值内存，小内存 VPS 适用）；
#   覆盖示例：BLOOMBREG_FFMPEG_BURN_ARGS='-threads 2' ./run_youtube_to_bilibili_bg.sh ...
# 非交互重定向到文件时 Python 默认会块缓冲 stdout，异常退出时末尾几行/Traceback 可能未刷盘；
#   故默认 PYTHONUNBUFFERED=1 且使用 python -u，便于日志完整记录终止原因（OOM/kill -9 仍无 traceback）。

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <视频ID> [传给 youtube_to_bilibili.py 的其它参数...]" >&2
  echo "示例: $0 JOU5iy56FjY" >&2
  exit 1
fi

VID="$1"
shift

ROOT="$(cd "$(dirname "$0")" && pwd)"
URL="https://www.youtube.com/watch?v=${VID}"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="${LOG_DIR}/youtube_to_bilibili_${VID}_${STAMP}.log"

PYTHON="${PYTHON:-python3.11}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export YTDLP_YOUTUBE_PLAYER_CLIENT="${YTDLP_YOUTUBE_PLAYER_CLIENT:-android_vr}"
export BLOOMBREG_FFMPEG_BURN_ARGS="${BLOOMBREG_FFMPEG_BURN_ARGS:--threads 1}"
cd "$ROOT"

nohup "$PYTHON" -u youtube_to_bilibili.py "$URL" "$@" >>"$LOG" 2>&1 &
PID=$!

echo "已后台启动"
echo "  视频 ID: ${VID}"
echo "  URL:     ${URL}"
echo "  yt-dlp:  YTDLP_YOUTUBE_PLAYER_CLIENT=${YTDLP_YOUTUBE_PLAYER_CLIENT}"
echo "  烧录:    BLOOMBREG_FFMPEG_BURN_ARGS=${BLOOMBREG_FFMPEG_BURN_ARGS}"
echo "  PID:     ${PID}"
echo "  日志:    ${LOG}"
echo "查看日志: tail -f ${LOG}"
