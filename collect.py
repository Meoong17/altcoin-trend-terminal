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
from altcoin.features import (score_components, classify_volume_flow,
                              score_participation, score_flow_rotation)
from altcoin.regime import classify_regime
from altcoin.history import (append_cycle, stats as history_stats,
                             regime_streak, macro_series, get_model_version)
from altcoin.regime import apply_hysteresis
from altcoin.correlation import concentration_warning
from altcoin.alerts import check_staleness
from liquidity.stablecoin_liquidity import compute_stablecoin_liquidity
from altcoin.news import fetch_news, is_configured as news_configured
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
from altcoin.vaf import evaluate_token, load_overrides, compute_entry_timing, entry_grade

SYMBOL_GROUPS = {
    # ── Sector taxonomy ──
    # Deterministic curation (no API category lookups — they drift and
    # misclassify). Rules:
    #   - Each symbol belongs to EXACTLY ONE sector (enforced by a
    #     self-test). Conflicts resolve to the more fundamental identity:
    #     NEAR is l1 (not ai), DOGE is l1 (own chain), RENDER is ai
    #     (not depin), LDO is defi (not staking).
    #   - "defi" is defined here explicitly and is NOT derived from the
    #     fundamentals protocol map (that map is a superset that also
    #     carries oracle slugs for the infra sector).
    #   - Coins outside every sector (EXTEND_TOP discoveries) are tagged
    #     "other" at runtime.
    #   - Wrong/renamed tickers cost nothing: volume-ranking prunes them.
    "l1": [
        "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
        "TRXUSDT", "TONUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
        "NEARUSDT", "ICPUSDT", "APTUSDT", "SUIUSDT", "ATOMUSDT", "ALGOUSDT",
        "HBARUSDT", "XLMUSDT", "ETCUSDT", "VETUSDT", "FLOWUSDT", "XTZUSDT",
        "SEIUSDT", "INJUSDT", "TIAUSDT", "EGLDUSDT", "KAVAUSDT",
        "SUSDT", "BERAUSDT",
    ],
    "l2": ["OPUSDT", "ARBUSDT", "STRKUSDT", "POLUSDT", "ZKUSDT",
            "TAIKOUSDT", "LINEAUSDT"],
    "defi": ["AAVEUSDT", "UNIUSDT", "PENDLEUSDT", "MORPHOUSDT", "LDOUSDT",
              "CRVUSDT", "COMPUSDT", "GMXUSDT", "DYDXUSDT", "JUPUSDT",
              "RAYUSDT", "CAKEUSDT", "SNXUSDT", "ENAUSDT", "MKRUSDT"],
    "infra": ["LINKUSDT", "PYTHUSDT", "GRTUSDT", "WUSDT", "AXLUSDT", "BANDUSDT"],
    "ai": ["TAOUSDT", "RENDERUSDT", "FETUSDT", "WLDUSDT", "VIRTUALUSDT"],
    "meme": ["SHIBUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT"],
    "gaming": ["SANDUSDT", "MANAUSDT", "AXSUSDT", "GALAUSDT", "IMXUSDT", "APEUSDT"],
    "depin": ["FILUSDT", "ARUSDT", "HNTUSDT", "IOTXUSDT", "THETAUSDT"],
    "rwa": ["ONDOUSDT", "POLYXUSDT", "OMUSDT"],
    "privacy": ["ZECUSDT", "DASHUSDT", "ZENUSDT"],
    "restaking": ["EIGENUSDT", "ETHFIUSDT"],
    # ── Added 2026-08-30 (source: CoinGecko category ∩ OKX spot universe) ──
    # Curated from CoinGecko categories (ai, depin, storage, lending-borrowing)
    # intersected with OKX spot tradable symbols; each coin belongs to exactly
    # one sector (self-test enforced). "compute_financialization" intentionally
    # has NO members: its natural coins (AKT, RLC, IO, NET, GPU, AIOZ) are not
    # listed on OKX, and RENDER/THETA already live in ai/depin — adding them
    # here would break one-coin-one-sector.
    "ai_compute": [
        "ARKMUSDT", "NMRUSDT", "KITEUSDT", "AIXBTUSDT", "KAITOUSDT",
        "GRASSUSDT", "SENTUSDT", "PROMPTUSDT", "MAGICUSDT", "SAHARAUSDT",
        "AEONUSDT", "ALLOUSDT", "ROBOUSDT", "LPTUSDT",
    ],
    "depin_infra": [
        "IOTAUSDT", "GLMUSDT", "PHAUSDT", "ZBCNUSDT", "EDGEUSDT",
        "SPACEUSDT", "ATHUSDT", "XCHUSDT",
    ],
    "storage": ["STXUSDT", "STORJUSDT", "CFGUSDT"],
    "credit": ["KMNOUSDT", "SPKUSDT", "NAVXUSDT"],
}

