"""
将英文字幕文件（.vtt）中的对话翻译为简体中文，生成 .zh-Hans.vtt。
保持原有时码与格式（含 position 等）。仅翻译对话，整条为 [音效] 等非对话内容保留原文。
支持任意文件名，如 1.vtt -> 1.zh-Hans.vtt。

翻译方式：
- 不填 API Key（推荐免费）：使用免费引擎（Google 网页版 / MyMemory），无需付费。
  安装: pip install deep-translator
- 填 GOOGLE_TRANSLATE_API_KEY：使用 Google 官方 API，批量更快、更稳定，按量计费。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from paths_config import VIDEO_SUBS_DIR, ensure_video_subs_dir

# ---------- 不填则使用免费翻译；填则使用 Google 官方 API（按量计费）----------
GOOGLE_TRANSLATE_API_KEY = ""
# -------------------------------------------------------------------------------

# 使用 API 时每批最多条数（Google 单次请求建议 ≤128 条）
BATCH_SIZE = 25
# 使用 API 时每批最大字符数（Google 单次约 30k，留余量）
BATCH_MAX_CHARS = 28000
# 免费翻译：并发线程数（过大易触发限流 429，建议 3～8）
FREE_CONCURRENT_WORKERS = 5
# 免费翻译时每批之间的间隔（秒），略降限流概率
FREE_BATCH_DELAY = 0.15


def _zip_eq(a, b):
    """与 Python 3.10+ 的 zip(..., strict=True) 相同，兼容 3.9。"""
    la, lb = len(a), len(b)
    if la != lb:
        raise ValueError(f"zip 参数长度不一致: {la} != {lb}")
    return zip(a, b)


# 视为非对话、不翻译：整块仅为 [音效/说明] 时保留原文
_BRACKET_ONLY = re.compile(r"^\s*\[[\s\S]*?\]\s*$", re.MULTILINE)

# Google Cloud Translation - Basic (v2) 计费：每月前 50 万字符免费，超出部分 $20/百万字符
GOOGLE_TRANSLATE_FREE_CHARS_PER_MONTH = 500_000
GOOGLE_TRANSLATE_USD_PER_MILLION_CHARS = 20.0


def _is_dialogue(text: str) -> bool:
    """判断该条是否为「对话」：仅当整块是 [xxx] 音效/说明时不翻译。"""
    t = text.strip()
    if not t:
        return False
    if _BRACKET_ONLY.match(t):
        return False
    return True


def _parse_vtt(path: Path) -> list[tuple[str, str]]:
    """解析 VTT：返回 [(时间行, 文本块), ...]，时间行含 --> 与可选的 position。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    # 去掉 BOM
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    cues = []
    i = 0
    # 跳过头部（WEBVTT、Kind、Language、空行）
    while i < len(lines) and not re.match(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*", lines[i]):
        i += 1
    while i < len(lines):
        time_line = lines[i].strip()
        if not re.match(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*", time_line):
            i += 1
            continue
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        cues.append((time_line, "\n".join(text_lines)))
        while i < len(lines) and not lines[i].strip():
            i += 1
    return cues


def _get_api_key() -> str:
    return (GOOGLE_TRANSLATE_API_KEY or os.environ.get("GOOGLE_TRANSLATE_API_KEY", "") or "").strip()


def _translate_batch_via_google_api(texts: list[str]) -> list[str]:
    """Google API 一次请求翻译多条，返回与 texts 同序的译文列表。"""
    key = _get_api_key()
    if not key or not texts:
        return texts
    url = f"https://translation.googleapis.com/language/translate/v2?key={key}"
    body = json.dumps({"q": texts, "source": "en", "target": "zh-CN"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return [t["translatedText"] for t in data["data"]["translations"]]


def _translate_via_free(en_text: str) -> str:
    """免费翻译：优先 Google 网页版，失败则用 MyMemory（均无需 API Key）。"""
    try:
        from deep_translator import GoogleTranslator, MyMemoryTranslator
    except ImportError:
        print("使用免费翻译请先安装: pip install deep-translator")
        raise
    # 先试 Google（非官方接口，可能 429）
    try:
        out = GoogleTranslator(source="en", target="zh-CN").translate(en_text)
        if out:
            return out
    except Exception:
        pass
    # 备用：MyMemory 免费接口（每日约 1000 次请求限制）
    try:
        out = MyMemoryTranslator(source="en", target="zh-CN").translate(en_text)
        if out:
            return out
    except Exception:
        pass
    return en_text


def _translate_to_zh_hans(en_text: str) -> str:
    """单条翻译（仅免费分支使用）。"""
    if not en_text.strip():
        return en_text
    try:
        return _translate_via_free(en_text) or en_text
    except Exception as e:
        print(f"  翻译失败，保留原文: {e!r}")
        return en_text


def _print_cost_estimate(num_chars: int) -> None:
    """根据本次翻译字符数估算 Google 翻译 API 费用并打印。"""
    if num_chars <= 0:
        return
    # 计费按「发送给 API 的字符数」，即源语言（英文）字符数
    over = max(0, num_chars - GOOGLE_TRANSLATE_FREE_CHARS_PER_MONTH)
    usd = (over / 1_000_000) * GOOGLE_TRANSLATE_USD_PER_MILLION_CHARS
    print(f"  本文件约 {num_chars:,} 字符（仅对话）。")
    if num_chars <= GOOGLE_TRANSLATE_FREE_CHARS_PER_MONTH:
        print(f"  预估费用：在免费额度内（每月前 50 万字符免费）。")
    else:
        print(f"  预估费用：约 ${usd:.2f} USD（超出免费额度部分按 $20/百万字符）。")
    print()


def _build_batches(cues: list[tuple[str, str]]) -> list[tuple[list[int], list[str]]]:
    """按 BATCH_SIZE 与 BATCH_MAX_CHARS 拆成 [(索引列表, 文本列表), ...]。"""
    batches = []
    idx_list = []
    text_list = []
    total_chars = 0
    for i, (_, text) in enumerate(cues):
        if not _is_dialogue(text):
            continue
        n = len(text) + 1
        if (idx_list and (len(idx_list) >= BATCH_SIZE or total_chars + n > BATCH_MAX_CHARS)):
            batches.append((idx_list, text_list))
            idx_list, text_list, total_chars = [], [], 0
        idx_list.append(i)
        text_list.append(text)
        total_chars += n
    if idx_list:
        batches.append((idx_list, text_list))
    return batches


def translate_vtt_to_zh_hans(vtt_path: str | Path) -> Path:
    vtt_path = Path(vtt_path)
    if not vtt_path.is_file():
        raise FileNotFoundError(f"找不到文件: {vtt_path}")

    # 输出路径：与源同目录，xxx.en.vtt -> xxx.zh-Hans.vtt；1.vtt -> 1.zh-Hans.vtt
    stem = vtt_path.stem
    if stem.endswith(".en"):
        base = vtt_path.parent / stem[:-3]
    else:
        base = vtt_path.parent / stem
    out_path = base.parent / (base.name + ".zh-Hans.vtt")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cues = _parse_vtt(vtt_path)
    api_key = _get_api_key()
    use_batch = bool(api_key)
    # 估算本次翻译字符数与费用（仅对话部分计费）
    dialogue_chars = sum(len(t) for _, t in cues if _is_dialogue(t))
    if use_batch and dialogue_chars > 0:
        _print_cost_estimate(dialogue_chars)
    print(f"共 {len(cues)} 条字幕，正在翻译为简体中文（{'Google API 批量' if use_batch else '免费翻译'}）…")

    if use_batch:
        # 批量：先建索引→文本批次，再逐批请求，最后按索引填回
        zh_blocks = [None] * len(cues)
        for i, (_, text) in enumerate(cues):
            if not _is_dialogue(text):
                zh_blocks[i] = text
        batches = _build_batches(cues)
        done = 0
        for idx_list, text_list in batches:
            try:
                translated = _translate_batch_via_google_api(text_list)
                for idx, zh in _zip_eq(idx_list, translated):
                    zh_blocks[idx] = zh or cues[idx][1]
            except Exception as e:
                print(f"  本批翻译失败，保留原文: {e!r}")
                for idx in idx_list:
                    zh_blocks[idx] = cues[idx][1]
            done += len(idx_list)
            print(f"  已处理 {done}/{len(cues)} 条")
        # 组装输出
        out_lines = ["WEBVTT", "Kind: captions", "Language: zh-Hans", ""]
        for (time_line, _), zh_block in _zip_eq(cues, zh_blocks):
            out_lines.append(time_line)
            out_lines.append(zh_block)
            out_lines.append("")
    else:
        # 免费翻译：多线程并发，保留顺序
        zh_blocks = [None] * len(cues)
        for i, (_, text) in enumerate(cues):
            if not _is_dialogue(text):
                zh_blocks[i] = text
        indices_to_translate = [i for i in range(len(cues)) if zh_blocks[i] is None]
        total = len(indices_to_translate)
        done = 0

        def do_one(idx: int) -> tuple[int, str]:
            return idx, _translate_to_zh_hans(cues[idx][1])

        with ThreadPoolExecutor(max_workers=FREE_CONCURRENT_WORKERS) as ex:
            futures = {ex.submit(do_one, idx): idx for idx in indices_to_translate}
            for fut in as_completed(futures):
                idx, zh = fut.result()
                zh_blocks[idx] = zh or cues[idx][1]
                done += 1
                if done % 50 == 0:
                    print(f"  已处理 {done}/{total} 条")
                if done % (FREE_CONCURRENT_WORKERS * 10) == 0 and done < total:
                    time.sleep(FREE_BATCH_DELAY)

        out_lines = ["WEBVTT", "Kind: captions", "Language: zh-Hans", ""]
        for (time_line, _), zh_block in _zip_eq(cues, zh_blocks):
            out_lines.append(time_line)
            out_lines.append(zh_block)
            out_lines.append("")

    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"已生成: {out_path}")
    return out_path


def main():
    if len(sys.argv) < 2:
        # 默认：优先 video_subs 目录下 1.vtt 或第一个 .en.vtt
        ensure_video_subs_dir()
        one_vtt = VIDEO_SUBS_DIR / "1.vtt"
        if one_vtt.is_file():
            vtt_path = one_vtt
        else:
            candidates = list(VIDEO_SUBS_DIR.glob("*.en.vtt"))
            if not candidates:
                # 再试项目根目录
                root = Path(__file__).resolve().parent
                one_vtt = root / "1.vtt"
                candidates = list(root.glob("*.en.vtt"))
                if one_vtt.is_file():
                    vtt_path = one_vtt
                elif candidates:
                    vtt_path = candidates[0]
                else:
                    print("用法: python translate_subs_to_zh_hans.py <英文字幕.vtt>")
                    print(f"或将英文字幕放入 {VIDEO_SUBS_DIR} 后直接运行。")
                    sys.exit(1)
            else:
                vtt_path = candidates[0]
        print(f"使用: {vtt_path}")
    else:
        vtt_path = Path(sys.argv[1]).resolve()

    translate_vtt_to_zh_hans(vtt_path)


if __name__ == "__main__":
    main()
