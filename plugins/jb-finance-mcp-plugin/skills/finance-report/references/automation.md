# Monthly automation

The launchd job, the status email, and the macOS gotchas. Read this only
when installing, verifying or troubleshooting the unattended monthly run
— an on-demand report never touches any of it.

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
