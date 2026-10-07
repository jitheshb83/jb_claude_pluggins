"""Parse re:member (Entercard Norge) credit-card statement PDFs.

Enable Banking only exposes *payment* accounts, so the card's itemized
purchases are not reachable through the bank API — the bank side shows only
the monthly lump bill payment to "Entercard Norge". These statements are
dropped in by hand under `data/cards/` and parsed here so that lump can be
decomposed into real spending categories (see the cards README for the
counting rules this implements).

Parsing is **positional**, not regex-over-flat-text: the statement has two
right-aligned amount columns (`Beløp` = a charge, `Innbetalt` = a payment
in) that a flat `extract_text()` collapses into indistinguishable trailing
numbers. Column x-bands below were measured off the real statements; they
have been stable across every issue seen so far, and `parse_statement`
cross-checks every parsed total against the statement's own printed
subtotals, so a layout change surfaces as a loud reconciliation error
rather than quietly wrong numbers.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from categories import categorize_card

# Measured x0 band starts for the transaction table (see module docstring).
_X_BOOKING_DATE = 112
_X_DESCRIPTION = 170
_X_PLACE = 288
_X_REFERENCE = 372
_X_AMOUNT = 440
# A charge right-aligns at x1 ~479, a payment-in at x1 ~535.
_CHARGE_X1 = (440, 492)
_CREDIT_X1 = (496, 548)

_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_DECIMAL_TAIL = re.compile(r"^\d{1,3},\d{2}$")
_DIGITS = re.compile(r"^\d{1,3}$")
_CARD_HEADER = re.compile(r"Kortnr:\s*(\S+?)\*+(\d{4}),\s*(.+?)(?:\s+-\s+fortsetter.*)?$")
_FX = re.compile(r"^([\d  ]*[\d,]+)\s+([A-Z]{3})\s+Kurs$")

# Page furniture repeated on every transaction page. These sit in the same
# x-band as a description, so without an explicit skip they get appended to
# the previous page's last transaction ("NARVESEN ... - fortsetter fra").
_BOILERPLATE = (
    "FAKTURA side",
    "re:member Mastercard",
    "Transaksjoner",
    "Bruksdato",
    "Fortsetter neste side",
    "Kontoinformasjon",
)

_PERIOD = re.compile(r"Faktura gjelder for perioden:\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")
_LABELLED_TOTALS = {
    "total_due": "Totalt skyldig beløp:",
    "previous_balance": "Overført saldo fra forrige periode:",
    "payments_in_period": "Innbetalinger i perioden:",
    "fees_in_period": "Gebyrer i perioden:",
}
_SPENT = re.compile(r"Brukt i fakturaperioden[^:]*:\s*([\d  ]+,\d{2})")
_INVOICE_DATE = re.compile(r"Fakturadato:\s*(\d{2}\.\d{2}\.\d{4})")
_DUE_DATE = re.compile(r"Forfallsdato:\s*(\d{2}\.\d{2}\.\d{4})")
_USED_CREDIT = re.compile(r"BENYTTET KREDITT I PERIODEN:\s*([\d  ]+,\d{2})")
_GRAND_TOTAL = re.compile(r"TOTALT:\s*([\d  ]+,\d{2})(?:\s+([\d  ]+,\d{2}))?")


class StatementParseError(RuntimeError):
    """Raised when a statement's parsed figures disagree with its own
    printed subtotals — i.e. the layout changed and the numbers can no
    longer be trusted. Deliberately fatal rather than a warning: a silently
    half-parsed statement would understate spending in the report."""


def _nok(raw: str) -> float:
    """'1 587,08' / '23 203,91' -> float. Norwegian format: space (or
    non-breaking space) thousands separator, comma decimal."""
    return float(raw.replace(" ", "").replace(" ", "").replace(",", "."))


def _iso(raw: str) -> str:
    return datetime.strptime(raw, "%d.%m.%Y").date().isoformat()


def _group_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group a page's words into visual lines, tolerating sub-point drift in
    `top` between glyphs on the same baseline."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        buckets.setdefault(round(word["top"] / 2), []).append(word)
    return [sorted(buckets[k], key=lambda w: w["x0"]) for k in sorted(buckets)]


def _amount_in_band(
    line: list[dict[str, Any]], band: tuple[float, float]
) -> float | None:
    """Reassemble a right-aligned amount whose thousands group is a separate
    word ('1' + '714,00'), but only if it right-aligns inside `band` — that
    alignment is the only thing distinguishing a charge from a payment in."""
    for i, word in enumerate(line):
        if not _DECIMAL_TAIL.match(word["text"]):
            continue
        if not band[0] <= word["x1"] <= band[1]:
            continue
        parts = [word["text"]]
        j, left_edge = i - 1, word["x0"]
        # Walk left over adjacent thousands groups. Adjacency (a few points
        # of gap) is the test, NOT a fixed x floor: a wide amount's leading
        # group starts left of the amount column's nominal edge, and
        # clipping it there silently divided the value by a thousand.
        while j >= 0 and _DIGITS.match(line[j]["text"]) and left_edge - line[j]["x1"] <= 6:
            parts.insert(0, line[j]["text"])
            left_edge = line[j]["x0"]
            j -= 1
        return _nok(" ".join(parts))
    return None


def _band_text(line: list[dict[str, Any]], lo: float, hi: float) -> str:
    return " ".join(w["text"] for w in line if lo <= w["x0"] < hi).strip()


# Issuer shown for a card directory. A directory with no entry here still
# parses — it is simply reported under its own folder name, rather than
# being mislabelled as a card it is not.
_ISSUERS = {"remember": "Entercard Norge"}


def parse_statement(path: Path, card: str | None = None) -> dict[str, Any]:
    """Parse one statement PDF into normalized transactions + its own totals.

    `card` identifies which card's folder this came from; it defaults to the
    PDF's parent directory name. It is NOT hardcoded, because labelling a
    second card's statements as the first card's would merge two unrelated
    spending histories into one.

    Raises StatementParseError if the parsed charges don't reconcile against
    the statement's printed per-card and grand totals."""
    card = card or path.parent.name
    import pdfplumber

    pages_lines: list[list[list[dict[str, Any]]]] = []
    flat_text_lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            lines = _group_lines(page.extract_words())
            pages_lines.append(lines)
            flat_text_lines.extend(" ".join(w["text"] for w in ln) for ln in lines)

    blob = "\n".join(flat_text_lines)

    period = _PERIOD.search(blob)
    if not period:
        raise StatementParseError(f"{path.name}: no 'Faktura gjelder for perioden' line")
    invoice_date = _INVOICE_DATE.search(blob)
    due_date = _DUE_DATE.search(blob)

    totals: dict[str, float] = {}
    for key, label in _LABELLED_TOTALS.items():
        for line in flat_text_lines:
            stripped = line.strip()
            if stripped.startswith(label):
                tail = stripped[len(label):].strip()
                if re.fullmatch(r"[\d  ]+,\d{2}", tail):
                    totals[key] = _nok(tail)
                    break
    spent = _SPENT.search(blob)
    if spent:
        totals["spent_in_period"] = _nok(spent.group(1))

    transactions: list[dict[str, Any]] = []
    cardholders: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None  # current cardholder
    in_payments_section = False

    for lines in pages_lines:
        for line in lines:
            text = " ".join(w["text"] for w in line).strip()

            if any(text.startswith(prefix) for prefix in _BOILERPLATE):
                continue

            if "INNBETALINGER, RENTER OG GEBYR" in text:
                in_payments_section, current = True, None
                continue
            header = _CARD_HEADER.search(text)
            if header:
                in_payments_section = False
                last4 = header.group(2)
                name = re.sub(r"\s+-\s+fortsetter.*$", "", header.group(3)).strip()
                existing = next((c for c in cardholders if c["card_last4"] == last4), None)
                if existing is None:
                    existing = {"card_last4": last4, "name": name}
                    cardholders.append(existing)
                current = existing
                continue
            used = _USED_CREDIT.search(text)
            if used:
                if current is not None:
                    current["used_credit_printed"] = _nok(used.group(1))
                continue
            if _GRAND_TOTAL.search(text) or text.startswith("Fortsetter neste side"):
                continue

            dates = [w for w in line if _DATE.match(w["text"])]
            starts_transaction = (
                len(dates) >= 2
                and dates[0]["x0"] < _X_BOOKING_DATE
                and dates[1]["x0"] < _X_DESCRIPTION
            )

            if starts_transaction:
                charge = _amount_in_band(line, _CHARGE_X1)
                credit = _amount_in_band(line, _CREDIT_X1)
                if charge is None and credit is None:
                    continue
                txn: dict[str, Any] = {
                    "purchase_date": _iso(dates[0]["text"]),
                    "booking_date": _iso(dates[1]["text"]),
                    "description": _band_text(line, _X_DESCRIPTION, _X_PLACE),
                    "place": _band_text(line, _X_PLACE, _X_REFERENCE),
                    "reference": _band_text(line, _X_REFERENCE, _X_AMOUNT),
                    "amount": charge if charge is not None else credit,
                    "direction": "DBIT" if charge is not None else "CRDT",
                    "cardholder": None if current is None else current["name"],
                    "card_last4": None if current is None else current["card_last4"],
                    "section": "payments_fees" if in_payments_section else "purchases",
                }
                transactions.append(txn)
                continue

            # Continuation line: wrapped description, or an FX detail pair.
            if transactions:
                desc_tail = _band_text(line, _X_DESCRIPTION, _X_PLACE)
                place_tail = _band_text(line, _X_PLACE, _X_REFERENCE)
                last = transactions[-1]
                fx = _FX.match(place_tail) or _FX.match(desc_tail)
                if fx:
                    last["fx"] = {
                        "amount": _nok(fx.group(1)),
                        "currency": fx.group(2),
                    }
                elif desc_tail and last.get("fx") is None:
                    last["description"] = f"{last['description']} {desc_tail}".strip()
                elif last.get("fx") is not None and re.fullmatch(
                    r"[\d  ]*[\d,]+", desc_tail or place_tail or ""
                ):
                    last["fx"]["rate"] = _nok(desc_tail or place_tail)

    _reconcile(path, transactions, totals, cardholders)

    return {
        "source_pdf": path.name,
        "card": card,
        "issuer": _ISSUERS.get(card, card),
        "currency": "NOK",
        "statement_period": {"from": _iso(period.group(1)), "to": _iso(period.group(2))},
        "invoice_date": _iso(invoice_date.group(1)) if invoice_date else None,
        "due_date": _iso(due_date.group(1)) if due_date else None,
        "totals": totals,
        "cardholders": cardholders,
        "transactions": transactions,
    }


