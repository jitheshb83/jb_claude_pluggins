# jb-finance-mcp-plugin

Claude Code plugin: bank account onboarding (Enable Banking — DNB, Nordea,
Revolut, or any Enable Banking-supported institution) and local HTML
finance-report generation, for [jb_gateway_mcp](https://github.com/jitheshb83/jb_gateway_mcp).

This plugin does **not** carry any credentials, bank app registration, or
account data. Every user installing it must connect their own accounts
locally — see below.

## What's in here

- **`skills/connect-bank-account`** — onboard/refresh an Enable Banking
  institution (90-day SCA consent).
- **`skills/finance-report`** — generate a local HTML spending/income
  report with a next-month forecast, cached under `~/Documents/MyFinance/`;
  optional monthly `launchd` automation with an email status notification.
  Also includes a "Loan details" section (balance, rate, next payment) from
  a manually-maintained Google Sheet — Enable Banking doesn't expose loan/
  mortgage accounts, so this data isn't pulled from the bank tools; see
  "Loan details" in `skills/finance-report/SKILL.md`.
- **`agents/finance-analyst`** — a subagent scoped to spending analysis,
  balance checks, and forecasts using the `bank.*` tools.
- **`.mcp.json`** — declares the `jb-gateway-mcp` server (see prerequisite
  below).

## Prerequisites

1. **Install `jb_gateway_mcp` standalone** so `jb-gateway-mcp`,
   `onboard-bank`, `onboard-google`, and `uninstall-google` land on your
   `PATH`:

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
   it to allow the `bank.*` tools for the caller id this `.mcp.json` uses
   (`local` by default). See the jb_gateway_mcp README §7c for the
   bank-tool grant shape.

4. **Connect a bank account** — run this plugin's `connect-bank-account`
   skill (or ask Claude: "connect my DNB account").

Steps 1–3 are shared with `jb-google-notify-plugin` — both plugins declare
the identical `jb-gateway-mcp` server command, so if you've already set
these up for one, you're done for both; just add the `bank.*` grants here.

## Install this plugin

```
/plugin marketplace add jitheshb83/jb_claude_pluggins
/plugin install jb-finance-mcp-plugin@jb-claude-plugins
/reload-plugins
```

## This plugin's own scripts

The skill scripts (`check_bank_status.py`, `generate_report.py`, ...) import
`jb_gateway_mcp` as a library directly, for direct-adapter calls without an
MCP session (see each script's docstring). This plugin's own
`pyproject.toml` declares that dependency separately from the standalone
`uv tool install` above — the two are independent installs of the same
package, which is expected. Run them via `uv run python skills/.../script.py`
from this plugin's root directory (wherever it ends up installed/cloned).

## Notes

- Bank tools are read-only by design — see jb_gateway_mcp's own README/
  DESIGN.md for the full security model.
- The monthly `launchd` automation and its email notification are optional
  and require manual setup per machine — see `skills/finance-report/SKILL.md`
  "Automating it monthly."
