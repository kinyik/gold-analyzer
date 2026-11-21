"""主程序：定时拿行情 → 算指标 → 关键位 → 策略（交易计划 + 信号）→ AI 解读 → 存档 → 提醒。

用法：
    python -m app.runner          一直运行（每 5 秒报价、每 15 分钟分析）
    python -m app.runner --once   只分析一次就结束（测试用）
"""
from __future__ import annotations

import argparse
import logging
import threading
from datetime import datetime, timezone

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler

from app import ai_analyst, messages, strategy
from app.alerts import Alerter
from app.config import TIMEFRAMES, Settings, get_settings
from app.data_sources import DataSource, build_source
from app.db import Database
from app.indicators import add_indicators, classify_trend, latest_values
from app.levels import split_zones, swing_points

log = logging.getLogger("runner")
PARAMS = strategy.StrategyParams()


class _LockedSource:
    """报价任务和分析任务在不同线程跑，同一时间只让一个去读 MT5。"""

    def __init__(self, source: DataSource) -> None:
        self._source = source
        self._lock = threading.Lock()
        self.name = source.name

    def get_tick(self):
        with self._lock:
            return self._source.get_tick()

    def get_rates(self, timeframe: str, count: int = 500):
        with self._lock:
            return self._source.get_rates(timeframe, count)


def _bar(row: pd.Series) -> dict:
    return {k: float(row[k]) for k in ("open", "high", "low", "close")}


def build_live_context(data: dict[str, pd.DataFrame], price: float, round_step: float) -> strategy.Context:
    """data：各周期「已收盘」并算好指标的 K 线。"""
    h1, h4, d1 = data["H1"], data["H4"], data["D1"]
    last, prev = h1.iloc[-1], h1.iloc[-2]
    highs, lows = swing_points(h1.tail(300), window=3)
    return strategy.make_context(
        time=h1.index[-1] + pd.Timedelta(hours=1),
        price=price,
        bar=_bar(last),
        prev_bar=_bar(prev),
        rsi=float(last["rsi14"]),
        atr=float(last["atr14"]),
        trends={tf: classify_trend(data[tf]) for tf in ("H1", "H4", "D1")},
        swing_highs=highs,
        swing_lows=lows,
        prev_day_high=float(d1["high"].iloc[-1]),
        prev_day_low=float(d1["low"].iloc[-1]),
        extras={"H4 EMA50": float(h4["ema50"].iloc[-1]), "H1 EMA200": float(last["ema200"])},
        round_step=round_step,
        params=PARAMS,
    )


