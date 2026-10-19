"""
game_types.py
Single source of truth for which games count, used by ml_trainer.py (training
rows and every rolling feature) and by predictor.py / data_fetcher.py (the same
features at inference). Keep training and inference on these definitions.

Uses the dataset's own gameType field (player_stats.gameType, Kaggle
eoinamoore/historical-nba-data-and-player-box-scores). Labels seen up to v519:

  Regular Season       kept. Includes 2024-25 and 2025-26 Cup group/knockout games.
  NBA Emirates Cup     kept. 2023 Cup group stage, quarterfinals and semifinals
                       (66 games, all counted as regular season).
  Playoffs             kept; the only label with is_playoff = 1.
  Play-in Tournament   kept; is_playoff = 0 (not a playoff game).
  Emirates NBA Cup     EXCLUDED. The 2025 Cup championship (doesn't count in
                       standings or stats). The 2023/2024 finals are absent
                       from the dataset.
  Preseason, Pre Season  EXCLUDED.
  All-Star Game        EXCLUDED.

This is an allowlist: a label not listed here is excluded (and reported by
ml_trainer), so a new non-competitive game type can't leak in silently.
"""

COUNTED_GAME_TYPES = ("Regular Season", "NBA Emirates Cup", "Playoffs", "Play-in Tournament")
PLAYOFF_GAME_TYPES = ("Playoffs",)

# For SQL: "... WHERE gameType IN (<COUNTED_SQL>)"
COUNTED_SQL = ", ".join(f"'{t}'" for t in COUNTED_GAME_TYPES)


def is_counted(game_type) -> bool:
    return game_type in COUNTED_GAME_TYPES


def is_playoff(game_type) -> bool:
    return game_type in PLAYOFF_GAME_TYPES


# The schedule table has no gameType; its gameId encodes it: 00<T><YY><NNNNN>,
# T = 1 preseason, 2 regular season, 3 All-Star, 4 playoffs, 5 play-in, 6 Cup final.
SCHEDULE_PLAYOFF_TYPE_DIGIT = "4"


def schedule_game_is_playoff(game_id) -> bool:
    return f"{int(game_id):010d}"[2] == SCHEDULE_PLAYOFF_TYPE_DIGIT
