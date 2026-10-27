"""Synthetic private Release transport; no credentials or real repository calls."""
import io
import json
import tarfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from ashare import private_checkpoints as pc
from ashare.core import TZ, atomic_json, data_dir

REAL_CLIENT = httpx.Client


class Store:
    repo = "example/private-data"

    def __init__(self):
        self.private = True
        self.releases = {}
        self.assets = {}
        self.calls = []
        self.mutations = []
        self.fail_after = None
        self.redirect = None
        self.client = REAL_CLIENT(base_url="https://api.github.com", headers={"Authorization": "Bearer synthetic"},
                                  transport=httpx.MockTransport(self.handle))

    def require_private(self):
        if not self.private:
            raise pc.CheckpointValidationError("Repository not private")

    def api(self, path, method="GET", body=None):
        response = self.client.request(method, f"/repos/{self.repo}/{path}", json=body)
        response.raise_for_status()
        return response.json()

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host in {"api.github.com", "uploads.github.com"}
        assert request.url.path.startswith(f"/repos/{self.repo}/")
        path = request.url.path.removeprefix(f"/repos/{self.repo}/")
        if request.method != "GET":
            self.mutations.append((request.method, path))
        if path in {"git/ref/heads/data", "git/ref/heads/migration-acceptance"}:
            return httpx.Response(200, json={"object": {"sha": ("a" if path.endswith("/data") else "b") * 40}})
        if path.startswith("releases/tags/"):
            tag = path.removeprefix("releases/tags/")
            value = next((r for r in self.releases.values() if r["tag_name"] == tag and r.get("draft") is False), None)
            return httpx.Response(200, json=value) if value else httpx.Response(404)
        if path == "releases" and request.method == "GET":
            page = int(request.url.params.get("page", 1))
            values = list(self.releases.values())[::-1]
            return httpx.Response(200, json=values[(page - 1) * 100:page * 100])
        if path == "releases" and request.method == "POST":
            body = json.loads(request.content)
            expected = "b" if body["tag_name"].startswith(pc.ACCEPTANCE_TAG_PREFIX) else "a"
            assert body["target_commitish"] == expected * 40
            identifier = len(self.releases) + 1
            value = {"id": identifier, **body}
            self.releases[identifier] = value
            if self.fail_after == "release":
                self.fail_after = None
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(201, json=value)
        if path.startswith("releases/assets/"):
            asset = self.assets[int(path.rsplit("/", 1)[-1])]
            if self.redirect:
                return httpx.Response(302, headers={"location": self.redirect})
            return httpx.Response(200, content=asset["body"])
        if path.startswith("releases/") and path.endswith("/assets"):
            identifier = int(path.split("/")[1])
            if request.method == "GET":
                return httpx.Response(200, json=[{k: v for k, v in a.items() if k != "body"} for a in self.assets.values() if a["release"] == identifier])
            name = request.url.params["name"]
            aid = len(self.assets) + 1
            asset = {"id": aid, "name": name, "size": len(request.content), "body": request.content, "release": identifier}
            self.assets[aid] = asset
            if self.fail_after == name:
                self.fail_after = None
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(201, json={k: v for k, v in asset.items() if k != "body"})
        if path.startswith("releases/") and request.method == "PATCH":
            identifier = int(path.split("/")[1])
            self.releases[identifier].update(json.loads(request.content))
            if self.fail_after == "publish":
                self.fail_after = None
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(200, json=self.releases[identifier])
        pytest.fail(f"Unexpected synthetic request {request.method} {path}")


@pytest.fixture(autouse=True)
def write_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.delenv("ASHARE_WRITE_BRANCH", raising=False)


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "now", lambda: datetime(2026, 10, 20, 17, tzinfo=TZ))
    root = data_dir() / "bars/day"
    root.mkdir(parents=True)
    frame = pd.DataFrame({"date": ["2026-10-19", "2026-10-20"], "open": [10., 11.], "high": [11., 12.],
                          "low": [9., 10.], "close": [10.5, 11.5], "volume": [1e6, 1.2e6], "amount": [1e7, 1.4e7]})
    frame.to_parquet(root / "600000.parquet", index=False)
    atomic_json(data_dir() / "calendar.json", {"dates": ["2026-10-19", "2026-10-20"], "valid_through": "2026-10-20"})
    path = tmp_path / "checkpoint.tar.gz"
    pc.deterministic_checkpoint(path)
    return path


