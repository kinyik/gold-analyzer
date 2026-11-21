from app import messages

BASE = {
    "price": 3525.9, "bid": 3525.78, "ask": 3526.01, "day_open": 3626.11,
    "trends": {"M15": "空", "H1": "空", "H4": "空", "D1": "空"},
    "indicators": {tf: {"rsi14": r, "atr14": 28.2} for tf, r in
                   (("M15", 38.4), ("H1", 28.3), ("H4", 18.5), ("D1", 28.3))},
    "zones": {
        "resistances": [{"price": 3590.44, "sources": ["前高"], "score": 2}],
        "supports": [{"price": 3500.0, "sources": ["整数关口", "昨日低点"], "score": 2}],
    },
    "bias": "空", "plan": [], "signal": None, "strategy_notes": [],
}

PLAN = {
    "direction": "空", "zone_low": 3582.92, "zone_high": 3597.97, "zone_price": 3590.44, "score": 3,
    "stop": 3606.42, "target": 3539.0, "rr": 3.0, "distance_atr": 2.3,
    "trigger": "价格回到 3582.92–3597.97，H1 收出看跌 Pin bar 或看跌吞没",
    "invalidation": "H1 收盘升破 3606.42，或 H4 趋势反转", "basis": ["关键位共振：前高、整数关口（强度 3）"],
}


def test_rsi_oversold_in_downtrend_says_wait():
    text = messages.rsi_message(BASE, "超卖")
    assert "不抄底" in text and "观望" in text
    assert "偏卖" in text
    assert "3,590.44" in text and "不构成投资建议" in text
    assert "-100.21" in text  # 今日涨跌


def test_plan_shows_sell_zone_with_stop_and_target():
    facts = {**BASE, "plan": [PLAN]}
    text = messages.plan_zone_message(facts, PLAN, 3585.0)
    assert "做空（卖）" in text
    assert "3,606.42" in text and "3,539.00" in text
    assert "还不是进场信号" in text


def test_signal_message():
    sig = {"direction": "空", "entry": 3590.0, "entry_low": 3585.8, "entry_high": 3594.2,
           "stop": 3606.42, "target": 3540.74, "rr": 3.0, "reasons": ["顺势", "看跌吞没"]}
    text = messages.signal_message({**BASE, "signal": sig})
    assert text.startswith("🔔")
    assert "风险约 16.4 美元" in text
    assert "看跌吞没" in text