class Runner:
    def __init__(self, settings: Settings, source: DataSource, db: Database) -> None:
        self.settings = settings
        self.source = _LockedSource(source)
        self.db = db
        self.alerter = Alerter(settings, db)
        self.prev_trends: dict[str, str] = {}
        self.plan: list[dict] = []
        self.h1_atr: float | None = None
        self.market_open: bool | None = None
        self.last_facts: dict | None = None

    def _check_market(self, tick_time: datetime) -> bool:
        """报价太久没更新 = 休市。状态改变时记一笔日志。"""
        age_min = (datetime.now(timezone.utc) - tick_time).total_seconds() / 60
        is_open = age_min <= self.settings.market_stale_min
        if is_open != self.market_open:
            if is_open:
                log.info("市场开盘中，提醒已启用")
            else:
                log.info("最新报价已是 %.0f 分钟前（休市），暂停所有提醒", age_min)
        self.market_open = is_open
        return is_open

    # ---------- 每几秒：报价 + 计划区提醒 ----------
    def tick_job(self) -> None:
        try:
            tick = self.source.get_tick()
        except Exception as exc:  # noqa: BLE001 - 保持程序不中断
            log.warning("取报价失败：%s", exc)
            return
        self.db.save_tick(tick.time, tick.bid, tick.ask)
        if not self._check_market(tick.time) or not self.h1_atr:
            return
        pad = 0.2 * self.h1_atr
        for item in self.plan:
            if item["zone_low"] - pad <= tick.mid <= item["zone_high"] + pad and self.last_facts:
                facts = {**self.last_facts, "price": round(tick.mid, 2), "bid": tick.bid, "ask": tick.ask}
                self.alerter.send(
                    f"plan:{item['direction']}:{item['zone_price']:.0f}",
                    messages.plan_zone_message(facts, item, tick.mid),
                )

    # ---------- 每 15 分钟：完整分析 ----------
    def analysis_job(self) -> dict | None:
        try:
            return self._analyze()
        except Exception:  # noqa: BLE001
            log.exception("分析失败，下次再试")
            return None

    def _analyze(self) -> dict:
        s = self.settings
        tick = self.source.get_tick()
        price = round(tick.mid, 2)
        self.db.save_tick(tick.time, tick.bid, tick.ask)

        closed: dict[str, pd.DataFrame] = {}
        day_open = None
        for tf in TIMEFRAMES:
            raw = self.source.get_rates(tf, s.bars_per_timeframe)
            self.db.save_candles(tf, raw, keep=s.bars_per_timeframe)
            closed[tf] = add_indicators(raw).iloc[:-1]  # 最后一根还没收完，不用
            if tf == "D1":
                day_open = float(raw["open"].iloc[-1])
        trends = {tf: classify_trend(closed[tf]) for tf in TIMEFRAMES}
        indicators = {tf: latest_values(closed[tf]) for tf in TIMEFRAMES}

        ctx = build_live_context(closed, price, s.round_level_step)
        result = strategy.evaluate(ctx, PARAMS)
        zones = split_zones(ctx.zones, price, min_score=1)

        facts = {
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "symbol": s.mt5_symbol,
            "price": price,
            "bid": tick.bid,
            "ask": tick.ask,
            "day_open": round(day_open, 2) if day_open else None,
            "trends": trends,
            "bias": result.bias,
            "indicators": indicators,
            "zones": {k: [z.to_dict() for z in v] for k, v in zones.items()},
            "plan": [p.to_dict() for p in result.plan],
            "signal": result.signal.to_dict() if result.signal else None,
            "strategy_notes": result.notes,
        }

        ai_text, ai_source = ai_analyst.analyze(facts, s)
        self.db.save_analysis(price, facts, ai_text, ai_source)
        log.info("分析完成：%s 价格 %.2f 方向 %s 计划 %d 个 信号 %s（%s）", s.mt5_symbol, price, result.bias,
                 len(result.plan), result.signal.direction if result.signal else "无", ai_source)

        self.plan = facts["plan"]
        self.h1_atr = ctx.atr
        self.last_facts = facts
        if self._check_market(tick.time):
            self._alerts_after_analysis(facts, result)
        self.prev_trends = trends
        return facts

    def _alerts_after_analysis(self, facts: dict, result: strategy.StrategyResult) -> None:
        price = facts["price"]
        sig = result.signal
        if sig and not self.db.has_similar_open_signal(sig.direction, sig.entry, (self.h1_atr or 1) * 0.5):
            self.db.save_signal(sig.to_dict())
            self.alerter.send(f"signal:{sig.direction}:{round(sig.entry)}", messages.signal_message(facts))
        for tf in ("H1", "H4", "D1"):
            old, new = self.prev_trends.get(tf), facts["trends"][tf]
            if old and old != new:
                self.alerter.send(f"trend:{tf}:{new}", messages.trend_message(facts, tf, old, new))
        rsi = facts["indicators"]["H1"]["rsi14"]
        if rsi is not None and (rsi >= 70 or rsi <= 30):
            state = "超买" if rsi >= 70 else "超卖"
            self.alerter.send(f"rsi:H1:{state}", messages.rsi_message(facts, state))


def main() -> None:
    parser = argparse.ArgumentParser(description="XAU/USD 自动分析")
    parser.add_argument("--once", action="store_true", help="只分析一次")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    settings = get_settings()
    source = build_source(settings)
    source.connect()
    db = Database(settings.db_path)
    runner = Runner(settings, source, db)

    facts = runner.analysis_job()
    if args.once:
        if facts:
            print(db.latest_analysis()["ai_text"])
        source.close()
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(runner.tick_job, "interval", seconds=settings.tick_interval_sec,
                      max_instances=1, coalesce=True)
    scheduler.add_job(runner.analysis_job, "cron", minute=f"*/{settings.analysis_interval_min}",
                      second=5, max_instances=1, coalesce=True)
    log.info("开始运行：每 %d 秒更新报价，每 %d 分钟分析一次（Ctrl+C 结束）",
             settings.tick_interval_sec, settings.analysis_interval_min)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        source.close()
        db.close()


if __name__ == "__main__":
    main()
