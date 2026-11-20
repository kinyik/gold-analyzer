"""自动支撑阻力：前高前低（swing high/low）聚类 + 整数关口。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Level:
    price: float
    kind: str  # "support" 或 "resistance"
    source: str  # "swing"、"round" 或 "swing+round"
    touches: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def swing_points(df: pd.DataFrame, window: int = 3) -> tuple[list[float], list[float]]:
    """某根 K 线的高点是前后各 window 根里最高的，就算前高；低点同理。
    最后 window 根还没被确认，不算。"""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    swing_highs, swing_lows = [], []
    for i in range(window, len(df) - window):
        seg_h = highs[i - window : i + window + 1]
        seg_l = lows[i - window : i + window + 1]
        if highs[i] == seg_h.max() and np.argmax(seg_h) == window:
            swing_highs.append(float(highs[i]))
        if lows[i] == seg_l.min() and np.argmin(seg_l) == window:
            swing_lows.append(float(lows[i]))
    return swing_highs, swing_lows


def _cluster(prices: list[float], tolerance: float) -> list[tuple[float, int]]:
    """把彼此距离小于 tolerance 的价位合并成一个，返回 (平均价, 碰触次数)。"""
    if not prices:
        return []
    prices = sorted(prices)
    clusters: list[list[float]] = [[prices[0]]]
    for p in prices[1:]:
        if p - clusters[-1][-1] <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(float(np.mean(c)), len(c)) for c in clusters]


def find_levels(
    df: pd.DataFrame,
    price: float,
    atr_value: float,
    window: int = 3,
    lookback: int = 300,
    round_step: float = 50.0,
    max_each: int = 3,
) -> dict[str, list[Level]]:
    recent = df.tail(lookback)
    tolerance = max(atr_value * 0.5, 1e-9)
    highs, lows = swing_points(recent, window)
    clusters = _cluster(highs + lows, tolerance)

    levels: list[Level] = []
    for lvl, touches in clusters:
        kind = "support" if lvl < price else "resistance"
        levels.append(Level(round(lvl, 2), kind, "swing", touches))

    # 整数关口：价格上下各两个
    if round_step > 0:
        base = math.floor(price / round_step) * round_step
        for r in (base - round_step, base, base + round_step, base + 2 * round_step):
            if abs(r - price) < 1e-9:
                continue
            near = [lv for lv in levels if abs(lv.price - r) <= tolerance]
            if near:
                for lv in near:
                    lv.source = "swing+round"
            else:
                kind = "support" if r < price else "resistance"
                levels.append(Level(round(r, 2), kind, "round", 0))

    supports = sorted((lv for lv in levels if lv.kind == "support"), key=lambda lv: -lv.price)
    resistances = sorted((lv for lv in levels if lv.kind == "resistance"), key=lambda lv: lv.price)
    return {"supports": supports[:max_each], "resistances": resistances[:max_each]}
