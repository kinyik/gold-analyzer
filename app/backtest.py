"""历史回测：用和实时分析完全相同的策略代码，逐根 H1 K 线重放历史。

    python -m app.backtest                 回测 MT5 上能拿到的全部 H1 历史
    python -m app.backtest --bars 20000    只用最近 20000 根

规则：
- 信号在 H1 收盘时产生，下一根 K 线开盘价进场
- 同一根 K 线同时碰到止损和目标，一律当作止损（保守）
- 每笔扣 spread（点差 + 滑点）成本；最多持有 72 根 H1，到期按收盘价离场
- 同一时间只持有一笔
- H4 由 H1 合成（对齐经纪商服务器时间），D1 用 MT5 原生日线；只用「已收盘」的大周期 K 线，没有未来数据
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from app.config import ROOT, get_settings
from app.indicators import add_indicators, trend_series
from app.levels import swing_flags
from app.strategy import StrategyParams, evaluate, make_context

SWING_WINDOW = 3
SWING_LOOKBACK = 300


@dataclass
class Trade:
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    exit_time: pd.Timestamp | None = None
    exit: float | None = None
    outcome: str = ""
    r: float = 0.0


def resample_h4(h1: pd.DataFrame, offset_hours: int = 2) -> pd.DataFrame:
    """按经纪商服务器时间（UTC+offset）切 4 小时。"""
    return h1.resample("4h", offset=f"{offset_hours % 4}h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def run_backtest(
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    d1: pd.DataFrame,
    params: StrategyParams = StrategyParams(),
    round_step: float = 50.0,
    cost: float = 0.30,
    warmup: int = 300,
    max_hold: int = 72,
) -> list[Trade]:
    h1 = add_indicators(h1)
    h4 = add_indicators(h4)
    d1 = add_indicators(d1)
    h1_trend = trend_series(h1).to_numpy()
    h4_trend = trend_series(h4).to_numpy()
    d1_trend = trend_series(d1).to_numpy()

    times = h1.index
    close_times = times + pd.Timedelta(hours=1)
    k4 = np.searchsorted((h4.index + pd.Timedelta(hours=4)).asi8, close_times.asi8, side="right") - 1
    kd = np.searchsorted((d1.index + pd.Timedelta(days=1)).asi8, close_times.asi8, side="right") - 1

    o, h, l, c = (h1[x].to_numpy() for x in ("open", "high", "low", "close"))
    rsi, atr, ema200 = h1["rsi14"].to_numpy(), h1["atr14"].to_numpy(), h1["ema200"].to_numpy()
    h4_ema50 = h4["ema50"].to_numpy()
    d1_high, d1_low = d1["high"].to_numpy(), d1["low"].to_numpy()
    is_high, is_low = swing_flags(h, l, SWING_WINDOW)
    high_idx, low_idx = np.flatnonzero(is_high), np.flatnonzero(is_low)

    trades: list[Trade] = []
    open_trade: Trade | None = None
    entry_idx = -1
    n = len(h1)
    for i in range(warmup, n):
        # ---- 管理持仓 ----
        if open_trade is not None and i >= entry_idx:
            t = open_trade
            long = t.direction == "多"
            exit_price, outcome = None, ""
            if long:
                if l[i] <= t.stop:
                    exit_price, outcome = min(o[i], t.stop) if i > entry_idx else t.stop, "止损"
                elif h[i] >= t.target:
                    exit_price, outcome = t.target, "目标"
            else:
                if h[i] >= t.stop:
                    exit_price, outcome = max(o[i], t.stop) if i > entry_idx else t.stop, "止损"
                elif l[i] <= t.target:
                    exit_price, outcome = t.target, "目标"
            if exit_price is None and i - entry_idx >= max_hold:
                exit_price, outcome = c[i], "到期"
            if exit_price is not None:
                risk = abs(t.entry - t.stop)
                gross = (exit_price - t.entry) if long else (t.entry - exit_price)
                t.exit, t.exit_time, t.outcome = float(exit_price), times[i], outcome
                t.r = (gross - cost) / risk
                trades.append(t)
                open_trade = None
        if open_trade is not None or i >= n - 1:
            continue
        if k4[i] < 0 or kd[i] < 0 or np.isnan(atr[i]) or np.isnan(rsi[i]):
            continue

        # ---- 产生信号（只用到第 i 根为止已知的数据）----
        lo = np.searchsorted(high_idx, i - SWING_LOOKBACK)
        hi = np.searchsorted(high_idx, i - SWING_WINDOW, side="right")
        swing_highs = h[high_idx[lo:hi]].tolist()
        lo = np.searchsorted(low_idx, i - SWING_LOOKBACK)
        hi = np.searchsorted(low_idx, i - SWING_WINDOW, side="right")
        swing_lows = l[low_idx[lo:hi]].tolist()
        ctx = make_context(
            time=close_times[i], price=float(c[i]),
            bar={"open": o[i], "high": h[i], "low": l[i], "close": c[i]},
            prev_bar={"open": o[i - 1], "high": h[i - 1], "low": l[i - 1], "close": c[i - 1]},
            rsi=float(rsi[i]), atr=float(atr[i]),
            trends={"H1": h1_trend[i], "H4": h4_trend[k4[i]], "D1": d1_trend[kd[i]]},
            swing_highs=swing_highs, swing_lows=swing_lows,
            prev_day_high=float(d1_high[kd[i]]), prev_day_low=float(d1_low[kd[i]]),
            extras={"H4 EMA50": h4_ema50[k4[i]], "H1 EMA200": ema200[i]},
            round_step=round_step, params=params,
        )
        sig = evaluate(ctx, params).signal
        if sig is None:
            continue
        entry = float(o[i + 1])
        long = sig.direction == "多"
        if (long and entry <= sig.stop) or (not long and entry >= sig.stop):
            continue
        open_trade = Trade(sig.direction, close_times[i], times[i + 1], entry, sig.stop, sig.target)
        entry_idx = i + 1
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"笔数": 0}
    r = np.array([t.r for t in trades])
    equity = np.cumsum(r)
    drawdown = np.max(np.maximum.accumulate(np.concatenate([[0], equity]))[1:] - equity)
    losses = r[r <= 0]
    streak = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    return {
        "笔数": len(r),
        "胜率": f"{(r > 0).mean():.1%}",
        "平均每笔R": round(r.mean(), 3),
        "总R": round(r.sum(), 1),
        "盈亏比(PF)": round(r[r > 0].sum() / -losses.sum(), 2) if losses.sum() < 0 else float("inf"),
        "最大回撤R": round(float(drawdown), 1),
        "最长连亏": streak,
    }


def yearly(trades: list[Trade]) -> pd.DataFrame:
    df = pd.DataFrame([{"year": t.entry_time.year, "r": t.r} for t in trades])
    if df.empty:
        return df
    g = df.groupby("year")["r"]
    return pd.DataFrame({"笔数": g.size(), "胜率": g.apply(lambda s: f"{(s > 0).mean():.0%}"),
                         "总R": g.sum().round(1)})


_DEFAULT = StrategyParams()
VARIANTS = {
    "完整策略": _DEFAULT,
    "去掉 K 线确认": replace(_DEFAULT, require_candle=False),
    "去掉时段过滤": replace(_DEFAULT, session_hours=None),
    "关键位强度≥1（不要求共振）": replace(_DEFAULT, min_zone_score=1),
    "只做顺势（不做震荡市）": replace(_DEFAULT, trade_ranges=False),
    "D1 不必同向": replace(_DEFAULT, strict_trend=False),
}


def walk_forward(h1, h4, d1, round_step: float, cost: float, split: str = "2023-01-01") -> None:
    """样本内（split 之前）挑参数，样本外（split 之后）检验。挑选规则事先定好：
    样本内至少 100 笔、平均每笔 R 最高的那组。"""
    split_ts = pd.Timestamp(split, tz="UTC")
    grid = []
    for strict in (False, True):
        for reward in (1.5, 2.0, 3.0):
            for score in (2, 3):
                grid.append(StrategyParams(strict_trend=strict, reward_r=reward, min_zone_score=score))
    rows = []
    for p in grid:
        trades = run_backtest(h1, h4, d1, p, round_step, cost)
        ins = [t for t in trades if t.entry_time < split_ts]
        oos = [t for t in trades if t.entry_time >= split_ts]
        a, b = summarize(ins), summarize(oos)
        rows.append({
            "D1必须同向": p.strict_trend, "目标R": p.reward_r, "关键位强度≥": p.min_zone_score,
            "样本内笔数": a.get("笔数", 0), "样本内平均R": a.get("平均每笔R", float("nan")),
            "样本内PF": a.get("盈亏比(PF)"), "样本外笔数": b.get("笔数", 0),
            "样本外平均R": b.get("平均每笔R", float("nan")), "样本外PF": b.get("盈亏比(PF)"),
            "样本外总R": b.get("总R"), "样本外胜率": b.get("胜率"),
        })
    table = pd.DataFrame(rows)
    print(f"\n=== 样本内 < {split} ≤ 样本外 ===")
    print(table.to_string(index=False))
    eligible = table[table["样本内笔数"] >= 100]
    if eligible.empty:
        print("没有样本内 ≥ 100 笔的组合")
        return
    best = eligible.sort_values("样本内平均R", ascending=False).iloc[0]
    print("\n按样本内挑出的参数：")
    print(best.to_string())


def main() -> None:
    parser = argparse.ArgumentParser(description="策略历史回测")
    parser.add_argument("--bars", type=int, default=50000, help="H1 K 线数量")
    parser.add_argument("--cost", type=float, default=0.30, help="每笔点差+滑点成本（美元）")
    parser.add_argument("--walkforward", action="store_true", help="样本内挑参数、样本外检验")
    args = parser.parse_args()

    from app.data_sources import build_source

    settings = get_settings()
    source = build_source(settings)
    source.connect()
    print("下载历史数据中（第一次可能要几分钟）…")
    h1 = source.get_rates("H1", args.bars)
    d1 = source.get_rates("D1", args.bars // 20 + 400)
    source.close()
    h4 = resample_h4(h1)
    print(f"H1 {len(h1)} 根：{h1.index[0]:%Y-%m-%d} → {h1.index[-1]:%Y-%m-%d}，每笔成本 {args.cost}")
    if args.walkforward:
        walk_forward(h1, h4, d1, settings.round_level_step, args.cost)
        return

    rows = {}
    main_trades = None
    for name, params in VARIANTS.items():
        trades = run_backtest(h1, h4, d1, params, settings.round_level_step, args.cost)
        rows[name] = summarize(trades)
        if main_trades is None:
            main_trades = trades
    pd.set_option("display.width", 200)
    print("\n=== 各版本对比 ===")
    print(pd.DataFrame(rows).T.to_string())
    print("\n=== 完整策略：分年度 ===")
    print(yearly(main_trades).to_string())
    longs = [t for t in main_trades if t.direction == "多"]
    shorts = [t for t in main_trades if t.direction == "空"]
    print("\n做多：", summarize(longs))
    print("做空：", summarize(shorts))

    out = ROOT / "data" / "backtest_trades.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame([t.__dict__ for t in main_trades]).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n每笔交易明细：{out}")


if __name__ == "__main__":
    main()
