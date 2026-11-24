"""每日休市那段时间的两个坑：时区误判、把已收盘的 K 线丢掉。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from app.data_sources import mt5_source
from app.data_sources.mt5_source import MT5Source
from app.runner import closed_bars


def test_stale_tick_during_daily_break_does_not_change_offset(monkeypatch):
    src = MT5Source("XAUUSD")  # 不连 MT5，只测时区推算
    now = 1_795_000_000.0  # 某个整点前后
    monkeypatch.setattr(mt5_source.time, "time", lambda: now)
    last_quote = now + 2 * 3600 - 8  # 服务器 UTC+2，最后一笔报价是 8 秒前
    src._update_offset(SimpleNamespace(time=last_quote))
    assert src._offset_sec == 2 * 3600

    # 休市 58 分钟后：报价停在原地，差值刚好接近 1 小时，不能被当成 UTC+1
    monkeypatch.setattr(mt5_source.time, "time", lambda: now + 3480)
    src._update_offset(SimpleNamespace(time=last_quote))
    assert src._offset_sec == 2 * 3600
    tick_age_min = (now + 3480 - (last_quote - src._offset_sec)) / 60
    assert tick_age_min > 50  # 程序会正确判断成休市

    # 重新开盘、报价往前走了：照常更新
    monkeypatch.setattr(mt5_source.time, "time", lambda: now + 7200)
    src._update_offset(SimpleNamespace(time=now + 7200 + 2 * 3600 - 1))
    assert src._offset_sec == 2 * 3600


def _bars(last_start: str, n: int = 3, freq: str = "h") -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(last_start, tz="UTC"), periods=n, freq=freq)
    return pd.DataFrame({"close": range(n)}, index=idx)


def test_forming_bar_is_dropped():
    df = _bars("2026-11-23 20:00")
    out = closed_bars(df, "H1", datetime(2026, 11, 23, 20, 30, tzinfo=timezone.utc))
    assert out.index[-1] == pd.Timestamp("2026-11-23 19:00", tz="UTC")


def test_finished_bar_is_kept_while_market_closed():
    df = _bars("2026-11-23 20:00")  # 20:00 那根在 21:00 收盘，之后休市没有新 K 线
    out = closed_bars(df, "H1", datetime(2026, 11, 23, 21, 30, tzinfo=timezone.utc))
    assert out.index[-1] == pd.Timestamp("2026-11-23 20:00", tz="UTC")
