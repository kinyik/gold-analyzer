"""主程序：定时拿行情 → 算指标 → 关键位 → 策略（交易计划 + 信号）→ AI 解读 → 存档 → 提醒。

用法：
    python -m app.runner          一直运行（每 5 秒报价、每 15 分钟分析）
    python -m app.runner --once   只分析一次就结束（测试用）
"""
from __future__ import annotations

import argparse
import os
import logging
import logging.handlers
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler

from app import ai_analyst, data_quality, messages, strategy
from app.alerts import Alerter
from app.config import TIMEFRAMES, Settings, get_settings
from app.data_sources import DataSource, build_source
from app.db import Database
from app.indicators import add_indicators, classify_trend, latest_values
from app.levels import split_zones, swing_points

log = logging.getLogger("runner")
PARAMS = strategy.StrategyParams()
TF_LENGTH = {"M15": pd.Timedelta(minutes=15), "H1": pd.Timedelta(hours=1),
             "H4": pd.Timedelta(hours=4), "D1": pd.Timedelta(days=1)}


def closed_bars(df: pd.DataFrame, tf: str, now: datetime) -> pd.DataFrame:
    """只留已收盘的 K 线。最后一根的时段还没结束才去掉；
    休市时最后一根其实已经收盘，不能一律丢掉，否则会拿一小时前的数据来分析。"""
    if len(df) and df.index[-1] + TF_LENGTH[tf] > pd.Timestamp(now):
        return df.iloc[:-1]
    return df


def write_heartbeat(settings: Settings) -> None:
    """看门狗（watchdog.ps1）靠这个文件判断程序是不是还活着、有没有卡住。"""
    try:
        (settings.db_path.parent / "heartbeat.txt").write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as exc:
        log.warning("写心跳文件失败：%s", exc)


def _setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    # 写到 data/runner.log，程序停了也查得到原因（最多 5 个 x 2MB）
    file = logging.handlers.RotatingFileHandler(
        settings.db_path.parent / "runner.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)


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
        self._reported_bad: set = set()

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
        write_heartbeat(self.settings)
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
            facts = self._analyze()
        except Exception:  # noqa: BLE001
            log.exception("分析失败，下次再试")
            return None
        try:
            self.report_job()
        except Exception:  # noqa: BLE001
            log.exception("定时研判发送失败")
        return facts

    # ---------- 每小时：主动推送行情研判 ----------
    def report_job(self, force: bool = False) -> bool:
        """距离上一次研判满 REPORT_INTERVAL_MIN 分钟、且正在开盘，就推送一条。"""
        interval = self.settings.report_interval_min
        if (interval <= 0 and not force) or not self.last_facts:
            return False
        if not self.market_open and not force:
            return False
        now = datetime.now(timezone.utc)
        row = self.db.conn.execute(
            "SELECT MAX(time) FROM alerts WHERE key LIKE 'report:%' AND delivered=1"
        ).fetchone()
        if row and row[0] and not force:
            last = datetime.fromisoformat(row[0])
            if (now - last).total_seconds() < interval * 60 - 90:  # 留 90 秒余量，配合每 15 分钟的分析节奏
                return False
        facts = self.last_facts
        try:
            tick = self.source.get_tick()
            if (now - tick.time).total_seconds() <= self.settings.market_stale_min * 60:
                facts = {**facts, "price": round(tick.mid, 2), "bid": tick.bid, "ask": tick.ask}
        except Exception as exc:  # noqa: BLE001
            log.warning("研判取最新报价失败，用分析时的价格：%s", exc)
        analysis = self.db.latest_analysis()
        text = messages.report_message(
            facts,
            analysis["ai_text"] if analysis else None,
            analysis["ai_source"] if analysis else None,
            now.strftime("%Y-%m-%d %H:%M"),
        )
        sent = self.alerter.send(f"report:{now:%Y%m%d%H%M}", text, scheduled=True)
        if sent:
            log.info("已推送定时行情研判")
        return sent

    def _analyze(self) -> dict:
        s = self.settings
        tick = self.source.get_tick()
        price = round(tick.mid, 2)
        self.db.save_tick(tick.time, tick.bid, tick.ask)

        closed: dict[str, pd.DataFrame] = {}
        day_open = None
        now = datetime.now(timezone.utc)
        m1_clean, bad = self._bad_prints()
        for tf in TIMEFRAMES:
            raw = data_quality.repair_bars(self.source.get_rates(tf, s.bars_per_timeframe),
                                           TF_LENGTH[tf], m1_clean, bad)
            self.db.save_candles(tf, raw, keep=s.bars_per_timeframe)
            closed[tf] = closed_bars(add_indicators(raw), tf, now)
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

    def _bad_prints(self) -> tuple[pd.DataFrame, list]:
        """用最近约一周的 1 分钟 K 线找经纪商异常报价（每日开盘那一分钟最常见）。"""
        try:
            m1 = self.source.get_rates("M1", 10_000)
        except Exception as exc:  # noqa: BLE001 - 拿不到 1 分钟数据就照原样分析
            log.debug("没有 1 分钟数据，跳过异常报价过滤：%s", exc)
            return pd.DataFrame(), []
        m1_clean, bad = data_quality.clean_minutes(m1)
        new = {b.time for b in bad} - self._reported_bad
        if new:
            for b in bad:
                if b.time in new:
                    log.info("忽略异常报价 %s %s=%.2f（改为 %.2f）", b.time, b.field, b.price, b.replaced_by)
            self._reported_bad |= new
        return m1_clean, bad

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
    parser.add_argument("--report-now", action="store_true", help="分析一次并立刻推送行情研判（不管上次什么时候发）")
    args = parser.parse_args()

    settings = get_settings()
    _setup_logging(settings)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    log.info("分析程序启动（PID %d）", os.getpid())
    source = build_source(settings)
    # 开机时 MT5 可能还没启动好：连不上就每 30 秒重试，直到连上（--once 只试一轮）
    while True:
        write_heartbeat(settings)
        try:
            source.connect()
            break
        except (ConnectionError, RuntimeError) as exc:
            if args.once or args.report_now:
                raise
            log.warning("暂时连不上数据源（%s），30 秒后重试", exc)
            time.sleep(30)
    db = Database(settings.db_path)
    runner = Runner(settings, source, db)

    write_heartbeat(settings)
    facts = runner.analysis_job()
    if args.report_now:
        print("研判已推送" if runner.report_job(force=True) else "研判没有发出（看上面的日志）")
        source.close()
        return
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
        log.info("收到结束指令，程序退出")
    except Exception:
        log.exception("程序意外退出")
        raise
    finally:
        source.close()
        db.close()


if __name__ == "__main__":
    main()
