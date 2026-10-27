from __future__ import annotations

import json
import os
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import timedelta

import pandas as pd

from . import providers, storage
from .core import ROOT, TradingCalendar, atomic_json, config, data_dir, digest, dt, iso, now, read_json
from .strategy import mainboard, market_regime, screen
from .context import apply_candidate_context, crowding, event_context, overseas_overlay


def collect(operation: str, *args, target: str | None = None, timeout=75):
    name = target or operation
    try:
        result = providers.call(operation, *args, timeout=timeout)
        if isinstance(result, list):
            result = {"items": result, "fetched_at": iso()}
        result["last_attempt"] = iso()
        result["error"] = None
        if name == "markets":
            samples = storage.snapshot("market_samples").get("items", {})
            for quote in result.get("items", []):
                if quote.get("market_time") and quote.get("price"):
                    points = samples.get(quote["symbol"], [])
                    keyed = {r["time"]: r for r in points}
                    keyed[quote["market_time"]] = {"time": quote["market_time"], "close": quote["price"]}
                    samples[quote["symbol"]] = sorted(keyed.values(), key=lambda r: r["time"])[-1500:]
            storage.save_snapshot("market_samples", {"items": samples, "fetched_at": iso()})
        if name in {"news", "announcements"}:
            if name == "announcements":
                result["coverage"] = {**storage.snapshot(name).get("coverage", {}), **result.get("coverage", {})}
            previous = {r.get("url") or r["title"]: r for r in storage.snapshot(name).get("items", [])}
            for row in result.get("items", []):
                key = row.get("url") or row["title"]
                if key in previous:
                    row["first_seen_at"] = previous[key]["first_seen_at"]
                previous[key] = row
            result["items"] = sorted(previous.values(), key=lambda r: r.get("published_at") or "", reverse=True)[:500]
        storage.save_snapshot(name, result)
        return result
    except Exception as exc:
        old = storage.snapshot(name)
        old.update(error=str(exc)[:300], last_attempt=iso())
        storage.save_snapshot(name, old)
        return old


