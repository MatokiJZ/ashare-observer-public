from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import broker, jobs, live, providers, storage, pricing, accounts, reviews, learning
from .cloud import sync_reports
from .core import TradingCalendar, config, dt, in_session, iso, now, paper_account
from .context import overseas_overlay

LOCK = threading.Lock()
STOP = threading.Event()
HISTORY_LOCK = threading.Lock()
RESEARCH_LOCK = threading.Lock()


def maintain_research():
    if not RESEARCH_LOCK.acquire(blocking=False):
        return
    try:
        reviews.rebuild()
        at = now()
        expected = TradingCalendar().completed(at)
        if storage.snapshot("learning_features").get("asof") != expected:
            learning.dataset(at)
        learning.evaluate_promotion(at)
    except Exception as exc:
        storage.save_snapshot("review_maintenance", {"state":"failed", "finished_at":iso(), "error":str(exc)[:250]})
    else:
        storage.save_snapshot("review_maintenance", {"state":"success", "finished_at":iso()})
    finally:
        RESEARCH_LOCK.release()


def schedule_research():
    last = storage.snapshot("review_maintenance").get("finished_at")
    if not RESEARCH_LOCK.locked() and (not last or (now()-dt(last)).total_seconds() >= 900):
        threading.Thread(target=maintain_research, daemon=True).start()


def maintain_histories():
    """Independent history I/O never blocks live quotes or paper exit checks."""
    if not HISTORY_LOCK.acquire(blocking=False):
        return
    try:
        jobs.warm_histories(storage.snapshot("universe").get("items", []), time.monotonic() + 180)
    except Exception as exc:
        storage.save_snapshot("history_refresh", {"state": "failed", "finished_at": iso(), "error": str(exc)[:250]})
    finally:
        HISTORY_LOCK.release()


def schedule_history_maintenance():
    if HISTORY_LOCK.locked():
        return
    cal = TradingCalendar()
    at = now()
    if not cal.known(at.date()):
        return
    previous = storage.snapshot("history_refresh")
    interval = 900 if previous.get("pending", 1) else 3600
    last = previous.get("finished_at")
    if not previous.get("eligible") or previous.get("expected_date") != cal.completed(at) or not last or (at-dt(last)).total_seconds() >= interval:
        threading.Thread(target=maintain_histories, daemon=True).start()


def current_regime(report, pool, current, at):
    from .strategy import live_regime
    baseline = pool.get("regime", report.get("regime", {"state": "halt", "exposure": 0})) if current else {"state": "halt", "exposure": 0}
    regime = live_regime(storage.snapshot("universe"), baseline, at)
    regime = overseas_overlay(regime, storage.snapshot("markets"), at)
    if current and pool.get("coverage", {}).get("ratio", 0) < .9:
        regime.update(state="halt", exposure=0, label="暂停新增", reason="最新候选池日线覆盖不足或全市场快照过期")
    return regime


