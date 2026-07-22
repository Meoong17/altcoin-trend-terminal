"""
Backtest harness — Fase 0.5's evaluation layer, homed in the
`backtest_runs` / `decision_quality_history` tables that have been
sitting empty in history.db since they were created.

Core principle, stated once so every function below can stay silent
about it: THIS HARNESS DOES NOT KNOW IF ANY SCORE IS GOOD. It only
knows how to check, correctly, once enough data exists. Running it
today (with ~1 day of accumulated history) is deliberate: it proves
the METHODOLOGY is correct — walk-forward splits, precision/recall
definitions, regime grouping — before the day it matters. Every report
this module produces carries an explicit `sufficient_sample` flag, and
the CLI output leads with it. A report with sufficient_sample=False is
not a negative result; it's "come back later," and must never be
read as either "the model works" or "the model doesn't."

Nothing here retrains, adjusts, or tunes any weight automatically.
Output is diagnostic only.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from altcoin.history import _conn

MIN_DAYS_FOR_VALIDITY = 60   # below this, every result is a labeled dry-run
DEFAULT_HORIZON_DAYS = 7
GRADE_RANK = {"A+": 4, "A": 3, "B": 2, "Avoid": 1}


# ── Pure, offline-testable building blocks ──

def forward_return(snap_by_symbol_date, symbol, date, dates_sorted, horizon_days):
    """
    Actual % price change from `date` to the snapshot `horizon_days`
    LATER IN THE STORED SEQUENCE for this symbol -- not calendar days,
    trading/collection days, since cycles aren't guaranteed to be daily
    forever. Returns None if either endpoint is missing (a coin that
    delisted mid-window correctly produces None, never an estimate --
    this is also where survivorship bias would sneak back in if we
    were tempted to skip missing coins instead of counting them as
    unknown outcomes).
    """
    try:
        i = dates_sorted.index(date)
    except ValueError:
        return None
    if i + horizon_days >= len(dates_sorted):
        return None
    future_date = dates_sorted[i + horizon_days]
    p0 = snap_by_symbol_date.get((symbol, date), {}).get("latest_price")
    p1 = snap_by_symbol_date.get((symbol, future_date), {}).get("latest_price")
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return p1 / p0 - 1


def walk_forward_splits(dates_sorted, n_splits=3, min_train_days=20, min_test_days=5):
    """
    Expanding-window walk-forward split: each split trains on
    everything up to a point and tests on the immediately following
    block, never the reverse (a test block is never used to inform an
    earlier train block -- the one non-negotiable rule of walk-forward
    validation). Returns [] rather than a misleading tiny split when
    there isn't enough history for even one honest split.
    """
    n = len(dates_sorted)
    if n < min_train_days + min_test_days:
        return []
    splits = []
    remaining = n - min_train_days
    test_size = max(min_test_days, remaining // n_splits)
    train_end = min_train_days
    while train_end + min_test_days <= n and len(splits) < n_splits:
        test_end = min(train_end + test_size, n)
        splits.append((dates_sorted[:train_end], dates_sorted[train_end:test_end]))
        train_end = test_end
    return splits


def precision_recall(rows, predicted_positive, actual_positive):
    """
    rows: list of arbitrary records. predicted_positive/actual_positive:
    functions row -> bool. Standard definitions; returns None (not 0)
    for precision/recall when their denominator is zero -- "no
    positive predictions were made" is a different fact from "0% of
    predictions were correct," and collapsing them to the same number
    would misreport a model that simply never said BUY as one that
    said BUY and was always wrong.
    """
    pred_pos = [r for r in rows if predicted_positive(r)]
    act_pos = [r for r in rows if actual_positive(r)]
    tp = sum(1 for r in pred_pos if actual_positive(r))
    precision = tp / len(pred_pos) if pred_pos else None
    recall = tp / len(act_pos) if act_pos else None
    return {"precision": round(precision, 3) if precision is not None else None,
            "recall": round(recall, 3) if recall is not None else None,
            "predicted_positive": len(pred_pos), "actual_positive": len(act_pos),
            "true_positive": tp, "n": len(rows)}


def spearman(xs, ys):
    """
    Rank correlation with average-rank tie handling, implemented from
    scratch (no scipy dependency in this project). Returns None below
    3 paired points -- a correlation from 2 points is a line, not a
    signal.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg_rank
            i = j + 1
        return r
    xs_p, ys_p = [p[0] for p in pairs], [p[1] for p in pairs]
    rx, ry = ranks(xs_p), ranks(ys_p)
    n = len(pairs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 1e-12 or vy <= 1e-12:
        return None
    return round(cov / (vx ** 0.5 * vy ** 0.5), 3)


def evaluate_rows(rows, horizon_days):
    """
    rows: [{symbol, date, trend_score, entry_grade, regime, fwd_return}]
    -> diagnostic dict. Every metric a self-contained honest attempt;
    absent inputs (e.g. no coin ever graded A+) yield None fields
    rather than 0 or a crash.
    """
    scored = [r for r in rows if r.get("fwd_return") is not None]
    out = {"n_rows": len(rows), "n_with_forward_return": len(scored),
           "horizon_days": horizon_days}

    ts_rows = [r for r in scored if r.get("trend_score") is not None]
    if ts_rows:
        out["trend_score_vs_forward_return"] = {
            "spearman": spearman([r["trend_score"] for r in ts_rows],
                                 [r["fwd_return"] for r in ts_rows]),
            "hit_rate_60plus": precision_recall(
                ts_rows, lambda r: r["trend_score"] >= 60, lambda r: r["fwd_return"] > 0),
            "hit_rate_below_40": precision_recall(
                ts_rows, lambda r: r["trend_score"] < 40, lambda r: r["fwd_return"] <= 0),
        }

    grade_rows = [r for r in scored if r.get("entry_grade") in GRADE_RANK]
    if grade_rows:
        out["entry_grade_vs_forward_return"] = precision_recall(
            grade_rows, lambda r: r["entry_grade"] in ("A+", "A"),
            lambda r: r["fwd_return"] > 0)
    else:
        out["entry_grade_vs_forward_return"] = None
        out["entry_grade_note"] = "no rows carried an entry_timing grade in this window"

    by_regime = {}
    for r in scored:
        by_regime.setdefault(r.get("regime") or "UNKNOWN", []).append(r)
    out["by_regime"] = {
        reg: {"n": len(rs),
              "spearman": spearman([x["trend_score"] for x in rs if x.get("trend_score") is not None],
                                   [x["fwd_return"] for x in rs if x.get("trend_score") is not None])}
        for reg, rs in by_regime.items()
    }
    return out


# ── Orchestration (touches history.db) ──

def _load_all(path=None):
    c = _conn(path)
    snap_rows = c.execute("SELECT date, symbol, payload FROM snapshots ORDER BY date").fetchall()
    macro_rows = c.execute("SELECT date, payload FROM macro ORDER BY date").fetchall()
    c.close()
    snap_by_symbol_date, dates = {}, []
    for date, symbol, payload in snap_rows:
        try:
            snap_by_symbol_date[(symbol, date)] = json.loads(payload)
        except ValueError:
            continue
        if date not in dates:
            dates.append(date)
    regime_by_date = {}
    for date, payload in macro_rows:
        try:
            regime_by_date[date] = (json.loads(payload).get("regime") or {}).get("state")
        except ValueError:
            pass
    return snap_by_symbol_date, sorted(set(dates)), regime_by_date


def build_rows(snap_by_symbol_date, dates_sorted, regime_by_date, horizon_days, date_subset=None):
    rows = []
    use_dates = date_subset if date_subset is not None else dates_sorted
    for date in use_dates:
        for (symbol, d), payload in snap_by_symbol_date.items():
            if d != date:
                continue
            rows.append({
                "symbol": symbol, "date": date,
                "trend_score": payload.get("trend_score"),
                "entry_grade": (payload.get("entry_timing") or {}).get("grade"),
                "regime": regime_by_date.get(date),
                "fwd_return": forward_return(snap_by_symbol_date, symbol, date,
                                             dates_sorted, horizon_days),
            })
    return rows


def run_backtest(horizon_days=DEFAULT_HORIZON_DAYS,
                 min_days_for_validity=MIN_DAYS_FOR_VALIDITY, path=None, write=True):
    """
    Full dry-run-or-real backtest against whatever history.db currently
    holds. Always returns a report; writes it to backtest_runs when
    write=True. `sufficient_sample` is the single field a caller MUST
    check before treating any number in this report as meaningful.
    """
    snap_by_symbol_date, dates_sorted, regime_by_date = _load_all(path)
    n_days = len(dates_sorted)
    sufficient = n_days >= min_days_for_validity

    splits = walk_forward_splits(dates_sorted)
    all_rows = build_rows(snap_by_symbol_date, dates_sorted, regime_by_date, horizon_days)
    overall = evaluate_rows(all_rows, horizon_days)

    fold_reports = []
    for train_dates, test_dates in splits:
        test_rows = build_rows(snap_by_symbol_date, dates_sorted, regime_by_date,
                               horizon_days, date_subset=test_dates)
        fold_reports.append({
            "train_days": len(train_dates), "test_days": len(test_dates),
            "test_range": [test_dates[0], test_dates[-1]] if test_dates else None,
            **evaluate_rows(test_rows, horizon_days),
        })

    report = {
        "run_id": f"bt_{int(time.time())}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": {"horizon_days": horizon_days, "min_days_for_validity": min_days_for_validity},
        "sufficient_sample": sufficient,
        "sample_days": n_days,
        "sample_note": (f"only {n_days} distinct collection days on record; "
                        f"need >= {min_days_for_validity} across multiple regimes "
                        f"before ANY number below should be trusted"
                        if not sufficient else
                        f"{n_days} days on record, meets the minimum -- still confirm "
                        f"multiple regimes are represented before trusting a single fold"),
        "walk_forward_folds": fold_reports,
        "overall_dry_run_preview": overall,
    }

    if write:
        try:
            c = _conn(path)
            with c:
                c.execute("INSERT OR REPLACE INTO backtest_runs VALUES (?,?,?,?)",
                          (report["run_id"], report["started_at"],
                           json.dumps(report["config"]), json.dumps(report)))
                for reg, m in overall.get("by_regime", {}).items():
                    if m.get("spearman") is not None:
                        c.execute("INSERT INTO decision_quality_history VALUES (?,?,?,?)",
                                  (report["run_id"], reg, "trend_score_spearman", m["spearman"]))
            c.close()
        except Exception as e:
            print(f"[backtest] failed to persist run: {e}", file=sys.stderr)

    return report


if __name__ == "__main__":
    rep = run_backtest()
    print(f"run_id: {rep['run_id']}")
    print(f"sufficient_sample: {rep['sufficient_sample']}  ({rep['sample_note']})")
    print(f"sample_days: {rep['sample_days']}")
    print(f"walk-forward folds produced: {len(rep['walk_forward_folds'])}")
    print(json.dumps(rep["overall_dry_run_preview"], indent=2))
    if not rep["sufficient_sample"]:
        print("\n>>> DRY RUN ONLY. Do not act on any number above. <<<")
