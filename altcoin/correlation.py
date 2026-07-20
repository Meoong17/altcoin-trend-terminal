"""
Portfolio correlation layer — addresses the gap flagged in review: the
system scores every coin independently and never warns that the top-N
highest-scoring coins might be five different tickers on the same bet
(e.g. five L2s that all fall together).

Zero new API calls: every coin's analyze_coin() result already carries
closes_30d, collected for the sparkline. This module only reads what's
already in memory each cycle.

Output is a WARNING layer, not a filter or portfolio optimizer — it
doesn't touch trend_score or remove any coin from view. It's the same
epistemic posture as the rest of the system: surface the risk, let the
person decide, never decide for them.
"""

from itertools import combinations


def daily_returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def pearson(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-18 or vb <= 1e-18:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def correlation_matrix(coins_closes):
    """
    {symbol: closes_30d} -> {(sym_a, sym_b): correlation}. Pure, so the
    dashboard/self-test can exercise it without live data. Pairs with
    insufficient overlap are omitted rather than guessed.
    """
    rets = {s: daily_returns(c) for s, c in coins_closes.items() if c and len(c) >= 6}
    out = {}
    for a, b in combinations(sorted(rets), 2):
        r = pearson(rets[a], rets[b])
        if r is not None:
            out[(a, b)] = round(r, 3)
    return out


def concentration_warning(top_symbols, coins_closes, threshold=0.75):
    """
    Given the current top-N symbols by trend_score (caller decides N) and
    their closes, compute average pairwise correlation among them.
    Returns None if fewer than 3 symbols have enough history, else:
        {"avg_corr": float, "pairs": int, "flag": bool,
         "high_pairs": [(a,b,corr), ...] sorted desc, "symbols": [...]}
    flag=True when avg_corr >= threshold -- a concentration warning, not
    a verdict: high correlation among winners can be a real regime
    (e.g. genuine sector rotation), not necessarily a mistake.
    """
    closes = {s: coins_closes.get(s) for s in top_symbols}
    mat = correlation_matrix(closes)
    if len(mat) < 3:
        return None
    vals = list(mat.values())
    avg = sum(vals) / len(vals)
    high = sorted(([a, b, r] for (a, b), r in mat.items() if r >= threshold),
                  key=lambda x: -x[2])
    return {
        "avg_corr": round(avg, 3),
        "pairs": len(mat),
        "flag": avg >= threshold,
        "high_pairs": high[:5],
        "symbols": [s for s in top_symbols if closes.get(s)],
    }
