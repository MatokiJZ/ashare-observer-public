"""Live observations are separate from immutable premarket research."""
from copy import deepcopy
import hashlib
import json
import time

from . import jobs, storage, pricing, research_data
from .context import apply_candidate_context, event_context
from .core import account_path, atomic_json, config, data_dir, dt, iso, now, quote_fresh, read_json
from .strategy import mainboard, market_regime, screen


def ensure_pool(report, calendar, at):
    cached = storage.snapshot("candidate_pool")
    cfg = config()
    history_revision = storage.snapshot("history_refresh").get("finished_at")
    if (cached.get("report_id") == report.get("id") and cached.get("strategy_version") == cfg["strategy_version"]
            and cached.get("history_revision") == history_revision
            and cached.get("generated_at") and 0 <= (at - dt(cached["generated_at"])).total_seconds() < cfg["candidate_pool_poll_seconds"]):
        return cached
    universe = storage.snapshot("universe")
    if (universe.get("error") or not universe.get("fetched_at")
            or not 0 <= (at - dt(universe["fetched_at"])).total_seconds() <= 240):
        return {"items": [], "error": "全市场行情过期，暂停更新候选池"}
    rows = [r for r in universe.get("items", []) if mainboard(r["code"]) and (r.get("amount") or 0) > 0]
    histories = storage.bars_many([r["code"] for r in rows])
    preliminary, _ = screen(rows, histories, {}, {}, calendar, at, limit=40)
    meta, finance = jobs.enrich(preliminary, time.monotonic() + 40)
    pool, coverage = screen(rows, histories, meta, finance, calendar, now(), limit=40)
    pool = jobs.enrich_context(pool, time.monotonic() + 45, limit=40)
    evidence = research_data.enrich(pool, time.monotonic() + 65)
    pricing.attach(pool, evidence, now())
    excluded = coverage.pop("excluded", [])
    # One immutable full filtering receipt per completed-history version and day.
    receipt = {"generated_at":iso(),"trade_date":at.date().isoformat(),"candidates":pool[:cfg["max_candidates"]],"research_pool":pool,"excluded":excluded,
               "kind":"intraday_supplement","strategy_version":cfg["strategy_version"]}
    receipt["id"] = at.strftime("%Y-%m-%d-%H%M%S")+"-pool"
    path = data_dir()/"decision_pools"/(at.date().isoformat()+"-"+str(coverage.get("expected_date"))+".json")
    if not path.exists():
        atomic_json(path,receipt)
    payload = {"report_id": report["id"], "generated_at": iso(), "items": pool,
               "history_revision": history_revision,
               "regime": market_regime(universe, coverage["ratio"], coverage["features"], now()),
               "strategy_version": cfg["strategy_version"], "coverage": {k:v for k,v in coverage.items() if k != "features"}}
    storage.save_snapshot("candidate_pool", payload)
    return payload


def rank_live(pool, quotes, sectors, sector_histories, announcements, news, at):
    """Only timestamped current quotes affect ranking. No intraday bar is used as a daily bar."""
    candidates = apply_candidate_context(deepcopy(pool), sector_histories, sectors, announcements, at)
    visible_news = event_context(news.get("items", []), at)["items"]
    quotes_by_code = {q["code"]: q for q in quotes}
    valid = [q for q in quotes if quote_fresh(q, at) and q.get("change_pct") is not None]
    moves = sorted(q["change_pct"] for q in valid)
    for c in candidates:
        q = quotes_by_code.get(c["code"], {})
        fresh = quote_fresh(q, at) and (q.get("price") or 0) > 0
        c["live_quote"] = q
        c["quote_fresh"] = fresh
        c["research_score"] = c["score"]
        c["news_events"] = [e for e in visible_news if e.get("code") == c["code"] or c["code"] in e["title"]
                            or (len(c["name"]) >= 3 and c["name"] in e["title"])]
        for e in c["news_events"]:
            if e["risk_terms"]:
                c["risks"].append("媒体风险报道待核实：" + e["title"])
        if not fresh:
            c["risks"].append("当前报价过期或缺失")
        else:
            if "ST" in q.get("name", "").upper() or "退" in q.get("name", ""):
                c["risks"].append("最新名称含风险警示或退市标记")
            if q["price"] < c["features"]["ma20"]:
                c["risks"].append("最新价格已跌破 20 日均线")
            if q["price"] > c["price"] + c["features"]["atr"]:
                c["risks"].append("最新价格偏离参考收盘超过 1 ATR，暂不追高")
            # A small, explicit experimental overlay; not a probability of success.
            if moves and q.get("change_pct") is not None:
                percentile = sum(m <= q["change_pct"] for m in moves) / len(moves)
                c["score"] = round(.9 * c["research_score"] + 10 * percentile, 1)
        c["risks"] = list(dict.fromkeys(c["risks"]))
        c["trade_eligible"] = not c["risks"]
        c["status"] = "观察 / 风险待核实" if c["risks"] else "等待盘中确认"
    return sorted(candidates, key=lambda c: (not c["trade_eligible"], -c["score"], c["code"]))


