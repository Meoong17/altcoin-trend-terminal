#!/usr/bin/env python3
"""
collect.py — Altcoin Trend Terminal entry point
====================================================

Produces a per-coin trend snapshot for every symbol in TRACKED_SYMBOLS,
sharing ONE computation of the macro liquidity layer (GLF, Repo Market
Stress) across all of them — these are asset-agnostic macro signals, so
computing them once per cycle and reusing them for every tracked coin is
both cheaper (no redundant FRED API calls) and more honest: if ETH's and
SOL's scores differ, the difference is guaranteed to come from their
coin-specific technical layer, not from two independently-computed (and
potentially silently drifting) copies of the same macro data.

Usage:
    python3 collect.py                 # print + write data.json
    python3 collect.py --symbols ETHUSDT,SOLUSDT,BNBUSDT

Add/remove tracked coins via TRACKED_SYMBOLS below or --symbols — no
code changes needed elsewhere, since altcoin/analyzer.py is fully
parameterized by symbol (see its own self-test for verification of this).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altcoin.analyzer import (analyze_multiple_coins, discover_top_symbols, compute_alt_season,
                              rank_symbols_by_volume, fetch_klines)
from altcoin.features import score_components
from altcoin.regime import classify_regime
from altcoin.history import append_cycle, stats as history_stats
from liquidity.global_liquidity_engine import compute_global_liquidity_factor
from liquidity.repo_market_stress import compute_repo_stress

TRACKED_SYMBOLS = ["ETHUSDT", "SOLUSDT", "BNBUSDT"]

# ── Curated universes ──
# L1 = coins that are the native asset of their own base-layer blockchain.
# Deliberately a hardcoded list, not an API category lookup: Binance has no
# category metadata, and third-party category APIs (CoinGecko etc.) drift.
# Curation notes:
#   - BTC excluded: it's the benchmark the whole ratio layer measures against
#   - L2s / rollups excluded (OP, ARB, MNT, POL, CELO post-migration...)
#   - Renamed/delisted tickers excluded (FTM→S, EOS→A, XMR, WAVES)
#   - The list self-heals at runtime: rank_symbols_by_volume() drops any
#     symbol Binance no longer trades, so a stale entry here costs nothing
from altcoin.fundamentals import (DEFI_PROTOCOLS, fetch_fundamentals,
                                   blend_composite, ANNUALIZE_30D)
from altcoin.vaf import evaluate_token, load_overrides

SYMBOL_GROUPS = {
    # DeFi tier: symbols mirror the fundamentals layer's protocol map so
    # the two universes can't drift apart
    "defi": sorted(DEFI_PROTOCOLS),
    # Infrastructure: oracles/data/interop — the non-DeFi sector with the
    # most measurable fundamentals (Chainlink & Pyth have fee data on
    # DeFiLlama, wired in fundamentals.DEFI_PROTOCOLS)
    "infra": ["LINKUSDT", "PYTHUSDT", "GRTUSDT", "WUSDT", "AXLUSDT"],
    # AI/compute: strong-narrative sector; fundamentals mostly n/a
    # free-tier, so these ride the technical + VaF-manual layers
    "ai": ["TAOUSDT", "RENDERUSDT", "FETUSDT", "NEARUSDT", "WLDUSDT"],
    "l1": [
        "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
        "TRXUSDT", "TONUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
        "NEARUSDT", "ICPUSDT", "APTUSDT", "SUIUSDT", "ATOMUSDT", "ALGOUSDT",
        "HBARUSDT", "XLMUSDT", "ETCUSDT", "VETUSDT", "FLOWUSDT", "XTZUSDT",
        "SEIUSDT", "INJUSDT", "TIAUSDT", "EGLDUSDT", "KAVAUSDT",
        "SUSDT",    # Sonic (ex-Fantom)
        "BERAUSDT", # Berachain
    ],
}

DATA_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")


def compute_coin_trend_score(coin_result, glf_score, repo_stress_score):
    """
    Combine one coin's technical state with the shared macro layer into a
    single 0-100 trend score.

    This is a deliberately simple, explainable starting formula — NOT a
    validated model. Unlike the SFC (BTC) system's ensemble, which went
    through extensive circular-labeling fixes and price-outcome-based
    training before its accuracy claims could be trusted, this altcoin
    scoring has had NO such validation yet. Treat trend_score as a
    directional heuristic to monitor, not a backtested signal, until it's
    been run against real price-outcome data the way ml_ensemble.py/
    ensemble_meta.py were for BTC.

    Formula: weighted blend of RSI (technical), momentum vs BTC (relative
    strength), and macro liquidity backdrop.
    """
    if coin_result.get("status") != "ok":
        return None, {"status": "unavailable"}

    rsi = coin_result.get("rsi")
    btc_ratio_trend = coin_result.get("btc_ratio_trend")
    momentum = coin_result.get("momentum")

    # RSI component: 0-100 already, but center it — RSI=50 is neutral,
    # not RSI=0. Rescale so neutral RSI contributes a neutral 50 to the
    # blend, not near-zero.
    rsi_component = rsi if rsi is not None else 50.0

    # BTC-relative strength: positive btc_ratio_trend = outperforming BTC
    # (classic "altseason" direction for this specific coin). Scaled by
    # an arbitrary-but-documented factor to bring typical daily-momentum
    # magnitudes (~0.001-0.02 range) into a 0-100-ish contribution;
    # this scaling has NOT been calibrated against real outcome data —
    # see the validation caveat in the docstring above.
    if btc_ratio_trend is not None:
        relative_strength_component = 50 + max(-50, min(50, btc_ratio_trend * 2000))
    else:
        relative_strength_component = 50.0

    # Macro backdrop: GLF > 50 = liquidity expansive = generally
    # supportive of risk assets including alts. Repo stress > 0.5 =
    # funding market stress = generally bearish/risk-off.
    macro_component = (glf_score if glf_score is not None else 50.0)
    macro_component -= (repo_stress_score - 0.5) * 40 if repo_stress_score is not None else 0

    # ── Score v2: blueprint §2.2A composite when the feature set exists ──
    feats = coin_result.get("features") or {}
    if any(v is not None for v in feats.values()):
        score, drivers = score_components(feats, rsi, macro_component)
        if score is not None:
            return score, {"status": "ok", "version": "v2-features",
                           "drivers": drivers}

    # Legacy v1 blend — kept for degraded rows (fallback source without
    # OHLC history) so a data-source failure degrades the score instead
    # of erasing it. Tagged so the UI/history can tell versions apart.
    trend_score = (
        0.35 * rsi_component +
        0.40 * relative_strength_component +
        0.25 * macro_component
    )
    trend_score = max(0.0, min(100.0, trend_score))

    detail = {
        "status": "ok",
        "version": "v1-legacy",
        "rsi_component": round(rsi_component, 2),
        "relative_strength_component": round(relative_strength_component, 2),
        "macro_component": round(macro_component, 2),
    }
    return round(trend_score, 2), detail


def resolve_groups(group_str):
    """
    "l1,defi" -> (merged_symbols, groups_map, label). Pure so the offline
    self-test can exercise merge/dedupe/labeling. Unknown group names
    raise ValueError with the valid options listed.

    A symbol may belong to several groups; groups_map keeps them all so
    the dashboard can filter without re-collecting.
    """
    names = [g.strip().lower() for g in (group_str or "").split(",") if g.strip()]
    unknown = [g for g in names if g not in SYMBOL_GROUPS]
    if unknown:
        raise ValueError(f"unknown group(s) {unknown}; valid: {sorted(SYMBOL_GROUPS)}")
    merged, groups_map = [], {}
    for g in names:
        for s in SYMBOL_GROUPS[g]:
            if s not in groups_map:
                merged.append(s)
                groups_map[s] = []
            groups_map[s].append(g)
    return merged, groups_map, "+".join(names)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated Binance symbols, e.g. ETHUSDT,SOLUSDT")
    parser.add_argument("--top", type=int, default=None, metavar="N",
                         help="Auto-track the top N altcoin USDT pairs by 24h volume "
                              "(stablecoins/leveraged tokens excluded, BTC is benchmark)")
    parser.add_argument("--group", type=str, default=None,
                         help="Curated universe(s), comma-separated: 'l1', 'defi', or "
                              "'l1,defi' for both in one dashboard. Combine with --top N "
                              "to keep only the N largest by 24h volume")
    args = parser.parse_args()

    # Symbol resolution priority: --group > --top > --symbols > SYMBOLS env >
    # TRACKED_SYMBOLS default. GROUP / TOP_N envs mirror the flags for cron
    # and GitHub Actions use.
    env_top = os.environ.get("TOP_N")
    top_n = args.top or (int(env_top) if env_top and env_top.isdigit() else None)
    group = args.group or os.environ.get("GROUP", "").strip().lower() or None

    universe = {"mode": "default"}
    groups_map = {}
    if group:
        curated, groups_map, group_label = resolve_groups(group)
        ranked = rank_symbols_by_volume(curated)
        if ranked is None:
            print("[Collect] Volume ranking failed — using curated order, no delist-pruning",
                  file=sys.stderr)
            symbols = list(curated)
        else:
            dropped = sorted(set(curated) - set(ranked))
            if dropped:
                print(f"[Collect] Pruned (not trading on Binance): {dropped}", file=sys.stderr)
            symbols = ranked
        if top_n:
            symbols = symbols[:top_n]
        universe = {"mode": group_label, "curated": len(curated),
                    "tracked": len(symbols), "groups": sorted(set(
                        g for gs in groups_map.values() for g in gs))}
    elif top_n:
        symbols = discover_top_symbols(top_n)
        if not symbols:
            print("[Collect] Discovery failed — falling back to static list", file=sys.stderr)
            symbols = TRACKED_SYMBOLS
        else:
            universe = {"mode": f"top {top_n} by volume", "tracked": len(symbols)}
    else:
        raw = args.symbols or os.environ.get("SYMBOLS")
        symbols = ([s.strip().upper() for s in raw.split(",") if s.strip()]
                   if raw else TRACKED_SYMBOLS)
        universe = {"mode": "custom" if raw else "default", "tracked": len(symbols)}

    print(f"[Collect] Tracking {len(symbols)} coins: {symbols}", file=sys.stderr)

    # ── Shared macro layer (computed ONCE, reused for every coin) ──
    print("[Collect] Computing shared macro layer (GLF, repo stress)...", file=sys.stderr)
    try:
        glf_score, glf_stress, glf_details = compute_global_liquidity_factor()
    except Exception as e:
        print(f"[Collect] GLF computation failed: {e}", file=sys.stderr)
        glf_score, glf_details = None, {"status": "error", "error": str(e)}

    try:
        repo_score, repo_details = compute_repo_stress()
    except Exception as e:
        print(f"[Collect] Repo stress computation failed: {e}", file=sys.stderr)
        repo_score, repo_details = None, {"status": "error", "error": str(e)}

    # ── Per-coin technical layer ──
    print("[Collect] Fetching per-coin technical data...", file=sys.stderr)
    coin_results = analyze_multiple_coins(symbols)

    coins_output = {}
    for symbol, coin_result in coin_results.items():
        trend_score, score_detail = compute_coin_trend_score(coin_result, glf_score, repo_score)
        coins_output[symbol] = {
            **coin_result,
            "trend_score": trend_score,
            "trend_score_detail": score_detail,
            **({"groups": groups_map[symbol]} if symbol in groups_map else {}),
        }

    # ── Fundamental Intelligence layer (DeFi protocols only) ──
    fund_symbols = [s for s in coins_output if s in DEFI_PROTOCOLS]
    if fund_symbols:
        print(f"[Collect] Fetching fundamentals for {len(fund_symbols)} DeFi protocols")
        try:
            fundamentals = fetch_fundamentals(fund_symbols)
        except Exception as e:  # layer failure must not sink the cycle
            print(f"[Collect] Fundamentals layer failed: {e}", file=sys.stderr)
            fundamentals = {}
        for symbol, (fscore, fdet, _raw) in fundamentals.items():
            coins_output[symbol]["fundamental_score"] = fscore
            coins_output[symbol]["fundamental_detail"] = fdet
            coins_output[symbol]["composite_score"] = blend_composite(
                coins_output[symbol].get("trend_score"), fscore)

        # ── VaF v1.0 layer (locked framework, hybrid auto/manual) ──
        raws = {s: r for s, (_, _, r) in fundamentals.items()}
        peer_raws = list(raws.values())
        from altcoin.coinstats_fallback import mcaps_for
        mcaps = mcaps_for(list(raws))
        peer_pf = [mcaps[s] / (r["fees_30d"] * ANNUALIZE_30D)
                   for s, r in raws.items()
                   if s in mcaps and r.get("fees_30d")]
        overrides = load_overrides()
        for symbol, raw in raws.items():
            row = coins_output[symbol]
            row["vaf"] = evaluate_token(
                symbol, raw, mcap=mcaps.get(symbol),
                features=row.get("features"),
                trend_drivers=(row.get("trend_score_detail") or {}).get("drivers"),
                peer_raws=peer_raws, peer_pf=peer_pf, overrides=overrides)
            print(f"[VaF] {symbol}: VaF={row['vaf']['vaf']} ({row['vaf']['tier']}) "
                  f"OTF={row['vaf']['otf']} VFR={row['vaf']['vfr']['display']} "
                  f"conf={row['vaf']['confidence']} -> {row['vaf']['verdict']}")

    btc_klines = fetch_klines("BTCUSDT")
    btc_closes = [c for _, _, _, c, _ in btc_klines] if btc_klines else None
    regime = classify_regime(btc_closes, glf_score=glf_score, repo_stress=repo_score)
    print(f"[Collect] Market regime: {regime['state']} ({'; '.join(regime['reasons'])})")

    alt_season = compute_alt_season(coin_results)
    if alt_season:
        print(f"[Collect] Alt Season Index: {alt_season['index']} ({alt_season['label']}, "
              f"{alt_season['outperformers']}/{alt_season['sample']} beat BTC 90d)")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "alt_season": alt_season,
        "regime": regime,
        "macro": {
            "glf_score": glf_score,
            "glf_details": glf_details,
            "repo_stress_score": repo_score,
            "repo_stress_details": repo_details,
        },
        "coins": coins_output,
    }

    with open(DATA_OUT, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[Collect] Wrote {DATA_OUT}")
    rows = append_cycle(coins_output, output["macro"], universe, regime)
    hs = history_stats()
    print(f"[Collect] History: +{rows} rows -> {hs['rows']} rows across {hs['days']} days")
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"GLF: {glf_score}  |  Repo Stress: {repo_score}")
    for symbol, data in coins_output.items():
        ts = data.get("trend_score")
        v24 = data.get("vol_24h_usd")
        vr = data.get("vol_ratio")
        vol_str = (f"vol24h=${v24/1e6:,.0f}M ratio={vr}x trend={data.get('vol_trend')}"
                   if v24 is not None else "vol=n/a")
        print(f"  {symbol}: trend_score={ts}  (RSI={data.get('rsi')}, "
              f"btc_ratio_trend={data.get('btc_ratio_trend')}, {vol_str})")


if __name__ == "__main__":
    main()
