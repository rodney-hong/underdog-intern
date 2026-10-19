# Labeling guide: check_set.csv

75 real items. Fill in the `label_*` columns by hand. The extraction model sees
exactly `source`, `published_at`, `previous_text` and `raw_text`, so judge from
those alone, not from what you know happened afterwards.

`stratum` is a regex-based selection tag. It can be wrong; ignore it when labeling.

| column | values | rule |
| --- | --- | --- |
| `label_player` | text | The player the item is about, "First Last" as normally written, suffix kept: `Jimmy Butler III`, `Michael Porter Jr.` |
| `label_team` | text | Full team name as it appears in the item: `Golden State Warriors` |
| `label_status` | `out` `doubtful` `questionable` `probable` `available` `unknown` | Availability for the player's **next game** (preseason games count). ESPN "Day-To-Day" is not a status: use the text ("won't play" = `out`). `unknown` if the item doesn't say. |
| `label_minutes_outlook` | `restricted` `normal` `unknown` | `restricted` only if minutes/workload/playing time will be limited in games. Limited *practice* is not `restricted`. Most items are `unknown`. |
| `label_is_change` | `true` `false` | Does this item report a change in availability vs `previous_text`? If `previous_text` is empty, judge from the text alone ("ruled out", "will return" = `true`; "remains out", a standing listing = `false`). |
| `label_source_time` | blank or ISO time | Leave **blank** to mean "same as `published_at`". Fill in only if the true source time differs. |
| `label_notes` | free text | Optional: ambiguity, two players mentioned, etc. |

## How it will be scored (fixed before labeling)

- Per field, exact match after normalization: case, surrounding whitespace, and
  accents for names (`Jokić` = `Jokic`). Names must otherwise match exactly.
- `player` is also scored after the existing fuzzy matcher
  (`difflib`, cutoff 0.85 as in `get_player_line`), reported separately.
- `is_change`: accuracy plus precision and recall for `true`, since `true` is
  what will trigger Phase 4 pulls. False positives cost credits.
- Every field is also reported per stratum and per source.
- Items you mark `skip` in `label_notes` are excluded and counted.
- No threshold is set here; you decide whether the numbers are good enough to enable.
