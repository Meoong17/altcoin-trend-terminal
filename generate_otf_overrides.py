#!/usr/bin/env python3
"""Generate proportional catalyst_timing & supply_timing for non-DeFi coins.

Reads data.json and existing vaf_overrides.json.
For every coin NOT in DEFI_PROTOCOLS and not already manually overridden,
computes data-driven scores from available metrics and appends an OTF-only entry.

Usage: python3 generate_otf_overrides.py
Output: updated vaf_overrides.json (backup written to vaf_overrides.json.bak)
"""

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data.json"
OVERRIDES_PATH = ROOT / "vaf_overrides.json"

# ── DeFi protocols from fundamentals.py ──
DEFI_PROTOCOLS = {
    "AAVEUSDT", "UNIUSDT", "PENDLEUSDT", "MORPHOUSDT",
    "LDOUSDT", "CRVUSDT", "COMPUSDT", "GMXUSDT", "DYDXUSDT",
    "JUPUSDT", "RAYUSDT", "CAKEUSDT", "SNXUSDT", "ENAUSDT",
    "MKRUSDT", "LINKUSDT", "PYTHUSDT",
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def safe(v, fallback=0.0):
    return v if v is not None else fallback


def catalyst_timing(coin):
    """0-10: proportional to trend strength, momentum, and volume activity."""
    ts = safe(coin.get("trend_score"), 0.0)
    mom = safe(coin.get("momentum"), 0.0)
    vol_r = safe(coin.get("vol_ratio"), 0.0)
    vol_t = safe(coin.get("vol_trend"), 0.0)
    vol24 = safe(coin.get("vol_24h_usd"), 0.0)

    # Base from trend_score (0-100 → 0-5)
    base = clamp(ts / 20.0, 0.0, 5.0)

    # Momentum bonus
    if mom > 0.01:
        mom_bonus = 2.0
    elif mom > 0.005:
        mom_bonus = 1.5
    elif mom > 0.001:
        mom_bonus = 1.0
    elif mom > 0:
        mom_bonus = 0.5
    else:
        mom_bonus = 0.0

    # Volume activity: spike + trend
    vol_bonus = 0.0
    if vol_r > 1.5:
        vol_bonus += 1.5
    elif vol_r > 1.0:
        vol_bonus += 1.0
    if vol_t > 1.1:
        vol_bonus += 0.5

    # Market size → catalyst visibility
    if vol24 >= 100_000_000:
        mkt_bonus = 1.0
    elif vol24 >= 10_000_000:
        mkt_bonus = 0.5
    else:
        mkt_bonus = 0.0

    score = base + mom_bonus + vol_bonus + mkt_bonus
    return round(clamp(score, 0.0, 10.0), 1)


def supply_timing(coin):
    """0-8: proportional to stability, liquidity depth, and return resilience."""
    vola = safe(coin.get("volatility"), 1.0)
    vol24 = safe(coin.get("vol_24h_usd"), 0.0)
    btc_tr = safe(coin.get("btc_ratio_trend"), 0.0)
    ret90 = safe(coin.get("ret_90d"), 0.0)

    # Price stability → supply distribution maturity
    if vola < 0.02:
        stab = 2.0
    elif vola < 0.04:
        stab = 1.5
    elif vola < 0.06:
        stab = 1.0
    else:
        stab = 0.5

    # Liquidity depth → supply absorption capacity
    if vol24 >= 100_000_000:
        liq = 2.5
    elif vol24 >= 10_000_000:
        liq = 2.0
    elif vol24 >= 1_000_000:
        liq = 1.0
    else:
        liq = 0.5

    # BTC ratio trend → demand pressure
    if btc_tr > 0.01:
        trend_bonus = 2.0
    elif btc_tr > 0:
        trend_bonus = 1.5
    elif btc_tr > -0.01:
        trend_bonus = 1.0  # neutral / range-bound
    else:
        trend_bonus = 0.5

    # Return resilience → supply being absorbed
    if ret90 > 0.2:
        ret_bonus = 1.5
    elif ret90 > 0:
        ret_bonus = 1.0
    elif ret90 > -0.1:
        ret_bonus = 0.5  # mild drawdown
    else:
        ret_bonus = 0.0

    score = stab + liq + trend_bonus + ret_bonus
    return round(clamp(score, 0.0, 8.0), 1)


def main():
    with open(DATA_PATH) as f:
        data = json.load(f)
    with open(OVERRIDES_PATH) as f:
        overrides = json.load(f)

    coins = data.get("coins", {})
    generated = {}
    skipped = {"no_data": 0, "defi": 0, "existing": 0, "bad_status": 0}

    for symbol, coin in coins.items():
        if coin.get("status") != "ok":
            skipped["bad_status"] += 1
            continue
        if symbol in DEFI_PROTOCOLS:
            skipped["defi"] += 1
            continue
        if symbol in overrides:
            skipped["existing"] += 1
            continue

        ct = catalyst_timing(coin)
        st = supply_timing(coin)

        generated[symbol] = {
            "otf": {
                "catalyst_timing": ct,
                "supply_timing": st,
            }
        }

    print(f"Generated OTF entries for {len(generated)} coins")
    print(f"  Skipped: {skipped}")

    if not generated:
        print("Nothing to generate.")
        return

    # Summary statistics
    cats = [v["otf"]["catalyst_timing"] for v in generated.values()]
    sups = [v["otf"]["supply_timing"] for v in generated.values()]
    print(f"  catalyst_timing: min={min(cats):.1f} max={max(cats):.1f} avg={sum(cats)/len(cats):.2f}")
    print(f"  supply_timing:   min={min(sups):.1f} max={max(sups):.1f} avg={sum(sups)/len(sups):.2f}")

    # ── Backup original ──
    bak = str(OVERRIDES_PATH) + ".bak"
    shutil.copy2(OVERRIDES_PATH, bak)
    print(f"  Backup written: {bak}")

    # ── Merge: keep existing keys, add generated ones ──
    # Sort: all existing keys first (preserving their relative order),
    # then new keys sorted alphabetically
    existing_keys = list(overrides.keys())
    new_keys = sorted(generated.keys())
    merged = {}
    for k in existing_keys:
        merged[k] = overrides[k]  # keep full existing entry
    for k in new_keys:
        if k not in merged:
            merged[k] = generated[k]

    with open(OVERRIDES_PATH, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")

    total = len(merged)
    print(f"  Total overrides in file: {total} (was {len(existing_keys)})")
    print("Done.")


if __name__ == "__main__":
    main()
