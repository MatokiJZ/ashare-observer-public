"""Local-only paper ledger; no broker credentials or live order endpoints."""
from __future__ import annotations

import json
import math
from datetime import datetime, time

from .core import PAPER_ACCOUNT, TradingCalendar, config, dt, in_session, iso, quote_fresh
from .storage import database, records


def initial_state():
    capital = config()["capital"]
    return {"cash": capital, "positions": [], "peak_equity": capital, "pending": [],
            "last_tick": None, "enabled": True, "realized_pnl": 0.0, "drawdown_halted": False}


def _state(con):
    row = con.execute("SELECT value FROM kv WHERE key='portfolio'").fetchone()
    return json.loads(row[0]) if row else initial_state()


def _save(con, state):
    con.execute("INSERT OR REPLACE INTO kv VALUES ('portfolio',?)", (json.dumps(state, ensure_ascii=False),))


def fees(side: str, quantity: int, price: float, cfg: dict) -> float:
    gross = quantity * price
    return round(max(cfg["commission_minimum"], gross * cfg["commission_rate"]) + gross * cfg["transfer_fee"] + (gross * cfg["stamp_tax_sell"] if side == "sell" else 0), 2)


def valuation(state):
    return state["cash"] + sum(p["quantity"] * p["mark"] for p in state["positions"])


def account():
    with database() as con:
        return _state(con)


def view():
    from .performance import summarize
    with database() as con:
        con.execute("BEGIN")
        state = _state(con)
        trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY ts,id")]
        contexts = {r["id"]: json.loads(r["payload"]) for r in con.execute("SELECT * FROM trade_context")}
        curve = [dict(r) for r in con.execute("SELECT * FROM equity ORDER BY ts")]
    equity = valuation(state)
    return {**state, "equity": equity, "return_pct": (equity / config()["capital"] - 1) * 100,
            "drawdown_pct": max(0, 1 - equity / state["peak_equity"]) * 100,
            "trades": list(reversed(trades))[:200], "curve": curve[-1000:],
            "performance": summarize(trades, contexts, curve, TradingCalendar()),
            "mode": "仅本地在线期间模拟；离线不成交", "fees_are_assumptions": True}


def toggle(enabled: bool):
    with database() as con:
        con.execute("BEGIN IMMEDIATE")
        state = _state(con)
        state["enabled"] = enabled
        if not enabled:
            state["pending"] = []
        _save(con, state)


def quote_capacity(order, quote, previous, at, cfg):
    """Only consecutive observed cumulative-volume increments after the signal count."""
    if not previous or not quote_fresh(previous, at, cfg["quote_stale_seconds"]):
        return 0
    start, end = dt(previous["market_time"]), dt(quote["market_time"])
    if not dt(order["created_at"]) <= start < end or start.date() != end.date():
        return 0
    if previous.get("cumulative_volume_shares") is None or quote.get("cumulative_volume_shares") is None:
        return 0
    volume = max(0, quote["cumulative_volume_shares"] - previous["cumulative_volume_shares"])
    return math.floor(volume * cfg["max_bar_participation"] / 100) * 100


def event_exit(position, events, at):
    from .context import event_context
    # Conservative risk rules; ordinary dividends and buyback announcements are not bad-news exits.
    terms = ("立案", "退市", "重大违法", "无法表示意见", "否定意见", "债务逾期", "业绩预亏", "破产", "风险警示", "合同终止", "终止合同", "订单取消")
    for e in event_context(events, at)["items"]:
        matches = e.get("code") == position["code"] or position["code"] in e["title"] or (len(position["name"]) >= 3 and position["name"] in e["title"])
        observed = max(dt(e["published_at"]), dt(e["first_seen_at"]))
        entry = dt(position.get("entry_time", position["entry_date"]+"T00:00:00+08:00"))
        if matches and observed >= entry and any(word in e["title"] for word in terms):
            return {"kind":"event", "reason":"新增重大风险线索，保守退出："+e["title"], "event":e}
    return None


def record_block(con, order, reason, at):
    if order.get("side","buy") != "buy":
        return
    con.execute("""INSERT INTO observations VALUES (?,?,?,?,?,?,?) ON CONFLICT(day,code,reason)
        DO UPDATE SET last_at=excluded.last_at,count=count+1,payload=excluded.payload""",
        (at.date().isoformat(),order["code"],reason,iso(at),iso(at),1,json.dumps({"order_id":order["id"]},ensure_ascii=False)))


