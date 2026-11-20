import numpy as np
import pandas as pd

from app.levels import find_levels, swing_points
from app.strategy import evaluate


def _zigzag() -> pd.DataFrame:
    # 在 1990 和 2010 之间来回走
    one = list(np.linspace(1990, 2010, 11)) + list(np.linspace(2010, 1990, 11))[1:-1]
    close = np.array(one * 8)
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close})


def test_swing_points_found():
    highs, lows = swing_points(_zigzag(), window=3)
    assert highs and lows
    assert all(abs(h - 2010.5) < 1e-6 for h in highs)
    assert all(abs(lv - 1989.5) < 1e-6 for lv in lows)


def test_find_levels_clusters_and_rounds():
    lv = find_levels(_zigzag(), price=2000, atr_value=2.0, round_step=50)
    assert lv["supports"][0].price == 1989.5
    assert lv["supports"][0].touches > 1
    assert lv["resistances"][0].price == 2010.5
    assert any(x.source == "round" for x in lv["resistances"] + lv["supports"])


def _snapshot(price=2000.0, h4="多", h1="多", rsi=50.0, resist=2030.0):
    return {
        "price": price,
        "trends": {"H4": h4, "H1": h1},
        "indicators": {"H1": {"atr14": 5.0, "rsi14": rsi, "ema20": 2001.0, "ema50": 1995.0}},
        "levels": {"H1": {"supports": [{"price": 1990.0}], "resistances": [{"price": resist}]}},
    }


def test_long_signal():
    res = evaluate(_snapshot())
    assert res.signal is not None
    s = res.signal
    assert s.direction == "多"
    assert s.stop == 1992.5 and s.target == 2015.0


def test_no_signal_when_h4_ranging():
    assert evaluate(_snapshot(h4="震荡")).signal is None


def test_no_signal_when_timeframes_conflict():
    assert evaluate(_snapshot(h1="空")).signal is None


def test_no_signal_when_rsi_extreme():
    res = evaluate(_snapshot(rsi=72))
    assert res.signal is None and any("RSI" in n for n in res.notes)


def test_no_signal_when_resistance_too_close():
    assert evaluate(_snapshot(resist=2005.0)).signal is None
