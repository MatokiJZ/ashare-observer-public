from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import broker, jobs, live, providers, service, storage, accounts, reviews, learning, comparison
from .core import ROOT, TradingCalendar, config, data_dir, dt, iso, now, read_json, paper_account, account_path
from .strategy import aligned_correlations
from .context import crowding, event_context


@asynccontextmanager
async def lifespan(app):
    thread = None
    if os.getenv("ASHARE_MONITOR", "1") == "1":
        thread = threading.Thread(target=service.monitor, daemon=True)
        thread.start()
    yield
    service.STOP.set()


app = FastAPI(title="A 股观察", lifespan=lifespan)


@app.middleware("http")
async def local_access(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return JSONResponse({"detail": "仅接受本机访问"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765", "http://127.0.0.1:5173", "http://localhost:5173"}:
        return JSONResponse({"detail": "拒绝跨站操作"}, status_code=403)
    return await call_next(request)


@app.get("/api/comparison/catalog")
def comparison_catalog():
    return comparison.catalog()


@app.get("/api/comparison")
def comparison_data(ids: str, window: int = 60, refresh: bool = False):
    try:
        return comparison.compare(ids.split(","), window, refresh)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/health")
def health():
    cal = TradingCalendar()
    coverages = [storage.snapshot(n).get("coverage", {}) for n in ["latest_report", "candidate_pool"]]
    coverage = max(coverages, key=lambda c: c.get("checked_at", ""))
    return {"ok": True, "time": iso(), "calendar_valid_through": cal.valid_through,
            "credential_mode": "dedicated" if os.getenv("ASHARE_GITHUB_TOKEN") or (Path.home() / ".config/ashare-observer/github-token").exists() else "existing_login",
            "calendar_known": cal.known(now().date()), "github_repo": config()["github_repo"],
            "monitor": storage.snapshot("monitor_status"), "sync": storage.snapshot("sync_status"), "cloud": storage.snapshot("cloud_status"),
            "run": storage.snapshot("run_status"), "probe": storage.snapshot("probe"),
            "history": storage.snapshot("history_refresh"), "coverage": coverage,
            "busy": service.LOCK.locked(), "sources": {k: {key: storage.snapshot(k).get(key) for key in ["fetched_at", "last_attempt", "error", "source", "delay_status"]} for k in ["markets", "universe", "sectors", "news"]}}


@app.get("/api/overview")
def overview():
    report = storage.snapshot("latest_report")
    markets = storage.snapshot("markets") or report.get("markets", {})
    index_history = storage.snapshot("index_histories").get("items", {})
    if not index_history.get("sh000300"):
        index_history["sh000300"] = storage.snapshot("index_history").get("items", [])
    samples = storage.snapshot("market_samples").get("items", {})
    for q in markets.get("items", []):
        if q["symbol"] in index_history:
            q["history"] = index_history[q["symbol"]]
        q["intraday"] = samples.get(q["symbol"], [])
        if q.get("market") == "A股":
            from .core import in_session
            cal = TradingCalendar()
            q["session_state"] = "open" if cal.known(now().date()) and cal.is_session(now().date()) and in_session(now()) else "closed"
    universe = storage.snapshot("universe").get("items", [])
    available = [r for r in universe if r.get("change_pct") is not None and (r.get("amount") or 0) > 0]
    stats = {"count": len(available), "advancing": sum(r["change_pct"] > 0 for r in available), "declining": sum(r["change_pct"] < 0 for r in available), "amount": sum(r.get("amount") or 0 for r in available)}
    live = storage.snapshot("live_regime")
    if not live.get("asof") or (now() - dt(live["asof"])).total_seconds() > 180:
        live = None
    return {"markets": markets, "report": report, "live_regime": live, "crowding": crowding(available), "stats": stats, "intraday": storage.snapshot("intraday"), "health": health()}


@app.get("/api/sectors")
def sectors():
    result = storage.snapshot("sectors")
    result["growth_note"] = "行情涨跌不等于经营增长。以下财务汇总仅含已验证披露日期的覆盖样本，不能代表全行业。"
    meta = storage.snapshot("metadata").get("items", {})
    finance = storage.snapshot("financials").get("items", {})
    growth = {}
    for code, info in finance.items():
        reports = info.get("reports", [])
        if reports and code in meta:
            sector = meta[code].get("sector", "未分类")
            growth.setdefault(sector, []).append({"code": code, **reports[0]})
    result["growth"] = growth
    return result


@app.get("/api/sectors/{symbol}/history")
def sector_history(symbol: str):
    import re
    if not re.fullmatch(r"BK\d+", symbol):
        raise HTTPException(400, "无效行业代码")
    cached = storage.snapshot(f"sector-{symbol}")
    if cached.get("fetched_at", "")[:10] == now().date().isoformat():
        return cached
    return jobs.collect("sector_history", symbol, target=f"sector-{symbol}", timeout=30)


@app.get("/api/candidates")
def candidates():
    report = storage.snapshot("latest_report")
    at = now()
    selection = live.current(report, at)
    basket = storage.snapshot("daily_selection")
    basket["current"] = basket.get("trade_date") == at.date().isoformat()
    codes = {c["code"] for c in report.get("candidates", []) + selection.get("candidates", []) + basket.get("items", [])}
    intraday = storage.snapshot("intraday")
    intraday["fresh"] = bool(intraday.get("fetched_at") and 0 <= (at-dt(intraday["fetched_at"])).total_seconds() <= 180)
    return {"report": report, "live": selection, "daily_selection":basket, "intraday": intraday,
            "quotes": {code:live.quote_view(code, at) for code in codes}}


@app.get("/api/candidate-versions")
def candidate_versions():
    paths = sorted((data_dir() / "candidate_versions").glob("*-live.json"), reverse=True)[:200]
    return [{k: read_json(p).get(k) for k in ["id", "trade_date", "generated_at", "changes"]} for p in paths]


@app.get("/api/candidate-versions/{version}")
def candidate_version(version: str):
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}-live", version):
        raise HTTPException(400, "候选版本标识无效")
    value = read_json(data_dir() / "candidate_versions" / (version + ".json"))
    if value is None:
        raise HTTPException(404, "未找到候选版本")
    return value


@app.get("/api/stocks/{code}")
def stock(code: str):
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "请输入六位股票代码")
    daily, minute = storage.bars(code), storage.bars(code, "5min")
    report = storage.snapshot("latest_report")
    selected = live.current(report, now()).get("candidates", []) or report.get("candidates", [])
    basket = storage.snapshot("daily_selection")
    planned = next((c for c in basket.get("items", []) if c["code"] == code), None) if basket.get("trade_date") == now().date().isoformat() else None
    return {"code": code, "daily": providers.clean(daily.tail(260).to_dict("records")), "minutes": providers.clean(minute.tail(240).to_dict("records")),
            "metadata": storage.snapshot("metadata").get("items", {}).get(code, {}), "financials": storage.snapshot("financials").get("items", {}).get(code, {}),
            "candidate": next((c for c in selected if c["code"] == code), planned), "daily_candidate":planned, "quote": live.quote_view(code, now()),
            "signal": next((s for s in storage.snapshot("intraday").get("items", []) if s["code"] == code), None)}


@app.get("/api/linkages")
def linkages():
    markets = storage.snapshot("markets").get("items", [])
    histories = {m["symbol"]: m.get("history", []) for m in markets}
    histories["sh000300"] = storage.snapshot("index_history").get("items", [])
    mappings = [{"foreign": "SOXX", "label": "美国半导体", "terms": ["半导体"]},
                {"foreign": "XLF", "label": "美国金融", "terms": ["银行", "保险", "证券"]},
                {"foreign": "XLE", "label": "美国能源", "terms": ["煤炭", "油气", "炼化"]},
                {"foreign": "XLK", "label": "美国科技", "terms": ["软件开发", "消费电子"]},
                {"foreign": "^N225", "label": "日经 225", "terms": ["汽车零部件", "消费电子"]},
                {"foreign": "^KS11", "label": "KOSPI", "terms": ["半导体", "元件"]}]
    industry = []
    for mapping in mappings:
        for sector in storage.snapshot("sectors").get("items", []):
            if not any(word in sector["name"] for word in mapping["terms"]):
                continue
            history = storage.snapshot(f"sector-{sector['symbol']}").get("items", [])
            anchor = [{"time": p["date"] + "T15:00:00+08:00", "close": p["close"]} for p in history]
            if anchor:
                for item in aligned_correlations({"industry": anchor, mapping["foreign"]: histories.get(mapping["foreign"], [])}, "industry"):
                    industry.append({**item, "sector": sector["name"], "foreign_name": mapping["label"]})
    return {"histories": histories, "markets": [{"symbol": m["symbol"], "name": m["name"], "market": m.get("market")} for m in markets],
            "correlations": aligned_correlations(histories, "sh000300"),
            "industry_correlations": industry,
            "method": "截至每个 A 股收盘时已可见的最近海外收益；节假日不重复计入；样本不足不显示相关系数。",
            "industry_mapping": [{"foreign": "SOXX · 美国半导体", "domestic": ["半导体", "半导体设备", "半导体材料"], "basis": "产业链研究假设，尚未完成样本外验证"}]}


@app.get("/api/events")
def events():
    result = storage.snapshot("news")
    result.update(event_context(result.get("items", []) + storage.snapshot("announcements").get("items", []), now()))
    return result


@app.get("/api/portfolio")
def portfolio():
    views = accounts.all_views()
    return {**views[0], "accounts":views, "monitor": storage.snapshot("monitor_status")}


class Toggle(BaseModel):
    enabled: bool


@app.post("/api/portfolio/enabled")
def set_enabled(body: Toggle, account: str = "baseline"):
    if account not in {"baseline","experiment"}:
        raise HTTPException(400,"未知账户")
    with paper_account(account):
        broker.toggle(body.enabled)
        return broker.view()


@app.get("/api/reviews")
def review_list():
    return reviews.listing()


@app.get("/api/reviews/{day}")
def review_day(day: str):
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",day):
        raise HTTPException(400,"复盘日期无效")
    value = read_json(data_dir()/"reviews"/(day+".json"))
    if value is None:
        return {"trade_date":day,"items":[],"summary":{},"execution":[],"status":"pending", "method":reviews.METHOD}
    return value


@app.get("/api/models")
def models():
    return learning.status()


@app.get("/api/alerts")
def alerts():
    return storage.records("alerts", 100)


@app.get("/api/reports")
def reports():
    paths = sorted((data_dir() / "reports").glob("*.json"), reverse=True)
    return [{k: read_json(p).get(k) for k in ["id", "trade_date", "generated_at", "kind", "mode", "status", "coverage"]} for p in paths[:200]]


@app.get("/api/reports/{report_id}")
def report(report_id: str):
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}-[a-z]+", report_id):
        raise HTTPException(400, "报告标识无效")
    result = read_json(data_dir() / "reports" / (report_id + ".json"))
    if result is None:
        raise HTTPException(404, "未找到报告")
    return result


