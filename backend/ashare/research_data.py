"""Bounded, dated evidence from public distributors; estimates stay estimates."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import re
import time

from .core import TZ, dt, iso, now
from . import storage

ORDER_WORDS = ("中标", "重大合同", "订单", "销售合同", "项目合同", "框架协议", "意向协议")


def published(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value)).replace(tzinfo=TZ)
    # Distributor often supplies only a date disguised as midnight.
    if stamp.time().isoformat() == "00:00:00":
        stamp = stamp.replace(hour=23, minute=59, second=59)
    return iso(stamp)


def normalize_report(row, year, seen):
    key = str(row.get("infoCode", ""))
    from .providers import number
    return {"id": key, "title": row.get("title", ""), "institution": row.get("orgSName") or row.get("orgName"),
            "published_at": published(row.get("publishDate")), "first_seen_at": seen,
            "industry": row.get("indvInduName") or row.get("industryName"),
            "forecast_eps": {str(year+i): number(row.get(field)) for i,field in enumerate(["predictThisYearEps", "predictNextYearEps", "predictNextTwoYearEps"])},
            "source": "券商 / 研究机构研报，经东方财富转引", "url": f"https://pdf.dfcfw.com/pdf/H3_{key}_1.pdf" if re.fullmatch(r"AP\d+", key) else None,
            "target_low": number(row.get("indvAimPriceL")), "target_high": number(row.get("indvAimPriceT")),
            "target_horizon": "原研报期限未统一，仅作参考", "verified_primary": False}


def fetch_company(code, sector):
    from .providers import http, _ak, clean, number
    at = now()
    params = {"industryCode":"*", "pageSize":100, "industry":"*", "rating":"*", "ratingChange":"*",
              "beginTime":(at-timedelta(days=90)).date().isoformat(), "endTime":at.date().isoformat(), "pageNo":1, "qType":0, "code":code}
    errors, reports, industries, orders, peers = [], [], [], [], []
    diagnostics = {}
    def unavailable(label, exc):
        errors.append(label + "本次获取失败，尚未取得完整的新样本")
        diagnostics[label] = str(exc)[:250]
    industry_code = None
    try:
        payload = http("https://reportapi.eastmoney.com/report/list", params=params).json()
        year = int(payload["currentYear"])
        if year != at.year:
            raise ValueError("研报预测年份与当前年份不一致")
        raw = payload.get("data", [])
        reports = [normalize_report(r, year, iso()) for r in raw if r.get("stockCode") == code]
        if raw:
            industry_code = raw[0].get("indvInduCode") or raw[0].get("industryCode")
        if payload.get("TotalPage", 1) > 1:
            errors.append("研报仅覆盖最近 100 份")
    except Exception as exc:
        unavailable("个股研报", exc)
    if industry_code:
        try:
            params.update(qType=1, code="", industryCode=industry_code, pageSize=10)
            payload = http("https://reportapi.eastmoney.com/report/list", params=params).json()
            industries = [normalize_report(r, at.year, iso()) for r in payload.get("data", []) if str(r.get("industryCode")) == str(industry_code)]
        except Exception as exc:
            unavailable("行业研报", exc)
    try:
        frame = _ak().stock_board_industry_cons_em(symbol=sector)
        for r in clean(frame.to_dict("records")):
            pe = number(r.get("市盈率-动态"))
            if str(r.get("代码")) != code and pe and 0 < pe <= 100 and not any(w in str(r.get("名称", "")).upper() for w in ["ST", "退"]):
                peers.append({"code":str(r["代码"]), "name":r.get("名称"), "pe":pe})
    except Exception as exc:
        unavailable("同行估值", exc)
    try:
        params = {"sr":"-1", "page_size":100, "page_index":1, "ann_type":"A", "client_source":"web", "stock_list":code,
                  "begin_time":(at-timedelta(days=90)).date().isoformat(), "end_time":at.date().isoformat()}
        payload = http("https://np-anotice-stock.eastmoney.com/api/security/ann", params=params).json()["data"]
        for row in payload.get("list", []):
            title = row.get("title", "")
            if not any(w in title for w in ORDER_WORDS):
                continue
            stage = "意向 / 框架，未确认收入" if any(w in title for w in ["意向", "框架", "预中标", "候选人"]) else "中标，需核实签约与执行" if "中标" in title else "合同公告，需核实履约与收入确认"
            if any(w in title for w in ["终止", "取消", "解除"]):
                stage = "终止 / 取消，需核实损失与责任"
            elif any(w in title for w in ["履行", "交付", "验收", "履约"]):
                stage = "履约进展，收入确认仍需核实"
            orders.append({"id":row["art_code"], "code":code, "title":title, "stage":stage,
                           "published_at":published(row.get("display_time") or row.get("notice_date")), "first_seen_at":iso(),
                           "url":f"https://data.eastmoney.com/notices/detail/{code}/{row['art_code']}.html",
                           "source":"公司公告 / 东方财富转引", "verified_primary":False,
                           "valuation_effect":"未取得已核实的金额、履约期及利润率，不另行加计盈利，避免与研报预测重复计算"})
        if payload.get("total_hits", 0) > 100:
            errors.append("订单公告只覆盖最近 100 份公告，可能有遗漏")
    except Exception as exc:
        unavailable("订单公告", exc)
    return {"code":code, "sector":sector, "fetched_at":iso(), "reports":reports, "industry_reports":industries, "orders":orders,
            "peers":peers, "peer_fetched_at":iso(), "peer_basis":"东方财富同分类行业的动态 PE；正值且不超过 100 倍，排除本股；是估值假设而非预测 PE", "errors":errors, "diagnostics":diagnostics}


def enrich(candidates, deadline):
    from . import providers
    cache = storage.snapshot("company_research")
    items = cache.get("items", {})
    def due(c):
        old = items.get(c["code"], {})
        retry = (old.get("errors") or old.get("error")) and old.get("last_attempt") and (now()-dt(old["last_attempt"])).total_seconds() >= 900
        return c.get("sector") != "行业待核实" and (old.get("last_attempt", "")[:10] != now().date().isoformat() or retry)
    chosen = [c for c in candidates if due(c)][:12]
    def fetch(c):
        if time.monotonic() >= deadline:
            return c["code"], None
        try:
            return c["code"], providers.call("company_research", c["code"], c["sector"], timeout=min(60,max(1,deadline-time.monotonic())))
        except Exception as exc:
            return c["code"], {"error":"公司研究数据本次更新失败，等待重试", "diagnostics":{"fetch":str(exc)[:250]}}
    with ThreadPoolExecutor(max_workers=4) as workers:
        for code,value in workers.map(fetch, chosen):
            if value is None:
                continue
            old = items.get(code, {})
            if "error" in value:
                items[code] = {**old, **value, "last_attempt":iso()}
                continue
            if not value.get("peers") and value.get("diagnostics", {}).get("同行估值") and old.get("peers"):
                value.update(peers=old["peers"], peer_fetched_at=old.get("peer_fetched_at"), peer_basis=old.get("peer_basis"))
                value["errors"].append("同行估值保留上次样本，超过 7 天自动停止用于公司情景")
            for field,label in [("reports","个股研报"),("industry_reports","行业研报"),("orders","订单公告")]:
                if not value.get(field) and value.get("diagnostics", {}).get(label) and old.get(field):
                    value[field] = old[field]
                    value["errors"].append(label+"保留上次可见记录，请结合原始日期判断")
            for field in ["reports", "industry_reports", "orders"]:
                prior = {r["id"]:r for r in old.get(field, [])}
                for row in value[field]:
                    previous = prior.get(row["id"])
                    # Do not backdate a revised earnings estimate.
                    if previous and previous.get("forecast_eps") == row.get("forecast_eps"):
                        row["first_seen_at"] = previous["first_seen_at"]
            items[code] = {**value, "last_attempt":iso()}
    storage.save_snapshot("company_research", {"items":items, "fetched_at":iso()})
    return items
