"""Frozen decisions and matured outcomes. Research marks are never paper fills."""
from __future__ import annotations

import hashlib
import json
from datetime import date, time

import pandas as pd

from . import storage
from .core import (PAPER_ACCOUNT, TradingCalendar, account_path, atomic_json, data_dir,
                   dt, iso, now, paper_account, read_json)

METHOD = ("以决策后第一个完整交易日开盘为研究参考价（盘前取当日，盘中/盘后取下一交易日），"
          "观察其后 1/3/5 个交易日收盘表现；不是建议价成交回测，不计入模拟账户。"
          "日线最高最低只作区间观察，不推断触价先后。")


def decisions():
    """Preserve versions, but count a security once per premarket/intraday cohort."""
    grouped = {}
    for folder, pattern in [("reports", "*.json"), ("candidate_versions", "*-live.json"), ("decision_pools", "*.json")]:
        for path in sorted((data_dir() / folder).glob(pattern)):
            value = read_json(path, {})
            stamp = value.get("generated_at")
            if not stamp:
                continue
            kind = value.get("kind", "intraday_supplement")
            group = (value.get("trade_date", stamp[:10]), kind)
            grouped.setdefault(group, []).append(value)
    result = []
    for (day, kind), versions in sorted(grouped.items()):
        versions.sort(key=lambda r: r["generated_at"])
        # Last *actual* premarket snapshot is the final premarket decision.
        if kind == "premarket":
            versions = [v for v in versions if dt(v["generated_at"]).time().replace(tzinfo=None) < time(9, 30)][-1:]
        seen = set()
        first_top = {}
        exclusions = {}
        for value in versions:
            for candidate in value.get("candidates",[]):
                first_top.setdefault(candidate["code"],value["id"])
            for candidate in value.get("excluded",[]):
                exclusions.setdefault(candidate["code"],(value,candidate))
        for value in versions:
            selected = {r["code"] for r in value.get("candidates", [])}
            rows = value.get("research_pool") or value.get("candidates", [])
            for rank, candidate in enumerate(rows, 1):
                if candidate["code"] in seen:
                    continue
                if candidate["code"] in first_top and first_top[candidate["code"]] != value["id"]:
                    continue
                seen.add(candidate["code"])
                result.append({"day": day, "kind": kind, "source_id": value["id"],
                    "decision_at": value["generated_at"], "rank": rank, "candidate": candidate,
                    "in_top_list": candidate["code"] in selected, "strategy_version": value.get("strategy_version"),
                    "model_version": candidate.get("model_version", "rules"),
                    "filtered": candidate.get("risks", [])})
        for code,(value,candidate) in exclusions.items():
            if code in seen:
                continue
            result.append({"day": day, "kind": kind, "source_id": value["id"],
                "decision_at": value["generated_at"], "rank": None, "candidate": candidate,
                "in_top_list": False, "strategy_version": value.get("strategy_version"),
                "model_version": "rules", "filtered": [candidate["reason"]]})
    return result