# symbol -> sector, for tagging coins in ANY mode (incl. TOP_N discovery)
SECTOR_LOOKUP = {s: g for g, syms in SYMBOL_GROUPS.items() for s in syms}

# ── Deterministic per-coin sector classification for coins that fall
# outside the curated SYMBOL_GROUPS lists (e.g. TOP_N / EXTEND_TOP
# discovery picks). Kept as a separate flat map instead of being folded
# into SYMBOL_GROUPS, so the group filter stays clean while every tracked
# coin still resolves to exactly one sector. Curated by hand (no live API
# category lookups — they drift and misclassify). Symbols already present
# in SYMBOL_GROUPS are ignored here to keep one-coin-one-sector.
SECTOR_OVERRIDES = {
    # ── L1 / sovereign chains ──
    "MATICUSDT": "l2", "FTMUSDT": "l1", "EOSUSDT": "l1",
    "ONGUSDT": "l1", "ONTUSDT": "l1", "XEMUSDT": "l1",
    "WAVESUSDT": "l1", "KDAUSDT": "l1", "MINAUSDT": "l1",
    "OSMOUSDT": "l1", "CFXUSDT": "l1", "KAIAUSDT": "l1",
    "KLAYUSDT": "l1", "ARDRUSDT": "l1", "ONEUSDT": "l1",
    "CKBUSDT": "l1", "DCRUSDT": "l1", "AIONUSDT": "l1",
    "NEBLUSDT": "l1", "BTGUSDT": "l1", "VITEUSDT": "l1",
    "COTIUSDT": "l1", "REEFUSDT": "l1", "ELFUSDT": "l1",
    "ACAUSDT": "l1", "HYPERUSDT": "l1", "SAGAUSDT": "l1",
    "NILUSDT": "l1", "TOMOUSDT": "l1", "WTCUSDT": "infra",
    # ── L2 / rollups ──
    "MOVRUSDT": "l2", "GLMRUSDT": "l2", "LOOMUSDT": "l2", "OMGUSDT": "l2",
    # ── DeFi ──
    "ALPACAUSDT": "defi", "MIRUSDT": "defi", "DEXEUSDT": "defi",
    "HIFIUSDT": "defi", "BAKEUSDT": "defi", "BONDUSDT": "defi",
    "OOKIUSDT": "defi", "FXSUSDT": "defi", "BALUSDT": "defi",
    "RENUSDT": "defi", "ANTUSDT": "defi", "CREAMUSDT": "defi",
    "KP3RUSDT": "defi", "KNCUSDT": "defi", "JSTUSDT": "defi",
    "FLMUSDT": "defi", "1INCHUSDT": "defi", "SUSHIUSDT": "defi",
    "ALCXUSDT": "defi", "UNFIUSDT": "defi", "DODOUSDT": "defi",
    "COWUSDT": "defi", "CVXUSDT": "defi", "CVPUSDT": "defi",
    "ORNUSDT": "defi", "NBSUSDT": "defi", "EDENUSDT": "defi",
    "FIDAUSDT": "defi", "LISTAUSDT": "defi", "USUALUSDT": "defi",
    "SPELLUSDT": "defi", "SXPUSDT": "defi", "WINGUSDT": "defi",
    "TCTUSDT": "defi", "VGXUSDT": "defi", "FORUSDT": "defi",
    "FRONTUSDT": "defi", "MAVUSDT": "defi", "TRIBEUSDT": "defi",
    "EULUSDT": "defi", "AEROUSDT": "defi", "SKYUSDT": "defi",
    "ALPHAUSDT": "defi", "BETAUSDT": "defi", "SRMUSDT": "defi",
    "BICOUSDT": "infra", "ORCAUSDT": "defi", "TRIBEUSDT": "defi",
    # ── AI / agents / data ──
    "RNDRUSDT": "ai", "AGIXUSDT": "ai", "OCEANUSDT": "ai",
    "COOKIEUSDT": "ai", "AIUSDT": "ai", "AIGENSYNUSDT": "ai",
    "VANRYUSDT": "ai", "MIRAUSDT": "ai", "SAPIENUSDT": "ai",
    "BREVUSDT": "ai", "A2ZUSDT": "ai", "0GUSDT": "ai",
    "HEIUSDT": "ai", "ASTERUSDT": "ai", "IOUSDT": "depin",
    # ── Infrastructure / oracles / naming / messaging ──
    "ZROUSDT": "infra", "ENSUSDT": "infra", "RIFUSDT": "infra",
    "LITUSDT": "infra", "TWTUSDT": "infra", "SXTUSDT": "infra",
    "DIAUSDT": "infra", "TRBUSDT": "infra", "CLVUSDT": "infra",
    "PNTUSDT": "infra", "AMPUSDT": "infra", "CELRUSDT": "infra",
    "RADUSDT": "infra", "STPTUSDT": "infra", "GNOUSDT": "infra",
    "MASKUSDT": "infra", "DOCKUSDT": "infra",
    # ── Privacy ──
    "XMRUSDT": "privacy", "TORNUSDT": "privacy", "SCRTUSDT": "privacy",
    "FIROUSDT": "privacy", "ZAMAUSDT": "privacy", "PIVXUSDT": "privacy",
    "ATAUSDT": "privacy",
    # ── RWA / tokenized (incl. stablecoins & wrapped) ──
    "XAUTUSDT": "rwa", "RLUSDUSDT": "rwa", "USDSUSDT": "rwa",
    "BFUSDUSDT": "rwa", "WBTCUSDT": "rwa",
    # ── Liquid staking / restaking ──
    "BETHUSDT": "restaking",
    # ── Gaming / GameFi / NFTs ──
    "BNXUSDT": "gaming", "PROMUSDT": "gaming", "DARUSDT": "gaming",
    "MBOXUSDT": "gaming", "ALICEUSDT": "gaming", "HIGHUSDT": "gaming",
    "TVKUSDT": "gaming", "YGGUSDT": "gaming", "BIGTIMEUSDT": "gaming",
    "PORTALUSDT": "gaming", "PYRUSDT": "gaming", "NOTUSDT": "gaming",
    "HMSTRUSDT": "gaming", "BEAMXUSDT": "gaming", "SUPERUSDT": "gaming",
    "RAREUSDT": "gaming", "NFPUSDT": "ai",
    # ── Meme ──
    "TRUMPUSDT": "meme", "PUMPUSDT": "meme", "BROCCOLI714USDT": "meme",
    "BOMEUSDT": "meme", "TURBOUSDT": "meme", "GIGGLEUSDT": "meme",
    "TUTUSDT": "meme", "NEIROUSDT": "meme", "PNUTUSDT": "meme",
    "PENGUUSDT": "meme", "MUBARAKUSDT": "meme", "HOODBUSDT": "meme",
    "GENIUSUSDT": "meme", "CHIPUSDT": "meme", "MMTUSDT": "meme",
    # ── DePIN / compute / storage-adjacent ──
    "LTOUSDT": "depin", "VANRYUSDT": "ai", "SOLVUSDT": "defi",
    # ══ Second pass: remaining discovery coins (verified real tokens) ══
    # ── L1 / sovereign chains ──
    "VICUSDT": "l1", "LUNCUSDT": "l1", "LUNAUSDT": "l1", "MOVEUSDT": "l1",
    "DYMUSDT": "l1", "LUMIAUSDT": "l1", "CHZUSDT": "l1", "DENTUSDT": "l1",
    "GRAMUSDT": "l1", "GASUSDT": "l1", "MITHUSDT": "l1", "IRISUSDT": "l1",
    "DNTUSDT": "l1", "SLFUSDT": "l1", "REDUSDT": "l1", "EDUUSDT": "l1",
    "OPENUSDT": "l1", "OPNUSDT": "l1", "PERLUSDT": "l1", "STOUSDT": "l1",
    # ── L2 / rollups / interop ──
    "SCRUSDT": "l2", "ALTUSDT": "l2", "ZKCUSDT": "l2", "INITUSDT": "l2",
    "OMNIUSDT": "l2", "HEMIUSDT": "l2", "FORMUSDT": "l2",
    # ── DeFi ──
    "NEXOUSDT": "defi", "RUNEUSDT": "defi", "YFIUSDT": "defi", "YFIIUSDT": "defi",
    "MDXUSDT": "defi", "BSWUSDT": "defi", "EPXUSDT": "defi", "LEVERUSDT": "defi",
    "AUTOUSDT": "defi", "HFTUSDT": "defi", "MITOUSDT": "defi", "OGNUSDT": "defi",
    "BIOUSDT": "defi", "AUCTIONUSDT": "defi", "REPUSDT": "defi", "METUSDT": "defi",
    "FFUSDT": "defi", "NEWTUSDT": "defi", "WLFIUSDT": "defi", "DREPUSDT": "defi",
    "BTSUSDT": "defi", "ANCUSDT": "defi", "BANKUSDT": "defi", "TSTUSDT": "defi",
    "UTKUSDT": "defi", "POLSUSDT": "defi", "STMXUSDT": "defi",
    # ── Credit / tokenized lending ──
    "TRUUSDT": "credit", "HUMAUSDT": "credit",
    # ── Privacy ──
    "ZKPUSDT": "privacy", "MOBUSDT": "privacy", "DUSKUSDT": "privacy",
    "PARTIUSDT": "privacy", "EPICUSDT": "privacy", "MFTUSDT": "privacy",
    # ── RWA / tokenized ──
    "POLYUSDT": "rwa", "MANTRAUSDT": "rwa", "PLUMEUSDT": "rwa",
    # ── Storage / file ──
    "BLZUSDT": "storage", "BTTCUSDT": "storage",
    # ── Liquid staking / restaking ──
    "BBUSDT": "restaking", "KERNELUSDT": "restaking", "LAYERUSDT": "restaking",
    "WBETHUSDT": "restaking", "JTOUSDT": "restaking",
    # ── AI / agents / data ──
    "VANAUSDT": "ai", "KATUSDT": "ai", "CTXCUSDT": "ai", "SOPHUSDT": "ai",
    "AUDIOUSDT": "ai", "VIBUSDT": "ai", "ACMUSDT": "ai",
    # ── Infrastructure / oracles / naming / messaging ──
    "SYNUSDT": "infra", "HOLOUSDT": "infra", "QNTUSDT": "infra", "IDUSDT": "infra",
    "GPSUSDT": "infra", "GALUSDT": "infra", "MULTIUSDT": "infra",
    "JASMYUSDT": "infra", "AMBUSDT": "infra", "SNTUSDT": "infra",
    # ── Gaming / GameFi / NFTs ──
    "COCOSUSDT": "gaming", "GFTUSDT": "gaming", "MCUSDT": "gaming",
    "ERNUSDT": "gaming", "PDAUSDT": "gaming", "PLAUSDT": "gaming",
    "LOKAUSDT": "gaming", "BEAMUSDT": "gaming", "ACEUSDT": "gaming",
    "COMBOUSDT": "gaming", "ERAUSDT": "gaming", "DEGOUSDT": "gaming",
    "HOOKUSDT": "gaming", "NIGHTUSDT": "gaming", "MEUSDT": "gaming",
    "GUNUSDT": "gaming", "OGUSDT": "gaming", "TLMUSDT": "gaming",
    "MBLUSDT": "gaming", "WCTUSDT": "gaming", "GMTUSDT": "gaming",
    "WINUSDT": "gaming", "TNSRUSDT": "gaming",
    # ── Meme ──
    "PEOPLEUSDT": "meme", "ORDIUSDT": "meme", "1000SATSUSDT": "meme",
    "BARDUSDT": "meme", "MEGAUSDT": "meme",
    # ── DePIN / compute ──
    "PONDUSDT": "depin",
    # ══ Third pass: last "other" coins researched & classified (Binance-listed) ══
    "CELOUSDT": "l1",        # Celo — mobile-first L1
    "XPLUSDT": "l1",         # Plasma — stablecoin-focused L1
    "WRXUSDT": "defi",       # WazirX — exchange token
    "ENSOUSDT": "defi",      # Enso — intent/automation layer
    "BTCSTUSDT": "defi",     # BTCST — hashrate yield token
    "DOLOUSDT": "defi",      # Dolomite — money market / lending
    "YBUSDT": "defi",        # Yield Basis — yield optimization
    "TREEUSDT": "defi",      # Treehouse — fixed-income yield protocol
    "PHBUSDT": "ai",         # Phoenix — decentralized AI compute
    "SHELLUSDT": "ai",       # MyShell — decentralized AI consumer layer
    "GTOUSDT": "gaming",     # Gifto — gifting/streaming
    "DUSDT": "gaming",       # Dar Open Network (ex Mines of Dalarnia)
    "BMTUSDT": "infra",      # Bubblemaps — on-chain data analytics
    "TOWNSUSDT": "infra",    # Towns — decentralized social
    "SIGNUSDT": "infra",     # Sign Protocol — attestation infra
    "UUSDT": "infra",        # Union — ZK interoperability layer
    "LAUSDT": "infra",       # Lagrange — ZK prover network
    "OPGUSDT": "ai_compute", # OpenGradient — verifiable AI compute
    "ATUSDT": "ai_compute",  # APRO — decentralized compute
}
# Hand-curated additions always win over the flat override map; merges in
# every symbol that is not already claimed by a curated group.
SECTOR_LOOKUP.update({s: g for s, g in SECTOR_OVERRIDES.items() if s not in SECTOR_LOOKUP})

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
    # NOTE (audit 2026-08, cf. 1.docx §2/§7 + analysis/macro_constant_test.py):
    # macro_component is the SAME global value for every coin in a snapshot,
    # so in v2 it adds zero cross-sectional ranking information and is NOT fed
    # into the coin score anymore (regime.py owns GLF/repo as the regime/
    # context layer). It is kept here only for the v1-legacy fallback blend
    # (degraded rows with no feature history) and for display.
    macro_component = (glf_score if glf_score is not None else 50.0)
    macro_component -= (repo_stress_score - 0.5) * 40 if repo_stress_score is not None else 0

    # ── Score v2: blueprint §2.2A composite when the feature set exists ──
    feats = coin_result.get("features") or {}

    # Volume / participation vs DIRECTIONAL FLOW are conceptually distinct
    # (volume = activity; flow = WHO presses the market and WHICH WAY).
    # participation uses volume level/acceleration; flow_rotation uses the
    # taker-buy share (aggressive buyers vs total volume) from the coin's
    # directional-flow metrics. Both are coin-specific, so unlike the old
    # constant macro they carry cross-sectional selection information.
    participation = score_participation(
        coin_result.get("vol_ratio"), coin_result.get("vol_trend"))
    flow_m = coin_result.get("flow") or {}
    flow_rotation = score_flow_rotation(
        flow_m.get("buy_share_3d"), flow_m.get("buy_share_7d"),
        flow_m.get("flow_trend"))

    if any(v is not None for v in feats.values()):
        score, drivers, coverage = score_components(
            feats, rsi, participation=participation, flow_rotation=flow_rotation)
        if score is not None:
            detail = {"status": "ok", "version": "v2-features",
                     "drivers": drivers, "coverage": coverage,
                     "participation": participation, "flow_rotation": flow_rotation}
            # Comparability flag: a non-Binance source means volume-derived
            # inputs (VolPct90, ATRexp feed the breakout component) were
            # degraded or absent for THIS coin on THIS cycle, so its score
            # isn't computed from the same component set as a Binance-served
            # coin even when the numeric coverage looks similar.
            src = coin_result.get("data_source")
            if src and src != "binance":
                detail["comparability_note"] = (
                    f"priced via {src} fallback this cycle \u2014 volume-derived "
                    "inputs may be degraded; compare with caution")
            return score, detail

    # Legacy v1 blend — kept for degraded rows (fallback source without
    # OHLC history) so a data-source failure degrades the score instead
    # of erasing it. Tagged so the UI/history can tell versions apart.
    # Weight alignment (audit 2026-08): macro cut from 0.25 -> 0.10 to match
    # the v2 treatment (constant global, no cross-sectional info); the freed
    # 0.15 goes to relative strength (the actual coin-specific signal).
    trend_score = (
        0.35 * rsi_component +
        0.55 * relative_strength_component +
        0.10 * macro_component
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
    if names == ["all"]:
        names = sorted(SYMBOL_GROUPS)
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
    parser.add_argument("--extend-top", type=int, default=None, metavar="N",
                         help="With --group: ALSO track the top N altcoin USDT pairs by "
                              "24h volume beyond the curated groups (tagged group 'market')")
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
            # Curated coins that Binance doesn't list but a fallback source
            # (Bybit) serves are real, tradable assets (e.g. Aethir/ATH) —
            # keep them so they surface via the Bybit fallback instead of
            # being dropped as if they were dead cards.
            try:
                from altcoin.bybit_fallback import fetch_klines_bybit
                keep = [s for s in dropped if fetch_klines_bybit(s, limit=1)]
                if keep:
                    print(f"[Collect] Bybit-only curated coins re-added: {keep}", file=sys.stderr)
                    ranked = ranked + keep
            except Exception as e:
                print(f"[Collect] Bybit re-add check failed: {e}", file=sys.stderr)
            symbols = ranked
        if top_n:
            symbols = symbols[:top_n]
        env_ext = os.environ.get("EXTEND_TOP")
        extend_n = args.extend_top or (int(env_ext) if env_ext and env_ext.isdigit() else None)
        if extend_n:
            market = discover_top_symbols(extend_n) or []
            added = [s for s in market if s not in groups_map]
            for s in added:
                groups_map[s] = ["market"]
            symbols = symbols + added
            group_label += f"+top{extend_n}"
            print(f"[Collect] Extended universe: +{len(added)} market coins "
                  f"beyond curated groups")
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

    # ── universal sector tagging ──
    # Every tracked coin gets a sector regardless of how it was selected:
    # curated members keep their group tag, discovery/custom coins get
    # their sector from SECTOR_LOOKUP, unknowns become "other". This is
    # what makes TOP_N / EXTEND_TOP universes fully categorized instead
    # of one undifferentiated "market" lump.
    for s in symbols:
        if s not in groups_map:
            groups_map[s] = [SECTOR_LOOKUP.get(s, "other")]
        elif groups_map[s] == ["market"]:
            groups_map[s] = [SECTOR_LOOKUP.get(s, "other")]
    universe["groups"] = sorted(set(g for gs in groups_map.values() for g in gs))

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
    # Loaded once, unconditionally: used by full VaF (DeFi/infra coins
    # below) AND by the standalone entry-timing pass (every coin) --
    # the same vaf_overrides.json schema serves both.
    overrides = load_overrides()

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
            # Persist the RAW fundamental inputs (tvl, fees_7d/30d,
            # revenue_30d, holders_revenue) per-date too, so a future
            # value test is not hostage to today's composite formula.
            # These flow into history.db snapshots via append_cycle.
            coins_output[symbol]["fundamental_raw"] = _raw
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
        for symbol, raw in raws.items():
            row = coins_output[symbol]
            row["mcap"] = mcaps.get(symbol)
            row["vaf"] = evaluate_token(
                symbol, raw, mcap=mcaps.get(symbol),
                features=row.get("features"),
                trend_drivers=(row.get("trend_score_detail") or {}).get("drivers"),
                peer_raws=peer_raws, peer_pf=peer_pf, overrides=overrides)
            print(f"[VaF] {symbol}: VaF={row['vaf']['vaf']} ({row['vaf']['tier']}) "
                  f"OTF={row['vaf']['otf']} VFR={row['vaf']['vfr']['display']} "
                  f"conf={row['vaf']['confidence']} -> {row['vaf']['verdict']}")

    # ── Entry Timing grade: OTF for the WHOLE universe, not just DeFi ──
    # Coins with a full VaF row reuse its OTF verbatim (single source of
    # truth, no risk of two "OTF" numbers drifting apart under the same
    # name). Every other coin gets OTF computed standalone from data it
    # already has (trend drivers + features) -- see compute_entry_timing.
    for symbol, row in coins_output.items():
        if row.get("status") != "ok":
            continue
        if "vaf" in row:
            row["entry_timing"] = {
                "otf": row["vaf"]["otf"],
                "coverage": row["vaf"]["coverage"].get("otf"),
                "grade": entry_grade(row["vaf"]["otf"], row["vaf"]["coverage"].get("otf")),
            }
        else:
            row["entry_timing"] = compute_entry_timing(
                features=row.get("features"),
                trend_drivers=(row.get("trend_score_detail") or {}).get("drivers"),
                overrides=overrides.get(symbol))
    graded = sum(1 for r in coins_output.values() if r.get("entry_timing", {}).get("grade") not in (None, "N/A"))
    print(f"[Collect] Entry Timing graded: {graded}/{len(coins_output)} coins")

    # ── Volume-flow label (DISPLAY-ONLY, never feeds scoring) ──
    # The "where does money move before price realizes it" layer. Classifies
    # each coin's volume signal (vol_ratio spike, vol_trend warming) against
    # price position (prox_30d_high) into EARLY ACCUMULATION / QI FLOW /
    # BREAKOUT / EXTENDED / NO SIGNAL. Purely a label on top of existing data
    # (no new API calls); it does NOT touch trend_score or entry_timing.
    for symbol, row in coins_output.items():
        if row.get("status") != "ok":
            continue
        row["volume_flow"] = classify_volume_flow(
            row.get("features"), row.get("vol_ratio"), row.get("vol_trend"),
            row.get("trend_score"))

    # ── News sentiment & catalyst layer (display-only) ──
    if news_configured():
        try:
            news = fetch_news(list(coins_output))
            for symbol, nd in news.items():
                if symbol in coins_output:
                    coins_output[symbol]["news"] = nd
            print(f"[Collect] News layer: {len(news)} coins with tagged posts")
        except Exception as e:
            print(f"[Collect] News layer failed: {e}", file=sys.stderr)

    # ── Stablecoin liquidity (measured leg of the v2.0 liquidity score) ──
    stable_score, stable_detail = compute_stablecoin_liquidity()
    if stable_score is not None:
        print(f"[Collect] Stablecoin liquidity: {stable_score} "
              f"(30d {stable_detail['growth_30d']:+.2%})")

    # ── Market structure snapshot: BTC dominance ──
    # CoinGecko /global is keyless; deltas come from OUR OWN accumulated
    # daily snapshots in history.db (honest n/a until >=8 days stored).
    btc_dom = None
    try:
        import requests as _rq
        g = _rq.get("https://api.coingecko.com/api/v3/global", timeout=15).json()
        btc_dom = round(float(g["data"]["market_cap_percentage"]["btc"]), 2)
    except Exception as e:
        print(f"[Collect] BTC.D fetch failed: {e}", file=sys.stderr)
    dom_hist = macro_series("macro.market.btc_dominance")
    dom_d7 = round(btc_dom - dom_hist[-7][1], 2) if btc_dom is not None and len(dom_hist) >= 7 else None
    market = {"btc_dominance": btc_dom, "btc_dominance_d7": dom_d7,
              "history_days": len(dom_hist)}

    btc_klines = fetch_klines("BTCUSDT")
    btc_closes = [c for _, _, _, c, _, _ in btc_klines] if btc_klines else None
    regime = classify_regime(btc_closes, glf_score=glf_score, repo_stress=repo_score)
    prev_state, streak = regime_streak()
    regime = apply_hysteresis(regime, prev_state, streak)
    if regime.get("held"):
        print(f"[Collect] Regime hysteresis: holding {regime['state']}, "
              f"pending {regime['pending']}")
    print(f"[Collect] Market regime: {regime['state']} ({'; '.join(regime['reasons'])})")

    alt_season = compute_alt_season(coin_results)
    if alt_season:
        print(f"[Collect] Alt Season Index: {alt_season['index']} ({alt_season['label']}, "
              f"{alt_season['outperformers']}/{alt_season['sample']} beat BTC 90d)")

    # ── Context Gate (DISPLAY-ONLY) — majors BTC+ETH + rotation ──
    # The gate the volume-flow layer is conditional on: money rotating into
    # alts only after majors (BTC+ETH) are risk-on. ETH is a MAJOR (co-leads
    # with BTC), NOT a member of the alt basket. Computed from data already in
    # memory (no new API calls). Display-only — never feeds scoring.
    #   majors_ret30 = median(BTC 30d, ETH 30d)   -> RISK_ON / NEUTRAL / RISK_OFF
    #   rotation     = alt_ret30 - majors_ret30   -> ALT_LEAD / FLAT / MAJ_LEAD
    #   gate_open    = majors RISK_ON AND rotation ALT_LEAD
    def _ret_from_closes(closes, lag=30):
        if closes is None or len(closes) <= lag:
            return None
        return closes[-1] / closes[-1 - lag] - 1
    btc_ret30 = _ret_from_closes(btc_closes)
    eth_ret30 = None
    if "ETHUSDT" in coins_output:
        eth_ret30 = (coins_output["ETHUSDT"].get("performance") or {}).get("ret_30d")
    majors_vals = [v for v in (btc_ret30, eth_ret30) if v is not None]
    majors_ret30 = (sum(majors_vals) / len(majors_vals)) if majors_vals else None
    alt_rets = [c.get("performance", {}).get("ret_30d") for s, c in coins_output.items()
                if c.get("status") == "ok" and s != "ETHUSDT"]
    alt_rets = [r for r in alt_rets if r is not None]
    alts_ret30 = (sum(alt_rets) / len(alt_rets)) if alt_rets else None
    context_gate = {
        "majors_ret30": round(majors_ret30, 4) if majors_ret30 is not None else None,
        "alts_ret30": round(alts_ret30, 4) if alts_ret30 is not None else None,
        "rotation": round(alts_ret30 - majors_ret30, 4)
            if (alts_ret30 is not None and majors_ret30 is not None) else None,
        "majors": ("RISK_ON" if majors_ret30 > 0.05 else
                   "RISK_OFF" if majors_ret30 < -0.05 else "NEUTRAL")
            if majors_ret30 is not None else None,
        "rotation_state": ("ALT_LEAD"
            if (alts_ret30 is not None and majors_ret30 is not None and
                alts_ret30 - majors_ret30 > 0.05)
            else "MAJ_LEAD"
            if (alts_ret30 is not None and majors_ret30 is not None and
                alts_ret30 - majors_ret30 < -0.05) else "FLAT"),
        "gate_open": bool(
            majors_ret30 is not None and alts_ret30 is not None and
            majors_ret30 > 0.05 and alts_ret30 - majors_ret30 > 0.05),
    }
    print(f"[Collect] Context Gate: majors={context_gate['majors']} "
          f"({context_gate['majors_ret30']}) rotation={context_gate['rotation_state']} "
          f"({context_gate['rotation']}) -> open={context_gate['gate_open']}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "alt_season": alt_season,
        "regime": regime,
        "market": market,
        "context_gate": context_gate,
        "macro": {
            "stablecoin_liquidity": stable_score,
            "stablecoin_detail": stable_detail,
            "glf_score": glf_score,
            "glf_details": glf_details,
            "repo_stress_score": repo_score,
            "repo_stress_details": repo_details,
        },
        "coins": coins_output,
    }

    # ── Portfolio correlation warning (top-10 by trend_score) ──
    # Zero new API calls: closes_30d is already in memory from analysis.
    ranked = sorted(
        (s for s, c in coins_output.items() if c.get("status") == "ok" and c.get("trend_score") is not None),
        key=lambda s: -coins_output[s]["trend_score"])[:10]
    closes_map = {s: coins_output[s].get("closes_30d") for s in ranked}
    conc = concentration_warning(ranked, closes_map)
    if conc:
        output["concentration_warning"] = conc
        flag = "\u26a0 HIGH CONCENTRATION" if conc["flag"] else "ok"
        print(f"[Collect] Top-10 correlation: avg={conc['avg_corr']} ({flag})")

    # ── Staleness alert: check the OUTGOING file's age before we overwrite it ──
    check_staleness(DATA_OUT, max_age_hours=int(os.environ.get("STALE_ALERT_HOURS", 8)))

    with open(DATA_OUT, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[Collect] Wrote {DATA_OUT}")
    model_version = get_model_version()
    rows = append_cycle(coins_output, {**output["macro"], "market": market},
                        universe, regime, model_version=model_version)
    hs = history_stats()
    print(f"[Collect] History: +{rows} rows -> {hs['rows']} rows across {hs['days']} days "
          f"(model_version={model_version})")

    # ── Evaluation pipeline (background, never touches production scoring) ──
    # Runs every cycle by design: builds the walk-forward evaluation dataset
    # continuously with zero manual intervention, exactly the pattern this
    # was designed for. sufficient_sample stays False and is reported
    # honestly until there's really enough history -- automating the RUN
    # never automates the CONCLUSION.
    try:
        from altcoin.backtest import run_backtest
        bt = run_backtest(write=True)
        print(f"[Backtest] sample_days={bt['sample_days']} "
              f"sufficient_sample={bt['sufficient_sample']} "
              f"folds={len(bt['walk_forward_folds'])}")
        with open(os.path.join(os.path.dirname(DATA_OUT), "backtest_latest.json"), "w") as f:
            json.dump(bt, f, indent=2, default=str)
    except Exception as e:
        print(f"[Backtest] evaluation pipeline failed (production unaffected): {e}",
              file=sys.stderr)

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
    from altcoin.alerts import send as _alert_send, format_exception_alert as _fmt_exc
    try:
        main()
    except Exception as e:
        _alert_send(_fmt_exc(e, context="collect.py main()"))
        raise  # cron log still gets the full traceback; alert is additive, not a replacement
