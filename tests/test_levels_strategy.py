from dataclasses import replace

import numpy as np
import pandas as pd

from app.backtest import resample_h4, run_backtest, summarize
from app.levels import Zone, build_zones, collect_candidates, swing_points
from app.strategy import (
    Context,
    StrategyParams,
    bearish_rejection,
    bullish_rejection,
    evaluate,
    market_bias,
)


# 规则测试用固定参数，不受默认值调整影响
P2 = StrategyParams(min_zone_score=2, reward_r=2.0, strict_trend=False)


def ev(ctx):
    return evaluate(ctx, P2)


def _zigzag() -> pd.DataFrame:
    one = list(np.linspace(1990, 2010, 11)) + list(np.linspace(2010, 1990, 11))[1:-1]
    close = np.array(one * 8)
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close})


def test_swing_points_found():
    highs, lows = swing_points(_zigzag(), window=3)
    assert highs and lows
    assert all(abs(h - 2010.5) < 1e-6 for h in highs)
    assert all(abs(lv - 1989.5) < 1e-6 for lv in lows)


def test_zones_merge_and_score():
    cands = collect_candidates(
        price=2003, atr=5.0, swing_highs=[2049.8, 2050.6], swing_lows=[1995.0],
        round_step=50, prev_day_high=2049.0, prev_day_low=None, extras={},
    )
    zones = build_zones(cands, atr=5.0)
    top = max(zones, key=lambda z: z.score)
    # 2050 附近：前高（2 次）+ 整数关口 + 昨日高点 → 3 个来源 + 测试 2 次 = 4
    assert abs(top.price - 2050) < 1
    assert set(top.sources) == {"前高", "整数关口", "昨日高点"}
    assert top.score == 4
    lone = [z for z in zones if abs(z.price - 1995) < 0.5][0]
    assert lone.score == 1


def test_candle_patterns():
    pin = {"open": 100.0, "high": 101.0, "low": 95.0, "close": 100.6}
    assert bullish_rejection(pin, {"open": 100, "high": 101, "low": 99, "close": 100})
    assert not bearish_rejection(pin, {"open": 100, "high": 101, "low": 99, "close": 100})
    engulf = {"open": 99.0, "high": 102.0, "low": 98.8, "close": 101.5}
    assert bullish_rejection(engulf, {"open": 101, "high": 101.2, "low": 98.9, "close": 99.2})
    bear_pin = {"open": 100.0, "high": 105.0, "low": 99.0, "close": 99.4}
    assert bearish_rejection(bear_pin, {"open": 100, "high": 101, "low": 99, "close": 100})


def test_market_bias():
    assert market_bias("多", "多") == "多"
    assert market_bias("多", "震荡") == "多"
    assert market_bias("多", "空") == "震荡"
    assert market_bias("空", "震荡") == "空"
    assert market_bias("多", "震荡", strict=True) == "震荡"
    assert market_bias("空", "空", strict=True) == "空"


def test_default_params_need_score_3():
    # 默认参数：强度 2 的区不再出信号，强度 3 的区才会
    weak = [Zone(2000.0, 1999.0, 2001.0, ["前低", "整数关口"], 1)]
    assert evaluate(_ctx(PIN_AT_SUPPORT, zones=weak)).signal is None
    strong = evaluate(_ctx(PIN_AT_SUPPORT))
    assert strong.signal is not None
    s = strong.signal
    assert abs((s.target - s.entry) / (s.entry - s.stop) - 3.0) < 0.01


def _ctx(bar, rsi=50.0, h4="多", d1="多", zones=None, hour=12, price=None):
    zones = zones if zones is not None else [
        Zone(2000.0, 1999.0, 2001.0, ["前低", "整数关口"], 2),  # 强度 3
        Zone(2040.0, 2039.0, 2041.0, ["前高", "昨日高点"], 1),  # 强度 2
    ]
    return Context(
        time=pd.Timestamp(f"2026-01-05 {hour:02d}:00", tz="UTC"),
        price=price if price is not None else bar["close"],
        bar=bar, prev_bar={"open": 2003, "high": 2004, "low": 2001, "close": 2002},
        rsi=rsi, atr=4.0, trend_h1="多", trend_h4=h4, trend_d1=d1, zones=zones,
    )


PIN_AT_SUPPORT = {"open": 2002.0, "high": 2003.0, "low": 1999.5, "close": 2002.6}


def test_long_signal_at_support_with_pin_bar():
    res = ev(_ctx(PIN_AT_SUPPORT))
    s = res.signal
    assert res.bias == "多"
    assert s is not None and s.direction == "多"
    # 止损 = min(区下沿 1999, K 线低点 1999.5) - 0.3*4 = 1997.8
    assert s.stop == 1997.8
    assert s.target == round(2002.6 + 2 * (2002.6 - 1997.8), 2)
    assert any("Pin bar" in r for r in s.reasons)


def test_no_signal_without_confirmation_candle():
    plain = {"open": 2002.0, "high": 2003.0, "low": 1999.5, "close": 2000.5}
    res = ev(_ctx(plain))
    assert res.signal is None
    assert any("等确认 K 线" in n for n in res.notes)


def test_no_signal_outside_session():
    res = ev(_ctx(PIN_AT_SUPPORT, hour=3))
    assert res.signal is None


def test_no_signal_when_resistance_too_close():
    zones = [
        Zone(2000.0, 1999.0, 2001.0, ["前低", "整数关口"], 2),
        Zone(2008.0, 2007.0, 2009.0, ["前高", "昨日高点"], 1),
    ]
    assert ev(_ctx(PIN_AT_SUPPORT, zones=zones)).signal is None


def test_range_market_requires_rsi_extreme():
    assert ev(_ctx(PIN_AT_SUPPORT, h4="震荡", d1="震荡", rsi=50)).signal is None
    assert ev(_ctx(PIN_AT_SUPPORT, h4="震荡", d1="震荡", rsi=35)).signal is not None


def test_plan_lists_buy_zone_with_basis():
    res = ev(_ctx(PIN_AT_SUPPORT, price=2010.0))
    assert len(res.plan) == 1
    p = res.plan[0]
    assert p.direction == "多" and p.zone_price == 2000.0
    assert any("共振" in b for b in p.basis)
    assert p.stop < p.zone_low < p.target


def _trend_frames(n=2000, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = 2000 + np.cumsum(rng.normal(0.05, 2.0, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0, 2, n)
    low = np.minimum(open_, close) - rng.uniform(0, 2, n)
    h1 = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)
    d1 = h1.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return h1, resample_h4(h1), d1


def test_backtest_runs_and_is_consistent():
    h1, h4, d1 = _trend_frames()
    trades = run_backtest(h1, h4, d1, replace(P2, require_candle=False), cost=0.0)
    assert trades, "随机走势里应该会有交易"
    for t in trades:
        assert t.entry_time > t.signal_time - pd.Timedelta(hours=1)
        if t.outcome == "目标":
            assert abs(t.r - 2.0) < 0.01  # 止损/目标四舍五入到 0.01
        if t.outcome == "止损":
            assert t.r <= -1.0 + 1e-6
    stats = summarize(trades)
    assert stats["笔数"] == len(trades)
