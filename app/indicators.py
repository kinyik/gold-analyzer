"""技术指标和趋势判断。全部由代码计算，AI 只负责解读这些数字。

EMA 和 Wilder 平滑（RSI、ATR 用）都以前 n 根的简单平均作为起点，
算法与 TradingView 的 ta.ema / ta.rma 一致，方便人工对照。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EMA_PERIODS = (20, 50, 200)


def _seeded_smooth(values: pd.Series, n: int, alpha: float) -> pd.Series:
    """前 n 个有效值取平均当起点，之后 out = alpha*x + (1-alpha)*prev。"""
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if len(valid) < n:
        return pd.Series(out, index=values.index)
    start = valid[0]
    seed_end = start + n - 1
    out[seed_end] = np.nanmean(arr[start : seed_end + 1])
    for i in range(seed_end + 1, len(arr)):
        x = arr[i]
        out[i] = out[i - 1] if np.isnan(x) else alpha * x + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index)


def ema(series: pd.Series, n: int) -> pd.Series:
    return _seeded_smooth(series, n, 2 / (n + 1))


def rma(series: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑。"""
    return _seeded_smooth(series, n, 1 / n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = rma(delta.clip(lower=0), n)
    loss = rma(-delta.clip(upper=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & gain.notna()] = 100.0
    out[(gain == 0) & (loss == 0)] = 50.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return rma(tr, n)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for n in EMA_PERIODS:
        out[f"ema{n}"] = ema(out["close"], n)
    out["rsi14"] = rsi(out["close"], 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["atr14"] = atr(out, 14)
    return out


def classify_trend(df: pd.DataFrame) -> str:
    """看最后一根 K 线：
    - 多：收盘 > EMA50，且 EMA20 > EMA50 > EMA200（EMA200 算不出来时只看前两个条件）
    - 空：收盘 < EMA50，且 EMA20 < EMA50 < EMA200
    - 其他都算「震荡」
    """
    last = df.iloc[-1]
    close, e20, e50, e200 = last["close"], last["ema20"], last["ema50"], last.get("ema200")
    if pd.isna(e20) or pd.isna(e50):
        return "震荡"
    has_200 = e200 is not None and not pd.isna(e200)
    if close > e50 and e20 > e50 and (not has_200 or e50 > e200):
        return "多"
    if close < e50 and e20 < e50 and (not has_200 or e50 < e200):
        return "空"
    return "震荡"


def latest_values(df: pd.DataFrame) -> dict:
    """最后一根 K 线的指标数字（四舍五入），给 AI 和仪表板用。"""
    last = df.iloc[-1]
    keys = ["close", "ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_hist", "atr14"]
    return {k: (None if pd.isna(last[k]) else round(float(last[k]), 2)) for k in keys}