def warm_histories(rows: list[dict], deadline: float) -> dict:
    """Resumable backfill. Retry old failures fairly, and keep progress on disk."""
    cfg = config()
    eligible = [r for r in rows if mainboard(str(r.get("code", ""))) and r.get("price") and (r.get("amount") or 0) > 0 and "ST" not in r.get("name", "").upper() and "退" not in r.get("name", "")]
    at = now()
    calendar = TradingCalendar()
    expected = calendar.completed(at)
    calendar_end = bisect_right(calendar.days, expected)
    histories = storage.bars_many([r["code"] for r in eligible])
    checks = storage.snapshot("history_checks").get("items", {})
    pending = []
    deferred = 0
    for row in eligible:
        code = row["code"]
        f = histories.get(code, pd.DataFrame())
        # Legacy files could contain a morning snapshot labelled as a future
        # final bar. Remove it now; it must never become valid merely with time.
        if not f.empty and "available_at" in f:
            visible = f[pd.to_datetime(f.available_at, utc=True, errors="coerce") <= at]
            if len(visible) != len(f):
                storage.save_bars(code, visible, replace=True)
                f = visible
        last = str(f.date.max())[:10] if not f.empty else ""
        check = checks.get(code, {})
        required = ["open", "close", "high", "low", "volume", "amount"]
        valid = not f.empty and set(required).issubset(f) and not f[required].tail(120).isna().any().any()
        secondary = f.get("source", pd.Series("", index=f.index)).fillna("").str.contains("sina").tail(60)
        action_flags = f.get("corporate_action_checked", pd.Series(None, index=f.index, dtype=object)).tail(60)
        unchecked = bool((secondary & ~action_flags.eq(True)).any())
        first_day = str(f.date.iloc[-120])[:10] if len(f) >= 120 else expected
        recent_days = calendar.days[bisect_left(calendar.days, first_day):calendar_end]
        gaps = bool(set(recent_days) - set(f.date.astype(str).str[:10])) if not f.empty else True
        verified = check.get("expected_date") == expected and check.get("state") == "success" and not unchecked
        needs = last != expected or not valid or len(f) < 120 or gaps or unchecked
        extension_only = not needs
        # Once daily freshness is restored, extend history for time-separated learning.
        history_target = max(cfg.get("fees_effective_from","2023-08-28"), (at - timedelta(days=cfg.get("history_days",650))).date().isoformat())
        extend = (not f.empty and str(f.date.min()) > history_target and
                  checks.get(code,{}).get("history_target") != history_target[:7])
        needs = needs or extend
        if needs and (not verified or extend):
            # Failures cool down for 15 minutes, then rejoin behind never-tried codes.
            if check.get("state") == "failed" and check.get("attempted_at") and (at-dt(check["attempted_at"])).total_seconds() < 900:
                deferred += 1
                continue
            only_actions = unchecked and not extend and last == expected and valid and len(f) >= 120 and (not gaps or check.get("expected_date") == expected)
            pending.append({**row, "only_actions": only_actions, "history_target":history_target[:7],"extension_only":extension_only})
    pending.sort(key=lambda r: (r["extension_only"], checks.get(r["code"], {}).get("attempted_at", ""), -(r.get("amount") or 0)))
    selected = pending[:cfg["history_batch_limit"]]
    status = {"state": "running", "started_at": iso(at), "expected_date": expected,
              "eligible": len(eligible), "queued": len(pending), "refreshed": 0, "attempted": 0, "errors": [], "sources": {}}
    storage.save_snapshot("history_refresh", status)
    # Probe one symbol once. A working fallback becomes first choice for the
    # rest of this batch, rather than paying a dead-source timeout for every stock.
    preferred = storage.snapshot("history_checks").get("preferred", "eastmoney")
    def fetch(row):
        remaining = deadline - time.monotonic()
        if remaining < 2:
            return row["code"], None, None
        try:
            if row.get("only_actions"):
                # This adapter is plain JSON over a bounded HTTP request; no
                # native decoder or child interpreter is necessary.
                actions = providers.corporate_actions(row["code"])
                return row["code"], {"actions": actions, "source": actions["source"]}, None
            return row["code"], providers.call("history", row["code"], preferred, timeout=min(35, remaining)), None
        except Exception as exc:
            return row["code"], None, str(exc)[:200]
    def record(result):
        nonlocal preferred
        code, value, error = result
        if value is None and error is None:
            return
        status["attempted"] += 1
        check = {"attempted_at": iso(), "expected_date": expected, "state": "failed" if error else "success"}
        if value:
            if "actions" in value:
                frame = storage.bars(code)
                frame["corporate_action_checked"] = True
                frame["corporate_action_event"] = frame.date.isin(value["actions"]["dates"])
            else:
                frame = pd.DataFrame(value["bars"])
            storage.save_bars(code, frame)
            status["refreshed"] += 1
            source = value["source"]
            status["sources"][source] = status["sources"].get(source, 0) + 1
            preferred = "sina" if "sina" in source else "eastmoney"
            check.update(source=source, last_date=str(frame.date.max())[:10], rows=len(frame), history_target=history_target[:7])
        else:
            check["error"] = error
            if len(status["errors"]) < 20:
                status["errors"].append({"code": code, "error": error})
        checks[code] = check
        if status["attempted"] % 25 == 0:
            storage.save_snapshot("history_refresh", {**status, "updated_at": iso()})
            storage.save_snapshot("history_checks", {"items": checks, "preferred": preferred, "fetched_at": iso()})
    if selected:
        record(fetch(selected[0]))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(fetch, row) for row in selected[1:]]):
            record(future.result())
    remaining = max(0, len(pending)-status["refreshed"]) + deferred
    status.update(state="partial" if remaining else "success", pending=remaining, deferred=deferred, finished_at=iso())
    storage.save_snapshot("history_checks", {"items": checks, "preferred": preferred, "fetched_at": iso()})
    storage.save_snapshot("history_refresh", status)
    return status


def close_increment(rows: list[dict]) -> dict:
    """Timestamped closing quotes update all existing histories in one bounded batch."""
    codes = [r["code"] for r in rows if mainboard(r["code"])]
    try:
        result = providers.call("daily_quotes", codes, timeout=65)
    except Exception as exc:
        return {"updated": 0, "error": str(exc)[:180]}
    count = 0
    for q in result["items"]:
        stamp = dt(q["market_time"]) if q.get("market_time") else None
        code = q["code"]
        if not stamp or stamp.date() != now().date() or stamp.hour < 15 or not (data_dir() / "bars/day" / f"{code}.parquet").exists():
            continue
        if not all(q.get(k) and q[k] > 0 for k in ["open", "high", "low", "price", "volume", "amount"]):
            continue
        storage.save_bars(code, pd.DataFrame([{"date": stamp.date().isoformat(), "open": q["open"], "close": q["price"], "high": q["high"], "low": q["low"],
            "volume": q["volume"] * 100, "amount": q["amount"], "turnover": q.get("turnover"), "change_pct": q.get("change_pct"), "available_at": iso()}]))
        count += 1
    return {"updated": count, "errors": result.get("errors", [])}


