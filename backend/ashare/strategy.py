from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .core import TZ, TradingCalendar, config, dt, iso


def mainboard(code: str) -> bool:
    return len(code) == 6 and code.isdigit() and code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def features(frame: pd.DataFrame, asof: datetime) -> dict | None:
    if frame.empty:
        return None
    frame = frame.copy()
    if "available_at" not in frame:
        return None
    frame = frame[pd.to_datetime(frame.available_at, utc=True) <= asof].sort_values("date")
    if len(frame) < 120:
        return None
    required = ["open", "high", "low", "close", "volume", "amount"]
    if frame[required].tail(120).isna().any().any() or (frame["close"].tail(120) <= 0).any():
        return None
    close = frame.close.astype(float)
    true_range = pd.concat([frame.high - frame.low, (frame.high - close.shift()).abs(), (frame.low - close.shift()).abs()], axis=1).max(axis=1)
    ma10, ma20, ma60 = [float(close.tail(n).mean()) for n in (10, 20, 60)]
    atr = float(true_range.tail(14).mean())
    low20, high20 = float(frame.low.tail(20).min()), float(frame.high.tail(20).max())
    corporate_gap = False
    if "change_pct" in frame:
        # Mismatch between raw close-to-close and quoted return often indicates a corporate action.
        corporate_gap = bool(((close.pct_change() * 100 - frame.change_pct).abs().tail(60) > 1).any())
    if "corporate_action_event" in frame:
        corporate_gap = corporate_gap or bool(frame.corporate_action_event.tail(60).fillna(False).any())
    if "source" in frame:
        secondary = frame.source.fillna("").str.contains("sina").tail(60)
        checked = frame.get("corporate_action_checked", pd.Series(False, index=frame.index)).tail(60).fillna(False).astype(bool)
        corporate_gap = corporate_gap or bool((secondary & ~checked).any())
    return {"close": float(close.iloc[-1]), "ma10": ma10, "ma20": ma20, "ma60": ma60, "atr": atr,
            "median_amount20": float(frame.amount.tail(20).median()),
            "return5": float(close.iloc[-1] / close.iloc[-6] - 1),
            "return20": float(close.iloc[-1] / close.iloc[-21] - 1),
            "return60": float(close.iloc[-1] / close.iloc[-61] - 1),
            "volatility20": float(close.pct_change().tail(20).std() * np.sqrt(252)),
            "high20": high20, "low20": low20,
            "position20": (float(close.iloc[-1]) - low20) / (high20 - low20) if high20 > low20 else 0.5,
            "high60": float(frame.high.tail(60).max()), "low60": float(frame.low.tail(60).min()),
            "high250": float(frame.high.tail(250).max()) if len(frame) >= 250 else None,
            "low250": float(frame.low.tail(250).min()) if len(frame) >= 250 else None,
            "volume_ratio": float(frame.volume.iloc[-1] / frame.volume.tail(20).mean()) if frame.volume.tail(20).mean() else 0,
            "pullback": bool((frame.low.tail(3) <= ma10).any() and (frame.close.tail(3) >= ma20).all()),
            "corporate_action_unverified": corporate_gap,
            "asof": str(frame.date.iloc[-1])[:10], "sessions": len(frame)}


