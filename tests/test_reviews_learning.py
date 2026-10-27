from copy import deepcopy
from datetime import datetime, timedelta
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ashare import accounts, broker, learning, reviews, storage
from ashare.api import app
from ashare.cloud import research_asset, allowed_checkpoint
from ashare.core import TZ, atomic_json, config, data_dir, iso, paper_account


def history(calendar):
    return pd.DataFrame([{"date":d,"open":10+i,"close":10.5+i,"high":11+i,"low":9+i,"volume":1000000,"amount":200000000,
        "available_at":d+"T16:10:00+08:00","corporate_action_checked":True,"corporate_action_event":False} for i,d in enumerate(calendar.days)])


def decision(stamp="2026-10-13T08:35:00+08:00"):
    return {"decision_at":stamp,"candidate":{"code":"600000"}}


def test_horizons_are_trading_sessions_and_future_bars_hidden(calendar):
    at=datetime(2026,10,14,17,tzinfo=TZ)
    r=reviews.outcomes(decision(),history(calendar),calendar,at)
    assert r["reference_date"]=="2026-10-13"
    assert r["horizons"]["1"]["status"]=="matured"
    assert r["horizons"]["3"]["status"]=="observing"
    assert r["horizons"]["5"]["end_date"]=="2026-10-20"
    assert r["horizons"]["1"]["return_pct"]==pytest.approx((14.5/13-1)*100)


def test_intraday_and_closing_cannot_use_that_mornings_open(calendar):
    at=datetime(2026,10,19,17,tzinfo=TZ)
    for stamp in ["2026-10-16T14:00:00+08:00","2026-10-16T16:30:00+08:00"]:
        r=reviews.outcomes(decision(stamp),history(calendar),calendar,at)
        assert r["reference_date"]=="2026-10-19"
        assert r["horizons"]["1"]["status"]=="observing"


def test_missing_or_corporate_action_never_becomes_zero_return(calendar):
    frame=history(calendar)
    at=datetime(2026,10,19,17,tzinfo=TZ)
    result=reviews.outcomes(decision(),frame[frame.date!="2026-10-14"],calendar,at)
    assert result["horizons"]["1"]["status"]=="missing"
    assert result["horizons"]["1"]["return_pct"] is None
    frame.loc[frame.date=="2026-10-14","corporate_action_event"]=True
    assert reviews.outcomes(decision(),frame,calendar,at)["horizons"]["1"]["status"]=="unverified"


def test_review_without_trades_is_idempotent_and_keeps_decision(calendar):
    storage.save_snapshot("calendar",calendar.payload)
    frame=history(calendar)
    storage.save_bars("600000",frame)
    c={"code":"600000","name":"示例","price_plan":{"buy_low":12,"buy_high":13},"risks":[]}
    report={"id":"2026-10-13-083500-update","generated_at":"2026-10-13T08:35:00+08:00","trade_date":"2026-10-13","kind":"premarket","candidates":[c]}
    path=data_dir()/"reports"/(report["id"]+".json");atomic_json(path,report)
    original=path.read_bytes()
    at=datetime(2026,10,19,17,tzinfo=TZ)
    first=reviews.rebuild(at)
    second=reviews.rebuild(at)
    assert first["updated"]==1 and second["updated"]==0
    assert path.read_bytes()==original
    result=json.loads((data_dir()/"reviews/2026-10-13.json").read_text())
    assert result["items"][0]["candidate"]["price_plan"]["buy_low"]==12
    assert all(not a["trades"] for a in result["execution"])


def test_account_context_does_not_share_cash_or_controls():
    storage.set_value("portfolio",{"cash":91234,"positions":[]})
    with paper_account("experiment"):
        assert broker.account()["cash"]==100000
        assert config()["trade_risk"]==.0075
        broker.toggle(False)
        storage.save_snapshot("daily_selection",{"trade_date":"example"})
    assert broker.account()["cash"]==91234
    assert storage.snapshot("daily_selection")=={}
    assert config()["trade_risk"]==.005
    with pytest.raises(ValueError):
        with paper_account("../../oops"): pass


