"""Telegram 推送。同一个提醒在冷却时间内不重复，每天有上限，避免变成噪音。
没设 Telegram token 时，提醒只打印在终端。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from app.config import Settings
from app.db import Database

log = logging.getLogger(__name__)

DISCLAIMER = "仅供参考，不构成投资建议。"


class Alerter:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    def send(self, key: str, text: str) -> bool:
        """返回 True 表示这次真的发出（或打印）了。"""
        now = datetime.now(timezone.utc)
        if self.db.alert_sent_since(key, now - timedelta(minutes=self.settings.alert_cooldown_min)):
            return False
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.db.alerts_count_since(day_start) >= self.settings.max_alerts_per_day:
            log.info("今天的提醒已达上限 %d 条，跳过：%s", self.settings.max_alerts_per_day, key)
            return False

        message = f"{text}\n\n{DISCLAIMER}"
        delivered = self._deliver(message)
        self.db.record_alert(key, message, delivered)
        return True

    def _deliver(self, message: str) -> bool:
        if not self.settings.telegram_enabled:
            print(f"\n[提醒]\n{message}\n")
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={"chat_id": self.settings.telegram_chat_id, "text": message},
                timeout=15,
            )
            if resp.ok:
                return True
            log.warning("Telegram 发送失败：HTTP %s %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            # 不打印异常全文，避免 URL 里的 token 出现在日志
            log.warning("Telegram 发送失败：%s", exc.__class__.__name__)
        return False
