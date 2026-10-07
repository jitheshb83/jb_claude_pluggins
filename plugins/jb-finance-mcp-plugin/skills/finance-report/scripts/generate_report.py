"""Generate a local personal-finance report (JSON data + HTML) for one or
more linked bank institutions, and cache both under ~/Documents/MyFinance/.

Fetches transactions directly via stored Enable Banking credentials — the
same path `check_bank_status.py --live` and the `bank.*` MCP tools use — so
it works standalone, without an MCP client session. Read-only: never touches
onboarding, consent, or payment-initiation code paths.

Run from this plugin's root directory (two levels above this file):
    uv run python skills/finance-report/scripts/generate_report.py \\
        --from 2026-07-01 --to 2026-07-31

Options:
    --institutions dnb,nordea   default: every institution with a valid,
                                 non-expired stored session
    --currency NOK               which currency's accounts to chart in the
                                 HTML report (default NOK); other currencies
                                 still get fetched, cached, and a footnote
                                 stat line, just not full charts — mixing
                                 currencies into one number is never done
    --out-dir PATH                default ~/Documents/MyFinance
    --refresh                    ignore existing per-month cache files
                                 covering this range and re-fetch all of
                                 them live
    --skip-loans                 skip the Loan Tracker sheet lookup
    --refresh-loans               ignore the 1-day loan sheet cache and
                                 re-fetch it live
    --loan-sheet-account          Google account for the Loan Tracker sheet
                                 (default: whatever's already cached)
    --loan-sheet-id                Drive file id for the Loan Tracker sheet
                                 (default: whatever's already cached)

See SKILL.md in this directory for the full write-up (categorization rules,
cache-reuse policy, known gotchas).
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from cards import (  # noqa: E402
    load_person_recipients,
    load_statements,
    statement_pdfs_present,
    write_analysis,
)
from categories import (  # noqa: E402
    CARD_CATEGORY_LABELS,
    categorize,
    categorize_card,
    dedupe_transactions,
)
from forecast import predicted_expense_total, update_and_predict  # noqa: E402
from loans import fetch_loan_details  # noqa: E402

from jb_gateway_mcp.adapters import enable_banking
from jb_gateway_mcp.cli.onboard_bank import _INSTITUTION_COUNTRY
from jb_gateway_mcp.credentials_bank import (
    BankCredentialNotFoundError,
    BankCredentialStore,
    NeedsReconsentError,
)

OUT_DIR_DEFAULT = Path.home() / "Documents" / "MyFinance"
CATEGORY_LABELS = {
    "mortgage": "Mortgage",
    "credit_card": "Credit card",
    "car_finance": "Car finance",
    "international_transfer": "Int'l transfer",
    "school_fees": "School fees",
    "insurance": "Insurance",
    "housing_fee": "Housing fee",
    "electricity": "Electricity",
    "telecom": "Telecom",
    "toll": "Toll/ferry",
    "parking": "Parking",
    "municipal_charge": "Municipal charge",
    "bank_fee": "Bank fee",
    "uncategorized": "Uncategorized",
    "salary": "Salary",
    "pension_benefit": "Pension/benefit",
    "dividend": "Dividend",
    "income_other": "Other income",
    "credit_card_settlement": "Credit card settlement",
}
CATEGORY_LABELS.update(CARD_CATEGORY_LABELS)

# The card's own purchases are the real spend; the monthly lump bill payment
# to Entercard is only the settlement of that debt. Counting both would
# double-count the same money, so once a statement has been decomposed the
# lump is reclassified here and excluded from income/expense exactly the way
# a transfer between the user's own accounts already is.
CARD_SETTLEMENT_CATEGORY = "credit_card_settlement"
NON_SPEND_CATEGORIES = {"internal_transfer", CARD_SETTLEMENT_CATEGORY}

# Maps a forecast/expense category to the Loan Tracker sheet's `loan_type`
# value, so the Predicted card can cross-reference a category's rule-based
# prediction against the sheet's stated next payment — informational only,
# never used to override predicted_next itself.
LOAN_TYPE_BY_CATEGORY = {"mortgage": "mortgage", "car_finance": "car"}
CATEGORY_BY_LOAN_TYPE = {v: k for k, v in LOAN_TYPE_BY_CATEGORY.items()}

# A loan row whose last_updated is older than this (or missing) is treated
# as stale: monthly_payment/next_payment_date fall back to a value derived
# from actual transaction history instead of trusting an out-of-date sheet
# entry. Fields with no honest transaction-derived equivalent
# (outstanding_balance, interest_rate_pct, original_amount, maturity_date)
# are never guessed — they're left as whatever the sheet says, stale or not.
_LOAN_STALE_DAYS = 30

# Preference order for which balance line to report as "balance in hand" —
# Enable Banking returns several types per account; these usually agree
# exactly for a simple checking account, but prefer the most current one.
_BALANCE_TYPE_PREFERENCE = ["ITAV", "XPCD", "OPBD", "ITBD", "OTHR"]


# ---------------------------------------------------------------- fetching --


def _month_windows(date_from: str, date_to: str) -> list[tuple[str, str]]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = min(date(cursor.year, cursor.month, last_day), end)
        windows.append((cursor.isoformat(), month_end.isoformat()))
        cursor = date.fromordinal(month_end.toordinal() + 1)
    return windows


def fetch_deduped(
    store: BankCredentialStore, institution: str, account_uid: str, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """list_transactions_detailed, with a monthly-chunk fallback + dedupe.

    Enable Banking has occasionally 422'd on continuation pages for a wide
    date range on this account set (cause unconfirmed, upstream). Chunking
    by calendar month works around it but reintroduces duplicate boundary
    rows (date_to is inclusive on both adjacent windows) — dedupe_transactions
    cleans that up either way, so it's applied unconditionally.
    """
    try:
        raw = enable_banking.list_transactions_detailed(
            store, institution, account_uid, date_from, date_to
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422:
            raise  # only 422 (pagination quirk) is worth retrying by chunking; a
            # 429 rate-limit would just get worse from more requests, not better
        raw = []
        for w_from, w_to in _month_windows(date_from, date_to):
            raw.extend(
                enable_banking.list_transactions_detailed(
                    store, institution, account_uid, w_from, w_to
                )
            )
    return dedupe_transactions(raw)


def resolve_institutions(store: BankCredentialStore, requested: list[str] | None) -> list[str]:
    candidates = requested or sorted(_INSTITUTION_COUNTRY)
    resolved = []
    for institution in candidates:
        try:
            store.get_valid_session(institution)
        except (BankCredentialNotFoundError, NeedsReconsentError) as exc:
            print(f"  [skip] {institution}: {exc}", file=sys.stderr)
            continue
        resolved.append(institution)
    return resolved


BALANCE_CACHE_TTL_MINUTES = 60


def _balance_cache_path(data_dir: Path) -> Path:
    return data_dir / "balance_cache.json"


def _load_balance_cache(data_dir: Path) -> dict[str, Any]:
    path = _balance_cache_path(data_dir)
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _cached_balance_fresh_enough(entry: dict[str, Any]) -> bool:
    fetched_at = datetime.fromisoformat(entry["fetched_at"])
    age_minutes = (datetime.now(UTC) - fetched_at).total_seconds() / 60
    return age_minutes < BALANCE_CACHE_TTL_MINUTES


def fetch_balance_total(
    store: BankCredentialStore,
    dataset: dict[str, Any],
    currency: str,
    data_dir: Path,
    refresh: bool = False,
) -> tuple[float, list[str]]:
    """Balance lookup for every account of `currency` across the
    institutions in `dataset` — live, but reused from a short-lived local
    cache (BALANCE_CACHE_TTL_MINUTES) per account rather than re-fetched on
    every single run. Balances don't need second-by-second freshness for a
    personal "balance in hand" figure, and Enable Banking enforces a daily
    (not short-term) per-consent access cap, so an avoidable repeat call
    can burn the whole day's quota for that institution — pass
    `refresh=True` (from --refresh) to force a live re-fetch regardless of
    cache age. One account failing (expired session, rate limit, ...)
    doesn't block the rest — it's reported as a warning and skipped."""
    cache = _load_balance_cache(data_dir)
    total = 0.0
    warnings: list[str] = []
    for institution, accounts in dataset["accounts"].items():
        for account in accounts:
            if account["currency"] != currency:
                continue
            cache_key = f"{institution}:{account['uid']}"
            cached = cache.get(cache_key)
            if cached and not refresh and _cached_balance_fresh_enough(cached):
                print(f"Using cached balance: {cache_key}")
                balances = cached["balances"]
            else:
                try:
                    balances = enable_banking.get_balance(store, institution, account["uid"])
                except Exception as exc:  # noqa: BLE001 - report, don't sink the rest
                    warnings.append(
                        f"{institution}/{account['name']}: {type(exc).__name__}: {exc}"
                    )
                    continue
                cache[cache_key] = {
                    "balances": balances,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            chosen = None
            for btype in _BALANCE_TYPE_PREFERENCE:
                chosen = next((b for b in balances if b["type"] == btype), None)
                if chosen:
                    break
            if chosen is None and balances:
                chosen = balances[0]
            if chosen and chosen.get("amount") is not None:
                total += float(chosen["amount"])
    _balance_cache_path(data_dir).write_text(json.dumps(cache, indent=2))
    return round(total, 2), warnings


# ---------------------------------------------------------------- compute --


def month_key(date_str: str) -> str:
    return date_str[:7]


def next_month_key(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    mon += 1
    if mon > 12:
        mon, year = 1, year + 1
    return f"{year:04d}-{mon:02d}"


def _parse_ddmmyyyy(raw: str | None) -> date | None:
    if not raw or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _add_one_month(d: date) -> date:
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _txn_sort_key(txn: dict[str, Any]) -> str:
    """Sort key tolerant of a missing booking date. Some DNB entries arrive
    with `date: null` (seen on a pending Pensjon/Trygd credit); a bare
    `txn["date"]` comparison raises TypeError as soon as one shares a list
    with a dated entry."""
    return txn.get("date") or ""


def _with_fallback_dates(
    txns: list[dict[str, Any]], window_from: str
) -> list[dict[str, Any]]:
    """Anchor any dateless transaction to its fetch window's start date.

    Each part passed to `_merge_datasets` spans a single calendar month, so
    the window's own start is guaranteed to be the right *month* for such an
    entry even though its day is unknown — which is what the month bucketing
    in `monthly_summaries_by_currency` needs. Falling back to the merged
    range's start instead would push an August entry into July. The entry is
    tagged `date_missing` so anything that needs a real observed date (see
    `_last_transaction_date`) can skip it rather than trust the 1st."""
    filled: list[dict[str, Any]] = []
    for txn in txns:
        if txn.get("date") is None:
            txn = {**txn, "date": window_from, "date_missing": True}
        filled.append(txn)
    return filled


def _last_transaction_date(dataset: dict[str, Any], category: str) -> date | None:
    found = [
        date.fromisoformat(txn["date"])
        for txns in dataset["transactions"].values()
        for txn in txns
        if txn["category"] == category and not txn.get("date_missing")
    ]
    return max(found) if found else None


def _enrich_stale_loan_row(
    row: dict[str, Any],
    dataset: dict[str, Any],
    forecast_model: dict[str, Any] | None,
    today: date,
) -> dict[str, Any]:
    """Fall back to transaction-derived values for monthly_payment/
    next_payment_date when the sheet row is stale or those fields are
    blank. Never touches outstanding_balance/interest_rate_pct/
    original_amount/maturity_date — those have no honest transaction-derived
    equivalent, so a stale/blank value there is left as-is, not guessed."""
    row = dict(row)
    last_updated = _parse_ddmmyyyy(row.get("last_updated"))
    stale = last_updated is None or (today - last_updated).days > _LOAN_STALE_DAYS
    category = CATEGORY_BY_LOAN_TYPE.get((row.get("loan_type") or "").strip().lower())

    if (stale or row.get("monthly_payment") is None) and forecast_model and category:
        entry = forecast_model.get("categories", {}).get(category)
        if entry is not None:
            row["monthly_payment"] = entry["predicted_next"]
            row["monthly_payment_estimated"] = True

    if (stale or not row.get("next_payment_date")) and category:
        last_txn_date = _last_transaction_date(dataset, category)
        if last_txn_date is not None:
            row["next_payment_date"] = _add_one_month(last_txn_date).strftime("%d/%m/%Y")
            row["next_payment_date_estimated"] = True

    return row


def build_dataset(
    store: BankCredentialStore, institutions: list[str], date_from: str, date_to: str
) -> dict[str, Any]:
    accounts_by_institution: dict[str, list[dict[str, Any]]] = {}
    for institution in institutions:
        accounts_by_institution[institution] = enable_banking.list_accounts(store, institution)

    own_names = {
        acc["name"].strip().lower()
        for accs in accounts_by_institution.values()
        for acc in accs
        if acc.get("name")
    }

    transactions_by_institution: dict[str, list[dict[str, Any]]] = {}
    for institution, accounts in accounts_by_institution.items():
        txns: list[dict[str, Any]] = []
        for account in accounts:
            fetched = fetch_deduped(store, institution, account["uid"], date_from, date_to)
            for txn in fetched:
                amount = float(txn["amount"])
                category = categorize(
                    txn["direction"],
                    txn.get("counterparty_name"),
                    txn.get("description"),
                    own_names,
                )
                txns.append(
                    {
                        "account_uid": account["uid"],
                        "account_name": account["name"],
                        "currency": account["currency"],
                        "date": txn["date"],
                        "amount": amount,
                        "direction": txn["direction"],
                        "counterparty_name": txn.get("counterparty_name"),
                        "description": txn.get("description"),
                        "category": category,
                    }
                )
        transactions_by_institution[institution] = sorted(txns, key=_txn_sort_key)

    return {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "source": "Enable Banking via jb_gateway_mcp (direct adapter call, not MCP protocol)",
        "period": {"from": date_from, "to": date_to},
        "institutions": institutions,
        "accounts": accounts_by_institution,
        "transactions": transactions_by_institution,
    }


def _full_month_cache_path(data_dir: Path, window_from: str, window_to: str) -> Path | None:
    """Cache path for a `_month_windows` window, or None if it isn't a full
    calendar month (a partial month's data must never be cached under the
    same key as the full month's — it would silently corrupt future lookups
    for that month)."""
    start = date.fromisoformat(window_from)
    end = date.fromisoformat(window_to)
    last_day = calendar.monthrange(start.year, start.month)[1]
    if start.day != 1 or end.day != last_day:
        return None
    return data_dir / f"{start.strftime('%Y-%m')}-transactions.json"


def _merge_datasets(
    parts: list[dict[str, Any]], date_from: str, date_to: str
) -> dict[str, Any]:
    """Combine one dataset per calendar-month window (each already deduped
    and sorted internally) into a single dataset spanning the full
    originally-requested range."""
    accounts: dict[str, list[dict[str, Any]]] = {}
    transactions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    institutions: list[str] = []
    for part in parts:
        for institution in part["institutions"]:
            if institution not in institutions:
                institutions.append(institution)
        accounts.update(part["accounts"])
        for institution, txns in part["transactions"].items():
            transactions[institution].extend(
                _with_fallback_dates(txns, part["period"]["from"])
            )
    for institution in transactions:
        transactions[institution].sort(key=_txn_sort_key)

    return {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "source": "Enable Banking via jb_gateway_mcp (direct adapter call, not MCP protocol)",
        "period": {"from": date_from, "to": date_to},
        "institutions": institutions,
        "accounts": accounts,
        "transactions": dict(transactions),
    }


def _months_in_range(date_from: str, date_to: str) -> list[str]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    months, cursor = [], start.replace(day=1)
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = _add_one_month(cursor.replace(day=1))
    return months


def apply_card_decomposition(
    dataset: dict[str, Any],
    statements: list[dict[str, Any]],
    date_from: str,
    date_to: str,
    currency: str,
    person_recipients: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Replace the bank's lump credit-card bill payments with the card's own
    itemized purchases, attributed by purchase date (`Bruksdato`).

    Two deliberate choices, both documented in data/cards/README.md:

    * **Decompose, don't add.** The lump is reclassified to
      CARD_SETTLEMENT_CATEGORY (non-spend) rather than deleted, so the cash
      movement stays auditable in the data while the purchases it settles
      are what actually counts as expense. Adding both would double-count.
    * **Purchase date, not bill date.** A 20 Aug purchase is August spend
      even though the bill cleared 15 Sep. A month's card spend therefore
      will *not* equal that month's bill payment; the returned
      reconciliation rows exist to make that difference explicit rather
      than surprising.

    Returns None when there is nothing to decompose for this currency."""
    statements = [st for st in statements if st["currency"] == currency]
    if not statements:
        return None

    card_txns: list[dict[str, Any]] = []
    for statement in statements:
        for line in statement["transactions"]:
            if line["section"] != "purchases":
                continue
            if not date_from <= line["purchase_date"] <= date_to:
                continue  # attributed to a month outside this report
            txn = {
                "account_uid": f"card-{line['card_last4']}",
                "account_name": line["cardholder"],
                "currency": statement["currency"],
                "date": line["purchase_date"],
                "amount": line["amount"],
                "direction": line["direction"],
                "counterparty_name": line["description"],
                "description": " ".join(
                    part for part in (line["description"], line["place"]) if part
                ),
                "category": categorize_card(
                    line["description"], line["place"], person_recipients
                ),
                "booking_date": line["booking_date"],
                "source_statement": statement["source_pdf"],
            }
            if line.get("fx"):
                txn["fx"] = line["fx"]
            card_txns.append(txn)

    covered_months: set[str] = set()
    for statement in statements:
        period = statement["statement_period"]
        covered_months.update(_months_in_range(period["from"], period["to"]))

    # Reclassify in-range lump bill payments, and match each to the statement
    # it settles by total_due so the report can show the chain rather than
    # asserting it.
    #
    # A lump is only dropped when its own month has itemized purchases to
    # replace it. Without that gate the automated monthly run silently
    # understated expenses by an entire card bill: on the 1st of the month
    # the statement covering the month just ended often is not in
    # data/cards/ yet, so the lump was reclassified as non-spend and nothing
    # took its place. Keeping the lump degrades back to the pre-decomposition
    # figure, which is right in aggregate, instead of losing the money.
    settlements: list[dict[str, Any]] = []
    kept_lumps: list[dict[str, Any]] = []
    for rows in dataset["transactions"].values():
        for row in rows:
            if row["category"] != "credit_card" or row["currency"] != currency:
                continue
            if month_key(row["date"]) not in covered_months:
                kept_lumps.append({"paid_on": row["date"], "amount": row["amount"]})
                continue
            row["category"] = CARD_SETTLEMENT_CATEGORY
            matched = next(
                (
                    st
                    for st in statements
                    if abs(st["totals"].get("total_due", -1) - row["amount"]) <= 0.02
                ),
                None,
            )
            if matched is not None:
                row["settles_statement"] = matched["source_pdf"]
            settlements.append(
                {
                    "paid_on": row["date"],
                    "amount": row["amount"],
                    "statement": None if matched is None else matched["source_pdf"],
                    "statement_period": None
                    if matched is None
                    else matched["statement_period"],
                }
            )

    range_months = _months_in_range(date_from, date_to)
    uncovered = [m for m in range_months if m not in covered_months]

    # A purchase late in month M is often booked in M+1 and therefore prints
    # on the *next* statement. The most recent covered month is only
    # complete once that following statement exists, so flag it instead of
    # presenting a possibly-short figure as final.
    latest_period_end = max(st["statement_period"]["to"] for st in statements)
    spillover_pending = [
        m for m in range_months if m == month_key(latest_period_end) and m not in uncovered
    ]

    spend_by_month: dict[str, float] = defaultdict(float)
    for txn in card_txns:
        if txn["direction"] == "DBIT" and txn["category"] not in NON_SPEND_CATEGORIES:
            spend_by_month[month_key(txn["date"])] += txn["amount"]

    card_name = statements[0]["card"]
    if card_txns:
        dataset["transactions"][card_name] = sorted(card_txns, key=_txn_sort_key)
        if card_name not in dataset["institutions"]:
            dataset["institutions"].append(card_name)

    return {
        "card": card_name,
        "issuer": statements[0]["issuer"],
        "statements": [
            {
                "source_pdf": st["source_pdf"],
                "period": st["statement_period"],
                "due_date": st["due_date"],
                "total_due": st["totals"].get("total_due"),
                "fees_in_period": st["totals"].get("fees_in_period"),
                "cardholders": st["cardholders"],
            }
            for st in statements
        ],
        "settlements": sorted(settlements, key=lambda s: s["paid_on"]),
        "purchase_count": len(card_txns),
        "spend_by_month": {m: round(v, 2) for m, v in sorted(spend_by_month.items())},
        "uncovered_months": uncovered,
        "spillover_pending_months": spillover_pending,
        "kept_lumps": sorted(kept_lumps, key=lambda item: item["paid_on"]),
    }


