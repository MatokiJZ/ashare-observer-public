"""Transparent research overlays, with missing evidence blocking new positions."""
from datetime import datetime

import numpy as np

from .core import dt, iso

RISK_TERMS = ("立案", "退市", "重大违法", "无法表示意见", "否定意见", "债务逾期", "业绩预亏", "减持计划", "破产", "停牌", "风险警示", "分红派息实施", "权益分派实施", "除权除息", "配股")
TOPICS = {"半导体": ("芯片", "半导体", "出口管制"), "能源": ("原油", "石油", "天然气", "能源"),
          "银行与金融": ("降息", "加息", "银行", "美联储", "日本央行"), "出口产业": ("关税", "贸易", "出口"),
          "宏观政策": ("政策", "财政", "货币", "地缘", "制裁")}


def event_context(items: list[dict], at: datetime) -> dict:
    rows = []
    for event in items:
        if not event.get("published_at") or not event.get("first_seen_at"):
            continue
        if max(dt(event["published_at"]), dt(event["first_seen_at"])) > at:
            continue
        title = event.get("title", "")
        rows.append({**event, "risk_terms": [s for s in RISK_TERMS if s in title],
                     "related_sectors": [k for k, words in TOPICS.items() if any(w in title for w in words)]})
    return {"items": sorted(rows, key=lambda r: r["published_at"], reverse=True), "asof": iso(at),
            "risk_hits": sum(bool(r["risk_terms"]) for r in rows), "method": "标题关键词规则，仅标注待核实风险及相关行业，不推断价格方向。"}


def crowding(rows: list[dict]) -> dict:
    usable = [r for r in rows if (r.get("amount") or 0) > 0 and r.get("change_pct") is not None]
    amounts = sorted([r["amount"] for r in usable], reverse=True)
    total = sum(amounts)
    return {"top20_amount_share": sum(amounts[:20]) / total if total else None,
            "extreme_move_share": sum(abs(r["change_pct"]) >= 7 for r in usable) / len(usable) if usable else None,
            "return_dispersion": float(np.std([r["change_pct"] for r in usable])) if usable else None,
            "samples": len(usable), "note": "成交集中度、大幅波动比例和横截面离散度是拥挤代理，不能识别量化资金的真实仓位。"}


def overseas_overlay(regime: dict, markets: dict, at: datetime) -> dict:
    current = dict(regime)
    visible = {q["symbol"]: q for q in markets.get("items", []) if q.get("market_time") and 0 <= (at - dt(q["market_time"])).total_seconds() <= 4 * 86400 and q.get("change_pct") is not None}
    shocks = [q["name"] for s, q in visible.items() if s in ("^GSPC", "^IXIC", "^N225", "^KS11") and q["change_pct"] <= -2]
    if shocks and current.get("state") == "normal":
        current.update(state="cautious", label="谨慎参与", exposure=.4, reason="外围指数跌幅达到 2%：" + "、".join(shocks))
    current["overseas_shocks"] = shocks
    return current


def apply_candidate_context(candidates, sector_histories, sector_rows, announcements, at):
    sector_symbols = {r["name"]: r["symbol"] for r in sector_rows}
    events = event_context(announcements.get("items", []), at)["items"]
    for c in candidates:
        # Re-evaluation always starts from the original technical/financial evidence.
        c.setdefault("base_risks", list(c.get("risks", [])))
        c.setdefault("base_reasons", list(c.get("reasons", [])))
        c.setdefault("technical_score", c["score"])
        c["risks"] = list(c["base_risks"])
        c["reasons"] = list(c["base_reasons"])
        c["score"] = c["technical_score"]
        points = sector_histories.get(sector_symbols.get(c["sector"]), [])
        points = [p for p in points if p.get("date", "") <= c["features"]["asof"]]
        if len(points) >= 21:
            closes = [p["close"] for p in points]
            c["sector_return20"] = closes[-1] / closes[-21] - 1
            strong = closes[-1] > float(np.mean(closes[-20:])) and c["sector_return20"] > 0
            c["sector_strong"] = strong
            c["score"] = round(c["score"] * .9 + min(1, max(0, .5 + c["sector_return20"] * 5)) * 10, 1)
            if not strong:
                c["risks"].append("行业未处于 20 日上升趋势")
            else:
                c["reasons"].append(f"行业 20 日上涨 {c['sector_return20']:.1%} 且位于 20 日均线上方")
        else:
            c["sector_return20"] = None
            c["risks"].append("行业历史趋势待核实")
        disclosure = announcements.get("coverage", {}).get(c["code"], {})
        if not disclosure.get("ok") or not disclosure.get("fetched_at") or (at - dt(disclosure["fetched_at"])).total_seconds() > 86400:
            c["risks"].append("近期公司公告覆盖不完整")
        c["events"] = [e for e in events if e.get("code") == c["code"]]
        uncertain_times = [e for e in announcements.get("items", []) if e.get("code") == c["code"] and (not e.get("published_at") or dt(e["published_at"]) > at)]
        if uncertain_times:
            c["risks"].append("近期公告发布时间尚需核实")
        for event in c["events"]:
            if event["risk_terms"]:
                c["risks"].append("公告需复核：" + event["title"])
        pe = c.get("valuation", {}).get("pe")
        if pe is None or pe <= 0:
            c["risks"].append("市盈率缺失或非正值，盈利估值待核实")
        elif pe > 100:
            c["risks"].append("动态市盈率超过 100 倍")
        c["risks"] = list(dict.fromkeys(c["risks"]))
        c["trade_eligible"] = not c["risks"]
        c["status"] = "观察 / 风险待核实" if c["risks"] else "等待盘中确认"
    return sorted(candidates, key=lambda c: (not c["trade_eligible"], -c["score"], c["code"]))
