"""把代码算好的数字交给本地 Ollama 写成中文分析。

- AI 只能用我们给的数字；输出里出现没给过的价位，就当作编造，改用文字模板。
- Ollama 没开、太慢或出错，也改用文字模板，所以没装 AI 也能用。
"""
from __future__ import annotations

import json
import logging
import re

import requests

from app.config import Settings

log = logging.getLogger(__name__)

PROMPT = """你是一名黄金（XAU/USD）技术分析助理。下面是程序已经算好的数据（JSON）。
规则：
1. 只能使用下面数据里出现的数字，不准自己编任何价格或指标数值。
2. 用简体中文，严格按以下五个标题输出，每项 1–3 句：
【局面】
【理由】
【关键位】
【失效条件】
【风险提醒】
3. 如果有信号，在【关键位】写出入场区、止损、目标；没有信号就说明在等什么。
4. 【风险提醒】最后一句固定写：仅供参考，不构成投资建议。

数据：
{facts}
"""

_NUMBER = re.compile(r"\d[\d,]*\.?\d*")


def _collect_numbers(obj, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        for m in _NUMBER.findall(obj):
            try:
                out.append(float(m.replace(",", "")))
            except ValueError:
                pass


def invented_prices(text: str, facts: dict, min_value: float = 500.0, tol: float = 0.6) -> list[float]:
    """找出 AI 写了、但数据里没有的「价格级」数字（>= min_value）。"""
    known: list[float] = []
    _collect_numbers(facts, known)
    bad = []
    for m in _NUMBER.findall(text):
        try:
            val = float(m.replace(",", ""))
        except ValueError:
            continue
        if val >= min_value and not any(abs(val - k) <= tol for k in known):
            bad.append(val)
    return bad


def template_analysis(facts: dict) -> str:
    trends = facts["trends"]
    h1 = facts["indicators"]["H1"]
    lv = facts["levels"]["H1"]
    sup = "、".join(f"{x['price']:.2f}" for x in lv["supports"]) or "暂无"
    res = "、".join(f"{x['price']:.2f}" for x in lv["resistances"]) or "暂无"
    trend_txt = "，".join(f"{tf} {t}" for tf, t in trends.items())
    sig = facts.get("signal")
    if sig:
        key = (
            f"信号：{sig['direction']}，入场区 {sig['entry_low']:.2f}–{sig['entry_high']:.2f}，"
            f"止损 {sig['stop']:.2f}，目标 {sig['target']:.2f}（{sig['rr']}R）。"
        )
        invalid = f"价格{'跌破' if sig['direction'] == '多' else '升破'} {sig['stop']:.2f} 信号作废。"
    else:
        key = "暂无信号：" + "；".join(facts.get("strategy_notes") or ["条件未满足"]) + "。"
        invalid = "等待 H4 和 H1 方向一致且价格回到关键位附近。"
    rsi = h1.get("rsi14")
    rsi_note = "超买" if rsi and rsi > 70 else "超卖" if rsi and rsi < 30 else "中性"
    return (
        f"【局面】现价 {facts['price']:.2f}，{trend_txt}。\n"
        f"【理由】H1 EMA20 {h1.get('ema20')}，EMA50 {h1.get('ema50')}，EMA200 {h1.get('ema200')}；"
        f"RSI {rsi}（{rsi_note}），MACD 柱 {h1.get('macd_hist')}，ATR {h1.get('atr14')}。\n"
        f"【关键位】上方阻力 {res}；下方支撑 {sup}。{key}\n"
        f"【失效条件】{invalid}\n"
        "【风险提醒】非农、CPI、美联储公布前后波动大，指标容易失灵。仅供参考，不构成投资建议。"
    )


def analyze(facts: dict, settings: Settings) -> tuple[str, str]:
    """返回 (分析文字, 来源 'ollama' 或 'template')。"""
    prompt = PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, indent=1))
    try:
        resp = requests.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=settings.ollama_timeout_sec,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
    except (requests.RequestException, ValueError) as exc:
        log.info("Ollama 不可用（%s），改用文字模板", exc.__class__.__name__)
        return template_analysis(facts), "template"

    if not text:
        return template_analysis(facts), "template"
    bad = invented_prices(text, facts)
    if bad:
        log.warning("AI 输出了数据里没有的价位 %s，改用文字模板", bad[:5])
        return template_analysis(facts), "template"
    return text, "ollama"
