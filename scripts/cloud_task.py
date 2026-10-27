"""Entry point used only by GitHub Actions. Never touches a user's paper ledger."""
import json
import os
from pathlib import Path

from ashare import jobs, storage, learning, reviews
from ashare.cloud import GitHub, pack_checkpoint, publish_report, publish_research_assets, sync_reports, bootstrap_checkpoint
from ashare.core import atomic_json, data_dir, iso, now


def reject_unmigrated_actions():
    # This legacy entrypoint has no private checkpoint upload/readback stage.
    # Do not unlock it merely by configuring a token or a workflow variable.
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise SystemExit("Live cloud migration is not enabled; use synthetic validation only")


def receipt(stage, state):
    # Public output must not contain provider responses, URLs, paths or payloads.
    print(json.dumps({"stage": stage, "state": state}))


def main():
    reject_unmigrated_actions()
    mode = os.environ.get("TASK_MODE", "update")
    if mode not in {"probe", "train", "bootstrap", "initial", "update", "fallback", "close"}:
        raise SystemExit("Unsupported research mode")
    if mode == "probe":
        result = jobs.probe()
        if not all(result.get(k, {}).get("ok") is True and result[k].get("empty") is False
                   for k in ["calendar", "quotes", "history"]) or result["history"].get("rows", 0) <= 0:
            raise SystemExit("Provider probe missing required data")
        receipt("probe", "complete")
        raise SystemExit(0)
    
    synced = sync_reports(recent_sessions=10)
    if synced.get("state") != "success" or synced.get("checkpoint", {}).get("state") != "success":
        raise SystemExit("Private input synchronization incomplete")
    receipt("sync", "complete")
    if mode == "train":
        jobs.collect("calendar",timeout=40)
        result = learning.train()
        if result.get("state") != "complete" or not result.get("model_id"):
            raise SystemExit("Training incomplete; insufficient samples do not pass validation")
        publish_research_assets()
        receipt("train", "complete")
        return
    if mode == "bootstrap":
        if synced.get("checkpoint", {}).get("source") != "bootstrap_release":
            bootstrap_checkpoint(GitHub())
        jobs.probe()
        mode = "close" if now().hour >= 16 else "update"
    current = storage.snapshot("latest_report")
    if mode == "fallback" and current.get("trade_date") == now().date().isoformat() and current.get("mode") == "update" and current.get("status") == "complete":
        receipt("fallback", "skipped_existing_report")
        raise SystemExit(0)
    result = jobs.build_report(mode, budget_seconds=480)
    out = Path("output")
    out.mkdir(exist_ok=True)
    atomic_json(out / "run-status.json", {**storage.snapshot("run_status"), "sync": synced})
    if "id" in result:
        if result.get("status") != "complete" or result.get("coverage", {}).get("covered", 0) <= 0:
            raise SystemExit("Report incomplete; empty or partial coverage does not pass validation")
        publish_report(result)
        # A delayed/missed closing job is repaired by the next premarket run.
        reviews.rebuild(local=False)
        publish_research_assets()
        if result.get("status") == "complete":
            pack_checkpoint(out / "checkpoint.tar.gz")
        receipt("report", "complete")
    elif result.get("state") == "skipped":
        receipt("report", "skipped_nontrading_day")
    else:
        raise SystemExit("Report generation incomplete")


if __name__ == "__main__":
    reject_unmigrated_actions()
    try:
        main()
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        status = {"state": "failed", "finished_at": iso(), "error": str(exc)[:300]}
        storage.save_snapshot("run_status", status)
        atomic_json(Path("output/run-status.json"), status)
        receipt("task", "failed")
        raise SystemExit(1) from None
