"""Telegram 一键设置：python telegram_setup.py

步骤：
1. 在 Telegram 找 @BotFather，发送 /newbot，按提示取名字，拿到 bot token。
2. 打开 .env，把 token 贴在 TELEGRAM_BOT_TOKEN= 后面，存档。
3. 在 Telegram 打开你的新机器人，按 Start（或随便发一句话）。
4. 运行这个脚本：它会自动找到你的 chat ID、写进 .env，并发一条测试消息。
"""
from __future__ import annotations

import sys

import requests
from dotenv import dotenv_values, set_key

from app.config import ROOT

ENV_PATH = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, **params) -> dict:
    try:
        resp = requests.post(API.format(token=token, method=method), json=params, timeout=45)
    except requests.RequestException as exc:
        sys.exit(f"连不上 Telegram（{exc.__class__.__name__}），请检查网络。")
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        if resp.status_code in (401, 404):
            sys.exit("bot token 不对。请到 @BotFather 重新复制，贴进 .env 的 TELEGRAM_BOT_TOKEN。")
        sys.exit(f"Telegram 返回错误：{data.get('description', resp.status_code)}")
    return data["result"]


def _private_chats(updates: list) -> dict:
    chats = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat and chat.get("type") == "private":
            chats[chat["id"]] = chat.get("first_name") or chat.get("username") or ""
    return chats


def main() -> None:
    if not ENV_PATH.exists():
        sys.exit("找不到 .env，请先双击 setup.bat。")
    token = (dotenv_values(ENV_PATH).get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        sys.exit("还没填 bot token：请把 @BotFather 给你的 token 贴进 .env 的 TELEGRAM_BOT_TOKEN= 后面。")

    bot = call(token, "getMe")
    print(f"机器人：@{bot['username']}")

    chats = _private_chats(call(token, "getUpdates", timeout=0))
    if not chats:
        print(f"等你在 Telegram 给 @{bot['username']} 发一句话（最多等 2 分钟）…")
        for _ in range(4):
            chats = _private_chats(call(token, "getUpdates", timeout=30))
            if chats:
                break
    if not chats:
        sys.exit(f"还没收到你的消息。请在 Telegram 打开 @{bot['username']}，按 Start 或发一句话，再运行一次。")
    chat_id, name = list(chats.items())[-1]
    set_key(str(ENV_PATH), "TELEGRAM_CHAT_ID", str(chat_id), quote_mode="never")
    print(f"已找到你的 chat ID 并写进 .env（{name}）")

    call(token, "sendMessage", chat_id=chat_id,
         text="✅ Gold Analyzer 已连上 Telegram。之后的金价提醒会发到这里。\n\n仅供参考，不构成投资建议。")
    print("已发送测试消息，请看 Telegram。")


if __name__ == "__main__":
    main()
