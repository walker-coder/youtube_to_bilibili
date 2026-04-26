# YouTube → 中英字幕 → 哔哩哔哩

将 YouTube 视频下载下来，英文字幕译为简体中文，烧录为画面双语字幕，并可一键投稿到哔哩哔哩。可选在投稿后轮询审核，若稿件被退回且说明中含时间轴，可自动剪片并替换重传。

## 环境要求

- Python 3.10+（建议）
- [ffmpeg](https://ffmpeg.org/) 已加入系统 `PATH`
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（随 `requirements.txt` 安装）

可选：若使用 YouTube 登录 cookies 下载时仍提示需 JS 验证，请安装 [Deno](https://deno.land/) 或 Node，或设置环境变量 `YTDLP_DENO_PATH` / `YTDLP_NODE_PATH` 指向可执行文件（见 [yt-dlp EJS 说明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)）。

云服务器若 **IPv6 不通**（`curl -6` 失败），下载默认启用 **仅 IPv4**（等同 `yt-dlp --force-ipv4`，环境变量 `YTDLP_FORCE_IPV4` 默认为 `1`）。若必须走 IPv6，设置 `YTDLP_FORCE_IPV4=0`。

若下载失败且日志出现 **PO Token / GVS**：新版 YouTube 下旧 `android` 客户端常需 PO Token；本仓库默认 **`YTDLP_YOUTUBE_PLAYER_CLIENT=android_vr`**（多数环境不要求 GVS PO Token）。仍失败时可试 `tv`、`web` 等或见 [yt-dlp PO Token 说明](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)。若仍 **HTTP 403**，请在服务器放置 **youtube_cookies.txt**。并执行 `yt-dlp -U` 保持最新。

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

在根目录放置浏览器导出的 **Netscape 格式** cookies，命名为 `youtube_cookies.txt` 或 `www.youtube.com_cookies`（`.txt` 可选），或设置环境变量 `YOUTUBE_COOKIES_FILE` / 命令行 `--cookies` 指向该文件。

**如何获取或更新 `youtube_cookies.txt`**

1. 用 Chrome / Edge 打开 [youtube.com](https://www.youtube.com) 并确认已登录 Google/YouTube。
2. 安装扩展 **「Get cookies.txt LOCALLY」**（Chrome 应用商店搜索全名；勿使用已下架的旧版「Get cookies.txt」）。
3. 在 YouTube 页面打开扩展，导出为 **Netscape** 格式。
4. 将导出文件放到项目根目录，**覆盖**原有文件：命名为 `youtube_cookies.txt`，或保留扩展默认名 `www.youtube.com_cookies`（可无 `.txt` 后缀）。若在其它机器跑脚本，把新文件拷到对应环境同一路径即可。

若下载仍 **HTTP 403**、提示需登录或行为像会话失效，即使文件未到「日历过期」，也应按上面步骤重新导出覆盖。Cookie 与账号登录态等价，**勿提交到 Git**（已在 `.gitignore` 中忽略）；若文件曾泄露，请在 Google 账号中退出其它会话或改密后再导出一份新文件。

**可选：自检文件内时间戳是否已过期**（仅表示各条 Cookie 的 `expires` 是否早于当前时间；服务端仍可能提前作废会话。）

```bash
python -c "from pathlib import Path; from datetime import datetime, timezone; now=int(datetime.now(timezone.utc).timestamp()); p=Path('youtube_cookies.txt'); bad=[l for l in p.read_text(encoding='utf-8', errors='replace').splitlines() if l and not l.startswith('#') and len(l.split('\t'))>=7 and int(l.split('\t')[4])>0 and int(l.split('\t')[4])<now]; print('expired lines:', len(bad) if bad else 0)"
```

在项目根目录执行；若输出 `expired lines: 0`，表示按 Netscape 第 5 列时间戳没有已过期行（`expires=0` 的会话型条目无法用此法判断）。

## 一键流水线

```bash
python youtube_to_bilibili.py "https://www.youtube.com/watch?v=..."
```

- 视频与中间文件默认在 `video_subs/`（目录已加入 `.gitignore`）。流水线**成功结束后**会删除当前视频的中间文件，**只保留** `yt_<视频ID>_bilingual.mp4`（其它视频 ID 的文件不删）。
- B 站标题默认在 YouTube 上传日期的前加前缀（`M/D/YYYY`），再接原标题；可用 `--title` 覆盖。
- 若需只生成本地双语视频、不上传：加 `--no-upload`。
- 若不需要上传后轮询审核与自动剪片：加 `--no-review-wait`（之后若要按退稿处理，见下节「退稿与替换稿件流程」）。
- 其它说明见 `youtube_to_bilibili.py` 文件头注释。

### 后台运行（Linux / macOS / SSH）

在服务器或本机终端里，只需传入 **YouTube 视频 ID**（`watch?v=` 后面一段），用 `nohup` 后台执行一键流水线，断开 SSH 后任务仍会继续。日志写入 **`logs/`**（目录已加入 `.gitignore`）。

```bash
chmod +x run_youtube_to_bilibili_bg.sh
./run_youtube_to_bilibili_bg.sh JOU5iy56FjY
# 等价于 python youtube_to_bilibili.py "https://www.youtube.com/watch?v=JOU5iy56FjY"

./run_youtube_to_bilibili_bg.sh JOU5iy56FjY --no-upload
```

其余参数（如 `--title`、`--no-review-wait`）写在视频 ID 之后即可。脚本默认使用 **`python3.11`**；若要改用其它解释器，可先执行 `export PYTHON=python3`（或指向虚拟环境里 `python`）。脚本内默认 **`export YTDLP_YOUTUBE_PLAYER_CLIENT=android_vr`**（若已在环境变量里设置过则不会覆盖）。查看进度：`tail -f logs/youtube_to_bilibili_<视频ID>_*.log`（具体文件名以脚本输出为准）。

### 从中间步骤继续

若下载已完成但后续步骤失败（例如翻译中断），可在 **`video_subs/`** 里文件齐全的前提下用 `resume_youtube_pipeline.py` 续跑，无需重下视频：

```bash
# 已有 yt_<视频ID>.mp4 与英文字幕，从翻译开始 → 烧录 → 上传
python resume_youtube_pipeline.py --from translate --vid 视频ID --url "https://www.youtube.com/watch?v=..."

# 已有中英 .vtt，仅从烧录开始
python resume_youtube_pipeline.py --from burn --vid 视频ID --url "..."

# 已有 yt_<视频ID>_bilingual.mp4，仅上传
python resume_youtube_pipeline.py --from upload --vid 视频ID --url "..."
```

只生成本地双语、不上传时可加 `--no-upload`（此时可不传 `--url`）。参数 `--title`、`--no-review-wait`、`--cookies` 等与一键脚本含义一致。

## 退稿与替换稿件流程（`bilibili_review.py`）

默认情况下，`youtube_to_bilibili.py` 上传成功后会**轮询审核**；若退回且说明中能解析出时间轴，会**自动剪片并替换分 P**，可多轮直到通过或超时（环境变量见下节）。

若你希望**先看清退稿原因、在本地改好成片再上传**，或替换接口曾报「获取 upload_id」等错误需要分步操作，可按下面顺序使用 **`bilibili_review.py`**（须在项目根目录、已配置 `bilibili_cookie.env`）。

### 1. 只查询当前状态与退稿说明（不剪片、不上传）

```bash
python bilibili_review.py BV1xxxxxxxxxx --query-reject
```

会拉取创作中心接口，打印审核/退稿文案；若能解析，会列出**建议处理的时间段**（与自动剪片逻辑一致）。接口快照会写入 `logs/bilibili_review_api_<BV>_*.json`。**不会**执行 ffmpeg、**不会**替换视频。

### 2. 直接按退稿说明自动剪出本地 `*_recut.mp4`（不上传）

若你希望脚本先按当前 BV 的退稿时间段帮你剪好本地视频，再决定是否上传，可直接复用内置 ffmpeg 剪片逻辑：

```bash
python bilibili_review.py BV1xxxxxxxxxx ./video_subs/你的成片.mp4 --recut-only
```

或使用：

```bash
python bilibili_review.py BV1xxxxxxxxxx --recut-only --video ./video_subs/你的成片.mp4
```

脚本会：

- 查询当前 BV 的退稿说明
- 解析时间段并应用 `BILIBILI_REVIEW_RECUT_PAD_SEC`
- 基于你提供的本地 MP4 生成同目录下的 `*_recut.mp4`
- **不会**上传、**不会**替换分 P

若当前稿件不是「已退回」，或未解析到时间段，命令会直接报错并停下，不会误剪视频。

### 3. 本地修改成片

你可以：

- 直接使用上一步生成的 `*_recut.mp4`
- 或按退稿说明继续手动剪辑/重编码，得到准备替换的 MP4

例如：`video_subs/yt_<视频ID>_bilingual_recut.mp4`

### 4. 仅上传并替换该 BV 的分 P

路径请紧接 BV 号，**`--replace-only` 放在路径之后**，避免参数被误解析：

```bash
python bilibili_review.py BV1xxxxxxxxxx ./video_subs/你的成片.mp4 --replace-only
```

或使用：

```bash
python bilibili_review.py BV1xxxxxxxxxx --replace-only --video ./video_subs/你的成片.mp4
```

若 UPOS 上传报错，可在服务器上设置 `BILIBILI_UPLOAD_LINE`（如 `bda2`），或升级 `bilibili-api-python`、检查 Cookie 与网络；详见 `bilibili_review.py` 文件头注释。

### 5. （可选）替换后继续自动轮询

替换成功后若仍希望**继续**由脚本轮询；若再次退稿则再按时间轴剪片并替换（与流水线步骤 5 相同逻辑），可在上一步追加 **`--resume-review`**：

```bash
python bilibili_review.py BV1xxxxxxxxxx ./video_subs/你的成片.mp4 --replace-only --resume-review
```

### 不上传、只轮询与自动剪片替换

若已通过 **`--query-reject`** 看过原因，且本地已改好成片，也可**不**用 `--replace-only`，直接以该成片为基准进入完整轮询（再退稿则自动剪片替换）：

```bash
python bilibili_review.py BV1xxxxxxxxxx ./video_subs/你的成片.mp4
```

第二参数省略时，脚本会选用 `video_subs/` 下最新的 `*_bilingual.mp4`，一般用于首次补跑轮询，不一定适合已改名的 recut 成片，**建议显式写出 MP4 路径**。

### 与一键流水线的关系

| 场景 | 做法 |
|------|------|
| 全程自动（上传 → 轮询 → 退稿则剪片替换） | 默认运行 `youtube_to_bilibili.py`，且**不要**加 `--no-review-wait` |
| 上传后不自动处理退稿 | 加 `--no-review-wait`，再按本节步骤 1～4（及可选 5）手动处理 |
| 只想查原因 | 仅用步骤 1：`--query-reject` |

## 其它脚本

| 脚本 | 说明 |
|------|------|
| `upload_bilibili.py` | 单独上传本地 MP4 到 B 站（需同上 Cookie） |
| `bilibili_review.py` | 轮询审核、退稿剪片替换；**仅查退稿** `--query-reject`；**仅本地剪片** `BV … 路径 --recut-only`；**仅替换分 P** `BV … 路径 --replace-only`；**替换后继续轮询** 再加 `--resume-review`。详见上节 |
| `download_bloomberg_china_show.py` | 按搜索词下载 Bloomberg「China Show」相关视频到 `video_subs/` |
| `bilingual_subs_to_video.py` | 将英/中字幕烧录进视频（流水线内部会调用） |
| `translate_subs_to_zh_hans.py` / `vtt_to_srt.py` | 翻译与字幕格式转换 |
| `resume_youtube_pipeline.py` | 从翻译/烧录/上传任一步继续，不重新下载（见上「从中间步骤继续」） |
| `run_youtube_to_bilibili_bg.sh` | 仅传视频 ID，`nohup` 后台跑 `youtube_to_bilibili.py`，日志在 `logs/`（见上「后台运行」） |

## 审核轮询（可选环境变量）

- `BILIBILI_REVIEW_POLL_INTERVAL_SEC`：轮询间隔（秒），默认 `30`
- `BILIBILI_REVIEW_MAX_WAIT_SEC`：单轮最长等待（秒），默认 `7200`；替换后会开启新一轮轮询
- `BILIBILI_REVIEW_MAX_REPLACE_ROUNDS`：最多剪片替换轮数，默认 `20`
- `BILIBILI_REVIEW_RECUT_PAD_SEC`：解析出的每段删除区间左右各扩展秒数，默认 `1.0`
- `BILIBILI_UPLOAD_LINE`：替换稿件时 UPOS 上传线路，可选 `bda2` / `qn` / `ws` / `bldsa`（小写）；不设时库内会依次尝试多线

更多说明见 `bilibili_review.py` 文件头。

## 免责声明

转载与投稿须遵守来源与哔哩哔哩社区规范，确保你有权使用相关素材；本仓库仅供个人学习与技术交流，请自行承担合规责任。

## 许可

若未另行声明，以仓库内文件为准；第三方依赖各自遵循其许可证。
