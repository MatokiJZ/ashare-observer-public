from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

from .core import PAPER_ACCOUNT, atomic_json, data_dir, iso, now, read_json
import threading
import tempfile
import fcntl

BAR_LOCK = threading.RLock()
BAR_STATE = threading.local()


@contextmanager
def bar_access():
    """Keep Parquet footer/data reads on the same file version across processes."""
    with BAR_LOCK:
        if getattr(BAR_STATE, "active", False):
            yield
            return
        with (data_dir() / ".bars.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            BAR_STATE.active = True
            try:
                yield
            finally:
                BAR_STATE.active = False
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def database():
    name = PAPER_ACCOUNT.get()
    con = sqlite3.connect(data_dir() / ("state.sqlite" if name == "baseline" else f"state-{name}.sqlite"), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL,
        fees REAL NOT NULL, reason TEXT NOT NULL, strategy TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, ts TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, value REAL NOT NULL, cash REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS trade_context (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS observations (
        day TEXT NOT NULL, code TEXT NOT NULL, reason TEXT NOT NULL,
        first_at TEXT NOT NULL, last_at TEXT NOT NULL, count INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(day,code,reason));
    CREATE TABLE IF NOT EXISTS sessions (day TEXT PRIMARY KEY, first_at TEXT NOT NULL, last_at TEXT NOT NULL, observations INTEGER NOT NULL);
    """)
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def get_value(key: str, default=None):
    with database() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_value(key: str, value) -> None:
    with database() as con:
        con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))


def alert(kind: str, message: str, key: str | None = None):
    with database() as con:
        con.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)", (key or f"{kind}:{iso()}", iso(), kind, message))


def records(table: str, limit=200) -> list[dict]:
    if table not in {"trades", "alerts", "equity", "signals"}:
        raise ValueError("unknown table")
    with database() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY ts DESC LIMIT ?", (limit,))]


def save_bars(symbol: str, frame: pd.DataFrame, frequency="day", replace=False):
    with bar_access():
        _save_bars(symbol, frame, frequency, replace)


def _save_bars(symbol, frame, frequency, replace):
    folder = data_dir() / "bars" / frequency
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{symbol}.parquet"
    if path.exists() and not replace:
        existing = pd.read_parquet(path)
        # A secondary provider may lack auxiliary fields such as quoted return.
        # Keep existing non-null values without replacing new prices or volumes.
        frame = frame.set_index("date").combine_first(existing.drop_duplicates("date", keep="last").set_index("date")).reset_index()
    if frequency == "day" and "available_at" in frame:
        frame = frame[pd.to_datetime(frame.available_at, utc=True, errors="coerce") <= now()]
    if frequency == "day" and {"volume", "amount"}.issubset(frame):
        frame = frame[~((frame.volume == 0) & (frame.amount == 0))]
    frame = frame.drop_duplicates("date", keep="last").sort_values("date")
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as f:
        temp = Path(f.name)
    try:
        frame.to_parquet(temp, index=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def bars(symbol: str, frequency="day") -> pd.DataFrame:
    if not symbol.replace(".", "").replace("_", "").isalnum():
        raise ValueError("invalid symbol")
    path = data_dir() / "bars" / frequency / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame()
    with bar_access():
        return pd.read_parquet(path).sort_values("date")


def bars_many(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """One DuckDB scan avoids thousands of per-file database startups."""
    folder = data_dir() / "bars/day"
    paths = [str(folder / (s + ".parquet")) for s in symbols if s.isdigit() and len(s) == 6 and (folder / (s + ".parquet")).exists()]
    if not paths:
        return {}
    with bar_access(), duckdb.connect(config={"threads": 2}) as con:
        frame = con.execute("SELECT * FROM read_parquet(?, filename=true, union_by_name=true) ORDER BY date", [paths]).df()
    return {Path(name).stem: group.drop(columns=["filename"]).reset_index(drop=True) for name, group in frame.groupby("filename", sort=False)}


def save_snapshot(name: str, value: dict):
    atomic_json(snapshot_path(name), value)


def snapshot(name: str) -> dict:
    return read_json(snapshot_path(name), {})


def snapshot_path(name: str):
    if name in {"daily_selection", "intraday", "live_regime"} and PAPER_ACCOUNT.get() != "baseline":
        return data_dir() / "accounts" / PAPER_ACCOUNT.get() / f"{name}.json"
    return data_dir() / f"{name}.json"
