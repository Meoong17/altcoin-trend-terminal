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
-- Evaluation harness homes (doc v2.0 §7/§9): schema ready NOW so the
-- day the dataset matures, backtests have somewhere to write. Metrics
-- like DQS/precision/recall live here AFTER a backtest produces them —
-- never displayed before that.
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, config TEXT, results TEXT);
CREATE TABLE IF NOT EXISTS decision_quality_history (
    run_id TEXT, regime TEXT, metric TEXT, value REAL);
"""


def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH)
    c.executescript(_SCHEMA)
    return c


def get_model_version(repo_dir=None):
    """
    Short git commit hash of the current codebase, used to stamp every
    snapshot -- automatic, zero-maintenance versioning. A manually-kept
    version string (e.g. "v1.8") relies on remembering to bump it every
    time a scoring formula changes; a git hash can't be forgotten,
    because it changes the moment the code that produced the score
    changes, with no discipline required.

    Falls back to "unknown" (never fabricated) if the directory isn't a
    git repo -- e.g. a deployment that stripped .git for size.
    """
    import subprocess
    repo_dir = repo_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=repo_dir, capture_output=True, text=True, timeout=5)
        h = out.stdout.strip()
        return h if out.returncode == 0 and h else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def append_cycle(coins, macro, universe, regime, path=None, today=None, model_version=None):
    """
    Persist one collector cycle. `coins` is the {symbol: result} dict
    (only status=ok rows are stored), `macro`/`universe`/`regime` are the
    same dicts that go into data.json. Returns rows written.

    Each stored coin payload is stamped with `market_regime` and
    `model_version` -- denormalized copies of data that's technically
    derivable by joining snapshots<->macro by date, but stamping them
    directly means a backtest query can group by regime or scoring
    version from the snapshots table alone, with no join, and a version
    change is visible in the row itself rather than only inferable from
    when scoring logic happened to change in the codebase.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    regime_state = (regime or {}).get("state")
    c = _conn(path)
    n = 0
    with c:
        for symbol, res in (coins or {}).items():
            if res.get("status") != "ok":
                continue
            stamped = dict(res)
            stamped["market_regime"] = regime_state
            if model_version:
                stamped["model_version"] = model_version
            c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                      (today, symbol, json.dumps(stamped, separators=(",", ":"))))
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


def regime_streak(path=None):
    """(state, consecutive_days) of the most recent stored regime, or
    (None, None). Used by the hysteresis layer."""
    c = _conn(path)
    rows = c.execute("SELECT date, payload FROM macro ORDER BY date DESC LIMIT 30").fetchall()
    c.close()
    if not rows:
        return None, None
    states = []
    for _, payload in rows:
        try:
            states.append((json.loads(payload).get("regime") or {}).get("state"))
        except ValueError:
            states.append(None)
    latest = states[0]
    if not latest:
        return None, None
    streak = 0
    for s in states:
        if s == latest:
            streak += 1
        else:
            break
    return latest, streak


def macro_series(key_path, limit=90, path=None):
    """[(date, value)] ascending for a dotted path inside the macro
    payload, e.g. 'market.btc_dominance'. Missing keys skipped."""
    c = _conn(path)
    rows = c.execute("SELECT date, payload FROM macro ORDER BY date ASC").fetchall()
    c.close()
    out = []
    for d, payload in rows[-limit:]:
        try:
            node = json.loads(payload)
            for part in key_path.split("."):
                node = node[part]
            if isinstance(node, (int, float)):
                out.append((d, float(node)))
        except (ValueError, KeyError, TypeError):
            continue
    return out
