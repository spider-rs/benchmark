"""Spider Browser -- https://spider.cloud

CDP proxy with stealth, residential proxy, and multi-browser rotation.
Requires: SPIDER_API_KEY env var.
"""

import os


async def connect() -> str:
    token = os.environ["SPIDER_API_KEY"]
    return f"wss://browser.spider.cloud/v1/browser?token={token}"


async def disconnect() -> None:
    pass
