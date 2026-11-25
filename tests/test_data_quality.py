"""异常报价过滤：用 2026-11-23 23:00 重新开盘的真实数据。"""
import pandas as pd

from app.data_quality import clean_minutes, repair_bars


def _m1(rows, start="2026-11-23 20:56"):
    idx = pd.date_range(start=pd.Timestamp(start, tz="UTC"), periods=len(rows), freq="min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


QUIET = [(3633.2, 3634.9, 3632.6, 3634.6), (3634.6, 3635.9, 3633.9, 3635.5), (3635.5, 3636.3, 3634.6, 3635.2)]


def _reopen():
    before = _m1(QUIET + [(3635.5, 3636.0, 3634.9, 3635.24)])
    after = _m1([
        (3594.63, 3641.45, 3590.17, 3641.18),  # 开盘那一分钟的异常价
        (3641.18, 3641.92, 3640.03, 3641.33),
        (3641.33, 3641.33, 3638.50, 3638.86),
        (3638.86, 3639.87, 3637.20, 3638.40),
    ], start="2026-11-23 23:00")
    return pd.concat([before, after])


def test_reopen_spike_is_removed():
    clean, bad = clean_minutes(_reopen())
    assert {b.field for b in bad} == {"open", "low"}
    row = clean.loc[pd.Timestamp("2026-11-23 23:00", tz="UTC")]
    assert row["low"] > 3630 and row["open"] > 3630
    assert row["close"] == 3641.18  # 收盘价不动


def test_real_gap_and_real_selloff_are_kept():
    gap = _m1(QUIET + [(3600.0, 3601.0, 3598.5, 3599.0), (3599.0, 3600.2, 3597.0, 3597.5),
                       (3597.5, 3598.0, 3595.0, 3596.0)])
    selloff = _m1(QUIET + [(3635.0, 3635.5, 3600.0, 3602.0), (3602.0, 3604.0, 3598.0, 3599.0),
                           (3599.0, 3601.0, 3596.0, 3597.0)])
    for df in (gap, selloff):
        clean, bad = clean_minutes(df)
        assert bad == []
        pd.testing.assert_frame_equal(clean, df)


def test_higher_timeframe_bar_is_rebuilt_from_clean_minutes():
    m1 = _reopen()
    clean, bad = clean_minutes(m1)
    h1 = pd.DataFrame({"open": [3594.63], "high": [3641.92], "low": [3590.17], "close": [3638.40]},
                      index=[pd.Timestamp("2026-11-23 23:00", tz="UTC")])
    fixed = repair_bars(h1, pd.Timedelta(hours=1), clean, bad)
    assert fixed["low"].iloc[0] == clean.loc["2026-11-23 23:00":]["low"].min() > 3630
    assert fixed["close"].iloc[0] == 3638.40
