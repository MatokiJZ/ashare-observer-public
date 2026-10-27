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


def token() -> str:
    value = os.getenv("ASHARE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
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
        self.repo = repository or config()["github_repo"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("尚未配置 GitHub 私有仓库")
        self.client = httpx.Client(base_url="https://api.github.com", headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, timeout=30, follow_redirects=False)

    def api(self, path: str, method="GET", body=None):
        response = self.client.request(method, f"/repos/{self.repo}/{path}", json=body)
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
        try:
            runs = gh.api("actions/workflows/cloud-research.yml/runs?per_page=5").get("workflow_runs", [])
            latest = runs[0] if runs else {}
            save_snapshot("cloud_status", {"checked_at": iso(), "status": latest.get("status"), "conclusion": latest.get("conclusion"),
                                          "created_at": latest.get("created_at"), "url": latest.get("html_url"), "notice": "失败、额度不足或调度漏跑请查看云端任务记录；当天无报告时不使用旧报告交易。"})
        except Exception as exc:
            save_snapshot("cloud_status", {"checked_at": iso(), "error": str(exc)[:180]})
        try:
            runs = gh.api("actions/workflows/model-training.yml/runs?per_page=1").get("workflow_runs", [])
            latest = runs[0] if runs else {}
            save_snapshot("training_cloud_status", {"checked_at":iso(), "status":latest.get("status"),
                "conclusion":latest.get("conclusion"), "created_at":latest.get("created_at"), "url":latest.get("html_url"),
                "notice":"云端超时、额度阻断或失败时保留有效模型；详细原因见任务记录。"})
        except Exception as exc:
            save_snapshot("training_cloud_status", {"checked_at":iso(), "error":str(exc)[:180]})
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


def publish_research_assets():
    """Publish immutable research/model artifacts only. Never local accounts/controls."""
    for path in (data_dir()/"reviews").glob("????-??-??.json"):
        review = read_json(path)
        if review.get("execution"):
            continue
        target = data_dir()/"cloud_reviews"/(review["trade_date"]+"-"+review["revision"]+".json")
        if not target.exists():
            atomic_json(target,review)
    gh = GitHub(os.environ.get("GITHUB_REPOSITORY"))
    assets = {str(p.relative_to(data_dir())):p for folder in ("models","training_runs","cloud_reviews")
              for p in (data_dir()/folder).rglob("*") if p.is_file() and research_asset(str(p.relative_to(data_dir())))}
    for attempt in range(3):
        parent = gh.api("git/ref/heads/data")["object"]["sha"]
        base = gh.api(f"git/commits/{parent}")["tree"]["sha"]
        existing = {r["path"]:r["sha"] for r in gh.api(f"git/trees/{base}?recursive=1").get("tree",[])}
        changed = []
        for path,source in sorted(assets.items()):
            raw = source.read_bytes()
            git_sha = hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
            if path in existing:
                if existing[path] != git_sha:
                    raise ValueError("拒绝覆盖研究版本 "+path)
                continue
            manifest = json.dumps({"sha256":digest(raw),"bytes":len(raw)}).encode()
            for name,body in [(path,raw),(path+".manifest",manifest)]:
                blob = gh.api("git/blobs","POST",{"content":base64.b64encode(body).decode(),"encoding":"base64"})
                changed.append({"path":name,"mode":"100644","type":"blob","sha":blob["sha"]})
        if not changed:
            return {"published":0}
        tree = gh.api("git/trees","POST",{"base_tree":base,"tree":changed})
        commit = gh.api("git/commits","POST",{"message":"Daily reviews and validated research artifacts","tree":tree["sha"],"parents":[parent]})
        try:
            gh.api("git/refs/heads/data","PATCH",{"sha":commit["sha"],"force":False})
            return {"published":len(changed)//2}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (409,422) or attempt==2:
                raise


def sync_checkpoint(gh: GitHub) -> dict:
    artifacts = gh.api("actions/artifacts?per_page=100").get("artifacts", [])
    choices = sorted([a for a in artifacts if a["name"].startswith("checkpoint-") and not a.get("expired")], key=lambda a: a["created_at"], reverse=True)
    failures = []
    for artifact in choices[:2]:
        try:
            return download_checkpoint(gh, artifact)
        except Exception as exc:
            failures.append({"artifact_id": artifact["id"], "error": str(exc)[:160]})
    # A durable release is the bootstrap fallback, never an Actions cache.
    bootstrap_checkpoint(gh)
    current = {"state": "success", "source": "bootstrap_release", "synced_at": iso(), "artifact_errors": failures}
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
    """One-time private release seed; subsequent runs use Actions artifacts."""
    release = gh.api("releases/tags/data-bootstrap")
    asset = next(a for a in release["assets"] if a["name"] == "checkpoint.tar.gz")
    if asset["size"] > 100 * 1024 * 1024:
        raise ValueError("初始化包超过大小限制")
    response = gh.client.get(f"/repos/{gh.repo}/releases/assets/{asset['id']}", headers={"Accept": "application/octet-stream"})
    if response.status_code == 302:
        location = response.headers["location"]
        if not location.startswith("https://"):
            raise ValueError("初始化包下载必须使用 HTTPS")
        response = httpx.get(location, timeout=120)
    response.raise_for_status()
    if len(response.content) > 100 * 1024 * 1024:
        raise ValueError("初始化包超过大小限制")
    path = data_dir() / "bootstrap.tar.gz"
    try:
        path.write_bytes(response.content)
        unpack_checkpoint(path, require_bars=True)
    finally:
        path.unlink(missing_ok=True)


def publish_report(report: dict):
    """Optimistic Git ref update; retry on races, never force-push or overwrite versions."""
    gh = GitHub(os.environ.get("GITHUB_REPOSITORY"))
    raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode()
    path = f"reports/{report['id']}.json"
    manifest = json.dumps({"sha256": digest(raw), "bytes": len(raw), "generated_at": report["generated_at"]}).encode()
    for attempt in range(3):
        try:
            ref = gh.api("git/ref/heads/data")
            parent = ref["object"]["sha"]
            base_tree = gh.api(f"git/commits/{parent}")["tree"]["sha"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            parent, base_tree = None, None
        if parent:
            try:
                old_raw = gh.content(path)
                if json.loads(old_raw) != report:
                    raise ValueError("拒绝覆盖已经发布的报告版本")
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        items = []
        for name, body in [(path, raw), (path + ".manifest", manifest)]:
            blob = gh.api("git/blobs", "POST", {"content": base64.b64encode(body).decode(), "encoding": "base64"})
            items.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        body = {"tree": items}
        if base_tree:
            body["base_tree"] = base_tree
        tree = gh.api("git/trees", "POST", body)
        commit = gh.api("git/commits", "POST", {"message": f"Research snapshot {report['id']}", "tree": tree["sha"], "parents": [parent] if parent else []})
        try:
            if parent:
                gh.api("git/refs/heads/data", "PATCH", {"sha": commit["sha"], "force": False})
            else:
                gh.api("git/refs", "POST", {"ref": "refs/heads/data", "sha": commit["sha"]})
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (409, 422) or attempt == 2:
                raise


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