def test_stable_pack_and_private_publication_readback(checkpoint, tmp_path):
    second = tmp_path / "another.tar.gz"
    pc.deterministic_checkpoint(second)
    assert checkpoint.read_bytes() == second.read_bytes()
    gh = Store()
    result = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert result["readback_verified"] is True
    assert result["day_files"] == 1 and result["last_date"] == "2026-10-20"
    assert gh.releases[1]["draft"] is False
    assert len(gh.assets) == 2
    mutations = list(gh.mutations)
    repeated = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert repeated["reused"] is True
    assert gh.mutations == mutations
    assert all(method != "DELETE" for method, _ in gh.mutations)


@pytest.mark.parametrize("stage", ["release", "checkpoint.tar.gz", "manifest.json", "publish"])
def test_uncertain_mutations_are_observed_not_blindly_repeated(checkpoint, stage):
    gh = Store(); gh.fail_after = stage
    result = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert result["readback_verified"] is True
    assert gh.mutations.count(("POST", "releases")) == 1
    assert gh.mutations.count(("POST", "releases/1/assets")) == 2
    assert gh.mutations.count(("PATCH", "releases/1")) == 1


def test_restore_verifies_then_imports(checkpoint):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    (data_dir() / "bars/day/600000.parquet").unlink()
    result = pc.restore_checkpoint(gh, "2026-10-20")
    assert result["source"] == "private_release"
    assert result["degraded"] is False
    assert len(pd.read_parquet(data_dir() / "bars/day/600000.parquet")) == 2


def test_acceptance_checkpoint_targets_isolated_head_and_is_not_production_input(checkpoint, monkeypatch):
    gh = Store()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASHARE_WRITE_BRANCH", "migration-acceptance")
    result = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert result["readback_verified"] and result["branch"] == "migration-acceptance"
    assert result["tag"].startswith(pc.ACCEPTANCE_TAG_PREFIX)
    assert gh.releases[1]["target_commitish"] == "b" * 40
    mutations = list(gh.mutations)
    repeated = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert repeated["reused"] and repeated["release_id"] == result["release_id"]
    assert mutations == gh.mutations
    with pytest.raises(pc.CheckpointNotFound):
        pc.restore_checkpoint(gh, "2026-10-20")


def test_same_bytes_acceptance_release_cannot_replace_production_restore(checkpoint, monkeypatch):
    gh = Store()
    production = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    production_release = dict(gh.releases[production["release_id"]])
    before = len(gh.mutations)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASHARE_WRITE_BRANCH", "migration-acceptance")
    isolated = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert production["sha256"] == isolated["sha256"]
    assert production["tag"] != isolated["tag"] and isolated["release_id"] != production["release_id"]
    assert gh.releases[production["release_id"]] == production_release
    assert not any(path.startswith("releases/1") for _, path in gh.mutations[before:])
    restored = pc.restore_checkpoint(gh, "2026-10-20")
    assert restored["release_id"] == production["release_id"] and restored["tag"] == production["tag"]


@pytest.mark.parametrize("branch", [None, "data", "main"])
def test_actions_checkpoint_without_isolated_namespace_never_contacts_storage(checkpoint, monkeypatch, branch):
    gh = Store()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    if branch:
        monkeypatch.setenv("ASHARE_WRITE_BRANCH", branch)
    with pytest.raises(ValueError, match="migration-acceptance"):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert not gh.calls and not gh.mutations


def test_empty_release_listing_is_distinct_from_corruption(checkpoint):
    gh = Store()
    with pytest.raises(pc.CheckpointNotFound):
        pc.restore_checkpoint(gh)
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    gh.assets[1]["body"] = b"bad"; gh.assets[1]["size"] = 3
    with pytest.raises(pc.CheckpointValidationError, match="No valid recent"):
        pc.restore_checkpoint(gh)


