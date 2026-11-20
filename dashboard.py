"""Streamlit 仪表板：streamlit run dashboard.py   然后浏览器打开 http://localhost:8501
数据全部从 SQLite 读，需要 runner 在后台跑。"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.config import TIMEFRAMES, get_settings
from app.db import Database
from app.indicators import add_indicators
from app.levels import find_levels

st.set_page_config(page_title="XAU/USD 金价分析", page_icon="🥇", layout="wide")
settings = get_settings()

TREND_COLOR = {"多": "#26a69a", "空": "#ef5350", "震荡": "#9e9e9e"}


@st.cache_resource
def get_db() -> Database:
    return Database(settings.db_path)


def chart(df: pd.DataFrame, levels: dict, tf: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K 线", increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
    ))
    for col, color in (("ema20", "#ffca28"), ("ema50", "#42a5f5"), ("ema200", "#ab47bc")):
        fig.add_trace(go.Scatter(x=df.index, y=df[col], name=col.upper(), line=dict(width=1.3, color=color)))
    for lv in levels["supports"]:
        fig.add_hline(y=lv.price, line=dict(color="#26a69a", dash="dot", width=1),
                      annotation_text=f"支撑 {lv.price:.2f}", annotation_position="right")
    for lv in levels["resistances"]:
        fig.add_hline(y=lv.price, line=dict(color="#ef5350", dash="dot", width=1),
                      annotation_text=f"阻力 {lv.price:.2f}", annotation_position="right")
    fig.update_layout(
        template="plotly_dark", height=560, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False, legend=dict(orientation="h", y=1.04),
        title=f"XAU/USD {tf}",
    )
    return fig


tf = st.sidebar.radio("K 线周期", TIMEFRAMES, index=1)
bars = st.sidebar.slider("显示 K 线数量", 60, 400, 150, step=10)
st.sidebar.caption(f"数据源：{settings.data_source}　品种：{settings.mt5_symbol}")
st.sidebar.caption("仅供学习和研究，不构成投资建议。")


@st.fragment(run_every=5)
def live() -> None:
    db = get_db()
    tick = db.latest_tick()
    analysis = db.latest_analysis()
    if not tick or not analysis:
        st.info("还没有数据。请先运行 runner（双击 start.bat，或 python -m app.runner）。")
        return
    payload = analysis["payload"]
    price = (tick["bid"] + tick["ask"]) / 2
    day_open = payload.get("day_open")

    st.title("🥇 XAU/USD 实时分析")
    c1, c2, c3, c3b, c4 = st.columns(5)
    change = price - day_open if day_open else None
    c1.metric("最新价", f"{price:.2f}",
              f"{change:+.2f}（{change / day_open:+.2%}）" if change is not None else None)
    c2.metric("Bid", f"{tick['bid']:.2f}")
    c3.metric("Ask", f"{tick['ask']:.2f}")
    c3b.metric("点差", f"{tick['ask'] - tick['bid']:.2f}")
    tick_time = datetime.fromisoformat(tick["time"])
    age = (datetime.now(timezone.utc) - tick_time).total_seconds()
    c4.metric("报价时间 (UTC)", tick_time.strftime("%H:%M:%S"),
              "数据延迟！" if age > 60 else None, delta_color="inverse")

    st.subheader("多周期趋势")
    cols = st.columns(len(TIMEFRAMES))
    for col, name in zip(cols, TIMEFRAMES):
        trend = payload["trends"].get(name, "-")
        ind = payload["indicators"].get(name, {})
        col.markdown(
            f"<div style='border:1px solid #333;border-radius:8px;padding:10px;text-align:center'>"
            f"<div style='opacity:.7'>{name}</div>"
            f"<div style='font-size:1.6em;color:{TREND_COLOR.get(trend, '#fff')}'>{trend}</div>"
            f"<div style='opacity:.7;font-size:.85em'>RSI {ind.get('rsi14')} · ATR {ind.get('atr14')}</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    left, right = st.columns([2, 1])
    with left:
        raw = db.load_candles(tf, limit=max(bars + 250, 500))
        if raw.empty:
            st.warning(f"没有 {tf} 的 K 线数据")
        else:
            df = add_indicators(raw)
            last_atr = float(df["atr14"].iloc[-1]) if pd.notna(df["atr14"].iloc[-1]) else 0.0
            levels = find_levels(df, price, last_atr, round_step=settings.round_level_step)
            st.plotly_chart(chart(df.tail(bars), levels, tf), use_container_width=True)
    with right:
        source = "本地 AI（Ollama）" if analysis["ai_source"] == "ollama" else "文字模板"
        st.subheader("最新分析")
        st.caption(f"{analysis['time'][:16].replace('T', ' ')} UTC · {source}")
        st.markdown(analysis["ai_text"].replace("\n", "  \n"))
        sig = payload.get("signal")
        if sig:
            st.success(f"信号：{sig['direction']}　入场 {sig['entry_low']}–{sig['entry_high']}　"
                       f"止损 {sig['stop']}　目标 {sig['target']}")

    st.subheader("信号记录")
    signals = db.recent_signals(50)
    if signals.empty:
        st.caption("暂无信号")
    else:
        signals["reasons"] = signals["reasons"].str.replace(r'[\[\]"]', "", regex=True)
        st.dataframe(signals.drop(columns=["id"]), use_container_width=True, hide_index=True)


live()
