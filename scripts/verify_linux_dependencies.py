"""Check the manual runner's strict lock files and isolated installed inventory.

pip verifies distribution hashes during installation; this additional check
detects omitted, changed or extra installed distributions. It does not attest
that third-party source code is safe. Run before creating any private token.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("requirements-linux-bootstrap.lock", "requirements-linux.lock")
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.!+-]*)(.*)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(text):
    """Only fixed versions and SHA256 hashes; no indexes, URLs or markers."""
    result, pending = {}, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if pending:
                raise ValueError("interrupted_requirement")
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line, pending = pending + line, ""
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError("invalid_requirement")
        name, version, suffix = match.groups()
        hashes = suffix.split()
        if not hashes or any(HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("invalid_requirement_hashes")
        name = canonical(name)
        if name in result:
            raise ValueError("duplicate_requirement")
        result[name] = version
    if pending or not result:
        raise ValueError("incomplete_lock")
    return result


def expected_versions(root=ROOT):
    expected = {}
    for name in LOCKS:
        for package, version in read_lock((root / name).read_text()).items():
            if package in expected and expected[package] != version:
                raise ValueError("conflicting_locks")
            expected[package] = version
    return expected


def validate_inventory(expected, installed):
    actual = {}
    for name, version in installed:
        key = canonical(name)
        if key in actual:
            raise ValueError("duplicate_distribution")
        actual[key] = version
    # pip belongs to the pinned setup-python runtime/venv, not the application.
    if set(actual) - set(expected) - {"pip"}:
        raise ValueError("unexpected_distribution")
    if any(actual.get(name) != version for name, version in expected.items()):
        raise ValueError("dependency_inventory_mismatch")


def supported_platform():
    return (sys.platform == "linux" and platform.machine() in {"x86_64", "AMD64"}
            and sys.version_info[:2] == (3, 12))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locks-only", action="store_true")
    args = parser.parse_args()
    try:
        if not supported_platform():
            raise ValueError("unsupported_platform")
        expected = expected_versions()
        if not args.locks_only:
            validate_inventory(expected, ((item.metadata["Name"], item.version)
                                         for item in importlib.metadata.distributions()))
        print(json.dumps({"scope": "public_dependencies", "state": "verified",
                          "checks": "locks" if args.locks_only else "installed_inventory",
                          "packages": len(expected)}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"scope": "public_dependencies", "state": "failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
