"""Bounded, replaceable public data adapters. No fabricated fallback rows."""
from __future__ import annotations

import contextlib
import io
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .core import TZ, config, iso, now

SOURCE_EM = "https://quote.eastmoney.com/"
SOURCE_TX = "https://gu.qq.com/"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value.strip()
    return value


def number(value, default=None):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default


def tx_symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def http(url, **kwargs):
    headers = kwargs.pop("headers", {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    response = requests.get(url, timeout=(5, 15), headers=headers, **kwargs)
    response.raise_for_status()
    return response


def tencent_quotes(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    response = http("https://qt.gtimg.cn/q=" + ",".join(symbols))
    response.encoding = "gbk"
    quotes = []
    for line in response.text.splitlines():
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].replace("v_", "")
        fields = line.split('="', 1)[1].rstrip('";').split("~")
        if len(fields) < 46:
            continue
        try:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            stamp = None
        quote = {
            "symbol": key, "code": fields[2], "name": fields[1],
            "price": number(fields[3]), "previous_close": number(fields[4]),
            "open": number(fields[5]), "volume": number(fields[6]),
            "cumulative_volume_shares": number(fields[6]) * 100 if number(fields[6]) is not None else None,
            "bid": number(fields[9]), "ask": number(fields[19]),
            "change_pct": number(fields[32]), "high": number(fields[33]),
            "low": number(fields[34]), "amount": (number(fields[37], 0) * 10000),
            "turnover": number(fields[38]), "pe": number(fields[39]),
            "market_time": iso(stamp) if stamp else None,
            "fetched_at": iso(), "source": "腾讯财经", "source_url": SOURCE_TX + key,
            "delay_status": "timestamped" if stamp else "unknown",
            "currency": "HKD" if key.startswith("hk") else "CNY",
        }
        if len(fields) > 48:
            quote["limit_up"] = number(fields[47])
            quote["limit_down"] = number(fields[48])
        quotes.append(quote)
    if not quotes:
        raise ValueError("腾讯行情返回空数据")
    return quotes


def daily_quotes(codes: list[str]) -> dict:
    batches = [[tx_symbol(c) for c in codes[i:i + 80]] for i in range(0, len(codes), 80)]
    items, errors = [], []
    def fetch(batch):
        try:
            return tencent_quotes(batch), None
        except Exception as exc:
            return [], str(exc)[:120]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rows, error in pool.map(fetch, batches):
            items.extend(rows)
            if error:
                errors.append(error)
    return {"items": items, "errors": errors, "fetched_at": iso()}


def yahoo_chart(symbol: str, period="1y", interval="1d") -> dict:
    from urllib.parse import quote
    payload = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}", params={"range": period, "interval": interval}).json()
    data = payload.get("chart", {}).get("result")
    if not data:
        raise ValueError("海外数据不可用")
    result = data[0]
    meta = result["meta"]
    stamps = result.get("timestamp", [])
    series = result.get("indicators", {}).get("quote", [{}])[0]
    zone = ZoneInfo(meta.get("exchangeTimezoneName", "UTC"))
    history = []
    for i, ts in enumerate(stamps):
        close = series.get("close", [None] * len(stamps))[i]
        if close is not None:
            day = datetime.fromtimestamp(ts, zone)
            hour, minute = (15, 30) if str(zone) in ("Asia/Tokyo", "Asia/Seoul") else (16, 0)
            available = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            observed = datetime.fromtimestamp(meta["regularMarketTime"], zone)
            if day.date() == observed.date() and observed < available:
                available = observed
            history.append({"date": day.date().isoformat(), "time": iso(available), "close": close})
    stamp = datetime.fromtimestamp(meta["regularMarketTime"], TZ)
    price = meta.get("regularMarketPrice")
    previous = meta.get("previousClose", meta.get("chartPreviousClose"))
    # chartPreviousClose over 3mo is not yesterday's close. Derive daily previous close.
    prior = [p["close"] for p in history if p["date"] < stamp.astimezone(zone).date().isoformat()]
    if prior:
        previous = prior[-1]
    return {"symbol": symbol, "price": price, "previous_close": previous,
            "change_pct": (price / previous - 1) * 100 if price and previous else None,
            "market_time": iso(stamp), "fetched_at": iso(), "source": "Yahoo Finance",
            "source_url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/",
            "delay_status": "unknown", "currency": meta.get("currency"),
            "exchange_timezone": str(zone), "history": history,
            "session_state": "open" if meta.get("currentTradingPeriod", {}).get("regular", {}).get("start", 0) <= now().timestamp() <= meta.get("currentTradingPeriod", {}).get("regular", {}).get("end", 0) else "closed"}


WORLD = [("^GSPC", "标普 500", "美国"), ("^IXIC", "纳斯达克综合", "美国"),
         ("^DJI", "道琼斯", "美国"), ("^N225", "日经 225", "日本"),
         ("^TOPX", "TOPIX", "日本"), ("^KS11", "KOSPI", "韩国"),
         ("^KQ11", "KOSDAQ", "韩国"), ("SOXX", "半导体 ETF", "美国"),
         ("XLF", "美国金融 ETF", "美国"), ("XLE", "美国能源 ETF", "美国"), ("XLK", "美国科技 ETF", "美国"),
         ("^VIX", "VIX", "辅助"), ("USDCNH=X", "美元 / 离岸人民币", "辅助")]


def markets() -> dict:
    rows, failures = [], []
    try:
        rows = tencent_quotes(["sh000001", "sz399001", "sh000300", "sh000905", "sh000852", "sz399006", "hkHSI", "hkHSTECH"])
        for row in rows:
            row["market"] = "港股" if row["symbol"].startswith("hk") else "A股"
    except Exception as exc:
        failures.append({"source": "腾讯指数", "error": str(exc)[:180]})
    def fetch(item):
        symbol, name, market = item
        try:
            return {**yahoo_chart(symbol), "name": name, "market": market}
        except Exception as exc:
            return {"symbol": symbol, "name": name, "market": market, "price": None,
                    "error": str(exc)[:120], "delay_status": "unavailable"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows.extend(pool.map(fetch, WORLD))
    return {"items": rows, "errors": failures, "fetched_at": iso()}


def _ak():
    import akshare as ak
    original = requests.sessions.Session.request
    if not getattr(original, "_bounded", False):
        def bounded(self, method, url, **kwargs):
            kwargs["timeout"] = kwargs.get("timeout") or (5, 12)
            return original(self, method, url, **kwargs)
        bounded._bounded = True
        requests.sessions.Session.request = bounded
    return ak


def calendar() -> dict:
    frame = _ak().tool_trade_date_hist_sina()
    days = sorted(str(d)[:10] for d in frame["trade_date"])
    if not days:
        raise ValueError("日历为空")
    return {"dates": days, "valid_through": days[-1], "source": "新浪财经 / AKShare 交易日历", "fetched_at": iso()}


def universe_em() -> dict:
    ak = _ak()
    frame = ak.stock_zh_a_spot_em()
    mapped = frame.rename(columns={"代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "change_pct", "成交额": "amount", "换手率": "turnover", "市盈率-动态": "pe", "总市值": "market_cap"})
    fields = [k for k in ["code", "name", "price", "change_pct", "amount", "turnover", "pe", "market_cap"] if k in mapped]
    rows = clean(mapped[fields].to_dict("records"))
    if len(rows) < 2500:
        raise ValueError("全市场列表不完整")
    return {"items": rows, "source": "东方财富 / AKShare", "fetched_at": iso(), "market_time": None, "delay_status": "unknown"}


def universe() -> dict:
    try:
        # AKShare can retry many pages: a per-request timeout does not bound
        # the whole source. Leave time for fallback within the outer 75s job.
        return call("universe_em", timeout=20)
    except Exception:
        from . import storage
        from .core import dt
        cached = storage.snapshot("universe")
        roster_at = cached.get("roster_asof") or cached.get("fetched_at")
        if len(cached.get("items", [])) >= 2500 and roster_at and 0 <= (now()-dt(roster_at)).total_seconds() <= 7*86400:
            codes = sorted({str(r["code"]).zfill(6) for r in cached["items"]})
        else:
            names = _ak().stock_info_a_code_name()
            codes = [str(c).zfill(6) for c in names["code"]]
            roster_at = iso()
        batches = [[tx_symbol(c) for c in codes[i:i + 80] if c.startswith(("0", "3", "6"))] for i in range(0, len(codes), 80)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            parts = list(pool.map(tencent_quotes, [b for b in batches if b]))
        rows = [r for part in parts for r in part]
        if len(rows) < 2500:
            raise ValueError("备用全市场快照不完整")
        return {"items": rows, "source": "腾讯财经", "fetched_at": iso(), "delay_status": "timestamped", "roster_asof":roster_at}


def history_em(code: str) -> pd.DataFrame:
    ak = _ak()
    start = max(config()["fees_effective_from"].replace("-", ""), (now() - timedelta(days=config().get("history_days",650))).strftime("%Y%m%d"))
    # Unadjusted prices for fills; adjustment factors must never change historical fills.
    frame = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, adjust="")
    mapping = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover", "涨跌幅": "change_pct"}
    frame = frame.rename(columns=mapping)
    required = ["date", "open", "close", "high", "low", "volume", "amount"]
    if frame.empty or not set(required).issubset(frame):
        raise ValueError("日线缺少必要字段")
    frame["date"] = frame["date"].astype(str).str[:10]
    frame["volume"] = frame["volume"] * 100  # Eastmoney lots -> shares.
    return frame


def corporate_actions(code: str) -> dict:
    raw = http(f"https://finance.sina.com.cn/realstock/company/{tx_symbol(code)}/qfq.js").text
    # Sina appends a JavaScript comment after the JSON object.
    factors, _ = json.JSONDecoder().raw_decode(raw.split("=", 1)[1].lstrip())
    if not isinstance(factors.get("data"), list):
        raise ValueError("除权记录格式异常")
    return {"dates": [r["d"] for r in factors["data"]], "source": "https://finance.sina.com.cn/", "fetched_at": iso()}


def history_sina(code: str) -> pd.DataFrame:
    # Decode the provider's raw OHLCV + actual turnover. Do not forward-fill
    # suspended dates or reconstruct turnover from close * volume.
    from akshare.stock.cons import zh_sina_a_stock_hist_url, hk_js_decode
    import py_mini_racer
    payload = http(zh_sina_a_stock_hist_url.format(tx_symbol(code))).text
    js = py_mini_racer.MiniRacer()
    try:
        js.eval(hk_js_decode)
        rows = js.call("d", payload.split("=", 1)[1].split(";")[0].replace('"', ''))
    finally:
        if callable(getattr(js, "close", None)):
            js.close()
    frame = pd.DataFrame(rows)
    frame["date"] = frame["date"].astype(str).str[:10]
    frame = frame[frame.date >= max(config()["fees_effective_from"], (now() - timedelta(days=config().get("history_days",650))).date().isoformat())]
    frame = frame[["date", "open", "close", "high", "low", "volume", "amount"]].copy()
    frame["corporate_action_checked"] = False
    frame["corporate_action_event"] = False
    try:
        events = set(corporate_actions(code)["dates"])
        frame["corporate_action_checked"] = True
        frame["corporate_action_event"] = frame.date.isin(events)
    except Exception:
        pass  # Prices remain useful; unknown actions block candidate execution.
    return frame


def normalize_history(frame: pd.DataFrame, source: str, at=None) -> dict:
    at = at or now()
    required = ["open", "close", "high", "low", "volume", "amount"]
    if frame.empty or not {"date", *required}.issubset(frame):
        raise ValueError("日线缺少真实价格、成交量或成交额")
    frame = frame.copy()
    frame["date"] = frame.date.astype(str).str[:10]
    frame["available_at"] = frame.date + "T16:10:00+08:00"
    # Discard today's unfinished bar, not merely give it a future availability.
    frame = frame[pd.to_datetime(frame.available_at, utc=True) <= at]
    frame = frame.drop_duplicates("date", keep="last").sort_values("date")
    frame[required] = frame[required].apply(pd.to_numeric, errors="coerce")
    # Some distributors repeat a previous candle with zero turnover. Such rows
    # are not observed trading sessions and must not count toward 120 bars.
    frame = frame[~((frame.volume == 0) & (frame.amount == 0))]
    tail = frame.tail(120)
    if tail.empty or tail[required].isna().any().any() or (tail[required] <= 0).any().any():
        raise ValueError("日线存在空值或无效价格/成交数据")
    if ((tail.high < tail[["open", "close", "low"]].max(axis=1)) | (tail.low > tail[["open", "close"]].min(axis=1))).any():
        raise ValueError("日线最高最低价冲突")
    vwap = tail.amount / tail.volume
    if ((vwap < tail.low * .98) | (vwap > tail.high * 1.02)).any():
        raise ValueError("成交额与成交量单位不一致")
    frame["source"] = source
    if source == SOURCE_EM:
        frame["corporate_action_checked"] = frame.get("change_pct", pd.Series(index=frame.index, dtype=float)).notna()
        frame["corporate_action_event"] = False
    frame["observed_at"] = iso(at)
    return {"bars": clean(frame.to_dict("records")), "source": source, "adjustment": "raw", "fetched_at": iso(at)}


def history(code: str, preferred="eastmoney") -> dict:
    sources = {"eastmoney": (history_em, SOURCE_EM), "sina": (history_sina, "https://finance.sina.com.cn/")}
    errors = []
    for name in dict.fromkeys([preferred, "eastmoney", "sina"]):
        try:
            fetch, source = sources[name]
            result = normalize_history(fetch(code), source)
            result["fallback_errors"] = errors
            return result
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)[:160]})
    raise ValueError("日线来源均不可用: " + json.dumps(errors, ensure_ascii=False))


def minutes(code: str) -> dict:
    frame = _ak().stock_zh_a_hist_min_em(symbol=code, period="5", adjust="")
    frame = frame.rename(columns={"时间": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"})
    if frame.empty:
        raise ValueError("分钟数据为空")
    frame["date"] = frame["date"].apply(lambda v: iso(datetime.fromisoformat(str(v)).replace(tzinfo=TZ)))
    frame["volume"] = frame["volume"] * 100
    # Eastmoney 5m timestamps label the end of the interval.
    return {"bars": clean(frame.to_dict("records")), "source": SOURCE_EM, "bar_label": "end", "fetched_at": iso()}


def metadata(code: str) -> dict:
    frame = _ak().stock_individual_info_em(symbol=code)
    values = dict(zip(frame["item"], frame["value"]))
    listing = str(values.get("上市时间", ""))[:8]
    listing_date = datetime.strptime(listing, "%Y%m%d").date().isoformat() if len(listing) == 8 else None
    return {"code": code, "sector": values.get("行业"), "listing_date": listing_date,
            "source": SOURCE_EM, "fetched_at": iso(), "classification": "东方财富行业"}


def financials(code: str) -> dict:
    frame = _ak().stock_financial_analysis_indicator_em(symbol=code + (".SH" if code.startswith("6") else ".SZ"), indicator="按报告期")
    records = clean(frame.to_dict("records"))
    # Publication date is mandatory for using the information in a signal.
    usable = []
    for r in records:
        publish = r.get("NOTICE_DATE") or r.get("PUBLISH_DATE")
        report = r.get("REPORT_DATE")
        if publish and str(publish)[:10] < now().date().isoformat():
            usable.append({"published_at": str(publish)[:10] + "T23:59:59+08:00", "report_date": str(report)[:10],
                           "revenue_growth": number(r.get("TOTALOPERATEREVETZ")),
                           "profit_growth": number(r.get("PARENTNETPROFITTZ")),
                           "roe": number(r.get("ROEJQ")), "operating_cash_per_share": number(r.get("MGJYXJJE"))})
    usable.sort(key=lambda r: r["report_date"], reverse=True)
    return {"code": code, "reports": usable[:8], "status": "available" if usable else "publication_date_missing",
            "source": SOURCE_EM, "fetched_at": iso()}


def sectors() -> dict:
    frame = _ak().stock_board_industry_name_em()
    frame = frame.rename(columns={"板块名称": "name", "板块代码": "symbol", "最新价": "price", "涨跌幅": "change_pct", "上涨家数": "advancing", "下跌家数": "declining", "换手率": "turnover", "领涨股票": "leader"})
    fields = [c for c in ["name", "symbol", "price", "change_pct", "advancing", "declining", "turnover", "leader"] if c in frame]
    return {"items": clean(frame[fields].to_dict("records")), "source": "东方财富行业", "fetched_at": iso(), "market_time": None, "delay_status": "unknown"}


def news(codes: list[str]) -> dict:
    ak = _ak()
    rows = []
    for code in codes[:6] or ["股市"]:
        frame = ak.stock_news_em(symbol=code)
        for r in frame.to_dict("records"):
            published = str(r.get("发布时间", ""))
            try:
                stamp = iso(datetime.fromisoformat(published).replace(tzinfo=TZ))
            except ValueError:
                stamp = None
            rows.append({"title": r.get("新闻标题"), "summary": str(r.get("新闻内容", ""))[:600],
                         "url": r.get("新闻链接"), "source": r.get("文章来源"),
                         "published_at": stamp, "first_seen_at": iso(), "code": code,
                         "classification": "媒体报道，需查看原文", "verified_primary": False})
    return {"items": rows, "fetched_at": iso(), "coverage": "东方财富关键词新闻；非全球新闻完整覆盖"}


def announcements(codes: list[str]) -> dict:
    """Recent issuer disclosures via a secondary distributor; preserve discovery time."""
    items, coverage = [], {}
    def fetch(code):
        params = {"sr": "-1", "page_size": 50, "page_index": 1, "ann_type": "A", "client_source": "web", "stock_list": code,
                  "begin_time": (now() - timedelta(days=10)).date().isoformat(), "end_time": (now() + timedelta(days=1)).date().isoformat()}
        try:
            payload = http("https://np-anotice-stock.eastmoney.com/api/security/ann", params=params).json()["data"]
            if not isinstance(payload.get("list"), list):
                raise ValueError("公告返回格式改变")
            rows = []
            for r in payload["list"]:
                published = r.get("display_time") or r.get("notice_date")
                try:
                    stamp = datetime.fromisoformat(published).replace(tzinfo=TZ)
                except (ValueError, TypeError):
                    stamp = None
                rows.append({"code": code, "title": r["title"], "summary": "公司公告，查看原文确认内容与适用时间。", "published_at": iso(stamp) if stamp else None,
                             "first_seen_at": iso(), "source": "公司公告 / 东方财富转引", "verified_primary": False,
                             "url": f"https://data.eastmoney.com/notices/detail/{code}/{r['art_code']}.html", "classification": "公告转引，非完整官方源校验"})
            return code, rows, {"ok": payload.get("total_hits", 0) <= 50, "fetched_at": iso(), "count": len(rows), "truncated": payload.get("total_hits", 0) > 50}
        except Exception as exc:
            return code, [], {"ok": False, "error": str(exc)[:120], "fetched_at": iso()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for code, rows, status in pool.map(fetch, codes[:40]):
            items.extend(rows)
            coverage[code] = status
    return {"items": items, "coverage": coverage, "fetched_at": iso(), "source": "东方财富公告转引"}


def index_history(symbol: str) -> dict:
    frame = _ak().stock_zh_index_daily_em(symbol=symbol, start_date=(now() - timedelta(days=config().get("history_days",650))).strftime("%Y%m%d"))
    frame["date"] = frame["date"].astype(str).str[:10]
    frame["time"] = frame["date"] + "T15:00:00+08:00"
    return {"items": clean(frame.to_dict("records")), "source": SOURCE_EM, "fetched_at": iso()}


def sector_history(symbol: str) -> dict:
    # AKShare's legacy HTTP host can return a non-JSON response. Use HTTPS
    # mirrors of the SAME industry index; never substitute another taxonomy.
    import re
    if not re.fullmatch(r"BK\d{4,6}", symbol):
        raise ValueError("板块代码格式错误")
    params = {"secid": f"90.{symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "0",
              "beg": (now() - timedelta(days=600)).strftime("%Y%m%d"), "end": now().strftime("%Y%m%d")}
    errors = []
    for host in ["61.push2his.eastmoney.com", "7.push2his.eastmoney.com"]:
        try:
            payload = http(f"https://{host}/api/qt/stock/kline/get", params=params,
                           headers={"User-Agent": "Mozilla/5.0", "Referer": SOURCE_EM}).json()
            rows = [r.split(",") for r in (payload.get("data") or {}).get("klines", [])]
            items = [{"date": r[0], "open": number(r[1]), "close": number(r[2]),
                      "high": number(r[3]), "low": number(r[4]),
                      "amount": number(r[6]) if len(r) > 6 else None}
                     for r in rows if len(r) >= 5 and number(r[2], 0) > 0]
            if not items:
                raise ValueError("板块历史为空")
            return {"items": items, "source": SOURCE_EM + f"bk/90.{symbol}.html", "fetched_at": iso(), "schema_version": 2}
        except Exception as exc:
            errors.append(str(exc)[:120])
    raise ValueError("板块历史暂不可用：" + "; ".join(errors))


def comparison_history(kind: str, symbol: str) -> dict:
    """Separate adjusted view cache; never save these rows as execution bars."""
    if kind == "sector":
        return {**sector_history(symbol), "adjustment": "index"}
    if kind not in {"stock", "index"}:
        raise ValueError("不支持的对比类型")
    tx = tx_symbol(symbol) if kind == "stock" else symbol
    adjustment = "qfq" if kind == "stock" else ""
    try:
        payload = http("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                       params={"param": f"{tx},day,,,640,{adjustment}"}).json()["data"][tx]
        rows = payload.get("qfqday", payload.get("day", []))
        # Tencent's amount is in ten-thousand yuan, for both stocks and indices.
        # OHLC comes from one adjustment vintage; turnover is actual yuan.
        items = [{"date": str(r[0])[:10], "open": number(r[1]), "close": number(r[2]),
                  "high": number(r[3]), "low": number(r[4]),
                  "amount": number(r[8]) * 10000 if len(r) > 8 and number(r[8]) is not None else None} for r in rows
                 if len(r) >= 6 and number(r[5], 0) > 0 and number(r[2], 0) > 0]
        if not items:
            raise ValueError("腾讯历史日线为空")
        return {"items": items, "source": SOURCE_TX + tx, "fetched_at": iso(),
                "adjustment": "qfq" if kind == "stock" else "index", "schema_version": 2}
    except Exception:
        if kind == "index":
            return {**index_history(symbol), "adjustment": "index", "schema_version": 2}
        frame = _ak().stock_zh_a_daily(symbol=tx, start_date=(now() - timedelta(days=800)).strftime("%Y%m%d"),
                                     end_date=now().strftime("%Y%m%d"), adjust="qfq")
        frame = frame[frame.volume > 0].copy()
        frame["date"] = frame.date.astype(str).str[:10]
        return {"items": clean(frame[[c for c in ["date", "open", "close", "high", "low", "amount"] if c in frame]].to_dict("records")),
                "source": "https://finance.sina.com.cn/", "fetched_at": iso(), "adjustment": "qfq", "schema_version": 2}


OPS = {"calendar": calendar, "universe": universe, "universe_em": universe_em, "markets": markets, "history": history,
       "corporate_actions": corporate_actions,
       "minutes": minutes, "metadata": metadata, "financials": financials, "sectors": sectors, "news": news,
       "quotes": tencent_quotes, "world_history": yahoo_chart, "index_history": index_history, "sector_history": sector_history, "daily_quotes": daily_quotes, "announcements": announcements,
       "comparison_history": comparison_history}


def company_research(code: str, sector: str) -> dict:
    from .research_data import fetch_company
    return fetch_company(code, sector)


OPS["company_research"] = company_research


def call(operation: str, *args, timeout=75) -> dict | list:
    result = subprocess.run([sys.executable, "-m", "ashare.providers", operation, json.dumps(args)], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        try:
            message = json.loads(result.stdout)["error"]
        except (ValueError, KeyError):
            message = result.stderr[-250:]
        raise RuntimeError(message or "数据请求失败")
    return json.loads(result.stdout)


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            value = OPS[sys.argv[1]](*json.loads(sys.argv[2]))
        print(json.dumps(clean(value), ensure_ascii=False, allow_nan=False, default=str))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:250]}"}, ensure_ascii=False))
        sys.exit(1)
