# jb_claude_pluggins

A Claude Code plugin marketplace hosting skills and agents that pair with
[jb_gateway_mcp](https://github.com/jitheshb83/jb_gateway_mcp) — a
standalone, credential-holding MCP gateway to Google APIs (Gmail, Calendar,
Drive) and read-only bank data (Enable Banking: DNB, Nordea, Revolut).

`jb_gateway_mcp` itself lives in its own repo and stays a plain,
standalone MCP server — no plugin-specific code in it. The plugins here
just package skills/agents on top of it and declare how to launch it.
Nothing in either repo carries credentials or account data; every user
connects their own accounts locally.

## Plugins in this marketplace

| Plugin | Purpose |
|---|---|
| [`jb-finance-mcp-plugin`](plugins/jb-finance-mcp-plugin/) | Bank account onboarding + local HTML finance reports |
| [`jb-google-notify-plugin`](plugins/jb-google-notify-plugin/) | Google account onboarding + report/notification delivery via Gmail |

Each plugin is independently installable and depends only on
`jb_gateway_mcp` being installed standalone — not on each other. See each
plugin's own README for setup.

## Install

```
/plugin marketplace add jitheshb83/jb_claude_pluggins
/plugin install jb-finance-mcp-plugin@jb-claude-plugins
/plugin install jb-google-notify-plugin@jb-claude-plugins
/reload-plugins
```

Both plugins declare the same `jb-gateway-mcp` MCP server command — if you
install both, Claude Code connects to it once (dedup by matching command),
not twice.

## Prerequisite: jb_gateway_mcp

Neither plugin bundles the MCP server itself — install it standalone first
so `jb-gateway-mcp`, `onboard-google`, `onboard-bank`, and
`uninstall-google` are on your `PATH`:

```bash
uv tool install --python 3.13 git+https://github.com/jitheshb83/jb_gateway_mcp.git
```

Then create a policy file — the server won't start without one, even to
serve `ping`, and neither plugin's `.mcp.json` overrides the default path:

```bash
mkdir -p ~/.jb_gateway_mcp
echo 'callers: {}' > ~/.jb_gateway_mcp/policy.yaml
```

That's a valid, safe, deny-everything starting point, shared by both
plugins (they declare the identical server command). Then follow
[jb_gateway_mcp's README](https://github.com/jitheshb83/jb_gateway_mcp#readme)
for the one-time Google OAuth client / Enable Banking application
registration, `policy.yaml` grants, and connecting your first account (or
just ask Claude — each plugin's `connect-*` skill walks through it).