def enrich(candidates: list[dict], deadline: float):
    meta = storage.snapshot("metadata").get("items", {})
    finance = storage.snapshot("financials").get("items", {})
    tasks = []
    for candidate in candidates:
        code = candidate["code"]
        if code not in meta or (now() - dt(meta[code]["fetched_at"])).days >= 7:
            tasks.append(("metadata", code))
        if code not in finance or (now() - dt(finance[code]["fetched_at"])).days >= 1:
            tasks.append(("financials", code))
    def fetch(pair):
        op, code = pair
        if time.monotonic() >= deadline:
            return op, code, None
        try:
            return op, code, providers.call(op, code, timeout=30)
        except Exception:
            return op, code, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for op, code, value in pool.map(fetch, tasks):
            if value:
                (meta if op == "metadata" else finance)[code] = value
    storage.save_snapshot("metadata", {"items": meta, "fetched_at": iso()})
    storage.save_snapshot("financials", {"items": finance, "fetched_at": iso()})
    return meta, finance


def collect_index_histories():
    previous = storage.snapshot("index_histories")
    cal = TradingCalendar()
    at = now()
    expected = cal.completed(at) if cal.known(at.date()) else ""
    benchmark = previous.get("items",{}).get("sh000300",[])
    latest_benchmark = max((r.get("date",r.get("time",""))[:10] for r in benchmark),default="")
    if previous.get("fetched_at", "")[:10] == at.date().isoformat() and len(previous.get("items", {})) >= 6 and latest_benchmark >= expected:
        return previous
    items = previous.get("items", {})
    def fetch(code):
        try:
            return code, providers.call("index_history", code, timeout=30)
        except Exception:
            return code, None
    with ThreadPoolExecutor(max_workers=3) as pool:
        for code, result in pool.map(fetch, ["sh000001", "sz399001", "sh000300", "sh000905", "sh000852", "sz399006"]):
            if result:
                items[code] = result["items"]
    payload = {"items": items, "fetched_at": iso()}
    storage.save_snapshot("index_histories", payload)
    if items.get("sh000300"):
        storage.save_snapshot("index_history", {"items": items["sh000300"], "fetched_at": iso()})
    return payload


def enrich_context(candidates, deadline, limit=None):
    sectors = storage.snapshot("sectors").get("items", [])
    names = {c["sector"] for c in candidates}
    symbols = [r["symbol"] for r in sectors if r["name"] in names]
    def fetch(symbol):
        cached = storage.snapshot(f"sector-{symbol}")
        if cached.get("fetched_at", "")[:10] != now().date().isoformat() and time.monotonic() < deadline:
            cached = collect("sector_history", symbol, target=f"sector-{symbol}", timeout=20)
        return symbol, cached.get("items", [])
    with ThreadPoolExecutor(max_workers=4) as pool:
        histories = dict(pool.map(fetch, symbols))
    disclosures = collect("announcements", [c["code"] for c in candidates], timeout=40) if time.monotonic() < deadline else storage.snapshot("announcements")
    return apply_candidate_context(candidates, histories, sectors, disclosures, now())[:limit or config()["max_candidates"]]


