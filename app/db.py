"""SQLite 存储：K 线、最新报价、每次分析、信号、已发送提醒。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    timeframe TEXT NOT NULL, time TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (timeframe, time)
);
CREATE TABLE IF NOT EXISTS ticks (
    time TEXT PRIMARY KEY, bid REAL, ask REAL
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
    price REAL, payload TEXT, ai_text TEXT, ai_source TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
    direction TEXT, timeframe TEXT, entry REAL, entry_low REAL, entry_high REAL,
    stop REAL, target REAL, rr REAL, reasons TEXT, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
    key TEXT NOT NULL, text TEXT, delivered INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_key_time ON alerts(key, time);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _write(self, sql: str, params=()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    # ---------- K 线 ----------
    def save_candles(self, timeframe: str, df: pd.DataFrame, keep: int = 1000) -> None:
        rows = [
            (timeframe, _iso(ts.to_pydatetime()), r.open, r.high, r.low, r.close, r.volume)
            for ts, r in df.iterrows()
        ]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)", rows
            )
            self.conn.execute(
                """DELETE FROM candles WHERE timeframe=? AND time NOT IN (
                       SELECT time FROM candles WHERE timeframe=? ORDER BY time DESC LIMIT ?)""",
                (timeframe, timeframe, keep),
            )
            self.conn.commit()

    def load_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT time, open, high, low, close, volume FROM candles WHERE timeframe=? "
            "ORDER BY time DESC LIMIT ?",
            self.conn,
            params=(timeframe, limit),
        )
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601")
        return df.set_index("time").sort_index()

    # ---------- 报价 ----------
    def save_tick(self, time: datetime, bid: float, ask: float) -> None:
        self._write("INSERT OR REPLACE INTO ticks VALUES (?,?,?)", (_iso(time), bid, ask))
        self._write("DELETE FROM ticks WHERE time < ?", (_iso(_now() - timedelta(days=1)),))

    def latest_tick(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM ticks ORDER BY time DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------- 分析 ----------
    def save_analysis(self, price: float, payload: dict, ai_text: str, ai_source: str) -> int:
        return self._write(
            "INSERT INTO analyses (time, price, payload, ai_text, ai_source) VALUES (?,?,?,?,?)",
            (_iso(_now()), price, json.dumps(payload, ensure_ascii=False), ai_text, ai_source),
        )

    def latest_analysis(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"] or "{}")
        return out

    # ---------- 信号 ----------
    def save_signal(self, sig: dict) -> int:
        return self._write(
            """INSERT INTO signals (time, direction, timeframe, entry, entry_low, entry_high,
                                    stop, target, rr, reasons) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                _iso(_now()), sig["direction"], sig["timeframe"], sig["entry"], sig["entry_low"],
                sig["entry_high"], sig["stop"], sig["target"], sig["rr"],
                json.dumps(sig["reasons"], ensure_ascii=False),
            ),
        )

    def recent_signals(self, limit: int = 50) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", self.conn, params=(limit,)
        )

    def has_similar_open_signal(self, direction: str, entry: float, tolerance: float) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE status='open' AND direction=? AND ABS(entry-?)<=? "
            "AND time >= ? LIMIT 1",
            (direction, entry, tolerance, _iso(_now() - timedelta(hours=24))),
        ).fetchone()
        return row is not None

    # ---------- 提醒 ----------
    def alert_sent_since(self, key: str, since: datetime) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE key=? AND time>=? LIMIT 1", (key, _iso(since))
        ).fetchone()
        return row is not None

    def alerts_count_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE time>=?", (_iso(since),)
        ).fetchone()[0]

    def record_alert(self, key: str, text: str, delivered: bool) -> None:
        self._write(
            "INSERT INTO alerts (time, key, text, delivered) VALUES (?,?,?,?)",
            (_iso(_now()), key, text, int(delivered)),
        )
