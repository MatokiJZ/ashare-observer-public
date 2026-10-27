"""Manual migration worker. Raw output is confined by private_cloud_entry.py.

This module does real research/storage work when explicitly gated; it never trades
or activates a model. Tests replace external providers and storage transports.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import cloud, jobs, learning, reviews, storage
from .core import TradingCalendar, atomic_json, data_dir, dt, iso, now, read_json
from . import private_checkpoints as checkpoints

MODES = {"initial", "update", "fallback", "close", "train"}
STAGES = ("input", "research", "publish", "readback")


def empty_receipt(mode):
    return {"schema_version": 1, "mode": mode, "state": "failed",
            "stages": {key: "pending" for key in STAGES},
            "counts": {key: 0 for key in ("reports", "models", "checkpoints")},
            "reason": "internal_error"}


def require_verified(value, minimum=1):
    if not isinstance(value, dict) or value.get("state") != "verified":
        raise ValueError("Storage verification incomplete")
    count = value.get("files")
    if type(count) is not int or count < minimum:
        raise ValueError("Storage verification is empty")


def verify_report(report, mode, at, expected):
    cloud.validate_report(report)
    coverage = report.get("coverage", {})
    total, covered, ratio = (coverage.get(k) for k in ("total", "covered", "ratio"))
    if (report.get("status") != "complete" or report.get("mode") != mode
            or report.get("trade_date") != at.date().isoformat()
            or dt(report["generated_at"]) < at
            or report.get("data_cutoff") != expected
            or coverage.get("expected_date") != expected
            or type(total) is not int or type(covered) is not int
            or not 0 < covered <= total
            or type(ratio) not in (int, float) or not .9 <= ratio <= 1
            or abs(ratio - covered / total) > 1e-6):
        raise ValueError("Report freshness or coverage incomplete")


def verify_model(result, at):
    identifier = result.get("model_id", "")
    if result.get("state") != "complete" or not learning.MODEL_ID.fullmatch(identifier):
        raise ValueError("Training did not produce a complete model")
    trained_at = dt(result["started_at"])
    reused = trained_at < at
    if reused and trained_at.isocalendar()[:2] != at.isocalendar()[:2]:
        raise ValueError("Training result is from an earlier week")
    manifest = read_json(data_dir() / "models" / identifier / "manifest.json", {})
    if (manifest.get("id") != identifier or manifest.get("automatic_trading_approved") is not False
            or any(type(manifest.get("samples", {}).get(k)) is not int
                   or manifest["samples"][k] <= 0 for k in ("train", "validation", "test"))):
        raise ValueError("Model evidence incomplete")
    split = manifest.get("split", {})
    train, validation, test = (split.get(k, []) for k in ("train", "validation", "test"))
    if not train or not validation or not test or not max(train) < min(validation) <= max(validation) < min(test):
        raise ValueError("Model chronological split incomplete")
    if max(test) > at.date().isoformat():
        raise ValueError("Model test data is from the future")
    return identifier, reused


def execute(mode, at=None):
    """Return only a safe receipt; private evidence is persisted separately."""
    at = (at or now()).replace(microsecond=0)
    receipt = empty_receipt(mode)
    phase = "input"
    try:
        branch = cloud.write_branch()
        if mode not in MODES:
            raise ValueError("Unknown mode")
        day = at.date().isoformat()
        key = f"{day}-{mode}"
        previous = cloud.get_run_receipt(key)
        if previous is not None:
            receipt.update(state="skipped", reason="already_verified")
            return receipt
        # Never label a delayed morning task as a timely premarket signal.
        minutes = at.hour * 60 + at.minute
        if (mode in {"initial", "update", "fallback"} and not 360 <= minutes < 570
                or mode == "close" and minutes < 970):
            raise ValueError("Requested report window has passed or not started")
        synced = cloud.sync_reports(recent_sessions=10)
        restored = synced.get("checkpoint", {})
        if synced.get("state") != "success" or restored.get("state") != "success" or restored.get("day_files", 0) <= 0:
            raise ValueError("Private input restore incomplete")
        revision = synced.get("data_revision", {})
        if revision.get("branch") != "data" or not re.fullmatch(r"[a-f0-9]{40}", revision.get("commit", "")):
            raise ValueError("Production input revision unavailable")
        calendar_payload = jobs.collect("calendar", timeout=40)
        if calendar_payload.get("error"):
            raise ValueError("Calendar update failed")
        calendar = TradingCalendar(calendar_payload)
        if mode != "train" and not calendar.is_session(at.date()):
            receipt.update(state="skipped", reason="nontrading_day")
            return receipt
        expected = calendar.completed(at)
        input_archive = data_dir() / "input-validation.tar.gz"
        checkpoints.deterministic_checkpoint(input_archive)
        try:
            input_evidence = checkpoints.inspect_checkpoint(input_archive, expected if mode == "train" else None)
        finally:
            input_archive.unlink(missing_ok=True)
        receipt["stages"]["input"] = "verified"
        phase = "research"
        reused_training = False
        before_control = storage.snapshot("model_control")
        source_keys = ("source", "release_id", "asset_id", "artifact_id", "tag", "sha256", "bytes",
                       "day_files", "first_date", "last_date", "degraded", "failed_release_ids", "readback_verified")
        evidence = {"input": {"data_revision": revision,
                              "restore": {k: restored[k] for k in source_keys if k in restored},
                              "inspection": input_evidence},
                    "expected_date": expected, "started_at": iso(at),
                    "code_revision": os.getenv("GITHUB_SHA", "synthetic-local"),
                    "input_branch": "data", "write_branch": branch}
        if mode == "train":
            force_fresh = branch == "migration-acceptance"
            existing_models = {path.name for path in (data_dir() / "models").iterdir()} if (data_dir() / "models").exists() else set()
            current_prefix = at.strftime("%Y-%m-%d-%H%M%S") + "-"
            if force_fresh and (any(identifier.startswith(current_prefix) for identifier in existing_models)
                    or (data_dir() / "training_runs" / (at.strftime("%Y-%m-%d-%H%M%S") + ".json")).exists()):
                raise ValueError("Fresh acceptance training timestamp already exists")
            # Only the isolated acceptance namespace bypasses the weekly reuse
            # shortcut. The fixed training recipe and model controls are unchanged.
            result = learning.train(at=at, force=force_fresh)
            model_id, reused_training = verify_model(result, at)
            if force_fresh and (reused_training or dt(result["started_at"]) != at
                    or model_id in existing_models or not model_id.startswith(current_prefix)):
                raise ValueError("Acceptance requires a newly trained model at this run timestamp")
            evidence["model_id"] = model_id
            evidence["research_reused"] = reused_training
            evidence["research_forced_fresh"] = force_fresh
        else:
            result = jobs.build_report(mode, budget_seconds=480)
            verify_report(result, mode, at, expected)
            review = reviews.rebuild(local=False)
            if review.get("state") in {"failed", "blocked"}:
                raise ValueError("Review stage incomplete")
            evidence["report_id"] = result["id"]
        if storage.snapshot("model_control") != before_control:
            raise ValueError("Research unexpectedly changed model controls")
        receipt["stages"]["research"] = "verified"
        phase = "publish"
        if mode != "train":
            published = cloud.publish_report(result)
            require_verified(published)
            evidence["report"] = published
            receipt["counts"]["reports"] = 1
        assets = cloud.publish_research_assets()
        if mode == "train":
            require_verified(assets, minimum=3)
            receipt["counts"]["models"] = 1
        elif assets.get("state") != "empty":
            require_verified(assets)
        evidence["assets"] = assets
        archive = data_dir() / "outgoing-checkpoint.tar.gz"
        checkpoints.deterministic_checkpoint(archive)
        try:
            gh = cloud.GitHub()
            try:
                checkpoint = checkpoints.publish_checkpoint(gh, archive, expected)
            finally:
                gh.client.close()
        finally:
            archive.unlink(missing_ok=True)
        if checkpoint.get("state") not in {"success", "verified"} or checkpoint.get("readback_verified") is not True:
            raise ValueError("Checkpoint readback incomplete")
        evidence["checkpoint"] = checkpoint
        receipt["counts"]["checkpoints"] = 1
        receipt["stages"]["publish"] = "verified"
        phase = "readback"
        # Each publisher verifies bytes from the remote ref/asset; the final
        # immutable receipt is committed only after those verifications succeed.
        receipt["stages"]["readback"] = "verified"
        receipt.update(state="verified", reason="complete")
        persisted = cloud.publish_run_receipt(key, {**receipt, "day": day, "evidence": evidence})
        require_verified(persisted)
        if reused_training:
            # Resume persistence after an earlier training completed, without
            # presenting a reused model as a fresh full-training acceptance.
            receipt.update(state="skipped", reason="reused_training")
        return receipt
    except Exception:
        receipt.update(state="failed", reason={"input": "input_invalid", "research": "research_incomplete",
            "publish": "publication_failed", "readback": "readback_failed"}[phase])
        if phase == "readback":
            receipt["stages"]["readback"] = "failed"
        return receipt


def main():
    # The wrapper creates both paths within its disposable directory. This module
    # is never the direct Actions entrypoint and has no credential fallback.
    target = os.getenv("ASHARE_PRIVATE_RECEIPT", "")
    if os.getenv("ASHARE_PRIVATE_WORKER") != "1" or not target or os.getenv("ASHARE_MONITOR") != "0":
        return 2
    receipt = execute(os.getenv("TASK_MODE", ""))
    atomic_json(Path(target), receipt)
    return 0 if receipt["state"] == "verified" else 3


if __name__ == "__main__":
    raise SystemExit(main())