def market_regime(universe: dict, coverage: float, usable_features: list[dict], asof: datetime) -> dict:
    if universe.get("error") or not universe.get("fetched_at") or not 0 <= (asof - dt(universe["fetched_at"])).total_seconds() <= 20 * 3600:
        return {"state": "halt", "label": "暂停新增", "reason": "全市场行情缺失或过期", "breadth": None, "exposure": 0}
    quotes = [q for q in universe.get("items", []) if q.get("change_pct") is not None]
    breadth = sum(q["change_pct"] > 0 for q in quotes) / len(quotes) if quotes else None
    trend_breadth = sum(f["close"] > f["ma20"] for f in usable_features) / len(usable_features) if usable_features else None
    if coverage < 0.9 or breadth is None or trend_breadth is None:
        return {"state": "halt", "label": "暂停新增", "reason": "日线覆盖不足 90%，正在积累数据", "breadth": breadth, "trend_breadth": trend_breadth, "exposure": 0}
    if breadth < 0.3 or trend_breadth < 0.3:
        return {"state": "halt", "label": "暂停新增", "reason": "上涨家数或趋势宽度低于 30%", "breadth": breadth, "trend_breadth": trend_breadth, "exposure": 0}
    cautious = breadth < 0.5 or trend_breadth < 0.5
    return {"state": "cautious" if cautious else "normal", "label": "谨慎参与" if cautious else "正常参与",
            "reason": "上涨家数与 20 日趋势宽度联合判断", "breadth": breadth, "trend_breadth": trend_breadth,
            "exposure": config()["cautious_exposure" if cautious else "normal_exposure"]}


def live_regime(universe: dict, baseline: dict, at: datetime) -> dict:
    current = {**baseline, "asof": iso(at)}
    if not universe.get("fetched_at") or (at - dt(universe["fetched_at"])).total_seconds() > 240 or universe.get("error"):
        return {**current, "state": "halt", "label": "暂停新增", "reason": "盘中全市场行情过期或更新失败", "exposure": 0}
    rows = [r for r in universe.get("items", []) if r.get("change_pct") is not None and (r.get("amount") or 0) > 0]
    if not rows:
        return {**current, "state": "halt", "label": "暂停新增", "reason": "缺少盘中市场宽度", "exposure": 0}
    breadth = sum(r["change_pct"] > 0 for r in rows) / len(rows)
    current["breadth"] = breadth
    if breadth < .3:
        current.update(state="halt", label="暂停新增", reason="盘中上涨家数跌至 30% 以下", exposure=0)
    elif breadth < .5 and current.get("state") == "normal":
        current.update(state="cautious", label="谨慎参与", reason="盘中上涨家数不足一半", exposure=config()["cautious_exposure"])
    return current


