from __future__ import annotations

from app.config import Settings
from app.data_sources.base import DataSource, Tick


def build_source(settings: Settings) -> DataSource:
    if settings.data_source == "demo":
        from app.data_sources.demo_source import DemoSource

        return DemoSource()
    if settings.data_source == "mt5":
        from app.data_sources.mt5_source import MT5Source

        return MT5Source(
            symbol=settings.mt5_symbol,
            path=settings.mt5_path or None,
            utc_offset_hours=settings.mt5_utc_offset_hours,
        )
    raise ValueError(f"未知的 DATA_SOURCE：{settings.data_source}（可选 mt5 或 demo）")


__all__ = ["DataSource", "Tick", "build_source"]
