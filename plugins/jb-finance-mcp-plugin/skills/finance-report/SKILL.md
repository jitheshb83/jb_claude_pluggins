---
name: finance-report
description: Generates a local HTML financial report (income/expense trend, income splits, category breakdown, month-over-month signals, live balance-in-hand, and a next-month expense/income forecast) from linked bank accounts (DNB, Nordea, Revolut via jb_gateway_mcp) plus manually-imported credit-card statement PDFs (re:member/Entercard), and caches the underlying data + a persisted forecast model under ~/Documents/MyFinance/ for reuse without re-hitting the bank API. Can run unattended via a scheduled launchd job (see launchd/) that auto-generates the report for the prior month on the 1st of each month and emails a success/failure status notification via Gmail. Use when asked for a spending/usage report, financial statistics, expense or income breakdown, budget trends, current balance, a prediction of next month's expenses, to import or analyze a credit-card statement, or to set up/check/troubleshoot the monthly automated report.
---

# Generating a personal finance report

Turns linked bank transaction data into a local HTML report with charts
(income vs. expense trend, net cash flow, expense category breakdown,
income breakdown, a month-over-month table, a live "balance in hand"
figure, and a rule-based prediction of next month's income/expenses) plus a
JSON data cache, both saved under `~/Documents/MyFinance/` so they persist
across sessions and don't need to be regenerated from scratch every time.
See `~/Documents/MyFinance/README.md` for the on-disk convention.

**Prerequisite**: `jb_gateway_mcp` installed standalone and at least one
bank institution connected via this plugin's `connect-bank-account` skill.
For the optional email notification step, a Google account with
`gmail.send` connected via the separate `jb-google-notify-plugin`'s
`connect-google-account` skill.

Do the manual walkthrough (like the one that produced the first report in
this project's history) only if this script doesn't fit — e.g. a currency
this script doesn't chart, or a one-off question that doesn't warrant a
saved report. For anything that matches "give me a report/stats for
period X", use the script; it's cheaper in tokens and already verified
against live data.

## What it does

`scripts/generate_report.py`:

1. **Checks the cache first, per calendar month.** The requested range is
   split into calendar-month windows; any month that already has
   `~/Documents/MyFinance/data/<YYYY-MM>-transactions.json` is read from
   there — no API call — and only genuinely missing months are fetched
   live (each newly-fetched full month gets its own cache file too, so a
   later overlapping request reuses it). A partial month (range doesn't
   start on the 1st / end on the last day) is never cached under a month
   key — caching a partial slice there would silently corrupt later
   lookups for that month — so it's always fetched fresh. Pass `--refresh`
   to ignore all cache files for this run and re-fetch every month live.
   This matters in practice: Enable Banking enforces a **daily**
   per-consent access cap (PSD2 "consented multiplicity without PSU
   involvement per day"), not a short burst limit — a 429 here means
   today's quota for that institution is spent, not "wait a few minutes."
   Reusing already-fetched months is the only way to avoid re-hitting it
   for data you already have.
2. **Otherwise fetches live**, directly via the stored Enable Banking
   credentials (same pattern as `connect-bank-account/scripts/check_bank_status.py`
   — imports `jb_gateway_mcp.adapters.enable_banking` and calls it directly,
   *not* through the MCP protocol, so it runs standalone).
3. **Categorizes** every transaction with keyword rules in `scripts/categories.py`
   (mortgage, credit_card, salary, insurance, etc. — extend that file as new
   recurring counterparties show up; unmatched transactions land in
   `income_other`/`uncategorized` so they stay visible rather than being
   silently mis-bucketed).
4. **Computes** monthly income/true-expense/net **and a category breakdown
   for both sides** (expenses *and* income — salary vs. pension/benefit vs.
   dividend vs. other) per currency. "True" expense excludes
   `internal_transfer` (money moved between the user's own accounts,
   detected by matching the counterparty name against the linked accounts'
   own names), so a self-transfer never gets counted as spend or income.
5. **Flags signals**: any expense category that stopped, newly appeared, or
   moved ≥15% month over month — purely rule-based, no LLM judgment baked
   into the script.
6. **Fetches balance** ("balance in hand") for every account in the
   requested currency — never read from the cached transaction snapshot,
   since a balance is inherently "right now," not history, but reused from
   a short-lived per-account cache (`data/balance_cache.json`,
   `BALANCE_CACHE_TTL_MINUTES` = 60) rather than re-fetched live on every
   run — a personal balance figure doesn't need second-by-second
   freshness, and re-fetching it needlessly eats into the same daily
   per-institution quota transactions do. `--refresh` forces a live
   re-fetch regardless of cache age (same flag that forces transactions to
   re-fetch). One account failing (expired session, rate limit) prints a
   warning and is excluded from the total rather than failing the whole
   report; skip this step entirely with `--skip-balance`.
7. **Updates a persisted forecast model** (`scripts/forecast.py`) — see
   "Forecasting" below — and renders its prediction for the month *after*
   the report's focus month.
8. **Renders** the HTML report and writes both files under
   `~/Documents/MyFinance/{data,reports}/`.
9. **Adds a "Loan details" card** from a manually-maintained Loan Tracker
   Google Sheet (`scripts/loans.py`) — see "Loan details" below. Purely
   informational: it never changes the forecast model or the Predicted
   card's numbers, only adds a cross-reference note next to a matching
   category's prediction.

10. **Decomposes the credit card** from statement PDFs under
    `data/cards/` (`scripts/cards.py`) — see "Credit-card statements"
    below. Unlike the Loan details card, this one *does* change the
    headline numbers: it replaces the lump bill payment with the card's
    real spending categories.

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

## Automating it monthly (launchd)

`scripts/run_monthly.sh` computes "last calendar month" relative to today
(BSD `date -v` arithmetic — macOS only) and runs `generate_report.py` for
exactly that month, logging everything to
`~/Documents/MyFinance/logs/<label>-run-<timestamp>.log` since it's meant
to run unattended. `launchd/com.jbgatewaymcp.financereport.monthly.plist`
is the tracked template that schedules it for 08:00 on the 1st of every
month (`StartCalendarInterval` with `Day: 1`). After `generate_report.py`
finishes, it also emails a short status notification via
`scripts/notify_email.py` — see "Email notifications" below.

`run_monthly.sh` resolves its own plugin root at runtime, so it works
wherever this plugin was actually installed — no path editing needed there.
Two things in this section *do* need editing for your own setup:
`FROM_ACCOUNT`/`TO_ADDRESS` near the top of `run_monthly.sh`, and the
`__PLUGIN_ROOT__`/`__HOME__` placeholders in the plist (see "Install"
below).

### Email notifications

`scripts/notify_email.py` sends via the Gmail adapter directly (same
direct-adapter-call pattern `generate_report.py` uses for bank data — not
through the MCP protocol, so it works standalone). Takes `--from-account`
and `--to-address` explicitly (both required — there's no baked-in
default); `run_monthly.sh` passes these from its own `FROM_ACCOUNT`/
`TO_ADDRESS` variables, which you should edit for your own accounts.
Requires the `gmail.send` OAuth scope on the stored Google token (onboarded
via the `jb-google-notify-plugin`'s `connect-google-account` skill), which
is **not** part of `onboard-google`'s default read-only scope set — if you
see `403 Insufficient Permission`, re-run `onboard-google` including
`https://www.googleapis.com/auth/gmail.send` alongside the existing
readonly scopes (all of them — re-consenting replaces the stored token
wholesale, it doesn't merge, so omitting a previously-granted scope
silently drops it).

- **On success**: subject `Finance report ready — <label>`, body has
  income/expenses/net/savings-rate headline numbers read from
  `data/<label>-summary.json` (the figures the report itself rendered),
  plus any card-coverage or balance caveats, plus the local report path.
  It deliberately does *not* recompute from `data/<label>-transactions.json`
  — that is the pre-decomposition bank snapshot, and deriving from it made
  the email contradict the report it announces whenever a card statement
  had been itemized. There is still a fallback to it for reports generated
  before summaries existed, and the email labels itself when that happens.
- **On failure**: subject `Finance report FAILED — <label>`, body has the
  failure reason and the log file path, plus a remediation hint for the
  most likely cause (expired bank consent).
- **Deliberately NOT the full report or transaction detail** — only
  headline numbers, to avoid duplicating sensitive financial detail into
  an email inbox beyond what's necessary. The full report always stays
  local; the email just says a new one exists (or doesn't) and why.
- `run_monthly.sh` is **not** `set -e` — a failing `generate_report.py`
  must still reach the failure-email branch below it, not abort the
  script first. The script's final `exit "$REPORT_EXIT"` deliberately
  preserves the *report generation's* exit code as the job's result even
  though the notification step runs after it — so launchd's "last exit
  code" always reflects whether the report itself succeeded, never masked
  by the email step's own success or failure.
- Manual/ad-hoc report generation (e.g. Claude building a report
  mid-conversation) never emails anything — only `run_monthly.sh` calls
  `notify_email.py`, by design, so on-demand use doesn't spam an inbox. For
  an ad-hoc "email me this report" request instead, use the
  `jb-google-notify-plugin`'s `report-notifier` agent.

**Install** (the live copy lives outside any repo, in
`~/Library/LaunchAgents/` — OS-specific, not version-controlled itself,
hence the tracked template here). First substitute the placeholders in the
plist for this plugin's actual install path and your home directory:

```bash
PLUGIN_ROOT="$(pwd)"   # run this from the plugin root, see "Running it" above
sed -e "s|__PLUGIN_ROOT__|$PLUGIN_ROOT|g" -e "s|__HOME__|$HOME|g" \
   skills/finance-report/launchd/com.jbgatewaymcp.financereport.monthly.plist \
   > ~/Library/LaunchAgents/com.jbgatewaymcp.financereport.monthly.plist
launchctl bootstrap gui/$(id -u) \
   ~/Library/LaunchAgents/com.jbgatewaymcp.financereport.monthly.plist
```

**Verify without waiting for the 1st**:
`launchctl kickstart -p gui/$(id -u)/com.jbgatewaymcp.financereport.monthly`,
then check the newest file in `~/Documents/MyFinance/logs/` and
`launchctl print gui/$(id -u)/com.jbgatewaymcp.financereport.monthly | grep "last exit"`
(0 = success; anything else, read the log).

**Uninstall**:
`launchctl bootout gui/$(id -u)/com.jbgatewaymcp.financereport.monthly`,
then delete the plist from `~/Library/LaunchAgents/`.

**If you move/reinstall this plugin to a different path**, the installed
plist does *not* follow it — `ProgramArguments` bakes in the absolute
`__PLUGIN_ROOT__` path at install time (see the `sed` step above), it
doesn't re-resolve at runtime. A plugin move without reinstalling the
plist leaves `launchctl` pointing at a script that no longer exists there
— the job fails silently (check `last exit code` per "Verify" above) with
no error surfaced anywhere else. Re-run the **Install** steps above
(bootout the old one, regenerate the plist from the new `PLUGIN_ROOT`,
bootstrap it) any time the plugin's install location changes.

**The gotcha that will eat an hour if you hit it blind**: a fresh
`launchd`-spawned process has **no access to `~/Documents`** by default —
macOS TCC (privacy protection) blocks it, even though your interactive
shell/IDE already has that access and so doesn't notice anything's wrong
when you test the script by hand. The failure mode is deceptive:
`/bin/zsh: can't open input file: ...` even when the file demonstrably
exists and is executable, or `Operation not permitted` on a plain `ls` of
the very same directory a normal terminal can read fine. Diagnosed by
running an isolated LaunchAgent that just `ls`s the target directory to
`/tmp` — confirms it's TCC, not a script bug, in one shot if you hit this
again on a fresh machine.

**Fix**: System Settings → Privacy & Security → Full Disk Access → add
`/bin/zsh` (Cmd+Shift+G to type the path), toggle it on. This is what
`ProgramArguments` in the plist invokes as the interpreter, so it's the
binary that needs the grant — not the script file itself, and not
`launchd`. Worth knowing this is a **broad** grant (every zsh script on
the machine gets `~/Documents` access, not just this job) — the
standard/only practical fix for this scenario on modern macOS, but flag it
rather than treat it as free.

**Known unresolved gotcha: `notify_email.py` can hang indefinitely under
launchd.** Triggering the job via `launchctl kickstart` has been observed
to leave `notify_email.py` running (not exited, not erroring) — most
likely a one-time macOS Keychain access prompt for the Gmail credential
that a launchd-spawned process hasn't been granted "Always Allow" for yet,
which a headless/non-interactive trigger can't answer. `generate_report.py`
itself completes and writes the report fine either way — only the email
step is affected. If a run seems stuck, check for a Keychain prompt on
screen and approve it; `ps aux | grep notify_email` confirms whether it's
actually hung versus just slow. Not yet fixed as of this writing — treat a
hung run as a signal to check for that prompt, not as a script bug to
chase in the code.
