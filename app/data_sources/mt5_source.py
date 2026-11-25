"""从 MetaTrader 5 读取 XAU/USD 行情（只读，不含任何下单代码）。

- 需要 Windows + 已登录的 MT5 软件（建议模拟账户）。
- MT5 返回的时间是「经纪商服务器时间」，这里统一换算成 UTC。
- 断线时自动重连。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from app.data_sources.base import OHLC_COLUMNS, DataSource, Tick

try:
    import MetaTrader5 as mt5
except ImportError:  # 非 Windows 或没装包
    mt5 = None

log = logging.getLogger(__name__)

_TIMEFRAME_ATTR = {
    "M1": "TIMEFRAME_M1",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class MT5Source(DataSource):
    name = "mt5"

    def __init__(
        self,
        symbol: str,
        path: str | None = None,
        utc_offset_hours: float | None = None,
        fallback_offset_hours: float = 2.0,
        max_retries: int = 5,
        retry_delay_sec: float = 3.0,
    ) -> None:
        self.symbol = symbol
        self.path = path
        self.max_retries = max_retries
        self.retry_delay_sec = retry_delay_sec
        self._fixed_offset = utc_offset_hours is not None
        self._fallback_hours = fallback_offset_hours
        initial = utc_offset_hours if utc_offset_hours is not None else fallback_offset_hours
        self._offset_sec = int(initial * 3600)
        self._offset_known = self._fixed_offset
        self._connected = False

    # ---------- 连接 ----------
    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError(
                "没有 MetaTrader5 Python 包（只支持 Windows）。请运行：pip install MetaTrader5"
            )
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            ok = mt5.initialize(path=self.path) if self.path else mt5.initialize()
            if ok:
                if not mt5.symbol_select(self.symbol, True):
                    err = mt5.last_error()
                    mt5.shutdown()
                    raise RuntimeError(
                        f"MT5 里找不到品种 {self.symbol!r}（{err}）。"
                        "请在 MT5「市场报价」里查看金价的确切名称，改 .env 的 MT5_SYMBOL。"
                    )
                self._connected = True
                info = mt5.account_info()
                if info is not None and info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
                    log.warning("当前登录的不是模拟账户。本工具只读行情，但建议改用模拟账户。")
                log.info("已连接 MT5，品种 %s", self.symbol)
                self._update_offset(mt5.symbol_info_tick(self.symbol))
                return
            last_error = mt5.last_error()
            log.warning("连接 MT5 失败（第 %d/%d 次）：%s", attempt, self.max_retries, last_error)
            time.sleep(self.retry_delay_sec)
        raise ConnectionError(
            f"连不上 MT5：{last_error}。请确认 MT5 软件已打开并登录。"
        )

    def close(self) -> None:
        if mt5 is not None and self._connected:
            mt5.shutdown()
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected or mt5.terminal_info() is None:
            self._connected = False
            log.info("MT5 未连接，正在重连…")
            self.connect()

    def _call(self, fn):
        """执行 MT5 调用；结果为空时重连一次再试。"""
        self._ensure_connected()
        result = fn()
        if result is None or (hasattr(result, "__len__") and len(result) == 0):
            log.warning("MT5 返回空结果（%s），尝试重连", mt5.last_error())
            self._connected = False
            self._ensure_connected()
            result = fn()
            if result is None or (hasattr(result, "__len__") and len(result) == 0):
                raise ConnectionError(f"MT5 取数据失败：{mt5.last_error()}")
        return result

    # ---------- 时区 ----------
    def _update_offset(self, tick) -> None:
        """用最新报价时间推算服务器时区（只在报价是「刚刚」的时候才可靠，周末休市时不更新）。

        时区一旦判断出来，只有报价时间「有在往前走」（市场真的在报价）时才允许改。
        否则每日休市那两小时里，停住的旧报价刚好差整数小时，会被误判成时区变了，
        让程序以为报价是新的、在休市时照发提醒。
        """
        if self._fixed_offset or tick is None:
            return
        last_seen, self._last_tick_server_time = getattr(self, "_last_tick_server_time", None), tick.time
        if self._offset_known and (last_seen is None or tick.time <= last_seen):
            return
        diff = tick.time - time.time()
        hours = round(diff / 3600)
        if abs(hours) <= 14 and abs(diff - hours * 3600) < 120:
            new_offset = hours * 3600
            if new_offset != self._offset_sec or not self._offset_known:
                log.info("经纪商服务器时区：UTC%+d", hours)
            self._offset_sec = new_offset
            self._offset_known = True
        elif not self._offset_known:
            log.warning(
                "暂时判断不出服务器时区（可能休市），先当作 UTC%+g。"
                "不对的话可在 .env 设置 MT5_SERVER_UTC_OFFSET_HOURS。", self._fallback_hours
            )

    def _to_utc(self, server_epoch: float) -> datetime:
        return datetime.fromtimestamp(server_epoch - self._offset_sec, tz=timezone.utc)

    # ---------- 行情 ----------
    def get_tick(self) -> Tick:
        tick = self._call(lambda: mt5.symbol_info_tick(self.symbol))
        self._update_offset(tick)
        return Tick(time=self._to_utc(tick.time), bid=float(tick.bid), ask=float(tick.ask))

    def get_rates(self, timeframe: str, count: int = 500) -> pd.DataFrame:
        if timeframe not in _TIMEFRAME_ATTR:
            raise ValueError(f"不支持的周期：{timeframe}")
        tf = getattr(mt5, _TIMEFRAME_ATTR[timeframe])
        rates = self._call(lambda: mt5.copy_rates_from_pos(self.symbol, tf, 0, count))
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"] - self._offset_sec, unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"}).set_index("time")
        return df[OHLC_COLUMNS].astype(float)
