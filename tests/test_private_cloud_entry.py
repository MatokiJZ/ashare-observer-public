"""Exercise the wrapper using synthetic child processes, never real inputs."""
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def entry():
    path = Path(__file__).resolve().parents[1] / "scripts/private_cloud_entry.py"
    spec = importlib.util.spec_from_file_location("private_cloud_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def approved_env():
    return {
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MatokiJZ/ashare-observer-public",
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123",
        "ASHARE_STORAGE_REPO": "MatokiJZ/ashare-observer",
        "ASHARE_STORAGE_TOKEN": "synthetic-placeholder",
        "ASHARE_PRIVATE_RUN_APPROVED": "1", "ASHARE_PROTECTED_ENVIRONMENT": "private-storage",
        "TASK_MODE": "update",
    }


def verified(mode="update"):
    return {"schema_version": 1, "mode": mode, "state": "verified",
            "stages": {name: "verified" for name in ("input", "research", "publish", "readback")},
            "counts": {"reports": 1, "models": int(mode == "train"), "checkpoints": 1}}


def synthetic_worker(monkeypatch, entry, code):
    original = subprocess.Popen
    calls = []

    def launch(command, **kwargs):
        assert command == [sys.executable, "-m", "ashare.private_runner"]
        calls.append(kwargs)
        return original([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(entry.subprocess, "Popen", launch)
    return calls


@pytest.mark.parametrize("key,value", [
    ("GITHUB_ACTIONS", "false"), ("GITHUB_REPOSITORY", "example/fork"),
    ("GITHUB_REF", "refs/heads/untrusted"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_SHA", "bad"),
    ("ASHARE_STORAGE_REPO", "example/wrong"), ("ASHARE_STORAGE_TOKEN", ""),
    ("ASHARE_PRIVATE_RUN_APPROVED", "0"), ("ASHARE_PROTECTED_ENVIRONMENT", "wrong"),
    ("TASK_MODE", "probe"),
])
def test_missing_gate_never_starts_worker_or_creates_temp(entry, approved_env, monkeypatch, key, value):
    approved_env[key] = value
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: pytest.fail("started worker"))
    monkeypatch.setattr(entry.tempfile, "TemporaryDirectory", lambda *a, **k: pytest.fail("created private directory"))
    result, code = entry.run(approved_env)
    assert code == 2 and result["state"] == "blocked" and not result["accepted"]


def test_native_python_and_descendant_output_is_discarded(entry, approved_env, monkeypatch, capfd):
    payload = verified()
    code = "\n".join([
        "import os,json,subprocess,sys,pathlib",
        "noise='PRIVATE_SENTINEL synthetic-person@example.invalid fake-secret-value'",
        "print(noise,flush=True)",
        "os.write(2,noise.encode())",
        "subprocess.run([sys.executable,'-c','import os;os.write(1,b\"CHILD_PRIVATE_SENTINEL\")'])",
        f"pathlib.Path(os.environ['ASHARE_PRIVATE_RECEIPT']).write_text({json.dumps(json.dumps(payload))})",
    ])
    approved_env.update(GITHUB_OUTPUT="/not/forwarded", GITHUB_TOKEN="not-forwarded",
                        PRIVATE_ACCOUNT_EMAIL="not-forwarded", ASHARE_DATA_DIR="/not/user/data",
                        PYTHONSTARTUP="/not/forwarded")
    calls = synthetic_worker(monkeypatch, entry, code)
    result, exitcode = entry.run(approved_env)
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""
    assert exitcode == 0 and result["accepted"]
    assert len(calls) == 1
    worker_env = calls[0]["env"]
    assert not any(key in worker_env for key in ("GITHUB_OUTPUT", "GITHUB_TOKEN", "PRIVATE_ACCOUNT_EMAIL", "PYTHONSTARTUP"))
    assert not Path(calls[0]["cwd"]).exists()
    assert worker_env["ASHARE_DATA_DIR"] != approved_env["ASHARE_DATA_DIR"]


def test_timeout_kills_group_and_cleans_temp(entry, approved_env, monkeypatch, capfd):
    calls = synthetic_worker(monkeypatch, entry, "import time,os;os.write(2,b'PRIVATE_SENTINEL');time.sleep(10)")
    result, code = entry.run(approved_env, timeout=.1)
    assert code == 1 and result["reason"] == "timeout" and not result["accepted"]
    assert capfd.readouterr().err == ""
    assert not Path(calls[0]["cwd"]).exists()


@pytest.mark.parametrize("mutation", [
    lambda p: p["counts"].update(reports=0),
    lambda p: p["counts"].update(checkpoints=0),
    lambda p: p["counts"].update(reports=True),
    lambda p: p["counts"].update(reports=1_000_001),
    lambda p: p["counts"].update(reports="PRIVATE_SENTINEL"),
    lambda p: p["stages"].update(readback="failed"),
    lambda p: p["stages"].update(readback="PRIVATE_SENTINEL"),
    lambda p: p.update(mode="train"),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(private_text="PRIVATE_SENTINEL"),
])
def test_invalid_or_incomplete_receipt_cannot_pass(entry, mutation):
    payload = verified()
    mutation(payload)
    result, code = entry.validate_receipt(payload, "update", 0)
    assert code != 0 and not result["accepted"]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def test_worker_failure_cannot_use_success_receipt(entry):
    result, code = entry.validate_receipt(verified(), "update", 1)
    assert code != 0 and not result["accepted"]


@pytest.mark.parametrize("reason", ["nontrading_day", "already_verified", "reused_training"])
def test_skip_is_not_green_acceptance(entry, reason):
    payload = verified()
    payload.update(state="skipped", reason=reason)
    payload["stages"] = {name: "pending" for name in payload["stages"]}
    result, code = entry.validate_receipt(payload, "update", 0)
    assert code == 3 and result["state"] == "skipped" and not result["accepted"]


def test_training_requires_model_count(entry):
    payload = verified("train")
    payload["reason"] = "complete"
    payload["counts"]["models"] = 0
    assert not entry.validate_receipt(payload, "train", 0)[0]["accepted"]
    payload["counts"]["models"] = 1
    assert entry.validate_receipt(payload, "train", 0)[0]["accepted"]
    payload["counts"]["checkpoints"] = 0
    assert not entry.validate_receipt(payload, "train", 0)[0]["accepted"]


def test_symlink_fifo_and_oversize_receipts_rejected(entry, tmp_path):
    target = tmp_path / "data.json"
    target.write_text(json.dumps(verified()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        entry.read_receipt(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        entry.read_receipt(fifo)
    target.write_text("x" * (entry.RECEIPT_LIMIT + 1))
    with pytest.raises(ValueError):
        entry.read_receipt(target)


def test_exception_payload_does_not_reach_public_output(entry, approved_env, monkeypatch, capfd):
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PRIVATE_SENTINEL")))
    result, code = entry.run(approved_env)
    assert code == 1 and "PRIVATE_SENTINEL" not in json.dumps(result)
    assert capfd.readouterr().err == ""


def test_template_is_inert_and_secret_task_is_main_only():
    root = Path(__file__).resolve().parents[1]
    path = root / "docs/templates/private-research.yml.example"
    text = path.read_text()
    assert "NOT_READY" in text and "workflow_dispatch:" in text
    assert "refs/heads/main" in text and "environment: private-storage" in text
    assert "contents: read" in text and "persist-credentials: false" in text
    assert "runs-on: ubuntu-latest" in text
    assert text.index("Install public dependencies") < text.index("Create scoped installation token")
    for forbidden in ("schedule:", "pull_request:", "pull_request_target:", "upload-artifact", "cache:", "actions: write"):
        assert forbidden not in text
    assert not re.search(r"^\s+contents: write$", text, re.M)
    assert not (root / ".github/workflows/private-research.yml").exists()