def outcomes(decision, frame, calendar, at):
    stamp = dt(decision["decision_at"])
    first = stamp.date().isoformat()
    if stamp.time().replace(tzinfo=None) >= time(9, 30):
        later = [d for d in calendar.days if d > first]
        first = later[0] if later else None
    result = {"reference_date": first, "reference_price": None, "horizons": {}}
    if frame.empty or not first or first not in calendar.days:
        return {**result, "status": "missing", "reason": "参考交易日行情或日历缺失"}
    visible = frame[pd.to_datetime(frame.available_at, utc=True) <= at].copy() if "available_at" in frame else frame.iloc[0:0]
    visible["date"] = visible.date.astype(str).str[:10]
    visible = visible.drop_duplicates("date").set_index("date")
    start = calendar.days.index(first)
    if first in visible.index:
        result["reference_price"] = float(visible.loc[first, "open"])
    complete = calendar.completed(at)
    for horizon in (1, 3, 5):
        end = calendar.days[start + horizon] if start + horizon < len(calendar.days) else None
        label = {"end_date": end, "status": "observing", "return_pct": None, "favorable_pct": None, "adverse_pct": None}
        if end and end <= complete:
            needed = calendar.days[start:start + horizon + 1]
            if any(d not in visible.index for d in needed):
                label.update(status="missing", reason="区间存在停牌或缺失日线，不能当作零收益")
            else:
                window = visible.loc[needed]
                uncertain = window.get("corporate_action_event", pd.Series(False, index=window.index)).fillna(False).any()
                uncertain = uncertain or not window.get("corporate_action_checked", pd.Series(False,index=window.index)).eq(True).all()
                ref = result["reference_price"]
                if uncertain or not ref or ref <= 0:
                    label.update(status="unverified", reason="区间公司行动未核实，原始价格不能直接比较")
                else:
                    label.update(status="matured", return_pct=(float(window.close.iloc[-1])/ref-1)*100,
                                 favorable_pct=(float(window.high.max())/ref-1)*100,
                                 adverse_pct=(float(window.low.min())/ref-1)*100)
        result["horizons"][str(horizon)] = label
    result["status"] = "matured" if all(h["status"] == "matured" for h in result["horizons"].values()) else "observing"
    return result


def record_execution(candidates, signals, regime, quotes, at):
    """Aggregate actual observations, not guessed historical non-fill reasons."""
    from . import broker
    account = broker.account()
    signal_map = {r["code"]: r for r in signals}
    quote_map = {r["code"]: r for r in quotes}
    with storage.database() as con:
        for candidate in candidates:
            code = candidate["code"]
            signal = signal_map.get(code, {})
            if con.execute("SELECT 1 FROM trades WHERE symbol=? AND side='buy' AND substr(ts,1,10)=?", (code, at.date().isoformat())).fetchone():
                reason = "已模拟买入"
            elif not account["enabled"]:
                reason = "账户暂停"
            elif account.get("drawdown_halted"):
                reason = "回撤锁定新增"
            elif regime.get("state") == "halt":
                reason = "市场或数据条件暂停：" + regime.get("reason", "")
            elif signal.get("state") in {"blocked", "invalid"}:
                reason = "条件失效：" + signal.get("label", "风险校验未通过")
            elif len(account["positions"]) >= 5:
                reason = "持仓数量已满"
            elif signal.get("state") == "triggered":
                reason = "已触发，等待后续可成交报价、成交量及仓位检查"
            else:
                reason = signal.get("label", "未到建议价格或缺少有效行情")
            payload = {"candidate": candidate, "signal": signal, "quote": quote_map.get(code), "regime": regime}
            con.execute("""INSERT INTO observations VALUES (?,?,?,?,?,?,?) ON CONFLICT(day,code,reason)
                DO UPDATE SET last_at=excluded.last_at,count=count+1,payload=excluded.payload""",
                (at.date().isoformat(), code, reason, iso(at), iso(at), 1, json.dumps(payload, ensure_ascii=False)))


def execution_for_day(day, name):
    from . import broker
    with paper_account(name):
        plan_files = sorted(account_path("daily_plans").glob(day + "*.json"))
        plans = [read_json(p) for p in plan_files]
        with storage.database() as con:
            trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE substr(ts,1,10)=? ORDER BY ts", (day,))]
            observations = [dict(r) for r in con.execute("SELECT code,reason,first_at,last_at,count FROM observations WHERE day=? ORDER BY code,first_at", (day,))]
        return {"account": name, "plans": plans, "trades": trades, "observations": observations,
                "note": "没有观测记录时无法区分离线、未运行或未被跟踪，不推断历史成交。"}


