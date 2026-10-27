"""Fail-closed public wrapper; worker output is discarded, never redacted later.

This file does not configure or attest GitHub environment protection. Its fixed
environment gates are an additional check, not a substitute for reviewing the
actual branch restrictions, approvals, credentials and workflow before use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile


COMPUTE_REPOSITORY = "MatokiJZ/ashare-observer-public"
STORAGE_REPOSITORY = "MatokiJZ/ashare-observer"
MODES = {"initial", "update", "fallback", "close", "train"}
STAGES = ("input", "research", "publish", "readback")
COUNTS = ("reports", "models", "checkpoints")
STAGE_STATES = {"verified", "failed", "not_started", "pending", "skipped"}
FAILURE_REASONS = {
    "input_invalid", "research_incomplete", "publication_failed",
    "readback_failed", "internal_error",
}
SKIP_REASONS = {"nontrading_day", "already_verified", "reused_training"}
TIMEOUT_SECONDS = 660
RECEIPT_LIMIT = 65536


def public_status(state, reason, mode=None):
    result = {"scope": "live_manual_validation", "state": state,
              "accepted": False, "reason": reason}
    if mode in MODES:
        result["mode"] = mode
    return result


def validate_environment(env):
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": COMPUTE_REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "ASHARE_STORAGE_REPO": STORAGE_REPOSITORY,
        "ASHARE_PRIVATE_RUN_APPROVED": "1",
        "ASHARE_PROTECTED_ENVIRONMENT": "private-storage",
    }
    if any(env.get(key) != value for key, value in expected.items()):
        return False
    return (env.get("TASK_MODE") in MODES
            and re.fullmatch(r"[a-f0-9]{40}", env.get("GITHUB_SHA", "")) is not None
            and bool(env.get("ASHARE_STORAGE_TOKEN", "").strip()))


def validate_receipt(value, mode, returncode):
    """Copy only fixed enums and bounded integer counts into public output."""
    invalid = public_status("failed", "invalid_receipt", mode)
    if not isinstance(value, dict) or set(value) - {
            "schema_version", "mode", "state", "stages", "counts", "reason"}:
        return invalid, 1
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("mode") != mode:
        return invalid, 1
    stages, counts = value.get("stages"), value.get("counts")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        return invalid, 1
    if any(type(stages[k]) is not str or stages[k] not in STAGE_STATES for k in STAGES):
        return invalid, 1
    if not isinstance(counts, dict) or set(counts) != set(COUNTS):
        return invalid, 1
    if any(type(counts[k]) is not int or not 0 <= counts[k] <= 1_000_000 for k in COUNTS):
        return invalid, 1
    state, reason = value.get("state"), value.get("reason")
    if state == "verified":
        sufficient = (counts["checkpoints"] >= 1
                      and (counts["models"] >= 1 if mode == "train" else counts["reports"] >= 1))
        if returncode != 0 or reason not in (None, "", "complete") or any(stages[k] != "verified" for k in STAGES) or not sufficient:
            return invalid, 1
        result = public_status("verified", "complete", mode)
        result.update(accepted=True, stages={k: stages[k] for k in STAGES}, counts={k: counts[k] for k in COUNTS})
        return result, 0
    if state == "skipped" and reason in SKIP_REASONS:
        return public_status("skipped", reason, mode), 3
    if state == "failed" and reason in FAILURE_REASONS:
        return public_status("failed", reason, mode), 1
    return invalid, 1


def read_receipt(path):
    # Never follow a worker-supplied symlink or read an unbounded JSON payload.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise ValueError("invalid receipt")
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("invalid receipt")
    with os.fdopen(fd, "rb") as stream:
        body = stream.read(RECEIPT_LIMIT + 1)
    if len(body) > RECEIPT_LIMIT:
        raise ValueError("invalid receipt")
    return json.loads(body)


def terminate_group(process):
    # Kill descendants as well, including after a parent exits early.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def worker_environment(env, temporary, receipt):
    # Do not forward workflow command files, unrelated credentials, personal
    # account metadata, user-selected data paths or Python startup settings.
    root = Path(__file__).resolve().parents[1]
    result = {
        "PATH": os.defpath,
        "PYTHONPATH": str(root / "backend"),
        "HOME": str(temporary / "home"),
        "TMPDIR": str(temporary / "tmp"),
        "LANG": "C.UTF-8",
        "ASHARE_DATA_DIR": str(temporary / "data"),
        "ASHARE_PRIVATE_RECEIPT": str(receipt),
        "ASHARE_PRIVATE_WORKER": "1",
        "ASHARE_MONITOR": "0",
        "ASHARE_COMPUTE_REPO": COMPUTE_REPOSITORY,
        "ASHARE_COMPUTE_WORKFLOW": "private-research.yml",
        "ASHARE_GITHUB_REPO": STORAGE_REPOSITORY,
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
    }
    for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF", "GITHUB_EVENT_NAME", "GITHUB_SHA",
                "ASHARE_STORAGE_REPO", "ASHARE_STORAGE_TOKEN", "ASHARE_PRIVATE_RUN_APPROVED",
                "ASHARE_PROTECTED_ENVIRONMENT", "TASK_MODE"):
        result[key] = env[key]
    if re.fullmatch(r"[0-9]{1,20}", env.get("GITHUB_RUN_ID", "")):
        result["GITHUB_RUN_ID"] = env["GITHUB_RUN_ID"]
    for name in ("home", "tmp", "data"):
        (temporary / name).mkdir(mode=0o700)
    return result


def run(env=None, timeout=TIMEOUT_SECONDS):
    env = dict(os.environ if env is None else env)
    mode = env.get("TASK_MODE")
    if not validate_environment(env):
        return public_status("blocked", "authorization_or_context_missing", mode), 2
    try:
        with tempfile.TemporaryDirectory(prefix="ashare-private-") as directory:
            temporary = Path(directory)
            receipt = temporary / "receipt.json"
            child_env = worker_environment(env, temporary, receipt)
            process = None
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "ashare.private_runner"],
                    cwd=temporary, env=child_env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                )
                returncode = process.wait(timeout=timeout)
                return validate_receipt(read_receipt(receipt), mode, returncode)
            except subprocess.TimeoutExpired:
                return public_status("failed", "timeout", mode), 1
            except (OSError, ValueError, TypeError):
                return public_status("failed", "invalid_receipt", mode), 1
            finally:
                if process is not None:
                    terminate_group(process)
    except Exception:
        return public_status("failed", "internal_error", mode), 1


def main():
    try:
        result, exitcode = run()
    except BaseException:
        result, exitcode = public_status("failed", "internal_error"), 1
    print(json.dumps(result, sort_keys=True))
    return exitcode


if __name__ == "__main__":
    raise SystemExit(main())
