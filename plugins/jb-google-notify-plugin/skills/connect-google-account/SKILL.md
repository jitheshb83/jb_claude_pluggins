---
name: connect-google-account
description: Connects/onboards a real Google account (Gmail, Calendar, Drive) to jb_gateway_mcp, or refreshes/reconnects one whose stored grant was revoked — runs the OAuth consent flow (`onboard-google`) and adds the matching grants to policy.yaml so the tools actually work end-to-end. Includes a status-check script (no browser needed) to see whether an account needs re-consent before a tool call fails. Use when asked to connect, link, onboard, authorize, hook up, refresh, renew, or reconnect a Google/Gmail/Drive/Calendar account for jb_gateway_mcp, or to check/troubleshoot whether a Google token is still valid.
---

# Connecting a Google account to jb_gateway_mcp

This wires up a **real** Google account so `gmail.*`, `calendar.*`, and
`drive.*` tool calls return live data instead of
`no credential stored for provider='google' account=...`. It's two
separate steps that both have to happen:

1. **`onboard-google`** — one-time OAuth consent flow, stores a refresh
   token in the OS keychain.
2. **`policy.yaml` grants** — even with a valid token, every
   caller/tool pair is denied until it's explicitly granted (deny-by-default).

Skipping step 2 is the most common "it onboarded fine but the tool call
still fails" case — check policy.yaml if that happens.

**Prerequisite**: `jb_gateway_mcp` installed standalone (`onboard-google`
on PATH — e.g.
`uv tool install --python 3.13 git+https://github.com/jitheshb83/jb_gateway_mcp.git`)
and its `.mcp.json` server connected. See this plugin's README. If you're
onboarding an account specifically so `jb-finance-mcp-plugin`'s
`finance-report` skill can email a monthly status notification, you need
the `gmail.send` scope granted below — that plugin's `run_monthly.sh`
expects the sending account to already be connected here.

## Refreshing vs. connecting

Unlike bank consent (`connect-bank-account`, in `jb-finance-mcp-plugin`,
SCA-backed, expires every 90 days by design), a Google refresh token
doesn't expire on a schedule — `CredentialStore.get_valid_token`
(`src/jb_gateway_mcp/credentials.py`, jb_gateway_mcp repo) silently mints a
new access token from it on every tool call, no human action ever needed
in the common case. **This is already fully automatic — there is nothing
to build or configure for it.** A manual **re-run of `onboard-google` is
only needed** if the refresh token itself was revoked (user removed the
app's access at myaccount.google.com/permissions, the OAuth client is
still in Google's "Testing" publishing status — refresh tokens for test
apps expire after 7 days regardless of use, a common surprise — or it
naturally expired after 6 months of no use) — that surfaces as
`NeedsReconsentError` from `src/jb_gateway_mcp/token_lifecycle.py` on a
tool call, not as a background failure. **This one case genuinely cannot
be automated** — Google requires a real browser login for it, by design,
not a gap in this project.

To check without waiting for a tool call to fail, run from this plugin's
root directory (the directory containing this skill's parent `skills/`
folder and this plugin's `pyproject.toml`):

```bash
uv run python skills/connect-google-account/scripts/check_google_status.py --account <email>
```

Reports, per account (`--account` is repeatable; there's no default — pass
every account you care about): not connected / valid (refreshing the
access token if it was near expiry, same as any real tool call would) /
needs re-consent. It's a local-ish check (only talks to Google's token
endpoint if a refresh is actually due) — safe to run anytime, including as
a first step before assuming something's broken.

If it says revoked/needs reconsent, **Steps 3–5 below are the fix** —
same command as a first-time connect, for the same `--account`. It
overwrites the stored token; no `policy.yaml` change is needed unless the
scopes are also changing.

## No fallback to other tools

If a `jb-gateway-mcp` tool call errors, is denied, or the account isn't
onboarded yet, the fix is to onboard/fix the gateway (this skill) or the
`run-jb-gateway-mcp` skill's troubleshooting (jb_gateway_mcp repo) — never
to silently substitute another data source for the same request. In
particular, this environment may also expose native `claude_ai_Google_*`
connectors (`Gmail`, `Google Calendar`, `Google Drive`). Those talk to
whatever Google account they're separately authorized against and bypass
this project's `policy.yaml` grants and audit log entirely — using them as
a stand-in defeats the reason this gateway exists (deny-by-default access
control + an auditable trail) and may return the wrong account's data. If
`jb-gateway-mcp` genuinely can't be made to work, stop and tell the user
what's broken — don't route the request through an unaudited alternative
without asking first.

## Prerequisites (human must have already done this)

A Google Cloud OAuth client + downloaded `client_secret.json`. If the user
doesn't have this yet, walk them through the jb_gateway_mcp repo's
README.md §2 rather than guessing — it requires a browser and a Google
Cloud Console project, which can't be done from the CLI:

