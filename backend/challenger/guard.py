"""
guard.py  (challenger, Stage 1)

DATA RULE: Stage 1 never loaded, fitted on or evaluated on a 2025-26 or 2026-27 game.
Stage 2 (pre-registration commit a760c8c) moves the guard to 2026-07-01 so the one-shot
test can score 2025-26 with the frozen files; 2026-27 stays excluded. Every query in
backend/challenger/ is bounded by `gameDateTimeEst < DATA_END` (or by an earlier tip
time), and what was actually loaded is checked again with assert_dev_only().
"""

import numpy as np
import pandas as pd

DATA_END = "2026-07-01"                          # exclusive: nothing on/after this is read
FORBIDDEN_SEASONS = frozenset({"2026-27"})       # Stage 1 was "2025-07-01" / {2025-26, 2026-27}

# Regular season as game_types.py counts it: "Regular Season" (incl. the 2024-25 Cup
# group/knockout games) and "NBA Emirates Cup" (the 2023 Cup group stage, QF and SF,
# which counted as regular-season games). Playoffs, play-in, preseason, All-Star and the
# Cup final ("Emirates NBA Cup") are excluded.
REGULAR_TYPES = ("Regular Season", "NBA Emirates Cup")
REGULAR_SQL = ("gameType IN (" + ", ".join(f"'{t}'" for t in REGULAR_TYPES) + ") "
               "AND numMinutes > 0 AND personId IS NOT NULL")


def season_of(ts: pd.Series) -> pd.Series:
    """'2024-25' for any date from 2024-07-01 to 2025-06-30."""
    start = np.where(ts.dt.month >= 7, ts.dt.year, ts.dt.year - 1)
    return pd.Series([f"{s}-{(s + 1) % 100:02d}" for s in start], index=ts.index)


def season_of_one(t) -> str:
    return season_of(pd.Series([pd.Timestamp(t)])).iloc[0]


def assert_dev_only(t: pd.Series, season: pd.Series | None = None) -> None:
    """Raise if any timestamp is on/after DATA_END or falls in a forbidden season."""
    if len(t) == 0:
        return
    if t.max() >= pd.Timestamp(DATA_END):
        raise AssertionError(f"DATA RULE: a game on/after {DATA_END} was loaded ({t.max()})")
    seasons = set(season if season is not None else season_of(t))
    if seasons & FORBIDDEN_SEASONS:
        raise AssertionError(f"DATA RULE: forbidden season loaded: {sorted(seasons & FORBIDDEN_SEASONS)}")


def assert_dev_tip(tip) -> None:
    """Raise unless a single target game is allowed in Stage 1."""
    tip = pd.Timestamp(tip)
    if tip >= pd.Timestamp(DATA_END) or season_of_one(tip) in FORBIDDEN_SEASONS:
        raise ValueError(f"DATA RULE: game at {tip} is in a reserved season; Stage 1 does not "
                         f"predict games on/after {DATA_END}")
