"""Offline boundary tests; these do not prove real private storage migration."""
import json
from pathlib import Path

import httpx
import pytest

from ashare import cloud
from ashare.core import atomic_json, data_dir


REAL_TOKEN = cloud.token
REAL_CLIENT = httpx.Client
PRIVATE_REPO = "example/private-data"
COMPUTE_REPO = "example/public-code"


@pytest.fixture(autouse=True)
def isolated_storage_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.delenv("ASHARE_STORAGE_REPO", raising=False)
    monkeypatch.delenv("ASHARE_STORAGE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("ASHARE_WRITE_BRANCH", raising=False)


def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", COMPUTE_REPO)
    monkeypatch.setenv("ASHARE_STORAGE_REPO", PRIVATE_REPO)
    monkeypatch.setenv("ASHARE_WRITE_BRANCH", "migration-acceptance")


def client_transport(monkeypatch, handler):
    monkeypatch.setattr(cloud, "token", lambda: "synthetic-test-token")
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kw: REAL_CLIENT(
        **kw, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("storage,compute", [
    ("", COMPUTE_REPO), (PRIVATE_REPO, ""),
    (COMPUTE_REPO, COMPUTE_REPO), (COMPUTE_REPO.upper(), COMPUTE_REPO),
])
def test_actions_requires_separate_explicit_storage(monkeypatch, storage, compute):
    actions(monkeypatch)
    monkeypatch.setenv("ASHARE_STORAGE_REPO", storage)
    monkeypatch.setenv("GITHUB_REPOSITORY", compute)
    with pytest.raises(ValueError, match="显式配置"):
        cloud.GitHub()


def test_actions_rejects_client_for_another_repository(monkeypatch):
    actions(monkeypatch)
    with pytest.raises(ValueError, match="配置以外"):
        cloud.GitHub("example/other-data")


def test_actions_does_not_fall_back_to_other_credentials(monkeypatch):
    actions(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-compute-token")
    monkeypatch.setenv("ASHARE_GITHUB_TOKEN", "synthetic-legacy-token")

    def forbidden(*a, **kw):
        raise AssertionError("Local credentials must never be consulted")

    monkeypatch.setattr(Path, "home", forbidden)
    monkeypatch.setattr(cloud.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="ASHARE_STORAGE_TOKEN"):
        REAL_TOKEN()
    monkeypatch.setenv("ASHARE_STORAGE_TOKEN", "synthetic-storage-token")
    assert REAL_TOKEN() == "synthetic-storage-token"


@pytest.mark.parametrize("metadata", [
    {}, {"private": False, "full_name": PRIVATE_REPO},
    {"private": "true", "full_name": PRIVATE_REPO},
    {"private": True, "full_name": "example/other-data"},
])
def test_unknown_public_or_wrong_target_fails_before_data_access(monkeypatch, metadata):
    actions(monkeypatch)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)

    client_transport(monkeypatch, handle)
    with pytest.raises(ValueError, match="私有仓库"):
        cloud.GitHub()
    assert [(r.method, r.url.path) for r in requests] == [("GET", f"/repos/{PRIVATE_REPO}")]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_unverifiable_metadata_fails_closed(monkeypatch, status):
    actions(monkeypatch)
    client_transport(monkeypatch, lambda request: httpx.Response(status, text="synthetic failure"))
    with pytest.raises(ValueError, match="无法核实"):
        cloud.GitHub()


def test_local_legacy_read_target_still_works_and_storage_override_is_consistent(monkeypatch):
    monkeypatch.setattr(cloud, "config", lambda: {"github_repo": PRIVATE_REPO})
    assert cloud.storage_repository() == PRIVATE_REPO
    monkeypatch.setenv("ASHARE_STORAGE_REPO", "example/new-private-data")
    assert cloud.storage_repository() == "example/new-private-data"
    with pytest.raises(ValueError, match="配置以外"):
        cloud.storage_repository(PRIVATE_REPO)


def test_valid_private_metadata_and_root_api_path(monkeypatch):
    actions(monkeypatch)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == f"/repos/{PRIVATE_REPO}"
        return httpx.Response(200, json={"private": True, "full_name": PRIVATE_REPO})

    client_transport(monkeypatch, handle)
    gh = cloud.GitHub()
    assert gh.repo == PRIVATE_REPO
    assert gh.api("")["private"] is True
    assert len(requests) == 2
    gh.client.close()


def test_visibility_change_blocks_mutation(monkeypatch):
    actions(monkeypatch)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"private": len(calls) == 1, "full_name": PRIVATE_REPO})

    client_transport(monkeypatch, handle)
    gh = cloud.GitHub()
    with pytest.raises(ValueError, match="私有仓库"):
        gh.api("git/blobs", "POST", {"content": "synthetic"})
    assert len(calls) == 2
    gh.client.close()


@pytest.mark.parametrize("kind", ["report", "research_asset"])
def test_public_compute_publishes_only_to_private_target_with_bot_metadata(monkeypatch, kind):
    from test_cloud_readback import FakeGit
    actions(monkeypatch)
    git = FakeGit()
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path.startswith(f"/repos/{PRIVATE_REPO}")
        path = request.url.path.removeprefix(f"/repos/{PRIVATE_REPO}/")
        if request.url.path == f"/repos/{PRIVATE_REPO}":
            return httpx.Response(200, json={"private": True, "full_name": PRIVATE_REPO})
        try:
            if path.startswith("contents/"):
                return httpx.Response(200, content=git.content(path[9:], request.url.params.get("ref", "data")))
            if request.url.query:
                path += "?" + request.url.query.decode()
            return httpx.Response(200, json=git.api(path, request.method, json.loads(request.content) if request.content else None))
        except httpx.HTTPStatusError as exc:
            return httpx.Response(exc.response.status_code)

    client_transport(monkeypatch, handle)
    if kind == "report":
        value = {"id": "2026-10-20-080000-update", "generated_at": "2026-10-20T08:00:00+08:00",
                 "trade_date": "2026-10-20", "schema_version": 1, "strategy_version": "synthetic", "candidates": []}
        receipt = cloud.publish_report(value)
    else:
        atomic_json(data_dir() / "training_runs/2026-10-20-080000.json", {
            "state": "insufficient", "started_at": "2026-10-20T08:00:00+08:00", "finished_at": "2026-10-20T08:01:00+08:00"})
        receipt = cloud.publish_research_assets()
    assert receipt["state"] == "verified" and receipt["published"] == 1
    assert git.reads
    commits = [json.loads(r.content) for r in requests if r.method == "POST" and r.url.path.endswith("/git/commits")]
    assert len(commits) == 1
    for identity in (commits[0]["author"], commits[0]["committer"]):
        assert identity == {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}
    assert all(COMPUTE_REPO not in str(r.url) for r in requests)
