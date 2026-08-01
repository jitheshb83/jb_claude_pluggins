---
name: connect-bank-account
description: Connects a new bank account (DNB, Nordea, Revolut, or any Enable Banking-supported institution) to jb_gateway_mcp, or refreshes/re-authorizes one whose 90-day consent has expired — runs the `onboard-bank` consent flow and verifies the resulting session against live data. Use when asked to connect, link, onboard, authorize, refresh, renew, or reconnect a bank account (or a specific bank — DNB, Nordea, Revolut) for jb_gateway_mcp.
---

# Connecting or refreshing a bank account in jb_gateway_mcp

`bank.*` tools (`list_accounts`, `get_balance`, `summarize_spending`,
`list_transactions_summary`, `list_transactions_detailed`) are backed by
**Enable Banking**, a licensed AISP aggregator — not a direct bank API
(direct PSD2 access requires being a regulated TPP with an eIDAS
certificate, not viable for a personal project; GoCardless, the original
choice, closed new signups in July 2025, hence Enable Banking).

**Prerequisite**: `jb_gateway_mcp` installed standalone (`onboard-bank` on
PATH — e.g. `uv tool install --python 3.13 git+https://github.com/jitheshb83/jb_gateway_mcp.git`)
and its `.mcp.json` server connected. See this plugin's README.

**Connect and refresh are the same operation.** There's no separate
"refresh" command — `onboard-bank --institution <alias>` both links a new
institution for the first time and re-authorizes one whose consent expired.
Every bank consent is SCA-backed (BankID login) and valid for **90 days**;
after that, tool calls raise `NeedsReconsentError` until re-run.

## Step 0 — check current status first (always safe, no live calls by default)

Before doing anything else, run this — it's a local keychain read, side
effect free. Run from this plugin's root directory (the directory
containing this skill's parent `skills/` folder and this plugin's
`pyproject.toml`):

```bash
uv run python skills/connect-bank-account/scripts/check_bank_status.py
```

Reports, per known institution alias: not connected / valid (with days
left) / EXPIRED. Add `--live` to also call `bank.get_balance` against the
real API for every currently-valid institution — a genuine end-to-end
check, at the cost of live calls. Use this output to decide whether the
user actually needs Step 3 below, or is already fine.

## No fallback to other tools

If a bank tool call errors, is denied, or an institution isn't onboarded,
the fix is this skill (connect/refresh) or `run-jb-gateway-mcp`'s
troubleshooting (in the jb_gateway_mcp repo) — never fabricate
balances/transactions or substitute another data source. There is no
native banking connector in this environment to accidentally fall back to,
but the same rule that applies to `connect-google-account` applies here:
don't guess at financial data.

## Prerequisites (human must have already done this once — first bank only)

An Enable Banking application, registered through their Control Panel. This
needs a browser and can't be automated:

1. `enablebanking.com/sign-in/` → sign in with email (magic link).
2. Control Panel → **API applications** → **Add a new application**.
   - Name: anything identifiable (e.g. `jb-gateway-mcp`).
   - Redirect URL: **exactly** `https://localhost:8080/callback` — Enable
     Banking requires `https://`, with no plain-http localhost exception
     (unlike Google), and this exact value is hardcoded as `_REDIRECT_URL`
     in `src/jb_gateway_mcp/cli/onboard_bank.py` (jb_gateway_mcp repo).
   - Privacy/Terms URL: required fields but **not validated** while the app
     stays in Restricted mode (own-accounts-only, which is what this
     project uses) — a placeholder like the repo's GitHub URL is fine.
   - Let the browser generate the private key (downloads as `<application-id>.pem`)
     rather than supplying your own — simplest, and the key never leaves
     the user's machine either way.
3. Store the `.pem` **outside any repo**, e.g.
   `~/.secrets/jb_gateway_mcp/enablebanking/<application-id>.pem` — same
   convention as the Google `client_secret.json`. Never paste its contents
   into chat.
