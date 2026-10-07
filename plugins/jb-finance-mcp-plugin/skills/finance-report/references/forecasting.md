# Forecasting

The persisted per-currency model and its four rules. Read this when a
prediction looks wrong, or before changing the rules.

## Forecasting


`scripts/forecast.py` keeps one small state file per currency —
`~/Documents/MyFinance/data/forecast_model_<currency>.json` — that
persists **across report runs**, not just within one. Every time
`generate_report.py` runs for a currency, it merges that run's per-category
monthly totals into the model's rolling history (last 6 months per
category), recomputes each category's prediction, and rewrites the file.
This is the "keep the logic local and update it looking at each new report"
behavior — the model accumulates, it doesn't restart from zero each time.

The rule per category (deliberately simple and auditable, not ML):

- Take the last up to 3 non-zero months on record.
- If the category had a non-zero value before but is 0 in the latest month
  → **`stopped`**, predict 0.
- Else if ≥2 non-zero points and their relative spread (population stdev /
  mean) is ≤5% → **`fixed`**, predict the latest observed value.
- Else if ≥2 non-zero points but more spread → **`average`**, predict the
  trailing mean.
- Else if exactly 1 point ever seen → **`single_observation`**, predict
  that value (low confidence — noted as such in the report).

Income gets the same treatment as one series (not split by category) for
the "predicted income" figure. The report's "Predicted — `<next month>`"
card shows every category's prediction next to its method, so the logic is
always visible, not a black box — if a prediction looks wrong, the reason
(which rule fired, on what history) is right there in the table, and the
underlying file is a plain JSON you can open directly.

**Extending/correcting it**: edit `FIXED_RELATIVE_STDEV` or
`MAX_HISTORY_MONTHS` in `forecast.py` if the 5%-variance or 6-month-window
defaults stop feeling right; both are single constants at the top of the
file. To reset a currency's model (e.g. after a life change that makes old
history misleading), delete `forecast_model_<currency>.json` — it gets
rebuilt from whatever's in `data/*.json` the next time the script runs for
that currency (though only categories from ranges you've actually
generated a report for; it doesn't backfill from raw history you haven't
fetched).
