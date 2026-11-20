"""数据源统一接口。以后换数据源（OANDA 等）只需新增一个实现这个接口的文件。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

OHLC_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Tick:
    time: datetime  # UTC
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class DataSource(ABC):
    """只读行情。这里不允许出现任何下单功能。"""

    name: str = "base"

    @abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:  # noqa: B027 - 可选实现
        pass

    @abstractmethod
    def get_tick(self) -> Tick: ...

    @abstractmethod
    def get_rates(self, timeframe: str, count: int = 500) -> pd.DataFrame:
        """返回按时间升序的 K 线：索引为 UTC 时间，列为 open/high/low/close/volume。
        最后一根可能是还没收完的当前 K 线。"""
