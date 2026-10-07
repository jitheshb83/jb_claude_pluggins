# Loan details

The manually-maintained Loan Tracker sheet and its stale-value fallbacks.
Read this when a loan figure looks wrong or is marked `(est.)`.

## Loan details


Enable Banking (this skill's only bank data source) doesn't expose loan or
mortgage accounts — PSD2's Account Information Service scope is legally
limited to payment accounts, confirmed live against this user's own DNB/
Nordea consents (see jb_gateway_mcp's project memory "Loan Tracker sheet"
for the full investigation). Loan/financing details are instead tracked by
hand in a Google Sheet and read via `scripts/loans.py`:

- Cached locally at `~/Documents/MyFinance/data/loan_tracker_cache.json`
  for **1 day** — a repeat run within that window never calls the Drive
  API, matching the `balance_cache.json` pattern above. Pass
  `--refresh-loans` to force a live re-fetch regardless of cache age.
- `--loan-sheet-account`/`--loan-sheet-id` only need to be passed once (or
  whenever they change) — once cached, later runs reuse the stored
  `source_account`/`source_file_id` automatically.
- Deliberately calls Drive's export endpoint directly with
  `mimeType="text/csv"` rather than going through
  `jb_gateway_mcp.adapters.google_drive.read_file` — that function
  hardcodes `text/plain`, which Drive's export API rejects for
  spreadsheets specifically (400 "requested conversion is not supported").
  `text/csv` is the correct format there, and only ever returns the
  sheet's first/active tab.
- The sheet's own number/date formatting (currency-symbol-prefixed
  amounts, DD/MM/YYYY dates) is deliberately left as-is at the source —
  `loans.py` parses amounts/rates into floats on the way in (tolerant of
  both US-style `393,507.39` and EU-style `393.507,39` grouping, since a
  sheet's regional format isn't something to assume), but dates are shown
  verbatim in the report. All free-text sheet fields (institution, loan
  type, notes, etc.) are HTML-escaped before rendering — a sheet is
  external input, not code-controlled text like `CATEGORY_LABELS`.
- The Predicted card's `mortgage`/`car_finance` rows (see "Forecasting"
  below) get an extra note per matching loan, e.g.
  `Loan Tracker (DNB): 12,500 NOK due 01/09/2026` — informational
  cross-reference only, never used to change `predicted_next` or
  `method`. If more than one loan shares a type (e.g. two car loans from
  different institutions), each gets its own note rather than one
  clobbering the other.
- **Stale-sheet fallback**: if a loan row's `last_updated` is more than 30
  days old (or missing) — or `monthly_payment`/`next_payment_date` is
  simply blank — those two fields fall back to a value derived from actual
  transaction history: `monthly_payment` from the matching category's own
  `forecast.py` prediction, `next_payment_date` from the most recent
  matching transaction's date plus one month. Each field is tagged
  `(est.)` independently — in both the Loan details card and the
  Predicted card's cross-reference note — so an estimated value is never
  confused with an actual sheet-sourced fact, and a row where only one of
  the two fields was estimated doesn't mislabel the other.
  `outstanding_balance`, `interest_rate_pct`, `original_amount`, and
  `maturity_date` are **never** estimated this way — transaction history
  has no honest way to recover a loan's actual principal, rate, or term,
  so a stale/blank value there is shown as-is (or `?`), not guessed.
- Skip this step entirely with `--skip-loans`.
