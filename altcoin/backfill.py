"""
Historical backfill from Binance's public spot klines archive
(data.binance.vision) -- lets history.db's evaluation dataset start with
months of real market history instead of only what's been collected
live since deployment.

This is deliberately NARROWER than live collection, on purpose, to avoid
reintroducing exactly the biases the rest of this system was built to
avoid:

  1. TECHNICAL COMPONENTS ONLY. macro_component is passed as None to
     score_components() for every backfilled row -- score_components()
     already renormalizes cleanly over None (the same mechanism that
     handles a live coin missing one feature), so no new logic was
     needed, just the right argument. VaF and Entry Timing are not
     backfilled at all: both depend on vaf_overrides.json, which
     reflects TODAY's analyst judgment. Applying it to a date months
     ago would be look-ahead bias -- using information from the future
     to score the past. Every backfilled row is tagged
     model_version="backfill-technical-only" specifically so a later
     analysis can filter these out if it needs the macro/VaF-complete
     rows only.

  2. CURATED SYMBOLS ONLY, not the EXTEND_TOP/TOP_N tail. The curated
     groups (SYMBOL_GROUPS) are long-listed, liquid, still-active assets
     -- survivorship bias is a much smaller concern for them than for
     the volume-ranked tail, where attempting to reconstruct "which 400
     coins would have ranked in the top-N by volume six months ago"
     without Binance's historical listing/delisting record would bake
     survivorship bias directly into the dataset.

  3. NEVER OVERWRITES AN EXISTING DATE. Backfill only fills gaps -- any
     date already in history.db (i.e. a real, live-collected cycle with
     full macro+VaF) is left untouched. A technical-only reconstruction
     must never downgrade a live row.

Source: Binance spot monthly klines archive, documented at
https://github.com/binance/binance-public-data -- no key required.
URL pattern: {BASE}/data/spot/monthly/klines/{SYMBOL}/1d/{SYMBOL}-1d-{Y}-{M}.zip
Column order (per the archive's own docs): open_time, open, high, low,
close, volume, close_time, quote_asset_volume, trades, taker_buy_base,
taker_buy_quote, ignore. Timestamps are milliseconds before 2025-01-01
and MICROSECONDS from 2025-01-01 onward (an explicit, documented
format change) -- _normalize_ts() detects and corrects this by
magnitude rather than trusting the filename's year, so a file that
happens to straddle the boundary is still handled correctly.
"""

import csv
import io
import os
import sys
import time
import zipfile
from datetime import datetime, timezone

import requests

BINANCE_ARCHIVE = os.environ.get("BINANCE_ARCHIVE_BASE", "https://data.binance.vision")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".backfill_cache")
MS_THRESHOLD = 10 ** 14  # ms epoch ~1.7e12 today; microsecond epoch ~1.7e15 -- clean separator


def _normalize_ts(raw_ts):
    """Binance archive timestamps are ms before 2025-01-01, microseconds
    from 2025-01-01 onward. Detect by magnitude (not by trusting the
    filename's year, which could be wrong or the row could straddle a
    month boundary) and always return milliseconds."""
    ts = int(raw_ts)
    return ts // 1000 if ts >= MS_THRESHOLD else ts


def parse_kline_csv_rows(rows):
    """
    Pure transform: raw CSV rows (list of str-lists, Binance's documented
    column order) -> sorted [(ts_ms, high, low, close, quote_volume), ...]
    -- the exact tuple shape analyze_coin()/fetch_klines() already use
    everywhere else in this codebase, so downstream reconstruction reuses
    every existing pure function unchanged. Malformed rows are skipped,
    never estimated.
    """
    out = []
    for row in rows or []:
        try:
            ts = _normalize_ts(row[0])
            high, low, close = float(row[2]), float(row[3]), float(row[4])
            quote_vol = float(row[7])
        except (IndexError, ValueError, TypeError):
            continue
        out.append((ts, high, low, close, quote_vol))
    out.sort(key=lambda t: t[0])
    return out


def _cache_path(symbol, year, month):
    d = os.path.join(CACHE_DIR, symbol)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}-1d-{year:04d}-{month:02d}.csv")


