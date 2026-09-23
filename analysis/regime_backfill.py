"""Reconstruct `market_regime` for historical snapshots — CONTEXT LAYER ONLY.

WHY
  `history.db` stores 292 daily snapshots (2025-10-31 .. 2026-09-23) but only
  49 of them carry a resolved regime: the collector only started writing the
  regime on 2026-07-13, and every backfilled row (2025-11 .. 2026-07) has
  `market_regime = "UNKNOWN"` / a null macro regime. Every regime-conditional
  analysis therefore collapses into one 17k-row UNKNOWN bucket (see
  backtest_latest.json `by_regime`), which makes "does the score only work in
  BULL_TREND?" unanswerable even with a year of data.

WHAT IT DOES
  Replays the model's OWN deterministic regime rules (`regime.classify_regime`,
  unchanged) over BTC daily closes up to each date, then applies the same
  hysteresis (`regime.apply_hysteresis`, min_dwell=6, escalation never delayed)
  sequentially, exactly like the live loop does.

WHAT IT DELIBERATELY DOES NOT DO
  1. It never overwrites `market_regime`. The result goes to a SEPARATE key
     `market_regime_reconstructed` (snapshots) / `regime_reconstructed`
     (macro table). Overwriting would destroy the point-in-time record of
     "what the model actually knew that day" — the whole reason the snapshot
     table exists. Analysis must opt in explicitly.
  2. It does NOT reproduce the macro legs of the rules: `glf_score` and
     `repo_stress` were not stored historically, so a RISK_OFF call that the
     live model made on repo stress alone cannot be reconstructed here; the
     volatility and 30d-return legs are reproduced exactly. Every written
     value carries `"legs": "vol+ret30 (glf/repo not stored historically)"`.

BTC close series: live snapshot `latest_price` for every date the collector
ran (point-in-time, as-collected), extended backwards with the Binance Vision
monthly cache (`.backfill_cache/BTCUSDT`) so the first snapshot dates have the
>=60 closes classify_regime() requires.

Usage:
  .venv/bin/python analysis/regime_backfill.py            # dry run (default)
  .venv/bin/python analysis/regime_backfill.py --write
"""
import argparse
import csv
import json
import os
import sqlite3
from collections import Counter, defaultdict

from altcoin.regime import THRESHOLDS, classify_regime, apply_hysteresis
from altcoin.backfill import parse_kline_csv_rows, CACHE_DIR

