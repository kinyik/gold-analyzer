"""过滤经纪商的异常报价（bad tick）。

例：2026-11-23 23:00 UTC 每日重新开盘的第一分钟，MT5 记了一笔 3,594.63，
前一分钟收 3,635、下一分钟开 3,641。这笔价格只存在一瞬间，但会被算进
M15 / H1 / H4 / D1 K 线的最低点，进而影响 ATR、前低和关键位。

做法：在 1 分钟 K 线上找「突然偏离、马上回来」的价格，改掉这一分钟，
再用干净的 1 分钟数据重算包含它的大周期 K 线。
判定很保守（偏离要同时超过 12 倍 1 分钟典型波幅和 0.5% 价格），
真正的急涨急跌会留下后续成交，不会被当成异常。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

RANGE_MULT = 12.0
MIN_PCT = 0.005


@dataclass
class BadPrint:
    time: pd.Timestamp
    field: str  # open / high / low
    price: float
    replaced_by: float


def clean_minutes(m1: pd.DataFrame) -> tuple[pd.DataFrame, list[BadPrint]]:
    """返回 (修正后的 1 分钟 K 线, 改了哪些价格)。"""
    if len(m1) < 3:
        return m1, []
    o, h, l, c = (m1[k].to_numpy(dtype=float).copy() for k in ("open", "high", "low", "close"))
    typical = float(np.median(h - l)) or 0.01
    found: list[BadPrint] = []
    n = len(m1)
    for i in range(1, n):
        refs = [c[i - 1], c[i]] + ([o[i + 1]] if i + 1 < n else [])
        lo_ref, hi_ref = min(refs), max(refs)
        thr = max(RANGE_MULT * typical, MIN_PCT * c[i])
        t = m1.index[i]
        if abs(o[i] - c[i - 1]) > thr and abs(o[i] - c[i]) > thr:
            new = float(np.clip(o[i], lo_ref, hi_ref))
            found.append(BadPrint(t, "open", o[i], new))
            o[i] = new
        body_lo, body_hi = min(o[i], c[i]), max(o[i], c[i])
        if l[i] < lo_ref - thr:
            found.append(BadPrint(t, "low", l[i], body_lo))
            l[i] = body_lo
        if h[i] > hi_ref + thr:
            found.append(BadPrint(t, "high", h[i], body_hi))
            h[i] = body_hi
        l[i], h[i] = min(l[i], body_lo), max(h[i], body_hi)
    out = m1.copy()
    out["open"], out["high"], out["low"], out["close"] = o, h, l, c
    return out, found


def repair_bars(bars: pd.DataFrame, length: pd.Timedelta, m1_clean: pd.DataFrame,
                bad: list[BadPrint]) -> pd.DataFrame:
    """用干净的 1 分钟数据重算含异常报价的大周期 K 线（只改 1 分钟数据完整覆盖的那几根）。"""
    if not bad or bars.empty or m1_clean.empty:
        return bars
    out = bars.copy()
    starts = out.index
    for t in sorted({b.time for b in bad}):
        k = starts.searchsorted(t, side="right") - 1
        if k < 0 or t >= starts[k] + length or starts[k] < m1_clean.index[0]:
            continue
        span = m1_clean.loc[starts[k]: starts[k] + length - pd.Timedelta(seconds=1)]
        if span.empty:
            continue
        out.iloc[k, out.columns.get_loc("open")] = float(span["open"].iloc[0])
        out.iloc[k, out.columns.get_loc("high")] = float(span["high"].max())
        out.iloc[k, out.columns.get_loc("low")] = float(span["low"].min())
    return out
