from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

import httpx

from .core import atomic_json, config, data_dir, digest, dt, iso, now, read_json
from .storage import save_snapshot, snapshot


def in_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def storage_repository(repository: str | None = None) -> str:
    """Keep the public compute repository out of the private data path."""
    storage = os.getenv("ASHARE_STORAGE_REPO", "").strip()
    compute = os.getenv("GITHUB_REPOSITORY", "").strip()
    if in_actions():
        if not storage or not compute or storage.casefold() == compute.casefold():
            raise ValueError("云端必须显式配置与计算仓库不同的私有数据仓库")
    if storage and repository and repository.casefold() != storage.casefold():
        raise ValueError("拒绝访问配置以外的数据仓库")
    result = storage or repository or config()["github_repo"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result):
        raise ValueError("尚未配置 GitHub 私有仓库")
    return result


def token() -> str:
    # A public job's repository token cannot authorize the private data store.
    # Never read a local credential file or invoke gh inside Actions.
    value = os.getenv("ASHARE_STORAGE_TOKEN", "").strip()
    if in_actions():
        if not value:
            raise ValueError("缺少仅授权私有数据仓库的 ASHARE_STORAGE_TOKEN")
        return value
    value = value or os.getenv("ASHARE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    private_file = Path.home() / ".config/ashare-observer/github-token"
    if not value and private_file.exists():
        value = private_file.read_text().strip()
    if not value:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            value = result.stdout.strip()
    if not value:
        raise ValueError("需要仅对该私有仓库具有 Contents / Actions 读取权限的 GitHub 凭据")
    return value


class GitHub:
    def __init__(self, repository: str | None = None):
        self.repo = storage_repository(repository)
        self.client = httpx.Client(base_url="https://api.github.com", headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, timeout=30, follow_redirects=False)
        try:
            self.require_private()
        except Exception:
            self.client.close()
            raise

    def require_private(self):
        """Fail closed on missing, inaccessible, public or mismatched metadata."""
        try:
            response = self.client.get(f"/repos/{self.repo}")
            response.raise_for_status()
            metadata = response.json()
            valid = (metadata.get("private") is True
                     and isinstance(metadata.get("full_name"), str)
                     and metadata["full_name"].casefold() == self.repo.casefold())
        except Exception:
            raise ValueError("无法核实数据仓库为预期的私有仓库") from None
        if not valid:
            raise ValueError("数据仓库不是预期的私有仓库")

    def api(self, path: str, method="GET", body=None):
        method = method.upper()
        if method != "GET":
            # Recheck before every mutation in case visibility changed mid-run.
            self.require_private()
        if path == "git/commits" and method == "POST":
            identity = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}
            body = {**body, "author": dict(identity), "committer": dict(identity)}
        endpoint = f"/repos/{self.repo}" + (f"/{path}" if path else "")
        response = self.client.request(method, endpoint, json=body)
        response.raise_for_status()
        return response.json() if response.content else {}

    def content(self, path: str, branch="data") -> bytes:
        # GitHub omits Base64 content above 1 MB. The raw media type supports
        # full research reports without following an expiring download URL.
        raw = bytearray()
        with self.client.stream("GET", f"/repos/{self.repo}/contents/{path}", params={"ref": branch},
                                headers={"Accept": "application/vnd.github.raw+json"}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError("报告超过 20 MB 下载限制")
        return bytes(raw)


def validate_report(value: dict):
    if value.get("schema_version") != 1 or not isinstance(value.get("strategy_version"), str):
        raise ValueError("报告格式或策略版本无效")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}-[a-z]+", value.get("id", "")):
        raise ValueError("报告标识无效")
    generated = dt(value["generated_at"])
    if generated > now():
        raise ValueError("报告时间在未来")
    if generated.date().isoformat() != value.get("trade_date"):
        raise ValueError("报告交易日期与生成日期冲突")
    if len(value.get("candidates", [])) > config()["max_candidates"]:
        raise ValueError("候选数量超过配置")
    for row in value.get("candidates", []):
        if not re.fullmatch(r"\d{6}", row.get("code", "")):
            raise ValueError("股票代码无效")