def refresh_cycle(full=False):
    if not LOCK.acquire(blocking=False):
        return {"state": "busy"}
    try:
        universe = storage.snapshot("universe")
        full = full or not universe.get("fetched_at") or (now()-dt(universe["fetched_at"])).total_seconds() >= config()["universe_poll_seconds"]
        if full:
            jobs.collect("calendar", timeout=40)
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = [pool.submit(jobs.collect, op) for op in (["markets", "universe", "sectors"] if full else ["markets"])]
            if full:
                tasks.append(pool.submit(jobs.collect_index_histories))
            for task in tasks:
                task.result()
        calendar = TradingCalendar()
        schedule_history_maintenance()
        schedule_research()
        at = now().replace(microsecond=0)
        report = storage.snapshot("latest_report")
        candidates = report.get("candidates", [])
        today_report = report.get("trade_date") == at.date().isoformat()
        can_trade = calendar.known(at.date()) and calendar.is_session(at.date()) and in_session(at)
        signals = []
        if can_trade:
            account = broker.account()
            pair = learning.shadow_accounts(at)
            account_specs = [("experiment",learning.active_model())]
            if pair:
                account_specs += [(pair["rules"],None),(pair["model"],pair["model_id"])]
            positions = list(account["positions"])
            other_pending, extra_frozen = [], []
            for name,_ in account_specs:
                with paper_account(name):
                    other = broker.account()
                    positions += other["positions"]
                    other_pending += other["pending"]
                    b = storage.snapshot("daily_selection")
                    if b.get("trade_date") == at.date().isoformat():
                        extra_frozen += b.get("items",[])
            frozen = storage.snapshot("daily_selection")
            frozen_items = frozen.get("items", []) if frozen.get("trade_date") == at.date().isoformat() else []
            pool = live.ensure_pool(report, calendar, at) if today_report else {"items": []}
            storage.save_snapshot("live_regime", current_regime(report, pool, today_report, now()))
            news = storage.snapshot("news")
            if not news.get("last_attempt") or (now() - dt(news["last_attempt"])).total_seconds() >= config()["news_poll_seconds"]:
                jobs.collect("news", ["国际政策", "美联储", "日本央行", "A股"] + [c["code"] for c in pool["items"][:2]], timeout=45)
                tracked = pool["items"] + positions + frozen_items
                if tracked:
                    jobs.collect("announcements", sorted({c["code"] for c in tracked}), timeout=40)
            codes = sorted({c["code"] for c in pool["items"] + positions + account["pending"] + other_pending + frozen_items + extra_frozen})
            if codes:
                quotes = jobs.collect("quotes", [providers.tx_symbol(c) for c in codes], target="watch_quotes", timeout=25)
                if today_report:
                    selection = live.evaluate(pool, report, [] if quotes.get("error") else quotes.get("items", []), now())
                    candidates = selection["candidates"]
                    selected = live.selected_today(report, candidates, [] if quotes.get("error") else quotes.get("items", []), account["positions"], now()) if pool.get("coverage", {}).get("ratio", 0) >= .9 else []
                else:
                    candidates = []
                    selected = []
                codes = sorted({c["code"] for c in candidates + selected + positions + account["pending"]})
                frames = {code:storage.bars(code, "5min") for code in codes}
                at = now().replace(microsecond=0)
                if today_report:
                    quote_map = {q["code"]:q for q in quotes.get("items", [])} if not quotes.get("error") else {}
                    signals = [pricing.price_signal(c, quote_map.get(c["code"], {}), at) for c in selected]
                selected_codes = {c["code"] for c in selected}
                signals += [{"code": o["code"], "state": "invalid", "label": "已退出当前候选名单"} for o in account["pending"]
                            if o.get("side", "buy") == "buy" and o["code"] not in selected_codes]
                regime = current_regime(report, pool, today_report, at)
                storage.save_snapshot("live_regime", regime)
                events = storage.snapshot("announcements").get("items", []) + storage.snapshot("news").get("items", [])
                broker.tick([] if quotes.get("error") else quotes.get("items", []), signals, regime, calendar, at, frames, events)
                reviews.record_execution(selected,signals,regime,quotes.get("items",[]) if not quotes.get("error") else [],at)
                for name,model_id in account_specs:
                    with paper_account(name):
                        other = broker.account()
                        other_regime = accounts.experimental_regime(pool,storage.snapshot("universe"),at,today_report)
                        ranked = accounts.eligible_candidates(live.rank_live(pool.get("items",[]),
                            quotes.get("items",[]) if not quotes.get("error") else [], storage.snapshot("sectors").get("items",[]),
                            {s["symbol"]:storage.snapshot(f"sector-{s['symbol']}").get("items",[]) for s in storage.snapshot("sectors").get("items",[])},
                            storage.snapshot("announcements"),storage.snapshot("news"),at),other_regime)
                        try:
                            ranked = learning.rank(ranked,model_id,at)
                        except Exception as exc:
                            ranked = []
                            other_regime.update(state="halt",exposure=0,data_ready=False,reason=str(exc)[:200])
                        chosen = live.selected_today(report,ranked,quotes.get("items",[]),other["positions"],at) if today_report and pool.get("coverage",{}).get("ratio",0) >= .9 else []
                        # Recheck frozen choices against current risk/model inputs without replacing frozen price/exit plans.
                        checks = {c["code"]:c for c in ranked}
                        for c in chosen:
                            check = checks.get(c["code"])
                            if not check or not check.get("trade_eligible"):
                                c["trade_eligible"] = False
                                c.setdefault("risks",[]).append("最新候选风险检查未通过")
                        other_signals = [pricing.price_signal(c,quote_map.get(c["code"],{}),at) for c in chosen] if today_report else []
                        codes_chosen = {c["code"] for c in chosen}
                        other_signals += [{"code":o["code"],"state":"invalid","label":"当前账户名单已失效"} for o in other["pending"] if o.get("side","buy")=="buy" and o["code"] not in codes_chosen]
                        # Model-ranked picks can be outside the baseline top ten.
                        # Collect their completed minute bars with the same cadence.
                        extra_codes = {c["code"] for c in chosen + other["positions"] + other["pending"]}
                        for code in extra_codes-set(frames):
                            frames[code] = storage.bars(code, "5min")
                        codes = sorted(set(codes) | extra_codes)
                        broker.tick([] if quotes.get("error") else quotes.get("items",[]),other_signals,other_regime,calendar,at,frames,events)
                        reviews.record_execution(chosen,other_signals,other_regime,quotes.get("items",[]) if not quotes.get("error") else [],at)
                        storage.save_snapshot("intraday",{"items":other_signals,"fetched_at":iso(at),"report_current":today_report})
                        storage.save_snapshot("live_regime",other_regime)
                def get_minutes(code):
                    try:
                        payload = providers.call("minutes", code, timeout=25)
                        storage.save_bars(code, pd.DataFrame(payload["bars"]), "5min")
                        return code, storage.bars(code, "5min")
                    except Exception:
                        return code, pd.DataFrame()
                with ThreadPoolExecutor(max_workers=3) as minute_workers:
                    frames = dict(minute_workers.map(get_minutes, codes))
            elif today_report:
                live.evaluate(pool, report, [], at)
        storage.save_snapshot("intraday", {"items": signals, "fetched_at": iso(at), "report_current": today_report})
        unavailable = storage.snapshot("markets").get("error") or storage.snapshot("universe").get("error")
        storage.save_snapshot("monitor_status", {"state": "offline" if unavailable else "online", "error": unavailable, "last_cycle": iso(), "market_open": can_trade})
        return {"state": "success"}
    except Exception as exc:
        storage.save_snapshot("monitor_status", {"state": "error", "last_attempt": iso(), "error": str(exc)[:250]})
        return {"state": "failed", "error": str(exc)[:250]}
    finally:
        LOCK.release()


