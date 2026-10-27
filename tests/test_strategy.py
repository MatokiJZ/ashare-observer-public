from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ashare.core import TZ, TradingCalendar, quote_fresh
from ashare.strategy import aligned_correlations, features, intraday_signal, mainboard
from ashare.research import purged_splits


def frame():
    days = pd.bdate_range("2025-01-01", periods=160)
    close = np.linspace(10, 16, len(days))
    return pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "open": close, "close": close, "low": close-.2, "high": close+.2, "volume": 10_000_000, "amount": 150_000_000, "available_at": days.strftime("%Y-%m-%dT16:10:00+08:00")})


def test_future_rows_do_not_change_features():
    f = frame()
    at = datetime.fromisoformat(f.available_at.iloc[130])
    expected = features(f.iloc[:131], at)
    f.loc[131:, "close"] = 9999
    assert features(f, at) == expected
    assert features(f, at)["sessions"] == 131


def test_missing_amount_is_not_treated_as_zero():
    f = frame()
    f.loc[150, "amount"] = np.nan
    assert features(f, datetime(2026, 1, 1, tzinfo=TZ)) is None


def test_calendar_uses_explicit_sessions(calendar):
    assert calendar.is_session(date(2026, 10, 12))
    assert not calendar.is_session(date(2026, 10, 11))
    assert calendar.previous(date(2026, 10, 12)) == "2026-10-09"
    assert calendar.elapsed("2026-10-09", "2026-10-13") == 2
    with pytest.raises(ValueError):
        calendar.is_session(date(2027, 1, 1))


def test_mainboard_scope():
    assert mainboard("600519") and mainboard("003000")
    assert not mainboard("300750") and not mainboard("688001") and not mainboard("830799")


def test_future_and_unknown_quotes_are_never_fresh():
    at = datetime(2026,10,13,10,tzinfo=TZ)
    assert quote_fresh({"market_time":"2026-10-13T09:59:00+08:00","delay_status":"timestamped"},at)
    assert not quote_fresh({"market_time":"2026-10-13T10:01:00+08:00","delay_status":"timestamped"},at)
    assert not quote_fresh({"market_time":"2026-10-13T10:00:00+08:00","delay_status":"unknown"},at)


def test_incomplete_minute_bars_never_trigger():
    c={"code":"600000","trade_eligible":True,"strategy":"pullback","price":10,"features":{"ma20":9,"atr":1}}
    rows=[{"date":f"2026-10-13T09:{m}:00+08:00","open":10,"close":10.1,"high":10.2,"low":10,"volume":10000,"amount":101000} for m in [35,40,45]]
    rows[-1].update(close=10.8,high=10.8)
    result=intraday_signal(c,pd.DataFrame(rows),datetime(2026,10,13,9,44,tzinfo=TZ))
    assert result["state"] != "triggered"


def test_stale_minute_bars_block_signal():
    c={"code":"600000","trade_eligible":True,"strategy":"pullback","price":10,"features":{"ma20":9,"atr":1}}
    rows=[{"date":f"2026-10-13T09:{m}:00+08:00","close":10.1,"high":10.2,"volume":10000,"amount":101000} for m in [35,40,45]]
    assert intraday_signal(c,pd.DataFrame(rows),datetime(2026,10,13,10,tzinfo=TZ))["state"] == "blocked"


def test_time_alignment_never_uses_same_day_us_close():
    china=[{"time":f"2026-10-{d:02}T15:00:00+08:00","close":100+d,"date":f"2026-10-{d:02}"} for d in range(8,14)]
    us=[{"time":f"2026-10-{d:02}T16:00:00-04:00","close":100+2*d,"date":f"2026-10-{d:02}"} for d in range(7,14)]
    before=aligned_correlations({"cn":china,"us":us},"cn",windows=(3,))
    us[-1]["close"]=1_000_000
    after=aligned_correlations({"cn":china,"us":us},"cn",windows=(3,))
    assert before==after


def test_purged_windows_have_five_session_gap():
    dates=pd.bdate_range("2023-01-01",periods=400).strftime("%Y-%m-%d").tolist()
    for split in purged_splits(dates):
        assert dates.index(split["test"][0])-dates.index(split["train"][-1])==6
        assert set(split["train"]).isdisjoint(split["test"])