def import_report(raw: bytes, expected_hash: str) -> bool:
    if digest(raw) != expected_hash:
        raise ValueError("报告完整性校验失败")
    value = json.loads(raw)
    validate_report(value)
    path = data_dir() / "reports" / (value["id"] + ".json")
    if path.exists() and digest(path.read_bytes()) != expected_hash:
        # Identical JSON serialized differently is safe; a rewritten report is not.
        if read_json(path) != value:
            raise ValueError("同一版本报告的内容发生改变")
    if not path.exists():
        atomic_json(path, value)
    current = snapshot("latest_report")
    newer = value["strategy_version"] == config()["strategy_version"] and (value["trade_date"], value["generated_at"]) > (current.get("trade_date", ""), current.get("generated_at", ""))
    if newer:
        save_snapshot("latest_report", value)
    return newer


def update_compute_status(gh):
    """Fetch public run metadata without ever forwarding the private data token."""
    compute = os.getenv("ASHARE_COMPUTE_REPO", "").strip()
    client = None
    try:
        if compute:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", compute) or compute.casefold() == gh.repo.casefold():
                raise ValueError("计算仓库配置无效")
            client = httpx.Client(base_url="https://api.github.com", headers={"Accept": "application/vnd.github+json"}, timeout=20, follow_redirects=False, trust_env=False)
            response = client.get(f"/repos/{compute}")
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("private") is not False or metadata.get("full_name", "").casefold() != compute.casefold():
                raise ValueError("计算仓库不是预期公开仓库")
        workflow_override = os.getenv("ASHARE_COMPUTE_WORKFLOW", "private-research.yml")
        if client and not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow_override):
            raise ValueError("计算工作流配置无效")
        for key, legacy_workflow in (("cloud_status", "cloud-research.yml"), ("training_cloud_status", "model-training.yml")):
            workflow = workflow_override if client else legacy_workflow
            try:
                path = f"actions/workflows/{workflow}/runs?per_page=1"
                if client:
                    response = client.get(f"/repos/{compute}/{path}")
                    response.raise_for_status()
                    payload = response.json()
                else:
                    payload = gh.api(path)
                latest = next(iter(payload.get("workflow_runs", [])), {})
                save_snapshot(key, {"checked_at": iso(), "source": "public_compute" if client else "legacy_private_compute",
                    "status": latest.get("status"), "conclusion": latest.get("conclusion"),
                    "created_at": latest.get("created_at"), "run_id": latest.get("id"),
                    "notice": ("共享计算工作流的最近运行，未按日报与训练模式区分；" if client else "") + "运行状态不代表数据或策略验收通过；当天无有效报告时不使用旧报告交易。"})
            except Exception:
                save_snapshot(key, {"checked_at": iso(), "state": "unavailable", "error": "workflow_status_unavailable"})
    except Exception:
        for key in ("cloud_status", "training_cloud_status"):
            save_snapshot(key, {"checked_at": iso(), "state": "unavailable", "error": "compute_repository_unverified"})
    finally:
        if client:
            client.close()


def sync_reports(recent_sessions: int | None = None) -> dict:
    status = {"state": "running", "started_at": iso()}
    save_snapshot("sync_status", status)
    try:
        gh = GitHub()
        # History recovery must not depend on report strategy migrations or a
        # single malformed historical report. Surface each failure separately.
        try:
            status["checkpoint"] = sync_checkpoint(gh)
        except Exception as exc:
            status["checkpoint"] = {"state": "failed", "error": str(exc)[:250]}
        update_compute_status(gh)
        tree = gh.api("git/trees/data?recursive=1")
        files = [r["path"] for r in tree.get("tree", []) if re.fullmatch(r"reports/\d{4}-\d{2}-\d{2}-\d{6}-[a-z]+\.json", r["path"])]
        cutoff = ""
        if recent_sessions:
            from .core import TradingCalendar
            days = [d for d in TradingCalendar().days if d <= now().date().isoformat()]
            cutoff = days[-recent_sessions] if len(days) >= recent_sessions else ""
            files = [p for p in files if p.split("/")[1][:10] >= cutoff]
        imported = 0
        # Read all missing compact reports. Existing immutable files need not be downloaded again.
        for path in sorted(files):
            if (data_dir() / path).exists():
                continue
            raw = gh.content(path)
            manifest = json.loads(gh.content(path + ".manifest"))
            import_report(raw, manifest["sha256"])
            imported += 1
        research = [r["path"] for r in tree.get("tree", []) if research_asset(r["path"])]
        if recent_sessions:
            newest_models = sorted({p.split("/")[1] for p in research if p.startswith("models/")},reverse=True)[:2]
            research = [p for p in research if (p.startswith("models/") and p.split("/")[1] in newest_models)
                        or (not p.startswith("models/") and p.split("/")[1][:10] >= cutoff)]
        for path in sorted(research):
            target = data_dir()/path
            if target.exists():
                if not path.startswith("models/") or target.name == "manifest.json":
                    continue
                expected = read_json(target.parent/"manifest.json",{}).get("files",{}).get(target.name)
                if expected == digest(target.read_bytes()):
                    continue
            raw = gh.content(path)
            receipt = json.loads(gh.content(path+".manifest"))
            if digest(raw) != receipt["sha256"]:
                raise ValueError("研究附件完整性校验失败")
            target.parent.mkdir(parents=True,exist_ok=True)
            temp = target.with_suffix(target.suffix+".download")
            temp.write_bytes(raw)
            temp.replace(target)
        training = sorted((data_dir()/"training_runs").glob("*.json"))
        if training:
            latest_training = read_json(training[-1])
            if latest_training.get("started_at","") > snapshot("training_status").get("started_at",""):
                save_snapshot("training_status",latest_training)
        status.update(state="success" if status["checkpoint"].get("state") == "success" else "partial", imported=imported, finished_at=iso(), latest_date=snapshot("latest_report").get("trade_date"))
        if status["latest_date"] != now().date().isoformat():
            status["notice"] = "当天报告尚未生成；当前展示历史报告"
    except Exception as exc:
        status.update(state="failed", finished_at=iso(), error=str(exc)[:250])
    save_snapshot("sync_status", status)
    return status