def download_month(symbol, year, month, interval="1d"):
    """
    One month of daily klines for `symbol`, as raw CSV rows. Cached to
    disk so repeated runs (or a failed run resumed) don't re-download.
    Returns [] (not None) for a month that doesn't exist yet (e.g. the
    current, not-yet-archived month, or a symbol not listed that far
    back) -- absence of a month is data, not an error to crash on.
    """
    cache_file = _cache_path(symbol, year, month)
    if os.path.exists(cache_file):
        with open(cache_file, newline="") as f:
            return list(csv.reader(f))

    url = f"{BINANCE_ARCHIVE}/data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{year:04d}-{month:02d}.zip"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 404:
            return []  # not yet archived, or symbol didn't exist that month
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
            with z.open(csv_name) as fh:
                rows = list(csv.reader(io.TextIOWrapper(fh, "utf-8")))
        # Binance's archive sometimes includes a header row; the first
        # column of real data is always numeric, so drop it if not.
        if rows and not rows[0][0].strip().isdigit():
            rows = rows[1:]
        with open(cache_file, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        return rows
    except (requests.RequestException, zipfile.BadZipFile, StopIteration) as e:
        print(f"[backfill] {symbol} {year}-{month:02d}: {e}", file=sys.stderr)
        return []


def fetch_symbol_history(symbol, months_back=6, end_year=None, end_month=None):
    """
    Concatenated, deduped, ascending klines for `symbol` over the last
    `months_back` calendar months. Returns [] if nothing could be
    fetched (e.g. symbol never listed on Binance spot) -- caller must
    treat that as "skip this symbol", never as zero price history.
    """
    now = datetime.now(timezone.utc)
    end_year, end_month = end_year or now.year, end_month or now.month
    all_rows = []
    y, m = end_year, end_month
    for _ in range(months_back):
        all_rows.extend(download_month(symbol, y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        time.sleep(0.2)  # polite pacing on a free public archive
    klines = parse_kline_csv_rows(all_rows)
    # de-dupe same-timestamp rows (can happen at month boundaries)
    seen, deduped = set(), []
    for k in klines:
        if k[0] not in seen:
            seen.add(k[0])
            deduped.append(k)
    return deduped


def reconstruct_daily_rows(symbol, klines, btc_closes_by_ts, min_history=91):
    """
    For each day in `klines` that has >= min_history trailing days
    available, reconstruct that day's technical-only trend_score using
    the SAME pure functions live production uses (compute_feature_set,
    score_components with macro_component=None). Returns
    [{date, symbol, status, trend_score, rsi, features, ...}, ...] in
    the same shape append_cycle() expects for a coin result, minus
    every macro/VaF/fundamental field (never fabricated for backfill).
    """
    from altcoin.features import compute_feature_set, score_components
    from altcoin.analyzer import _compute_rsi

    closes = [k[3] for k in klines]
    highs = [k[1] for k in klines]
    lows = [k[2] for k in klines]
    quote_vols = [k[4] for k in klines]
    timestamps = [k[0] for k in klines]

    rows = []
    for i in range(min_history, len(klines)):
        window_end = i + 1
        c_slice, h_slice, l_slice, v_slice = (closes[:window_end], highs[:window_end],
                                              lows[:window_end], quote_vols[:window_end])
        ts = timestamps[i]
        date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
        btc_slice = btc_closes_by_ts.get(date)

        rsi = _compute_rsi(c_slice)
        feats = compute_feature_set(h_slice, l_slice, c_slice, v_slice, btc_slice)
        score, drivers, coverage = score_components(feats, rsi, macro_component=None)

        rows.append({
            "date": date, "symbol": symbol, "status": "ok",
            "latest_price": c_slice[-1], "rsi": rsi,
            "trend_score": score,
            "trend_score_detail": {"status": "ok" if score is not None else "no_data",
                                   "version": "v2-features-backfill",
                                   "drivers": drivers, "coverage": coverage},
            "features": feats,
            "closes_30d": c_slice[-30:],
        })
    return rows


def date_key_for(ts_ms, _unused=None):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def run_backfill(symbols=None, months_back=6, path=None,
                 model_version_tag="backfill-technical-only"):
    """
    Orchestrates the full backfill: fetch BTC history once, fetch each
    curated symbol's history, reconstruct technical-only daily scores,
    and insert into history.db for any date NOT already present.

    Returns {"days_added": int, "symbols_processed": int, "symbols_skipped": [...]}.
    """
    from collect import SYMBOL_GROUPS
    from altcoin.history import append_cycle, _conn

    symbols = symbols or sorted({s for syms in SYMBOL_GROUPS.values() for s in syms})

    print(f"[backfill] fetching BTC history ({months_back} months)...")
    btc_klines = fetch_symbol_history("BTCUSDT", months_back)
    if not btc_klines:
        print("[backfill] could not fetch BTC history -- aborting (no benchmark, no reconstruction)",
              file=sys.stderr)
        return {"days_added": 0, "symbols_processed": 0, "symbols_skipped": symbols}
    btc_by_date = {date_key_for(k[0]): [x[3] for x in btc_klines[:i + 1]]
                   for i, k in enumerate(btc_klines)}

    c = _conn(path)
    existing_dates = {r[0] for r in c.execute("SELECT DISTINCT date FROM snapshots").fetchall()}
    c.close()

    by_date = {}
    skipped = []
    for symbol in symbols:
        klines = fetch_symbol_history(symbol, months_back)
        if not klines:
            skipped.append(symbol)
            continue
        rows = reconstruct_daily_rows(symbol, klines, btc_by_date)
        for row in rows:
            if row["date"] in existing_dates:
                continue  # never overwrite a real, live-collected day
            by_date.setdefault(row["date"], {})[symbol] = row
        print(f"[backfill] {symbol}: {len(rows)} candidate days reconstructed")

    days_added = 0
    for date, coins in sorted(by_date.items()):
        rows_written = append_cycle(coins, {}, {"mode": "backfill"}, {"state": None},
                                    path=path, today=date, model_version=model_version_tag)
        if rows_written:
            days_added += 1

    print(f"[backfill] done: {days_added} new days, {len(symbols) - len(skipped)}/{len(symbols)} "
          f"symbols had usable history, {len(skipped)} skipped")
    return {"days_added": days_added, "symbols_processed": len(symbols) - len(skipped),
            "symbols_skipped": skipped}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Backfill history.db from Binance's historical klines archive")
    p.add_argument("--months", type=int, default=6, help="how many months back to fetch")
    p.add_argument("--symbols", type=str, default=None, help="comma-separated override; default: all curated groups")
    args = p.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    result = run_backfill(symbols=syms, months_back=args.months)
    print(result)