def run_local_report():
    if not LOCK.acquire(blocking=False):
        return {"state": "busy"}
    try:
        # History maintenance owns backfill; a report uses the available snapshot.
        # This avoids two backfill writers and duplicate network work.
        schedule_history_maintenance()
        return jobs.build_report("local", warm=False)
    except Exception as exc:
        result = {"state": "failed", "finished_at": iso(), "error": str(exc)[:250]}
        storage.save_snapshot("run_status", result)
        return result
    finally:
        LOCK.release()


def sync():
    if not LOCK.acquire(blocking=False):
        return {"state": "busy"}
    try:
        return sync_reports()
    finally:
        LOCK.release()


def monitor():
    STOP.clear()
    storage.save_snapshot("monitor_status", {"state": "starting", "started_at": iso()})
    sync()
    refresh_cycle(full=True)
    ticks = 0
    while not STOP.wait(config()["market_poll_seconds"]):
        ticks += 1
        refresh_cycle(full=ticks % 3 == 0)
        if ticks % 5 == 0 and not LOCK.locked() and not in_session(now()):
            jobs.collect("news", ["国际政策", "美联储", "日本央行", "A股"], timeout=45)
            candidates = storage.snapshot("latest_report").get("candidates", [])
            if candidates:
                jobs.collect("announcements", [c["code"] for c in candidates], timeout=40)
        if ticks % 15 == 0:
            sync()
        # Reconnect after an entire missed day: never use yesterday's candidates as today's signals.
        report = storage.snapshot("latest_report")
        t = now()
        cal = TradingCalendar()
        if cal.known(t.date()) and cal.is_session(t.date()) and report.get("trade_date") != t.date().isoformat():
            previous = storage.snapshot("run_status")
            last_attempt = previous.get("finished_at") or previous.get("started_at")
            if not last_attempt or (t-dt(last_attempt)).total_seconds() >= 900:
                run_local_report()
