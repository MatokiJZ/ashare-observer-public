"""Read-only, mixed-asset comparisons. Never feeds adjusted prices into a ledger."""
from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import providers, storage
from .core import TradingCalendar, data_dir, dt, iso, now

INDICES = {
    "sh000300": "沪深300", "sh000001": "上证指数", "sz399001": "深证成指",
    "sh000905": "中证500", "sh000852": "中证1000", "sz399006": "创业板指",
}
MAX_ITEMS = 8
WINDOWS = (20, 60, 120, 250)
POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="comparison")
LOCK = threading.Lock()
PENDING: set[tuple[str, str]] = set()


def catalog() -> dict:
    universe, sectors = storage.snapshot("universe"), storage.snapshot("sectors")
    items = {}
    for row in universe.get("items", []):
        code = str(row.get("code", ""))
        if re.fullmatch(r"[036]\d{5}", code):
            key = "stock:" + code
            items[key] = {"id": key, "kind": "stock", "symbol": code, "name": row.get("name") or code}
    for row in sectors.get("items", []):
        symbol = str(row.get("symbol", ""))
        if re.fullmatch(r"BK\d{4,6}", symbol):
            key = "sector:" + symbol
            items[key] = {"id": key, "kind": "sector", "symbol": symbol, "name": row.get("name") or symbol}
    for symbol, name in INDICES.items():
        key = "index:" + symbol
        items[key] = {"id": key, "kind": "index", "symbol": symbol, "name": name}
    return {"items": list(items.values()), "max_items": MAX_ITEMS, "windows": WINDOWS,
            "sources": {"stock": universe.get("fetched_at"), "sector": sectors.get("fetched_at")},
            "scope": "沪深 A 股、东方财富行业板块、6 个主要 A 股指数"}


def selected_assets(ids: list[str]) -> list[dict]:
    if not 1 <= len(ids) <= MAX_ITEMS or len(set(ids)) != len(ids):
        raise ValueError(f"请选择 1–{MAX_ITEMS} 个不同的对比对象")
    known = {row["id"]: row for row in catalog()["items"]}
    if any(key not in known for key in ids):
        raise ValueError("有对象不在可用目录中，请按名称或代码重新选择")
    return [known[key] for key in ids]


def cache_name(asset: dict) -> str:
    # Called only with a validated catalog record; no caller-controlled paths.
    return "comparison-" + asset["id"].replace(":", "-")


def candle(row: dict) -> list[float] | None:
    values = [providers.number(row.get(k)) for k in ("open", "close", "low", "high")]
    if any(v is None or v <= 0 for v in values):
        return None
    opening, closing, low, high = values
    return values if low <= min(opening, closing) <= max(opening, closing) <= high else None


def cached_history(asset: dict) -> dict:
    own = storage.snapshot(cache_name(asset))
    fallback = {}
    if asset["kind"] == "sector":
        fallback = storage.snapshot("sector-" + asset["symbol"])
    elif asset["kind"] == "index":
        all_indices = storage.snapshot("index_histories")
        fallback = {"items": all_indices.get("items", {}).get(asset["symbol"], []),
                    "source": providers.SOURCE_EM, "fetched_at": all_indices.get("fetched_at")}
        if not fallback["items"] and asset["symbol"] == "sh000300":
            fallback = storage.snapshot("index_history")
    # A comparison refresh must not overwrite the collector's independent cache.
    def freshness(value):
        rows = value.get("items", [])
        latest = max(rows, key=lambda r: str(r.get("date", "")), default={})
        return (str(latest.get("date", ""))[:10], bool(candle(latest)), value.get("fetched_at") or "")
    if fallback.get("items") and (not own.get("items") or freshness(fallback) > freshness(own)):
        return {**fallback, "source": fallback.get("source") or providers.SOURCE_EM,
                "adjustment": "index", "last_attempt": own.get("last_attempt"), "error": own.get("error")}
    return own


def valid_rows(payload: dict, expected: str, calendar: TradingCalendar, at) -> dict[str, dict]:
    rows = {}
    sessions = set(calendar.days)
    for row in payload.get("items", []):
        day = str(row.get("date", ""))[:10]
        value = providers.number(row.get("close"))
        available = row.get("available_at")
        try:
            if available and dt(available) > at:
                continue
        except (TypeError, ValueError):
            continue
        if day in sessions and day <= expected and value is not None and value > 0:
            rows[day] = {**row, "close": value}
    return dict(sorted(rows.items()))


def _fetch(asset: dict, key: tuple[str, str]):
    name = cache_name(asset)
    try:
        result = providers.call("comparison_history", asset["kind"], asset["symbol"], timeout=40)
        if not result.get("items"):
            raise ValueError("来源没有返回有效日线")
        # Replace the whole adjusted series: mixing adjustment vintages creates jumps.
        storage.save_snapshot(name, {**result, "last_attempt": iso(), "error": None})
    except Exception as exc:
        storage.save_snapshot(name, {**storage.snapshot(name), "last_attempt": iso(), "error": str(exc)[:220]})
    finally:
        with LOCK:
            PENDING.discard(key)


