"""Telegram 提醒的文字：事件 + 行情 + 关键位 + 策略按规则给出的买卖倾向 + 回测参考。

买卖倾向全部来自 strategy.py 的固定规则（facts 里的 bias / signal / plan），
这里只负责把它写成看得懂的话，不另外做判断。
"""
from __future__ import annotations

# 默认参数的回测成绩（STRATEGY.md）；改了参数或重跑回测后要同步更新
BACKTEST_NOTE = (
    "这套规则 2018–2026 回测：420 笔，胜率 29.5%，平均每笔 +0.09R；"
    "样本外（2023–2026）平均 +0.10R。优势很小、会连亏，统计上未显著。"
)
DISCLAIMER = "⚠ 按固定规则计算，仅供参考，不构成投资建议。先用模拟账户验证。"

DIR_WORD = {"多": "做多（买）", "空": "做空（卖）"}


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:,.2f}"


def market_block(facts: dict) -> str:
    price = facts["price"]
    lines = [f"💰 现价 {_fmt(price)}（bid {_fmt(facts.get('bid'))} / ask {_fmt(facts.get('ask'))}）"]
    day_open = facts.get("day_open")
    if day_open:
        chg = price - day_open
        lines.append(f"📅 今日 {chg:+,.2f}（{chg / day_open:+.2%}）")
    trends = facts.get("trends", {})
    ind = facts.get("indicators", {})
    parts = []
    for tf in ("M15", "H1", "H4", "D1"):
        if tf in trends:
            rsi = ind.get(tf, {}).get("rsi14")
            parts.append(f"{tf} {trends[tf]}" + (f"(RSI {rsi:.0f})" if rsi is not None else ""))
    lines.append("📊 趋势：" + "　".join(parts))
    atr = ind.get("H1", {}).get("atr14")
    if atr:
        lines.append(f"📏 H1 平均波幅（ATR）约 {atr:.1f} 美元")
    return "\n".join(lines)


def zones_block(facts: dict) -> str:
    zones = facts.get("zones", {})

    def line(z: dict) -> str:
        return f"{_fmt(z['price'])}（{'+'.join(z['sources'])}，强度 {z['score']}）"

    res = zones.get("resistances", [])[:2]
    sup = zones.get("supports", [])[:2]
    out = ["📍 关键位"]
    out.append("　上方阻力：" + ("；".join(line(z) for z in res) if res else "暂无"))
    out.append("　下方支撑：" + ("；".join(line(z) for z in sup) if sup else "暂无"))
    return "\n".join(out)


def _bias_text(bias: str) -> str:
    if bias == "多":
        return "H4 和 D1 都向上 → 只考虑买，不做空"
    if bias == "空":
        return "H4 和 D1 都向下 → 只考虑卖，不做多"
    return "H4 和 D1 方向不一致 → 震荡市，只在强关键位高抛低吸"


def advice_block(facts: dict) -> str:
    """策略按规则的买卖倾向。"""
    bias = facts.get("bias", "震荡")
    lines = [f"🧭 策略倾向：{'偏买' if bias == '多' else '偏卖' if bias == '空' else '震荡'}（{_bias_text(bias)}）"]
    sig = facts.get("signal")
    if sig:
        risk = abs(sig["entry"] - sig["stop"])
        lines += [
            f"✅ 现在有信号：{DIR_WORD[sig['direction']]}",
            f"　进场参考 {_fmt(sig['entry_low'])}–{_fmt(sig['entry_high'])}",
            f"　止损 {_fmt(sig['stop'])}（风险约 {risk:.1f} 美元）",
            f"　目标 {_fmt(sig['target'])}（{sig['rr']:.0f} 倍风险）",
            "　根据：" + "；".join(sig["reasons"]),
        ]
        return "\n".join(lines)

    plan = facts.get("plan") or []
    if plan:
        lines.append("⏳ 现在还没有进场信号，按规则等：")
        for p in plan:
            lines += [
                f"　▸ {DIR_WORD[p['direction']]}：价格到 {_fmt(p['zone_low'])}–{_fmt(p['zone_high'])}"
                f"（离现价约 {p['distance_atr']} 个 ATR）",
                f"　　触发：{p['trigger']}",
                f"　　止损 {_fmt(p['stop'])}　目标 {_fmt(p['target'])}",
                "　　根据：" + "；".join(p["basis"][:3]),
                f"　　作废：{p['invalidation']}",
            ]
    else:
        lines.append("⏸ 建议观望：附近没有足够强的关键位（强度 ≥ 3），不追单。")
        if bias == "空":
            lines.append("　趋势向下但价格不在阻力位，现在卖属于追空；等反弹到强阻力再看。")
        elif bias == "多":
            lines.append("　趋势向上但价格不在支撑位，现在买属于追高；等回调到强支撑再看。")
    return "\n".join(lines)


def rsi_comment(facts: dict, state: str) -> str:
    bias = facts.get("bias", "震荡")
    if state == "超卖":
        if bias == "空":
            return ("超卖 = 短时间跌得太急，可能出现技术反弹，但大趋势仍向下。"
                    "规则：不抄底买；也不在这里追空，等反弹到阻力区再找卖点。")
        if bias == "多":
            return "上升趋势里的超卖，常是回调买点。规则：关注下方强支撑，收出看涨确认 K 线再买。"
        return "震荡市超卖：规则允许在强支撑区、RSI ≤ 40 且有看涨确认 K 线时买。"
    if bias == "多":
        return ("超买 = 短时间涨得太急，可能回调，但大趋势仍向上。"
                "规则：不摸顶卖；也不在这里追高，等回调到支撑区再找买点。")
    if bias == "空":
        return "下跌趋势里的超买，常是反弹卖点。规则：关注上方强阻力，收出看跌确认 K 线再卖。"
    return "震荡市超买：规则允许在强阻力区、RSI ≥ 60 且有看跌确认 K 线时卖。"


def compose(header: str, facts: dict, comment: str | None = None) -> str:
    parts = [header]
    if comment:
        parts.append(f"💡 {comment}")
    parts += [market_block(facts), zones_block(facts), advice_block(facts), f"📈 {BACKTEST_NOTE}", DISCLAIMER]
    return "\n\n".join(parts)


def signal_message(facts: dict) -> str:
    sig = facts["signal"]
    return compose(f"🔔 XAU/USD 新信号：{DIR_WORD[sig['direction']]}", facts)


def rsi_message(facts: dict, state: str) -> str:
    rsi = facts["indicators"]["H1"]["rsi14"]
    return compose(f"⚠ XAU/USD H1 RSI {rsi:.1f}，{state}", facts, rsi_comment(facts, state))


def trend_message(facts: dict, tf: str, old: str, new: str) -> str:
    meaning = {
        "H1": "短线方向改变，留意是回调还是转势。",
        "H4": "中线方向改变，策略的买卖方向可能随之改变。",
        "D1": "大方向改变，是比较重要的变化。",
    }[tf]
    return compose(f"↪ XAU/USD {tf} 趋势转向：{old} → {new}", facts, meaning)


def plan_zone_message(facts: dict, item: dict, price: float) -> str:
    word = DIR_WORD[item["direction"]]
    candle = "看涨" if item["direction"] == "多" else "看跌"
    comment = (f"价格 {_fmt(price)} 已进入{word}计划区 {_fmt(item['zone_low'])}–{_fmt(item['zone_high'])}。"
               f"现在还不是进场信号：要等 H1 收出{candle} Pin bar 或吞没 K 线，程序会再发「新信号」。")
    return compose(f"📍 XAU/USD 进入{word}计划区", facts, comment)