def test_matured_multiple_candidates_remain_stable_on_repeated_rebuild(calendar):
    storage.save_snapshot("calendar",calendar.payload)
    candidates = [{"code":code,"name":code} for code in ("600000","600001")]
    for candidate in candidates:
        storage.save_bars(candidate["code"],history(calendar))
    report = {"id":"2026-10-13-083500-update","generated_at":"2026-10-13T08:35:00+08:00",
              "trade_date":"2026-10-13","kind":"premarket","candidates":candidates}
    atomic_json(data_dir()/"reports"/(report["id"]+".json"),report)
    at = datetime(2026,10,20,17,tzinfo=TZ)
    assert reviews.rebuild(at)["updated"] == 1
    assert reviews.rebuild(at)["updated"] == 0
    result = json.loads((data_dir()/"reviews/2026-10-13.json").read_text())
    assert all(r["outcome"]["horizons"]["5"]["status"] == "matured" for r in result["items"])


def test_full_market_timeout_leaves_budget_for_fallback(monkeypatch):
    import subprocess
    from ashare import providers
    from ashare.core import now
    rows = [{"code":str(600000+i)} for i in range(2500)]
    storage.save_snapshot("universe",{"items":rows,"fetched_at":iso(now())})
    def fail(operation,timeout):
        assert operation == "universe_em" and timeout < 75
        raise subprocess.TimeoutExpired(operation,timeout)
    monkeypatch.setattr(providers,"call",fail)
    monkeypatch.setattr(providers,"tencent_quotes",lambda symbols:[{"code":s[2:]} for s in symbols])
    result = providers.universe()
    assert result["source"] == "腾讯财经" and len(result["items"]) == 2500


def test_drawdown_recovery_waits_next_day_and_halt_never_resets():
    with paper_account("experiment"):
        state={"drawdown_halted":True,"peak_equity":120000}
        regime={"exposure":.8};cfg=config()
        at=datetime(2026,10,13,10,tzinfo=TZ)
        a,b=broker.apply_drawdown(state,cfg,regime,.085,at)
        assert a["trade_risk"]==.00375 and b["exposure"]==.4
        a,b=broker.apply_drawdown(state,cfg,regime,.055,at)
        assert a["trade_risk"]==.00375
        a,b=broker.apply_drawdown(state,cfg,regime,.055,at+timedelta(days=1))
        assert a["trade_risk"]==.0075 and state["drawdown_halted"] and state["peak_equity"]==120000


def test_weak_market_relaxation_does_not_relax_data_guard():
    at=datetime(2026,10,13,10,tzinfo=TZ)
    pool={"coverage":{"ratio":.99},"regime":{"trend_breadth":.2}}
    universe={"fetched_at":iso(at),"items":[{"change_pct":1 if i<2 else -1,"amount":1e8} for i in range(10)]}
    with paper_account("experiment"):
        r=accounts.experimental_regime(pool,universe,at)
        assert r["state"]=="weak" and r["exposure"]==.2
        universe["error"]="failed"
        assert accounts.experimental_regime(pool,universe,at)["state"]=="halt"
        universe.pop("error");pool["regime"]["trend_breadth"]=.09
        assert accounts.experimental_regime(pool,universe,at)["state"]=="halt"


def test_training_split_chronological_and_gaps_enforced():
    dates=[d.date().isoformat() for d in pd.bdate_range("2023-01-01",periods=400)]
    split=learning.temporal_split(dates)
    assert [len(split[k]) for k in ["train","validation","test"]]==[252,42,63]
    assert dates.index(split["validation"][0])-dates.index(split["train"][-1])==6
    assert dates.index(split["test"][0])-dates.index(split["validation"][-1])==6
    assert not learning.temporal_split(dates[:300])["ready"]


