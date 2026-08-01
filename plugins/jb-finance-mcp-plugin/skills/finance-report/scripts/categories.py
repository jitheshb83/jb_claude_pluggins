"""Keyword-based transaction categorization for the finance-report skill.

Heuristic, not exhaustive: matched in order against
"<counterparty_name> <description>" lowercased, first hit wins. Extend
CATEGORY_RULES / SALARY_EMPLOYERS as new recurring counterparties show up —
unmatched transactions land in "income_other" (CRDT) or "uncategorized"
(DBIT) so they stay visible in the report instead of being silently
mis-bucketed.
"""

from __future__ import annotations

# (category, [keywords]) -- checked in order, first match wins.
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("mortgage", ["betaling på lån", "restgjeld", "avdrag"]),
    ("credit_card", ["entercard"]),
    ("car_finance", ["dnb finans"]),
    ("international_transfer", ["wise"]),
    ("school_fees", ["school", "skole"]),
    ("insurance", ["forsikring"]),
    ("housing_fee", ["boligsamei", "boligsameie"]),
    ("electricity", ["energi", "electricity", "strøm"]),
    ("telecom", ["telia", "telenor"]),
    ("toll", ["skyttelpass", "bompeng", "autopass"]),
    ("parking", ["easypark", "parking", "betongbygg"]),
    ("municipal_charge", ["kommune"]),
    ("dividend", ["kundeutbytte", "utbytte"]),
    ("pension_benefit", ["nav", "pensjon", "trygd"]),
    ("bank_fee", ["prislagte tjenester", "gebyr"]),
]

# Substrings that mark a CRDT transaction as salary. Add new employers here.
SALARY_EMPLOYERS: list[str] = ["infosys"]


def categorize(
    direction: str,
    counterparty_name: str | None,
    description: str | None,
    own_names: set[str],
) -> str:
    """Assign one category label to a transaction.

    own_names: lowercased account-holder names across every linked account
    being processed together, so a transfer between two of the user's own
    accounts is recognized as internal_transfer regardless of which
    institution's counterparty field it shows up in.
    """
    name = (counterparty_name or "").strip().lower()
    if name and name in own_names:
        return "internal_transfer"

    haystack = f"{counterparty_name or ''} {description or ''}".lower()

    for category, keywords in CATEGORY_RULES:
        if any(keyword in haystack for keyword in keywords):
            return category

    if direction == "CRDT":
        if any(employer in haystack for employer in SALARY_EMPLOYERS):
            return "salary"
        return "income_other"
    return "uncategorized"


def dedupe_transactions(transactions: list[dict]) -> list[dict]:
    """Drop exact duplicates left by overlapping date-range fetch windows.

    Enable Banking treats date_to as inclusive on both ends of adjacent
    windows, so stitching e.g. [May1,Jun1] + [Jun1,Jul1] double-counts every
    Jun1 row (found empirically — see finance-report/SKILL.md). Key is
    (date, amount, direction, description); two genuinely distinct
    same-day transactions essentially never share all three.
    """
    seen: set[tuple] = set()
    result: list[dict] = []
    for txn in transactions:
        key = (txn.get("date"), txn.get("amount"), txn.get("direction"), txn.get("description"))
        if key in seen:
            continue
        seen.add(key)
        result.append(txn)
    return sorted(result, key=lambda t: t.get("date") or "")
