#!/usr/bin/env bash
# 后台运行 youtube_to_bilibili.py，只需传入 YouTube 视频 ID（watch?v= 后面的 11 位）。
# 用法:
#   chmod +x run_youtube_to_bilibili_bg.sh
#   ./run_youtube_to_bilibili_bg.sh JOU5iy56FjY
#   ./run_youtube_to_bilibili_bg.sh JOU5iy56FjY --no-upload
# 日志在 logs/ 目录；进程为 nohup 后台任务，断开 SSH 后仍继续跑。

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

PYTHON="${PYTHON:-python}"
cd "$ROOT"

nohup "$PYTHON" youtube_to_bilibili.py "$URL" "$@" >>"$LOG" 2>&1 &
PID=$!

echo "已后台启动"
echo "  视频 ID: ${VID}"
echo "  URL:     ${URL}"
echo "  PID:     ${PID}"
echo "  日志:    ${LOG}"
echo "查看日志: tail -f ${LOG}"
