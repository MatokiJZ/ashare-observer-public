from datetime import datetime
import pytest
from ashare import performance, storage
from ashare.core import TZ, iso


def fill(id, ts, side, quantity, price, fee):
    return {"id": id, "ts": ts, "symbol": "600000", "side": side, "quantity": quantity, "price": price, "fees": fee,
            "strategy": "pullback", "reason": "达到最长持有期"}


def test_partial_sales_are_one_round_trip_and_costs_reconcile(calendar):
    trades = [fill("buy", "2026-10-12T10:00:00+08:00", "buy", 1000, 10, 5),
              fill("sell1", "2026-10-13T10:00:00+08:00", "sell", 400, 11, 7),
              fill("sell2", "2026-10-14T10:00:00+08:00", "sell", 600, 9, 8)]
    contexts = {"buy": {"candidate": {"strategy_version": "v1", "selection_version": "live-v1"}, "benchmark": {"price": 100}},
                "sell1": {"benchmark": {"price": 110}}, "sell2": {"benchmark": {"price": 90}}}
    partial = performance.summarize(trades[:2], contexts, [], calendar)
    assert partial["closed_trades"] == 0 and partial["win_rate_pct"] is None
    result = performance.summarize(trades, contexts, [], calendar)
    trip = result["closed"][0]
    assert result["closed_trades"] == 1 and result["win_rate_pct"] == 0
    assert trip["net_pnl"] == -220  # 4,400+5,400-10,000-5-7-8, independently summed.
    assert trip["net_return_pct"] == pytest.approx(-220/10005*100)
    assert trip["benchmark_return_pct"] == pytest.approx(-2)
    assert trip["holding_sessions"] == 2
    assert result["by_strategy"][0]["strategy_version"] == "v1"


def test_missing_benchmark_is_not_zero_and_unmatched_sales_not_counted(calendar):
    trades = [fill("buy", "2026-10-12T10:00:00+08:00", "buy", 100, 10, 5),
              fill("sell", "2026-10-13T10:00:00+08:00", "sell", 200, 11, 8)]
    result = performance.summarize(trades, {}, [], calendar)
    assert result["closed_trades"] == 1
    assert result["unmatched_sell_quantity"] == 100
    assert result["benchmark_samples"] == 0 and result["mean_excess_pct"] is None
    assert result["closed"][0]["net_pnl"] == 91


def test_full_observed_curve_drawdown_includes_initial_capital(calendar):
    curve = [{"ts": "2026-10-12T10:00:00+08:00", "value": 99900},
             {"ts": "2026-10-13T10:00:00+08:00", "value": 110000},
             {"ts": "2026-10-14T10:00:00+08:00", "value": 99000}]
    result = performance.summarize([],{},curve,calendar)
    assert result["max_drawdown_pct"] == pytest.approx(10)
    assert result["observed_sessions"] == 3


def test_benchmark_cannot_use_future_or_unknown_timestamp():
    at = datetime(2026,10,14,10,tzinfo=TZ)
    storage.save_snapshot("markets", {"items": [{"symbol":"sh000300", "price":4000,
        "market_time":"2026-10-14T10:01:00+08:00", "delay_status":"timestamped"}]})
    assert performance.fill_context({"id":"x", "created_at":iso(at)},at)["benchmark"] is None


def test_trade_diagnostics_distinguish_cost_evidence_from_timing_hypotheses(calendar):
    trades = [fill("buy", "2026-10-12T10:00:00+08:00", "buy", 100, 10, 5),
              fill("sell", "2026-10-13T10:00:00+08:00", "sell", 100, 10.05, 5)]
    contexts = {"sell":{"position_observation":{"observed_high":10.5,"observed_low":9.5}}}
    trip = performance.summarize(trades,contexts,[],calendar)["closed"][0]
    assert trip["net_pnl"] == -5
    assert trip["observed_favorable_pct"] == pytest.approx(5)
    assert trip["observed_adverse_pct"] == pytest.approx(-5)
    assert any(text.startswith("证据：交易费用") for text in trip["findings"])
    assert any(text.startswith("待检验：") for text in trip["findings"])
    legacy = performance.summarize(trades,{},[],calendar)["closed"][0]
    assert legacy["observed_favorable_pct"] is None