def research_asset(path):
    return bool(re.fullmatch(r"models/\d{4}-\d{2}-\d{2}-\d{6}-[a-f0-9]{8}/(manifest\.json|return\.txt|risk\.txt)",path)
        or re.fullmatch(r"training_runs/\d{4}-\d{2}-\d{2}-\d{6}\.json",path)
        or re.fullmatch(r"cloud_reviews/\d{4}-\d{2}-\d{2}-[a-f0-9]{16}\.json",path))


def validate_research_payloads(assets: dict[str, bytes]):
    """Check complete model bundles and receipt schemas without activating models."""
    models = {path.split("/")[1] for path in assets if path.startswith("models/")}
    for identifier in models:
        prefix = f"models/{identifier}/"
        expected = {prefix + name for name in ("manifest.json", "return.txt", "risk.txt")}
        if not expected.issubset(assets):
            raise ValueError("模型附件不完整")
        manifest = json.loads(assets[prefix + "manifest.json"])
        if (manifest.get("id") != identifier or set(manifest.get("files", {})) != {"return.txt", "risk.txt"}
                or not isinstance(manifest.get("schema"), str) or not manifest["schema"]
                or not isinstance(manifest.get("features"), list) or not manifest["features"]
                or not all(isinstance(f, str) and f for f in manifest["features"])):
            raise ValueError("模型清单格式无效")
        if dt(manifest["generated_at"]) > now():
            raise ValueError("模型清单时间无效")
        for name, checksum in manifest["files"].items():
            body = assets[prefix + name]
            if not body or not isinstance(checksum, str) or digest(body) != checksum:
                raise ValueError("模型内容校验失败")
    for path, raw in assets.items():
        if not research_asset(path):
            raise ValueError("研究附件路径无效")
        if path.startswith("training_runs/"):
            value = json.loads(raw)
            if value.get("state") not in {"complete", "insufficient", "failed"}:
                raise ValueError("训练记录状态无效")
            started, finished = dt(value["started_at"]), dt(value["finished_at"])
            if finished < started or finished > now():
                raise ValueError("训练记录时间无效")
            if value["state"] == "complete" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}-[a-f0-9]{8}", value.get("model_id", "")):
                raise ValueError("训练记录缺少模型标识")
        elif path.startswith("cloud_reviews/"):
            value = json.loads(raw)
            expected = f"cloud_reviews/{value.get('trade_date')}-{value.get('revision')}.json"
            if value.get("schema_version") != 1 or path != expected or value.get("execution"):
                raise ValueError("复盘附件格式或范围无效")


def _verify_files(gh, files: dict[str, bytes], branch="data"):
    """Read bytes and hash receipts from the same immutable Git commit."""
    for path, expected in files.items():
        body = gh.content(path, branch=branch)
        receipt = json.loads(gh.content(path + ".manifest", branch=branch))
        if body != expected or receipt.get("sha256") != digest(body) or receipt.get("bytes") != len(body):
            raise ValueError("私有文件写后回读校验失败")


