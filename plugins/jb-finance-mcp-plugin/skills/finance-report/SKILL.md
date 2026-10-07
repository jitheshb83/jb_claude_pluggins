---
name: finance-report
description: Generates a local HTML financial report and the compact JSON behind it — income/expense trend, category breakdown, how you paid (credit card vs each bank account), month-over-month signals, live balance-in-hand, loan details, and a next-month forecast. Sources are linked bank accounts (DNB, Nordea, Revolut via jb_gateway_mcp) plus credit-card statement PDFs imported by hand (re:member/Entercard), cached under ~/Documents/MyFinance/ so repeat questions never re-hit the bank API. Use this skill for ANY question about this user's own money, even when they never say the word report — where their money went, how much they spent on groceries or travel or a named merchant, what they put on the card versus the bank, spending or income breakdowns, budget trends, savings rate, current balance, next month's predicted expenses, a loan or mortgage balance or payment date, importing or analyzing a card statement, or setting up and troubleshooting the automated monthly run. Prefer it over querying bank tools directly, because the cached JSON usually answers the question outright for a fraction of the tokens and without spending the day's API quota.
---

# Generating a personal finance report

Turns linked bank data and credit-card statement PDFs into a local HTML
report plus compact JSON, under `~/Documents/MyFinance/`. Everything
persists, so most questions are answered by reading a small file rather
than regenerating anything. See `~/Documents/MyFinance/README.md` for the
on-disk convention.

**Prerequisite**: `jb_gateway_mcp` installed standalone and at least one
institution connected via this plugin's `connect-bank-account` skill. The
optional status email also needs `gmail.send` via the
`jb-google-notify-plugin`'s `connect-google-account` skill.

## Answer from the cached JSON before running anything

Two scarce resources shape how this skill should be used: Enable Banking
enforces a **daily** per-consent access cap (not a burst limit — a 429
means today's quota for that institution is gone), and every file read
costs context. Both point the same way: read the smallest artifact that
answers the question, and only generate when something is genuinely
missing.

| Question | Read this | Not this |
|---|---|---|
| Figures for a period already reported | `data/<label>-summary.json` | the HTML, or raw month caches |
| Card spend, categories, cardholder split | `data/cards/<card>/card-analysis.json` (~5 KB) | the statement PDFs (~500 KB) |
| One merchant's history | `data/cards/<card>/card-merchants.json` | the PDFs |
| A month's raw transactions | `data/<YYYY-MM>-transactions.json` | a live re-fetch |

`<label>-summary.json` holds the **final** post-decomposition figures —
monthly income/expense/net, both breakdowns, signals, balance, payment-method
split and card coverage. `<YYYY-MM>-transactions.json` is the *raw bank*
snapshot written before card decomposition, so never quote a month's
headline figures from it.

Never extract text from a statement PDF to answer a question: the parsed
JSON is authoritative and ~90x cheaper.

Generate a fresh report when the user asks for one, when the period has
never been reported, or when new statement PDFs have landed. A manual
walkthrough is only worth it for something the script genuinely can't do —
an uncharted currency, or a one-off that doesn't warrant a saved report.

## What a run does

`scripts/generate_report.py`, in order:

1. **Reuses the per-month cache**, fetching live only the months genuinely
   missing. A partial month is never cached under a month key — a partial
   slice there would silently corrupt later lookups for that month.
2. **Fetches live** via stored Enable Banking credentials, importing
   `jb_gateway_mcp.adapters.enable_banking` directly rather than through
   the MCP protocol, so it runs standalone — the same pattern as
   `connect-bank-account/scripts/check_bank_status.py`.
3. **Re-derives every category** from the cached raw fields, so a rule fix
   applies retroactively and for free → `references/categorization.md`.
4. **Decomposes the credit card** from statement PDFs, replacing the lump
   bill with real categories → `references/cards.md`. This one changes the
   headline numbers.
5. **Computes** monthly income / true expense / net and a breakdown of
   both sides per currency. "True" excludes `internal_transfer` and
   `credit_card_settlement` — moves between the user's own instruments are
   neither income nor spend.
6. **Flags signals**: a category that stopped, newly appeared, or moved
   ≥15% month over month. Purely rule-based, no LLM judgment in the script.
7. **Splits expenses by payment instrument** — card vs each bank account
   → `references/rendering.md`.
8. **Fetches balance in hand**, live but reused from a 60-minute per-account
   cache; one account failing is a warning, not a failed report.
   `--skip-balance` skips it.
9. **Updates the persisted forecast** and predicts the following month →
   `references/forecasting.md`.
10. **Adds loan details** from the Loan Tracker sheet → `references/loans.md`.
    Informational only; never changes the forecast.
11. **Writes** the HTML, `<label>-summary.json`, and the card JSON.

## Running it


Run from this plugin's root directory (the directory containing this
skill's parent `skills/` folder and this plugin's `pyproject.toml`):

