"""用演示数据源跑一遍完整流程（不需要 MT5、Ollama、Telegram）。"""
from dataclasses import replace
from datetime import timedelta

from app import ai_analyst
from app.alerts import Alerter
from app.config import Settings
from app.data_sources.base import Tick
from app.data_sources.demo_source import DemoSource
from app.db import Database
from app.runner import Runner


def _settings(tmp_path) -> Settings:
    return replace(
        Settings(),
        data_source="demo",
        db_path=tmp_path / "test.db",
        telegram_bot_token="",
        telegram_chat_id="",
        ollama_url="http://127.0.0.1:9",  # 一定连不上 → 走文字模板
        ollama_timeout_sec=2,
        bars_per_timeframe=600,
    )


def test_full_analysis_with_demo(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    source = DemoSource(history_days=120)
    source.connect()
    facts = Runner(settings, source, db).analysis_job()

    assert facts is not None
    assert set(facts["trends"]) == {"M15", "H1", "H4", "D1"}
    analysis = db.latest_analysis()
    assert analysis["ai_source"] == "template"
    assert "【局面】" in analysis["ai_text"]
    assert not db.load_candles("H1").empty
    db.close()


class _ClosedMarketSource(DemoSource):
    """报价停在两天前，模拟周末休市。"""

    def get_tick(self):
        tick = super().get_tick()
        return Tick(time=tick.time - timedelta(days=2), bid=tick.bid, ask=tick.ask)


def test_no_alerts_when_market_closed(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    source = _ClosedMarketSource(history_days=120)
    source.connect()
    runner = Runner(settings, source, db)
    sent = []
    monkeypatch.setattr(runner.alerter, "send", lambda key, text: sent.append(key))
    runner.prev_trends = {"H1": "?", "H4": "?", "D1": "?"}  # 保证「趋势转向」会触发
    runner.plan = [{"direction": "多", "zone_low": 0, "zone_high": 1e9, "zone_price": 1.0,
                    "trigger": "", "stop": 0, "target": 0, "basis": []}]
    runner.h1_atr = 5.0

    assert runner.analysis_job() is not None  # 照样分析、存档
    runner.tick_job()
    assert runner.market_open is False
    assert sent == []
    assert db.latest_analysis() is not None
    db.close()


def test_alerts_sent_when_market_open(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    source = DemoSource(history_days=120)
    source.connect()
    runner = Runner(settings, source, db)
    sent = []
    monkeypatch.setattr(runner.alerter, "send", lambda key, text: sent.append(key))
    runner.prev_trends = {"H1": "?", "H4": "?", "D1": "?"}
    runner.analysis_job()
    assert runner.market_open is True
    assert any(k.startswith("trend:") for k in sent)
    db.close()


def test_invented_prices_detected():
    facts = {"price": 4012.35, "levels": {"x": [{"price": 3990.0}]}}
    assert ai_analyst.invented_prices("现价 4012.35，支撑 3990", facts) == []
    assert ai_analyst.invented_prices("目标 4100", facts) == [4100.0]


def test_trade_advice_detected():
    assert ai_analyst.trade_advice_words("建议建立空头仓位，止损设在 2755") == ["仓位", "止损"]
    assert ai_analyst.trade_advice_words("多周期偏空，上方阻力 2767") == []


def test_alert_dedup_and_daily_cap(tmp_path, capsys):
    settings = replace(_settings(tmp_path), max_alerts_per_day=2)
    db = Database(settings.db_path)
    alerter = Alerter(settings, db)
    assert alerter.send("k1", "hello") is True
    assert alerter.send("k1", "hello") is False  # 冷却中
    assert alerter.send("k2", "hi") is True
    assert alerter.send("k3", "hey") is False  # 超过每日上限
    assert "仅供参考" in capsys.readouterr().out
    db.close()
