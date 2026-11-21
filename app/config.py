"""读取 .env 配置。所有密钥只放在 .env，绝不写进代码或上传 GitHub。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TIMEFRAMES = ("M15", "H1", "H4", "D1")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = _str(name)
    return float(value) if value else default


def _optional_float(name: str) -> float | None:
    value = _str(name).lower()
    if value in ("", "auto"):
        return None
    return float(value)


@dataclass(frozen=True)
class Settings:
    # 数据源：mt5（真实行情）或 demo（随机演示数据，没装 MT5 时测试用）
    data_source: str = field(default_factory=lambda: _str("DATA_SOURCE", "mt5").lower())
    mt5_symbol: str = field(default_factory=lambda: _str("MT5_SYMBOL", "XAUUSD"))
    mt5_path: str = field(default_factory=lambda: _str("MT5_PATH"))
    # 经纪商服务器比 UTC 快几小时；留空或 auto 表示用实时报价自动判断
    mt5_utc_offset_hours: float | None = field(
        default_factory=lambda: _optional_float("MT5_SERVER_UTC_OFFSET_HOURS")
    )
    # 自动判断失败（例如周末休市）时用这个；大多数 MT5 经纪商是 UTC+2（夏令时 +3）
    mt5_utc_offset_fallback: float = field(
        default_factory=lambda: _float("MT5_SERVER_UTC_OFFSET_FALLBACK", 2)
    )

    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID"))

    ollama_url: str = field(default_factory=lambda: _str("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _str("OLLAMA_MODEL", "qwen2.5:7b"))
    ollama_timeout_sec: int = field(default_factory=lambda: _int("OLLAMA_TIMEOUT_SEC", 180))

    db_path: Path = field(
        default_factory=lambda: (ROOT / _str("DB_PATH", "data/gold.db")).resolve()
    )
    bars_per_timeframe: int = field(default_factory=lambda: _int("BARS_PER_TIMEFRAME", 1000))
    analysis_interval_min: int = field(default_factory=lambda: _int("ANALYSIS_INTERVAL_MIN", 15))
    tick_interval_sec: int = field(default_factory=lambda: _int("TICK_INTERVAL_SEC", 5))
    round_level_step: float = field(default_factory=lambda: _float("ROUND_LEVEL_STEP", 50))
    alert_cooldown_min: int = field(default_factory=lambda: _int("ALERT_COOLDOWN_MIN", 60))
    max_alerts_per_day: int = field(default_factory=lambda: _int("MAX_ALERTS_PER_DAY", 20))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def get_settings() -> Settings:
    return Settings()
