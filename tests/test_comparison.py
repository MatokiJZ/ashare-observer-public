from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ashare import comparison as c, storage, providers
from ashare.api import app
from ashare.core import TZ, TradingCalendar


def fixture_data():
    # Explicit fixture exchange calendar, with a holiday omitted.
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-08-03", "2026-09-30") if d.strftime("%Y-%m-%d") != "2026-09-25"]
    cal = TradingCalendar({"dates": days, "valid_through": "2026-09-30"})
    assets = [{"id": "stock:600519", "symbol": "600519", "name": "股票", "kind": "stock"},
              {"id": "index:sh000300", "symbol": "sh000300", "name": "指数", "kind": "index"}]
    price, rows = 100, []
    for i, day in enumerate(days):
        price *= 1 + (.01 if i % 3 else -.02)
        rows.append({"date": day, "close": price})
    payloads = [{"items": rows, "adjustment": "qfq"}, {"items": [{**r, "close": r["close"] * 30} for r in rows]}]
    return cal, assets, payloads


def test_common_baseline_returns_and_correlation():
    cal, assets, payloads = fixture_data()
    out = c.calculate(assets, payloads, 20, cal, datetime(2026, 9, 30, 17, tzinfo=TZ))
    assert len(out["dates"]) == 21
    assert "2026-09-25" not in out["dates"]
    assert all(s["values"][0] == 0 for s in out["series"])
    assert out["series"][0]["values"] == pytest.approx(out["series"][1]["values"])
    assert out["pairs"][0]["samples"] == 20
    assert out["pairs"][0]["value"] == pytest.approx(1)
    first = payloads[0]["items"][-21]["close"]
    last = payloads[0]["items"][-1]["close"]
    assert out["series"][0]["change_pct"] == pytest.approx((last / first - 1) * 100)


def test_missing_day_is_gap_and_multiday_return_is_not_a_daily_return():
    cal, assets, payloads = fixture_data()
    missing = payloads[0]["items"].pop(-5)["date"]
    out = c.calculate(assets, payloads, 20, cal, datetime(2026, 9, 30, 17, tzinfo=TZ))
    assert out["series"][0]["values"][out["dates"].index(missing)] is None
    assert out["series"][0]["state"] == "partial"
    assert out["pairs"][0]["samples"] == 18
    assert out["pairs"][0]["value"] is None


def test_short_history_shared_start_stale_end_and_no_unfinished_bar():
    cal, assets, payloads = fixture_data()
    payloads[0]["items"] = payloads[0]["items"][-12:-2]
    out = c.calculate(assets, payloads, 60, cal, datetime(2026, 9, 30, 10, tzinfo=TZ))
    assert out["expected_through"] == "2026-09-29"
    assert "2026-09-30" not in out["dates"]
    assert out["baseline_date"] == payloads[0]["items"][0]["date"]
    assert out["series"][0]["state"] == "stale"
    assert out["series"][0]["change_pct"] is None
    assert out["series"][1]["values"][0] == 0
    assert out["warnings"]


def test_no_common_start_unavailable_constant_and_single_series():
    cal, assets, payloads = fixture_data()
    at = datetime(2026, 9, 30, 17, tzinfo=TZ)
    constant = {"items": [{**r, "close": 100} for r in payloads[0]["items"]]}
    out = c.calculate(assets, [constant, payloads[1]], 20, cal, at)
    assert out["pairs"][0]["value"] is None
    assert "波动" in out["pairs"][0]["reason"]
    empty = c.calculate(assets, [{}, payloads[1]], 20, cal, at)
    assert empty["series"][0]["state"] == "unavailable"
    assert empty["series"][0]["values"] == [None] * 21
    assert empty["warnings"]
    payloads[0]["items"] = payloads[0]["items"][-2:]
    payloads[1]["items"] = payloads[1]["items"][:-2]
    disjoint = c.calculate(assets, payloads, 20, cal, at)
    assert disjoint["baseline_date"] is None
    single = c.calculate(assets[:1], payloads[:1], 20, cal, at)
    assert single["series"][0]["points"] == 2
    assert single["pairs"] == []


