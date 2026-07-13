"""
Point-in-time persistence — blueprint Fase 0.5 (the step the roadmap
skipped): every ML phase downstream needs accumulated history, and
data.json is overwritten each cycle. Every day not stored here is
training data lost permanently.

Append-only SQLite, one row per (date, symbol), last write of the day
wins (multiple intra-day cycles just refresh that day's snapshot).

Two survivorship-bias guarantees, both deliberate:
    1. The `universe` table records which symbols were TRACKED each day
       (point-in-time listing). The live dashboard's delist-pruning is
       correct for display but is exactly the bias a backtest must not
       inherit — this table preserves what was actually investable.
    2. Rows are never deleted. A coin that later delists keeps its
       final drawdown in the data.

Schema is JSON-payload based on purpose: the feature set will evolve
(blueprint phases add features), and schema migrations on a research
store are wasted motion. Readers should treat missing keys as None.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("HISTORY_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "history.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (date, symbol));
CREATE TABLE IF NOT EXISTS universe (
    date TEXT PRIMARY KEY, mode TEXT, symbols TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro (
    date TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH)
    c.executescript(_SCHEMA)
    return c


def append_cycle(coins, macro, universe, regime, path=None, today=None):
    """
    Persist one collector cycle. `coins` is the {symbol: result} dict
    (only status=ok rows are stored), `macro`/`universe`/`regime` are the
    same dicts that go into data.json. Returns rows written.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    c = _conn(path)
    n = 0
    with c:
        for symbol, res in (coins or {}).items():
            if res.get("status") != "ok":
                continue
            c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                      (today, symbol, json.dumps(res, separators=(",", ":"))))
            n += 1
        c.execute("INSERT OR REPLACE INTO universe VALUES (?,?,?)",
                  (today, (universe or {}).get("mode"),
                   json.dumps(sorted((coins or {}).keys()))))
        c.execute("INSERT OR REPLACE INTO macro VALUES (?,?)",
                  (today, json.dumps({"macro": macro, "regime": regime},
                                     separators=(",", ":"))))
    c.close()
    return n


def stats(path=None):
    c = _conn(path)
    days = c.execute("SELECT COUNT(DISTINCT date) FROM snapshots").fetchone()[0]
    rows = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    c.close()
    return {"days": days, "rows": rows}
