from __future__ import annotations

import hashlib
import json
import os
import tempfile
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")
PAPER_ACCOUNT = ContextVar("paper_account", default="baseline")


@contextmanager
def paper_account(name="baseline"):
    if name not in {"baseline", "experiment"} and not re.fullmatch(r"shadow-(rules|model)-[a-z0-9-]{1,80}", name):
        raise ValueError("未知模拟账户")
    token = PAPER_ACCOUNT.set(name)
    try:
        yield
    finally:
        PAPER_ACCOUNT.reset(token)


def account_path(folder: str) -> Path:
    root = data_dir() / folder
    return root if PAPER_ACCOUNT.get() == "baseline" else root / PAPER_ACCOUNT.get()


def now() -> datetime:
    return datetime.now(TZ)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def dt(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result.astimezone(TZ)


def config() -> dict:
    result = json.loads((ROOT / "config.json").read_text())
    result["github_repo"] = os.getenv("ASHARE_GITHUB_REPO", result["github_repo"])
    if PAPER_ACCOUNT.get() != "baseline":
        result.update(result.get("experiment", {}))
    return result


def data_dir() -> Path:
    marker = ROOT / ".runtime-path"
    default = Path(marker.read_text().strip()) / "data" if marker.exists() else ROOT / "data"
    target = Path(os.getenv("ASHARE_DATA_DIR", default))
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_json(path: Path, content: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
        temp = f.name
    os.replace(temp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def in_session(value: datetime) -> bool:
    t = value.astimezone(TZ).time().replace(tzinfo=None)
    return time(9, 30) <= t <= time(11, 30) or time(13) <= t <= time(15)


class TradingCalendar:
    """Explicit exchange dates, never silently substitute weekdays."""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or read_json(data_dir() / "calendar.json", {})
        self.days = sorted(set(self.payload.get("dates", [])))
        self.valid_through = self.payload.get("valid_through", self.days[-1] if self.days else "")

    def known(self, day: date) -> bool:
        return bool(self.days) and self.days[0] <= day.isoformat() <= self.valid_through

    def is_session(self, day: date) -> bool:
        if not self.known(day):
            raise ValueError(f"交易日历未覆盖 {day}，停止信号与交易")
        return day.isoformat() in self.days

    def previous(self, day: date) -> str:
        if not self.known(day):
            raise ValueError("交易日历覆盖不足")
        eligible = [d for d in self.days if d < day.isoformat()]
        if not eligible:
            raise ValueError("缺少上一交易日")
        return eligible[-1]

    def elapsed(self, start: str, end: str) -> int:
        if not self.known(date.fromisoformat(start)) or not self.known(date.fromisoformat(end)):
            raise ValueError("持有期间交易日历不完整")
        return len([d for d in self.days if start < d <= end])

    def completed(self, at: datetime) -> str:
        """Daily sources are only considered final at 16:10 Beijing time."""
        at = at.astimezone(TZ)
        if self.is_session(at.date()) and at.time().replace(tzinfo=None) >= time(16, 10):
            return at.date().isoformat()
        return self.previous(at.date())


def quote_fresh(quote: dict, at: datetime, seconds: int = 180) -> bool:
    if not quote.get("market_time") or quote.get("delay_status") == "unknown":
        return False
    age = (at - dt(quote["market_time"])).total_seconds()
    return 0 <= age <= seconds
