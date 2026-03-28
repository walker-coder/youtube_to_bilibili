# YouTube → 中英字幕 → 哔哩哔哩

将 YouTube 视频下载下来，英文字幕译为简体中文，烧录为画面双语字幕，并可一键投稿到哔哩哔哩。可选在投稿后轮询审核，若稿件被退回且说明中含时间轴，可自动剪片并替换重传。

## 环境要求

- Python 3.10+（建议）
- [ffmpeg](https://ffmpeg.org/) 已加入系统 `PATH`
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（随 `requirements.txt` 安装）

可选：若使用 YouTube 登录 cookies 下载时仍提示需 JS 验证，请安装 [Deno](https://deno.land/) 或 Node，或设置环境变量 `YTDLP_DENO_PATH` / `YTDLP_NODE_PATH` 指向可执行文件（见 [yt-dlp EJS 说明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)）。

云服务器若 **IPv6 不通**（`curl -6` 失败），下载默认启用 **仅 IPv4**（等同 `yt-dlp --force-ipv4`，环境变量 `YTDLP_FORCE_IPV4` 默认为 `1`）。若必须走 IPv6，设置 `YTDLP_FORCE_IPV4=0`。

若视频流仍 **HTTP 403**：脚本默认 `YTDLP_YOUTUBE_PLAYER_CLIENT=android`（可多客户端：`android,web`）；并请在服务器放置 **youtube_cookies.txt**（从本机浏览器导出 Netscape cookies 后上传）。仍失败则执行 `yt-dlp -U` 升级。

## 安装

```bash
cd /path/to/bloombreg
pip install -r requirements.txt
```

## 配置

### 哔哩哔哩（投稿）

在项目根目录创建 `bilibili_cookie.env`（**勿提交到 Git**，见 `.gitignore`），例如：

```env
BILIBILI_SESSDATA=你的SESSDATA
BILIBILI_BILI_JCT=你的bili_jct
```

可选：`BILIBILI_BUVID3`、`BILIBILI_DEDEUSERID`、`BILIBILI_TID`（分区，默认 138）。

Cookie 获取方式：浏览器登录 bilibili.com → 开发者工具 → 应用 → Cookie → 复制对应字段。详情见 `upload_bilibili.py` 顶部注释。

### 封面

若项目根目录存在 **`bloomberg.jpg`**，投稿时优先使用该图作为封面；否则可使用环境变量 `BILIBILI_COVER` 指定路径，再否则从视频首帧截取。

### YouTube（可选，用于下载限速或会员内容）

在根目录放置浏览器导出的 Netscape cookies，命名为 `youtube_cookies.txt` 或 `www.youtube.com_cookies`（`.txt` 可选），或设置 `YOUTUBE_COOKIES_FILE` / 使用 `--cookies`。**不要**把 YouTube cookies 写进 `bilibili_cookie.env`（那是 B 站专用格式）。

## 一键流水线

```bash
python youtube_to_bilibili.py "https://www.youtube.com/watch?v=..."
```

- 视频与中间文件默认在 `video_subs/`（目录已加入 `.gitignore`）。
- B 站标题默认在 YouTube 上传日期的前加前缀（`M/D/YYYY`），再接原标题；可用 `--title` 覆盖。
- 若需只生成本地双语视频、不上传：加 `--no-upload`。
- 若不需要上传后轮询审核与自动剪片：加 `--no-review-wait`。
- 其它说明见 `youtube_to_bilibili.py` 文件头注释。

## 其它脚本

| 脚本 | 说明 |
|------|------|
| `upload_bilibili.py` | 单独上传本地 MP4 到 B 站（需同上 Cookie） |
| `bilibili_review.py` | 对已投稿 BV 轮询审核；退回则按 `【HH:MM:SS-HH:MM:SS】` 剪片并替换稿件。可单独执行：`python bilibili_review.py BVxxx [可选：本地双语mp4路径]` |
| `download_bloomberg_china_show.py` | 按搜索词下载 Bloomberg「China Show」相关视频到 `video_subs/` |
| `bilingual_subs_to_video.py` | 将英/中字幕烧录进视频（流水线内部会调用） |
| `translate_subs_to_zh_hans.py` / `vtt_to_srt.py` | 翻译与字幕格式转换 |

## 审核轮询（可选环境变量）

- `BILIBILI_REVIEW_POLL_INTERVAL_SEC`：轮询间隔（秒），默认 `30`
- `BILIBILI_REVIEW_MAX_WAIT_SEC`：最长等待（秒），默认 `7200`

## 免责声明

转载与投稿须遵守来源与哔哩哔哩社区规范，确保你有权使用相关素材；本仓库仅供个人学习与技术交流，请自行承担合规责任。

## 许可

若未另行声明，以仓库内文件为准；第三方依赖各自遵循其许可证。
