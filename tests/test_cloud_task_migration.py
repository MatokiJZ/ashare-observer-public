"""Synthetic control-flow tests; no provider call, real training or publication."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def task(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts/cloud_task.py"
    spec = importlib.util.spec_from_file_location("migration_cloud_task", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("TASK_MODE", "update")
    monkeypatch.setattr(module, "sync_reports", lambda **kw: {
        "state": "success", "checkpoint": {"state": "success"}})

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected research or publication side effect")

    for name in ("publish_report", "publish_research_assets", "pack_checkpoint"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.jobs, "collect", forbidden)
    monkeypatch.setattr(module.jobs, "build_report", forbidden)
    monkeypatch.setattr(module.learning, "train", forbidden)
    return module


def test_live_actions_remain_blocked_even_with_configured_storage(task, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASHARE_STORAGE_REPO", "example/private-storage")
    monkeypatch.setenv("ASHARE_STORAGE_TOKEN", "synthetic-placeholder")
    monkeypatch.setattr(task, "sync_reports", lambda **kw: pytest.fail("Must not read private data"))
    with pytest.raises(SystemExit, match="not enabled"):
        task.main()


@pytest.mark.parametrize("state", ["failed", "partial", "running", "success"])
def test_incomplete_sync_stops_before_research(task, monkeypatch, state):
    monkeypatch.setattr(task, "sync_reports", lambda **kw: {
        "state": state, "checkpoint": {"state": "failed"}})
    with pytest.raises(SystemExit, match="synchronization incomplete"):
        task.main()


@pytest.mark.parametrize("state", ["insufficient", "failed", "running", "complete"])
def test_training_without_complete_model_never_publishes(task, monkeypatch, state):
    monkeypatch.setenv("TASK_MODE", "train")
    monkeypatch.setattr(task.jobs, "collect", lambda *a, **kw: {})
    monkeypatch.setattr(task.learning, "train", lambda: {"state": state})
    with pytest.raises(SystemExit, match="Training incomplete"):
        task.main()


@pytest.mark.parametrize("status,covered", [("partial", 100), ("complete", 0)])
def test_partial_or_empty_report_never_publishes(task, monkeypatch, status, covered):
    monkeypatch.setattr(task.jobs, "build_report", lambda *a, **kw: {
        "id": "synthetic-report", "status": status, "coverage": {"covered": covered}})
    with pytest.raises(SystemExit, match="Report incomplete"):
        task.main()


def test_nontrading_skip_is_explicit_not_complete(task, monkeypatch, capsys):
    monkeypatch.setattr(task.jobs, "build_report", lambda *a, **kw: {"state": "skipped"})
    task.main()
    assert '"state": "skipped_nontrading_day"' in capsys.readouterr().out


def test_probe_success_with_empty_payload_is_rejected(task, monkeypatch):
    monkeypatch.setenv("TASK_MODE", "probe")
    monkeypatch.setattr(task.jobs, "probe", lambda: {
        key: {"ok": True, "empty": True, "rows": 0} for key in ["calendar", "quotes", "history"]})
    with pytest.raises(SystemExit, match="missing required data"):
        task.main()


def test_public_receipt_does_not_include_training_payload(task, monkeypatch, capsys):
    monkeypatch.setenv("TASK_MODE", "train")
    monkeypatch.setattr(task.jobs, "collect", lambda *a, **kw: {})
    monkeypatch.setattr(task.learning, "train", lambda: {
        "state": "complete", "model_id": "synthetic-model",
        "untrusted_detail": "PRIVATE_PAYLOAD_SENTINEL"})
    calls = []
    monkeypatch.setattr(task, "publish_research_assets", lambda: calls.append("publish"))
    task.main()
    output = capsys.readouterr().out
    assert calls == ["publish"]
    assert "PRIVATE_PAYLOAD_SENTINEL" not in output
    assert "synthetic-model" not in output
    assert '"stage": "train", "state": "complete"' in output


def test_manual_validation_workflow_has_no_data_or_schedules():
    source = Path(__file__).resolve().parents[1] / ".github/workflows/migration-validation.yml"
    text = source.read_text()
    assert "workflow_dispatch:" in text
    assert "runs-on: ubuntu-latest" in text
    assert "persist-credentials: false" in text
    assert "contents: read" in text
    for forbidden in ("schedule:", "pull_request_target", "secrets.", "upload-artifact", "cache:", "cloud_task.py", "contents: write"):
        assert forbidden not in text
