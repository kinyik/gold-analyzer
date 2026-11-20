"""演示数据源：随机生成像金价的走势。没有 MT5 时用来测试整条流程，数字不是真行情。"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.data_sources.base import OHLC_COLUMNS, DataSource, Tick

_FREQ = {"M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}


class DemoSource(DataSource):
    name = "demo"

    def __init__(self, start_price: float = 4000.0, seed: int = 7, history_days: int = 400):
        self._rng = np.random.default_rng(seed)
        self._start_price = start_price
        self._history_days = history_days
        self._m15: pd.DataFrame | None = None

    def connect(self) -> None:
        end = pd.Timestamp(datetime.now(timezone.utc)).floor("15min")
        index = pd.date_range(end=end, periods=self._history_days * 96, freq="15min", tz="UTC")
        n = len(index)
        # 带缓慢趋势变化的随机游走
        drift = np.sin(np.linspace(0, 6 * np.pi, n)) * 0.00008
        returns = drift + self._rng.normal(0, 0.0012, n)
        close = self._start_price * np.exp(np.cumsum(returns))
        open_ = np.concatenate([[close[0]], close[:-1]])
        wiggle = np.abs(self._rng.normal(0, 0.0008, n)) * close
        high = np.maximum(open_, close) + wiggle
        low = np.minimum(open_, close) - wiggle
        volume = self._rng.integers(200, 2000, n).astype(float)
        self._m15 = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )
        self._m15.index.name = "time"

    def _ensure(self) -> pd.DataFrame:
        if self._m15 is None:
            self.connect()
        return self._m15

    def get_tick(self) -> Tick:
        df = self._ensure()
        now = datetime.now(timezone.utc)
        bar_time = pd.Timestamp(now).floor("15min")
        last = df.iloc[-1]
        price = float(last["close"]) * float(np.exp(self._rng.normal(0, 0.0003)))
        if bar_time > df.index[-1]:
            df.loc[bar_time] = [last["close"], max(last["close"], price), min(last["close"], price), price, 0.0]
        else:
            df.loc[df.index[-1], "close"] = price
            df.loc[df.index[-1], "high"] = max(float(last["high"]), price)
            df.loc[df.index[-1], "low"] = min(float(last["low"]), price)
        spread = 0.25
        return Tick(time=now, bid=round(price - spread / 2, 2), ask=round(price + spread / 2, 2))

    def get_rates(self, timeframe: str, count: int = 500) -> pd.DataFrame:
        df = self._ensure()
        if timeframe == "M15":
            out = df
        else:
            out = df.resample(_FREQ[timeframe], label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna()
        return out[OHLC_COLUMNS].tail(count).copy()