def test_api_validation_and_distinct_stock_index_ids(monkeypatch):
    storage.save_snapshot("universe", {"items": [{"code": "000001", "name": "平安银行"}]})
    storage.save_snapshot("sectors", {"items": [{"symbol": "BK0475", "name": "银行"}]})
    client = TestClient(app)
    listed = client.get("/api/comparison/catalog").json()["items"]
    assert {"stock:000001", "index:sh000001", "sector:BK0475"} <= {r["id"] for r in listed}
    for ids in ["stock:../../state", "index:sh000001,index:sh000001", ",".join(["index:sh000001"] * 9)]:
        assert client.get("/api/comparison", params={"ids": ids}).status_code == 422
    cal, _, _ = fixture_data()
    storage.save_snapshot("calendar", cal.payload)
    monkeypatch.setattr(c, "now", lambda: datetime(2026, 9, 30, 17, tzinfo=TZ))
    monkeypatch.setattr(c, "request_refresh", lambda *args: False)
    response = client.get("/api/comparison", params={"ids": "stock:000001,index:sh000001,sector:BK0475"})
    assert response.status_code == 200
    assert len(response.json()["series"]) == 3
    assert client.get("/api/comparison", params={"ids": "index:sh000001", "window": 2}).status_code == 422


def test_failed_refresh_preserves_previous_series_and_retry_is_throttled(monkeypatch):
    asset = {"id": "stock:600519", "kind": "stock", "symbol": "600519"}
    old = {"items": [{"date": "2026-09-29", "close": 100}], "adjustment": "qfq"}
    storage.save_snapshot(c.cache_name(asset), old)
    monkeypatch.setattr(c.providers, "call", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("测试断网")))
    key = ("test", asset["id"])
    c.PENDING.add(key)
    c._fetch(asset, key)
    cached = c.cached_history(asset)
    assert cached["items"] == old["items"]
    assert "测试断网" in cached["error"]
    assert key not in c.PENDING
    assert c.request_refresh(asset, cached, "2026-09-30", c.now(), force=True) is False
    # Separate cache must not write adjusted prices to execution bars / ledgers.
    assert not (c.data_dir() / "bars").exists()
    assert not (c.data_dir() / "state.sqlite").exists()


def test_adjusted_provider_removes_suspended_rows_and_keeps_own_series(monkeypatch):
    class Response:
        def json(self):
            return {"data": {"sh600519": {"qfqday": [["2026-09-29", "9", "10", "11", "8", "20"],
                                                      ["2026-09-30", "9", "10", "11", "8", "0"]]}}}
    monkeypatch.setattr(providers, "http", lambda *a, **kw: Response())
    result = providers.comparison_history("stock", "600519")
    assert result["adjustment"] == "qfq"
    assert result["items"] == [{"date": "2026-09-29", "open": 9, "close": 10, "high": 11, "low": 8, "amount": None}]


def test_future_availability_is_excluded_and_window_affects_all_outputs():
    cal, assets, payloads = fixture_data()
    payloads[0]["items"][-1]["available_at"] = "2026-10-01T16:10:00+08:00"
    at = datetime(2026, 9, 30, 17, tzinfo=TZ)
    small = c.calculate(assets, payloads, 20, cal, at)
    large = c.calculate(assets, payloads, 60, cal, at)
    assert small["series"][0]["change_pct"] is None
    assert large["baseline_date"] < small["baseline_date"]
    assert large["series"][1]["points"] > small["series"][1]["points"]
    assert large["pairs"][0]["samples"] > small["pairs"][0]["samples"]


def test_sector_https_fallback_preserves_index_identity(monkeypatch):
    urls = []
    class Response:
        def json(self):
            return {"data": {"klines": ["2026-09-30,100,102,103,99,1000"]}}
    def fetch(url, **kwargs):
        urls.append(url)
        assert kwargs["params"]["secid"] == "90.BK0475"
        assert kwargs["headers"]["Referer"] == providers.SOURCE_EM
        if len(urls) == 1:
            raise ValueError("空响应")
        return Response()
    monkeypatch.setattr(providers, "http", fetch)
    result = providers.sector_history("BK0475")
    assert len(urls) == 2 and all(url.startswith("https://") for url in urls)
    assert result["items"] == [{"date": "2026-09-30", "open": 100, "close": 102, "high": 103, "low": 99, "amount": None}]
    assert "BK0475" in result["source"]