def _reconcile(
    path: Path,
    transactions: list[dict[str, Any]],
    totals: dict[str, float],
    cardholders: list[dict[str, Any]],
) -> None:
    """Cross-check parsed charges against the statement's own printed
    subtotals. Tolerance is 0.02 to absorb float summation noise only — far
    below the smallest real line on a statement, so it cannot hide a missed
    or double-counted transaction."""
    purchases = [t for t in transactions if t["section"] == "purchases"]

    for holder in cardholders:
        printed = holder.get("used_credit_printed")
        if printed is None:
            continue
        parsed = sum(
            t["amount"] for t in purchases
            if t["card_last4"] == holder["card_last4"] and t["direction"] == "DBIT"
        )
        if abs(parsed - printed) > 0.02:
            raise StatementParseError(
                f"{path.name}: card ...{holder['card_last4']} parsed charges "
                f"{parsed:.2f} != printed BENYTTET KREDITT {printed:.2f}"
            )
        holder["used_credit_parsed"] = round(parsed, 2)

    spent = totals.get("spent_in_period")
    if spent is not None:
        parsed_total = sum(t["amount"] for t in purchases if t["direction"] == "DBIT")
        if abs(parsed_total - spent) > 0.02:
            raise StatementParseError(
                f"{path.name}: parsed charges {parsed_total:.2f} != printed "
                f"'Brukt i fakturaperioden' {spent:.2f}"
            )

    payments = totals.get("payments_in_period")
    if payments is not None:
        parsed_credits = sum(t["amount"] for t in transactions if t["direction"] == "CRDT")
        if abs(parsed_credits - payments) > 0.02:
            raise StatementParseError(
                f"{path.name}: parsed payments-in {parsed_credits:.2f} != printed "
                f"'Innbetalinger i perioden' {payments:.2f}"
            )