1. console.cloud.google.com → create/pick a project.
2. APIs & Services → Library → enable Gmail API, Google Calendar API,
   Google Drive API.
3. APIs & Services → OAuth consent screen → configure (External is fine
   for personal use; add the account as a test user if the app stays in
   "Testing" mode).
4. APIs & Services → Credentials → Create Credentials → OAuth client ID →
   **Desktop app** → download the JSON.
5. Store that file **outside any repo** (e.g.
   `~/.secrets/jb_gateway_mcp/client_secret.json`). Never place it inside
   a project directory.

If the user already has this file, ask for its path rather than guessing a
location.

## Step 1 — collect inputs

Ask (don't assume):

- **Account email** to onboard.
- **Path to `client_secret.json`.**
- **Scopes**: read-only only (default), or also send/write? Mapping:

  | Capability | Scope | Tool(s) it unlocks |
  |---|---|---|
  | Gmail read | `gmail.readonly` | `gmail.list_messages`, `gmail.read_message` |
  | Gmail send | `gmail.send` | `gmail.send_message` (write — not default) |
  | Calendar read | `calendar.readonly` | `calendar.list_events` |
  | Calendar write | `calendar.events` | `calendar.create_event` (write — not default) |
  | Drive read | `drive.readonly` | `drive.list_files`, `drive.read_file` |

  Only request write scopes if the user explicitly wants the agent able to
  send mail / create events on their behalf — flag it as a write
  capability before adding it.

## Step 2 — check port 8080 is free

`onboard-google` briefly starts a local HTTP server on `localhost:8080` to
catch the OAuth redirect. If something else is bound to it, the command
will fail.

```bash
lsof -i :8080
```

If occupied, tell the user rather than killing an unknown process for them.

## Step 3 — run the onboarding flow

This is **interactive and blocking** — it opens a real browser window for
the user to log in and click "Allow". Run it in the foreground (not
`run_in_background`) and tell the user to complete the consent screen:

```bash
onboard-google \
  --account <email> \
  --client-secrets <path-to-client_secret.json>
```

Add `--scopes` explicitly only if write scopes were requested in Step 1,
e.g.:

```bash
  --scopes \
    https://www.googleapis.com/auth/gmail.readonly \
    https://www.googleapis.com/auth/gmail.send \
    https://www.googleapis.com/auth/calendar.readonly \
    https://www.googleapis.com/auth/drive.readonly
```

On success it prints the account and granted scopes — **never a token
value**. If it prints a `RuntimeError` about a missing refresh token,
that's a known Google quirk (it omits the refresh token on repeat consent
for the same account/app) — re-run the command.

## Step 4 — add the policy.yaml grants

The token existing is not enough — read `policy.yaml` first, then add an
`allow` entry for each tool matching the scopes actually granted in Step 3,
under the caller id in use (`local` by default, or whatever
`JB_GATEWAY_CALLER_ID` is set to for the target client — check
`.mcp.json` / the client's config if unsure). Use the scope→tool mapping
from Step 1. Read-only example:

```yaml
callers:
  local:
    allow:
      - tool: gmail.list_messages
        scope: gmail.readonly
      - tool: gmail.read_message
        scope: gmail.readonly
      - tool: calendar.list_events
        scope: calendar.readonly
      - tool: drive.list_files
        scope: drive.readonly
      - tool: drive.read_file
        scope: drive.readonly
```

Don't add grants for tools/scopes that weren't actually requested in Step
3 — a grant for a scope the token doesn't have will just fail at the
Google API call, not at the policy check.

Show the diff and confirm before considering this step done if the change
adds write scopes (`gmail.send_message`, `calendar.create_event`) — those
let the agent send mail / create events on the user's behalf.

## Step 5 — verify against the real account

Prefer a direct MCP tool call against the connected account over editing
the shared smoke-test script. If the client already has this server
connected (e.g. this Claude Code session via `.mcp.json`), just call one of
the now-granted tools (e.g. `gmail.list_messages` or
`calendar.list_events`) with the onboarded account email and confirm it
returns real data instead of the credential/policy error.

Alternatively, for a scripted check outside the current session, the
jb_gateway_mcp repo's `run-jb-gateway-mcp` skill's smoke test can be
pointed at a real account by editing `ACCOUNT` in `scripts/smoke_test.py`
for a local one-off run. Don't commit a real account email into that
shared script.

## Multiple accounts

Repeat Steps 1–5 per account — `onboard-google` is safe to re-run for
different `--account` values; each gets its own keychain entry. Policy
grants in `policy.yaml` are per-caller, not per-account, so no extra step
is needed there unless the user wants different tool access per account
(not supported by the current single-caller-per-account-set policy shape —
flag this limitation if asked).

## Disconnecting

Not this skill's job — see the jb_gateway_mcp repo's README.md
§"Uninstalling" / `uninstall-google` (revokes the Google-side grant and
deletes the keychain entry).