def test_newer_fetch_of_old_index_cannot_replace_newer_prices():
    asset = {"id": "index:sh000300", "kind": "index", "symbol": "sh000300"}
    storage.save_snapshot(c.cache_name(asset), {"items": [{"date": "2026-09-30", "close": 4000}], "fetched_at": "2026-09-30T17:00:00+08:00"})
    storage.save_snapshot("index_histories", {"items": {"sh000300": [{"date": "2026-09-29", "close": 3900}]}, "fetched_at": "2026-09-30T18:00:00+08:00"})
    assert c.cached_history(asset)["items"][0]["date"] == "2026-09-30"


def test_candles_require_real_valid_ohlc_and_preserve_missing_amount():
    cal, assets, payloads = fixture_data()
    rows = payloads[0]["items"]
    good = rows[-1]
    good.update(open=good["close"] * .99, low=good["close"] * .98, high=good["close"] * 1.02, amount=12345678)
    rows[-2].update(open=100, low=110, high=90, amount=-2)  # Invalid OHLC must not draw.
    out = c.calculate(assets, payloads, 20, cal, datetime(2026, 9, 30, 17, tzinfo=TZ))
    s = out["series"][0]
    assert s["candles"][-1] == [good["open"], good["close"], good["low"], good["high"]]
    assert s["candles"][-2] is None
    assert s["candle_points"] == 1
    assert s["amounts"][-1] == 12345678
    assert s["amounts"][-2] is None
    assert s["amount_points"] == 1
    assert out["series"][1]["candle_points"] == 0  # Close-only caches remain lines only.
    assert out["series"][1]["points"] == 21


@pytest.mark.parametrize("kind,symbol,tx", [("stock", "600519", "sh600519"), ("index", "sh000300", "sh000300")])
def test_comparison_amount_is_yuan_for_both_stock_and_index(monkeypatch, kind, symbol, tx):
    class Response:
        def json(self):
            return {"data": {tx: {"qfqday" if kind == "stock" else "day": [["2026-09-30", "10", "11", "12", "9", "100", {}, "0.5", "123.45"]]}}}
    monkeypatch.setattr(providers, "http", lambda *a, **kw: Response())
    result = providers.comparison_history(kind, symbol)
    assert result["items"][0]["amount"] == 1234500
    assert c.candle(result["items"][0]) == [10, 11, 9, 12]


def test_legacy_close_only_cache_automatically_upgrades(monkeypatch):
    asset = {"id": "stock:600519", "kind": "stock", "symbol": "600519"}
    at = datetime(2026, 9, 30, 17, tzinfo=TZ)
    payload = {"items": [{"date": "2026-09-30", "close": 100}],
               "fetched_at": "2026-09-30T16:58:00+08:00", "last_attempt": "2026-09-30T16:58:00+08:00"}
    submitted = []
    monkeypatch.setattr(c.POOL, "submit", lambda *args: submitted.append(args))
    key = (str(c.data_dir()), asset["id"])
    try:
        assert c.request_refresh(asset, payload, "2026-09-30", at)
        assert len(submitted) == 1
        assert c.request_refresh(asset, payload, "2026-09-30", at)
        assert len(submitted) == 1
    finally:
        c.PENDING.discard(key)


def test_future_duplicate_cannot_leak_into_candle_or_turnover():
    cal, assets, payloads = fixture_data()
    good = {**payloads[0]["items"][-1], "open": 100, "low": 50, "high": 200, "amount": 1234}
    future = {**good, "open": 999, "low": 1, "high": 1000, "amount": 99999, "available_at": "2026-10-01T16:00:00+08:00"}
    payloads[0]["items"].extend([good, future])
    out = c.calculate(assets, payloads, 20, cal, datetime(2026, 9, 30, 17, tzinfo=TZ))
    assert out["series"][0]["candles"][-1][0] == 100
    assert out["series"][0]["amounts"][-1] == 1234
