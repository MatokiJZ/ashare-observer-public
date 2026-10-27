"""CPU LightGBM experiments; immutable model files and local promotion gates.

Historical daily experiments are exploratory (current roster / revised history).
They cannot activate a model without forward, observed same-risk paper evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from . import storage
from .core import (TradingCalendar, atomic_json, config, data_dir, digest, dt, iso,
                   now, paper_account, read_json)

FEATURES = ["return5", "return20", "return60", "ma10_distance", "ma20_distance", "ma60_distance",
            "atr_ratio", "volatility20", "position20", "volume_ratio", "amount_log",
            "market_return20", "market_volatility20", "relative20", "breadth", "trend_breadth"]
SCHEMA = "daily-price-volume-v1"
MODEL_ID = re.compile(r"\d{4}-\d{2}-\d{2}-\d{6}-[a-f0-9]{8}")
LIMITATIONS = ["当前留存股票池，历史退市/ST/行业成分不完整，存在幸存者偏差",
               "历史行情可能修订；首版不使用事后新闻、财报、研报或订单特征",
               "日线下一开盘研究不代表分钟建议价一定能成交；必须通过在线并行模拟"]
_CACHE = {}


def execution_policy():
    cfg = config()
    value = {"experiment":cfg.get("experiment"), "schema":SCHEMA,
             **{k:cfg[k] for k in ["price_plan_version","selection_version","regular_stop_pct","regular_take_profit_pct",
                 "max_holding_sessions","daily_picks","max_positions","max_stock_weight","max_sector_weight",
                 "commission_rate","commission_minimum","stamp_tax_sell","transfer_fee","slippage_bps",
                 "max_bar_participation","min_median_amount","atr_stop_multiple"]}}
    return digest(json.dumps(value,sort_keys=True).encode())


def benchmark_frame():
    rows = storage.snapshot("index_histories").get("items", {}).get("sh000300", []) or storage.snapshot("index_history").get("items", [])
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["date"] = frame.get("date", frame.get("time")).astype(str).str[:10]
    return frame.drop_duplicates("date").sort_values("date").set_index("date")


def security_features(frame, days):
    frame = frame.drop_duplicates("date").set_index("date").sort_index().reindex(days)
    close = frame.close.astype(float)
    tr = pd.concat([frame.high-frame.low, (frame.high-close.shift()).abs(), (frame.low-close.shift()).abs()], axis=1).max(axis=1)
    f = pd.DataFrame(index=frame.index)
    for n in (5, 20, 60):
        f[f"return{n}"] = close/close.shift(n)-1
    for n in (10, 20, 60):
        f[f"ma{n}_distance"] = close/close.rolling(n).mean()-1
    f["atr"] = tr.rolling(14).mean()
    f["atr_ratio"] = f.atr/close
    f["volatility20"] = close.pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)
    low, high = frame.low.rolling(20).min(), frame.high.rolling(20).max()
    f["position20"] = (close-low)/(high-low).replace(0, np.nan)
    f["volume_ratio"] = frame.volume/frame.volume.rolling(20).mean()
    f["amount_log"] = np.log1p(frame.amount.rolling(20).median())
    f["liquid"] = frame.amount.rolling(20).median() >= config()["min_median_amount"]
    f["advancing"] = close > close.shift()
    f["above_ma20"] = close > close.rolling(20).mean()
    f["eligible"] = (close > close.rolling(20).mean()) & (close.rolling(20).mean() > close.rolling(60).mean()) & f.liquid
    # Match the existing rule candidate pool using only each day's visible bars.
    pullback = (pd.concat([frame.low.shift(i) <= close.rolling(10).mean() for i in range(3)], axis=1).any(axis=1)
                & pd.concat([close.shift(i) >= close.rolling(20).mean() for i in range(3)], axis=1).all(axis=1)
                & (f.volume_ratio < 1))
    f["eligible"] &= (pullback | (close >= high*.95)) & (f.atr > 0)
    known = frame.get("corporate_action_checked", pd.Series(False, index=frame.index)).eq(True)
    events = frame.get("corporate_action_event", pd.Series(False, index=frame.index)).eq(True)
    if "change_pct" in frame:
        events |= (close.pct_change(fill_method=None)*100-frame.change_pct).abs() > 1
    f["verified"] = known & ~events
    f["eligible"] &= f.verified.rolling(60).sum().eq(60) & close.rolling(120).count().eq(120)
    for name in ("open", "close", "high", "low", "volume"):
        f[name] = frame[name]
    f["previous_close"] = close.shift()
    f["previous_volume"] = frame.volume.shift()
    return f


def dataset(at=None, save_latest=True):
    at = at or now()
    cal = TradingCalendar()
    end = cal.completed(at)
    bench = benchmark_frame()
    if bench.empty:
        raise ValueError("缺少沪深 300 历史，不能训练超额收益模型")
    days = [d for d in cal.days if config()["fees_effective_from"] <= d <= end]
    bench = bench.reindex(days)
    bclose = bench.close.astype(float)
    market = pd.DataFrame({"market_return20": bclose/bclose.shift(20)-1,
                           "market_volatility20": bclose.pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)}, index=days)
    h = config()["learning"]["label_sessions"]
    # Feature close on T; reference entry T+1 open, exit T+1+h close (T+1 respected).
    bopen = bench["open"] if "open" in bench else None
    if bopen is None:
        raise ValueError("基准缺少开盘价，不能伪造与股票同期间的收益")
    market["benchmark_label"] = bclose.shift(-1-h)/bopen.shift(-1)-1
    from .strategy import mainboard
    codes = [p.stem for p in (data_dir()/"bars/day").glob("*.parquet") if mainboard(p.stem)]
    frames = storage.bars_many(codes)
    parts = []
    for code, frame in frames.items():
        if "available_at" not in frame:
            continue
        frame = frame[pd.to_datetime(frame.available_at, utc=True) <= at].copy()
        frame["date"] = frame.date.astype(str).str[:10]
        f = security_features(frame, days)
        f["code"] = code
        f["raw_label"] = f.close.shift(-1-h)/f.open.shift(-1)-1
        # Labels cannot bridge suspensions, missing sessions or unverified actions.
        valid_future = pd.concat([f.verified.shift(-i).eq(True) & f.close.shift(-i).notna() for i in range(1, h+2)], axis=1).all(axis=1)
        f.loc[~valid_future, "raw_label"] = np.nan
        parts.append(f)
    if not parts:
        raise ValueError("没有足够的有效历史股票数据")
    all_rows = pd.concat(parts).rename_axis("date").reset_index()
    breadth = all_rows[all_rows.close.notna()].groupby("date").agg(breadth=("advancing", "mean"), trend_breadth=("above_ma20", "mean"))
    all_rows = all_rows.join(market, on="date").join(breadth, on="date")
    all_rows["relative20"] = all_rows.return20-all_rows.market_return20
    all_rows["label"] = all_rows.raw_label-all_rows.benchmark_label
    end_dates = {d: (days[i+h+1] if i+h+1 < len(days) else None) for i, d in enumerate(days)}
    all_rows["label_end"] = all_rows.date.map(end_dates)
    for col in FEATURES + ["raw_label", "label"]:
        all_rows[col] = all_rows[col].astype("float32")
    if save_latest:
        latest = all_rows[all_rows.date == end].replace([np.inf, -np.inf], np.nan)
        columns = ["code", "date", *FEATURES]
        rows = json.loads(latest[columns].to_json(orient="records"))
        storage.save_snapshot("learning_features", {"schema": SCHEMA, "asof": end, "generated_at": iso(at), "items": {r["code"]: r for r in rows}})
    return all_rows.replace([np.inf, -np.inf], np.nan)


def temporal_split(dates, cfg=None):
    cfg = cfg or config()["learning"]
    train, valid, test, gap = (cfg[k] for k in ["train_sessions", "validation_sessions", "test_sessions", "embargo_sessions"])
    required = train+valid+test+2*gap
    dates = sorted(set(dates))
    if len(dates) < required:
        return {"ready": False, "available_sessions": len(dates), "required_sessions": required}
    dates = dates[-required:]
    a, b = train+gap, train+gap+valid+gap
    return {"ready": True, "train": dates[:train], "validation": dates[a:a+valid], "test": dates[b:], "embargo_sessions": gap}


def daily_portfolio(frame, predictions, cost_multiplier=1., model=True):
    """Matched daily proxy: close decision, next open fills, stop/TP evaluated at close.

    Never infer the intraday ordering of high/low. Halted exits stay invested.
    """
    from .broker import fees
    with paper_account("experiment"):
        cfg = config()
    cash, peak, max_dd = float(cfg["capital"]), float(cfg["capital"]), 0.
    positions, pending, trips, curve = {}, [], [], []
    unverified_codes = set()
    halt, reduced = False, False
    rows = frame.copy()
    rows["prediction"] = predictions
    for day, section in rows.groupby("date", sort=True):
        by_code = section.set_index("code")
        for code, p in list(positions.items()):
            if code not in by_code.index:
                unverified_codes.add(code)
                continue
            q = by_code.loc[code]
            if not q.verified or not np.isfinite(q.close):
                p["uncertain"] = True
                unverified_codes.add(code)
                continue
            p["mark"] = float(q.open)
            locked = q.high == q.low or q.open <= q.previous_close*.905
            if p.get("exit") and not locked:
                quantity = min(p["quantity"], math.floor(q.previous_volume*.01/100)*100) if np.isfinite(q.previous_volume) else 0
                if quantity <= 0:
                    continue
                price = float(q.open)*(1-cfg["slippage_bps"]/10000*cost_multiplier)
                fee = fees("sell", quantity, price, cfg)*cost_multiplier
                cash += quantity*price-fee
                p["exit_gross"] += quantity*price-fee
                p["quantity"] -= quantity
                if not p["quantity"]:
                    trips.append(p["exit_gross"]-p["cost"])
                    del positions[code]
                    continue
        equity = cash+sum(p["quantity"]*p["mark"] for p in positions.values())
        peak = max(peak, equity)
        dd = 1-equity/peak
        max_dd = max(max_dd, dd)
        halt |= dd >= .12
        reduced = dd >= .08 or (reduced and dd >= .06)
        for item in pending:
            code = item["code"]
            if halt or code in positions or len(positions) >= 5 or code not in by_code.index:
                continue
            q = by_code.loc[code]
            if not q.verified or not np.isfinite(q.open) or q.high == q.low or not q.previous_close*.905 < q.open < q.previous_close*1.095:
                continue
            # Historical sector membership is missing: use a conservative 35% total cap.
            # This cannot violate the 35% single-sector limit even if all belong to one sector.
            exposure = min(.35, item["exposure"])*(.5 if reduced else 1)
            available = min(equity*.2, cash, equity*exposure-(equity-cash))
            price = float(q.open)*(1+cfg["slippage_bps"]/10000*cost_multiplier)
            stop_distance = min(price*.05, item["atr"]*2)
            budget = equity*.0075*(.5 if reduced else 1)*item.get("risk_multiplier", 1)
            if not np.isfinite(q.previous_volume):
                continue
            qty = max(0, math.floor(min(available/price, budget/max(stop_distance,.01), q.previous_volume*.01)/100)*100)
            while qty and (qty*price+fees("buy", qty, price, cfg)*cost_multiplier > cash or
                          qty*stop_distance+(fees("buy",qty,price,cfg)+fees("sell",qty,price-stop_distance,cfg))*cost_multiplier > budget):
                qty -= 100
            if qty:
                cost = qty*price+fees("buy",qty,price,cfg)*cost_multiplier
                cash -= cost
                positions[code] = {"quantity":qty, "mark":price, "stop":price-stop_distance,
                    "tp":price+min(price*.08,item["atr"]*3), "holding":-1,"cost":cost,"exit_gross":0.,"uncertain":False}
        # Today's close affects marking and tomorrow's exits only, never today's size.
        for code,p in positions.items():
            if code in by_code.index:
                q = by_code.loc[code]
                if q.verified and np.isfinite(q.close):
                    p["mark"] = float(q.close)
                    p["holding"] += 1
                    p["exit"] = p.get("exit",False) or q.close <= p["stop"] or q.close >= p["tp"] or p["holding"] >= 5
        equity = cash+sum(p["quantity"]*p["mark"] for p in positions.values())
        peak = max(peak, equity)
        max_dd = max(max_dd, 1-equity/peak)
        curve.append({"date":day, "equity":equity})
        valid = section[section.eligible & section[FEATURES].notna().all(axis=1)].copy()
        if model:
            cost = (2*cfg["commission_rate"]+cfg["stamp_tax_sell"]+2*cfg["transfer_fee"]+2*cfg["slippage_bps"]/10000)*cost_multiplier
            valid = valid[valid.prediction > cost]
            valid = valid.sort_values(["prediction", "code"], ascending=[False, True])
        else:
            valid["score"] = valid.return20.rank(pct=True)*.5+valid.amount_log.rank(pct=True)*.3-valid.volatility20.rank(pct=True)*.2
            valid = valid.sort_values(["score", "code"], ascending=[False, True])
        minimum = min(section.breadth.iloc[0], section.trend_breadth.iloc[0])
        # Weak-market industry qualification cannot be reconstructed historically.
        exposure = .8 if minimum >= .5 else .5 if minimum >= .3 else 0.
        pending = [{"code":r.code, "atr":float(r.atr), "exposure":exposure,
                    "risk_multiplier":.5 if model and getattr(r,"downside",0) < -.05 else 1.} for r in valid.head(3).itertuples()]
    return {"return_pct": (curve[-1]["equity"]/cfg["capital"]-1)*100 if curve else None,
            "max_drawdown_pct": max_dd*100, "closed_trades":len(trips), "net_pnl":sum(trips),
            "unverified_positions": len(unverified_codes), "curve":curve,
            "method":"收盘决策，下一开盘尝试成交；T+1、费用、滑点及封板限制；容量按前一日量的 1% 作研究假设，不能替代在线成交量验证；缺少历史行业时总仓位额外限制 35%。"}


def historical_gate(metrics):
    a, b, stress = (metrics.get(k, {}) for k in ("model", "rules", "double_cost"))
    return (all(isinstance(x.get("return_pct"), (int,float)) for x in (a,b,stress)) and
            a["return_pct"] > max(0., b["return_pct"]) and stress["return_pct"] > 0 and
            a.get("closed_trades",0) > 0 and a.get("max_drawdown_pct",100) <= 12 and
            not any(x.get("unverified_positions") for x in (a,b,stress)))


def train(at=None, force=False):
    at = at or now()
    cfg = config()["learning"]
    week = f"{at.isocalendar().year}-W{at.isocalendar().week:02d}"
    previous = storage.snapshot("training_status")
    if previous.get("week") == week and previous.get("state") == "complete" and not force:
        return previous
    started = time.monotonic()
    status = {"state":"running", "started_at":iso(at), "week":week, "recipe":cfg["recipe"]}
    storage.save_snapshot("training_status", status)
    try:
        from lightgbm import LGBMRegressor, early_stopping
        rows = dataset(at)
        usable = rows[rows.eligible & rows[FEATURES+["label", "raw_label"]].notna().all(axis=1)]
        split = temporal_split(usable.date.tolist())
        if not split["ready"]:
            status.update(state="insufficient", split=split, reason="成熟标签交易日不足，继续补充历史，不启用模型")
        else:
            train_rows, validation, test = [usable[usable.date.isin(split[k])] for k in ("train", "validation", "test")]
            if train_rows.label_end.max() >= split["validation"][0] or validation.label_end.max() >= split["test"][0]:
                raise ValueError("未来标签穿越隔离边界")
            params = dict(n_estimators=250, num_leaves=15, max_depth=5, learning_rate=.04,
                          min_child_samples=200, reg_lambda=10., colsample_bytree=.8, n_jobs=2, verbosity=-1, random_state=19)
            fitted = []
            def enforce_budget(env):
                if time.monotonic()-started > cfg["max_train_seconds"]:
                    raise TimeoutError("训练超过时间预算，保留已有版本")
            for target, objective in [("label","regression"),("raw_label","quantile")]:
                if time.monotonic()-started > cfg["max_train_seconds"]:
                    raise TimeoutError("训练时间预算已用完，保留已有版本")
                model = LGBMRegressor(**params, objective=objective, **({"alpha":.2} if objective=="quantile" else {}))
                model.fit(train_rows[FEATURES], train_rows[target], eval_set=[(validation[FEATURES],validation[target])], callbacks=[early_stopping(20,verbose=False),enforce_budget])
                fitted.append(model)
            # Test dates are never used to fit/early-stop or select hyperparameters.
            test_all = rows[rows.date.isin(split["test"])].copy()
            test_all["downside"] = fitted[1].predict(test_all[FEATURES])
            predictions = fitted[0].predict(test_all[FEATURES])
            metrics = {"model":daily_portfolio(test_all,predictions), "rules":daily_portfolio(test_all,predictions,model=False),
                       "double_cost":daily_portfolio(test_all,predictions,cost_multiplier=2)}
            signature = digest(json.dumps({"recipe":cfg, "split":split, "schema":SCHEMA},sort_keys=True).encode())[:8]
            model_id = at.strftime("%Y-%m-%d-%H%M%S")+"-"+signature
            root = data_dir()/"models"/model_id
            root.mkdir(parents=True, exist_ok=True)
            files = {}
            for name, model in zip(("return.txt","risk.txt"),fitted):
                body = model.booster_.model_to_string().encode()
                target = root/name
                if target.exists() and target.read_bytes() != body:
                    raise ValueError("拒绝覆盖模型版本")
                target.write_bytes(body)
                files[name] = digest(body)
            policy = {"learning":cfg,"features":FEATURES,"params":params,"risk":config().get("experiment"),
                      "execution_policy":execution_policy(),
                      "entry_exit":{k:config()[k] for k in ["price_plan_version","selection_version","regular_stop_pct","regular_take_profit_pct","max_holding_sessions"]}}
            manifest = {"id":model_id, "schema":SCHEMA, "features":FEATURES, "recipe":cfg["recipe"],
                "policy_fingerprint":digest(json.dumps(policy,sort_keys=True).encode()),
                "execution_policy":execution_policy(),
                "generated_at":iso(at), "trained_through":split["train"][-1], "test_through":split["test"][-1],
                "split":split, "files":files, "metrics":metrics, "historical_pass":historical_gate(metrics),
                "limitations":LIMITATIONS, "samples":{k:len(v) for k,v in [("train",train_rows),("validation",validation),("test",test)]},
                "params":params, "runtime_seconds":round(time.monotonic()-started,2), "automatic_trading_approved":False}
            atomic_json(root/"manifest.json",manifest)
            status.update(state="complete", model_id=model_id, historical_pass=manifest["historical_pass"],
                          reason="历史检验通过，等待在线并行验证" if manifest["historical_pass"] else "历史检验未达标，保留规则策略")
    except Exception as exc:
        status.update(state="failed", error=str(exc)[:350])
    status["finished_at"] = iso()
    storage.save_snapshot("training_status",status)
    atomic_json(data_dir()/"training_runs"/(at.strftime("%Y-%m-%d-%H%M%S")+".json"),status)
    return status


def manifests():
    return [read_json(p) for p in sorted((data_dir()/"models").glob("*/manifest.json"),reverse=True) if MODEL_ID.fullmatch(p.parent.name)]


def load_model(model_id):
    if not MODEL_ID.fullmatch(model_id):
        raise ValueError("模型编号无效")
    root = data_dir()/"models"/model_id
    m = read_json(root/"manifest.json",{})
    if m.get("id") != model_id or m.get("schema") != SCHEMA or m.get("features") != FEATURES or not m.get("historical_pass"):
        raise ValueError("模型未通过历史检验或特征版本不兼容")
    if m.get("execution_policy") != execution_policy():
        raise ValueError("风险或交易规则已变化，需要重新验证模型")
    for name in ("return.txt","risk.txt"):
        if digest((root/name).read_bytes()) != m.get("files",{}).get(name):
            raise ValueError("模型文件完整性校验失败")
    if model_id not in _CACHE:
        from lightgbm import Booster
        _CACHE.clear()
        _CACHE[model_id] = (m, Booster(model_file=str(root/"return.txt")), Booster(model_file=str(root/"risk.txt")))
    return _CACHE[model_id]


def latest_candidate():
    for m in manifests():
        if m.get("historical_pass"):
            try:
                load_model(m["id"])
                return m
            except Exception:
                continue
    return None


def rank(candidates, model_id, at):
    rows = deepcopy(candidates)
    if not model_id:
        for c in rows:
            c["model_version"] = "rules"
        return rows
    m, model, risk = load_model(model_id)
    features = storage.snapshot("learning_features")
    if features.get("schema") != SCHEMA or features.get("asof") != TradingCalendar().completed(at):
        raise ValueError("模型特征不是最近已完成交易日，暂停模型新增")
    for c in rows:
        f = features.get("items",{}).get(c["code"])
        c["model_version"] = model_id
        if not f or any(f.get(k) is None for k in FEATURES):
            c["trade_eligible"] = False
            c.setdefault("risks",[]).append("模型输入缺失")
            continue
        vector = pd.DataFrame([{k:f[k] for k in FEATURES}])
        predicted, downside = float(model.predict(vector,num_threads=2)[0]), float(risk.predict(vector,num_threads=2)[0])
        # Cost estimate is disclosed, not a probability or a cash P&L promise.
        cost = 2*config()["commission_rate"]+config()["stamp_tax_sell"]+2*config()["transfer_fee"]+2*config()["slippage_bps"]/10000
        c["model_prediction"] = {"excess_return_pct":predicted*100, "after_estimated_cost_pct":(predicted-cost)*100,
                                 "downside_quantile20_pct":downside*100, "horizon_sessions":3,
                                 "note":"3 日研究超额收益估计；不是绝对盈利或上涨概率，最低佣金以实际撮合为准"}
        c["model_risk_multiplier"] = .5 if downside < -.05 else 1.
        if predicted <= cost:
            c["trade_eligible"] = False
            c.setdefault("risks",[]).append("模型估计超额收益未覆盖假设成本")
    return sorted(rows,key=lambda c:(not c.get("trade_eligible"),-c.get("model_prediction",{}).get("after_estimated_cost_pct",-999),c["code"]))


def shadow_accounts(at=None):
    at = at or now()
    model = latest_candidate()
    if not model:
        return None
    control = storage.snapshot("model_control")
    recipe = model.get("policy_fingerprint",model["recipe"])
    if control.get("shadow_recipe") != recipe:
        epoch = hashlib.sha256((recipe+iso(at)).encode()).hexdigest()[:12]
        control.update(shadow_recipe=recipe, shadow_epoch=epoch, shadow_started_at=iso(at))
        storage.save_snapshot("model_control",control)
    epoch = control["shadow_epoch"]
    return {"rules":"shadow-rules-"+epoch, "model":"shadow-model-"+epoch, "model_id":model["id"]}


def promotion_gate(model, rules, paired_sessions, historical):
    a,b = model.get("performance",{}), rules.get("performance",{})
    cfg = config()["learning"]
    checks = {
        "historical":bool(historical), "sessions":paired_sessions >= cfg["minimum_observed_sessions"],
        "trades":a.get("closed_trades",0) >= cfg["minimum_closed_trades"],
        "positive":model.get("return_pct",0) > 0 and a.get("net_pnl",0) > 0,
        "beats_rules":model.get("return_pct",0) > rules.get("return_pct",0),
        "drawdown":a.get("max_drawdown_pct",100) <= 12 and not model.get("drawdown_halted",False)}
    return {"passed":all(checks.values()), "checks":checks, "paired_sessions":paired_sessions,
            "closed_trades":a.get("closed_trades",0)}


def evaluate_promotion(at=None):
    from . import broker
    at = at or now()
    pair = shadow_accounts(at)
    control = storage.snapshot("model_control")
    if not pair:
        return control
    views, session_sets = {}, []
    for kind in ("rules","model"):
        with paper_account(pair[kind]):
            views[kind] = broker.view()
            with storage.database() as con:
                # Multiple spaced observations; one isolated tick is not a valid day.
                session_sets.append({r["day"] for r in con.execute("SELECT * FROM sessions")
                    if r["observations"] >= 10 and (dt(r["last_at"])-dt(r["first_at"])).total_seconds() >= 1800
                    and r["day"] <= TradingCalendar().completed(at)})
    manifest = load_model(pair["model_id"])[0]
    gate = promotion_gate(views["model"],views["rules"],len(session_sets[0]&session_sets[1]),manifest["historical_pass"])
    control.update(gate=gate, evaluated_at=iso(at), comparison={k:{"return_pct":v["return_pct"],"performance":{n:v["performance"].get(n) for n in ("closed_trades","net_pnl","max_drawdown_pct")}} for k,v in views.items()})
    with paper_account("experiment"):
        locked = broker.account().get("drawdown_halted",False)
    if gate["passed"] and not locked and control.get("active_model") != pair["model_id"]:
        transition = {"at":iso(at),"previous":control.get("active_model"),"next":pair["model_id"],"gate":gate}
        control.update(previous_model=control.get("active_model"),active_model=pair["model_id"],activated_at=iso(at))
        atomic_json(data_dir()/"model_transitions"/(at.strftime("%Y-%m-%d-%H%M%S")+".json"),transition)
    storage.save_snapshot("model_control",control)
    return control


def active_model():
    control = storage.snapshot("model_control")
    for key in ("active_model","previous_model"):
        identifier = control.get(key)
        if identifier:
            try:
                load_model(identifier)
                if key == "previous_model":
                    control.update(active_model=identifier,rollback_at=iso(),rollback_reason="当前模型损坏或不兼容")
                    storage.save_snapshot("model_control",control)
                return identifier
            except Exception as exc:
                control["load_error"] = str(exc)[:200]
    if control.get("active_model"):
        control.update(active_model=None,rollback_at=iso(),rollback_reason="没有可用的已验证模型，退回规则")
        storage.save_snapshot("model_control",control)
    return None


def status():
    return {"training":storage.snapshot("training_status"), "control":storage.snapshot("model_control"),
            "cloud":storage.snapshot("training_cloud_status"),
            "feature_status":{k:storage.snapshot("learning_features").get(k) for k in ("asof","generated_at","schema")},
            "models":[{k:m.get(k) for k in ("id","recipe","generated_at","historical_pass","samples","metrics","limitations")} for m in manifests()[:20]],
            "minimums":config()["learning"], "method":"每日复盘、每周训练；历史通过后同风险在线并行验证，达标才接管实验账户新开仓。",
            "limitations":LIMITATIONS}