def test_corruption_never_changes_local_data(checkpoint):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    target = data_dir() / "bars/day/600000.parquet"
    before = target.read_bytes()
    gh.assets[2]["body"] = b'{}'; gh.assets[2]["size"] = 2
    with pytest.raises(pc.CheckpointValidationError):
        pc.restore_checkpoint(gh)
    assert target.read_bytes() == before


def test_older_valid_checkpoint_fallback_is_explicit(checkpoint, tmp_path):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    atomic_json(data_dir() / "universe.json", {"items": [{"code": "600000"}]})
    second = tmp_path / "second.tar.gz"; pc.deterministic_checkpoint(second)
    pc.publish_checkpoint(gh, second, "2026-10-20")
    gh.assets[3]["body"] = b"bad"; gh.assets[3]["size"] = 3
    result = pc.restore_checkpoint(gh, "2026-10-20")
    assert result["release_id"] == 1
    assert result["degraded"] is True and result["failed_release_ids"] == [2]


def test_private_visibility_required_before_any_release_access(checkpoint):
    gh = Store(); gh.private = False
    with pytest.raises(pc.CheckpointValidationError):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    with pytest.raises(pc.CheckpointValidationError):
        pc.restore_checkpoint(gh)
    assert gh.calls == []


def test_immutable_existing_asset_conflict_never_deletes(checkpoint):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    gh.releases[1]["draft"] = True
    gh.assets[1]["body"] = b"conflict"; gh.assets[1]["size"] = 8
    before = list(gh.mutations)
    with pytest.raises(pc.CheckpointValidationError, match="conflict"):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert gh.mutations == before


def test_redirect_does_not_forward_bearer_or_cookies(checkpoint, monkeypatch):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    gh.redirect = "https://release-assets.githubusercontent.com/synthetic-download"
    body = gh.assets[1]["body"]
    requests = []
    def signed(request):
        requests.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=body)
    monkeypatch.setattr(pc.httpx, "Client", lambda **kw: REAL_CLIENT(**kw, transport=httpx.MockTransport(signed)))
    assert pc._download(gh, gh.assets[1], pc.MAX_ARCHIVE) == body
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["http://release-assets.githubusercontent.com/a", "https://evil.invalid/a", "https://release-assets.githubusercontent.com.evil.invalid/a", "https://user:password@release-assets.githubusercontent.com/a"])
def test_untrusted_redirect_rejected(checkpoint, url):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    gh.redirect = url
    with pytest.raises(pc.CheckpointValidationError, match="redirect rejected"):
        pc._download(gh, gh.assets[1], pc.MAX_ARCHIVE)


def archive_with(tmp_path, files, duplicate=False):
    path = tmp_path / "bad.tar.gz"
    inner = {"schema_version": 1, "files": {name: pc._sha(body) for name, body in files.items()}}
    with tarfile.open(path, "w:gz") as tar:
        entries = list(files.items()) + [("manifest.json", json.dumps(inner).encode())]
        if duplicate:
            entries.append(entries[0])
        for name, body in entries:
            info = tarfile.TarInfo(name); info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def test_rejects_duplicate_members_and_traversal(tmp_path):
    for path in [archive_with(tmp_path, {"../escape": b"x"}), archive_with(tmp_path, {"calendar.json": b"{}"}, duplicate=True)]:
        with pytest.raises(pc.CheckpointValidationError):
            pc.inspect_checkpoint(path)


def test_empty_and_future_data_never_publish(checkpoint, tmp_path):
    for dates in [[], ["2099-01-01"]]:
        frame = pd.DataFrame({"date": dates, **{key: [10.] * len(dates) for key in ["open", "high", "low", "close", "volume", "amount"]}})
        payload = io.BytesIO(); frame.to_parquet(payload, index=False)
        path = archive_with(tmp_path, {"bars/day/600000.parquet": payload.getvalue()})
        gh = Store()
        with pytest.raises(pc.CheckpointValidationError):
            pc.publish_checkpoint(gh, path, "2026-10-20")
        assert gh.mutations == []