def evaluate(pool, report, quotes, at):
    sectors = storage.snapshot("sectors").get("items", [])
    names = {c["sector"] for c in pool.get("items", [])}
    histories = {s["symbol"]: storage.snapshot(f"sector-{s['symbol']}").get("items", []) for s in sectors if s["name"] in names}
    ranked = rank_live(pool.get("items", []), quotes, sectors, histories, storage.snapshot("announcements"), storage.snapshot("news"), at)
    pricing.attach(ranked, storage.snapshot("company_research").get("items", {}), at)
    ranked.sort(key=lambda c:(not c["trade_eligible"], c["price_plan"].get("status") != "ready", -c["score"], c["code"]))
    candidates = ranked[:config()["max_candidates"]]
    previous = storage.snapshot("live_candidates")
    fingerprint = hashlib.sha256(json.dumps([(c["code"], c["strategy"], c["trade_eligible"], c["risks"], c.get("price_plan", {}).get("id")) for c in candidates], ensure_ascii=False).encode()).hexdigest()
    changed = (previous.get("fingerprint") != fingerprint or previous.get("report_id") != report["id"]
               or previous.get("strategy_version") != config()["strategy_version"]
               or previous.get("selection_version") != config()["selection_version"])
    revision = iso(at) if changed else previous["revision_at"]
    version = f"{at.strftime('%Y-%m-%d-%H%M%S')}-live" if changed else previous["id"]
    old = {c["code"] for c in previous.get("candidates", [])} if previous.get("trade_date") == at.date().isoformat() else set()
    new = {c["code"] for c in candidates}
    for c in candidates:
        c.update(report_id=report["id"], candidate_version=version, strategy_version=config()["strategy_version"], selection_version=config()["selection_version"])
    payload = {"id": version, "trade_date": at.date().isoformat(), "generated_at": iso(at), "revision_at": revision,
               "report_id": report["id"], "fingerprint": fingerprint, "candidates": candidates, "pool_count": len(ranked),
               "pool_generated_at": pool.get("generated_at"), "coverage": pool.get("coverage"),
               "strategy_version": config()["strategy_version"], "selection_version": config()["selection_version"],
               "changes": {"added": sorted(new-old), "removed": sorted(old-new)} if changed else previous.get("changes", {}),
               "error": pool.get("error"),
               "method": "约 15 分钟重建前 40 技术候选池；每轮报价和新闻风险校验后重排最多 10 只。研究排序占 90%，池内当日涨幅排序占 10%；不把新闻标题直接当作上涨预测。"}
    if changed:
        path = data_dir() / "candidate_versions" / (version + ".json")
        if not path.exists():
            atomic_json(path, payload)
    storage.save_snapshot("live_candidates", payload)
    if candidates:
        storage.save_snapshot("last_visible_candidates", payload)
    return payload


def selected_today(report, ranked, quotes, positions, at):
    """Freeze first qualified basket; recheck risks without moving its price limits."""
    existing = storage.snapshot("daily_selection")
    held = {p["code"] for p in positions}
    basket = pricing.daily_selection([c for c in ranked if c["code"] not in held], report["id"], at, existing)
    if basket != existing:
        storage.save_snapshot("daily_selection", basket)
        if basket["items"]:
            atomic_json(account_path("daily_plans") / (at.date().isoformat() + "-" + config()["strategy_version"] + ".json"), basket)
    sectors = storage.snapshot("sectors").get("items", [])
    names = {c["sector"] for c in basket["items"]}
    histories = {s["symbol"]:storage.snapshot(f"sector-{s['symbol']}").get("items", []) for s in sectors if s["name"] in names}
    return rank_live(basket["items"], quotes, sectors, histories, storage.snapshot("announcements"), storage.snapshot("news"), at)


def current(report, at):
    live = storage.snapshot("live_candidates")
    if (live.get("trade_date") == at.date().isoformat() and live.get("report_id") == report.get("id")
            and live.get("strategy_version") == config()["strategy_version"] and (live.get("candidates") or not live.get("error"))):
        return live
    if live.get("error") or report.get("status") == "partial":
        previous = storage.snapshot("last_visible_candidates")
        if not previous.get("candidates"):
            for path in sorted((data_dir()/"candidate_versions").glob(at.date().isoformat()+"-*-live.json"), reverse=True):
                row = read_json(path, {})
                if row.get("candidates") and row.get("strategy_version") == config()["strategy_version"]:
                    previous = row
                    break
        if previous.get("trade_date") == at.date().isoformat() and previous.get("strategy_version") == config()["strategy_version"]:
            previous = deepcopy(previous)
            previous.update(stale=True,error="最新研究数据不可用；保留上次观察名单及其原始时间，暂停新增模拟仓位。")
            return previous
    return {}


def quote_view(code, at):
    quotes = storage.snapshot("watch_quotes")
    q = next((q for q in quotes.get("items", []) if q["code"] == code), {})
    return {**q, "fresh": not quotes.get("error") and quote_fresh(q, at), "age_seconds": round((at-dt(q["market_time"])).total_seconds()) if q.get("market_time") else None}
