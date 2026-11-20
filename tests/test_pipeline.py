"""用演示数据源跑一遍完整流程（不需要 MT5、Ollama、Telegram）。"""
from dataclasses import replace

from app import ai_analyst
from app.alerts import Alerter
from app.config import Settings
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


def test_invented_prices_detected():
    facts = {"price": 4012.35, "levels": {"x": [{"price": 3990.0}]}}
    assert ai_analyst.invented_prices("现价 4012.35，支撑 3990", facts) == []
    assert ai_analyst.invented_prices("目标 4100", facts) == [4100.0]


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