def test_stale_checkpoint_rejected(checkpoint):
    with pytest.raises(pc.CheckpointValidationError, match="older than required"):
        pc.inspect_checkpoint(checkpoint, "2026-10-21")


def test_size_limit_checked_before_download(checkpoint):
    gh = Store()
    with pytest.raises(pc.CheckpointValidationError, match="too large"):
        pc._download(gh, {"id": 1, "size": pc.MAX_ARCHIVE + 1}, pc.MAX_ARCHIVE)
    assert gh.calls == []


def test_unobserved_upload_stops_without_retry_or_publish(checkpoint):
    gh = Store()
    original = gh.client
    count = []
    def interrupted(request):
        if request.url.host == "uploads.github.com":
            count.append(request)
            raise httpx.ReadTimeout("synthetic pre-write failure", request=request)
        return gh.handle(request)
    gh.client = REAL_CLIENT(base_url="https://api.github.com", transport=httpx.MockTransport(interrupted))
    with pytest.raises(pc.CheckpointUncertain, match="upload outcome unresolved"):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert len(count) == 1
    assert gh.releases[1]["draft"] is True
    assert gh.assets == {}
    assert not any(method == "PATCH" for method, _ in gh.mutations)
    original.close()


@pytest.mark.parametrize("asset", [{}, {"id": -1, "size": 1}, {"id": "1", "size": 1}, {"id": 1, "size": -1}])
def test_legacy_asset_transport_validates_identity(asset):
    gh = Store()
    with pytest.raises(pc.CheckpointValidationError):
        pc.download_asset(gh, asset)
    assert gh.calls == []


def test_resume_draft_hidden_from_tag_endpoint_after_interruption(checkpoint):
    gh = Store()
    tag = pc.TAG_PREFIX + pc.inspect_checkpoint(checkpoint)["sha256"]
    gh.releases[1] = {"id": 1, "tag_name": tag, "draft": True}
    assert gh.client.get(f"/repos/{gh.repo}/releases/tags/{tag}").status_code == 404
    result = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert result["release_id"] == 1 and result["reused"] is True
    assert len(gh.releases) == 1
    assert ("POST", "releases") not in gh.mutations


def test_duplicate_draft_tag_stops_without_mutation(checkpoint):
    gh = Store()
    tag = pc.TAG_PREFIX + pc.inspect_checkpoint(checkpoint)["sha256"]
    gh.releases = {i: {"id": i, "tag_name": tag, "draft": True} for i in [1, 2]}
    with pytest.raises(pc.CheckpointValidationError, match="Duplicate checkpoint release tag"):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert not gh.mutations


@pytest.mark.parametrize("matching_draft", [False, True])
def test_full_bounded_release_listing_is_uncertain_not_absent(checkpoint, monkeypatch, matching_draft):
    gh = Store()
    tag = pc.TAG_PREFIX + pc.inspect_checkpoint(checkpoint)["sha256"]
    gh.releases = {i: {"id": i, "tag_name": "unrelated-" + str(i), "draft": True} for i in range(1, 101)}
    if matching_draft:
        gh.releases[100]["tag_name"] = tag
    monkeypatch.setattr(pc, "MAX_RELEASE_PAGES", 1)
    with pytest.raises(pc.CheckpointUncertain, match="bounded listing"):
        pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert not gh.mutations


def test_draft_discovery_follows_next_page(checkpoint):
    gh = Store()
    tag = pc.TAG_PREFIX + pc.inspect_checkpoint(checkpoint)["sha256"]
    gh.releases[1] = {"id": 1, "tag_name": tag, "draft": True}
    gh.releases.update({i: {"id": i, "tag_name": "unrelated-" + str(i), "draft": True} for i in range(2, 102)})
    result = pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    assert result["release_id"] == 1
    assert ("POST", "releases") not in gh.mutations
    assert any(r.url.params.get("page") == "2" for r in gh.calls)