DB = os.environ.get("HISTORY_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "history.db"))
MIN_DWELL = 6


# ── BTC daily close series (point-in-time live values + archive extension) ──
def live_btc_closes():
    """BTC daily closes from the public spot API (last ~100 days). The final
    candle is IN PROGRESS, so it is dropped — a half-formed bar is not an
    observation. Used only to extend the series past the archive's last
    complete month (BTCUSDT is not part of the tracked altcoin universe, so
    there are no BTCUSDT snapshots to read live values from)."""
    from altcoin.analyzer import fetch_klines, _compute_rvm  # noqa: F401
    kl = fetch_klines("BTCUSDT", limit=100)
    if not kl:
        return {}
    out = {}
    for ts, _h, _l, close, _v, _t in kl[:-1]:
        if close and close > 0:
            out[_iso(ts)] = close
    return out


def btc_closes_by_date():
    """{date_iso: close}, later sources overriding earlier ones.

    Precedence: archive (complete historical months) -> live spot API
    (recent, complete bars only) -> live snapshot `latest_price` (point-in-
    time as-collected value; wins for shared dates when present).
    """
    out = {}
    # 1. archive
    sym_dir = os.path.join(CACHE_DIR, "BTCUSDT")
    if os.path.isdir(sym_dir):
        for fn in sorted(os.listdir(sym_dir)):
            if not fn.endswith(".csv"):
                continue
            with open(os.path.join(sym_dir, fn), newline="") as f:
                for ts, _h, _l, close, _v, _t in parse_kline_csv_rows(list(csv.reader(f))):
                    if close and close > 0:
                        out[_iso(ts)] = close
    # 2. live API extension (fills the gap between the last archived month
    #    and today; archive values for the same date are already final)
    for d, close in live_btc_closes().items():
        out.setdefault(d, close)
    # 3. live snapshots override
    c = sqlite3.connect(DB)
    for date, payload in c.execute(
            "SELECT date, payload FROM snapshots WHERE symbol='BTCUSDT'"):
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        px = p.get("latest_price")
        if px and px > 0:
            out[date] = px
    c.close()
    return out


def _iso(ts_ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ts_ms / 1000, datetime.timezone.utc).date().isoformat()


def reconstruct(btc):
    """Ordered [(date, regime_dict)] replaying classify+hysteresis."""
    dates = sorted(btc)
    closes_all = [btc[d] for d in dates]
    out = []
    prev_state, dwell = None, 0
    for i, d in enumerate(dates):
        raw = classify_regime(closes_all[:i + 1])
        state = raw.get("state")
        if state == "UNKNOWN":
            out.append((d, raw))
            continue
        held = apply_hysteresis(raw, prev_state, dwell, MIN_DWELL)
        final = held.get("state")
        if final == prev_state:
            dwell += 1
        else:
            prev_state, dwell = final, 1
        rec = dict(held)
        rec["legs"] = "vol+ret30 (glf/repo not stored historically)"
        rec["source"] = "reconstructed"
        out.append((d, rec))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="persist (default is dry-run)")
    a = ap.parse_args()

    btc = btc_closes_by_date()
    print(f"BTC closes: {len(btc)} dates "
          f"({min(btc)} .. {max(btc)})" if btc else "BTC closes: NONE — abort")
    if not btc:
        return
    rec = dict(reconstruct(btc))

    c = sqlite3.connect(DB)
    snap_dates = [r[0] for r in c.execute(
        "SELECT DISTINCT date FROM snapshots ORDER BY date")]
    macro_dates = {r[0] for r in c.execute("SELECT date FROM macro")}
    stored = {}
    for date, payload in c.execute("SELECT date, payload FROM macro"):
        try:
            stored[date] = ((json.loads(payload).get("regime") or {})
                            .get("state"))
        except ValueError:
            stored[date] = None

    print(f"\nsnapshot dates: {len(snap_dates)}  |  macro rows: {len(macro_dates)}")
    dist = Counter(rec.get(d, {}).get("state", "UNKNOWN") for d in snap_dates)
    print(f"reconstructed regime distribution: {dict(dist)}")
    print(f"stored (macro) distribution       : "
          f"{dict(Counter(v or 'UNKNOWN' for v in stored.values()))}")
    covered = sum(1 for d in snap_dates if rec.get(d, {}).get("state") not in (None, "UNKNOWN"))
    print(f"resolved coverage: {covered}/{len(snap_dates)} snapshot dates "
          f"(before: 49)")
    disagree = [d for d in snap_dates if d in stored and stored[d]
                and rec.get(d, {}).get("state") != stored[d]]
    print(f"dates where reconstruction differs from the stored/live regime: "
          f"{len(disagree)}/{sum(1 for d in snap_dates if stored.get(d))}")
    for d in disagree[:10]:
        print(f"   {d}: stored={stored[d]} reconstructed="
              f"{rec.get(d, {}).get('state')}")

    if not a.write:
        print("\nDRY RUN — nothing written. Re-run with --write to persist "
              "`market_regime_reconstructed` (snapshots) / "
              "`regime_reconstructed` (macro).")
        return

    n_snap = 0
    for d in snap_dates:
        r = rec.get(d)
        if not r or r.get("state") in (None, "UNKNOWN"):
            continue
        cur = c.execute(
            "UPDATE snapshots SET payload = json_set(payload, "
            "'$.market_regime_reconstructed', json(?)) WHERE date=?",
            (json.dumps(r), d))
        n_snap += cur.rowcount
    n_mac = 0
    for d in sorted(macro_dates):
        r = rec.get(d)
        if not r or r.get("state") in (None, "UNKNOWN"):
            continue
        cur = c.execute(
            "UPDATE macro SET payload = json_set(payload, "
            "'$.regime_reconstructed', json(?)) WHERE date=?",
            (json.dumps(r), d))
        n_mac += cur.rowcount
    c.commit()
    c.close()
    print(f"\nWROTE market_regime_reconstructed to {n_snap} snapshot rows "
          f"across {len(snap_dates)} dates; regime_reconstructed to {n_mac} "
          f"macro rows.")
    print("Original `market_regime` / `regime` fields are untouched.")


if __name__ == "__main__":
    main()
