"""把代码算好的数字交给本地 Ollama 写成中文分析。

- AI 只负责解读局面；交易计划和信号全部由代码算，接在 AI 文字后面（program_block）。
- AI 只能用我们给的数字；输出里出现没给过的价位、或自己写交易建议，就改用文字模板。
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
   交易计划由程序另外显示，不用你写。【关键位】照抄「关键位」里最近的一个阻力和一个支撑的句子，
   不要改动来源和强度。
4. 【风险提醒】最后一句固定写：仅供参考，不构成投资建议。

数据：
{facts}
"""

_NUMBER = re.compile(r"\d[\d,]*\.?\d*")
# AI 不该自己给交易建议；出现这些字就当作越权，改用模板
TRADE_ADVICE_WORDS = ("入场", "开仓", "建仓", "仓位", "止损", "止盈", "目标价", "目标看")


def trade_advice_words(text: str) -> list[str]:
    return [w for w in TRADE_ADVICE_WORDS if w in text]


def _dir_word(d: str) -> str:
    return "做多" if d == "多" else "做空"


def program_block(facts: dict) -> str:
    """由代码生成的交易计划和信号，接在分析文字后面。"""
    lines = [f"【程序判断】方向：{facts.get('bias', '-')}（H4 和 D1 同向才有方向，否则按震荡市处理）"]
    sig = facts.get("signal")
    if sig:
        lines.append(
            f"【程序信号】{_dir_word(sig['direction'])}：进场参考 {sig['entry_low']:.2f}–{sig['entry_high']:.2f}，"
            f"止损 {sig['stop']:.2f}，目标 {sig['target']:.2f}（{sig['rr']:.0f}R）。根据：" + "；".join(sig["reasons"])
        )
    else:
        notes = "；".join(dict.fromkeys(facts.get("strategy_notes") or ["条件未满足"]))
        lines.append(f"【程序信号】暂无：{notes}。")
    for i, p in enumerate(facts.get("plan") or [], 1):
        lines.append(
            f"【计划 {i}：{_dir_word(p['direction'])}】关注 {p['zone_low']:.2f}–{p['zone_high']:.2f}"
            f"（离现价 {p['distance_atr']} 个 ATR）。触发：{p['trigger']}。"
            f"参考止损 {p['stop']:.2f}，目标 {p['target']:.2f}。根据：" + "；".join(p["basis"])
            + f"。作废：{p['invalidation']}。"
        )
    if not facts.get("plan"):
        lines.append("【计划】附近没有合格的关键位，先观望。")
    return "\n".join(lines)


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


def _zone_text(zones: list[dict]) -> str:
    if not zones:
        return "暂无"
    return "、".join(f"{z['price']:.2f}（{'+'.join(z['sources'])}，强度 {z['score']}）" for z in zones[:3])


def template_analysis(facts: dict) -> str:
    trends = facts["trends"]
    h1 = facts["indicators"]["H1"]
    zones = facts.get("zones", {})
    trend_txt = "，".join(f"{tf} {t}" for tf, t in trends.items())
    rsi = h1.get("rsi14")
    rsi_note = "超买" if rsi and rsi >= 70 else "超卖" if rsi and rsi <= 30 else "中性"
    return (
        f"【局面】现价 {facts['price']:.2f}，{trend_txt}。\n"
        f"【理由】H1 EMA20 {h1.get('ema20')}，EMA50 {h1.get('ema50')}，EMA200 {h1.get('ema200')}；"
        f"RSI {rsi}（{rsi_note}），MACD 柱 {h1.get('macd_hist')}，ATR {h1.get('atr14')}。\n"
        f"【关键位】上方阻力 {_zone_text(zones.get('resistances', []))}；"
        f"下方支撑 {_zone_text(zones.get('supports', []))}。\n"
        "【失效条件】见下方各计划的「作废」条件。\n"
        "【风险提醒】非农、CPI、美联储公布前后波动大，关键位容易被瞬间击穿。仅供参考，不构成投资建议。\n"
        + program_block(facts)
    )


def _zone_sentence(z: dict) -> str:
    return f"{z['price']:.2f}：由{'、'.join(z['sources'])}组成，强度 {z['score']}"


def _for_ai(facts: dict) -> dict:
    """给 AI 的数据：去掉交易计划和信号（那部分由代码写），并把状态先写成文字。"""
    out = copy.deepcopy(facts)
    for key in ("plan", "signal", "strategy_notes"):
        out.pop(key, None)
    # 小模型读嵌套 JSON 容易张冠李戴，关键位直接写成句子
    zones = out.pop("zones", {})
    out["关键位"] = {
        "上方阻力": [_zone_sentence(z) for z in zones.get("resistances", [])[:3]],
        "下方支撑": [_zone_sentence(z) for z in zones.get("supports", [])[:3]],
    }
    for ind in out.get("indicators", {}).values():
        rsi = ind.get("rsi14")
        if rsi is not None:
            ind["rsi状态"] = "超买" if rsi >= 70 else "超卖" if rsi <= 30 else "中性"
        hist = ind.get("macd_hist")
        if hist is not None:
            ind["macd状态"] = "多头动能" if hist > 0 else "空头动能"
    return out


def analyze(facts: dict, settings: Settings) -> tuple[str, str]:
    """返回 (分析文字, 来源 'ollama' 或 'template')。"""
    prompt = PROMPT.format(facts=json.dumps(_for_ai(facts), ensure_ascii=False, indent=1))
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
    return f"{text}\n{program_block(facts)}", "ollama"