def _data_parent(gh):
    try:
        parent = gh.api("git/ref/heads/data")["object"]["sha"]
        base = gh.api(f"git/commits/{parent}")["tree"]["sha"]
        tree = gh.api(f"git/trees/{base}?recursive=1")
        if tree.get("truncated"):
            raise ValueError("私有数据目录不完整")
        return parent, base, {r["path"]: r["sha"] for r in tree.get("tree", [])}
    except httpx.HTTPStatusError as exc:
        # Only a missing branch is a valid empty repository. Other missing
        # objects indicate a failed/inconsistent read and must not erase history.
        if exc.response.status_code == 404 and exc.request.url.path.endswith("/git/ref/heads/data"):
            return None, None, {}
        raise


def _publish_immutable(gh, files: dict[str, bytes], message: str):
    if not files:
        return {"state": "empty", "files": 0, "published": 0}
    for attempt in range(3):
        parent, base, existing = _data_parent(gh)
        changed = []
        published = 0
        for path, raw in sorted(files.items()):
            if not raw or len(raw) > 20 * 1024 * 1024:
                raise ValueError("私有附件为空或超过大小限制")
            if path in existing:
                # Existing files are never trusted just because their names match.
                _verify_files(gh, {path: raw}, branch=parent)
                continue
            if path + ".manifest" in existing:
                raise ValueError("私有附件清单缺少对应内容")
            manifest = json.dumps({"sha256": digest(raw), "bytes": len(raw)}, sort_keys=True).encode()
            for name, body in ((path, raw), (path + ".manifest", manifest)):
                blob = gh.api("git/blobs", "POST", {"content": base64.b64encode(body).decode(), "encoding": "base64"})
                changed.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            published += 1
        if not changed:
            _verify_files(gh, files, branch=parent)
            return {"state": "verified", "files": len(files), "published": 0, "commit": parent}
        body = {"tree": changed}
        if base:
            body["base_tree"] = base
        tree = gh.api("git/trees", "POST", body)
        commit = gh.api("git/commits", "POST", {"message": message, "tree": tree["sha"], "parents": [parent] if parent else []})
        try:
            if parent:
                gh.api("git/refs/heads/data", "PATCH", {"sha": commit["sha"], "force": False})
            else:
                gh.api("git/refs", "POST", {"ref": "refs/heads/data", "sha": commit["sha"]})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (409, 422) or attempt == 2:
                raise
            continue
        except httpx.TransportError:
            # A ref update may have executed despite a lost response. Check the
            # actual branch once; never blindly repeat an uncertain mutation.
            actual = gh.api("git/ref/heads/data")["object"]["sha"]
            _verify_files(gh, files, branch=actual)
            return {"state": "verified", "files": len(files), "published": published, "commit": actual, "recovered_uncertain_ref": True}
        actual = gh.api("git/ref/heads/data")["object"]["sha"]
        _verify_files(gh, files, branch=actual)
        return {"state": "verified", "files": len(files), "published": published, "commit": actual}
    raise ValueError("私有数据并发写入未完成")


def publish_research_assets():
    """Publish and read back immutable research files; never accounts/controls."""
    for path in (data_dir() / "reviews").glob("????-??-??.json"):
        review = read_json(path)
        if review.get("execution"):
            continue
        target = data_dir() / "cloud_reviews" / (review["trade_date"] + "-" + review["revision"] + ".json")
        if not target.exists():
            atomic_json(target, review)
    assets = {str(p.relative_to(data_dir())): p.read_bytes()
              for folder in ("models", "training_runs", "cloud_reviews")
              for p in (data_dir() / folder).rglob("*")
              if p.is_file() and research_asset(str(p.relative_to(data_dir())))}
    validate_research_payloads(assets)
    if not assets:
        return {"state": "empty", "files": 0, "published": 0}
    gh = GitHub()
    return _publish_immutable(gh, assets, "Verified private research artifacts")


RUN_KEY = re.compile(r"(\d{4}-\d{2}-\d{2})-(initial|update|fallback|close|train)")