def build_report(mode="update", warm=True, budget_seconds=540) -> dict:
    started = now()
    deadline = time.monotonic() + budget_seconds
    status = {"state": "running", "started_at": iso(started), "mode": mode}
    storage.save_snapshot("run_status", status)
    calendar_payload = collect("calendar", timeout=40)
    calendar = TradingCalendar(calendar_payload)
    if not calendar.known(started.date()):
        status.update(state="failed", error="交易日历未覆盖今天，不能生成选股", finished_at=iso())
        storage.save_snapshot("run_status", status)
        return status
    if not calendar.is_session(started.date()):
        status.update(state="skipped", reason="A 股休市日", finished_at=iso())
        storage.save_snapshot("run_status", status)
        return status
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(collect, ["universe", "markets", "sectors"]))
    universe = storage.snapshot("universe")
    rows = universe.get("items", [])
    # Exclude delisted and non-trading symbols from the current universe; do not use it for unbiased historical tests.
    rows = [r for r in rows if r.get("price") and (r.get("amount") or 0) > 0]
    incremental = close_increment(rows) if now().hour >= 16 else {"updated": 0}
    warming = warm_histories(rows, deadline - 190) if warm else {"refreshed": 0}
    warming["closing_increment"] = incremental
    histories = storage.bars_many([r["code"] for r in rows if mainboard(r["code"])])
    at = now().replace(microsecond=0)
    candidates, coverage = screen(rows, histories, {}, {}, calendar, at, limit=40)
    meta, finance = enrich(candidates, deadline - 100)
    candidates, coverage = screen(rows, histories, meta, finance, calendar, at, limit=40)
    research_pool = enrich_context(candidates, deadline - 75, limit=40)
    from . import pricing, research_data
    evidence = research_data.enrich(research_pool, deadline - 15)
    candidates = research_pool[:config()["max_candidates"]]
    regime = market_regime(universe, coverage["ratio"], coverage.pop("features"), at)
    if time.monotonic() < deadline - 30:
        collect("news", ["国际政策", "美联储", "日本央行", "A股"] + [c["code"] for c in candidates[:2]], timeout=45)
    index_data = storage.snapshot("index_histories")
    if time.monotonic() < deadline - 10 and (len(index_data.get("items", {})) < 6 or index_data.get("fetched_at", "")[:10] != at.date().isoformat()):
        collect_index_histories()
    # The report becomes observable only after all included source reads finish.
    at = now().replace(microsecond=0)
    pricing.attach(research_pool, evidence, at)
    regime = overseas_overlay(regime, storage.snapshot("markets"), at)
    events = event_context(storage.snapshot("news").get("items", []) + storage.snapshot("announcements").get("items", []), at)
    sources = {k: {key: storage.snapshot(k).get(key) for key in ["source", "fetched_at", "last_attempt", "error", "delay_status"]} for k in ["calendar", "universe", "markets", "sectors", "news"]}
    kind = "closing" if mode == "close" else ("intraday_supplement" if at.hour * 60 + at.minute >= 9 * 60 + 30 else "premarket")
    excluded = coverage.pop("excluded", [])
    report = {"schema_version": 1, "strategy_version": config()["strategy_version"], "trade_date": at.date().isoformat(),
              "code_revision": os.getenv("GITHUB_SHA") or ((ROOT / ".revision").read_text().strip() if (ROOT / ".revision").exists() else "workspace"),
              "generated_at": iso(at), "started_at": iso(started), "kind": kind, "mode": mode,
              "status": "complete" if coverage["ratio"] >= 0.9 and not universe.get("error") else "partial", "coverage": coverage, "warming": warming,
              "data_cutoff": coverage["expected_date"] if coverage["covered"] else None,
              "regime": regime, "candidates": candidates, "research_pool": research_pool, "excluded":excluded, "sources": sources, "crowding": crowding(rows),
              "markets": storage.snapshot("markets"), "sectors": storage.snapshot("sectors"),
              "events": events["items"][:100],
              "limitations": ["观察名单不等于买入指令", "免费行情的真实延迟逐项标记", "财报与新闻未实现完整官方源覆盖", "当前股票池不用于宣称无幸存者偏差的历史业绩", "跨市场关系是相关性，非因果结论"]}
    report["id"] = f"{report['trade_date']}-{at.strftime('%H%M%S')}-{mode}"
    path = data_dir() / "reports" / (report["id"] + ".json")
    if path.exists():
        return read_json(path)
    atomic_json(path, report)
    previous = storage.snapshot("latest_report")
    if (report["trade_date"], report["generated_at"]) > (previous.get("trade_date", ""), previous.get("generated_at", "")):
        storage.save_snapshot("latest_report", report)
    status.update(state="success" if report["status"] == "complete" else "partial", finished_at=iso(), report_id=report["id"], coverage=coverage)
    storage.save_snapshot("run_status", status)
    return report


def probe() -> dict:
    results = {}
    for operation, args in [("calendar", []), ("quotes", [["sh600519"]]), ("history", ["600519"]), ("history", ["600519", "sina"]), ("markets", []), ("financials", ["600519"])]:
        label = "history_sina" if operation == "history" and len(args) > 1 else operation
        start = time.monotonic()
        try:
            data = providers.call(operation, *args, timeout=65)
            results[label] = {"ok": True, "elapsed": round(time.monotonic() - start, 2), "empty": not bool(data),
                                  "source": data.get("source") if isinstance(data, dict) else None}
            if operation == "history":
                results[label].update(rows=len(data["bars"]), last_date=data["bars"][-1]["date"], action_checked=data["bars"][-1].get("corporate_action_checked"), fallback_errors=data.get("fallback_errors", []))
                if label == "history_sina" and "sina" not in data.get("source", ""):
                    results[label].update(ok=False, error="新浪本次不可用，实际由备用来源返回数据")
        except Exception as exc:
            results[label] = {"ok": False, "elapsed": round(time.monotonic() - start, 2), "error": str(exc)[:250]}
    storage.save_snapshot("probe", {"checked_at": iso(), "checks": results})
    return results
