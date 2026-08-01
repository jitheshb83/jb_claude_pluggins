# jb-google-notify-plugin

Claude Code plugin: Google account onboarding and report/notification
delivery via Gmail, for [jb_gateway_mcp](https://github.com/jitheshb83/jb_gateway_mcp).

Scoped narrowly on purpose — this plugin (and its agent) is for **pushing
reports and status notifications by email**, not general inbox triage,
calendar management, or Drive access. The underlying `jb_gateway_mcp`
server exposes `calendar.*`/`drive.*`/full `gmail.*` tools too; this
plugin just doesn't wrap them in a persona.

This plugin does **not** carry any credentials or account data. Every user
installing it must connect their own Google account locally — see below.

## What's in here

- **`skills/connect-google-account`** — one-time OAuth consent flow +
  `policy.yaml` grants for a real Google account.
- **`agents/report-notifier`** — a subagent scoped to sending short
  notification emails via `gmail.send_message` — nothing else.
- **`.mcp.json`** — declares the `jb-gateway-mcp` server (identical to
  `jb-finance-mcp-plugin`'s — Claude Code dedupes to one running server
  process when both plugins are installed, since it matches by endpoint).

## Prerequisites

1. **Install `jb_gateway_mcp` standalone** so `jb-gateway-mcp` and
   `onboard-google` land on your `PATH`:

   ```bash
   uv tool install --python 3.13 git+https://github.com/jitheshb83/jb_gateway_mcp.git
   ```

2. **Create a policy file.** The server won't start without one, even to
   serve `ping` — its default is `~/.jb_gateway_mcp/policy.yaml`, and this
   plugin's `.mcp.json` doesn't override that:

   ```bash
   mkdir -p ~/.jb_gateway_mcp
   echo 'callers: {}' > ~/.jb_gateway_mcp/policy.yaml
   ```

3. **Grant policy access.** That file denies every tool by default — edit
   it to allow `gmail.send_message` (and any read scopes you want) for the
   caller id this `.mcp.json` uses (`local` by default). `gmail.send` is a
   write scope and isn't onboarded by default — request it explicitly
   during onboarding if you want notifications sent.

4. **Connect a Google account** — run this plugin's `connect-google-account`
   skill (or ask Claude: "connect my Google account for notifications").

Steps 1–3 are shared with `jb-finance-mcp-plugin` — both plugins declare
the identical `jb-gateway-mcp` server command, so if you've already set
these up for one, you're done for both; just add the `gmail.*` grants here.

## Install this plugin

```
/plugin marketplace add jitheshb83/jb_claude_pluggins
/plugin install jb-google-notify-plugin@jb-claude-plugins
/reload-plugins
```

## This plugin's own scripts

`check_google_status.py` imports `jb_gateway_mcp` as a library directly for
a local keychain status check without an MCP session. This plugin's own
`pyproject.toml` declares that dependency separately from the standalone
`uv tool install` above — the two are independent installs of the same
package, which is expected. Run it via
`uv run python skills/connect-google-account/scripts/check_google_status.py --account <email>`
from this plugin's root directory.

## Relationship to jb-finance-mcp-plugin

`jb-finance-mcp-plugin`'s monthly finance-report automation sends its own
status email directly (via `jb_gateway_mcp.adapters.google_gmail`, not
through this plugin's agent — it runs unattended, with no live Claude
session). It requires the same Google account, connected here, with
`gmail.send` granted. The two plugins stay independently installable; this
is a shared-account dependency, not a code dependency.