4. **Critical, easy-to-miss step**: a freshly registered application starts
   in status **"Inactive"** — every API call, even `GET /aspsps`, returns
   `403 Forbidden` until you click **"Activate by linking accounts"** in
   the Control Panel and complete a bank login through *their* hosted UI
   (not this project's code) for at least one account. In Restricted mode,
   the API only ever serves accounts that have gone through this linking —
   so **do this once per institution** you plan to connect (DNB, Nordea,
   Revolut, ...), not just the first one, or `onboard-bank` will 403 for
   the others too.

If the user already has an application registered, ask for the
`application_id` and the `.pem` path rather than assuming a location.

## Supported institutions

Defined in `src/jb_gateway_mcp/cli/onboard_bank.py` (jb_gateway_mcp repo) as
`_INSTITUTION_COUNTRY`/`_INSTITUTION_NAME_HINT`:

| Alias | Country | ASPSP name hint |
|---|---|---|
| `dnb` | NO | `dnb` |
| `nordea` | NO | `nordea` |
| `revolut` | NO | `revolut` |

**Adding a new one**: add an alias to both dicts in the jb_gateway_mcp repo
(country code + a lowercase substring/exact hint to match against
`GET /aspsps?country=<cc>`'s `name` field). Ask the user which country the
account is registered under if it's not obvious (e.g. Revolut/Wise-style
multi-country providers) — don't assume, it determines which ASPSP entry
gets matched. The resolver in `_resolve_aspsp` tries an **exact**
case-insensitive name match first, and only falls back to substring
matching (with an ambiguity error if that's still not unique) — this
exists because Enable Banking's Norway list has both `"DNB"` and
`"DNB Corporate Mastercard"`, which a naive substring match can't tell
apart.

## Step 1 — collect inputs

- **Institution alias** (from the table above, or a new one — see above).
- If Step 0 showed no app credential stored: **`application_id`** and the
  **path to the `.pem`** (first bank only — every institution after that
  reuses the stored app credential automatically).

## Step 2 — run the flow yourself; this cannot be run end-to-end via a tool call

`onboard-bank` calls `input()` partway through to wait for the human to
paste back a URL after completing the bank login in their browser — there
is no way to relay that from a browser action back into a running command's
stdin through automated tool calls. **Hand the exact command to the user
and wait for them to report the result** — do not attempt to script around
this (e.g. piping a guessed value into stdin); the code is single-use and
tied to a real SCA login only the human can complete.

```bash
onboard-bank --institution <alias> \
  --application-id <uuid> --private-key <path/to/key.pem>   # first bank only
```

For every institution after the first, drop the last two flags — the app
credential is already stored:

```bash
onboard-bank --institution <alias>
```

Tell the user what to expect:
1. It prints a bank login URL and opens it in their browser.
2. They log in (BankID/SCA) and approve consent.
3. The browser **fails to load** the final redirect page — expected,
   nothing is listening on `https://localhost:8080/callback`.
4. They copy the full URL from the address bar (contains `?code=...`) and
   paste it at the terminal prompt.
5. It prints e.g. `dnb onboarded: 1 account(s) linked, consent valid until
   2026-10-30` — never a secret value.

If it fails with `multiple ASPSPs matched institution=...: <names>` — an
institution genuinely is ambiguous by name in that country; report the
candidate names to the user and ask which one, then narrow
`_INSTITUTION_NAME_HINT` for that alias (e.g. to the exact name) and retry.

If it fails with `403 Forbidden` on the very first call
(`GET /aspsps`) — the application (or that specific institution) hasn't
been through **"Activate by linking accounts"** yet; see Prerequisites
step 4.

## Step 3 — verify against live data

Re-run Step 0's script with `--live`, or call the tools directly:

```bash
uv run python skills/connect-bank-account/scripts/check_bank_status.py --live
```

Or, if this session already has `jb-gateway-mcp` connected with the updated
tool list, call `bank.list_accounts`/`bank.get_balance` directly for the
institution and confirm it returns real data instead of `no bank consent
stored for institution=...`.

## Refreshing (re-running for an already-connected institution)

Exactly Step 2 again, for the same alias, no new flags needed. The old
session is overwritten with a fresh 90-day one. There's no way to "extend"
a live session without a new SCA login — PSD2 requires it.

## Multiple institutions

Repeat Steps 1–3 per institution. The app credential (Step 1's
`application_id`/`.pem`) is shared across all institutions and only
provided once; sessions are stored per institution and don't interfere
with each other.