def changed_bars(checkpoint, tmp_path, change):
    bodies = pc._bodies(checkpoint.read_bytes())
    frame = pd.read_parquet(io.BytesIO(bodies["bars/day/600000.parquet"]))
    change(frame)
    payload = io.BytesIO(); frame.to_parquet(payload, index=False)
    bodies["bars/day/600000.parquet"] = payload.getvalue()
    return archive_with(tmp_path, bodies)


@pytest.mark.parametrize("column,value", [
    ("open", 0), ("high", float("inf")), ("low", -1), ("close", float("nan")),
    ("high", 5), ("low", 20), ("volume", -1), ("amount", -1),
    ("volume", float("inf")), ("amount", float("nan")),
])
def test_invalid_ohlc_and_turnover_rejected_before_publication(checkpoint, tmp_path, column, value):
    path = changed_bars(checkpoint, tmp_path, lambda frame: frame.loc.__setitem__((0, column), value))
    gh = Store()
    with pytest.raises(pc.CheckpointValidationError, match="bars invalid|OHLC or turnover invalid"):
        pc.publish_checkpoint(gh, path, "2026-10-20")
    assert not gh.mutations


@pytest.mark.parametrize("hour,minute", [(9, 5), (16, 9)])
def test_same_day_bar_before_publication_boundary_is_rejected(checkpoint, monkeypatch, hour, minute):
    monkeypatch.setattr(pc, "now", lambda: datetime(2026, 10, 20, hour, minute, tzinfo=TZ))
    with pytest.raises(pc.CheckpointValidationError, match="unfinished daily bars"):
        pc.inspect_checkpoint(checkpoint)


def test_future_available_at_rejected(checkpoint, tmp_path):
    def mutate(frame):
        frame["available_at"] = ["2026-10-19T16:10:00+08:00", "2026-10-20T17:01:00+08:00"]
    path = changed_bars(checkpoint, tmp_path, mutate)
    with pytest.raises(pc.CheckpointValidationError, match="availability"):
        pc.inspect_checkpoint(path)


def test_parquet_row_column_and_decoded_size_limits_precede_pandas(checkpoint, monkeypatch):
    body = pc._bodies(checkpoint.read_bytes())["bars/day/600000.parquet"]
    for limit, value in [("MAX_PARQUET_ROWS", 1), ("MAX_PARQUET_COLUMNS", 1), ("MAX_PARQUET_DECODED", 1)]:
        with monkeypatch.context() as context:
            context.setattr(pc, limit, value)
            with pytest.raises(pc.CheckpointValidationError, match="dimensions|decoded size"):
                pc._read_bounded_bars(body)


def test_dictionary_encoded_string_expansion_is_bounded_before_pandas():
    # Compressed footer/page sizes alone underestimate repeated long strings.
    dates = pd.bdate_range(end="2026-10-20", periods=2000).strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame({"date": dates, "source": ["x" * 65536] * len(dates),
                          **{key: [10.] * len(dates) for key in ["open", "high", "low", "close", "volume", "amount"]}})
    payload = io.BytesIO(); frame.to_parquet(payload, index=False, use_dictionary=True)
    assert len(payload.getvalue()) < 1024 * 1024
    with pytest.raises(pc.CheckpointValidationError, match="string expansion"):
        pc._read_bounded_bars(payload.getvalue())


def test_restore_discovers_published_checkpoint_after_unrelated_page(checkpoint):
    gh = Store()
    pc.publish_checkpoint(gh, checkpoint, "2026-10-20")
    gh.releases.update({i: {"id": i, "tag_name": "unrelated-" + str(i), "draft": True} for i in range(2, 102)})
    result = pc.restore_checkpoint(gh, "2026-10-20")
    assert result["release_id"] == 1
    assert any(r.url.params.get("page") == "2" for r in gh.calls)


def test_restore_full_bounded_listing_never_claims_not_found(checkpoint, monkeypatch):
    gh = Store()
    gh.releases = {i: {"id": i, "tag_name": "unrelated-" + str(i), "draft": True} for i in range(1, 101)}
    monkeypatch.setattr(pc, "MAX_RELEASE_PAGES", 1)
    with pytest.raises(pc.CheckpointUncertain):
        pc.restore_checkpoint(gh)
