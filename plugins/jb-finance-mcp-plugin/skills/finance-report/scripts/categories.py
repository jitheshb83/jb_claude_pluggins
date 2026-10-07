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
    # Funding a linked account from the user's own credit card ("Top-Up by
    # *5541"). Must come first and must beat the CRDT income fallback: the
    # money arriving is a move between the user's own instruments, not
    # income. The card side already books the outgoing leg as
    # internal_transfer, so without this the same 500 kr counted as income
    # here and the eventual purchase counted as spend.
    ("internal_transfer", ["top-up by *", "top up by *"]),
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
    person_recipients: tuple[str, ...] | list[str] = (),
) -> str:
    """Assign one category label to a transaction.

    own_names: lowercased account-holder names across every linked account
    being processed together, so a transfer between two of the user's own
    accounts is recognized as internal_transfer regardless of which
    institution's counterparty field it shows up in.

    person_recipients: the same private-person list `categorize_card` uses
    (from local user data). Applied here too so paying a given person is
    categorized the same way whether it went out on the card or straight
    from a bank account, rather than landing in `uncategorized` on one side
    only.
    """
    name = (counterparty_name or "").strip().lower()
    if name and name in own_names:
        return "internal_transfer"

    haystack = f"{counterparty_name or ''} {description or ''}".lower()

    for category, keywords in CATEGORY_RULES:
        if any(keyword in haystack for keyword in keywords):
            return category

    if person_recipients and any(p in haystack for p in person_recipients):
        return "person_transfer"

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


# --- Credit-card statement categorization ----------------------------------
# The bank API only ever shows the monthly Entercard bill as one lump; these
# rules categorize the itemized purchases parsed out of the statement PDFs
# (see cards.py) so that lump can be decomposed into real categories.
#
# Matched in order against "<description> <place>" lowercased, first hit
# wins. Grounded in the merchants actually present in this user's
# statements, NOT a speculative list of Norwegian retailers — extend it as
# new merchants appear. An unmatched purchase lands in "uncategorized" so it
# stays visible in the report rather than being silently mis-bucketed.
CARD_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    # Own-account top-up: money moved to the user's own Revolut account, which
    # the report already covers as a linked institution. Counting it as spend
    # here would double-count whatever it was then spent on.
    ("internal_transfer", ["revolut"]),
    ("bank_fee", ["gebyr"]),
    (
        "groceries",
        [
            "rema 1000", "meny", "coop prix", "extra vestby", "nordby supermar",
            "abiramy cash", "hoang asia mat", "global smak",
        ],
    ),
    (
        "dining_takeaway",
        [
            "sodexo", "narvesen", "dominos", "restaurant", "sushi",
            "mcdnygaardskrysset", "bakeri", "gigaboks",
        ],
    ),
    ("public_transport", ["vy app", "vygruppen", "ruter"]),
    ("travel", ["ryanair"]),
    ("fitness", ["evofitness"]),
    ("education", ["simplilearn"]),
    ("electronics", ["power moss", "avxperten"]),
    (
        "home_goods",
        ["jysk", "ikea", "jula", "clas oh", "rusta", "normal moss", "plantehallen"],
    ),
    ("clothing", ["hm no"]),
    ("books", ["ark ski"]),
    ("telecom", ["mycall"]),
    ("ev_charging", ["kople"]),
]

# Vipps person-to-person transfers. Vipps prefixes a merchant and a private
# person identically ("Vipps*<name>"), so a private payment is only
# recognizable by the recipient being a person.
#
# The recipient names are NOT in this file. They are real people, and this
# repository states it carries no account data — so they live in local user
# data (`data/cards/person-recipients.json`, read by cards.py) and are
# passed in. With none configured, such a payment lands in "uncategorized",
# where it stays visible rather than being silently mis-bucketed.

CARD_CATEGORY_LABELS = {
    "groceries": "Groceries",
    "dining_takeaway": "Dining/takeaway",
    "public_transport": "Public transport",
    "travel": "Travel",
    "fitness": "Fitness",
    "education": "Education",
    "electronics": "Electronics",
    "home_goods": "Home goods",
    "clothing": "Clothing",
    "books": "Books",
    "ev_charging": "EV charging",
    "person_transfer": "Person transfer",
}


def categorize_card(
    description: str | None,
    place: str | None,
    person_recipients: tuple[str, ...] | list[str] = (),
) -> str:
    """Assign one category to a parsed credit-card statement line.

    `person_recipients` comes from local user data (see the note above);
    matching a Vipps payment against it is the only way to tell a private
    transfer from a merchant purchase. Add names there rather than widening
    the merchant rules, so a new Vipps *merchant* is never silently booked
    as a person transfer.
    """
    haystack = f"{description or ''} {place or ''}".lower()

    if "vipps" in haystack and any(p in haystack for p in person_recipients):
        return "person_transfer"

    for category, keywords in CARD_CATEGORY_RULES:
        if any(keyword in haystack for keyword in keywords):
            return category

    return "uncategorized"
