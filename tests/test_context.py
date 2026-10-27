from datetime import datetime
from copy import deepcopy

from ashare.context import apply_candidate_context, crowding, event_context, overseas_overlay
from ashare.core import TZ

AT = datetime(2026, 10, 13, 8, 35, tzinfo=TZ)


def test_news_requires_both_published_and_seen_before_decision():
    event = {"title": "半导体出口管制政策", "published_at": "2026-10-13T07:00:00+08:00", "first_seen_at": "2026-10-13T08:36:00+08:00"}
    assert event_context([event], AT)["items"] == []
    event["first_seen_at"] = "2026-10-13T08:30:00+08:00"
    assert "半导体" in event_context([event], AT)["items"][0]["related_sectors"]


def test_future_or_old_overseas_price_does_not_change_regime():
    baseline = {"state": "normal", "exposure": .8}
    q = {"symbol": "^IXIC", "name": "纳斯达克", "market_time": "2026-10-13T09:00:00+08:00", "change_pct": -5}
    assert overseas_overlay(baseline, {"items": [q]}, AT)["state"] == "normal"
    q["market_time"] = "2026-10-13T04:00:00+08:00"
    assert overseas_overlay(baseline, {"items": [q]}, AT)["exposure"] == .4


def test_missing_announcements_and_industry_block_entry():
    c = {"code": "600000", "sector": "银行", "risks": [], "score": 90, "features": {"asof": "2026-10-12"}, "valuation": {"pe": 10}}
    out = apply_candidate_context([c], {}, [], {}, AT)[0]
    assert not out["trade_eligible"]
    assert "行业历史趋势待核实" in out["risks"]
    assert "近期公司公告覆盖不完整" in out["risks"]


def test_current_disclosure_with_risk_overrides_positive_trend():
    c = {"code": "600000", "sector": "银行", "risks": [], "reasons": [], "score": 90, "features": {"asof": "2026-10-12"}, "valuation": {"pe": 10}}
    hist = {"BK001": [{"date": f"2026-09-{d:02}", "close": d+10} for d in range(1, 25)]}
    anns = {"coverage": {"600000": {"ok": True, "fetched_at": "2026-10-13T08:30:00+08:00"}}, "items": [{"code": "600000", "title": "关于立案调查的公告", "published_at": "2026-10-12T18:00:00+08:00", "first_seen_at": "2026-10-13T08:30:00+08:00"}]}
    out = apply_candidate_context([c], hist, [{"name": "银行", "symbol": "BK001"}], anns, AT)[0]
    assert out["sector_strong"]
    assert not out["trade_eligible"]
    assert any("立案" in r for r in out["risks"])


def test_crowding_missing_rows_never_create_zero_observation():
    assert crowding([])["top20_amount_share"] is None
    assert crowding([{"amount": 100, "change_pct": 8}, {"amount": 300, "change_pct": -1}])["extreme_move_share"] == .5
