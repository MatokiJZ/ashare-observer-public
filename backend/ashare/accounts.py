"""Independent ledgers share observations, never cash, orders or drawdown state."""
from copy import deepcopy

from . import storage
from .core import config, dt, iso, paper_account


def experimental_regime(pool, universe, at, current=True):
    base = pool.get("regime", {})
    result = {"asof": iso(at), "state": "halt", "label": "暂停新增", "exposure": 0}
    if (not current or pool.get("coverage", {}).get("ratio", 0) < .9 or universe.get("error")
            or not universe.get("fetched_at") or not 0 <= (at-dt(universe["fetched_at"])).total_seconds() <= 240):
        return {**result, "reason": "当日报告、日线覆盖或最新行情未就绪"}
    values = [r["change_pct"] for r in universe.get("items", []) if r.get("change_pct") is not None and (r.get("amount") or 0) > 0]
    trend = base.get("trend_breadth")
    if not values or trend is None:
        return {**result, "reason": "市场宽度缺失"}
    breadth = sum(v > 0 for v in values) / len(values)
    result.update(breadth=breadth, trend_breadth=trend)
    minimum = min(breadth, trend)
    if minimum < .1:
        return {**result, "reason": "上涨或趋势宽度低于 10%，暂停新增"}
    state = "weak" if minimum < .3 else "cautious" if minimum < .5 else "normal"
    cfg = config()
    result.update(state=state, label={"weak": "弱市小仓位", "cautious": "谨慎参与", "normal": "正常参与"}[state],
                  exposure=cfg[state + "_exposure"], reason="适度进取：市场宽度分档控制仓位")
    from .context import overseas_overlay
    return overseas_overlay(result, storage.snapshot("markets"), at)


def eligible_candidates(candidates, regime):
    rows = deepcopy(candidates)
    for row in rows:
        row["strategy_version"] = config()["strategy_version"]
        row.setdefault("model_version", "rules")
        if regime.get("state") == "weak" and not row.get("sector_strong"):
            row.setdefault("risks", []).append("弱市仅参与强势行业")
            row["trade_eligible"] = False
    return rows


def all_views():
    from . import broker
    result = []
    for name, label in [("baseline", "旧策略对照"), ("experiment", "适度进取实验")]:
        with paper_account(name):
            cfg = config()
            result.append({**broker.view(), "account_id": name, "account_label": label,
                "daily_selection": storage.snapshot("daily_selection"), "execution": storage.snapshot("intraday"),
                "regime": storage.snapshot("live_regime"),
                "risk_policy": {k: cfg[k] for k in ("trade_risk", "drawdown_review", "drawdown_halt", "normal_exposure", "cautious_exposure")}})
    return result
