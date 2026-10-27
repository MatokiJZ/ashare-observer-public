"""Restricted point-in-time daily research, kept separate from the online ledger."""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from .broker import fees
from .core import TZ, config
from .strategy import features


def daily_baseline(frame: pd.DataFrame, strategy="breakout", cost_multiplier=1.0) -> dict:
    """A single-security diagnostic, not a historical whole-universe stock-selection claim.

    Signal after close; next-session open fill, T+1 sales and conservative handling of
    ambiguous daily limit bars. Does not infer minute fills from daily OHLC.
    """
    cfg = config()
    if frame.empty or len(frame) < 130:
        return {"status": "insufficient", "reason": "至少需要 130 根带可见时间的日线", "trades": []}
    frame = frame.sort_values("date").reset_index(drop=True)
    if str(frame.date.iloc[0]) < cfg["fees_effective_from"]:
        return {"status": "unsupported", "reason": "当前费用模型未覆盖该历史时期", "trades": []}
    cash = float(cfg["capital"])
    position = None
    trades, curve = [], []
    pending_buy = False
    stop = 0
    slippage = cfg["slippage_bps"] / 10000 * cost_multiplier
    pending_sell = False
    for i in range(120, len(frame)):
        row = frame.iloc[i]
        previous = frame.iloc[i - 1]
        # Refuse possible limit-locked bars and discontinuous raw corporate-action prices.
        limit_locked = row.high == row.low or row.volume <= 0
        corporate = abs(float(row.open / previous.close - 1)) > 0.11
        if pending_sell and position and not limit_locked and not corporate:
            price = float(row.open) * (1 - slippage)
            fee = fees("sell", position["quantity"], price, cfg) * cost_multiplier
            cash += position["quantity"] * price - fee
            pnl = (price - position["price"]) * position["quantity"] - position["fee"] - fee
            trades.append({"entry_date": position["date"], "exit_date": str(row.date), "quantity": position["quantity"], "pnl": pnl, "holding_sessions": i - position["index"], "entry_price": position["price"], "exit_price": price})
            position = None
            pending_sell = False
        if pending_buy and not position and not limit_locked and not corporate:
            price = float(row.open) * (1 + slippage)
            quantity = math.floor(min(cash * .2 / price, cash * .005 / max(stop, .01), row.volume * .01) / 100) * 100
            fee = fees("buy", quantity, price, cfg) * cost_multiplier
            if quantity > 0 and quantity * price + fee <= cash:
                cash -= quantity * price + fee
                position = {"date": str(row.date), "index": i, "quantity": quantity, "price": price, "fee": fee, "stop": price - stop}
            pending_buy = False
        at = datetime.fromisoformat(str(row.date)[:10] + "T16:15:00").replace(tzinfo=TZ)
        f = features(frame.iloc[:i + 1], at)
        if position:
            if i > position["index"] and (row.close < position["stop"] or i - position["index"] >= 5):
                pending_sell = True
        elif f and not f["corporate_action_unverified"] and f["median_amount20"] >= cfg["min_median_amount"]:
            trend = f["close"] > f["ma20"] > f["ma60"]
            condition = f["pullback"] and f["volume_ratio"] < 1 if strategy == "pullback" else row.close > frame.high.iloc[i-20:i].max() and f["volume_ratio"] >= 1.5
            pending_buy = bool(trend and condition)
            stop = 2 * f["atr"]
        curve.append({"date": str(row.date), "equity": cash + (position["quantity"] * row.close if position else 0)})
    series = pd.Series([p["equity"] for p in curve])
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return {"status": "exploratory", "strategy": strategy, "cost_multiplier": cost_multiplier, "trades": trades,
            "curve": curve, "return_pct": (series.iloc[-1] / cfg["capital"] - 1) * 100,
            "max_drawdown_pct": float((1 - series / series.cummax()).max() * 100),
            "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else None,
            "profit_factor": gross_win / gross_loss if gross_loss else None,
            "benchmark_return_pct": (float(frame.close.iloc[-1]) / float(frame.close.iloc[120]) - 1) * 100,
            "limitations": ["单股日线基线；不是全股票池选股回测", "不含分钟入场、新闻、历史行业成分或退市样本", "历史数据修订及复权事件需额外核实", "未完成 60 日模拟与独立样本外验收，不宣称策略有效"]}


def purged_splits(dates: list[str], train=252, test=63, embargo=5):
    """Forward-only windows; labels of up to five sessions cannot overlap the test."""
    splits = []
    for start in range(train + embargo, len(dates) - test + 1, test):
        splits.append({"train": dates[start - embargo - train:start - embargo], "test": dates[start:start + test], "embargo_sessions": embargo})
    return splits