def monthly_summaries_by_currency(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """currency -> month ("YYYY-MM") ->
    {income, true_expense, net, category_breakdown, income_breakdown}"""

    def _new_month() -> dict[str, Any]:
        return {
            "income": 0.0,
            "true_expense": 0.0,
            "category_breakdown": defaultdict(float),
            "income_breakdown": defaultdict(float),
        }

    by_currency: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_new_month))
    for txns in dataset["transactions"].values():
        for txn in txns:
            bucket = by_currency[txn["currency"]][month_key(txn["date"])]
            if txn["category"] in NON_SPEND_CATEGORIES:
                continue  # a move between own accounts / a card bill settlement
            if txn["direction"] == "CRDT":
                bucket["income"] += txn["amount"]
                bucket["income_breakdown"][txn["category"]] += txn["amount"]
            else:
                bucket["true_expense"] += txn["amount"]
                bucket["category_breakdown"][txn["category"]] += txn["amount"]

    result: dict[str, dict[str, Any]] = {}
    for currency, months in by_currency.items():
        result[currency] = {}
        for month, agg in sorted(months.items()):
            income, expense = round(agg["income"], 2), round(agg["true_expense"], 2)
            result[currency][month] = {
                "income": income,
                "true_expense": expense,
                "net": round(income - expense, 2),
                "category_breakdown": {
                    k: round(v, 2) for k, v in agg["category_breakdown"].items()
                },
                "income_breakdown": {
                    k: round(v, 2) for k, v in agg["income_breakdown"].items()
                },
            }
    return result


