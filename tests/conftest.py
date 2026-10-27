import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASHARE_MONITOR", "0")
    monkeypatch.setenv("ASHARE_GITHUB_REPO", "")
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.delenv("ASHARE_WRITE_BRANCH", raising=False)
    monkeypatch.delenv("ASHARE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ASHARE_STORAGE_TOKEN", raising=False)
    monkeypatch.delenv("ASHARE_STORAGE_REPO", raising=False)
    monkeypatch.delenv("ASHARE_COMPUTE_REPO", raising=False)
    monkeypatch.delenv("ASHARE_COMPUTE_WORKFLOW", raising=False)
    monkeypatch.delenv("ASHARE_LEGACY_ARTIFACTS", raising=False)
    monkeypatch.delenv("ASHARE_PRIVATE_WORKER", raising=False)
    monkeypatch.delenv("ASHARE_PRIVATE_RECEIPT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    """Providers must be mocked; never collect data or load GitHub credentials."""
    from ashare import cloud, providers

    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock external providers and network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    # Provider subprocesses / native HTTP clients bypass Python socket patches.
    monkeypatch.setattr(providers, "call", blocked)
    monkeypatch.setattr(providers, "_ak", blocked)
    monkeypatch.setattr(cloud, "token", blocked)


@pytest.fixture
def calendar():
    from ashare.core import TradingCalendar
    return TradingCalendar({"dates": ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"], "valid_through": "2026-10-20"})
