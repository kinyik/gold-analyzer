"""信号规则。

⚠ 这里先放一套「示范规则」，让整条流程能跑起来。等你写好 STRATEGY.md，
请让 Claude Code 按你的规则改写 evaluate()，参数集中在 StrategyParams。

示范规则（H4 定方向，H1 找入场）：
1. H4 趋势是「多」或「空」才考虑；H1 趋势不能和 H4 相反。
2. 回调入场：H1 价格离 EMA20 / EMA50 / 最近支撑(做多)或阻力(做空) 不超过 0.5 个 H1 ATR。
3. RSI(H1) 不能太极端：做多 40–65，做空 35–60。
4. 止损 = 入场价 ∓ 1.5 个 H1 ATR；目标 = 2 倍风险（2R）。
5. 如果反方向最近的阻力/支撑比 1.5R 还近，目标被挡住，不出信号。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class StrategyParams:
    pullback_atr: float = 0.5
    long_rsi: tuple[float, float] = (40, 65)
    short_rsi: tuple[float, float] = (35, 60)
    stop_atr: float = 1.5
    reward_r: float = 2.0
    min_room_r: float = 1.5
    entry_zone_atr: float = 0.25


@dataclass
class Signal:
    direction: str  # "多" 或 "空"
    timeframe: str
    entry: float
    entry_low: float
    entry_high: float
    stop: float
    target: float
    rr: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StrategyResult:
    signal: Signal | None
    notes: list[str]  # 没出信号时，说明卡在哪一条


def evaluate(snapshot: dict, params: StrategyParams = StrategyParams()) -> StrategyResult:
    """snapshot 由 runner 组好：
    {"price": float, "trends": {"H4": "多", ...},
     "indicators": {"H1": {...latest_values...}},
     "levels": {"H1": {"supports": [{"price":..}], "resistances": [...]}}}"""
    price = snapshot["price"]
    trends = snapshot["trends"]
    h1 = snapshot["indicators"]["H1"]
    levels = snapshot["levels"]["H1"]
    atr = h1.get("atr14")
    rsi = h1.get("rsi14")
    notes: list[str] = []

    if not atr or rsi is None:
        return StrategyResult(None, ["H1 指标数据不足"])

    h4, h1_trend = trends.get("H4"), trends.get("H1")
    if h4 not in ("多", "空"):
        return StrategyResult(None, [f"H4 是「{h4}」，没有明确方向"])
    direction = h4
    opposite = "空" if direction == "多" else "多"
    if h1_trend == opposite:
        return StrategyResult(None, [f"H1 是「{h1_trend}」，和 H4 方向相反"])

    supports = [lv["price"] for lv in levels.get("supports", [])]
    resistances = [lv["price"] for lv in levels.get("resistances", [])]
    anchors = {"EMA20": h1.get("ema20"), "EMA50": h1.get("ema50")}
    if direction == "多" and supports:
        anchors["最近支撑"] = supports[0]
    if direction == "空" and resistances:
        anchors["最近阻力"] = resistances[0]
    near = [
        (name, val) for name, val in anchors.items()
        if val is not None and abs(price - val) <= params.pullback_atr * atr
    ]
    if not near:
        notes.append(f"价格离 EMA20/EMA50/关键位都超过 {params.pullback_atr} 个 ATR，等回调")
    lo, hi = params.long_rsi if direction == "多" else params.short_rsi
    if not lo <= rsi <= hi:
        notes.append(f"RSI {rsi:.1f} 不在 {lo:.0f}–{hi:.0f} 之间")
    if notes:
        return StrategyResult(None, notes)

    sign = 1 if direction == "多" else -1
    risk = params.stop_atr * atr
    stop = price - sign * risk
    target = price + sign * params.reward_r * risk
    blockers = resistances if direction == "多" else supports
    if blockers:
        room = abs(blockers[0] - price)
        if room < params.min_room_r * risk:
            return StrategyResult(
                None,
                [f"前方 {blockers[0]:.2f} 只有 {room / risk:.1f}R 空间，不到 {params.min_room_r}R"],
            )

    zone = params.entry_zone_atr * atr
    reasons = [
        f"H4 趋势「{h4}」，H1「{h1_trend}」",
        "回调到 " + "、".join(f"{n} {v:.2f}" for n, v in near),
        f"RSI(H1) {rsi:.1f}",
        f"止损 {params.stop_atr} ATR（ATR {atr:.2f}），目标 {params.reward_r}R",
    ]
    signal = Signal(
        direction=direction,
        timeframe="H1",
        entry=round(price, 2),
        entry_low=round(price - zone, 2),
        entry_high=round(price + zone, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        rr=params.reward_r,
        reasons=reasons,
    )
    return StrategyResult(signal, [])
