"""Versioned conditional prices, not promised future returns."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math

import numpy as np

from .core import config, dt, iso, quote_fresh


def visible(rows, at):
    return [r for r in rows if r.get("published_at") and r.get("first_seen_at")
            and max(dt(r["published_at"]),dt(r["first_seen_at"])) <= at]


def value_scenarios(candidate, evidence, at):
    cfg = config()
    year = str(at.year+1)
    latest = {}
    for r in sorted(visible(evidence.get("reports", []), at), key=lambda r:r["published_at"]):
        if (at-dt(r["published_at"])).days <= cfg["forecast_max_age_days"] and r.get("institution") and r.get("forecast_eps", {}).get(year) is not None:
            latest[r["institution"]] = r
    eps = [r["forecast_eps"][year] for r in latest.values()]
    peers = list({r.get("code"):r for r in evidence.get("peers", []) if r.get("code")}.values())
    peer_current = bool(evidence.get("peer_fetched_at") and 0 <= (at-dt(evidence["peer_fetched_at"])).total_seconds() <= 7*86400)
    multiples = [r["pe"] for r in peers if r.get("code") != candidate["code"] and r.get("pe") and 0 < r["pe"] <= 100] if peer_current else []
    orders = visible(evidence.get("orders", []), at)
    industries = visible(evidence.get("industry_reports", []), at)
    reasons = [f"同一机构仅取最新一份预测；{year} 年 EPS 共 {len(eps)} 家机构，行业估值样本 {len(multiples)} 家。",
               "EPS × 行业估值倍数为情景假设，行业动态 PE 并非远期 PE；不是统计置信区间，也不保证届时达到。",
               "订单仅按披露阶段列示；金额、利润率和履约期未核实，不在研报 EPS 上重复加计。"]
    finance = candidate.get("financials", [])
    if finance:
        f = finance[0]
        number = lambda key: f"{f[key]:.2f}%" if isinstance(f.get(key),(int,float)) else "未取得"
        reasons.append(f"已披露财报 {f.get('report_date','')}：营收同比 {number('revenue_growth')}，利润同比 {number('profit_growth')}，ROE {number('roe')}；盈利预测仍需后续财报验证。")
    result = {"status":"insufficient", "horizon":f"6–12 个月情景参考，使用 {year} 年预测 EPS；不延长短线持有期",
              "method":"peer_eps",
              "forecast_year":int(year), "institution_count":len(eps), "peer_count":len(multiples),
              "bear":None, "base":None, "bull":None, "reasons":reasons, "asof":iso(at),
              "reports":visible(evidence.get("reports", []),at)[:12], "industry_reports":industries[:6], "orders":orders[:10],
              "peer_sample": peers if peer_current else [], "peer_fetched_at":evidence.get("peer_fetched_at"), "peer_basis":evidence.get("peer_basis"),
              "errors":[x.split("：")[0]+"本次获取失败，等待重试" if "HTTPSConnectionPool" in x or "timed out" in x else x
                        for x in evidence.get("errors", []) + ([evidence["error"]] if evidence.get("error") else [])]}
    targets = []
    for r in latest.values():
        prices = [r[k] for k in ["target_low","target_high"] if isinstance(r.get(k),(int,float)) and math.isfinite(r[k]) and r[k] > 0]
        if prices:
            targets.append({"institution":r["institution"], "mid":sum(prices)/len(prices), "url":r.get("url"), "published_at":r["published_at"]})
    if len(targets) >= cfg["forecast_min_institutions"]:
        prices = [r["mid"] for r in targets]
        result.update(status="scenario",method="analyst_targets",bear=round(min(prices),2),base=round(float(np.median(prices)),2),bull=round(max(prices),2),
                      analyst_targets=targets,horizon="机构明确目标价参考，原研报期限未统一；不延长 1–5 个交易日持有期",
                      reason=f"优先采用 {len(prices)} 家机构原研报明确给出的目标价；每家区间取中点，基准取机构中位数。较低 / 较高参考取这些中点的最小 / 最大值，并非悲观与乐观情景。")
        result["reasons"][1] = "明确目标价优先于宽行业 PE 外推；机构观点可能共同偏乐观，区间不代表概率或保证达到。"
        return result
    if len(eps) < cfg["forecast_min_institutions"] or len(multiples) < cfg["forecast_min_peers"] or not eps or min(eps) <= 0:
        result["reason"] = "预测机构或可比估值不足，或存在非正盈利预测；暂不给出公司价值价格。"
        return result
    if any(word in candidate.get("sector", "") for word in ["银行", "保险", "证券"]):
        result["reason"] = "金融行业还需 PB、资本充足率及资产质量校验；当前不套用普通公司的 PE 目标价。"
        return result
    e = np.quantile(eps, [.25,.5,.75]); p = np.quantile(multiples,[.25,.5,.75])
    result.update(status="scenario", bear=round(float(e[0]*p[0]),2), base=round(float(e[1]*p[1]),2), bull=round(float(e[2]*p[2]),2),
                  eps_median=float(e[1]), pe_median=float(p[1]), reason=f"基准：{e[1]:.3f} 元预测 EPS × {p[1]:.2f} 倍可比行业估值。保守/乐观分别采用样本四分位组合。")
    return result


def make_plan(candidate, evidence, at):
    cfg = config(); f = candidate.get("features", {})
    required = ["close", "ma10", "ma20", "atr", "high20", "high60"]
    if any(not isinstance(f.get(k), (int,float)) or not math.isfinite(f[k]) or f[k] <= 0 for k in required):
        return {"status":"unavailable", "reasons":["缺少完整日线指标，不能生成价格计划"], "version":cfg["price_plan_version"]}
    atr, close, ma20 = f["atr"], f["close"], f["ma20"]
    valuation = value_scenarios(candidate,evidence,at)
    if candidate["strategy"] == "pullback":
        anchor = max(f["ma10"], ma20+.25*atr)
        low, high = max(ma20+.01,anchor-.25*atr), min(close+.5*atr,anchor+.25*atr)
        structure_stop = ma20-.25*atr
        rationale = f"以 10 日均线 {f['ma10']:.2f} 元附近作为回踩观察位，上下容许 0.25 ATR（{.25*atr:.2f} 元）；跌破 20 日趋势不接低价。"
    else:
        anchor = f["high20"]+.01
        low, high = anchor, min(f["high20"]+.25*atr,close+atr)
        structure_stop = f["high20"]-atr
        rationale = f"20 日高点 {f['high20']:.2f} 元上方一档作为突破触发价，最多高出 0.25 ATR，超过区间不追价。"
    low = math.ceil(low*100)/100; high = math.floor(high*100)/100
    entry = round(min(high,max(low,anchor)),2)
    regular_stop = round(entry*(1-cfg["regular_stop_pct"]),2)
    stop = round(max(regular_stop, high-2*atr, ma20, min(structure_stop,low-.01)),2)
    regular_tp = round(entry*(1+cfg["regular_take_profit_pct"]),2)
    resistances = [x for x in [f["high20"],f["high60"]] if x > high+.01]
    technical_target = round(min(resistances) if resistances else high+3*atr,2)
    target = min(regular_tp, technical_target)
    if valuation["base"] is not None:
        target = min(target,valuation["base"])
    risk = high-stop
    cost = 2*max(cfg["commission_minimum"]/100,high*cfg["commission_rate"]) + (high+target)*(cfg["transfer_fee"]+cfg["slippage_bps"]/10000) + target*cfg["stamp_tax_sell"]
    rr = (target-high-cost)/(risk+cost) if risk > 0 else 0
    blockers = []
    if high < low or stop >= low or target <= high:
        blockers.append("当前支撑、压力或估值位置不形成有效买卖区间")
    if valuation["base"] is not None and valuation["base"] <= high:
        blockers.append(f"公司基准情景 {valuation['base']:.2f} 元低于买入区间，不建立新仓位")
    if rr < cfg["minimum_reward_risk"]:
        blockers.append(f"按买入上限和 100 股费用保守估计，净盈亏比 {rr:.2f}，低于 {cfg['minimum_reward_risk']:.1f}")
    reasons = [rationale, f"常规保护为建议价下方 {cfg['regular_stop_pct']:.0%}；结合 2 ATR 和趋势失效位，采用较紧的 {stop:.2f} 元止损。",
               f"短线压力/波动目标 {technical_target:.2f} 元，常规止盈 {regular_tp:.2f} 元；" + (f"有效止盈取较早到达的 {target:.2f} 元。" if target > high else "估值或压力上限低于买区，不能形成止盈空间。"),
               "重大负面事件可以提前退出；利好不放宽止损、不抬高已冻结的止盈或延长 5 个交易日持有期。"]
    result = {"version":cfg["price_plan_version"], "status":"ready" if not blockers else "observe", "asof":iso(at),
              "trade_date":at.date().isoformat(), "data_date":f.get("asof"), "entry":entry if high >= low else None, "buy_low":low if high >= low else None, "buy_high":high if high >= low else None,
              "stop":stop, "regular_stop":regular_stop, "regular_take_profit":regular_tp, "take_profit":round(target,2) if target > high else None,
              "short_target":technical_target, "short_horizon":"1–5 个交易日条件目标，非收益预测", "reward_risk":round(rr,2),
              "risk_per_share":round(risk,4), "reasons":reasons, "blockers":blockers, "valuation":valuation,
              "event_rule":"新公告/报道出现重大负面线索时触发风控退出，记录信息来源；T+1 不可卖则保留退出意图。"}
    stable = {k:v for k,v in result.items() if k not in {"asof","valuation"}}
    stable["code"] = candidate["code"]
    stable["valuation_inputs"] = {k:valuation.get(k) for k in ["forecast_year","method","status","bear","base","bull","peer_sample"]}
    stable["valuation_inputs"]["reports"] = [{k:r.get(k) for k in ["id","forecast_eps","first_seen_at"]} for r in valuation["reports"]]
    result["id"] = hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return result


def attach(candidates, evidence, at):
    for c in candidates:
        c["price_plan"] = make_plan(c,evidence.get(c["code"],{}),at)
        if c["price_plan"]["status"] != "ready" and not c.get("risks"):
            c["status"] = "观察 / 价格条件未通过"
    return candidates


def daily_selection(candidates, report_id, at, existing=None):
    if existing and existing.get("trade_date") == at.date().isoformat() and existing.get("strategy_version") == config()["strategy_version"] and existing.get("items"):
        return existing
    chosen = [deepcopy(c) for c in candidates if c.get("trade_eligible") and c.get("price_plan",{}).get("status") == "ready"][:config()["daily_picks"]]
    for i,c in enumerate(chosen,1):
        c.update(daily_rank=i,strategy_version=config()["strategy_version"],report_id=report_id)
    return {"trade_date":at.date().isoformat(), "selected_at":iso(at), "report_id":report_id,
            "strategy_version":config()["strategy_version"], "items":chosen,
            "rule":"首次得到合格名单后冻结最多 3 只；当天不补位、不重复买同股。新风险只阻断或收紧退出，不追价。"}


def price_signal(candidate, quote, at, allowed=True):
    plan = candidate.get("price_plan",{})
    result = {"code":candidate["code"], "asof":iso(at), "state":"waiting", "label":"等待建议价格区间", "price_plan":plan}
    if not allowed or not candidate.get("trade_eligible") or plan.get("status") != "ready":
        return {**result,"state":"blocked","label":"当日计划条件失效或风险阻断"}
    if plan.get("trade_date") != at.date().isoformat() or not quote_fresh(quote,at):
        return {**result,"state":"blocked","label":"当日计划或报价不可用"}
    price = quote.get("ask")
    if not price or price <= plan["stop"]:
        return {**result,"state":"invalid","label":"报价缺失或已破止损位"}
    if plan["buy_low"] <= price <= plan["buy_high"]:
        result.update(state="triggered",label="已到建议区间，等待后续可成交报价", id=f"price:{at.date()}:{candidate['code']}:{iso(at)}",
                      created_at=iso(at),expires_at=iso(at+timedelta(minutes=10)),candidate=candidate,execution="observed_volume",
                      reason=f"当日第 {candidate.get('daily_rank','?')} 名，买卖报价触及冻结区间 {plan['buy_low']:.2f}–{plan['buy_high']:.2f}")
    return result
