"""把代码算好的数字交给本地 Ollama 写成中文分析。

- AI 只能用我们给的数字；输出里出现没给过的价位，就当作编造，改用文字模板。
- Ollama 没开、太慢或出错，也改用文字模板，所以没装 AI 也能用。
"""
from __future__ import annotations

import copy
import json
import logging
import re

import requests

from app.config import Settings

log = logging.getLogger(__name__)

PROMPT = """你是一名黄金（XAU/USD）技术分析助理。下面是程序已经算好的数据（JSON）。
规则：
1. 只能使用下面数据里出现的数字，不准自己编任何价格或指标数值。
   描述各周期 RSI 和 MACD 时，直接照抄该周期的「rsi状态」「macd状态」，不要自己比较大小。
2. 用简体中文，严格按以下五个标题输出，每项 1–3 句：
【局面】
【理由】
【关键位】
【失效条件】
【风险提醒】
3. 你只负责解读局面。绝对不要给出任何交易建议：不准写入场、开仓、仓位、止损、目标。
   交易信号由程序另外显示，不用你写。【关键位】只列出数据里的支撑和阻力并说明意义。
4. 【风险提醒】最后一句固定写：仅供参考，不构成投资建议。

数据：
{facts}
"""

_NUMBER = re.compile(r"\d[\d,]*\.?\d*")
# AI 不该自己给交易建议；出现这些字就当作越权，改用模板
TRADE_ADVICE_WORDS = ("入场", "开仓", "建仓", "仓位", "止损", "止盈", "目标价", "目标看")


def trade_advice_words(text: str) -> list[str]:
    return [w for w in TRADE_ADVICE_WORDS if w in text]


def signal_line(facts: dict) -> str:
    """由代码生成的信号说明，接在 AI 文字后面。"""
    sig = facts.get("signal")
    if sig:
        return (
            f"【程序信号】{sig['direction']}：入场区 {sig['entry_low']:.2f}–{sig['entry_high']:.2f}，"
            f"止损 {sig['stop']:.2f}，目标 {sig['target']:.2f}（{sig['rr']}R）。"
        )
    notes = "；".join(facts.get("strategy_notes") or ["条件未满足"])
    return f"【程序信号】暂无信号：{notes}。"


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


def _with_labels(facts: dict) -> dict:
    """小模型比较数字容易出错，先由代码把每个周期的状态写成文字。"""
    out = copy.deepcopy(facts)
    for ind in out.get("indicators", {}).values():
        rsi = ind.get("rsi14")
        if rsi is not None:
            ind["rsi状态"] = "超买" if rsi >= 70 else "超卖" if rsi <= 30 else "中性"
        hist = ind.get("macd_hist")
        if hist is not None:
            ind["macd状态"] = "多头动能" if hist > 0 else "空头动能"
    source_names = {"swing": "前高前低", "round": "整数关口", "swing+round": "前高前低+整数关口"}
    for tf_levels in out.get("levels", {}).values():
        for group in tf_levels.values():
            for lv in group:
                lv["source"] = source_names.get(lv.get("source"), lv.get("source"))
    return out


def analyze(facts: dict, settings: Settings) -> tuple[str, str]:
    """返回 (分析文字, 来源 'ollama' 或 'template')。"""
    prompt = PROMPT.format(facts=json.dumps(_with_labels(facts), ensure_ascii=False, indent=1))
    try:
        resp = requests.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": "30m",  # 两次分析之间模型留在显卡里，不用每次重新加载
                "options": {"temperature": 0.2},
            },
            timeout=settings.ollama_timeout_sec,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
    except (requests.RequestException, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            detail += f" {exc.response.status_code} {exc.response.text[:200]}"
        log.info("Ollama 不可用（%s），改用文字模板", detail)
        return template_analysis(facts), "template"

    if not text:
        return template_analysis(facts), "template"
    bad = invented_prices(text, facts)
    if bad:
        log.warning("AI 输出了数据里没有的价位 %s，改用文字模板", bad[:5])
        return template_analysis(facts), "template"
    advice = trade_advice_words(text)
    if advice:
        log.warning("AI 自己写了交易建议（%s），改用文字模板", "、".join(advice))
        return template_analysis(facts), "template"
    return f"{text}\n{signal_line(facts)}", "ollama"
