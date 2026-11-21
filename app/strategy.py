"""关键位共振 + K 线确认 策略（规则说明见 STRATEGY.md）。

参考的公开方法：
- Alexander Elder「三重滤网」：大周期定方向（D1/H4），小周期找进场（H1）
- 关键位 / 供需区：前高前低、昨日高低点、整数关口、重要均线重叠处（共振）
- 价格行为确认（Al Brooks 等）：到了关键位还要收出 Pin bar 或吞没 K 线
- 风险管理（海龟交易法 ATR 止损、Van Tharp R 倍数）：止损放在区外 + ATR 缓冲，目标 2R，
  前方空间不够 1.5R 不做
- 只在伦敦、纽约时段出信号

同一套代码同时用于实时分析（runner）和历史回测（backtest），保证回测测的就是你在用的规则。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from app.levels import Zone, build_zones, collect_candidates


@dataclass(frozen=True)
class StrategyParams:
    # 默认值按「样本内（2018–2022）挑选、样本外（2023–2026）检验」选出，见 STRATEGY.md
    min_zone_score: int = 3  # 关键位强度 ≥ 3（多个来源共振 / 多次测试）
    max_plan_distance_atr: float = 4.0  # 计划只看现价附近 4 个 ATR 内的区
    stop_buffer_atr: float = 0.3
    max_risk_atr: float = 2.5  # 止损超过 2.5 ATR 太宽，不做
    reward_r: float = 3.0
    min_room_r: float = 1.5  # 前方最近的强关键位至少要有 1.5R 空间
    trend_rsi_cap: float = 70.0  # 顺势做多 RSI < 70；做空 RSI > 30
    range_rsi_long: float = 40.0  # 震荡市只在 RSI <= 40 时于支撑做多
    range_rsi_short: float = 60.0
    require_candle: bool = True
    session_hours: tuple[int, int] | None = (7, 20)  # UTC，K 线收盘时间
    merge_atr: float = 0.35
    trade_ranges: bool = True  # 震荡市是否也做（高抛低吸）
    strict_trend: bool = True  # H4 和 D1 必须同方向才算有方向


@dataclass
class Context:
    time: pd.Timestamp  # 最后一根已收盘 H1 的收盘时间（UTC）
    price: float
    bar: dict  # 最后一根已收盘 H1：open/high/low/close
    prev_bar: dict
    rsi: float
    atr: float
    trend_h1: str
    trend_h4: str
    trend_d1: str
    zones: list[Zone]


@dataclass
class Signal:
    direction: str
    timeframe: str
    entry: float
    entry_low: float
    entry_high: float
    stop: float
    target: float
    rr: float
    zone: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanItem:
    direction: str
    zone_low: float
    zone_high: float
    zone_price: float
    score: int
    stop: float
    target: float
    rr: float
    distance_atr: float
    trigger: str
    invalidation: str
    basis: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StrategyResult:
    bias: str
    signal: Signal | None
    plan: list[PlanItem]
    notes: list[str]


# ---------------- K 线形态 ----------------
def _parts(bar: dict):
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    rng = h - l
    body = abs(c - o)
    return o, h, l, c, rng, body, min(o, c) - l, h - max(o, c)


def bullish_rejection(bar: dict, prev: dict) -> str | None:
    o, h, l, c, rng, body, lower, upper = _parts(bar)
    if rng <= 0:
        return None
    if lower >= 2 * body and lower >= 0.5 * rng and c >= l + 0.6 * rng:
        return "看涨 Pin bar（长下影线）"
    if c > o and prev["close"] < prev["open"] and c >= prev["open"] and o <= prev["close"]:
        return "看涨吞没"
    return None


def bearish_rejection(bar: dict, prev: dict) -> str | None:
    o, h, l, c, rng, body, lower, upper = _parts(bar)
    if rng <= 0:
        return None
    if upper >= 2 * body and upper >= 0.5 * rng and c <= h - 0.6 * rng:
        return "看跌 Pin bar（长上影线）"
    if c < o and prev["close"] > prev["open"] and c <= prev["open"] and o >= prev["close"]:
        return "看跌吞没"
    return None


# ---------------- 组装上下文 ----------------
def make_context(
    time: pd.Timestamp,
    price: float,
    bar: dict,
    prev_bar: dict,
    rsi: float,
    atr: float,
    trends: dict[str, str],
    swing_highs: list[float],
    swing_lows: list[float],
    prev_day_high: float | None,
    prev_day_low: float | None,
    extras: dict[str, float | None],
    round_step: float,
    params: StrategyParams,
) -> Context:
    candidates = collect_candidates(
        price, atr, swing_highs, swing_lows, round_step, prev_day_high, prev_day_low, extras
    )
    return Context(
        time=time, price=price, bar=bar, prev_bar=prev_bar, rsi=rsi, atr=atr,
        trend_h1=trends["H1"], trend_h4=trends["H4"], trend_d1=trends["D1"],
        zones=build_zones(candidates, atr, params.merge_atr),
    )


def market_bias(trend_h4: str, trend_d1: str, strict: bool = False) -> str:
    """第一层滤网：H4 定方向，D1 不能反对（strict=True 时 D1 必须同方向）。"""
    if strict:
        return trend_h4 if trend_h4 == trend_d1 and trend_h4 in ("多", "空") else "震荡"
    if trend_h4 == "多" and trend_d1 != "空":
        return "多"
    if trend_h4 == "空" and trend_d1 != "多":
        return "空"
    return "震荡"


def _directions(bias: str, params: StrategyParams) -> list[str]:
    if bias in ("多", "空"):
        return [bias]
    return ["多", "空"] if params.trade_ranges else []


def _obstacle(direction: str, entry: float, zones: list[Zone], atr: float, min_score: int) -> Zone | None:
    gap = 0.25 * atr
    if direction == "多":
        ahead = [z for z in zones if z.score >= min_score and z.low > entry + gap]
        return min(ahead, key=lambda z: z.price) if ahead else None
    ahead = [z for z in zones if z.score >= min_score and z.high < entry - gap]
    return max(ahead, key=lambda z: z.price) if ahead else None


def _trade_levels(direction, zone, entry, atr, zones, params, bar=None):
    """返回 (stop, target, 错误说明)。"""
    if direction == "多":
        base = min(zone.low, bar["low"]) if bar else zone.low
        stop = base - params.stop_buffer_atr * atr
        risk = entry - stop
    else:
        base = max(zone.high, bar["high"]) if bar else zone.high
        stop = base + params.stop_buffer_atr * atr
        risk = stop - entry
    if risk <= 0:
        return None, None, "价格已经越过止损位"
    if risk > params.max_risk_atr * atr:
        return None, None, f"止损要 {risk / atr:.1f} 个 ATR，太宽"
    sign = 1 if direction == "多" else -1
    target = entry + sign * params.reward_r * risk
    obs = _obstacle(direction, entry, zones, atr, params.min_zone_score)
    if obs is not None:
        room = abs(obs.price - entry)
        if room < params.min_room_r * risk:
            word = "阻力" if direction == "多" else "支撑"
            return None, None, f"前方{word} {obs.price:.2f} 只有 {room / risk:.1f}R 空间"
    return stop, target, None


def _rsi_ok(direction: str, bias: str, rsi: float, params: StrategyParams) -> tuple[bool, str]:
    if bias == direction:  # 顺势
        if direction == "多":
            return rsi < params.trend_rsi_cap, f"RSI {rsi:.1f} 已超买"
        return rsi > 100 - params.trend_rsi_cap, f"RSI {rsi:.1f} 已超卖"
    if direction == "多":  # 震荡市高抛低吸
        return rsi <= params.range_rsi_long, f"震荡市做多需 RSI ≤ {params.range_rsi_long:.0f}（现 {rsi:.1f}）"
    return rsi >= params.range_rsi_short, f"震荡市做空需 RSI ≥ {params.range_rsi_short:.0f}（现 {rsi:.1f}）"


def _trend_text(ctx: Context, bias: str) -> str:
    if bias in ("多", "空"):
        return f"顺势：H4「{ctx.trend_h4}」、D1「{ctx.trend_d1}」（三重滤网第一层）"
    return f"震荡市高抛低吸：H4「{ctx.trend_h4}」、D1「{ctx.trend_d1}」没有一致方向"


# ---------------- 主函数 ----------------
def evaluate(ctx: Context, params: StrategyParams = StrategyParams()) -> StrategyResult:
    bias = market_bias(ctx.trend_h4, ctx.trend_d1, params.strict_trend)
    notes: list[str] = []
    if not ctx.atr or ctx.rsi is None or pd.isna(ctx.atr) or pd.isna(ctx.rsi):
        return StrategyResult(bias, None, [], ["H1 指标数据不足"])
    directions = _directions(bias, params)
    plan = _build_plan(ctx, bias, directions, params, notes)
    signal = _find_signal(ctx, bias, directions, params, notes)
    return StrategyResult(bias, signal, plan, notes)


def _build_plan(ctx, bias, directions, params, notes) -> list[PlanItem]:
    plan = []
    atr = ctx.atr
    for d in directions:
        if d == "多":
            side = [z for z in ctx.zones if z.price <= ctx.price + 0.25 * atr]
        else:
            side = [z for z in ctx.zones if z.price >= ctx.price - 0.25 * atr]
        side = [
            z for z in side
            if z.score >= params.min_zone_score
            and abs(ctx.price - z.price) <= params.max_plan_distance_atr * atr
        ]
        side.sort(key=lambda z: (-z.score, abs(ctx.price - z.price)))
        chosen = None
        last_err = None
        for z in side:
            stop, target, err = _trade_levels(d, z, z.price, atr, ctx.zones, params)
            if err is None:
                chosen = (z, stop, target)
                break
            last_err = err
        if chosen is None:
            word = "做多" if d == "多" else "做空"
            notes.append(f"附近没有合格的{word}区" + (f"（{last_err}）" if last_err else ""))
            continue
        z, stop, target = chosen
        candle = "看涨 Pin bar 或看涨吞没" if d == "多" else "看跌 Pin bar 或看跌吞没"
        trigger = f"价格回到 {z.low:.2f}–{z.high:.2f}，H1 收出{candle}"
        if bias not in ("多", "空"):
            trigger += (f"，且 RSI ≤ {params.range_rsi_long:.0f}" if d == "多"
                        else f"，且 RSI ≥ {params.range_rsi_short:.0f}")
        if params.session_hours:
            trigger += f"（伦敦/纽约时段，UTC {params.session_hours[0]}:00–{params.session_hours[1]}:00）"
        basis = [
            f"关键位共振：{'、'.join(z.sources)}（强度 {z.score}）",
            _trend_text(ctx, bias),
        ]
        if z.touches >= 2:
            basis.append(f"这个价位被前高前低测试过 {z.touches} 次")
        basis.append(f"止损在区外 {params.stop_buffer_atr} ATR，目标 {params.reward_r:.0f}R，前方空间 ≥ {params.min_room_r}R")
        plan.append(PlanItem(
            direction=d, zone_low=round(z.low, 2), zone_high=round(z.high, 2), zone_price=round(z.price, 2),
            score=z.score, stop=round(stop, 2), target=round(target, 2), rr=params.reward_r,
            distance_atr=round(abs(ctx.price - z.price) / atr, 1), trigger=trigger,
            invalidation=f"H1 收盘{'跌破' if d == '多' else '升破'} {stop:.2f}，或 H4 趋势反转",
            basis=basis,
        ))
    return plan


def _find_signal(ctx, bias, directions, params, notes) -> Signal | None:
    if params.session_hours:
        start, end = params.session_hours
        if not start <= ctx.time.hour <= end:
            notes.append("不在伦敦/纽约时段，不出信号")
            return None
    bar, atr = ctx.bar, ctx.atr
    notes_before = len(notes)
    for d in directions:
        if d == "多":
            touched = [z for z in ctx.zones if z.score >= params.min_zone_score
                       and bar["low"] <= z.high and bar["close"] >= z.low and z.price <= bar["close"] + 0.25 * atr]
        else:
            touched = [z for z in ctx.zones if z.score >= params.min_zone_score
                       and bar["high"] >= z.low and bar["close"] <= z.high and z.price >= bar["close"] - 0.25 * atr]
        if not touched:
            continue
        zone = max(touched, key=lambda z: z.score)
        ok, why = _rsi_ok(d, bias, ctx.rsi, params)
        if not ok:
            notes.append(why)
            continue
        pattern = None
        if params.require_candle:
            pattern = bullish_rejection(bar, ctx.prev_bar) if d == "多" else bearish_rejection(bar, ctx.prev_bar)
            if pattern is None:
                notes.append(f"价格在{'支撑' if d == '多' else '阻力'}区 {zone.price:.2f}，等确认 K 线")
                continue
        entry = ctx.price
        stop, target, err = _trade_levels(d, zone, entry, atr, ctx.zones, params, bar)
        if err:
            notes.append(err)
            continue
        reasons = [
            _trend_text(ctx, bias),
            f"回到关键位 {zone.price:.2f}：{'、'.join(zone.sources)}（强度 {zone.score}）",
        ]
        if pattern:
            reasons.append(f"确认 K 线：{pattern}")
        reasons.append(f"RSI(H1) {ctx.rsi:.1f}，ATR {atr:.2f}")
        zone_pad = 0.15 * atr
        return Signal(
            direction=d, timeframe="H1", entry=round(entry, 2),
            entry_low=round(entry - zone_pad, 2), entry_high=round(entry + zone_pad, 2),
            stop=round(stop, 2), target=round(target, 2), rr=params.reward_r,
            zone=round(zone.price, 2), reasons=reasons,
        )
    if len(notes) == notes_before:
        notes.append("最新一根 H1 没有碰到强关键位，等价格回到计划区")
    return None