def rebuild(at=None, local=True):
    at = at or now()
    calendar = TradingCalendar()
    if not calendar.known(at.date()):
        return {"state": "blocked", "reason": "交易日历未覆盖当前日期"}
    rows = [r for r in decisions() if dt(r["decision_at"]) <= at]
    old_reviews = {p.stem:read_json(p,{}) for p in (data_dir()/"reviews").glob("????-??-??.json")}
    matured_cache = {(r["source_id"],r["candidate"]["code"]):r["outcome"] for value in old_reviews.values() for r in value.get("items",[])
               if r.get("outcome",{}).get("horizons",{}).get("5",{}).get("status") == "matured"}
    frames = storage.bars_many(sorted({r["candidate"]["code"] for r in rows if (r["source_id"],r["candidate"]["code"]) not in matured_cache}))
    from .learning import benchmark_frame
    benchmark = benchmark_frame()
    grouped = {}
    for row in rows:
        c = row["candidate"]
        result = matured_cache.get((row["source_id"],c["code"])) or outcomes(row, frames.get(c["code"], pd.DataFrame()), calendar, at)
        for h in result.get("horizons",{}).values():
            start,end = result.get("reference_date"),h.get("end_date")
            if h["status"] == "matured" and start in benchmark.index and end in benchmark.index and "open" in benchmark:
                opening = float(benchmark.loc[start,"open"])
                if opening > 0:
                    h["benchmark_return_pct"] = (float(benchmark.loc[end,"close"])/opening-1)*100
                    h["excess_return_pct"] = h["return_pct"]-h["benchmark_return_pct"]
        matured = result.get("horizons",{}).get("3",{})
        observations = []
        if matured.get("status") == "matured":
            if matured.get("excess_return_pct") is not None:
                observations.append("3 日研究表现跑赢同期沪深 300" if matured["excess_return_pct"]>0 else "3 日研究表现未体现基准优势")
            else:
                observations.append("同期基准数据缺失，暂不能判断超额表现")
            plan = c.get("price_plan",{})
            if plan.get("take_profit") and result.get("reference_price"):
                distance = (plan["take_profit"]/result["reference_price"]-1)*100
                if matured.get("favorable_pct",0) >= distance:
                    observations.append("研究区间高点覆盖原止盈价；日线无法确认实际成交顺序")
        row = {**row,"findings":observations}
        grouped.setdefault(row["day"], []).append({**row, "outcome": result})
    changed = 0
    for day, items in grouped.items():
        top = [r for r in items if r["in_top_list"]]
        stats = {}
        for h in ("1", "3", "5"):
            observed = [r["outcome"]["horizons"].get(h, {}) for r in top]
            returns = [v["return_pct"] for v in observed if v.get("status") == "matured"]
            stats[h] = {"matured": len(returns), "total": len(top), "mean_return_pct": sum(returns)/len(returns) if returns else None,
                        "positive_pct": sum(r > 0 for r in returns)/len(returns)*100 if returns else None}
        final_dates = [h["end_date"] for r in items for h in r["outcome"].get("horizons",{}).values() if h.get("end_date")]
        through = min(calendar.completed(at), max(final_dates,default=day))
        body = {"schema_version": 1, "trade_date": day, "data_through": through,
                "items": items, "summary": stats, "method": METHOD,
                "execution": [execution_for_day(day, n) for n in ("baseline", "experiment")] if local else [],
                "historical_limitations": "历史未保存的过滤条件、新闻和执行观测不补造；相同股票在不同名单类别中分别观察。"}
        revision = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        path = data_dir() / "reviews" / f"{day}.json"
        old = old_reviews.get(day,{})
        if old.get("revision") != revision:
            old = {**body, "revision": revision, "generated_at": iso(at)}
            atomic_json(path,old)
            changed += 1
        old_reviews[day] = old
    status = {"state": "success", "finished_at": iso(at), "dates": len(grouped), "updated": changed,
              "data_through": calendar.completed(at), "local_execution_included": local}
    storage.save_snapshot("review_status", status)
    storage.save_snapshot("review_index",{"items":[{k:r.get(k) for k in ("trade_date","generated_at","data_through","revision","summary")}
                            for day,r in sorted(old_reviews.items(),reverse=True)]})
    return status


def listing():
    items = storage.snapshot("review_index").get("items",[])
    cal, at = TradingCalendar(), now()
    previous = cal.previous(at.date()) if cal.known(at.date()) else None
    return {"items": items, "default_date": previous, "status": storage.snapshot("review_status"), "method": METHOD}