def _cache_path(parsed_dir: Path, pdf: Path) -> Path:
    return parsed_dir / f"{pdf.stem}.json"


def load_statements(cards_dir: Path, refresh: bool = False) -> list[dict[str, Any]]:
    """Parse every statement PDF under `cards_dir`, reusing a per-PDF parsed
    JSON unless the source file changed (mtime+size) or `refresh` is set.
    Parsing is pure CPU on a local file, but the cache keeps the parsed form
    inspectable alongside the bank snapshots."""
    statements: list[dict[str, Any]] = []
    # A statement dropped straight into cards/ instead of cards/<card>/ is
    # invisible to the glob below. Say so rather than quietly reporting on
    # less data than the user thinks they provided.
    for stray in sorted(cards_dir.glob("*.pdf")):
        print(
            f"  [cards warning] {stray.name} is directly in {cards_dir}/ and "
            f"will be ignored — move it into a card folder, e.g. "
            f"{cards_dir.name}/remember/",
            file=sys.stderr,
        )
    for card_dir in sorted(p for p in cards_dir.iterdir() if p.is_dir()):
        parsed_dir = card_dir / "parsed"
        parsed_dir.mkdir(exist_ok=True)
        for pdf in sorted(card_dir.glob("*.pdf")):
            stamp = {"mtime": int(pdf.stat().st_mtime), "size": pdf.stat().st_size}
            cache = _cache_path(parsed_dir, pdf)
            if not refresh and cache.exists():
                cached = json.loads(cache.read_text())
                if cached.get("_source_stamp") == stamp:
                    statements.append(cached)
                    continue
            parsed = parse_statement(pdf, card=card_dir.name)
            parsed["_source_stamp"] = stamp
            cache.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
            statements.append(parsed)
    return statements