def _run_receipt_path(key, value=None):
    match = RUN_KEY.fullmatch(key)
    if not match:
        raise ValueError("迁移记录标识无效")
    from datetime import date
    date.fromisoformat(match[1])
    if value is not None:
        if (value.get("schema_version") != 1 or value.get("state") != "verified"
                or value.get("day") != match[1] or value.get("mode") != match[2]
                or any(k in value for k in ("logs", "raw_logs", "stdout", "stderr"))):
            raise ValueError("迁移记录格式无效")
    return f"migration_runs/{key}.json"


def get_run_receipt(key: str):
    path = _run_receipt_path(key)
    gh = GitHub()
    try:
        parent = gh.api("git/ref/heads/data")["object"]["sha"]
        raw = gh.content(path, branch=parent)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    receipt = json.loads(gh.content(path + ".manifest", branch=parent))
    if receipt.get("sha256") != digest(raw) or receipt.get("bytes") != len(raw):
        raise ValueError("迁移记录校验失败")
    value = json.loads(raw)
    _run_receipt_path(key, value)
    return value


def publish_run_receipt(key: str, value: dict):
    path = _run_receipt_path(key, value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return _publish_immutable(GitHub(), {path: raw}, "Verified private migration receipt")


def sync_checkpoint(gh: GitHub) -> dict:
    from .private_checkpoints import CheckpointNotFound, restore_checkpoint
    try:
        return restore_checkpoint(gh, expected_date=None)
    except CheckpointNotFound:
        pass
    failures = []
    # Old Actions artifacts are an explicit compatibility option, not a
    # permanent permission requirement for the new private Release storage.
    if os.getenv("ASHARE_LEGACY_ARTIFACTS") == "1":
        artifacts = gh.api("actions/artifacts?per_page=100").get("artifacts", [])
        choices = sorted([a for a in artifacts if a["name"].startswith("checkpoint-") and not a.get("expired")], key=lambda a: a["created_at"], reverse=True)
        for artifact in choices[:2]:
            try:
                result = download_checkpoint(gh, artifact)
                result.update(source="legacy_artifact", degraded=True)
                save_snapshot("checkpoint_status", result)
                return result
            except Exception:
                failures.append({"artifact_id": artifact["id"], "error": "legacy_checkpoint_invalid"})
    stats = bootstrap_checkpoint(gh)
    current = {"state": "success", "source": "bootstrap_release", "degraded": True,
               "readback_verified": False, "synced_at": iso(), "artifact_errors": failures, **stats}
    save_snapshot("checkpoint_status", current)
    return current


def download_checkpoint(gh: GitHub, artifact: dict) -> dict:
    current = snapshot("checkpoint_status")
    if current.get("artifact_id") == artifact["id"] and current.get("day_files", 0) > 0:
        return current
    if artifact["size_in_bytes"] > 105 * 1024 * 1024:
        raise ValueError("云端检查点超过大小限制")
    response = gh.client.get(f"/repos/{gh.repo}/actions/artifacts/{artifact['id']}/zip")
    if response.status_code != 302:
        response.raise_for_status()
        raise ValueError("检查点下载地址异常")
    url = response.headers["location"]
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        raise ValueError("检查点必须通过 HTTPS 下载")
    # Never forward the GitHub bearer token to the signed storage URL.
    raw = bytearray()
    with httpx.stream("GET", url, timeout=60) as stream:
        stream.raise_for_status()
        for chunk in stream.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 105 * 1024 * 1024:
                raise ValueError("检查点下载超出限制")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if z.namelist() != ["checkpoint.tar.gz"] or z.getinfo("checkpoint.tar.gz").file_size > 100 * 1024 * 1024:
            raise ValueError("检查点附件内容不符合约定")
        target = data_dir() / "download-checkpoint.tar.gz"
        try:
            target.write_bytes(z.read("checkpoint.tar.gz"))
            stats = unpack_checkpoint(target, require_bars=True)
        finally:
            target.unlink(missing_ok=True)
    current = {"state": "success", "artifact_id": artifact["id"], "created_at": artifact["created_at"], "synced_at": iso(), **stats}
    save_snapshot("checkpoint_status", current)
    return current


def bootstrap_checkpoint(gh: GitHub):
    """Legacy private release seed; new runs publish verified private Releases."""
    release = gh.api("releases/tags/data-bootstrap")
    asset = next(a for a in release["assets"] if a["name"] == "checkpoint.tar.gz")
    from .private_checkpoints import download_asset, inspect_checkpoint
    raw = download_asset(gh, asset, max_bytes=100 * 1024 * 1024)
    path = data_dir() / "bootstrap.tar.gz"
    try:
        path.write_bytes(raw)
        inspected = inspect_checkpoint(path)
        restored = unpack_checkpoint(path, require_bars=True)
        return {**inspected, **restored, "release_id": release.get("id"),
                "asset_id": asset.get("id"), "tag": "data-bootstrap"}
    finally:
        path.unlink(missing_ok=True)


def publish_report(report: dict):
    """Validate, publish immutably and verify the bytes read from private storage."""
    validate_report(report)
    raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode()
    path = f"reports/{report['id']}.json"
    return _publish_immutable(GitHub(), {path: raw}, "Verified private research snapshot")


CHECKPOINT_FILES = {"calendar.json", "universe.json", "sectors.json", "metadata.json", "financials.json", "index_history.json", "index_histories.json", "announcements.json", "company_research.json", "history_checks.json"}


def allowed_checkpoint(name: str) -> bool:
    return name in CHECKPOINT_FILES or bool(re.fullmatch(r"bars/day/\d{6}\.parquet", name)) or bool(re.fullmatch(r"sector-BK\d+\.json", name))


def pack_checkpoint(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    root = data_dir()
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and allowed_checkpoint(str(p.relative_to(root)))}
    manifest = {name: digest(body) for name, body in files.items()}
    with tarfile.open(output, "w:gz") as tar:
        for name, body in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            tar.addfile(entry, io.BytesIO(body))
        body = json.dumps({"schema_version": 1, "files": manifest}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    if output.stat().st_size > 100 * 1024 * 1024:
        output.unlink()
        raise ValueError("检查点超过 100 MB，停止上传以保护免费额度")


def unpack_checkpoint(path: Path, require_bars=False):
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > 800 * 1024 * 1024 or len(members) > 7000:
            raise ValueError("检查点超出安全大小")
        if any(not m.isfile() or (m.name != "manifest.json" and not allowed_checkpoint(m.name)) for m in members):
            raise ValueError("检查点包含非法路径或文件类型")
        manifest = json.loads(tar.extractfile("manifest.json").read())
        if manifest.get("schema_version") != 1:
            raise ValueError("检查点版本不匹配")
        day_files = sum(m.name.startswith("bars/day/") for m in members)
        if require_bars and not day_files:
            raise ValueError("检查点没有日线，尝试上一个完整版本")
        bodies = {}
        for m in members:
            if m.name == "manifest.json":
                continue
            body = tar.extractfile(m).read()
            if digest(body) != manifest["files"].get(m.name):
                raise ValueError("检查点文件校验失败")
            bodies[m.name] = body
        # Validate the whole archive before any write; local portfolio is never in the whitelist.
        for name, body in bodies.items():
            target = data_dir() / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.startswith("bars/"):
                import pandas as pd
                from .storage import save_bars
                incoming = pd.read_parquet(io.BytesIO(body))
                existing = pd.read_parquet(target) if target.exists() else pd.DataFrame(columns=incoming.columns)
                # Fill missing historical dates even when local has a newer
                # closing date. Never overwrite locally newer observations.
                combined = incoming.set_index("date")
                local = existing.set_index("date")
                from .storage import bar_access
                with bar_access():
                    # Re-read under the shared writer lock in case local repair
                    # completed while the archive was being downloaded.
                    if target.exists():
                        local = pd.read_parquet(target).set_index("date")
                    merged = local.combine_first(combined).reset_index()
                    save_bars(target.stem, merged, replace=True)
            elif name.endswith(".json"):
                incoming = json.loads(body)
                existing = read_json(target, {})
                if name in {"metadata.json", "financials.json", "company_research.json", "history_checks.json"}:
                    combined = dict(existing.get("items", {}))
                    for code,row in incoming.get("items", {}).items():
                        stamp = "attempted_at" if name == "history_checks.json" else "fetched_at"
                        if row.get(stamp, "") >= combined.get(code, {}).get(stamp, ""):
                            combined[code] = row
                    atomic_json(target, {**existing, **incoming, "items":combined})
                    continue
                if existing.get("items") and incoming.get("items") == []:
                    continue
                if incoming.get("fetched_at", "") >= existing.get("fetched_at", ""):
                    atomic_json(target, incoming)
            else:
                temp = target.with_suffix(".tmp")
                temp.write_bytes(body)
                temp.replace(target)
        return {"day_files": day_files}
