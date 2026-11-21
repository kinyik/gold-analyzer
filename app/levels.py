"""关键位：把前高前低、昨日高低点、整数关口、重要均线合并成「区」，
重叠的来源越多，区越强（共振）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

SWING_LABELS = ("前高", "前低")


@dataclass
class Zone:
    price: float
    low: float
    high: float
    sources: list[str] = field(default_factory=list)
    touches: int = 0  # 前高前低在这里出现过几次

    @property
    def score(self) -> int:
        """强度 = 不同来源的数量；同一价位被测试 2 次以上再加 1。"""
        return len(set(self.sources)) + (1 if self.touches >= 2 else 0)

    def to_dict(self) -> dict:
        return {
            "price": round(self.price, 2),
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "sources": self.sources,
            "touches": self.touches,
            "score": self.score,
        }


def swing_points(df: pd.DataFrame, window: int = 3) -> tuple[list[float], list[float]]:
    """某根 K 线的高点是前后各 window 根里最高的，就算前高；低点同理。
    最后 window 根还没被确认，不算。"""
    highs, lows = swing_flags(df["high"].to_numpy(), df["low"].to_numpy(), window)
    return (
        [float(x) for x in df["high"].to_numpy()[highs]],
        [float(x) for x in df["low"].to_numpy()[lows]],
    )


def swing_flags(high: np.ndarray, low: np.ndarray, window: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """返回布尔数组：第 j 根是否前高 / 前低（要到第 j+window 根收盘后才确认）。"""
    n = len(high)
    is_high = np.zeros(n, dtype=bool)
    is_low = np.zeros(n, dtype=bool)
    for i in range(window, n - window):
        seg_h = high[i - window : i + window + 1]
        seg_l = low[i - window : i + window + 1]
        is_high[i] = np.argmax(seg_h) == window
        is_low[i] = np.argmin(seg_l) == window
    return is_high, is_low


def collect_candidates(
    price: float,
    atr: float,
    swing_highs: list[float],
    swing_lows: list[float],
    round_step: float = 50.0,
    prev_day_high: float | None = None,
    prev_day_low: float | None = None,
    extras: dict[str, float | None] | None = None,
    span_atr: float = 10.0,
) -> list[tuple[float, str]]:
    span = span_atr * atr
    out: list[tuple[float, str]] = []
    out += [(p, "前高") for p in swing_highs if abs(p - price) <= span]
    out += [(p, "前低") for p in swing_lows if abs(p - price) <= span]
    if round_step > 0:
        k = math.floor((price - span) / round_step)
        while k * round_step <= price + span:
            out.append((k * round_step, "整数关口"))
            k += 1
    if prev_day_high is not None:
        out.append((prev_day_high, "昨日高点"))
    if prev_day_low is not None:
        out.append((prev_day_low, "昨日低点"))
    for name, val in (extras or {}).items():
        if val is not None and not np.isnan(val) and abs(val - price) <= span:
            out.append((float(val), name))
    return out


def build_zones(candidates: list[tuple[float, str]], atr: float, merge_atr: float = 0.35) -> list[Zone]:
    """相距不到 merge_atr 个 ATR 的价位合并成一个区。"""
    if not candidates or atr <= 0:
        return []
    tol = merge_atr * atr
    pad = 0.1 * atr
    items = sorted(candidates, key=lambda x: x[0])
    groups: list[list[tuple[float, str]]] = [[items[0]]]
    for p, s in items[1:]:
        if p - groups[-1][0][0] <= tol:
            groups[-1].append((p, s))
        else:
            groups.append([(p, s)])
    zones = []
    for g in groups:
        prices = [p for p, _ in g]
        sources = list(dict.fromkeys(s for _, s in g))
        touches = sum(1 for _, s in g if s in SWING_LABELS)
        zones.append(Zone(float(np.mean(prices)), min(prices) - pad, max(prices) + pad, sources, touches))
    return zones


def split_zones(zones: list[Zone], price: float, min_score: int = 1, limit: int = 4) -> dict[str, list[Zone]]:
    """价格下方的区当支撑（近的在前），上方的当阻力。"""
    strong = [z for z in zones if z.score >= min_score]
    supports = sorted((z for z in strong if z.price < price), key=lambda z: -z.price)
    resistances = sorted((z for z in strong if z.price >= price), key=lambda z: z.price)
    return {"supports": supports[:limit], "resistances": resistances[:limit]}