def build_signals(monthly: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule-based month-over-month flags: stopped, new, or >15% moved categories."""
    months = sorted(monthly)
    if len(months) < 2:
        return []
    prev_key, cur_key = months[-2], months[-1]
    prev_cats = monthly[prev_key]["category_breakdown"]
    cur_cats = monthly[cur_key]["category_breakdown"]
    signals = []
    for cat in sorted(set(prev_cats) | set(cur_cats)):
        prev_amt, cur_amt = prev_cats.get(cat, 0.0), cur_cats.get(cat, 0.0)
        label = CATEGORY_LABELS.get(cat, cat)
        if prev_amt > 0 and cur_amt == 0:
            signals.append(
                {"type": "stopped", "category": label, "from_month": prev_key, "amount": prev_amt}
            )
        elif prev_amt == 0 and cur_amt > 0:
            signals.append(
                {"type": "new", "category": label, "month": cur_key, "amount": cur_amt}
            )
        elif prev_amt > 0 and abs(cur_amt - prev_amt) / prev_amt >= 0.15:
            pct = round((cur_amt - prev_amt) / prev_amt * 100)
            signals.append(
                {"type": "up" if pct > 0 else "down", "category": label, "pct": pct,
                 "from": prev_amt, "to": cur_amt}
            )
    return signals


# ----------------------------------------------------------------- render --

_CSS = """
.viz-root {
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --text-muted:#898781; --grid:#e1e0d9;
  --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-income:#2a78d6; --series-expense:#eb6834;
  --series-net-pos:#2a78d6; --series-net-neg:#e34948;
  --status-good:#0ca30c; --status-warning:#fab219;
  --status-serious:#ec835a; --good-text:#006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#ffffff;
    --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a;
    --baseline:#383835; --border:rgba(255,255,255,0.10);
    --series-income:#3987e5; --series-expense:#d95926;
    --series-net-pos:#3987e5; --series-net-neg:#e66767;
    --good-text:#0ca30c;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#ffffff;
  --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a;
  --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-income:#3987e5; --series-expense:#d95926;
  --series-net-pos:#3987e5; --series-net-neg:#e66767;
  --good-text:#0ca30c;
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--page); color:var(--text-primary);
  font-family: system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
}
.wrap { max-width:980px; margin:0 auto; padding:32px 20px 64px; }
header.report-head { margin-bottom:28px; }
header.report-head h1 { font-size:26px; margin:0 0 4px; }
header.report-head p { margin:0; color:var(--text-secondary); font-size:14px; }
.card {
  background:var(--surface-1); border:1px solid var(--border);
  border-radius:12px; padding:20px 22px; margin-bottom:20px;
}
.card h2 { font-size:15px; margin:0 0 4px; }
.card h3 { font-size:13px; margin:14px 0 4px; color:var(--text-muted); font-weight:600; }
.card .sub { color:var(--text-secondary); font-size:12.5px; margin:0 0 18px; }
.kpi-row {
  display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));
  gap:14px; margin-bottom:20px;
}
.kpi {
  background:var(--surface-1); border:1px solid var(--border);
  border-radius:12px; padding:16px 18px;
}
.kpi .label { font-size:12px; color:var(--text-secondary); margin-bottom:8px; }
.kpi .value { font-size:26px; font-weight:600; }
.kpi .delta { font-size:12.5px; margin-top:6px; color:var(--text-secondary); }
.gbar-chart {
  display:flex; align-items:flex-end; gap:36px; height:220px; padding:0 8px;
  border-bottom:1px solid var(--baseline); position:relative;
}
.gbar-chart .grid-line { position:absolute; left:0; right:0; height:1px; background:var(--grid); }
.gbar-group {
  display:flex; align-items:flex-end; gap:6px; flex:1;
  justify-content:center; height:100%; position:relative; z-index:1;
}
.gbar { width:34px; border-radius:4px 4px 0 0; position:relative; }
.gbar .val {
  position:absolute; top:-18px; left:50%; transform:translateX(-50%);
  font-size:11px; color:var(--text-secondary); white-space:nowrap;
}
.gbar.income { background:var(--series-income); }
.gbar.expense { background:var(--series-expense); }
.gbar-labels { display:flex; gap:36px; padding:8px 8px 0; }
.gbar-labels > div { flex:1; text-align:center; font-size:12.5px; color:var(--text-secondary); }
.legend-row {
  display:flex; gap:18px; margin-top:14px; font-size:12.5px; color:var(--text-secondary);
}
.legend-row .sw {
  display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:6px; vertical-align:-1px;
}
.divbar-row { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.divbar-row .m-label { width:64px; font-size:12.5px; color:var(--text-secondary); flex-shrink:0; }
.divbar-track {
  flex:1; height:22px; position:relative; background:var(--grid);
  border-radius:4px; overflow:hidden;
}
.divbar-mid { position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--baseline); }
.divbar-fill { position:absolute; top:2px; bottom:2px; border-radius:3px; }
.divbar-fill.pos { left:50%; background:var(--series-net-pos); }
.divbar-fill.neg { right:50%; background:var(--series-net-neg); }
.divbar-val { width:100px; text-align:right; font-size:12.5px; flex-shrink:0; }
.divbar-val.neg { color:var(--series-net-neg); }
.divbar-val.pos { color:var(--good-text); }
.hbar-row { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.hbar-label { width:150px; font-size:12.5px; color:var(--text-secondary); flex-shrink:0; }
.hbar-track { flex:1; height:18px; background:var(--grid); border-radius:3px; overflow:hidden; }
.hbar-fill { height:100%; background:var(--series-income); border-radius:3px 0 0 3px; }
.hbar-val { width:130px; text-align:right; font-size:12.5px; flex-shrink:0; }
.hbar-pct { color:var(--text-muted); font-size:11.5px; margin-left:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--grid); }
th { color:var(--text-secondary); font-weight:500; font-size:12px; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tr:last-child td { border-bottom:none; }
.callout {
  display:flex; gap:12px; padding:12px 14px; border-radius:10px;
  margin-bottom:10px; border:1px solid var(--border);
}
.callout .icon {
  width:20px; height:20px; border-radius:50%; flex-shrink:0; display:flex;
  align-items:center; justify-content:center; font-size:12px;
  font-weight:700; color:#fff; margin-top:1px;
}
.callout.good .icon { background:var(--status-good); }
.callout.warning .icon { background:var(--status-warning); color:#3a2c00; }
.callout .body b { display:block; font-size:13.5px; margin-bottom:2px; }
.callout .body span { font-size:12.5px; color:var(--text-secondary); }
footer.report-foot { color:var(--text-muted); font-size:11.5px; margin-top:8px; line-height:1.6; }
@media (max-width:640px) {
  .kpi-row { grid-template-columns:repeat(2,1fr); }
  .hbar-label { width:100px; }
}
@media print {
  body { background:#fff; }
  .card { break-inside:avoid; }
  .wrap { max-width:100%; padding:0 12px; }
}
"""


def render_html(
    dataset: dict[str, Any],
    currency: str,
    monthly: dict[str, Any],
    signals: list[dict[str, Any]],
    balance_total: float | None = None,
    balance_warnings: list[str] | None = None,
    forecast_model: dict[str, Any] | None = None,
    loan_rows: list[dict[str, Any]] | None = None,
    card_info: dict[str, Any] | None = None,
) -> str:
    months = sorted(monthly.get(currency, {}))
    if not months:
        raise ValueError(f"no transactions found in currency {currency!r} for this period")
    focus = months[-1]
    focus_data = monthly[currency][focus]
    prev_data = monthly[currency][months[-2]] if len(months) > 1 else None
    balance_warnings = balance_warnings or []
    today = date.fromisoformat(dataset["generated_at"])
    loan_rows = [
        _enrich_stale_loan_row(row, dataset, forecast_model, today) for row in (loan_rows or [])
    ]
    loan_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in loan_rows:
        if row.get("loan_type"):
            loan_by_type[row["loan_type"].strip().lower()].append(row)

    max_flow = max(max(m["income"], m["true_expense"]) for m in monthly[currency].values()) or 1
    gbar_groups = []
    for m in months:
        d = monthly[currency][m]
        income_h, expense_h = d["income"] / max_flow * 100, d["true_expense"] / max_flow * 100
        gbar_groups.append(f"""
      <div class="gbar-group">
        <div class="gbar income" style="height:{income_h:.1f}%">
          <span class="val">{d['income']:,.0f}</span>
        </div>
        <div class="gbar expense" style="height:{expense_h:.1f}%">
          <span class="val">{d['true_expense']:,.0f}</span>
        </div>
      </div>""")
    gbar_labels = "".join(f"<div>{m}</div>" for m in months)

    max_abs_net = max(abs(monthly[currency][m]["net"]) for m in months) or 1
    divbar_rows = []
    for m in months:
        net = monthly[currency][m]["net"]
        cls, width = ("pos", net) if net >= 0 else ("neg", -net)
        width_pct = width / max_abs_net * 100
        sign = "+" if net >= 0 else "−"
        divbar_rows.append(f"""
    <div class="divbar-row">
      <div class="m-label">{m}</div>
      <div class="divbar-track">
        <div class="divbar-mid"></div>
        <div class="divbar-fill {cls}" style="width:{width_pct:.1f}%"></div>
      </div>
      <div class="divbar-val {cls}">{sign}{abs(net):,.0f}</div>
    </div>""")

    cats = sorted(focus_data["category_breakdown"].items(), key=lambda kv: -kv[1])
    max_cat = cats[0][1] if cats else 1
    total_expense = focus_data["true_expense"] or 1
    hbar_rows = []
    for cat, amt in cats:
        label = CATEGORY_LABELS.get(cat, cat)
        width_pct = amt / max_cat * 100
        pct_of_total = amt / total_expense * 100
        hbar_rows.append(f"""
    <div class="hbar-row">
      <div class="hbar-label">{label}</div>
      <div class="hbar-track"><div class="hbar-fill" style="width:{width_pct:.1f}%"></div></div>
      <div class="hbar-val">{amt:,.0f} <span class="hbar-pct">{pct_of_total:.0f}%</span></div>
    </div>""")

    income_cats = sorted(focus_data["income_breakdown"].items(), key=lambda kv: -kv[1])
    max_income_cat = income_cats[0][1] if income_cats else 1
    total_income = focus_data["income"] or 1
    income_hbar_rows = []
    for cat, amt in income_cats:
        label = CATEGORY_LABELS.get(cat, cat)
        width_pct = amt / max_income_cat * 100
        pct_of_total = amt / total_income * 100
        income_hbar_rows.append(f"""
    <div class="hbar-row">
      <div class="hbar-label">{label}</div>
      <div class="hbar-track"><div class="hbar-fill" style="width:{width_pct:.1f}%"></div></div>
      <div class="hbar-val">{amt:,.0f} <span class="hbar-pct">{pct_of_total:.0f}%</span></div>
    </div>""")
    income_hbar_html = "".join(income_hbar_rows) if income_hbar_rows else (
        '<p style="font-size:12.5px;color:var(--text-secondary)">'
        "No categorized income this period.</p>"
    )

    all_cats = sorted({c for m in months for c in monthly[currency][m]["category_breakdown"]})
    table_header = "".join(f"<th class='num'>{m}</th>" for m in months)
    table_rows = []
    for cat in all_cats:
        label = CATEGORY_LABELS.get(cat, cat)
        cells = "".join(
            f"<td class='num'>{monthly[currency][m]['category_breakdown'].get(cat, 0):,.0f}</td>"
            for m in months
        )
        table_rows.append(f"<tr><td>{label}</td>{cells}</tr>")

    callouts = []
    for s in signals:
        if s["type"] == "stopped":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>{s["category"]} stopped after {s["from_month"]}</b>'
                f'<span>Was {s["amount"]:,.0f} {currency}/month — confirm this was'
                f" intentional.</span></div></div>"
            )
        elif s["type"] == "new":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>New: {s["category"]} in {s["month"]}</b>'
                f'<span>{s["amount"]:,.0f} {currency} — wasn\'t present the month'
                f" before.</span></div></div>"
            )
        elif s["type"] == "down":
            callouts.append(
                f'<div class="callout good"><div class="icon">✓</div>'
                f'<div class="body"><b>{s["category"]} down {abs(s["pct"])}%</b>'
                f'<span>{s["from"]:,.0f} → {s["to"]:,.0f} {currency} month over'
                f" month.</span></div></div>"
            )
        elif s["type"] == "up":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>{s["category"]} up {s["pct"]}%</b>'
                f'<span>{s["from"]:,.0f} → {s["to"]:,.0f} {currency} month over'
                f" month.</span></div></div>"
            )
    if not callouts:
        callouts.append(
            '<p style="font-size:12.5px;color:var(--text-secondary);margin:0;">'
            "No large month-over-month category swings detected.</p>"
        )

    savings_rate = focus_data["net"] / focus_data["income"] * 100 if focus_data["income"] else 0.0
    prev_income_delta = ""
    if prev_data and prev_data["income"]:
        pct = (focus_data["income"] - prev_data["income"]) / prev_data["income"] * 100
        prev_income_delta = f"{'↑' if pct >= 0 else '↓'} {abs(pct):.0f}% vs {months[-2]}"

    institutions_label = " & ".join(i.upper() for i in dataset["institutions"])
    net_color = "var(--good-text)" if focus_data["net"] >= 0 else "var(--series-net-neg)"
    net_sign = "+" if focus_data["net"] >= 0 else "−"
    savings_color = "var(--good-text)" if savings_rate >= 0 else "var(--series-net-neg)"
    hbar_html = "".join(hbar_rows) if hbar_rows else (
        '<p style="font-size:12.5px;color:var(--text-secondary)">'
        "No categorized expenses this period.</p>"
    )

    balance_kpi = ""
    if balance_total is not None:
        balance_kpi = f"""
    <div class="kpi">
      <div class="label">Balance in hand (now)</div>
      <div class="value">{balance_total:,.0f} {currency}</div>
      <div class="delta">Current (within {BALANCE_CACHE_TTL_MINUTES}min), not from the {focus} snapshot</div>
    </div>"""
    balance_note = ""
    if balance_warnings:
        joined = "; ".join(balance_warnings)
        balance_note = f"""
  <p style="font-size:11.5px;color:var(--status-warning);margin:-6px 0 20px;">
    Balance unavailable for: {joined}
  </p>"""

    forecast_html = ""
    if forecast_model:
        next_month = next_month_key(focus)
        income_pred = forecast_model.get("income", {})
        predicted_income = income_pred.get("predicted_next", 0.0)
        predicted_expense = predicted_expense_total(forecast_model)
        predicted_net = round(predicted_income - predicted_expense, 2)
        pred_net_color = "var(--good-text)" if predicted_net >= 0 else "var(--series-net-neg)"
        pred_net_sign = "+" if predicted_net >= 0 else "−"

        method_notes = {
            "fixed": "flat recurring cost, low variance",
            "average": "based on recent trailing average",
            "stopped": "stopped recently, predicted 0",
            "single_observation": "only one data point so far, low confidence",
            "no_recent_data": "no recent data",
        }
        cat_rows = []
        for cat, entry in sorted(
            forecast_model.get("categories", {}).items(),
            key=lambda kv: -kv[1]["predicted_next"],
        ):
            if entry["predicted_next"] == 0 and entry["method"] not in ("stopped",):
                continue
            label = CATEGORY_LABELS.get(cat, cat)
            note = method_notes.get(entry["method"], entry["method"])
            loan_type = LOAN_TYPE_BY_CATEGORY.get(cat)
            for loan_row in (loan_by_type.get(loan_type, []) if loan_type else []):
                loan_payment = loan_row.get("monthly_payment")
                if loan_payment is None:
                    continue
                payment_tag = " (est.)" if loan_row.get("monthly_payment_estimated") else ""
                due_tag = " (est.)" if loan_row.get("next_payment_date_estimated") else ""
                loan_currency = html.escape(str(loan_row.get("currency") or currency))
                loan_due = html.escape(str(loan_row.get("next_payment_date") or "?")) + due_tag
                loan_institution = html.escape(str(loan_row.get("institution") or "?")).upper()
                note += (
                    f" — Loan Tracker ({loan_institution}): {loan_payment:,.0f}{payment_tag}"
                    f" {loan_currency} due {loan_due}"
                )
            cat_rows.append(
                f"<tr><td>{label}</td><td class='num'>{entry['predicted_next']:,.0f}"
                f" {currency}</td><td>{note}</td></tr>"
            )
        income_note = method_notes.get(income_pred.get("method", ""), income_pred.get("method", ""))

        forecast_html = f"""
  <div class="card">
    <h2>Predicted — {next_month}</h2>
    <p class="sub">
      Local rule-based forecast (fixed/average/stopped per category, no ML) —
      see forecast_model_{currency}.json, updated by this report each run
    </p>
    <div class="kpi-row" style="grid-template-columns:repeat(3,1fr);">
      <div class="kpi">
        <div class="label">Predicted income</div>
        <div class="value">{predicted_income:,.0f} {currency}</div>
        <div class="delta">{income_note}</div>
      </div>
      <div class="kpi">
        <div class="label">Predicted expenses</div>
        <div class="value">{predicted_expense:,.0f} {currency}</div>
      </div>
      <div class="kpi">
        <div class="label">Predicted net</div>
        <div class="value" style="color:{pred_net_color}">
          {pred_net_sign}{abs(predicted_net):,.0f} {currency}
        </div>
      </div>
    </div>
    <table>
      <tr><th>Category</th><th class="num">Predicted</th><th>Basis</th></tr>
      {''.join(cat_rows)}
    </table>
  </div>"""

    card_footnote = ""
    card_html = ""
    if card_info:
        card_footnote = (
            "The monthly credit-card bill payment is likewise excluded "
            "(category: credit_card_settlement) — the card's own itemized "
            "purchases are counted instead, on the date they were made."
        )
        statement_rows = []
        for st in card_info["statements"]:
            holders = ", ".join(
                f"{html.escape(c['name'].title())} &middot;&middot;&middot;{c['card_last4']}"
                f" ({c.get('used_credit_parsed', 0):,.0f})"
                for c in st["cardholders"]
            )
            statement_rows.append(
                "<tr>"
                f"<td>{html.escape(st['period']['from'])} &rarr; {html.escape(st['period']['to'])}</td>"
                f"<td class='num'>{(st['total_due'] or 0):,.0f} {currency}</td>"
                f"<td>{html.escape(str(st['due_date']))}</td>"
                f"<td class='num'>{(st['fees_in_period'] or 0):,.0f}</td>"
                f"<td>{holders}</td>"
                "</tr>"
            )

        settle_rows = []
        for item in card_info["settlements"]:
            if item["statement"] is None:
                matched = "<span class='warn'>no statement in data/cards/ matches this amount</span>"
            else:
                period = item["statement_period"]
                matched = (
                    f"settles {html.escape(period['from'])} &rarr; "
                    f"{html.escape(period['to'])} &check;"
                )
            settle_rows.append(
                "<tr>"
                f"<td>{html.escape(item['paid_on'])}</td>"
                f"<td class='num'>{item['amount']:,.2f} {currency}</td>"
                f"<td>{matched}</td>"
                "</tr>"
            )

        spend_rows = "".join(
            "<tr>"
            f"<td>{html.escape(month)}</td>"
            f"<td class='num'>{amount:,.0f} {currency}</td>"
            "</tr>"
            for month, amount in card_info["spend_by_month"].items()
        )

        notes = []
        for month in card_info["uncovered_months"]:
            notes.append(
                f"<p class='warn'>No statement covers {html.escape(month)}, so "
                "its card bill is still counted as one lump "
                "&ldquo;Credit card&rdquo; expense rather than itemized "
                "categories. The month's total is right; only its breakdown is "
                "coarse. Drop that month's statement PDF into "
                f"<code>data/cards/{html.escape(card_info['card'])}/</code> and "
                "re-run to break it out.</p>"
            )
        if card_info["kept_lumps"]:
            kept = ", ".join(
                f"{item['amount']:,.0f} on {html.escape(item['paid_on'])}"
                for item in card_info["kept_lumps"]
            )
            notes.append(
                f"<p class='sub'>Left as lump payments (no itemized statement "
                f"for their month): {kept}.</p>"
            )
        for month in card_info["spillover_pending_months"]:
            notes.append(
                f"<p class='sub'>{html.escape(month)} is the most recent covered "
                "month. A purchase made late in it is often booked in the "
                "following month and prints on the <em>next</em> statement, which "
                "isn't in <code>data/cards/</code> yet — so this month's card "
                "spend may still rise slightly.</p>"
            )

        card_html = f"""
  <div class="card">
    <h2>Credit card &mdash; {html.escape(card_info['issuer'])}</h2>
    <p class="sub">
      {card_info['purchase_count']} itemized purchases parsed from
      {len(card_info['statements'])} statement PDF(s) in
      <code>data/cards/remember/</code>. These purchases <em>are</em> the
      expense figures in the breakdown above, broken out by real category.
      The monthly lump bill payment to the issuer is reclassified as
      &ldquo;Credit card settlement&rdquo; and excluded from spend &mdash;
      counting both would double-count the same money. Every statement's
      parsed total is checked against its own printed subtotals before use.
    </p>
    <table>
      <tr>
        <th>Statement period</th><th class="num">Total due</th><th>Due date</th>
        <th class="num">Fees</th><th>Cardholders (used credit)</th>
      </tr>
      {''.join(statement_rows)}
    </table>
    <h3>Bill payments seen on the bank side</h3>
    <table>
      <tr><th>Paid on</th><th class="num">Amount</th><th>Reconciliation</th></tr>
      {''.join(settle_rows)}
    </table>
    <h3>Card spend by purchase month</h3>
    <p class="sub">
      Attributed by <code>Bruksdato</code> (purchase date), not by when the
      bill was paid &mdash; so these will not equal the bill payments above,
      which lag by about a month.
    </p>
    <table>
      <tr><th>Month</th><th class="num">Card spend</th></tr>
      {spend_rows}
    </table>
    {''.join(notes)}
  </div>"""

    loan_html = ""
    if loan_rows:
        loan_row_html = []
        for row in loan_rows:
            row_currency = html.escape(str(row.get("currency") or currency))
            balance = row.get("outstanding_balance")
            balance_str = f"{balance:,.0f} {row_currency}" if balance is not None else "?"
            payment = row.get("monthly_payment")
            payment_str = f"{payment:,.0f} {row_currency}" if payment is not None else "?"
            if row.get("monthly_payment_estimated"):
                payment_str += " (est.)"
            rate = row.get("interest_rate_pct")
            rate_str = f"{rate:.2f}%" if rate is not None else "?"
            interest_type = html.escape(str(row.get("interest_type") or "?"))
            next_payment_str = html.escape(str(row.get("next_payment_date") or "?"))
            if row.get("next_payment_date_estimated"):
                next_payment_str += " (est.)"
            institution = html.escape(str(row.get("institution") or "?")).upper()
            loan_type = html.escape(str(row.get("loan_type") or "?"))
            maturity_date = html.escape(str(row.get("maturity_date") or "?"))
            notes = html.escape(str(row.get("notes") or ""))
            loan_row_html.append(
                "<tr>"
                f"<td>{institution}</td>"
                f"<td>{loan_type}</td>"
                f"<td class='num'>{balance_str}</td>"
                f"<td>{rate_str} ({interest_type})</td>"
                f"<td class='num'>{payment_str}</td>"
                f"<td>{next_payment_str}</td>"
                f"<td>{maturity_date}</td>"
                f"<td>{notes}</td>"
                "</tr>"
            )
        loan_html = f"""
  <div class="card">
    <h2>Loan details</h2>
    <p class="sub">
      From the manually-maintained Loan Tracker sheet — not part of the
      transaction-based figures above. "(est.)" means the sheet was stale
      or missing that field, so it's estimated from actual transaction
      history instead (outstanding balance/rate/maturity are never
      estimated this way — no honest transaction-derived equivalent
      exists for those).
    </p>
    <table>
      <tr>
        <th>Institution</th><th>Type</th><th class="num">Outstanding</th>
        <th>Rate</th><th class="num">Next payment</th><th>Due</th>
        <th>Maturity</th><th>Notes</th>
      </tr>
      {''.join(loan_row_html)}
    </table>
  </div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{focus} Financial Report — {institutions_label}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
  <header class="report-head">
    <h1>Financial Report — {focus}</h1>
    <p>Accounts: {institutions_label} &middot; Currency: {currency} &middot;
       Generated {dataset['generated_at']} from live Enable Banking data</p>
  </header>

  <div class="kpi-row">
    <div class="kpi">
      <div class="label">Income ({focus})</div>
      <div class="value">{focus_data['income']:,.0f} {currency}</div>
      <div class="delta">{prev_income_delta}</div>
    </div>
    <div class="kpi">
      <div class="label">True expenses ({focus})</div>
      <div class="value">{focus_data['true_expense']:,.0f} {currency}</div>
    </div>
    <div class="kpi">
      <div class="label">Net cash flow</div>
      <div class="value" style="color:{net_color}">
        {net_sign}{abs(focus_data['net']):,.0f} {currency}
      </div>
    </div>
    <div class="kpi">
      <div class="label">Savings rate</div>
      <div class="value" style="color:{savings_color}">{savings_rate:+.1f}%</div>
    </div>{balance_kpi}
  </div>
  <p style="font-size:11.5px;color:var(--text-muted);margin:-6px 0 20px;">
    "True" figures exclude transfers between your own accounts
    (category: internal_transfer) — those are moves, not income or spend.
    {card_footnote}
  </p>{balance_note}

  <div class="card">
    <h2>Income vs. expenses by month</h2>
    <p class="sub">{institutions_label}, {currency}</p>
    <div class="gbar-chart">
      <div class="grid-line" style="bottom:0%"></div>
      <div class="grid-line" style="bottom:33.3%"></div>
      <div class="grid-line" style="bottom:66.6%"></div>
      {''.join(gbar_groups)}
    </div>
    <div class="gbar-labels">{gbar_labels}</div>
    <div class="legend-row">
      <span><span class="sw" style="background:var(--series-income)"></span>Income</span>
      <span><span class="sw" style="background:var(--series-expense)"></span>Expenses (true)</span>
    </div>
  </div>

  <div class="card">
    <h2>Net cash flow by month</h2>
    <p class="sub">Income minus true expenses</p>
    {''.join(divbar_rows)}
  </div>

  <div class="card">
    <h2>Where {focus}'s money went</h2>
    <p class="sub">True expenses &middot; total {focus_data['true_expense']:,.0f} {currency}</p>
    {hbar_html}
  </div>

  <div class="card">
    <h2>Where {focus}'s money came from</h2>
    <p class="sub">Income &middot; total {focus_data['income']:,.0f} {currency}</p>
    {income_hbar_html}
  </div>

  <div class="card">
    <h2>Category breakdown by month</h2>
    <table><tr><th>Category</th>{table_header}</tr>{''.join(table_rows)}</table>
  </div>
{forecast_html}
{card_html}
{loan_html}

  <div class="card">
    <h2>Signals</h2>
    <p class="sub">
      Rule-based month-over-month flags (≥15% move, category stopped, or category new)
    </p>
    {''.join(callouts)}
  </div>

  <footer class="report-foot">
    Source: live transaction data via Enable Banking (jb_gateway_mcp), pulled
    {dataset['generated_at']}. Categorization is heuristic keyword matching —
    see categories.py. This report is a data summary, not financial advice.
  </footer>
</div>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------- io --


def _label_for_range(date_from: str, date_to: str) -> str:
    """"YYYY-MM" for one full calendar month, "YYYY-MM_to_YYYY-MM" for several
    full calendar months, else the literal dates — always unambiguous, but
    collapses to the readable form whenever the range is month-aligned."""
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    end_last_day = calendar.monthrange(end.year, end.month)[1]
    month_aligned = start.day == 1 and end.day == end_last_day
    if not month_aligned:
        return f"{date_from}_to_{date_to}"
    if (start.year, start.month) == (end.year, end.month):
        return start.strftime("%Y-%m")
    return f"{start.strftime('%Y-%m')}_to_{end.strftime('%Y-%m')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument(
        "--institutions", default=None, help="comma-separated, default: all connected"
    )
    parser.add_argument("--currency", default="NOK")
    parser.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--skip-balance",
        action="store_true",
        help="skip the live 'balance in hand' lookup (e.g. to avoid an extra API call)",
    )
    parser.add_argument(
        "--skip-loans", action="store_true", help="skip the Loan Tracker sheet lookup"
    )
    parser.add_argument(
        "--refresh-loans",
        action="store_true",
        help="ignore the 1-day loan sheet cache and re-fetch it live",
    )
    parser.add_argument(
        "--loan-sheet-account",
        default=None,
        help="Google account for the Loan Tracker sheet (default: whatever's already cached)",
    )
    parser.add_argument(
        "--loan-sheet-id",
        default=None,
        help="Drive file id for the Loan Tracker sheet (default: whatever's already cached)",
    )
    parser.add_argument(
        "--skip-cards",
        action="store_true",
        help="ignore data/cards/ statement PDFs; leave the credit-card bill "
        "as one lump expense instead of decomposing it",
    )
    parser.add_argument(
        "--refresh-cards",
        action="store_true",
        help="re-parse every statement PDF even if its parsed JSON is current",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()
    data_dir, reports_dir = out_dir / "data", out_dir / "reports"
    data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    label = _label_for_range(args.date_from, args.date_to)
    windows = _month_windows(args.date_from, args.date_to)
    window_cache_paths = [
        (w_from, w_to, _full_month_cache_path(data_dir, w_from, w_to)) for w_from, w_to in windows
    ]

    store = BankCredentialStore()

    # Only resolve institutions / touch the network if at least one window
    # actually needs a live fetch — a request fully covered by per-month
    # caches (the common case for a range you've already reported on before)
    # never has to reach the API at all.
    needs_live_fetch = args.refresh or any(
        cache_path is None or not cache_path.exists() for _, _, cache_path in window_cache_paths
    )
    institutions: list[str] = []
    if needs_live_fetch:
        try:
            store.get_app_credential()
        except BankCredentialNotFoundError:
            print(
                "No Enable Banking app credential stored — "
                "run the connect-bank-account skill first.",
                file=sys.stderr,
            )
            return 1
        requested = args.institutions.split(",") if args.institutions else None
        institutions = resolve_institutions(store, requested)
        if not institutions:
            print("No connected institutions with a valid session.", file=sys.stderr)
            return 1

    parts: list[dict[str, Any]] = []
    for w_from, w_to, cache_path in window_cache_paths:
        if cache_path is not None and cache_path.exists() and not args.refresh:
            print(f"Using cached data: {cache_path}")
            parts.append(json.loads(cache_path.read_text()))
            continue
        print(f"Fetching live: {', '.join(institutions)} for {w_from}..{w_to}")
        try:
            part = build_dataset(store, institutions, w_from, w_to)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                print(
                    "Enable Banking rate-limited this request (429). "
                    "Wait a bit and re-run — no data was written.",
                    file=sys.stderr,
                )
                return 1
            raise
        if cache_path is not None:
            cache_path.write_text(json.dumps(part, indent=2))
            print(f"Wrote month cache: {cache_path}")
        parts.append(part)

    dataset = _merge_datasets(parts, args.date_from, args.date_to)

    # Credit-card statements are local PDFs, not an API: parsing them costs
    # nothing and never touches the bank quota, so this runs before the
    # summaries so the decomposed categories feed everything downstream
    # (breakdown, signals, forecast) rather than being bolted on at render.
    card_info: dict[str, Any] | None = None
    cards_dir = data_dir / "cards"
    if not args.skip_cards and statement_pdfs_present(cards_dir):
        statements = load_statements(cards_dir, refresh=args.refresh_cards)
        # Always emit the JSON artifacts, independent of the HTML: the
        # per-statement parsed form plus the compact analysis rollup. They
        # are what later questions should be answered from instead of
        # re-reading the PDFs.
        for analysis_path in write_analysis(cards_dir, statements):
            print(f"Wrote card JSON: {analysis_path}")
        card_info = apply_card_decomposition(
            dataset,
            statements,
            args.date_from,
            args.date_to,
            args.currency,
            load_person_recipients(cards_dir),
        )
        if card_info:
            print(
                f"Decomposed {card_info['card']} card: "
                f"{card_info['purchase_count']} purchases from "
                f"{len(card_info['statements'])} statement(s)"
            )
            for month in card_info["uncovered_months"]:
                print(
                    f"  [card warning] no statement covers {month} — its card "
                    "bill is left as a single lump expense (not itemized); "
                    "drop that month's statement PDF in and re-run for a real "
                    "category breakdown",
                    file=sys.stderr,
                )
            for month in card_info["spillover_pending_months"]:
                print(
                    f"  [card note] {month} is the latest covered month; "
                    "purchases made late in it may be booked onto the next "
                    "statement, which isn't in data/cards/ yet",
                    file=sys.stderr,
                )

    monthly = monthly_summaries_by_currency(dataset)
    if args.currency not in monthly:
        available = ", ".join(sorted(monthly)) or "none"
        print(
            f"No {args.currency} transactions in this dataset. "
            f"Currencies present: {available}",
            file=sys.stderr,
        )
        return 1
    signals = build_signals(monthly[args.currency])
    forecast_model = update_and_predict(out_dir, args.currency, monthly[args.currency])
    print(f"Updated forecast model: {out_dir / 'data' / f'forecast_model_{args.currency}.json'}")

    balance_total: float | None = None
    balance_warnings: list[str] = []
    if not args.skip_balance:
        balance_total, balance_warnings = fetch_balance_total(
            store, dataset, args.currency, data_dir, refresh=args.refresh
        )
        for warning in balance_warnings:
            print(f"  [balance warning] {warning}", file=sys.stderr)

    loan_rows: list[dict[str, Any]] = []
    if not args.skip_loans:
        loan_rows = fetch_loan_details(
            out_dir, args.loan_sheet_account, args.loan_sheet_id, refresh=args.refresh_loans
        )

    # Final, post-decomposition figures as JSON. notify_email.py reads this
    # rather than re-deriving from the raw month cache: that cache is written
    # before card decomposition, so deriving from it made the monthly email
    # contradict the very report it announces (it reported the lump card bill
    # as expense while the report itself reported itemized purchases). One
    # computed artifact, one set of numbers. Also the cheap thing to read for
    # later analysis of a period already reported on.
    summary_path = data_dir / f"{label}-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": dataset["generated_at"],
                "label": label,
                "period": {"from": args.date_from, "to": args.date_to},
                "currency": args.currency,
                "institutions": dataset["institutions"],
                "focus_month": sorted(monthly[args.currency])[-1],
                "monthly": {
                    month: {
                        "income": round(figures["income"], 2),
                        "true_expense": round(figures["true_expense"], 2),
                        "net": round(figures["net"], 2),
                        "category_breakdown": {
                            k: round(v, 2)
                            for k, v in sorted(
                                figures["category_breakdown"].items(), key=lambda kv: -kv[1]
                            )
                        },
                        "income_breakdown": {
                            k: round(v, 2)
                            for k, v in sorted(
                                figures["income_breakdown"].items(), key=lambda kv: -kv[1]
                            )
                        },
                    }
                    for month, figures in sorted(monthly[args.currency].items())
                },
                "signals": signals,
                "balance_in_hand": balance_total,
                "balance_warnings": balance_warnings,
                "card": None
                if not card_info
                else {
                    "card": card_info["card"],
                    "purchase_count": card_info["purchase_count"],
                    "spend_by_month": card_info["spend_by_month"],
                    "uncovered_months": card_info["uncovered_months"],
                    "spillover_pending_months": card_info["spillover_pending_months"],
                    "kept_lumps": card_info["kept_lumps"],
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"Wrote summary: {summary_path}")

    institutions_slug = "-".join(dataset["institutions"])
    report_path = reports_dir / f"{label}-{institutions_slug}-{args.currency}-report.html"
    report_path.write_text(
        render_html(
            dataset,
            args.currency,
            monthly,
            signals,
            balance_total=balance_total,
            balance_warnings=balance_warnings,
            forecast_model=forecast_model,
            loan_rows=loan_rows,
            card_info=card_info,
        )
    )
    print(f"Wrote report: {report_path}")

    other_currencies = sorted(set(monthly) - {args.currency})
    if other_currencies:
        print(
            f"Note: also has data in {', '.join(other_currencies)} "
            "(not charted here — rerun with --currency to see them)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
