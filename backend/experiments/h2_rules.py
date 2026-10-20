"""
h2_rules.py — H2 market fair probability (preregistration.md, "H2 clarification").

Pure functions, no I/O, not wired into anything. Rules, fixed before any 2026-27
result existed:

  a) Per book (fanduel, draftkings only): if the book's player_points line equals
     ud_point, use that market; otherwise use its player_points_alternate outcomes at
     ud_point.
  b) If a side has more than one price at that point in the chosen market, convert
     each to implied probability and average them.
  c) The book qualifies only if both sides are priced at that point (DraftKings'
     Over-only alternate rungs do not qualify).
  d) Devig each qualifying book with Shin's method; fair P(over) = the average across
     qualifying books.
  e) If no book qualifies, return None. Such rows are excluded from H2 and counted.

Implementation notes (choices the rules leave open, stated here):
  - rows: mappings with keys bookmaker, market_key, side, point, price (American odds),
    e.g. line_snapshots rows for one player in one pull. Pass one player's rows;
    other books and markets in them are ignored.
  - "The book's player_points line equals ud_point" is read as: the book has at least
    one player_points outcome at ud_point. Points compare with tolerance 1e-9.
  - Shin's method for the two outcomes: implied probabilities pi_i (sum beta),
    p_i(z) = (sqrt(z^2 + 4(1-z) pi_i^2 / beta) - z) / (2(1-z)), with z in [0, 1)
    solved by bisection so that sum p_i = 1. If beta <= 1 (no overround) Shin's z is
    0 and the probabilities are pi_i / beta; this case is outside rules a)-e) and is
    flagged in the detail output.
"""

from __future__ import annotations

import math

BOOKS = ("fanduel", "draftkings")
MAIN = "player_points"
ALT = "player_points_alternate"
_TOL = 1e-9


def implied_prob(american: float) -> float:
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def shin_devig(pi: list[float]) -> tuple[list[float], float, bool]:
    """Shin probabilities for implied probabilities pi. Returns (p, z, no_overround)."""
    beta = sum(pi)
    if beta <= 1.0:
        return [x / beta for x in pi], 0.0, True

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * x * x / beta) - z) / (2.0 * (1.0 - z))
                for x in pi]

    lo, hi = 0.0, 1.0 - 1e-12          # sum(probs(0)) = sqrt(beta) > 1; sum -> < 1 as z -> 1
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(probs(mid)) > 1.0:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2.0
    return probs(z), z, False


def _at(rows, book: str, market: str, point: float) -> list:
    return [r for r in rows if r["bookmaker"] == book and r["market_key"] == market
            and r["point"] is not None and abs(float(r["point"]) - point) < _TOL]


def book_fair_over(rows, book: str, ud_point: float) -> dict:
    """One book's result under rules a)-d): qualifies, market used, prices, Shin P(over)."""
    main_at = _at(rows, book, MAIN, ud_point)
    market = MAIN if main_at else ALT                                    # rule a
    at = main_at if main_at else _at(rows, book, ALT, ud_point)
    over = [r["price"] for r in at if r["side"] == "Over" and r["price"] is not None]
    under = [r["price"] for r in at if r["side"] == "Under" and r["price"] is not None]
    out = {"book": book, "market": market, "over_prices": over, "under_prices": under,
           "qualifies": bool(over and under)}                            # rule c
    if not out["qualifies"]:
        return out
    pi_over = sum(implied_prob(p) for p in over) / len(over)             # rule b
    pi_under = sum(implied_prob(p) for p in under) / len(under)
    (p_over, _), z, no_overround = shin_devig([pi_over, pi_under])       # rule d
    out.update({"implied_over": pi_over, "implied_under": pi_under,
                "overround": pi_over + pi_under - 1.0, "shin_z": z,
                "no_overround": no_overround, "fair_over": p_over})
    return out


def h2_fair_prob_detail(rows, ud_point: float) -> dict:
    per_book = [book_fair_over(rows, b, ud_point) for b in BOOKS]
    fair = [b["fair_over"] for b in per_book if b["qualifies"]]
    return {"fair_over": sum(fair) / len(fair) if fair else None,            # rules d, e
            "n_books": len(fair), "books": per_book}


def h2_fair_prob(rows, ud_point: float) -> float | None:
    """Fair P(over) at ud_point averaged over qualifying books, or None (rule e)."""
    return h2_fair_prob_detail(rows, ud_point)["fair_over"]
