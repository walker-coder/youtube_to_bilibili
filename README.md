# YouTube → 中英字幕 → 哔哩哔哩

将 YouTube 视频下载、英文字幕译为简体中文、烧录为画面双语字幕，并可一键投稿哔哩哔哩。可选在投稿后轮询审核；若稿件被退回且说明中含时间轴，可自动剪片并替换重传。

---

## 环境要求

- Python 3.10+（建议）
- [ffmpeg](https://ffmpeg.org/) 已加入系统 `PATH`
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（随 `requirements.txt` 安装）

**可选**

- YouTube 需 JS 验证时：安装 [Deno](https://deno.land/) 或 Node，或设置 `YTDLP_DENO_PATH` / `YTDLP_NODE_PATH`（见 [yt-dlp EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)）。
- 云服务器 **IPv6 不通**：下载默认 **仅 IPv4**（`YTDLP_FORCE_IPV4` 默认 `1`）；若必须 IPv6，设 `YTDLP_FORCE_IPV4=0`。
- **PO Token / GVS / 403**：本仓库默认 `YTDLP_YOUTUBE_PLAYER_CLIENT=android_vr`；仍失败可试 `tv`、`web` 等，或放置 `youtube_cookies.txt`，并保持 `yt-dlp -U`。

## 安装

```bash
cd /path/to/bloombreg
pip install -r requirements.txt
```

## 配置

### 哔哩哔哩（投稿）

在项目根目录创建 `bilibili_cookie.env`（**勿提交 Git**，见 `.gitignore`）：

```env
BILIBILI_SESSDATA=你的SESSDATA
BILIBILI_BILI_JCT=你的bili_jct
```

可选：`BILIBILI_BUVID3`、`BILIBILI_DEDEUSERID`、`BILIBILI_TID`（分区，默认 138）。Cookie 获取方式见 `upload_bilibili.py` 顶部注释。

### 封面

根目录存在 **`bloomberg.jpg`** 时优先作封面；否则可用 `BILIBILI_COVER`，再否则从视频首帧截取。

### YouTube（可选）

根目录放置 Netscape 格式 cookies：`youtube_cookies.txt` 或 `www.youtube.com_cookies*.txt`，或设 `YOUTUBE_COOKIES_FILE` / 使用 `--cookies`。

---

## 一键流水线（任意 YouTube 链接）

```bash
python youtube_to_bilibili.py "https://www.youtube.com/watch?v=..."
```

| 行为 | 说明 |
|------|------|
| 输出目录 | `video_subs/`，成功结束后仅保留当前视频的 `yt_<视频ID>_bilingual.mp4` |
| 标题 | 默认可带上传日期；`--title` 覆盖 |
| 只本地、不上传 | `--no-upload` |
| 不上传后轮询审核/剪片 | `--no-review-wait` |
| 从某步继续 | `--from-step` 2/3/4（需已有中间文件） |

更多见 `youtube_to_bilibili.py` 文件头注释。

### 后台运行（Linux / SSH）

```bash
chmod +x run_youtube_to_bilibili_bg.sh
./run_youtube_to_bilibili_bg.sh <YouTube视频ID>
# 参数接在 ID 后，如 --no-upload
```

日志：`logs/youtube_to_bilibili_<视频ID>_*.log`。解释器可用 `export PYTHON=python3`。

### 中断后续跑

下载已完成而后续失败时，可用 `resume_youtube_pipeline.py`（需 `video_subs` 内文件齐全）：

```bash
python resume_youtube_pipeline.py --from translate --vid <视频ID> --url "https://..."
python resume_youtube_pipeline.py --from burn --vid <视频ID> --url "https://..."
python resume_youtube_pipeline.py --from upload --vid <视频ID> --url "https://..."
```

`--no-upload` 时可不传 `--url`。详见脚本内说明。

---

## The China Show（Bloomberg）自动化

搜索词与日期筛选逻辑在 `download_bloomberg_china_show.py` 中实现；**两条入口用途不同**：

| 脚本 | 用途 |
|------|------|
| **`china_show_daily_to_bilibili.py`** | 搜「今日上传」的 China Show，对**未在** `china_show_processed_video_ids.json` 中的视频调用 **`youtube_to_bilibili.run_pipeline`**（下载 → 译 → 烧录 → 上传）。适合定时「一条龙」。 |
| **`download_bloomberg_china_show.py`** | **仅**用 yt-dlp 下载到 `video_subs/`（`Bloomberg_China_Show_*.mp4`），**不**走翻译/B 站。适合只要源片存档或单独调试下载。 |

二者**不要**为同一目的重复定时：要上 B 站用 **`china_show_daily_to_bilibili.py`** 即可。

### 日志与跳过（定时任务）

| 文件（均在 `logs/`） | 含义 |
|----------------------|------|
| `china_show_YYYYMMDD.log` | 仅 **`download_bloomberg_china_show.py`**：当日已成功下载则再次运行会跳过；`--force` 重跑。 |
| `china_show_daily_YYYYMMDD.log` | 仅 **`china_show_daily_to_bilibili.py`**：当日**本轮待处理视频全部流水线成功**后写入；存在则当日后续运行直接退出；`--force` 忽略。 |

### 定时示例（cron）

工作日每 10 分钟跑一次 daily（路径自行替换）：

```cron
*/10 * * * 1-5 cd /path/to/bloombreg && /usr/bin/python3 china_show_daily_to_bilibili.py >> logs/china_show_daily_cron.log 2>&1
```

仅下载脚本同理，改用 `download_bloomberg_china_show.py` 与独立日志文件即可。

---

## 其它脚本

| 脚本 | 说明 |
|------|------|
| `upload_bilibili.py` | 单独上传本地 MP4 |
| `bilibili_review.py` | 对已投稿 BV 轮询审核；退回则解析时间轴、剪片并替换。`python bilibili_review.py BVxxx [本地双语mp4]` |
| `bilingual_subs_to_video.py` | 英/中字幕烧录（流水线内部调用） |
| `translate_subs_to_zh_hans.py` / `vtt_to_srt.py` | 翻译与字幕格式 |
| `zh_sensitive_replace.py` | 中文字幕敏感词替换（流水线可选用） |
| `test_bilibili_cookie.py` / `test_cjk_font_subtitle.py` | 连接与字体测试 |

---

## 审核与剪片（`bilibili_review`）

上传后轮询、退回说明解析、ffmpeg 剪片等见 `bilibili_review.py` 文件头（环境变量、完整接口 JSON 落盘 `logs/bilibili_review_api_*.json`、区间左右扩展 `BILIBILI_REVIEW_RECUT_PAD_SEC` 等）。

常用环境变量：

- `BILIBILI_REVIEW_POLL_INTERVAL_SEC`：轮询间隔（秒），默认 `30`
- `BILIBILI_REVIEW_MAX_WAIT_SEC`：单轮最长等待（秒），默认 `7200`
- `BILIBILI_REVIEW_MAX_REPLACE_ROUNDS`：最多剪片替换轮数，默认 `20`

---

## 免责声明

转载与投稿须遵守来源与哔哩哔哩社区规范；本仓库仅供个人学习与技术交流，请自行承担合规责任。

## 许可

若未另行声明，以仓库内文件为准；第三方依赖各自遵循其许可证。
