"""Immutable private Release checkpoints. No Actions artifacts or public output.

All methods operate on an already scoped, private-verified cloud.GitHub client.
A completed release has both assets, both verified by download before publication.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
from datetime import date, time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pac
import pyarrow.parquet as pq

from .core import data_dir, dt, iso, now

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 800 * 1024 * 1024
MAX_FILES = 7000
MAX_MANIFEST = 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_PARQUET_BYTES = 16 * 1024 * 1024
MAX_PARQUET_DECODED = 64 * 1024 * 1024
MAX_PARQUET_ROWS = 30000
MAX_PARQUET_COLUMNS = 64
MAX_RELEASE_PAGES = 10
TAG_PREFIX = "ashare-checkpoint-v1-"
ACCEPTANCE_TAG_PREFIX = "ashare-acceptance-checkpoint-v1-"
ASSET_NAMES = {"checkpoint.tar.gz", "manifest.json"}


class CheckpointValidationError(ValueError):
    """A candidate exists but cannot safely be used."""


class CheckpointNotFound(CheckpointValidationError):
    """No published version-1 checkpoint exists; a legacy path may be considered."""


class CheckpointUncertain(CheckpointValidationError):
    """A mutation may have executed; inspect remote state before retrying."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def nonfinite(value):
        raise CheckpointValidationError("Non-finite checkpoint JSON number")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CheckpointValidationError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError) as exc:
        raise CheckpointValidationError("Invalid checkpoint JSON") from None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CheckpointValidationError("Invalid checkpoint date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise CheckpointValidationError("Invalid checkpoint date") from None
    return value


def _bodies(raw):
    from .cloud import allowed_checkpoint
    if not raw or len(raw) > MAX_ARCHIVE:
        raise CheckpointValidationError("Checkpoint compressed size invalid")
    result = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive:
                total += member.size
                if len(result) >= MAX_FILES or total > MAX_EXPANDED or member.size < 0:
                    raise CheckpointValidationError("Checkpoint expanded size invalid")
                if (not member.isfile() or member.name in result
                        or (member.name != "manifest.json" and not allowed_checkpoint(member.name))):
                    raise CheckpointValidationError("Checkpoint path or file type invalid")
                if member.name == "manifest.json" and member.size > MAX_MANIFEST:
                    raise CheckpointValidationError("Checkpoint inner manifest too large")
                if ((member.name.endswith(".parquet") and member.size > MAX_PARQUET_BYTES)
                        or (member.name.endswith(".json") and member.size > MAX_JSON_BYTES)):
                    raise CheckpointValidationError("Checkpoint member size invalid")
                body = archive.extractfile(member).read(member.size + 1)
                if len(body) != member.size:
                    raise CheckpointValidationError("Checkpoint truncated")
                result[member.name] = body
    except (tarfile.TarError, OSError, EOFError):
        raise CheckpointValidationError("Checkpoint archive invalid") from None
    manifest = _json(result.pop("manifest.json", b"{}"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CheckpointValidationError("Checkpoint inner manifest invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(result):
        raise CheckpointValidationError("Checkpoint manifest file set mismatch")
    if any(files[name] != _sha(body) for name, body in result.items()):
        raise CheckpointValidationError("Checkpoint content hash mismatch")
    return result


def _inspect(raw, expected_date=None):
    expected = _date(expected_date) if expected_date is not None else None
    bodies = _bodies(raw)
    first, last, day_files = [], [], 0
    for name, body in bodies.items():
        if name.endswith(".json"):
            value = _json(body)
            if not isinstance(value, dict):
                raise CheckpointValidationError("Checkpoint JSON schema invalid")
            if "fetched_at" in value and not isinstance(value["fetched_at"], str):
                raise CheckpointValidationError("Checkpoint timestamp schema invalid")
            if name in {"metadata.json", "financials.json", "company_research.json", "history_checks.json"}:
                items = value.get("items", {})
                if not isinstance(items, dict) or any(not isinstance(row, dict) for row in items.values()):
                    raise CheckpointValidationError("Checkpoint item schema invalid")
                for row in items.values():
                    for stamp in ("attempted_at", "fetched_at"):
                        if stamp in row and not isinstance(row[stamp], str):
                            raise CheckpointValidationError("Checkpoint timestamp schema invalid")
        else:
            try:
                frame = _read_bounded_bars(body)
                required = {"date", "open", "high", "low", "close", "volume", "amount"}
                if frame.empty or not required.issubset(frame.columns):
                    raise CheckpointValidationError("Checkpoint bars empty or schema invalid")
                dates = [_date(value) for value in frame["date"].tolist()]
                if len(set(dates)) != len(dates) or dates != sorted(dates):
                    raise CheckpointValidationError("Checkpoint dates duplicated or unordered")
                at = now()
                if max(dates) > at.date().isoformat():
                    raise CheckpointValidationError("Checkpoint has future dates")
                if max(dates) == at.date().isoformat() and at.time().replace(tzinfo=None) < time(16, 10):
                    raise CheckpointValidationError("Checkpoint contains unfinished daily bars")
                numeric = frame[["open", "high", "low", "close", "volume", "amount"]].apply(pd.to_numeric, errors="coerce")
                if not np.isfinite(numeric.to_numpy(dtype=float)).all():
                    raise CheckpointValidationError("Checkpoint numeric bars invalid")
                if ((numeric[["open", "high", "low", "close"]] <= 0).any().any()
                        or (numeric[["volume", "amount"]] < 0).any().any()
                        or (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
                        or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()):
                    raise CheckpointValidationError("Checkpoint OHLC or turnover invalid")
                if "available_at" in frame:
                    for value in frame["available_at"].tolist():
                        if not isinstance(value, str) or dt(value) > at:
                            raise CheckpointValidationError("Checkpoint availability is future or invalid")
            except CheckpointValidationError:
                raise
            except Exception:
                raise CheckpointValidationError("Checkpoint parquet invalid") from None
            first.append(min(dates)); last.append(max(dates)); day_files += 1
    if not day_files:
        raise CheckpointValidationError("Checkpoint contains no day bars")
    if expected is not None and (expected > now().date().isoformat() or max(last) < expected):
        raise CheckpointValidationError("Checkpoint older than required date")
    result = {"schema_version": 1, "sha256": _sha(raw), "bytes": len(raw),
              "day_files": day_files, "first_date": min(first), "last_date": max(last),
              "files": {name: _sha(body) for name, body in sorted(bodies.items())}}
    return result


def _read_bounded_bars(body):
    """Inspect the footer before decoding and bound dictionary expansion too."""
    if len(body) > MAX_PARQUET_BYTES:
        raise CheckpointValidationError("Checkpoint parquet compressed size invalid")
    options = {"thrift_string_size_limit": MAX_MANIFEST, "thrift_container_size_limit": 100000}
    parquet = pq.ParquetFile(io.BytesIO(body), **options)
    metadata = parquet.metadata
    if (not 0 < metadata.num_rows <= MAX_PARQUET_ROWS
            or not 0 < metadata.num_columns <= MAX_PARQUET_COLUMNS
            or not 0 < metadata.num_row_groups <= 512):
        raise CheckpointValidationError("Checkpoint parquet dimensions invalid")
    decoded = 0
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        for column in range(group.num_columns):
            size = group.column(column).total_uncompressed_size
            if size < 0:
                raise CheckpointValidationError("Checkpoint parquet metadata invalid")
            decoded += size
    if decoded > MAX_PARQUET_DECODED:
        raise CheckpointValidationError("Checkpoint parquet decoded size invalid")
    schema = parquet.schema_arrow
    if len(set(schema.names)) != len(schema.names):
        raise CheckpointValidationError("Checkpoint parquet columns duplicated")
    strings = []
    for field in schema:
        if pa.types.is_string(field.type):
            strings.append(field.name)
        elif not (pa.types.is_integer(field.type) or pa.types.is_floating(field.type) or pa.types.is_boolean(field.type)):
            raise CheckpointValidationError("Checkpoint parquet column type invalid")
    # Decoding strings as dictionaries prevents a tiny repeated dictionary from
    # expanding into many gigabytes before we can check its actual string sizes.
    parquet = pq.ParquetFile(io.BytesIO(body), read_dictionary=strings, **options)
    table = parquet.read(use_threads=False)
    if table.nbytes > MAX_PARQUET_DECODED:
        raise CheckpointValidationError("Checkpoint parquet decoded size invalid")
    worst = metadata.num_rows * metadata.num_columns * 8
    for name in strings:
        longest = 0
        for chunk in table[name].chunks:
            values = chunk.dictionary if pa.types.is_dictionary(chunk.type) else chunk
            longest = max(longest, pac.max(pac.binary_length(values)).as_py() or 0)
        worst += metadata.num_rows * (longest + 64)
    if worst > MAX_PARQUET_DECODED:
        raise CheckpointValidationError("Checkpoint parquet string expansion invalid")
    return table.to_pandas()


def inspect_checkpoint(path: Path, expected_date: str | None = None) -> dict:
    path = Path(path)
    if path.stat().st_size > MAX_ARCHIVE:
        raise CheckpointValidationError("Checkpoint compressed size invalid")
    return _inspect(path.read_bytes(), expected_date)


def deterministic_checkpoint(path: Path) -> dict:
    """Pack the existing whitelist with stable names, tar metadata and gzip time."""
    from .cloud import allowed_checkpoint
    bodies = {}
    expanded = 0
    for source in sorted(data_dir().rglob("*")):
        name = source.relative_to(data_dir()).as_posix()
        if source.is_symlink():
            if allowed_checkpoint(name):
                raise CheckpointValidationError("Checkpoint symlink input rejected")
            continue
        if source.is_file() and allowed_checkpoint(name):
            expanded += source.stat().st_size
            if expanded > MAX_EXPANDED or len(bodies) + 2 > MAX_FILES:
                raise CheckpointValidationError("Checkpoint file too large")
            bodies[name] = source.read_bytes()
    if len(bodies) + 1 > MAX_FILES or sum(map(len, bodies.values())) > MAX_EXPANDED:
        raise CheckpointValidationError("Checkpoint input size invalid")
    inner = {"schema_version": 1, "files": {name: _sha(body) for name, body in bodies.items()}}
    bodies["manifest.json"] = json.dumps(inner, sort_keys=True, separators=(",", ":")).encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for name, body in sorted(bodies.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
    raw = output.getvalue()
    stats = _inspect(raw)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return stats


def _scope(gh):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", gh.repo):
        raise CheckpointValidationError("Checkpoint repository invalid")
    gh.require_private()


def _identity(release, tag=None):
    if (not isinstance(release, dict) or type(release.get("id")) is not int or release["id"] <= 0
            or type(release.get("draft")) is not bool
            or not re.fullmatch(r"(?:" + TAG_PREFIX + "|" + ACCEPTANCE_TAG_PREFIX + r")[a-f0-9]{64}", release.get("tag_name", ""))
            or (tag and release["tag_name"] != tag)):
        raise CheckpointValidationError("Checkpoint release identity invalid")
    return release


def _assets(gh, release):
    assets = gh.api(f"releases/{release['id']}/assets?per_page=100")
    if not isinstance(assets, list) or len(assets) > 2:
        raise CheckpointValidationError("Checkpoint asset set invalid")
    found = {}
    for asset in assets:
        name = asset.get("name")
        if (name not in ASSET_NAMES or name in found or type(asset.get("id")) is not int
                or asset["id"] <= 0 or type(asset.get("size")) is not int or asset["size"] < 0):
            raise CheckpointValidationError("Checkpoint asset identity invalid")
        found[name] = asset
    return found


def _read_stream(response, limit):
    response.raise_for_status()
    if response.status_code != 200:
        raise CheckpointValidationError("Checkpoint download did not finish")
    result = bytearray()
    for chunk in response.iter_bytes():
        result.extend(chunk)
        if len(result) > limit:
            raise CheckpointValidationError("Checkpoint download size invalid")
    return bytes(result)


def _download(gh, asset, limit):
    _scope(gh)
    if asset["size"] > limit:
        raise CheckpointValidationError("Checkpoint download too large")
    endpoint = f"https://api.github.com/repos/{gh.repo}/releases/assets/{asset['id']}"
    with gh.client.stream("GET", endpoint, headers={"Accept": "application/octet-stream"}, follow_redirects=False) as response:
        if response.status_code != 302:
            raw = _read_stream(response, limit)
        else:
            location = response.headers.get("location", "")
            url = urlparse(location)
            if (url.scheme != "https" or url.hostname not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                    or url.username is not None or url.password is not None or url.port not in (None, 443)):
                raise CheckpointValidationError("Checkpoint redirect rejected")
            # A new unauthenticated client prevents bearer/cookie propagation.
            with httpx.Client(timeout=120, follow_redirects=False, trust_env=False) as public:
                with public.stream("GET", location) as signed:
                    raw = _read_stream(signed, limit)
    if len(raw) != asset["size"]:
        raise CheckpointValidationError("Checkpoint asset length mismatch")
    return raw


def download_asset(gh, asset: dict, max_bytes: int = MAX_ARCHIVE) -> bytes:
    """Shared bounded Release-asset transport, including legacy bootstrap."""
    if (not isinstance(asset, dict) or type(asset.get("id")) is not int or asset["id"] <= 0
            or type(asset.get("size")) is not int or asset["size"] < 0
            or type(max_bytes) is not int or not 0 < max_bytes <= MAX_ARCHIVE):
        raise CheckpointValidationError("Checkpoint asset identity invalid")
    return _download(gh, asset, max_bytes)


def _find(gh, tag):
    try:
        return _identity(gh.api(f"releases/tags/{tag}"), tag)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    # GitHub's tag endpoint returns published releases only. Drafts must be
    # recovered from the authenticated list; a timeout must not create duplicates.
    matches = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        values = gh.api(f"releases?per_page=100&page={page}")
        if not isinstance(values, list) or len(values) > 100:
            raise CheckpointValidationError("Checkpoint release listing invalid")
        for value in values:
            if isinstance(value, dict) and value.get("tag_name") == tag:
                matches.append(_identity(value, tag))
                if len(matches) > 1:
                    raise CheckpointValidationError("Duplicate checkpoint release tag")
        if len(values) < 100:
            return matches[0] if matches else None
    # A full final page cannot prove absence, even if it contained one match.
    raise CheckpointUncertain("Checkpoint draft discovery exceeded bounded listing")


def _write(gh, path, method, body):
    _scope(gh)
    return gh.api(path, method, body)


def _upload(gh, release, name, raw):
    assets = _assets(gh, release)
    if name in assets:
        existing = _download(gh, assets[name], MAX_ARCHIVE if name.endswith(".gz") else MAX_MANIFEST)
        if existing != raw:
            raise CheckpointValidationError("Immutable checkpoint asset conflict")
        return
    _scope(gh)
    url = f"https://uploads.github.com/repos/{gh.repo}/releases/{release['id']}/assets"
    try:
        response = gh.client.post(url, params={"name": name}, content=raw,
                                  headers={"Content-Type": "application/octet-stream"}, follow_redirects=False)
        response.raise_for_status()
        if response.status_code != 201:
            raise CheckpointValidationError("Checkpoint upload did not finish")
    except (httpx.TransportError, httpx.HTTPStatusError):
        # A timeout or 422 race can mean the upload succeeded. Observe, never
        # delete/re-upload an ambiguous asset or overwrite an existing version.
        assets = _assets(gh, release)
        if name not in assets:
            raise CheckpointUncertain("Checkpoint upload outcome unresolved") from None
        existing = _download(gh, assets[name], MAX_ARCHIVE if name.endswith(".gz") else MAX_MANIFEST)
        if existing != raw:
            raise CheckpointValidationError("Immutable checkpoint asset conflict")


def _verify(gh, release, expected_date=None, prefix=TAG_PREFIX):
    release = _identity(release)
    assets = _assets(gh, release)
    if set(assets) != ASSET_NAMES:
        raise CheckpointValidationError("Checkpoint release incomplete")
    raw = _download(gh, assets["checkpoint.tar.gz"], MAX_ARCHIVE)
    manifest = _json(_download(gh, assets["manifest.json"], MAX_MANIFEST))
    stats = _inspect(raw, expected_date)
    if prefix not in {TAG_PREFIX, ACCEPTANCE_TAG_PREFIX} or manifest != stats or release["tag_name"] != prefix + stats["sha256"]:
        raise CheckpointValidationError("Checkpoint outer manifest mismatch")
    return raw, stats


def publish_checkpoint(gh, path: Path, expected_date: str) -> dict:
    from .cloud import write_branch
    branch = write_branch()
    prefix = ACCEPTANCE_TAG_PREFIX if branch == "migration-acceptance" else TAG_PREFIX
    _scope(gh)
    stats = inspect_checkpoint(path, expected_date)
    raw = Path(path).read_bytes()
    tag = prefix + stats["sha256"]
    release = _find(gh, tag)
    reused = release is not None
    if release is None:
        parent = gh.api(f"git/ref/heads/{branch}").get("object", {}).get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40}", parent):
            raise CheckpointValidationError("Private data revision unavailable")
        body = {"tag_name": tag, "target_commitish": parent, "name": "Private research checkpoint",
                "body": "Versioned private checkpoint; validate both assets before use.",
                "draft": True, "prerelease": False}
        try:
            release = _identity(_write(gh, "releases", "POST", body), tag)
        except (httpx.TransportError, httpx.HTTPStatusError):
            release = _find(gh, tag)
            if release is None:
                raise CheckpointUncertain("Checkpoint release creation outcome unresolved") from None
    if not release.get("draft"):
        _, verified = _verify(gh, release, expected_date, prefix=prefix)
        return _receipt(release, verified, reused=True, branch=branch)
    _upload(gh, release, "checkpoint.tar.gz", raw)
    manifest = json.dumps(stats, sort_keys=True, separators=(",", ":")).encode()
    if len(manifest) > MAX_MANIFEST:
        raise CheckpointValidationError("Checkpoint manifest too large")
    _upload(gh, release, "manifest.json", manifest)
    _, verified = _verify(gh, release, expected_date, prefix=prefix)
    try:
        _write(gh, f"releases/{release['id']}", "PATCH", {"draft": False, "make_latest": "false"})
    except (httpx.TransportError, httpx.HTTPStatusError):
        current = _find(gh, tag)
        if current is None or current.get("draft") is not False:
            raise CheckpointUncertain("Checkpoint publication outcome unresolved") from None
    current = _find(gh, tag)
    if current is None or current.get("draft") is not False:
        raise CheckpointUncertain("Checkpoint publication not observed")
    _, verified = _verify(gh, current, expected_date, prefix=prefix)
    return _receipt(current, verified, reused=reused, branch=branch)


def _receipt(release, stats, **extra):
    return {"state": "success", "source": "private_release", "release_id": release["id"],
            "tag": release["tag_name"], "sha256": stats["sha256"], "bytes": stats["bytes"],
            "day_files": stats["day_files"], "first_date": stats["first_date"], "last_date": stats["last_date"],
            "readback_verified": True, **extra}


def restore_checkpoint(gh, expected_date: str | None = None) -> dict:
    _scope(gh)
    # Bounded listing; GitHub orders releases by creation time. Old versions and
    # all unrelated releases remain untouched.
    choices = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        releases = gh.api(f"releases?per_page=100&page={page}")
        if not isinstance(releases, list) or len(releases) > 100:
            raise CheckpointValidationError("Checkpoint release listing invalid")
        choices.extend(r for r in releases if isinstance(r, dict) and not r.get("draft")
                       and str(r.get("tag_name", "")).startswith(TAG_PREFIX))
        if len(choices) >= 2 or len(releases) < 100:
            break
    else:
        raise CheckpointUncertain("Checkpoint discovery exceeded bounded listing")
    if not choices:
        raise CheckpointNotFound("No published private checkpoint")
    failures = []
    for release in choices[:2]:
        try:
            raw, stats = _verify(gh, release, expected_date)
        except (CheckpointValidationError, httpx.HTTPError):
            failures.append(release.get("id"))
            continue
        # Entire archive and every Parquet/JSON is checked before any local write.
        from .cloud import unpack_checkpoint
        from .storage import save_snapshot
        with tempfile.TemporaryDirectory(prefix="ashare-checkpoint-") as folder:
            path = Path(folder) / "checkpoint.tar.gz"
            path.write_bytes(raw)
            unpack_checkpoint(path, require_bars=True)
        receipt = _receipt(release, stats, degraded=bool(failures), failed_release_ids=failures,
                           synced_at=iso())
        save_snapshot("checkpoint_status", receipt)
        return receipt
    raise CheckpointValidationError("No valid recent private checkpoint")
