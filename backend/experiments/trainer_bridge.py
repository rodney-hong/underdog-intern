"""
trainer_bridge.py

Recovers per-row identity (player name, actual stat value, real book line) from
ml_trainer.build_training_data() WITHOUT reimplementing any of its feature
engineering and WITHOUT modifying ml_trainer.py.

Why this is needed
------------------
ml_trainer.build_training_data() is the single source of truth for which rows
exist and in what order. But the frame it returns only carries:

    14 feature columns + game_date + stat_type + hit

It deliberately drops player_name, the line value, and the actual stat value.
The experiment needs all three, and it needs them aligned to *exactly* the rows
build_training_data() produced — otherwise the test split would not match the
one train_model() used.

How it works
------------
We call ml_trainer.build_training_data() unchanged, under a trace hook that is
active only for that one code object. On each line event inside the function we
snapshot the loop locals (`stat_label`, `valid`, `line`, `col`); on the return
event we snapshot the fully-built source frame `df`. Overwriting per stat_label
means we end up holding the *final* value of `valid`/`line` for each stat.

Every recovered column is then cross-checked against the canonical frame
(row counts, game_date equality, hit == actual > line, line == avg10 - line_diff).
If ml_trainer's internals ever change shape, those assertions fail loudly rather
than silently producing a mismatched test set.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_trainer  # noqa: E402

# Identity columns this module adds on top of ml_trainer's own output.
IDENTITY_COLS = ["player_name", "actual", "real_line"]


def _capture_build_training_data() -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """Run ml_trainer.build_training_data() and capture its internal state."""
    target_code = ml_trainer.build_training_data.__code__
    captured: dict = {"df": None, "per_stat": {}}

    def local_tracer(frame, event, arg):
        loc = frame.f_locals
        if event == "line":
            stat_label = loc.get("stat_label")
            valid = loc.get("valid")
            if stat_label is not None and valid is not None:
                captured["per_stat"][stat_label] = {
                    "valid": valid,
                    "line": loc.get("line"),
                    "col": loc.get("col"),
                }
        elif event == "return":
            captured["df"] = loc.get("df")
        return local_tracer

    def global_tracer(frame, event, arg):
        # Cheap filter: line-trace only build_training_data's own frame.
        if frame.f_code is target_code:
            return local_tracer
        return None

    previous = sys.gettrace()
    sys.settrace(global_tracer)
    try:
        data = ml_trainer.build_training_data()
    finally:
        sys.settrace(previous)

    if captured["df"] is None:
        raise RuntimeError(
            "trainer_bridge: failed to capture ml_trainer.build_training_data()'s "
            "source frame. Has the function been refactored?"
        )
    if not captured["per_stat"]:
        raise RuntimeError(
            "trainer_bridge: failed to capture any per-stat masks from "
            "ml_trainer.build_training_data(). Has the loop been refactored?"
        )
    return data, captured["per_stat"], captured["df"]


def build_labelled_data_with_identity(stats: list[str]) -> pd.DataFrame:
    """
    Return ml_trainer.build_training_data()'s frame for `stats`, with
    player_name / actual / real_line attached, row-for-row.

    Raises if any cross-check against the canonical frame fails.
    """
    data, per_stat, source_df = _capture_build_training_data()
    if data.empty:
        raise RuntimeError("build_training_data() returned no rows.")

    frames: list[pd.DataFrame] = []
    for stat in stats:
        canonical = data[data["stat_type"] == stat].reset_index(drop=True)
        if canonical.empty:
            raise RuntimeError(f"No rows for stat_type={stat!r} in training data.")

        cap = per_stat.get(stat)
        if cap is None:
            raise RuntimeError(f"trainer_bridge: no captured mask for {stat!r}.")

        valid = cap["valid"]
        col = cap["col"]
        line = cap["line"]

        recovered = pd.DataFrame(
            {
                "player_name": source_df["player_name"][valid].values,
                "actual": source_df[col][valid].values,
                "real_line": np.asarray(line[valid].values, dtype="float64"),
                "_check_game_date": source_df["gameDateTimeEst"][valid].values,
            }
        )

        # --- cross-checks against the canonical frame -------------------------
        if len(recovered) != len(canonical):
            raise RuntimeError(
                f"trainer_bridge[{stat}]: recovered {len(recovered):,} rows but "
                f"build_training_data() produced {len(canonical):,}."
            )
        if not (recovered["_check_game_date"].values == canonical["game_date"].values).all():
            raise RuntimeError(f"trainer_bridge[{stat}]: game_date misalignment.")

        expected_hit = (recovered["actual"].values > recovered["real_line"].values).astype(int)
        if not (expected_hit == canonical["hit"].values).all():
            raise RuntimeError(
                f"trainer_bridge[{stat}]: recovered actual/line do not reproduce "
                f"the 'hit' label."
            )

        # line == avg_pts_last10 - line_diff, by construction in ml_trainer.
        implied_line = canonical["avg_pts_last10"].values - canonical["line_diff"].values
        if not np.allclose(implied_line, recovered["real_line"].values, atol=1e-6, equal_nan=True):
            raise RuntimeError(
                f"trainer_bridge[{stat}]: recovered real_line disagrees with "
                f"avg_pts_last10 - line_diff."
            )

        merged = canonical.copy()
        for c in IDENTITY_COLS:
            merged[c] = recovered[c].values
        frames.append(merged)

    return pd.concat(frames, ignore_index=True)


def test_split(stat_data: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Reproduce train_model()'s 80/20 temporal split for one stat type.

    Mirrors ml_trainer.train_model() exactly:
        sort_values("game_date") -> reset_index(drop=True) -> int(len * 0.8)

    Returns (sorted_full_frame, split_index). Test rows are iloc[split_index:].
    """
    ordered = stat_data.sort_values("game_date").reset_index(drop=True)
    split = int(len(ordered) * 0.8)
    return ordered, split