@app.get("/api/research-snapshot")
def research():
    body = {"generated_at": iso(), "report": storage.snapshot("latest_report"), "live_candidates": storage.snapshot("live_candidates"), "daily_selection":storage.snapshot("daily_selection"), "intraday": storage.snapshot("intraday"), "portfolio": broker.view(), "accounts":accounts.all_views(),"reviews":reviews.listing(),"models":learning.status(), "health": health()}
    return Response(json.dumps(body, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="research-snapshot.json"'})


@app.get("/api/paper-audit")
def paper_audit(account: str = "baseline"):
    if account not in {"baseline","experiment"}:
        raise HTTPException(400,"未知账户")
    with paper_account(account):
        return _paper_audit()


def _paper_audit():
    with storage.database() as con:
        con.execute("BEGIN")
        trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY ts,id")]
        contexts = {r["id"]:json.loads(r["payload"]) for r in con.execute("SELECT * FROM trade_context")}
        equity = [dict(r) for r in con.execute("SELECT * FROM equity ORDER BY ts")]
        signals = [json.loads(r["payload"]) for r in con.execute("SELECT * FROM signals ORDER BY ts")]
    body = {"generated_at":iso(), "trades":trades, "contexts":contexts, "equity":equity, "signals":signals,
            "daily_plans":[read_json(p) for p in sorted(account_path("daily_plans").glob("*.json"))],
            "note":"只记录在线可见行情；参数变更须另建版本并独立验证。"}
    return Response(json.dumps(body,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":'attachment; filename="paper-audit.json"'})


@app.post("/api/actions/{action}")
def action(action: str, tasks: BackgroundTasks):
    operations = {"refresh": lambda: service.refresh_cycle(full=True), "sync": service.sync, "report": service.run_local_report}
    if action not in operations:
        raise HTTPException(404, "未知操作")
    if service.LOCK.locked():
        return {"state": "busy", "message": "已有任务运行中"}
    tasks.add_task(operations[action])
    return {"state": "queued"}


dist = ROOT / "frontend" / "dist"
if (dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}")
def ui(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "未找到接口")
    if not (dist / "index.html").exists():
        return JSONResponse({"message": "请先构建本地网站：cd frontend && npm run build"}, status_code=503)
    return FileResponse(dist / "index.html")
