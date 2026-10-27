from copy import deepcopy
from datetime import datetime, timedelta

from ashare import live, storage
from ashare.context import apply_candidate_context
from ashare.core import TZ, config, data_dir, iso

AT = datetime(2026, 10, 14, 10, 5, tzinfo=TZ)


def evidence():
    candidates = [{"code": code, "name": name, "sector": "银行", "score": score, "risks": [], "reasons": [],
                   "price": 10, "features": {"asof": "2026-10-13", "ma20": 9, "atr": 1}, "valuation": {"pe": 10}, "strategy": "pullback"}
                  for code,name,score in [("600000", "测试甲公司", 90), ("600001", "测试乙公司", 89)]]
    sectors = [{"name": "银行", "symbol": "BK001"}]
    histories = {"BK001": [{"date": f"2026-09-{d:02}", "close": d+10} for d in range(1,25)]}
    anns = {"coverage": {c["code"]: {"ok": True, "fetched_at": iso(AT)} for c in candidates}, "items": []}
    quotes = [{"code": c["code"], "price": 10, "amount": 100_000_000, "change_pct": i, "market_time": iso(AT), "delay_status": "timestamped"} for i,c in enumerate(candidates)]
    return candidates,quotes,sectors,histories,anns


def test_repeated_context_does_not_compound_scores_or_stale_risks():
    c,_,s,h,a = evidence()
    one = apply_candidate_context(deepcopy(c), h, s, {}, AT)
    assert all(not x["trade_eligible"] for x in one)
    two = apply_candidate_context(one, h, s, a, AT)
    three = apply_candidate_context(deepcopy(two), h, s, a, AT)
    assert two == three
    assert all(x["trade_eligible"] for x in two)
    assert len(two[0]["reasons"]) == 1


def test_live_price_and_newly_visible_news_change_candidate_order_without_mutating_pool():
    c,q,s,h,a = evidence()
    original = deepcopy(c)
    event = {"title": "测试甲公司收到立案通知", "published_at": iso(AT-timedelta(minutes=10)), "first_seen_at": iso(AT+timedelta(seconds=1))}
    before = live.rank_live(c,q,s,h,a,{"items": [event]},AT)
    assert all(x["trade_eligible"] for x in before)
    after = live.rank_live(c,q,s,h,a,{"items": [event]},AT+timedelta(seconds=2))
    assert after[0]["code"] == "600001" and not after[1]["trade_eligible"]
    assert any("媒体风险" in r for r in after[1]["risks"])
    assert c == original
    q[1]["price"] = 8
    assert all(not x["trade_eligible"] for x in live.rank_live(c,q,s,h,a,{"items": [event]},AT+timedelta(seconds=2)))


def test_unknown_or_stale_quotes_never_allow_new_positions():
    c,q,s,h,a = evidence()
    q[0]["delay_status"] = "unknown"
    q[1]["market_time"] = iso(AT-timedelta(minutes=4))
    assert all(not x["trade_eligible"] for x in live.rank_live(c,q,s,h,a,{},AT))


def test_live_versions_preserve_report_and_deduplicate_unchanged_roster():
    c,q,s,h,a = evidence()
    storage.save_snapshot("sectors", {"items": s})
    storage.save_snapshot("sector-BK001", {"items": h["BK001"]})
    storage.save_snapshot("announcements", a)
    report = {"id": "original", "candidates": deepcopy(c), "trade_date": "2026-10-14"}
    storage.save_snapshot("latest_report", report)
    pool = {"items": c, "generated_at": iso(AT)}
    first = live.evaluate(pool, report, q, AT)
    again = live.evaluate(pool, report, q, AT+timedelta(seconds=30))
    assert first["id"] == again["id"]
    assert len(list((data_dir()/"candidate_versions").glob("*.json"))) == 1
    q[0]["price"] = 8
    changed = live.evaluate(pool,report,q,AT+timedelta(seconds=40))
    assert changed["id"] != first["id"]
    assert storage.snapshot("latest_report") == report
    assert live.current(report, AT+timedelta(days=1)) == {}


def test_service_uses_dynamic_pool_and_cancels_removed_pending_entries(calendar, monkeypatch):
    from ashare import service, broker
    c,q,s,h,a = evidence()
    report = {"id": "original", "trade_date": "2026-10-14", "candidates": [], "regime": {"state": "normal", "exposure": .8}}
    storage.save_snapshot("latest_report", report)
    storage.save_snapshot("sectors", {"items": s})
    storage.save_snapshot("sector-BK001", {"items": h["BK001"]})
    storage.save_snapshot("announcements", a)
    storage.save_snapshot("news", {"last_attempt": iso(AT)})
    storage.save_snapshot("universe", {"fetched_at": iso(AT), "items": q})
    account = broker.initial_state()
    account["pending"] = [{"code": "600002", "side": "buy"}]
    storage.set_value("portfolio", account)
    monkeypatch.setattr(service, "now", lambda: AT)
    monkeypatch.setattr(service, "TradingCalendar", lambda: calendar)
    monkeypatch.setattr(live, "ensure_pool", lambda *args: {"items":c, "coverage":{"ratio":1}, "generated_at":iso(AT)})
    monkeypatch.setattr(service.jobs, "collect", lambda op,*args,**kw: {"items":q} if op=="quotes" else {})
    monkeypatch.setattr(service.providers, "call", lambda *args,**kw: {"bars": []})
    called = []
    monkeypatch.setattr(broker, "tick", lambda *args: called.append(args))
    result = service.refresh_cycle()
    assert result["state"] == "success"
    assert called and called[0][2]["state"] != "halt"
    assert any(x["code"] == "600002" and x["state"] == "invalid" for x in called[0][1])
    assert storage.snapshot("live_candidates")["pool_count"] == 2
    assert storage.snapshot("latest_report") == report



def test_source_outage_keeps_dated_read_only_prices_without_faking_fresh_report():
    old={'id':'old','report_id':'original','trade_date':'2026-10-14','strategy_version':config()['strategy_version'],
         'generated_at':iso(AT),'candidates':[{'code':'600000','price_plan':{'entry':10}}]}
    storage.save_snapshot('last_visible_candidates',old)
    storage.save_snapshot('live_candidates',{'error':'行情过期','trade_date':'2026-10-14','report_id':'new'})
    viewed=live.current({'id':'new','status':'partial'},AT+timedelta(minutes=10))
    assert viewed['stale'] and viewed['generated_at']==old['generated_at'] and viewed['candidates']==old['candidates']
    assert storage.snapshot('last_visible_candidates')==old
    assert live.current({'id':'new','status':'partial'},AT+timedelta(days=1))=={}