```bash
uv run python skills/finance-report/scripts/generate_report.py \
    --from 2026-07-01 --to 2026-07-31
```

Options:

| Flag | Default | Notes |
|---|---|---|
| `--institutions dnb,nordea` | every institution with a valid stored session | comma-separated aliases from `connect-bank-account` |
| `--currency NOK` | `NOK` | which currency's accounts get charted; others are still fetched/cached and get a one-line footnote — currencies are never summed together |
| `--out-dir PATH` | `~/Documents/MyFinance` | override for testing |
| `--refresh` | off | ignore every per-month transaction cache file this range touches AND the balance cache, re-fetch everything live |
| `--skip-balance` | off | skip the live "balance in hand" lookup — useful if you're rate-limited or just want the cached-only report faster |
| `--skip-loans` | off | skip the Loan Tracker sheet lookup entirely |
| `--skip-cards` | off | ignore `data/cards/` statement PDFs; leave the card bill as one lump expense |
| `--refresh-cards` | off | re-parse every statement PDF even if its parsed JSON is current |
| `--refresh-loans` | off | ignore the 1-day loan sheet cache, re-fetch it live |
| `--loan-sheet-account` | whatever's already cached | Google account for the Loan Tracker sheet; only needed the first time or if it changes |
| `--loan-sheet-id` | whatever's already cached | Drive file id for the Loan Tracker sheet; only needed the first time or if it changes |

For a single calendar month, `--from`/`--to` should span the 1st to the
last day of that month — the report's "focus month" (the KPI row, the
category breakdown, the signals) is always the *last* calendar month in the
range, with earlier months providing trend context in the charts.

## Filename convention


- Data (cache, one file per calendar month, shared across every report that
  covers that month): `data/<YYYY-MM>-transactions.json`. This is the *raw
  bank* snapshot, written before credit-card decomposition — never read it
  for a month's final figures.
- Final computed figures for a report: `data/<label>-summary.json` —
  post-decomposition monthly income/expense/net, both breakdowns, signals,
  balance and card coverage. `notify_email.py` reads this so the email can
  never disagree with the report, and it is the cheapest artifact to read
  when revisiting a period already reported on.
- Balance cache (per account, `BALANCE_CACHE_TTL_MINUTES`-old entries are
  still reused): `data/balance_cache.json`
- Forecast model (persists across runs, one per currency, not per period):
  `data/forecast_model_<currency>.json`
- Loan Tracker sheet cache (1-day TTL, one entry regardless of currency):
  `data/loan_tracker_cache.json`
- Card statements (manual drop-in) and their generated JSON:
  `data/cards/<card>/*.pdf`, `data/cards/<card>/parsed/<stem>.json`
  (re-parsed automatically when the PDF's mtime/size changes),
  `data/cards/<card>/card-analysis.json` (compact rollup — read this one)
  and `data/cards/<card>/card-merchants.json` (merchant detail)
- Report: `reports/<label>-<institutions>-<currency>-report.html` — currency
  is part of the filename so that running the same institutions+range for
  two different `--currency` values (e.g. NOK then EUR) writes two separate
  files instead of the second silently overwriting the first.
- `<label>` (report filenames only, not the data cache) is `YYYY-MM` for one
  full calendar month, `YYYY-MM_to_YYYY-MM` for several full calendar
  months, or the literal ISO dates if the range isn't month-aligned.

## Reference files

Read the one that matches the task rather than all of them — each is
self-contained, and loading them all would cost more than the report.

| File | Read it when |
|---|---|
| `references/cards.md` | importing a statement, or a card figure looks wrong |
| `references/categorization.md` | adding or fixing a category rule |
| `references/rendering.md` | changing anything visual in the HTML |
| `references/forecasting.md` | a prediction looks wrong |
| `references/loans.md` | a loan figure is wrong or marked `(est.)` |
| `references/automation.md` | installing or troubleshooting the monthly job |