def tick(quotes: list[dict], signals: list[dict], regime: dict, calendar: TradingCalendar,
         at: datetime, bars_by_code: dict | None = None, events: list[dict] | None = None):
    cfg = config()
    if not calendar.is_session(at.date()) or not in_session(at):
        return
    quotes_by_code = {q["code"]: q for q in quotes}
    with database() as con:
        con.execute("BEGIN IMMEDIATE")
        state = _state(con)
        gap = not state["last_tick"] or (at - dt(state["last_tick"])).total_seconds() > cfg["quote_stale_seconds"]
        if gap:
            state["pending"] = []  # Never execute a pre-sleep order after reconnect.
            state["previous_quotes"] = {}
        for position in state["positions"]:
            q = quotes_by_code.get(position["code"], {})
            if quote_fresh(q, at, cfg["quote_stale_seconds"]) and (q.get("price") or 0) > 0:
                position.update(mark=q["price"], mark_time=q["market_time"])
                position["observed_high"] = max(position.get("observed_high",position["entry_price"]),q["price"])
                position["observed_low"] = min(position.get("observed_low",position["entry_price"]),q["price"])
        equity = valuation(state)
        state["peak_equity"] = max(equity, state["peak_equity"])
        drawdown = 1 - equity / state["peak_equity"]
        cfg, regime = apply_drawdown(state, cfg, regime, drawdown, at)
        if drawdown >= cfg["drawdown_halt"]:
            state["drawdown_halted"] = True
        if drawdown >= cfg["drawdown_review"]:
            con.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)", (f"dd:{at.date()}", iso(at), "risk", f"模拟组合回撤达到检查阈值；达到 {cfg['drawdown_halt']:.0%} 将锁定新增仓位"))
        active = state["enabled"] and cfg["paper_auto_enabled"]
        if not active:
            state["pending"] = []
        # Orders can only consume quotes and completed bars observed after creation.
        remaining = []
        blocked_codes = {s["code"] for s in signals if s.get("state") in {"blocked", "invalid"}}
        for order in state["pending"] if active else []:
            if con.execute("SELECT 1 FROM trades WHERE id=?", (order["id"],)).fetchone():
                continue
            if order.get("side", "buy") == "buy" and order["code"] in blocked_codes:
                record_block(con,order,"风险条件变化，取消未成交买入",at)
                continue
            if dt(order["expires_at"]) < at:
                record_block(con,order,"入场意图已过期",at)
                continue
            code = order["code"]
            q = quotes_by_code.get(code, {})
            if not quote_fresh(q, at, cfg["quote_stale_seconds"]) or dt(q["market_time"]) <= dt(order["created_at"]):
                record_block(con,order,"缺少信号产生后的新鲜报价",at)
                remaining.append(order)
                continue
            if order.get("execution") == "observed_volume":
                capacity = quote_capacity(order, q, state.get("previous_quotes", {}).get(code), at, cfg)
            else:
                frame = (bars_by_code or {}).get(code)
                if frame is None or frame.empty:
                    remaining.append(order)
                    continue
                completed = frame[frame.date.map(lambda x: dt(order["created_at"]) < dt(x) <= at)]
                if completed.empty or (at - dt(completed.iloc[-1].date)).total_seconds() > 90:
                    remaining.append(order)
                    continue
                capacity = math.floor(float(completed.iloc[-1].volume) * cfg["max_bar_participation"] / 100) * 100
            if capacity < 100:
                record_block(con,order,"已观测成交量不足一手撮合容量",at)
                remaining.append(order)
                continue
            side = order.get("side", "buy")
            position = next((p for p in state["positions"] if p["code"] == code), None)
            price = q.get("ask" if side == "buy" else "bid")
            if not price or not q.get("limit_up") or not q.get("limit_down"):
                record_block(con,order,"买卖盘或涨跌停价格缺失",at)
                remaining.append(order)
                continue
            if side == "buy" and (price >= q["limit_up"] or price <= q["limit_down"]):
                record_block(con,order,"价格在涨跌停边界，不能确认可成交",at)
                remaining.append(order)
                continue
            if side == "sell" and price <= q["limit_down"]:
                remaining.append(order)
                continue
            price *= 1 + cfg["slippage_bps"] / 10000 * (1 if side == "buy" else -1)
            price = round(price, 2)
            if not q["limit_down"] <= price <= q["limit_up"]:
                remaining.append(order)
                continue
            if side == "buy":
                c = order["candidate"]
                plan = c.get("price_plan", {})
                if plan:
                    bought = {r[0] for r in con.execute("SELECT symbol FROM trades WHERE side='buy' AND substr(ts,1,10)=?", (at.date().isoformat(),))}
                    if code in bought or len(bought) >= cfg["daily_picks"]:
                        continue
                    if plan.get("trade_date") != at.date().isoformat() or plan.get("status") != "ready":
                        continue
                    if not plan["buy_low"] <= price <= plan["buy_high"]:
                        record_block(con,order,"计入滑点后的买入价不在建议区间",at)
                        remaining.append(order)
                        continue
                if "ST" in q.get("name", "").upper() or "退" in q.get("name", "") or not c.get("trade_eligible", True):
                    continue
                if position or len(state["positions"]) >= cfg["max_positions"] or regime["state"] == "halt" or state["drawdown_halted"]:
                    record_block(con,order,"已有持仓、仓位数量上限或市场/账户暂停新增",at)
                    continue
                if price < c["features"]["ma20"] or price > c["price"] + c["features"]["atr"]:
                    continue
                sector_value = sum(p["quantity"] * p["mark"] for p in state["positions"] if p["sector"] == c["sector"])
                invested = equity - state["cash"]
                allowance = min(equity * cfg["max_stock_weight"], equity * cfg["max_sector_weight"] - sector_value,
                                equity * regime["exposure"] - invested, state["cash"] - cfg["commission_minimum"])
                stop = max(plan["stop"], round(price*(1-cfg["regular_stop_pct"]),2)) if plan else price-c["stop_distance"]
                stop_distance = price-stop
                if stop_distance <= 0:
                    continue
                risk_budget = equity * cfg["trade_risk"] * min(1., max(0., c.get("model_risk_multiplier", 1.)))
                quantity = max(0, math.floor(min(allowance / price, risk_budget / stop_distance, capacity) / 100) * 100)
                fee = fees("buy", quantity, price, cfg)
                while quantity > 0 and (quantity * price + fee > state["cash"] or
                    quantity * (stop_distance + price * cfg["slippage_bps"] / 10000) + fee + fees("sell", quantity, max(.01, price - stop_distance), cfg) > risk_budget):
                    quantity -= 100
                    fee = fees("buy", quantity, price, cfg)
                if quantity == 0:
                    record_block(con,order,"现金、行业/总仓位或单笔风险预算不足一手",at)
                    continue
                state["cash"] -= quantity * price + fee
                state["positions"].append({"code": code, "name": c["name"], "sector": c["sector"], "quantity": quantity,
                    "entry_price": price, "entry_fee": fee, "entry_date": at.date().isoformat(),
                    "entry_time": iso(at), "entry_report": c.get("report_id"), "candidate_version": c.get("candidate_version"),
                    "strategy_version": c.get("strategy_version", cfg["strategy_version"]), "selection_version": c.get("selection_version", "legacy"),
                    "model_version": c.get("model_version", "rules"),
                    "observed_high":price, "observed_low":price,
                    "exit_config": {k:cfg[k] for k in ["atr_stop_multiple", "max_holding_sessions"]},
                    "stop": max(0.01, stop), "ma20": c["features"]["ma20"], "atr": c["features"]["atr"],
                    "price_plan": plan, "take_profit": min(plan["take_profit"],round(price*(1+cfg["regular_take_profit_pct"]),2)) if plan else None,
                    "high_water": price, "strategy": c["strategy"], "mark": price, "mark_time": q["market_time"]})
            else:
                if not position or position["entry_date"] >= at.date().isoformat():
                    continue
                order["position_observation"] = {k:position.get(k) for k in ("observed_high","observed_low")}
                quantity = min(position["quantity"], capacity)
                fee = fees("sell", quantity, price, cfg)
                allocated_entry_fee = position["entry_fee"] * quantity / position["quantity"]
                state["realized_pnl"] += quantity * (price - position["entry_price"]) - fee - allocated_entry_fee
                state["cash"] += quantity * price - fee
                position["quantity"] -= quantity
                position["entry_fee"] -= allocated_entry_fee
                if position["quantity"] == 0:
                    state["positions"].remove(position)
            con.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)", (order["id"], iso(at), code, side, quantity, price, fee, order.get("reason", "后续完整 K 线与新报价确认"), order.get("candidate", {}).get("strategy", position["strategy"] if position else "")))
            from .performance import fill_context
            context = {**fill_context(order, at), "fill_quote":q, "capacity_shares":capacity,
                       "previous_quote":state.get("previous_quotes", {}).get(code), "execution":order.get("execution", "completed_bar")}
            con.execute("INSERT OR IGNORE INTO trade_context VALUES (?,?)", (order["id"], json.dumps(context, ensure_ascii=False)))
            equity = valuation(state)
        state["pending"] = remaining
        # Exit intents are made at the observed quote, then filled on a later observation.
        if active:
            from datetime import timedelta
            for p in state["positions"]:
                q = quotes_by_code.get(p["code"], {})
                if not quote_fresh(q, at, cfg["quote_stale_seconds"]):
                    continue
                p["high_water"] = max(p["high_water"], q["price"])
                exit_cfg = p.get("exit_config", cfg)
                p["stop"] = max(p["stop"], p["high_water"] - exit_cfg["atr_stop_multiple"] * p["atr"])
                holding = calendar.elapsed(p["entry_date"], at.date().isoformat())
                intent = event_exit(p, events or [], at)
                if q["price"] <= max(p["stop"],p["ma20"]):
                    intent = intent or {"kind":"stop", "reason":"价格触及止损或趋势失效位"}
                elif p.get("take_profit") and q["price"] >= p["take_profit"]:
                    intent = intent or {"kind":"take_profit", "reason":"达到冻结止盈 / 目标价"}
                elif holding >= exit_cfg["max_holding_sessions"]:
                    intent = intent or {"kind":"time", "reason":"达到最长持有期"}
                if intent and not p.get("exit_intent"):
                    p["exit_intent"] = {**intent,"triggered_at":iso(at),"trigger_price":q["price"],"stop":p["stop"],"take_profit":p.get("take_profit")}
                    con.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)", (f"exit:{p['code']}:{iso(at)}",iso(at),"paper",intent["reason"]+("；T+1 等待可卖" if p["entry_date"] == at.date().isoformat() else "；等待后续可成交报价")))
                if p.get("exit_intent") and p["entry_date"] < at.date().isoformat() and not any(o["code"] == p["code"] for o in state["pending"]):
                    state["pending"].append({"id": f"sell:{p['code']}:{iso(at)}", "code": p["code"], "side": "sell", "created_at": iso(at), "expires_at": iso(at + timedelta(minutes=10)), "reason":p["exit_intent"]["reason"],
                        "exit_intent":p["exit_intent"], "price_plan":p.get("price_plan"), "execution":"observed_volume"})
            # No new entries during the first 15 minutes or after 14:45.
            if time(9, 45) <= at.time().replace(tzinfo=None) <= time(14, 45) and regime["state"] != "halt" and not state["drawdown_halted"]:
                for signal in signals:
                    if signal.get("state") != "triggered" or not 0 <= (at - dt(signal["created_at"])).total_seconds() < 2:
                        continue
                    inserted = con.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?)", (signal["id"], iso(at), json.dumps(signal, ensure_ascii=False))).rowcount
                    if inserted and not any(p["code"] == signal["code"] for p in state["positions"] + state["pending"]):
                        state["pending"].append(signal)
        previous = state.get("previous_quotes", {})
        state["previous_quotes"] = {code:(previous[code] if code in previous and dt(previous[code]["market_time"]) > dt(q["market_time"]) else q)
                                   for code,q in quotes_by_code.items() if quote_fresh(q,at,cfg["quote_stale_seconds"])}
        state["last_tick"] = iso(at)
        con.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (iso(at), valuation(state), state["cash"]))
        if regime.get("data_ready",True) and regime.get("breadth") is not None and any(quote_fresh(q, at) for q in quotes):
            con.execute("""INSERT INTO sessions VALUES (?,?,?,1) ON CONFLICT(day)
                DO UPDATE SET last_at=excluded.last_at,observations=observations+1""", (at.date().isoformat(), iso(at), iso(at)))
        _save(con, state)


def apply_drawdown(state, cfg, regime, drawdown, at):
    """Hysteresis: recover only on the next observed trading date, never reset peak."""
    cfg, regime = dict(cfg), dict(regime)
    if PAPER_ACCOUNT.get() == "baseline":
        return cfg, regime
    if drawdown >= cfg["drawdown_review"]:
        state["risk_reduced"] = True
        state.pop("recovery_date", None)
    elif state.get("risk_reduced"):
        if drawdown < cfg["drawdown_recover"]:
            since = state.setdefault("recovery_date", at.date().isoformat())
            if since < at.date().isoformat():
                state["risk_reduced"] = False
                state.pop("recovery_date", None)
        else:
            state.pop("recovery_date", None)
    if state.get("risk_reduced"):
        cfg["trade_risk"] *= .5
        regime["exposure"] *= .5
    return cfg, regime
