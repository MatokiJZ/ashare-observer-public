"""Synthetic Git API state, with immutable reads; never contacts a repository."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import sys
import tarfile
import types

import httpx
import pytest

from ashare import cloud, storage
from ashare.core import atomic_json, data_dir, digest

REAL_CLIENT = httpx.Client


def missing(path):
    request = httpx.Request("GET", "https://api.github.com/repos/example/private/" + path)
    return httpx.HTTPStatusError("synthetic missing", request=request, response=httpx.Response(404, request=request))


class FakeGit:
    repo = "example/private"

    def __init__(self):
        self.heads = {}
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.requests = []
        self.reads = []
        self.lose_ref_response = False
        self.corrupt_readback = False

    @property
    def head(self):
        return self.heads.get("data")

    @head.setter
    def head(self, value):
        self.heads["data"] = value

    def api(self, path, method="GET", body=None):
        self.requests.append((path, method, deepcopy(body)))
        if path.startswith("git/ref/heads/"):
            head = self.heads.get(path.removeprefix("git/ref/heads/"))
            if not head:
                raise missing(path)
            return {"object": {"sha": head}}
        if path.startswith("git/commits/"):
            return {"tree": {"sha": self.commits[path.split("/")[-1]]}}
        if path.startswith("git/trees/"):
            reference = path.split("/")[-1].split("?")[0]
            tree = self.trees[self.commits.get(reference, reference)]
            return {"tree": [{"path": p, "sha": sha} for p, sha in tree.items()]}
        if path == "git/blobs":
            raw = base64.b64decode(body["content"])
            sha = hashlib.sha1(raw).hexdigest()
            self.blobs[sha] = raw
            return {"sha": sha}
        if path == "git/trees":
            tree = dict(self.trees.get(body.get("base_tree"), {}))
            tree.update({row["path"]: row["sha"] for row in body["tree"]})
            sha = "tree-" + str(len(self.trees))
            self.trees[sha] = tree
            return {"sha": sha}
        if path == "git/commits":
            sha = "commit-" + str(len(self.commits))
            self.commits[sha] = body["tree"]
            return {"sha": sha}
        if path == "git/refs" or path.startswith("git/refs/heads/"):
            branch = body["ref"].removeprefix("refs/heads/") if path == "git/refs" else path.removeprefix("git/refs/heads/")
            self.heads[branch] = body["sha"]
            if self.lose_ref_response:
                self.lose_ref_response = False
                raise httpx.ReadTimeout("synthetic lost response")
            return {"object": {"sha": self.heads[branch]}}
        raise AssertionError((path, method))

    def content(self, path, branch="data"):
        self.reads.append((path, branch))
        commit = self.heads.get(branch, branch)
        tree = self.trees[self.commits[commit]]
        if path not in tree:
            raise missing("contents/" + path)
        value = self.blobs[tree[path]]
        if self.corrupt_readback and not path.endswith(".manifest"):
            return value + b"corrupted"
        return value


@pytest.fixture(autouse=True)
def write_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.delenv("ASHARE_WRITE_BRANCH", raising=False)


@pytest.fixture
def git(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(cloud, "GitHub", lambda: git)
    monkeypatch.delenv("ASHARE_COMPUTE_REPO", raising=False)
    monkeypatch.delenv("ASHARE_LEGACY_ARTIFACTS", raising=False)
    return git


def report():
    return {"schema_version": 1, "strategy_version": "synthetic",
            "id": "2026-10-20-080000-update", "trade_date": "2026-10-20",
            "generated_at": "2026-10-20T08:00:00+08:00", "candidates": []}


def make_model():
    identifier = "2026-10-20-080000-1234abcd"
    root = data_dir() / "models" / identifier
    root.mkdir(parents=True)
    files = {"return.txt": b"synthetic-return", "risk.txt": b"synthetic-risk"}
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    atomic_json(root / "manifest.json", {"id": identifier, "schema": "synthetic-v1", "features": ["synthetic"],
        "generated_at": "2026-10-20T08:00:00+08:00", "files": {name: digest(raw) for name, raw in files.items()}})
    return root


def test_report_write_and_noop_both_read_back_hash_and_schema(git):
    first = cloud.publish_report(report())
    assert first["state"] == "verified" and first["published"] == first["files"] == 1
    assert git.reads and all(ref.startswith("commit-") for _, ref in git.reads)
    commits = len(git.commits)
    second = cloud.publish_report(report())
    assert second["state"] == "verified" and second["published"] == 0
    assert len(git.commits) == commits


def test_report_schema_rejected_before_any_remote_write(git):
    with pytest.raises(ValueError, match="格式"):
        cloud.publish_report({"id": "bad"})
    assert git.requests == []


def test_same_report_id_different_bytes_cannot_overwrite(git):
    cloud.publish_report(report())
    old = git.head
    changed = {**report(), "strategy_version": "different"}
    with pytest.raises(ValueError, match="回读"):
        cloud.publish_report(changed)
    assert git.head == old


def test_corrupt_remote_bytes_never_return_verified(git):
    git.corrupt_readback = True
    with pytest.raises(ValueError, match="回读"):
        cloud.publish_report(report())


def test_bad_hash_receipt_not_trusted_on_idempotent_run(git):
    cloud.publish_report(report())
    tree = git.trees[git.commits[git.head]]
    manifest = "reports/" + report()["id"] + ".json.manifest"
    git.blobs[tree[manifest]] = b'{"sha256":"wrong","bytes":1}'
    with pytest.raises(ValueError, match="回读"):
        cloud.publish_report(report())


def test_lost_ref_response_checks_outcome_without_duplicate_commit(git):
    git.lose_ref_response = True
    result = cloud.publish_report(report())
    assert result["state"] == "verified" and result["recovered_uncertain_ref"]
    assert len(git.commits) == 1


def test_model_bundle_requires_complete_hash_matched_files(git):
    root = make_model()
    (root / "risk.txt").unlink()
    with pytest.raises(ValueError, match="不完整"):
        cloud.publish_research_assets()
    assert not git.requests
    (root / "risk.txt").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="校验"):
        cloud.publish_research_assets()
    assert not git.requests


def test_complete_model_bundle_is_read_back_and_does_not_activate(git):
    make_model()
    result = cloud.publish_research_assets()
    assert result["state"] == "verified" and result["files"] == 3
    assert len(git.reads) == 6
    assert storage.snapshot("model_control") == {}


def test_empty_research_is_not_reported_as_verified(git):
    assert cloud.publish_research_assets() == {"state": "empty", "files": 0, "published": 0}
    assert not git.requests


@pytest.mark.parametrize("value", [
    {"state": "running"}, {"state": "complete", "started_at": "bad", "finished_at": "bad"},
    {"state": "insufficient", "started_at": "2026-10-20T09:00:00+08:00", "finished_at": "2026-10-20T08:00:00+08:00"},
])
def test_training_receipt_needs_terminal_state_and_ordered_times(git, value):
    atomic_json(data_dir() / "training_runs/2026-10-20-080000.json", value)
    with pytest.raises((ValueError, KeyError)):
        cloud.publish_research_assets()
    assert not git.requests


def test_run_receipt_is_persistent_verified_and_immutable(git):
    key = "2026-10-20-update"
    value = {"schema_version": 1, "state": "verified", "mode": "update", "day": "2026-10-20", "stages": ["synthetic"]}
    assert cloud.get_run_receipt(key) is None
    assert cloud.publish_run_receipt(key, value)["state"] == "verified"
    assert cloud.get_run_receipt(key) == value
    assert cloud.publish_run_receipt(key, value)["published"] == 0
    with pytest.raises(ValueError, match="回读"):
        cloud.publish_run_receipt(key, {**value, "stages": []})
    with pytest.raises(ValueError, match="格式"):
        cloud.publish_run_receipt(key, {**value, "mode": "train"})


def test_run_receipt_missing_manifest_is_failure_not_missing_run(git):
    key = "2026-10-20-update"
    value = {"schema_version": 1, "state": "verified", "mode": "update", "day": "2026-10-20"}
    cloud.publish_run_receipt(key, value)
    tree = git.trees[git.commits[git.head]]
    del tree[f"migration_runs/{key}.json.manifest"]
    with pytest.raises(httpx.HTTPStatusError):
        cloud.get_run_receipt(key)


def test_release_not_found_uses_bootstrap_without_actions_permission(git, monkeypatch):
    class NotFound(Exception): pass
    def absent(*a, **kw): raise NotFound()
    module = types.SimpleNamespace(CheckpointNotFound=NotFound, restore_checkpoint=absent)
    monkeypatch.setitem(sys.modules, "ashare.private_checkpoints", module)
    monkeypatch.setattr(cloud, "bootstrap_checkpoint", lambda gh: {"day_files": 2})
    result = cloud.sync_checkpoint(git)
    assert result["source"] == "bootstrap_release" and result["degraded"]
    assert result["day_files"] == 2 and not result["readback_verified"]
    assert not git.requests


def test_invalid_new_release_does_not_fall_back_to_old_seed(git, monkeypatch):
    class NotFound(Exception): pass
    def corrupted(*a, **kw): raise ValueError("synthetic checksum mismatch")
    module = types.SimpleNamespace(CheckpointNotFound=NotFound, restore_checkpoint=corrupted)
    monkeypatch.setitem(sys.modules, "ashare.private_checkpoints", module)
    monkeypatch.setattr(cloud, "bootstrap_checkpoint", lambda gh: pytest.fail("Must not hide corrupted release"))
    with pytest.raises(ValueError, match="checksum"):
        cloud.sync_checkpoint(git)


def test_bootstrap_inspects_entire_archive_before_restoring(monkeypatch):
    from ashare import private_checkpoints
    sequence = []
    gh = types.SimpleNamespace(api=lambda path: {"id": 2, "assets": [{"id": 1, "name": "checkpoint.tar.gz", "size": 1}]})
    monkeypatch.setattr(private_checkpoints, "download_asset", lambda *a, **kw: b"synthetic")
    def inspect(path):
        sequence.append("inspect")
        assert path.read_bytes() == b"synthetic"
        return {"day_files": 3, "last_date": "2026-10-20", "sha256": "synthetic-hash"}
    def restore(path, require_bars):
        sequence.append("restore")
        assert require_bars and sequence == ["inspect", "restore"]
        return {"day_files": 3}
    monkeypatch.setattr(private_checkpoints, "inspect_checkpoint", inspect)
    monkeypatch.setattr(cloud, "unpack_checkpoint", restore)
    assert cloud.bootstrap_checkpoint(gh) == {"day_files": 3, "last_date": "2026-10-20", "sha256": "synthetic-hash",
                                            "release_id": 2, "asset_id": 1, "tag": "data-bootstrap"}
    assert sequence == ["inspect", "restore"]
    assert not (data_dir() / "bootstrap.tar.gz").exists()


@pytest.mark.parametrize("invalid", ["parquet", "json"])
def test_bootstrap_bad_payload_does_not_partially_write_local_data(monkeypatch, invalid):
    from ashare import private_checkpoints
    bodies = {"universe.json": b'{"fetched_at":"2026-10-20T10:00:00+08:00","items":[{"code":"600001"}]}',
              "calendar.json": b'{"dates":["2026-10-20"]}',
              "bars/day/600001.parquet": b"not-a-parquet-file"}
    if invalid == "json":
        bodies["calendar.json"] = b"invalid-json"
    manifest = json.dumps({"schema_version": 1, "files": {p: digest(raw) for p, raw in bodies.items()}}).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name, raw in [*bodies.items(), ("manifest.json", manifest)]:
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    atomic_json(data_dir() / "universe.json", {"fetched_at": "2026-10-19T10:00:00+08:00", "items": [{"code": "600002"}]})
    atomic_json(data_dir() / "calendar.json", {"dates": ["2026-10-19"]})
    before = {p.name: p.read_bytes() for p in data_dir().iterdir() if p.is_file()}
    gh = types.SimpleNamespace(api=lambda path: {"assets": [{"id": 1, "name": "checkpoint.tar.gz", "size": len(archive.getvalue())}]})
    monkeypatch.setattr(private_checkpoints, "download_asset", lambda *a, **kw: archive.getvalue())
    with pytest.raises(private_checkpoints.CheckpointValidationError):
        cloud.bootstrap_checkpoint(gh)
    after = {p.name: p.read_bytes() for p in data_dir().iterdir() if p.is_file()}
    assert after == before
    assert not (data_dir() / "bars/day/600001.parquet").exists()


def test_public_compute_status_never_receives_private_auth(git, monkeypatch):
    monkeypatch.setenv("ASHARE_COMPUTE_REPO", "example/public")
    seen = []
    def handle(request):
        seen.append(request)
        assert "authorization" not in request.headers
        if request.url.path == "/repos/example/public":
            return httpx.Response(200, json={"full_name": "example/public", "private": False})
        assert "private-research.yml" in request.url.path
        return httpx.Response(200, json={"workflow_runs": [{"id": 123, "status": "completed", "conclusion": "success"}]})
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kw: REAL_CLIENT(**kw, transport=httpx.MockTransport(handle)))
    cloud.update_compute_status(git)
    assert len(seen) == 3 and not git.requests
    assert storage.snapshot("cloud_status")["source"] == "public_compute"
    assert "未按日报与训练模式区分" in storage.snapshot("training_cloud_status")["notice"]


def test_unverified_compute_status_is_nonblocking_and_explicit(git, monkeypatch):
    monkeypatch.setenv("ASHARE_COMPUTE_REPO", "example/public")
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kw: REAL_CLIENT(**kw, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"full_name": "example/public", "private": True}))))
    cloud.update_compute_status(git)
    assert storage.snapshot("cloud_status")["state"] == "unavailable"
    assert not git.requests


@pytest.mark.parametrize("value,actions,expected", [
    (None, False, "data"), ("data", False, "data"),
    ("migration-acceptance", False, "migration-acceptance"),
    ("migration-acceptance", True, "migration-acceptance"),
])
def test_allowed_write_namespaces(monkeypatch, value, actions, expected):
    monkeypatch.setenv("GITHUB_ACTIONS", str(actions).lower())
    if value is not None:
        monkeypatch.setenv("ASHARE_WRITE_BRANCH", value)
    assert cloud.write_branch() == expected


@pytest.mark.parametrize("value,actions", [(None, True), ("data", True), ("main", True), ("main", False)])
def test_invalid_write_namespace_fails_before_remote_access(git, monkeypatch, value, actions):
    monkeypatch.setenv("GITHUB_ACTIONS", str(actions).lower())
    if value is not None:
        monkeypatch.setenv("ASHARE_WRITE_BRANCH", value)
    monkeypatch.setattr(cloud, "GitHub", lambda: pytest.fail("No client or credentials before namespace check"))
    with pytest.raises(ValueError):
        cloud.publish_report(report())
    with pytest.raises(ValueError):
        cloud.publish_research_assets()
    with pytest.raises(ValueError):
        cloud.publish_run_receipt("2026-10-20-update", {})
    with pytest.raises(ValueError):
        cloud.get_run_receipt("2026-10-20-update")
    assert not git.requests


def acceptance(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASHARE_WRITE_BRANCH", "migration-acceptance")


def test_acceptance_starts_independent_root_and_preserves_production(git, monkeypatch):
    cloud.publish_report(report())
    production = git.head
    production_tree = deepcopy(git.trees[git.commits[production]])
    before = len(git.requests)
    acceptance(monkeypatch)
    result = cloud.publish_report({**report(), "strategy_version": "synthetic-acceptance"})
    assert result["branch"] == "migration-acceptance" and result["published"] == 1
    assert git.head == production and git.trees[git.commits[production]] == production_tree
    writes = [(p, b) for p, m, b in git.requests[before:] if m != "GET"]
    commit = next(b for p, b in writes if p == "git/commits")
    assert commit["parents"] == []
    assert not any(p == "git/refs/heads/data" or b.get("ref") == "refs/heads/data" for p, b in writes)
    assert git.heads["migration-acceptance"] != production


def test_production_receipt_does_not_skip_or_conflict_with_acceptance(git, monkeypatch):
    key = "2026-10-20-update"
    value = {"schema_version": 1, "state": "verified", "mode": "update", "day": "2026-10-20", "stages": ["production"]}
    cloud.publish_run_receipt(key, value)
    production = git.head
    acceptance(monkeypatch)
    assert cloud.get_run_receipt(key) is None
    isolated = {**value, "stages": ["acceptance"]}
    assert cloud.publish_run_receipt(key, isolated)["branch"] == "migration-acceptance"
    assert cloud.get_run_receipt(key) == isolated
    assert cloud.publish_run_receipt(key, isolated)["published"] == 0
    make_model()
    assert cloud.publish_research_assets()["branch"] == "migration-acceptance"
    assert git.head == production
    assert json.loads(git.content(f"migration_runs/{key}.json", production)) == value
    assert not any(p.startswith("models/") for p in git.trees[git.commits[production]])


def test_uncertain_acceptance_ref_reads_acceptance_without_changing_production(git, monkeypatch):
    cloud.publish_report(report())
    production = git.head
    acceptance(monkeypatch)
    git.lose_ref_response = True
    result = cloud.publish_report(report())
    assert result["recovered_uncertain_ref"] and result["branch"] == "migration-acceptance"
    assert result["commit"] == git.heads["migration-acceptance"] and git.head == production
    assert len(git.commits) == 2


@pytest.mark.parametrize("path,method,body", [
    ("git/refs", "POST", {"ref": "refs/heads/data", "sha": "a" * 40}),
    ("git/refs/heads/data", "PATCH", {"sha": "a" * 40, "force": False}),
    ("git/refs/heads/migration-acceptance", "PATCH", {"sha": "a" * 40, "force": True}),
    ("git/refs/heads/migration-acceptance", "DELETE", {}),
])
def test_low_level_ref_guard_rejects_production_or_force_before_http(monkeypatch, path, method, body):
    acceptance(monkeypatch)
    gh = cloud.GitHub.__new__(cloud.GitHub)
    gh.require_private = lambda: pytest.fail("Ref rejection must precede HTTP")
    with pytest.raises(ValueError, match="拒绝"):
        gh.api(path, method, body)


def test_input_reads_stay_pinned_to_production_even_when_head_advances(git, monkeypatch):
    cloud.publish_report(report())
    make_model()
    cloud.publish_research_assets()
    source = "a" * 40
    git.commits[source] = git.commits[git.head]
    git.head = source
    # Force reads from the remote snapshot, not files left by fixture setup.
    for path in (data_dir() / "models").rglob("*"):
        if path.is_file():
            path.unlink()
    acceptance(monkeypatch)
    monkeypatch.setattr(cloud, "sync_checkpoint", lambda gh: {"state": "success", "day_files": 1})
    monkeypatch.setattr(cloud, "update_compute_status", lambda gh: None)
    original = git.api
    def advance_after_listing(path, method="GET", body=None):
        result = original(path, method, body)
        if path == f"git/trees/{source}?recursive=1":
            git.head = "b" * 40  # deliberately unreadable new head
        return result
    monkeypatch.setattr(git, "api", advance_after_listing)
    git.reads.clear()
    result = cloud.sync_reports()
    assert result["state"] == "success", result
    assert result["data_revision"] == {"branch": "data", "commit": source}
    assert git.reads and all(ref == source for _, ref in git.reads)
    assert any(path.startswith("reports/") for path, _ in git.reads)
    assert any(path.startswith("models/") for path, _ in git.reads)
