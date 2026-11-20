"""主程序：定时拿行情 → 算指标 → 策略 → AI 解读 → 存档 → 提醒。

用法：
    python -m app.runner          一直运行（每 5 秒报价、每 15 分钟分析）
    python -m app.runner --once   只分析一次就结束（测试用）
"""
from __future__ import annotations

import argparse
import logging
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from app import ai_analyst, strategy
from app.alerts import Alerter
from app.config import TIMEFRAMES, Settings, get_settings
from app.data_sources import DataSource, build_source
from app.db import Database
from app.indicators import add_indicators, classify_trend, latest_values
from app.levels import find_levels

log = logging.getLogger("runner")


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


class Runner:
    def __init__(self, settings: Settings, source: DataSource, db: Database) -> None:
        self.settings = settings
        self.source = _LockedSource(source)
        self.db = db
        self.alerter = Alerter(settings, db)
        self.prev_trends: dict[str, str] = {}
        self.key_levels: list[float] = []
        self.h1_atr: float | None = None

    # ---------- 每几秒：报价 + 关键位提醒 ----------
    def tick_job(self) -> None:
        try:
            tick = self.source.get_tick()
        except Exception as exc:  # noqa: BLE001 - 保持程序不中断
            log.warning("取报价失败：%s", exc)
            return
        self.db.save_tick(tick.time, tick.bid, tick.ask)
        if not self.h1_atr:
            return
        for level in self.key_levels:
            if abs(tick.mid - level) <= 0.15 * self.h1_atr:
                self.alerter.send(
                    f"level:{level:.2f}",
                    f"📍 XAU/USD {tick.mid:.2f} 碰到关键位 {level:.2f}",
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

        trends, indicators, levels = {}, {}, {}
        day_open = None
        for tf in TIMEFRAMES:
            raw = self.source.get_rates(tf, s.bars_per_timeframe)
            self.db.save_candles(tf, raw, keep=s.bars_per_timeframe)
            df = add_indicators(raw)
            trends[tf] = classify_trend(df)
            indicators[tf] = latest_values(df)
            if tf in ("H1", "H4"):
                lv = find_levels(df, price, indicators[tf]["atr14"] or 0, round_step=s.round_level_step)
                levels[tf] = {k: [x.to_dict() for x in v] for k, v in lv.items()}
            if tf == "D1":
                day_open = float(raw["open"].iloc[-1])

        facts = {
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "symbol": s.mt5_symbol,
            "price": price,
            "bid": tick.bid,
            "ask": tick.ask,
            "day_open": round(day_open, 2) if day_open else None,
            "trends": trends,
            "indicators": indicators,
            "levels": levels,
        }
        result = strategy.evaluate(facts)
        facts["signal"] = result.signal.to_dict() if result.signal else None
        facts["strategy_notes"] = result.notes

        ai_text, ai_source = ai_analyst.analyze(facts, s)
        self.db.save_analysis(price, facts, ai_text, ai_source)
        log.info("分析完成：%s 价格 %.2f 趋势 %s 信号 %s（%s）",
                 s.mt5_symbol, price, trends, result.signal.direction if result.signal else "无", ai_source)

        self._update_level_watch(levels, indicators)
        self._alerts_after_analysis(facts, result)
        self.prev_trends = trends
        return facts

    def _update_level_watch(self, levels: dict, indicators: dict) -> None:
        h1 = levels.get("H1", {})
        watch = [x["price"] for x in h1.get("supports", [])[:2] + h1.get("resistances", [])[:2]]
        self.key_levels = watch
        self.h1_atr = indicators["H1"]["atr14"]

    def _alerts_after_analysis(self, facts: dict, result: strategy.StrategyResult) -> None:
        price = facts["price"]
        sig = result.signal
        if sig and not self.db.has_similar_open_signal(sig.direction, sig.entry, (self.h1_atr or 1) * 0.5):
            self.db.save_signal(sig.to_dict())
            self.alerter.send(
                f"signal:{sig.direction}:{round(sig.entry)}",
                f"🔔 XAU/USD 新信号：{sig.direction}\n"
                f"入场区 {sig.entry_low:.2f}–{sig.entry_high:.2f}\n"
                f"止损 {sig.stop:.2f}　目标 {sig.target:.2f}（{sig.rr}R）\n"
                "理由：" + "；".join(sig.reasons),
            )
        for tf in ("H1", "H4", "D1"):
            old, new = self.prev_trends.get(tf), facts["trends"][tf]
            if old and old != new:
                self.alerter.send(
                    f"trend:{tf}:{new}", f"↪ XAU/USD {tf} 趋势转向：{old} → {new}（现价 {price:.2f}）"
                )
        rsi = facts["indicators"]["H1"]["rsi14"]
        if rsi is not None and (rsi >= 70 or rsi <= 30):
            state = "超买" if rsi >= 70 else "超卖"
            self.alerter.send(f"rsi:H1:{state}", f"⚠ XAU/USD H1 RSI {rsi:.1f}，{state}（现价 {price:.2f}）")


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
