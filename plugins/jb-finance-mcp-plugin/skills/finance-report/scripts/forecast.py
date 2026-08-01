"""Simple, auditable next-month forecasting — no ML.

One rule per category, applied to a small rolling history that's persisted
to disk so it accumulates across monthly report runs instead of restarting
from scratch each time. The rule is deliberately simple enough to read off
the report: mean + population stdev of the last up to 3 non-zero
observations; low relative spread -> "fixed" (predict the latest value),
otherwise "average" (predict the trailing mean). A category with history
that goes to zero this month is called "stopped" (predict 0), and a
category seen for the first time is "single_observation" (low confidence).

State file: <out_dir>/data/forecast_model_<currency>.json — one per
currency, since categories/amounts across currencies are never combined.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

MAX_HISTORY_MONTHS = 6
FIXED_RELATIVE_STDEV = 0.05  # below this, treat a recurring cost as "fixed"


def _model_path(out_dir: Path, currency: str) -> Path:
    return out_dir / "data" / f"forecast_model_{currency}.json"


def load_model(out_dir: Path, currency: str) -> dict[str, Any]:
    path = _model_path(out_dir, currency)
    if path.exists():
        return json.loads(path.read_text())
    return {"currency": currency, "categories": {}, "income": {"history": {}}}


def _merge_series(history: dict[str, float], new_points: dict[str, float]) -> dict[str, float]:
    merged = {**history, **new_points}
    kept_months = sorted(merged)[-MAX_HISTORY_MONTHS:]
    return {m: merged[m] for m in kept_months}


def _predict(history: dict[str, float]) -> tuple[float, str]:
    months = sorted(history)
    if not months:
        return 0.0, "no_data"

    window = [history[m] for m in months[-3:]]
    latest = window[-1]
    prior = window[:-1]
    if latest == 0 and any(v > 0 for v in prior):
        return 0.0, "stopped"

    nonzero = [v for v in window if v > 0]
    if len(nonzero) >= 2:
        mean = statistics.mean(nonzero)
        stdev = statistics.pstdev(nonzero)
        if mean > 0 and stdev / mean <= FIXED_RELATIVE_STDEV:
            return round(latest, 2), "fixed"
        return round(mean, 2), "average"
    if len(nonzero) == 1:
        return round(nonzero[0], 2), "single_observation"
    return 0.0, "no_recent_data"


def update_and_predict(out_dir: Path, currency: str, monthly: dict[str, Any]) -> dict[str, Any]:
    """Merge this run's per-month category data into the persisted model,
    recompute every prediction, save, and return the updated model.

    `monthly` is monthly_summaries_by_currency(...)[currency] — a
    "YYYY-MM" -> {income, true_expense, net, category_breakdown} mapping.
    """
    model = load_model(out_dir, currency)
    categories = model.setdefault("categories", {})

    all_cats = {c for m in monthly.values() for c in m["category_breakdown"]}
    for cat in all_cats:
        entry = categories.setdefault(cat, {"history": {}})
        new_points = {
            month: data["category_breakdown"].get(cat, 0.0) for month, data in monthly.items()
        }
        entry["history"] = _merge_series(entry["history"], new_points)
        entry["predicted_next"], entry["method"] = _predict(entry["history"])

    income_entry = model.setdefault("income", {"history": {}})
    income_points = {month: data["income"] for month, data in monthly.items()}
    income_entry["history"] = _merge_series(income_entry["history"], income_points)
    income_entry["predicted_next"], income_entry["method"] = _predict(income_entry["history"])

    model["currency"] = currency
    _model_path(out_dir, currency).write_text(json.dumps(model, indent=2, sort_keys=True))
    return model


def predicted_expense_total(model: dict[str, Any]) -> float:
    return round(sum(c["predicted_next"] for c in model.get("categories", {}).values()), 2)
