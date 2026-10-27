from datetime import datetime, timedelta
import json

import pandas as pd

from ashare import broker
from ashare.core import TZ, iso, config
from ashare.storage import set_value, records


def setup_order(at, side="buy", entry_date="2026-10-12"):
    candidate={"code":"600000","name":"测试股票","sector":"测试行业","strategy":"pullback","price":10,"features":{"ma20":9,"atr":1},"stop_distance":2}
    state=broker.initial_state()
    state["last_tick"]=iso(at-timedelta(seconds=30))
    state["pending"]=[{"id":"test-order","code":"600000","candidate":candidate,"side":side,"created_at":iso(at-timedelta(minutes=2)),"expires_at":iso(at+timedelta(minutes=8))}]
    if side=="sell":
        state["cash"]=90000
        state["positions"]=[{"code":"600000","name":"测试股票","sector":"测试行业","quantity":1000,"entry_date":entry_date,"entry_price":10,"entry_fee":5,"stop":9,"ma20":9,"atr":1,"high_water":10,"strategy":"pullback","mark":10,"mark_time":iso(at)}]
    set_value("portfolio",state)
    q={"code":"600000","price":10,"bid":9.99,"ask":10.01,"limit_up":11,"limit_down":9,"market_time":iso(at),"delay_status":"timestamped"}
    bars={"600000":pd.DataFrame([{"date":iso(at-timedelta(seconds=10)),"volume":1_000_000}])}
    return state,q,bars


def test_position_risk_cash_and_idempotence(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at)
    regime={"state":"normal","exposure":.8}
    broker.tick([q],[],regime,calendar,at,bars)
    v=broker.view()
    assert len(v["trades"])==1
    assert v["positions"][0]["quantity"]==200  # 500 risk / 2 stop, rounded down to a 100-share lot.
    assert v["cash"]>0
    assert v["positions"][0]["quantity"]*2<=500
    broker.tick([q],[],regime,calendar,at,bars)
    assert len(broker.view()["trades"])==1


def test_t_plus_one(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at,"sell","2026-10-13")
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"]
    assert broker.view()["positions"][0]["quantity"]==1000


def test_sell_next_session_and_fee_accounting(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at,"sell")
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    view=broker.view()
    assert len(view["trades"])==1 and not view["positions"]
    assert view["trades"][0]["fees"]>5
    assert view["realized_pnl"]<0


def test_limit_down_cannot_be_sold(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at,"sell")
    q.update(price=9,bid=9)
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"]


def test_offline_orders_discarded(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    state,q,bars=setup_order(at)
    state["last_tick"]=iso(at-timedelta(minutes=10))
    set_value("portfolio",state)
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"] and not broker.view()["pending"]


def test_same_observation_cannot_fill_order(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    state,q,bars=setup_order(at)
    state["pending"][0]["created_at"]=iso(at)
    set_value("portfolio",state)
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"]


def test_unknown_limit_prices_prevent_fills(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at)
    q.pop("limit_up")
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"]


def test_new_risk_cancels_an_unfilled_buy(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    _,q,bars=setup_order(at)
    broker.tick([q],[{"code":"600000","state":"blocked"}],{"state":"normal","exposure":.8},calendar,at,bars)
    assert not broker.view()["trades"] and not broker.view()["pending"]


def test_stop_risk_budget_includes_fees(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    state,q,bars=setup_order(at)
    state["pending"][0]["candidate"]["stop_distance"] = 2.5
    set_value("portfolio",state)
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert broker.view()["positions"][0]["quantity"] == 100


def test_replayed_fill_id_cannot_change_cash_or_positions(calendar):
    at=datetime(2026,10,13,10,5,10,tzinfo=TZ)
    state,q,bars=setup_order(at)
    order=state["pending"][0]
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    after=broker.account()
    cash=after["cash"]
    after["pending"]=[order]
    set_value("portfolio",after)
    broker.tick([q],[],{"state":"normal","exposure":.8},calendar,at,bars)
    assert broker.account()["cash"] == cash
    assert len(broker.view()["trades"]) == 1