def statement_pdfs_present(cards_dir: Path) -> bool:
    return cards_dir.is_dir() and any(cards_dir.glob("*/*.pdf"))


# Categories that move money without being consumption. Kept in sync with
# generate_report.NON_SPEND_CATEGORIES by intent, but duplicated as a literal
# so cards.py stays importable on its own (the standalone CLI below must work
# without pulling in the bank adapters).
_NON_SPEND = {"internal_transfer", "credit_card_settlement"}

ANALYSIS_FILENAME = "card-analysis.json"
MERCHANTS_FILENAME = "card-merchants.json"
PERSON_RECIPIENTS_FILENAME = "person-recipients.json"


def load_person_recipients(cards_dir: Path) -> tuple[str, ...]:
    """Private-person Vipps recipients, from local user data.

    Kept out of the plugin source on purpose: these are real people's names,
    and this repo carries no account data. Expected shape is a JSON list of
    lowercase substrings, e.g. ["jane doe", "john s"]. A missing or
    malformed file yields none, in which case a private Vipps payment lands
    in `uncategorized` — visible, not mis-bucketed.
    """
    path = cards_dir / PERSON_RECIPIENTS_FILENAME
    if not path.exists():
        return ()
    try:
        names = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"  [cards warning] {path} is not valid JSON ({exc}); ignoring", file=sys.stderr)
        return ()
    if not isinstance(names, list):
        print(f"  [cards warning] {path} should contain a JSON list; ignoring", file=sys.stderr)
        return ()
    return tuple(str(n).strip().lower() for n in names if str(n).strip())


def _month(iso: str) -> str:
    return iso[:7]


