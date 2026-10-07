# Credit-card statements

Everything about importing re:member (Entercard) statement PDFs and
decomposing the lump bill into real categories. Read this when working
with `data/cards/`, adding a statement, or debugging a card figure.

## Credit-card statements (no API)


Enable Banking exposes only *payment* accounts, so a credit card's itemized
purchases are unreachable — the bank side shows just the monthly lump bill
payment to the issuer ("Entercard Norge"). Statement PDFs are therefore
imported by hand:

```
data/cards/remember/<anything>.pdf       <- drop the statement PDF in
data/cards/remember/parsed/<stem>.json   <- generated: full fidelity, one per statement
data/cards/remember/card-analysis.json   <- generated: compact per-month rollup  (~5 KB)
data/cards/remember/card-merchants.json  <- generated: merchant-level detail    (~12 KB)
```

**All three JSON forms are written on every run**, including by the
standalone command below — parsing a local PDF costs no bank API quota, so
there is no reason to make it opt-in.

### Answering a card question without burning tokens

Read `card-analysis.json`. It has per-month spend, category totals,
cardholder split, the statement/reconciliation chain, coverage, and FX
purchases — enough for almost any follow-up, at roughly a tenth the size of
the per-statement `parsed/*.json` and a nineteenth of the PDFs. Go to
`card-merchants.json` only for a merchant-specific question ("how much at
REMA this quarter"); it is ~3x the summary's size and the two files share no
data, so reading both is never necessary for a question the summary answers.
**Never** extract text from the PDFs to answer a question — that is the most
expensive path and the parsed JSON is authoritative.

Regenerate the JSON without producing a report (no bank calls at all):

```bash
uv run python skills/finance-report/scripts/cards.py          # all cards
uv run python skills/finance-report/scripts/cards.py --refresh  # re-parse unchanged PDFs
```

`scripts/cards.py` parses them **positionally**, not by regex over flat
text: the statement has two right-aligned amount columns (`Beløp` = a
charge, `Innbetalt` = a payment in) that `extract_text()` collapses into
indistinguishable trailing numbers. Column x-bands are measured constants
at the top of the file. It also handles Norwegian number format
(`23 203,91`), thousands groups split across word tokens (`1` + `714,00`),
multi-line FX detail (`686,700 EUR Kurs 11,190`), per-cardholder sections,
and page furniture that otherwise gets appended to the previous
transaction's description.

**Every statement is reconciled against its own printed subtotals** —
per-cardholder `BENYTTET KREDITT I PERIODEN`, `Brukt i fakturaperioden`,
and `Innbetalinger i perioden`. A mismatch over 0.02 raises
`StatementParseError` and aborts the run. This is deliberate: a layout
change that silently half-parsed a statement would understate spending,
which is worse than no report.

Two counting rules, both consequential:

- **Decompose, don't add.** The lump bill payment is reclassified to
  `credit_card_settlement` and excluded from spend exactly the way
  `internal_transfer` already is — *not deleted*, so the cash movement
  stays auditable in the data. The card's purchases become the expense
  figures. Counting both would double-count the same money.
- **Attributed by purchase date (`Bruksdato`)**, not bill date. A 20 Aug
  purchase is August spend even though the bill cleared 15 Sep. A month's
  card spend therefore will **not** equal that month's bill payment; the
  report shows both side by side rather than letting that surprise you.

Consequences worth knowing:

- A bill payment whose amount matches no statement in `data/cards/` is
  flagged in the report rather than silently assumed. This is normal at
  the start of a range — the first payment in it usually settles a
  statement from before the range.
- A month in the report range with **no** covering statement keeps its lump
  bill as a plain `credit_card` expense instead of being decomposed, and
  says so in the report and the email. Only a month with itemized purchases
  to put in its place has its lump reclassified — otherwise the automated
  monthly run would silently understate expenses by an entire card bill,
  since on the 1st the statement covering the month just ended usually is
  not in `data/cards/` yet. The month's total stays correct; only its
  breakdown is coarse.
- The most recent covered month is flagged as provisional. A purchase made
  late in a month is often booked the following month and prints on the
  *next* statement, so that month's card spend can still rise.
- A card is identified by its folder name under `data/cards/`, and each
  card gets its own `card-analysis.json`/`card-merchants.json` in that
  folder. Statements are never pooled across cards.
- A PDF left directly in `data/cards/` instead of `data/cards/<card>/` is
  ignored by the glob, so the loader warns about it by name rather than
  reporting on less data than you think you supplied.
- Vipps private-person recipients live in
  `data/cards/person-recipients.json` (a JSON list of lowercase name
  substrings), **not** in the plugin source — they are real people's names
  and this repo carries no account data. Without that file a private Vipps
  payment lands in `uncategorized`, which is visible rather than
  mis-bucketed. Merchant keywords stay in the source since they are generic
  retailers, not personal data.
- Merchant rules live in `CARD_CATEGORY_RULES` in `scripts/categories.py`,
  grounded in merchants actually seen in these statements rather than a
  speculative retailer list. Unmatched purchases land in `uncategorized`
  so they stay visible. `CARD_PERSON_RECIPIENTS` lists known Vipps
  person-to-person recipients, since Vipps formats a private person and a
  merchant identically.
- **Known limitation — mixed coverage skews the predicted total.** If some
  months in the model's rolling history were decomposed and another kept its
  lump, `credit_card` and the per-merchant categories are both non-zero in
  that window. Each month's own breakdown is still internally consistent
  (never both for the same month), but `predicted_expense_total` sums every
  category's independent prediction, so it over-predicts by roughly one card
  bill. The per-category rows in the Predicted card remain individually
  correct and show which rule fired. Mitigation: keep statements current so
  coverage is uniform. Not worked around in code — doing so would require
  the forecast to know these two category sets are alternative
  representations of the same spending, which is a bigger change than the
  skew justifies.
- Switching a lump category to decomposed ones leaves the old
  `credit_card` entry in the forecast model. `forecast.py` records a zero
  for any category on record but absent from a month's breakdown, so it
  correctly decays to a 0 prediction instead of predicting a cost that no
  longer exists.