def request_refresh(asset: dict, payload: dict, expected: str, at, force=False) -> bool:
    key = (str(data_dir()), asset["id"])
    latest = max((str(r.get("date", ""))[:10] for r in payload.get("items", []) if str(r.get("date", ""))[:10] <= expected), default="")
    needs_candles = not any(str(r.get("date", ""))[:10] == latest and candle(r) for r in payload.get("items", []))
    with LOCK:
        if key in PENDING:
            return True
        attempt = payload.get("last_attempt")
        try:
            age = (at - dt(attempt)).total_seconds() if attempt else float("inf")
        except (TypeError, ValueError):
            age = float("inf")
        if age < (60 if force or (needs_candles and not payload.get("error")) else 900):
            return False
        if not force and not needs_candles and latest >= expected and (payload.get("fetched_at") or "")[:10] == at.date().isoformat():
            return False
        if len(PENDING) >= 24:
            return False
        PENDING.add(key)
        POOL.submit(_fetch, asset, key)
        return True


def calculate(assets: list[dict], payloads: list[dict], window: int, calendar: TradingCalendar, at) -> dict:
    """Same baseline, session grid and window for plots, returns and correlations."""
    if window not in WINDOWS:
        raise ValueError("观察区间必须是 20、60、120 或 250 个交易日")
    if not calendar.known(at.date()):
        raise ValueError("交易日历未覆盖当前日期，暂不能对齐走势")
    expected = calendar.completed(at)
    dates = [d for d in calendar.days if d <= expected][-(window + 1):]
    records = [valid_rows(p, expected, calendar, at) for p in payloads]
    maps = [{d: r["close"] for d, r in rows.items()} for rows in records]
    usable = [m for m in maps if any(d in m for d in dates)]
    common = set(dates)
    for mapping in usable:
        common.intersection_update(mapping)
    start = min(common) if usable and common else None
    axis = [d for d in dates if start and d >= start]
    series, daily_returns = [], []
    for asset, payload, mapping, by_date in zip(assets, payloads, maps, records):
        base = mapping.get(start)
        values = [(mapping[d] / base - 1) * 100 if base and d in mapping else None for d in axis]
        returns = {d: mapping[d] / mapping[previous] - 1
                   for previous, d in zip(axis, axis[1:]) if previous in mapping and d in mapping}
        daily_returns.append(returns)
        latest = max(mapping, default=None)
        observed = sum(v is not None for v in values)
        state = "unavailable" if not base else "stale" if latest < expected else "partial" if observed < len(axis) else "ready"
        # Close-only legacy caches remain valid for lines, never synthetic candles.
        candles = [candle(by_date[d]) if d in mapping else None for d in axis]
        amounts = [providers.number(by_date[d].get("amount")) if d in mapping else None for d in axis]
        amounts = [v if v is not None and v >= 0 else None for v in amounts]
        series.append({**asset, "values": values, "prices": [mapping.get(d) for d in axis],
                       "candles": candles, "amounts": amounts, "amount_unit": "CNY",
                       "candle_points": sum(v is not None for v in candles),
                       "amount_points": sum(v is not None for v in amounts),
                       "state": state, "points": observed, "expected_points": len(axis),
                       "last_date": latest, "last_close": mapping.get(latest),
                       "change_pct": values[-1] if values else None,
                       "source": payload.get("source"), "fetched_at": payload.get("fetched_at"),
                       "adjustment": payload.get("adjustment", "index"), "error": payload.get("error")})
    pairs = []
    for i, left in enumerate(assets):
        for j in range(i + 1, len(assets)):
            days = sorted(set(daily_returns[i]) & set(daily_returns[j]))
            a = [daily_returns[i][d] for d in days]
            b = [daily_returns[j][d] for d in days]
            reason, value = None, None
            if len(days) < 20:
                reason = "有效共同日收益不足 20 个"
            elif np.std(a) < 1e-12 or np.std(b) < 1e-12:
                reason = "至少一条序列没有足够的波动"
            else:
                value = float(np.corrcoef(a, b)[0, 1])
                if not math.isfinite(value):
                    value, reason = None, "无法计算有效相关性"
            pairs.append({"left": left["id"], "right": assets[j]["id"], "value": value,
                          "samples": len(days), "reason": reason})
    warnings = []
    if not start:
        warnings.append("所选对象在该区间内没有共同有效起点；等待数据补齐或调整选择。")
    elif start > dates[0]:
        warnings.append(f"部分对象历史较短，共同起点缩短至 {start}。")
    if len(usable) < len(assets):
        warnings.append("部分对象暂无可用日线，图表仅对齐已加载对象；补齐后共同起点可能变化。")
    return {"generated_at": iso(at), "window": window, "requested_start": dates[0] if dates else None,
            "expected_through": expected, "baseline_date": start, "dates": axis, "series": series,
            "pairs": pairs, "warnings": warnings,
            "method": "共同起点为 0%；股票为前复权收盘价变化，板块和指数为公布点位变化。仅用已完成日线，不是实际投资收益。相关性为同一交易日收益率的 Pearson 系数，缺失日不填补。"}


def compare(ids: list[str], window=60, refresh=False) -> dict:
    assets = selected_assets(ids)
    at, calendar = now(), TradingCalendar()
    payloads = [cached_history(a) for a in assets]
    result = calculate(assets, payloads, window, calendar, at)
    for asset, payload, series in zip(assets, payloads, result["series"]):
        series["refreshing"] = request_refresh(asset, payload, result["expected_through"], at, refresh)
    result["refreshing"] = any(s["refreshing"] for s in result["series"])
    return result