def build_analysis(
    statements: list[dict[str, Any]],
    person_recipients: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compact, analysis-ready rollups of every parsed statement.

    Returns `(summary, merchants)`, written as two files on purpose. These
    are what a question about card spending should be answered from — not
    the PDFs, and not the per-statement `parsed/*.json`, which carry every
    raw line and grow without bound as statements accumulate.

    The split is the point: per-month totals by category and cardholder
    answer most questions on their own, and the merchant-level detail is
    roughly three times that size, so keeping them apart means the common
    case reads the small file instead of paying for merchant rows nobody
    asked about. Nothing is duplicated between them — a per-month merchant
    figure lives only in `merchants[*].by_month`.

    Months span whatever the statements cover, deliberately *not* a report
    range: these are reusable artifacts, not scoped to whichever period
    happened to be reported last.
    """
    by_month: dict[str, dict[str, Any]] = {}
    merchants: dict[str, dict[str, Any]] = {}
    fx_purchases: list[dict[str, Any]] = []

    def month_bucket(month: str) -> dict[str, Any]:
        return by_month.setdefault(
            month,
            {
                "spend": 0.0,
                "purchase_count": 0,
                "non_spend": defaultdict(float),
                "by_category": defaultdict(float),
                "by_cardholder": defaultdict(float),
            },
        )

    for statement in statements:
        for line in statement["transactions"]:
            if line["section"] != "purchases" or line["direction"] != "DBIT":
                continue
            month = _month(line["purchase_date"])
            category = categorize_card(
                line["description"], line["place"], person_recipients
            )
            bucket = month_bucket(month)
            bucket["purchase_count"] += 1

            if category in _NON_SPEND:
                bucket["non_spend"][category] += line["amount"]
            else:
                bucket["spend"] += line["amount"]
                bucket["by_category"][category] += line["amount"]
                if line["cardholder"]:
                    bucket["by_cardholder"][line["cardholder"]] += line["amount"]

            entry = merchants.setdefault(
                line["description"],
                {
                    "category": category,
                    "count": 0,
                    "total": 0.0,
                    "by_month": defaultdict(float),
                    "first_seen": line["purchase_date"],
                    "last_seen": line["purchase_date"],
                },
            )
            entry["count"] += 1
            entry["total"] += line["amount"]
            entry["by_month"][month] += line["amount"]
            entry["first_seen"] = min(entry["first_seen"], line["purchase_date"])
            entry["last_seen"] = max(entry["last_seen"], line["purchase_date"])

            if line.get("fx"):
                fx_purchases.append(
                    {
                        "purchase_date": line["purchase_date"],
                        "description": line["description"],
                        "amount": line["amount"],
                        "fx": line["fx"],
                    }
                )

    months_out: dict[str, Any] = {}
    for month in sorted(by_month):
        bucket = by_month[month]
        months_out[month] = {
            "spend": round(bucket["spend"], 2),
            "purchase_count": bucket["purchase_count"],
            "non_spend": {k: round(v, 2) for k, v in sorted(bucket["non_spend"].items())},
            "by_category": {
                k: round(v, 2)
                for k, v in sorted(bucket["by_category"].items(), key=lambda kv: -kv[1])
            },
            "by_cardholder": {
                k: round(v, 2)
                for k, v in sorted(bucket["by_cardholder"].items(), key=lambda kv: -kv[1])
            },
        }

    merchants_out = {
        name: {
            "category": entry["category"],
            "count": entry["count"],
            "total": round(entry["total"], 2),
            "by_month": {m: round(v, 2) for m, v in sorted(entry["by_month"].items())},
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
        }
        for name, entry in sorted(merchants.items(), key=lambda kv: -kv[1]["total"])
    }

    covered: set[str] = set()
    for statement in statements:
        period = statement["statement_period"]
        cursor = date.fromisoformat(period["from"]).replace(day=1)
        end = date.fromisoformat(period["to"])
        while cursor <= end:
            covered.add(cursor.strftime("%Y-%m"))
            year, mon = (cursor.year + 1, 1) if cursor.month == 12 else (cursor.year, cursor.month + 1)
            cursor = date(year, mon, 1)

    first = statements[0]
    provenance = {
        "generated_at": datetime.now(UTC).isoformat(),
        "card": first["card"],
        "issuer": first["issuer"],
        "currency": first["currency"],
        "source_pdfs": [st["source_pdf"] for st in statements],
    }
    merchants_file = {
        **provenance,
        "note": (
            f"Merchant-level detail for the {first['card']} card. Read "
            f"{ANALYSIS_FILENAME} first — it has the per-month category and "
            "cardholder totals, and is much smaller. Come here only for a "
            "merchant-specific question."
        ),
        "merchant_count": len(merchants_out),
        "merchants": merchants_out,
    }
    summary = {
        "generated_at": provenance["generated_at"],
        "card": first["card"],
        "issuer": first["issuer"],
        "currency": first["currency"],
        "source_pdfs": [st["source_pdf"] for st in statements],
        "note": (
            "Spend attributed by purchase date (Bruksdato), not bill date. "
            "`spend` excludes non-consumption categories (see `non_spend`). "
            "Read this file for card questions instead of the PDFs or "
            f"parsed/*.json — it is the compact form. Merchant-level detail "
            f"lives in {MERCHANTS_FILENAME}."
        ),
        "statements": [
            {
                "source_pdf": st["source_pdf"],
                "period": st["statement_period"],
                "invoice_date": st["invoice_date"],
                "due_date": st["due_date"],
                "totals": st["totals"],
                "cardholders": [
                    {
                        "name": c["name"],
                        "card_last4": c["card_last4"],
                        "used_credit": c.get("used_credit_parsed"),
                    }
                    for c in st["cardholders"]
                ],
            }
            for st in statements
        ],
        "coverage": {
            "covered_months": sorted(covered),
            "statement_periods": [
                [st["statement_period"]["from"], st["statement_period"]["to"]]
                for st in statements
            ],
        },
        "totals": {
            "spend": round(sum(m["spend"] for m in months_out.values()), 2),
            "non_spend": round(
                sum(v for m in months_out.values() for v in m["non_spend"].values()), 2
            ),
            "purchase_count": sum(m["purchase_count"] for m in months_out.values()),
        },
        "by_month": months_out,
        "fx_purchases": fx_purchases,
    }
    return summary, merchants_file


def write_analysis(
    cards_dir: Path,
    statements: list[dict[str, Any]],
    person_recipients: tuple[str, ...] | None = None,
) -> list[Path]:
    """Write the analysis rollups for each card, into that card's own folder.

    Grouped by card on purpose: `statements` may span several cards, and
    rolling them into one file keyed off the first statement's card would
    both mislabel the result and leave the other cards with no analysis at
    all.
    """
    if person_recipients is None:
        person_recipients = load_person_recipients(cards_dir)
    by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for statement in statements:
        by_card[statement["card"]].append(statement)

    written: list[Path] = []
    for card, card_statements in sorted(by_card.items()):
        # Chronological, so `source_pdfs` and the statement chain read in
        # order regardless of how the files happened to be named.
        card_statements.sort(key=lambda st: st["statement_period"]["from"])
        summary, merchants = build_analysis(card_statements, person_recipients)
        for filename, payload in (
            (ANALYSIS_FILENAME, summary),
            (MERCHANTS_FILENAME, merchants),
        ):
            path = cards_dir / card / filename
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=float)
            )
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    """Standalone: parse every statement PDF to JSON without running a full
    report. Writes the per-statement `parsed/*.json` and the compact
    `card-analysis.json`, so the JSON exists even when nobody asked for an
    HTML report (and costs no bank API quota — these are local files)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cards-dir",
        default=str(Path.home() / "Documents" / "MyFinance" / "data" / "cards"),
    )
    parser.add_argument("--refresh", action="store_true", help="re-parse unchanged PDFs")
    args = parser.parse_args(argv)

    cards_dir = Path(args.cards_dir).expanduser()
    if not statement_pdfs_present(cards_dir):
        print(f"No statement PDFs under {cards_dir}/<card>/", flush=True)
        return 1

    statements = load_statements(cards_dir, refresh=args.refresh)
    for statement in statements:
        period = statement["statement_period"]
        print(
            f"{statement['source_pdf']}: {period['from']}..{period['to']} "
            f"total {statement['totals'].get('total_due', 0):,.2f} "
            f"{statement['currency']} "
            f"({len([t for t in statement['transactions'] if t['section'] == 'purchases'])} purchases)"
        )
    written = write_analysis(cards_dir, statements)
    for path in written:
        print(f"\nWrote {path} ({path.stat().st_size:,} bytes)")
    if written:
        analysis = json.loads(written[0].read_text())
        print(
            f"  {analysis['totals']['purchase_count']} purchases, "
            f"spend {analysis['totals']['spend']:,.2f} {analysis['currency']}, "
            f"months {', '.join(analysis['by_month'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
