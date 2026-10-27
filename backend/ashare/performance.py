"""After-the-fact evaluation of observed fills; never a source of entry signals."""
from collections import defaultdict
from .core import config, dt, quote_fresh
from . import storage


def fill_context(order, at):
    benchmark = next((q for q in storage.snapshot("markets").get("items", []) if q["symbol"] == "sh000300"), {})
    if not quote_fresh(benchmark, at) or not (benchmark.get("price") or 0) > 0:
        benchmark = None
    return {"candidate": order.get("candidate"), "signal_id": order["id"], "decision_at": order["created_at"],
            "position_observation":order.get("position_observation"),
            "price_plan": order.get("price_plan") or order.get("candidate", {}).get("price_plan"), "exit_intent": order.get("exit_intent"),
            "benchmark": benchmark, "cost_model": {k:config()[k] for k in ["commission_rate", "commission_minimum", "stamp_tax_sell", "transfer_fee", "slippage_bps"]}}


def metrics(trips):
    pnls = [t["net_pnl"] for t in trips]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    excess = [t["excess_return_pct"] for t in trips if t["excess_return_pct"] is not None]
    return {"closed_trades": len(trips), "wins": len(wins), "losses": len(losses), "flat": sum(x == 0 for x in pnls),
            "win_rate_pct": 100*len(wins)/len(trips) if trips else None,
            "net_pnl": round(sum(pnls), 2), "expectancy": round(sum(pnls)/len(pnls), 2) if pnls else None,
            "payoff_ratio": (sum(wins)/len(wins)) / (-sum(losses)/len(losses)) if wins and losses else None,
            "profit_factor": sum(wins)/-sum(losses) if losses else None,
            "benchmark_samples": len(excess), "mean_excess_pct": sum(excess)/len(excess) if excess else None}


def summarize(trades, contexts, curve, calendar):
    lots = defaultdict(list)
    closed, unmatched = [], 0
    for trade in trades:
        code = trade["symbol"]
        ctx = contexts.get(trade["id"], {})
        if trade["side"] == "buy":
            candidate = ctx.get("candidate") or {}
            lots[code].append({"id": trade["id"], "code": code, "name": candidate.get("name", code),
                "entry_time": trade["ts"], "entry_price": trade["price"], "quantity": trade["quantity"], "remaining": trade["quantity"],
                "entry_fees": trade["fees"], "exit_fees": 0., "exit_gross": 0., "exit_reasons": [], "exit_times": [],
                "strategy": trade["strategy"], "strategy_version": candidate.get("strategy_version", "未记录的旧版本"),
                "selection_version": candidate.get("selection_version", "未记录"), "report_id": candidate.get("report_id"),
                "model_version": candidate.get("model_version", "rules"),
                "candidate_version": candidate.get("candidate_version"), "benchmark_entry": (ctx.get("benchmark") or {}).get("price"),
                "price_plan": ctx.get("price_plan"), "entry_reason":trade["reason"], "exit_triggers":[],
                "observed_high":None, "observed_low":None,
                "benchmark_exit_gross": 0., "benchmark_complete": True})
            continue
        left = trade["quantity"]
        for lot in lots[code]:
            if not left or not lot["remaining"]:
                continue
            quantity = min(left, lot["remaining"])
            lot["exit_fees"] += trade["fees"] * quantity / trade["quantity"]
            lot["exit_gross"] += trade["price"] * quantity
            lot["remaining"] -= quantity
            left -= quantity
            lot["exit_reasons"].append(trade["reason"])
            lot["exit_times"].append(trade["ts"])
            observed = ctx.get("position_observation") or {}
            for key, choose in (("observed_high",max),("observed_low",min)):
                if observed.get(key) is not None:
                    lot[key] = choose(lot[key] if lot[key] is not None else observed[key],observed[key])
            if ctx.get("exit_intent"):
                lot["exit_triggers"].append(ctx["exit_intent"])
            bp = (ctx.get("benchmark") or {}).get("price")
            if bp:
                lot["benchmark_exit_gross"] += bp * quantity
            else:
                lot["benchmark_complete"] = False
            if lot["remaining"] == 0:
                cost = lot["entry_price"] * lot["quantity"] + lot["entry_fees"]
                net = lot["exit_gross"] - lot["exit_fees"] - cost
                benchmark_return = ((lot["benchmark_exit_gross"]/lot["quantity"]/lot["benchmark_entry"]-1)*100
                                    if lot["benchmark_complete"] and lot["benchmark_entry"] else None)
                entry_date, exit_date = lot["entry_time"][:10], trade["ts"][:10]
                favorable = (lot["observed_high"]/lot["entry_price"]-1)*100 if lot["observed_high"] is not None else None
                adverse = (lot["observed_low"]/lot["entry_price"]-1)*100 if lot["observed_low"] is not None else None
                gross = lot["exit_gross"]-lot["entry_price"]*lot["quantity"]
                findings = []
                if gross > 0 and net <= 0:
                    findings.append("证据：交易费用抵消了价差收益；滑点已含在成交价中")
                if adverse is not None and adverse < -2:
                    findings.append("待检验：入场时点可优化，持仓期间已观测到超过 2% 的不利变化；不能据此认定买入过早")
                if favorable is not None and favorable-gross/(lot["entry_price"]*lot["quantity"])*100 > 2:
                    findings.append("待检验：持仓出现利润回吐，可比较止盈规则；高点不是可实现利润")
                closed.append({**lot, "exit_time": trade["ts"], "exit_price": lot["exit_gross"]/lot["quantity"],
                    "observed_favorable_pct":favorable,"observed_adverse_pct":adverse,"findings":findings,
                    "net_pnl": round(net, 2), "net_return_pct": net/cost*100,
                    "total_fees": round(lot["entry_fees"]+lot["exit_fees"], 2),
                    "holding_sessions": calendar.elapsed(entry_date, exit_date) if calendar.known(dt(trade["ts"]).date()) and entry_date in calendar.days else None,
                    "benchmark_return_pct": benchmark_return, "excess_return_pct": net/cost*100-benchmark_return if benchmark_return is not None else None})
        unmatched += left
    groups = defaultdict(list)
    for trip in closed:
        groups[(trip["strategy"], trip["strategy_version"], trip["selection_version"], trip["model_version"])].append(trip)
    peak, max_dd = config()["capital"], 0.
    for point in curve:
        peak = max(peak, point["value"])
        max_dd = max(max_dd, 1-point["value"]/peak)
    sessions = len({p["ts"][:10] for p in curve})
    return {**metrics(closed), "closed": list(reversed(closed))[:200],
            "by_strategy": [{"strategy": key[0], "strategy_version": key[1], "selection_version": key[2], "model_version":key[3], **metrics(rows)} for key,rows in groups.items()],
            "max_drawdown_pct": max_dd*100, "observed_sessions": sessions, "unmatched_sell_quantity": unmatched,
            "sample_status": "可开始复核，仍需独立样本外验证" if sessions >= 60 and len(closed) >= 30 else "积累样本中：至少观察 60 个交易日和 30 笔完整交易后再复核；这不是统计有效性的保证",
            "method": "一笔买入到全部卖完计一笔完整交易；部分卖出不重复计胜率。盈亏含双边费用，滑点已在成交价中。同期沪深 300 使用成交时可见报价，缺失则不估算。模型须通过独立样本外及并行模拟门槛才可接管新开仓。"}