def test_feature_future_price_mutation_does_not_change_earlier_inputs():
    days=[d.date().isoformat() for d in pd.bdate_range("2024-01-01",periods=160)]
    base=pd.DataFrame({"date":days,"open":np.arange(160)+100.,"close":np.arange(160)+101.,"high":np.arange(160)+102.,"low":np.arange(160)+99.,"volume":1000000,"amount":200000000,"corporate_action_checked":True,"corporate_action_event":False})
    changed=base.copy();changed.loc[150:,"close"]*=10
    a,b=learning.security_features(base,days),learning.security_features(changed,days)
    pd.testing.assert_frame_equal(a.iloc[:150],b.iloc[:150])


def test_promotion_requires_forward_trades_and_beats_same_risk_rules():
    model={"return_pct":5,"performance":{"closed_trades":30,"net_pnl":1000,"max_drawdown_pct":9}}
    rules={"return_pct":4}
    assert learning.promotion_gate(model,rules,60,True)["passed"]
    assert not learning.promotion_gate(model,rules,59,True)["passed"]
    assert not learning.promotion_gate(model,{"return_pct":6},60,True)["passed"]
    assert not learning.promotion_gate(model,rules,60,False)["passed"]
    model["drawdown_halted"]=True
    assert not learning.promotion_gate(model,rules,60,True)["passed"]


def test_cloud_cannot_import_accounts_or_activation():
    for path in ["state-experiment.sqlite","model_control.json","../models/test/return.txt","accounts/experiment/daily_selection.json"]:
        assert not research_asset(path) and not allowed_checkpoint(path)
    assert research_asset("models/2026-10-21-100000-1234abcd/manifest.json")


def test_new_api_empty_states_and_account_toggle():
    with TestClient(app) as client:
        assert client.get("/api/models").json()["models"]==[]
        assert client.get("/api/reviews").status_code==200
        assert client.get("/api/reviews/2026-10-20").json()["status"]=="pending"
        assert client.get("/api/reviews/invalid").status_code==400
        assert len(client.get("/api/portfolio").json()["accounts"])==2
        assert client.post("/api/portfolio/enabled?account=experiment",json={"enabled":False}).status_code==200
        views=client.get("/api/portfolio").json()["accounts"]
        assert views[0]["enabled"] and not views[1]["enabled"]


def test_filtered_then_promoted_stock_is_reviewed_at_first_selection():
    for clock,rows,excluded in [("100000",[],[{"code":"600000","reason":"未满足条件"}]),("110000",[{"code":"600000","name":"示例"}],[])]:
        r={"id":"2026-10-13-"+clock+"-local","trade_date":"2026-10-13","generated_at":"2026-10-13T"+clock[:2]+":00:00+08:00",
           "kind":"intraday_supplement","candidates":rows,"excluded":excluded}
        atomic_json(data_dir()/"reports"/(r["id"]+".json"),r)
    result=reviews.decisions()
    assert len(result)==1 and result[0]["in_top_list"]
    assert result[0]["decision_at"]=="2026-10-13T11:00:00+08:00"


def test_full_lightgbm_training_is_recorded_but_does_not_activate(monkeypatch):
    days=[d.date().isoformat() for d in pd.bdate_range("2024-01-01",periods=400)]
    records=[]
    for i,d in enumerate(days[:-4]):
        records.append({**{f:float(i%20)/100 for f in learning.FEATURES},"date":d,"code":"600000","eligible":True,
            "label":.02,"raw_label":.03,"label_end":days[i+4],"atr":1.,"verified":True,"open":100+i,
            "close":101+i,"high":102+i,"low":99+i,"volume":10000000.,"previous_volume":10000000.,"previous_close":100+i,
            "breadth":.6,"trend_breadth":.6})
    monkeypatch.setattr(learning,"dataset",lambda *a,**k:pd.DataFrame(records))
    result=learning.train(datetime(2026,10,13,17,tzinfo=TZ))
    assert result["state"]=="complete",result
    manifest=learning.manifests()[0]
    assert manifest["samples"]["train"]==252
    assert (data_dir()/"models"/manifest["id"]/"return.txt").exists()
    assert storage.snapshot("model_control").get("active_model") is None
    result_again=learning.train(datetime(2026,10,14,17,tzinfo=TZ))
    assert result_again["model_id"]==result["model_id"]


