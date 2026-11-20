import numpy as np
import pandas as pd
import pytest

from app.indicators import add_indicators, atr, classify_trend, ema, rma, rsi


def _series(values):
    return pd.Series(values, dtype=float)


def test_ema_seeded_with_sma():
    s = _series([1, 2, 3, 4, 5, 6])
    out = ema(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)


def test_rma_seeded_with_sma():
    s = _series([2, 4, 6, 8])
    out = rma(s, 2)
    assert out.iloc[1] == pytest.approx(3.0)
    assert out.iloc[2] == pytest.approx(0.5 * 6 + 0.5 * 3.0)


def test_rsi_extremes():
    up = _series(range(1, 40))
    down = _series(range(40, 1, -1))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)


def test_rsi_known_value():
    # 涨跌交替、幅度相同 → RSI 接近 50
    s = _series(np.tile([10.0, 11.0], 50))
    assert 45 < rsi(s).iloc[-1] < 55


def test_atr_constant_range():
    n = 30
    df = pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.0] * n})
    assert atr(df).iloc[-1] == pytest.approx(2.0)


def _trend_df(slope: float, n: int = 300) -> pd.DataFrame:
    close = 2000 + slope * np.arange(n)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0})


def test_classify_trend():
    assert classify_trend(add_indicators(_trend_df(1.0))) == "多"
    assert classify_trend(add_indicators(_trend_df(-1.0))) == "空"
    flat = add_indicators(_trend_df(0.0))
    assert classify_trend(flat) == "震荡"
