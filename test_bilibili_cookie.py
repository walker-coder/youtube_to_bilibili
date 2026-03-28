"""临时脚本：仅验证 bilibili_cookie.env 中的 Cookie，不上传视频。用后可删。"""
import asyncio
import os
import sys

import upload_bilibili as u

u._load_local_env()


async def main() -> None:
    s = os.environ.get("BILIBILI_SESSDATA", "").strip()
    j = os.environ.get("BILIBILI_BILI_JCT", "").strip()
    print("SESSDATA 长度:", len(s), "  bili_jct 长度:", len(j))
    if not s or not j:
        print("失败: 未读取到 BILIBILI_SESSDATA 或 BILIBILI_BILI_JCT。")
        print("请确认项目根目录存在 bilibili_cookie.env，且两行已填写（无多余空格）。")
        sys.exit(1)
    from bilibili_api import Credential

    c = Credential(sessdata=s, bili_jct=j)
    ok = await c.check_valid()
    print("Cookie 校验:", "有效，可以投稿" if ok else "无效或已过期，请重新登录后复制 Cookie")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    asyncio.run(main())