def test_model_hash_failure_reverts_without_touching_ledger(monkeypatch):
    identifier="2026-10-13-100000-1234abcd"
    root=data_dir()/"models"/identifier;root.mkdir(parents=True)
    atomic_json(root/"manifest.json",{"id":identifier,"schema":learning.SCHEMA,"features":learning.FEATURES,
        "historical_pass":True,"execution_policy":learning.execution_policy(),"files":{"return.txt":"bad","risk.txt":"bad"}})
    (root/"return.txt").write_text("corrupted")
    storage.save_snapshot("model_control",{"active_model":identifier})
    with paper_account("experiment"):
        state=broker.initial_state();state["drawdown_halted"]=True;storage.set_value("portfolio",state)
    assert learning.active_model() is None
    with paper_account("experiment"):
        assert broker.account()["drawdown_halted"]


def test_live_cycle_drives_two_independent_accounts(calendar,monkeypatch):
    from ashare import service,live,jobs,providers
    clock=[datetime(2026,10,13,10,tzinfo=TZ)]
    monkeypatch.setattr(service,"now",lambda:clock[0]);monkeypatch.setattr(live,"now",lambda:clock[0])
    monkeypatch.setattr(service,"schedule_history_maintenance",lambda:None)
    monkeypatch.setattr(service,"schedule_research",lambda:None)
    monkeypatch.setattr(learning,"shadow_accounts",lambda at:None)
    monkeypatch.setattr(learning,"active_model",lambda:None)
    storage.save_snapshot("calendar",calendar.payload)
    c={"code":"600000","name":"示例","sector":"测试行业","sector_strong":True,"trade_eligible":True,"score":90,"strategy":"pullback",
       "price":10,"features":{"ma20":9,"atr":1},"risks":[],"price_plan":{"status":"ready","trade_date":"2026-10-13","buy_low":9.9,"buy_high":10.2,"stop":9.5,"take_profit":10.8}}
    pool={"items":[c],"coverage":{"ratio":1},"regime":{"state":"normal","exposure":.8,"breadth":.6,"trend_breadth":.6}}
    storage.save_snapshot("latest_report",{"id":"2026-10-13-083500-update","trade_date":"2026-10-13","candidates":[c]})
    storage.save_snapshot("news",{"last_attempt":iso(clock[0]),"items":[]})
    monkeypatch.setattr(live,"ensure_pool",lambda *a:deepcopy(pool))
    monkeypatch.setattr(live,"rank_live",lambda candidates,*a:deepcopy(candidates))
    monkeypatch.setattr(live,"evaluate",lambda *a:{"candidates":[deepcopy(c)]})
    def collect(op,*args,target=None,**kwargs):
        if op=="quotes":
            minute=clock[0].minute
            payload={"items":[{"code":"600000","price":10,"ask":10.01,"bid":9.99,"limit_up":11,"limit_down":9,
                "market_time":iso(clock[0]),"delay_status":"timestamped","cumulative_volume_shares":10000000+minute*1000000}]}
        elif op=="universe":
            payload={"fetched_at":iso(clock[0]),"items":[{"change_pct":1 if i<6 else -1,"amount":1e8} for i in range(10)]}
        else: payload=storage.snapshot(op) or {"items":[]}
        storage.save_snapshot(target or op,payload);return payload
    monkeypatch.setattr(jobs,"collect",collect)
    collect("universe")
    monkeypatch.setattr(providers,"call",lambda *a,**k:{"bars":[{"date":iso(clock[0]),"volume":1000000}]})
    assert service.refresh_cycle()["state"]=="success"
    clock[0]+=timedelta(minutes=1)
    assert service.refresh_cycle()["state"]=="success"
    baseline=broker.view()
    with paper_account("experiment"):
        experiment=broker.view()
    assert len(baseline["trades"])==len(experiment["trades"])==1
    assert experiment["positions"][0]["quantity"]>baseline["positions"][0]["quantity"]
    assert baseline["positions"][0]["strategy_version"]!=experiment["positions"][0]["strategy_version"]