def screen(rows: list[dict], histories: dict[str, pd.DataFrame], metadata: dict,
           financials: dict, calendar: TradingCalendar, asof: datetime, limit: int | None = None) -> tuple[list[dict], dict]:
    settings = config()
    eligible = [r for r in rows if mainboard(str(r.get("code", ""))) and "ST" not in r.get("name", "").upper() and "退" not in r.get("name", "")]
    expected_date = calendar.completed(asof)
    breakdown = {"missing": 0, "short": 0, "invalid": 0, "stale": 0}
    examples = {k: [] for k in breakdown}
    prepared = []
    excluded = []
    def reject(row, reason):
        excluded.append({"code":row["code"],"name":row.get("name",row["code"]),"reason":reason})
    for row in rows:
        if mainboard(str(row.get("code",""))) and ("ST" in row.get("name", "").upper() or "退" in row.get("name", "")):
            reject(row,"风险警示或退市股票")
    fs = []
    for row in eligible:
        code = row["code"]
        frame = histories.get(code, pd.DataFrame())
        try:
            f = features(frame, asof)
        except (ValueError, TypeError, KeyError):
            f = None
        if not f or f["asof"] != expected_date:
            visible = frame[pd.to_datetime(frame.available_at, utc=True, errors="coerce") <= asof] if not frame.empty and "available_at" in frame else frame
            reason = "missing" if frame.empty else "short" if len(visible) < 120 else "stale" if f else "invalid"
            breakdown[reason] += 1
            reject(row, {"missing":"缺少日线","short":"有效日线不足 120 根","stale":"日线未更新","invalid":"日线字段异常"}[reason])
            if len(examples[reason]) < 10:
                examples[reason].append({"code": code, "name": row.get("name"), "rows": len(visible), "last_date": str(visible.date.max())[:10] if not visible.empty else None})
            continue
        fs.append(f)
        if f["median_amount20"] < settings["min_median_amount"] or f["corporate_action_unverified"]:
            reject(row, "公司行动未核实" if f["corporate_action_unverified"] else "成交额低于门槛")
            continue
        if not (f["close"] > f["ma20"] > f["ma60"] and f["atr"] > 0):
            reject(row, "未形成均线多头趋势")
            continue
        strategy = "pullback" if f["pullback"] and f["volume_ratio"] < 1 else "breakout"
        if strategy == "breakout" and f["close"] < f["high20"] * 0.95:
            reject(row, "未达到回踩或接近突破的条件")
            continue
        meta = metadata.get(code, {})
        fin = financials.get(code, {})
        financial_rows = [r for r in fin.get("reports", []) if dt(r["published_at"]) <= asof]
        risks = []
        if not meta.get("listing_date") or not meta.get("sector"):
            risks.append("上市日期或行业资料待核实")
        elif sum(meta["listing_date"] <= d <= asof.date().isoformat() for d in calendar.days) < settings["min_listing_sessions"]:
            reject(row, "上市交易日不足 120 日")
            continue
        if not financial_rows:
            risks.append("缺少带披露日期的财务数据")
        else:
            fin_current = financial_rows[0]
            if fin_current.get("profit_growth") is not None and fin_current["profit_growth"] < -50:
                risks.append("最近一期利润同比下滑超过 50%")
            if fin_current.get("operating_cash_per_share") is not None and fin_current["operating_cash_per_share"] < 0:
                risks.append("最近一期每股经营现金流为负")
        prepared.append({"code": code, "name": row["name"], "sector": meta.get("sector", "行业待核实"),
                         "strategy": strategy, "strategy_label": "趋势回踩" if strategy == "pullback" else "放量突破",
                         "features": f, "financials": financial_rows[:1], "price": f["close"], "valuation": {"pe": row.get("pe"), "basis": "动态市盈率，行业间不直接比较"},
                         "risks": risks, "status": "观察 / 资料待核实" if risks else "等待盘中确认",
                         "trade_eligible": not risks, "reasons": ["收盘价高于 20 日均线，20 日均线高于 60 日均线", "近 20 日成交额中位数达到 1 亿元", "缩量回踩后观察恢复" if strategy == "pullback" else "接近 20 日高点，等待突破和量能确认"],
                         "entry": "完成的 5 分钟 K 线站上 VWAP 及前一根高点" if strategy == "pullback" else "完成的 5 分钟 K 线突破 20 日高点，成交量达到历史同时段的 1.5 倍",
                         "invalidation": "跌破 20 日均线、财务风险或行情过期；未触发则不买入",
                         "stop_distance": round(2 * f["atr"], 4), "max_sessions": settings["max_holding_sessions"]})
    if prepared:
        df = pd.DataFrame([{"code": c["code"], "momentum": c["features"]["return20"], "liquidity": c["features"]["median_amount20"], "volatility": -c["features"]["volatility20"]} for c in prepared]).set_index("code")
        scores = (df.rank(pct=True) * pd.Series({"momentum": 0.5, "liquidity": 0.3, "volatility": 0.2})).sum(axis=1) * 100
        for c in prepared:
            c["score"] = round(float(scores[c["code"]]), 1)
        prepared.sort(key=lambda c: (-c["score"], c["code"]))
    for row in prepared[limit or settings["max_candidates"]:]:
        reject(row, "符合技术条件但排序未进入研究池")
    return prepared[:limit or settings["max_candidates"]], {"total": len(eligible), "covered": len(fs), "ratio": len(fs) / len(eligible) if eligible else 0, "features": fs, "excluded":excluded,
           "expected_date": expected_date, "checked_at": iso(asof), "breakdown": breakdown, "examples": examples,
           "definition": "当前有成交的沪深主板非 ST 股票中，至少有 120 根有效日线且更新至最近已完成交易日的比例；不足 120 根单列，不伪造补齐。"}


