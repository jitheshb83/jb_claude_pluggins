# Categorization

Why categories are re-derived rather than trusted from cache, and the
rules that keep the bank and card sides agreeing. Read this when adding
or fixing a category rule.

## Categorization is derived, not stored


`recategorize()` re-derives every bank transaction's category from its
cached raw fields (counterparty, description, direction) after the per-month
caches are merged, and reports how many changed.

`build_dataset` bakes a category in at fetch time and the cache stores it,
so without this pass an improved rule in `categories.py` would only affect
months fetched *after* the change — correcting an older month would mean
re-fetching it live and spending that institution's daily API quota on data
already sitting on disk. Treating the category as derived makes a rule fix
retroactive and free. Card lines are categorized separately by
`categorize_card` and are skipped here.

Two rules worth knowing about, both added because the bank and card sides
disagreed about the same money:

- `top-up by *` → `internal_transfer`. Funding a linked account from the
  user's own credit card. The card side already books the outgoing leg as
  an internal transfer; without this the arriving money counted as *income*
  as well, and the eventual purchase counted as spend.
- `categorize()` now takes the same `person_recipients` list as
  `categorize_card`, so paying a given person is categorized the same way
  whether it left the card or a bank account.

## Unmatched transactions stay visible

Keyword rules in `scripts/categories.py` are heuristic and never
exhaustive. Anything that matches nothing lands in `income_other` (for a
credit) or `uncategorized` (for a debit) rather than being forced into the
nearest-looking bucket. That is deliberate: an uncategorized line is
visible in the report and invites a rule, whereas a silently mis-bucketed
one corrupts a category's history and the forecast built on it. Extend
`CATEGORY_RULES` / `SALARY_EMPLOYERS` as new recurring counterparties show
up; because categories are re-derived on load, the fix applies to every
month already on disk.
