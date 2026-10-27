"""Synthetic orchestration tests. Real GitHub and providers are never called."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from ashare import private_runner as runner
from ashare.core import TZ, atomic_json, data_dir, iso


@pytest.fixture
def pipeline(monkeypatch, calendar):
    at = datetime(2026, 10, 20, 8, 35, tzinfo=TZ)
    calls = []
    monkeypatch.setattr(runner.cloud, "get_run_receipt", lambda key: None)
    monkeypatch.setattr(runner.cloud, "sync_reports", lambda **kw: {
        "state": "success", "checkpoint": {"state": "success", "day_files": 1, "source": "synthetic",
            "release_id": 7, "tag": "synthetic-version", "sha256": "synthetic-hash", "degraded": True, "failed_release_ids": [8]}})
    monkeypatch.setattr(runner.jobs, "collect", lambda *a, **kw: calendar.payload)
    monkeypatch.setattr(runner.checkpoints, "deterministic_checkpoint", lambda path: path.write_bytes(b"synthetic"))
    monkeypatch.setattr(runner.checkpoints, "inspect_checkpoint", lambda *a: {"day_files": 1, "last_date": "2026-10-19"})
    report = {"id": "2026-10-20-083501-update", "mode": "update", "schema_version": 1,
        "strategy_version": "synthetic", "generated_at": "2026-10-20T08:35:01+08:00", "trade_date": "2026-10-20",
        "status": "complete", "data_cutoff": "2026-10-19", "candidates": [],
        "coverage": {"total": 10, "covered": 10, "ratio": 1., "expected_date": "2026-10-19"}}
    monkeypatch.setattr(runner.jobs, "build_report", lambda *a, **kw: calls.append("research") or report)
    monkeypatch.setattr(runner.reviews, "rebuild", lambda **kw: calls.append("review") or {"updated": 0})
    monkeypatch.setattr(runner.cloud, "publish_report", lambda value: calls.append("report") or {"state": "verified", "files": 1})
    monkeypatch.setattr(runner.cloud, "publish_research_assets", lambda: calls.append("assets") or {"state": "verified", "files": 3})
    monkeypatch.setattr(runner.cloud, "GitHub", lambda: SimpleNamespace(client=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(runner.checkpoints, "publish_checkpoint", lambda *a: calls.append("checkpoint") or {
        "state": "success", "readback_verified": True})
    def persist(key, value):
        assert calls[-1] == "checkpoint"
        assert value["state"] == "verified" and set(value["stages"].values()) == {"verified"}
        assert value["evidence"]["input"]["restore"]["release_id"] == 7
        assert value["evidence"]["input"]["restore"]["failed_release_ids"] == [8]
        calls.append("receipt")
        return {"state": "verified", "files": 1}
    monkeypatch.setattr(runner.cloud, "publish_run_receipt", persist)
    identifier = "2026-10-20-083500-1234abcd"
    def train(**kw):
        calls.append("train")
        atomic_json(data_dir()/"models"/identifier/"manifest.json", {
            "id": identifier, "automatic_trading_approved": False,
            "samples": {"train": 252, "validation": 42, "test": 63},
            "split": {"train": ["2025-01-01"], "validation": ["2025-06-01"], "test": ["2026-10-19"]}})
        return {"state": "complete", "model_id": identifier, "started_at": iso(at)}
    monkeypatch.setattr(runner.learning, "train", train)
    return SimpleNamespace(at=at, calls=calls, report=report)


def test_daily_requires_all_stages_and_persists_receipt_last(pipeline):
    result = runner.execute("update", pipeline.at)
    assert result["state"] == "verified", result
    assert result["counts"] == {"reports": 1, "models": 0, "checkpoints": 1}
    assert pipeline.calls == ["research", "review", "report", "assets", "checkpoint", "receipt"]


def test_training_requires_real_new_model_and_private_checkpoint(pipeline):
    result = runner.execute("train", pipeline.at)
    assert result["state"] == "verified", result
    assert pipeline.calls == ["train", "assets", "checkpoint", "receipt"]
    assert result["counts"] == {"reports": 0, "models": 1, "checkpoints": 1}


def test_existing_verified_key_skips_before_collecting(pipeline, monkeypatch):
    monkeypatch.setattr(runner.cloud, "get_run_receipt", lambda key: {"state": "verified"})
    result = runner.execute("update", pipeline.at)
    assert result["state"] == "skipped" and result["reason"] == "already_verified"
    assert not pipeline.calls


@pytest.mark.parametrize("state,files", [("failed", 1), ("partial", 1), ("success", 0)])
def test_bad_input_never_runs_research(pipeline, monkeypatch, state, files):
    monkeypatch.setattr(runner.cloud, "sync_reports", lambda **kw: {
        "state": state, "checkpoint": {"state": "success", "day_files": files}})
    assert runner.execute("update", pipeline.at)["reason"] == "input_invalid"
    assert not pipeline.calls


@pytest.mark.parametrize("change", [
    {"status": "partial"}, {"data_cutoff": "2026-10-16"},
    {"generated_at": "2026-10-20T08:00:00+08:00"},
    {"coverage": {"total": 0, "covered": 0, "ratio": 1., "expected_date": "2026-10-19"}},
    {"coverage": {"total": 10, "covered": 1, "ratio": 1., "expected_date": "2026-10-19"}},
])
def test_stale_partial_or_fake_coverage_report_cannot_publish(pipeline, change):
    pipeline.report.update(change)
    assert runner.execute("update", pipeline.at)["reason"] == "research_incomplete"
    assert pipeline.calls == ["research"]


def test_delayed_premarket_does_not_masquerade_as_timely_signal(pipeline):
    assert runner.execute("update", pipeline.at.replace(hour=10))["reason"] == "input_invalid"
    assert not pipeline.calls


def test_weekly_insufficient_does_not_publish_or_accept(pipeline, monkeypatch):
    monkeypatch.setattr(runner.learning, "train", lambda **kw: {"state": "insufficient"})
    assert runner.execute("train", pipeline.at)["reason"] == "research_incomplete"
    assert not pipeline.calls


def test_same_week_training_can_resume_storage_without_claiming_fresh_acceptance(pipeline, monkeypatch):
    train = runner.learning.train
    def reused(**kw):
        result = train(**kw)
        result["started_at"] = "2026-10-19T08:00:00+08:00"
        return result
    monkeypatch.setattr(runner.learning, "train", reused)
    result = runner.execute("train", pipeline.at)
    assert result["state"] == "skipped" and result["reason"] == "reused_training"
    assert pipeline.calls[-1] == "receipt"


def test_prior_week_model_cannot_stand_in_for_this_weeks_training(pipeline, monkeypatch):
    train = runner.learning.train
    def reused(**kw):
        result = train(**kw)
        result["started_at"] = "2026-10-12T08:00:00+08:00"
        return result
    monkeypatch.setattr(runner.learning, "train", reused)
    assert runner.execute("train", pipeline.at)["reason"] == "research_incomplete"
    assert pipeline.calls == ["train"]


def test_storage_readback_failure_does_not_persist_completion(pipeline, monkeypatch):
    monkeypatch.setattr(runner.checkpoints, "publish_checkpoint", lambda *a: {
        "state": "success", "readback_verified": False})
    result = runner.execute("update", pipeline.at)
    assert result["state"] == "failed" and result["reason"] == "publication_failed"
    assert "receipt" not in pipeline.calls


def test_final_receipt_failure_cannot_claim_success(pipeline, monkeypatch):
    monkeypatch.setattr(runner.cloud, "publish_run_receipt", lambda *a: {"state": "empty", "files": 0})
    result = runner.execute("update", pipeline.at)
    assert result["state"] == "failed" and result["reason"] == "readback_failed"


def test_worker_cannot_start_without_wrapper_marker(monkeypatch):
    monkeypatch.delenv("ASHARE_PRIVATE_WORKER", raising=False)
    assert runner.main() == 2