def intraday_signal(candidate: dict, frame: pd.DataFrame, at: datetime) -> dict:
    result = {"code": candidate["code"], "state": "waiting", "label": "尚未触发", "asof": iso(at)}
    if not candidate.get("trade_eligible"):
        return {**result, "state": "blocked", "label": "风险资料待核实"}
    if frame.empty:
        return {**result, "state": "blocked", "label": "缺少分钟行情"}
    f = frame.copy()
    times = pd.to_datetime(f.date, utc=True)
    f = f[times <= at].sort_values("date")
    today = f[f.date.str.startswith(at.date().isoformat())]
    if len(today) < 3:
        return {**result, "label": "等待至少三根完整 5 分钟 K 线"}
    last, previous = today.iloc[-1], today.iloc[-2]
    age = (at - dt(last.date)).total_seconds()
    if age < 0 or age > 90:
        return {**result, "state": "blocked", "label": "分钟行情过期"}
    feature = candidate["features"]
    if last.close < feature["ma20"]:
        return {**result, "state": "invalid", "label": "已跌破 20 日均线"}
    volume = float(today.volume.sum())
    vwap = float(today.amount.sum()) / volume if volume else None
    if not vwap or float(last.close) > feature["close"] + feature["atr"]:
        return {**result, "label": "价格偏离过大或成交资料缺失"}
    threshold = float(previous.high)
    matched = last.close > threshold and last.close > vwap
    ratio = None
    if candidate["strategy"] == "breakout":
        slot = dt(last.date).strftime("%H:%M")
        past = f[(~f.date.str.startswith(at.date().isoformat())) & f.date.map(lambda x: dt(x).strftime("%H:%M") == slot)].tail(10)
        if len(past) < 5 or past.volume.median() <= 0:
            return {**result, "label": "同时间段历史分钟不足 5 日"}
        ratio = float(last.volume / past.volume.median())
        threshold = feature["high20"]
        matched = last.close > threshold and ratio >= 1.5 and last.close > vwap
    result.update({"bar_end": last.date, "vwap": vwap, "volume_ratio": ratio, "trigger_price": threshold})
    if matched:
        result.update({"state": "triggered", "label": "条件已触发，等待后续行情模拟成交",
                       "id": f"{candidate['code']}:{candidate['strategy']}:{last.date}", "created_at": iso(at),
                       "expires_at": iso(at + timedelta(minutes=10)), "candidate": candidate})
    return result


def aligned_correlations(histories: dict[str, list[dict]], anchor: str, windows=(20, 60, 120)) -> list[dict]:
    """At each A-share close use only prices whose exchange timestamp was already observable."""
    anchor_rows = histories.get(anchor, [])
    if not anchor_rows:
        return []
    left = pd.DataFrame(anchor_rows).sort_values("time")
    left["time"] = pd.to_datetime(left.time, utc=True)
    left["anchor_return"] = left.close.pct_change()
    results = []
    for symbol, rows in histories.items():
        if symbol == anchor or not rows:
            continue
        right = pd.DataFrame(rows).sort_values("time")
        right["time"] = pd.to_datetime(right.time, utc=True)
        right["foreign_return"] = right.close.pct_change()
        # Distinct sessions only; never forward-fill returns across a holiday.
        joined = pd.merge_asof(left, right[["time", "foreign_return"]].rename(columns={"time": "observed_time"}), left_on="time", right_on="observed_time", direction="backward", tolerance=pd.Timedelta("3d"))
        joined = joined.drop_duplicates("observed_time").dropna(subset=["anchor_return", "foreign_return"])
        for window in windows:
            tail = joined.tail(window)
            corr = tail.anchor_return.corr(tail.foreign_return) if len(tail) >= window else None
            results.append({"symbol": symbol, "window": window, "samples": len(tail), "correlation": float(corr) if corr is not None and np.isfinite(corr) else None})
    return results
