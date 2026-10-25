"""report.py  (challenger, Stage 1): the results .md from stage1.py's output dict."""

MODELS = ["challenger", "b_pooled_hgb", "a_last10"]
LINE_TYPES = ["median", "median-2", "median+2", "rung below", "rung above"]


def _bs(c):
    return "—" if not c.get("n") else f"{c['brier']:.4f} / {c['ece']:.4f}"


def _ci(b):
    return f"{b['mean']:+.4f} [{b['ci95'][0]:+.4f}, {b['ci95'][1]:+.4f}]"


def write_md(r: dict, path: str) -> None:
    """Also usable on a saved results .json: write_md(json.load(f), path)."""
    m = r["meta"]
    L = ["# Challenger Stage 1: development results on 2024-25", "",
         f"Generated {m['generated_utc']}. Regular season (incl. 2023 Cup games), played rows "
         f"(minutes > 0). No game on/after {m['data_end_exclusive']} loaded (max date loaded "
         f"{m['max_date_loaded']}; {m['rows_loaded']:,} rows, seasons {m['seasons_loaded'][0]}.."
         f"{m['seasons_loaded'][1]}, used for history only before {m['train_first']}). No backfilled lines.", "",
         f"- Tuning: fit {m['tune'][0]}..{m['tune'][1]}, validate {m['tune'][2]}.",
         f"- Dev model: fit {m['dev'][0]}..{m['dev'][1]}, evaluate {m['dev'][2]} "
         f"({m['rows']['eval_2024_25']:,} rows per stat, {m['eval_games']:,} games; rows need >= "
         f"{m['min_prior']} earlier games).",
         f"- NB dispersion (all three models): MLE on out-of-fold training predictions, folds = "
         f"contiguous season blocks " + ", ".join(f"{str(b[0])}..{str(b[-1])}" for b in m["oof_blocks"])
         + f" (baseline (a) has no fitted parameters, so its "
         f"in-sample fit is out of fold).",
         "- Rows: " + ", ".join(f"{k} {v:,}" for k, v in m["rows"].items()) + ".", ""]

    L += ["## Summary: NB log-likelihood per game and MAE, 2024-25", "",
          "| stat | challenger LL | (b) pooled HGB LL | (a) last-10 LL | challenger − (b) [95% CI] | "
          "challenger MAE | (b) MAE | (a) MAE |", "|---|---|---|---|---|---|---|---|"]
    for s, x in r["results_2024_25"].items():
        a = x["all"]
        L.append(f"| {s} | {a['challenger']['ll']:.4f} | {a['b_pooled_hgb']['ll']:.4f} | {a['a_last10']['ll']:.4f} | "
                 f"{_ci(x['boot_ll_challenger_minus_b'])} | {a['challenger']['mae']:.4f} | "
                 f"{a['b_pooled_hgb']['mae']:.4f} | {a['a_last10']['mae']:.4f} |")
    p = r["pooled_2024_25"]
    L += ["", f"Pooled over the four stats (each player-game-stat one row): challenger − (b) "
              f"{_ci(p['challenger_minus_b'])}; challenger − (a) {_ci(p['challenger_minus_a'])} nats per row "
              f"(game-clustered bootstrap, {p['challenger_minus_b']['games']:,} games, 2,000 resamples).", ""]

    L += ["## Calibration of P(stat > L), 2024-25", "",
          "Lines per row: the player's median of his last 20 earlier games (min 5), median ± 2, and the "
          "nearest x4.5/x9.5 rungs below (≤) and above (>) the median. Lines below 0 are dropped. "
          "Over = stat > L (a push on an integer line counts as not over). ECE over 10 equal-width bins.", "",
          "| stat | line | n | over rate | challenger Brier / ECE | (b) Brier / ECE | (a) Brier / ECE |",
          "|---|---|---|---|---|---|---|"]
    for s, x in r["results_2024_25"].items():
        for lt in LINE_TYPES:
            c = {k: x["all"][k]["lines"][lt] for k in MODELS}
            n = c["challenger"].get("n", 0)
            L.append(f"| {s} | {lt} | {n:,} | " + (f"{c['challenger']['base_rate']:.3f}" if n else "—") + " | "
                     + " | ".join(_bs(c[k]) for k in MODELS) + " |")
    L += ["", "### Reliability, median line (mean predicted P / observed over rate / n)", ""]
    for s, x in r["results_2024_25"].items():
        L += [f"**{s}**", "", "| bin | challenger | (b) pooled HGB | (a) last-10 |", "|---|---|---|---|"]
        bins = sorted({b["bin"] for k in MODELS for b in x["all"][k]["lines"]["median"].get("reliability", [])})
        for bn in bins:
            cells = []
            for k in MODELS:
                e = [b for b in x["all"][k]["lines"]["median"]["reliability"] if b["bin"] == bn]
                cells.append(f"{e[0]['mean_p']:.3f} / {e[0]['observed']:.3f} / {e[0]['n']:,}" if e else "—")
            L.append(f"| {bn} | " + " | ".join(cells) + " |")
        L.append("")

    L += ["## By experience (prior NBA seasons), 2024-25", "",
          "| stat | bucket | rows | LL challenger / (b) / (a) | challenger − (b) [95% CI] | MAE challenger / (b) / (a) | "
          "Brier @median challenger / (b) / (a) |", "|---|---|---|---|---|---|---|"]
    for s, x in r["results_2024_25"].items():
        for bk, e in x["by_experience"].items():
            L.append(f"| {s} | {bk} | {e['challenger']['rows']:,} | "
                     + " / ".join(f"{e[k]['ll']:.4f}" for k in MODELS) + f" | {_ci(e['boot_ll_challenger_minus_b'])} | "
                     + " / ".join(f"{e[k]['mae']:.4f}" for k in MODELS) + " | "
                     + " / ".join(_bs(e[k]["lines"]["median"]).split(" / ")[0] for k in MODELS) + " |")
    L.append("")

    d = r["design"]
    L += ["## Chosen design", "",
          f"- Minutes model (shared by the four stats): HGB, squared error, {d['minutes_params']}; "
          f"features {d['minutes_feats']}."]
    for s in d["rate_feats"]:
        L.append(f"- Rate model {s}: HGB, Poisson loss on stat/minutes weighted by minutes, {d['rate_params'][s]}; "
                 f"features {d['rate_feats'][s]}.")
    L.append("- NB alpha (dev / frozen): " + ", ".join(
        f"{s} {r['alphas']['challenger_dev'][s]:.4f} / {r['alphas']['challenger_frozen'][s]:.4f}"
        for s in d["rate_feats"]) + ".")
    L.append("- Baseline (b) alpha (dev / frozen): " + ", ".join(
        f"{s} {r['alphas']['baseline_b_dev'][s]:.4f} / {r['alphas']['baseline_b_frozen'][s]:.4f}"
        for s in d["rate_feats"]) + f"; (a) " + ", ".join(
        f"{s} {v:.4f}" for s, v in r["alphas"]["baseline_a"].items()) + ".")
    L += ["", "## Every tuning choice (fit 2012-13..2022-23, validate 2023-24)", "",
          "Minutes grid (validation MSE, minutes², lower is better):", "",
          "| max_iter | leaves | min leaf | val MSE |", "|---|---|---|---|"]
    t = r["tuning"]
    for g in t["minutes_grid"]:
        q = g["params"]
        L.append(f"| {q['max_iter']} | {q['max_leaf_nodes']} | {q['min_samples_leaf']} | {g['val_mse']} |")
    L += ["", "Minutes feature-group changes (each vs the tuned base; accepted if MSE improves by > 0.1%):", "",
          "| change | val MSE | base | accepted |", "|---|---|---|---|"]
    for c in t["minutes_feature_changes"]:
        L.append(f"| {c['change']} | {c['val_mse']} | {c['vs_base']} | {c['accepted']} |")
    for s in d["rate_feats"]:
        L += ["", f"Rate {s}: grid (validation NB log-lik per row, alpha profiled on validation; tuning only):", "",
              "| max_iter | leaves | min leaf | val LL | alpha |", "|---|---|---|---|---|"]
        for g in t[s]["grid"]:
            q = g["params"]
            L.append(f"| {q['max_iter']} | {q['max_leaf_nodes']} | {q['min_samples_leaf']} | {g['val_ll']} | {g['alpha']} |")
        L += ["", f"Rate {s}: feature-group changes (accepted if LL improves by > 0.0005 per row):", "",
              "| change | val LL | base | accepted |", "|---|---|---|---|"]
        for c in t[s]["feature_changes"]:
            L.append(f"| {c['change']} | {c['val_ll']} | {c['vs_base']} | {c['accepted']} |")
        L.append(f"\nFinal validation LL: {t[s]['val_ll_final']}")

    pa = r["parity"]
    L += ["", "## Train/inference parity", "",
          f"challenger.inference.features_before_tip (SQL on rows before tip, one placeholder row) vs the "
          f"training frame on {pa['rows_checked']} rows of 2023-24/2024-25 (a quarter drawn from rookies and "
          f"first games back from an absence), {pa['features_per_row']} features each: "
          f"**{pa['mismatches']} mismatches** (exact equality, NaN = NaN).",
          f"challenger_predict smoke test: person {r['smoke']['personId']}, game {r['smoke']['gameId']}, Points > "
          f"{r['smoke']['line']}: inference {r['smoke']['p_over_inference']:.6f}, batch "
          f"{r['smoke']['p_over_batch']:.6f}, equal = {r['smoke']['equal']}.", "",
          "## Model files (not committed)", ""]
    for k, v in r["models"].items():
        L.append(f"- {k}: backend/{v['path'].replace(chr(92), '/')}  file SHA-256 `{v['sha256']}`"
                 + (f", content fingerprint `{v['fingerprint']}`" if v.get("fingerprint") else ""))
    L.append("")
    L.append("The file SHA-256 pins the saved artifact. A refit with the same code and data gives identical "
             "trees, bin thresholds and alphas (checked for the frozen challenger) but different pickle bytes, "
             "so model.fingerprint() hashes the fitted content for refit checks.")
    L += ["", "## CPU time", "", "| phase | wall s | CPU s (all threads) |", "|---|---|---|"]
    for k, v in m["timings"].items():
        L.append(f"| {k} | {v['wall_s']} | {v['cpu_s']} |")
    L.append(f"\n{m['cpu_logical_cores']} logical cores.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
