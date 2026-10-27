"""Entry point used only by GitHub Actions. Never touches a user's paper ledger."""
import json
import os
from pathlib import Path

from ashare import jobs, storage, learning, reviews
from ashare.cloud import GitHub, pack_checkpoint, publish_report, publish_research_assets, sync_reports, bootstrap_checkpoint
from ashare.core import atomic_json, data_dir, iso, now

def main():
    mode = os.environ.get("TASK_MODE", "update")
    if mode == "probe":
        result = jobs.probe()
        print(json.dumps(result, ensure_ascii=False))
        if not all(result[k]["ok"] for k in ["calendar", "quotes", "history"]):
            raise SystemExit("关键数据接口在云端不可用")
        raise SystemExit(0)
    
    synced = sync_reports(recent_sessions=10)
    print(json.dumps({"sync": synced}, ensure_ascii=False))
    if mode == "train":
        jobs.collect("calendar",timeout=40)
        result = learning.train()
        publish_research_assets()
        print(json.dumps(result,ensure_ascii=False))
        if result.get("state") == "failed":
            raise SystemExit("模型训练失败，已有研究和规则交易不受影响")
        return
    if mode == "bootstrap":
        if synced.get("checkpoint", {}).get("source") != "bootstrap_release":
            bootstrap_checkpoint(GitHub())
        print(json.dumps({"source_probe": jobs.probe()}, ensure_ascii=False))
        mode = "close" if now().hour >= 16 else "update"
    current = storage.snapshot("latest_report")
    if mode == "fallback" and current.get("trade_date") == now().date().isoformat() and current.get("mode") == "update" and current.get("status") == "complete":
        print("已有完整更新版，跳过补跑")
        raise SystemExit(0)
    result = jobs.build_report(mode, budget_seconds=480)
    out = Path("output")
    out.mkdir(exist_ok=True)
    atomic_json(out / "run-status.json", {**storage.snapshot("run_status"), "sync": synced})
    if "id" in result:
        publish_report(result)
        # A delayed/missed closing job is repaired by the next premarket run.
        print(json.dumps({"review":reviews.rebuild(local=False)},ensure_ascii=False))
        publish_research_assets()
        if result.get("status") == "complete":
            pack_checkpoint(out / "checkpoint.tar.gz")
        print(json.dumps({"report": result["id"], "status": result["status"], "coverage": result["coverage"]}, ensure_ascii=False))
    elif result.get("state") == "failed":
        raise SystemExit(result["error"])


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        status = {"state": "failed", "finished_at": iso(), "error": str(exc)[:300]}
        storage.save_snapshot("run_status", status)
        atomic_json(Path("output/run-status.json"), status)
        raise
