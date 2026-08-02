#!/bin/zsh
# Monthly finance-report runner — intended to be invoked by a launchd
# LaunchAgent on the 1st of each month, not run by hand (though it's safe
# to run manually any time; it just regenerates last month's report).
#
# Computes "last month" relative to today, calls generate_report.py for
# that exact calendar month, then emails a short status notification
# (success with headline numbers, or failure with the reason) via
# notify_email.py. Meant to run unattended, so everything is also logged
# to a file regardless of whether the email succeeds.
#
# Deliberately NOT `set -e`: a failed generate_report.py run must still
# reach the failure-notification email below it, not abort the script.
#
# Resolves its own location so it works wherever this plugin was actually
# installed/cloned, instead of a hardcoded path — unlike the private
# original this was adapted from, a published plugin can't assume where
# the user put it.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"   # scripts -> finance-report -> skills -> plugin root
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
OUT_DIR="$HOME/Documents/MyFinance"
LOG_DIR="$OUT_DIR/logs"
CURRENCY="NOK"
INSTITUTIONS="dnb,nordea,revolut"

# Notification addresses — edit these for your own setup, or set them via
# environment before invoking this script (e.g. from the launchd plist).
FROM_ACCOUNT="${FROM_ACCOUNT:-jithesh83@gmail.com}"
TO_ADDRESS="${TO_ADDRESS:-jithesh@jithonline.com}"

mkdir -p "$LOG_DIR"

# BSD date (macOS): first of this month, minus one day = last day of last
# month; first of that month = last month's start.
LAST_MONTH_END=$(date -v1d -v-1d +%Y-%m-%d)
LAST_MONTH_START=$(date -v1d -v-1d -v1d +%Y-%m-%d)
LABEL=$(date -v1d -v-1d +%Y-%m)

LOG_FILE="$LOG_DIR/${LABEL}-run-$(date +%Y%m%d-%H%M%S).log"

{
  echo "=== finance-report monthly run: $(date) ==="
  echo "Period: $LAST_MONTH_START .. $LAST_MONTH_END"
  cd "$PLUGIN_DIR"

  "$UV_BIN" run python skills/finance-report/scripts/generate_report.py \
    --from "$LAST_MONTH_START" --to "$LAST_MONTH_END" \
    --institutions "$INSTITUTIONS" --currency "$CURRENCY"
  REPORT_EXIT=$?
  echo "generate_report.py exit: $REPORT_EXIT"

  if [ "$REPORT_EXIT" -eq 0 ]; then
    # Read the actual path back out of this run's own log rather than
    # reconstructing it from $INSTITUTIONS — generate_report.py names the
    # file after whichever institutions actually succeeded, which can be a
    # subset of $INSTITUTIONS if one had an expired/missing session (still
    # exit 0 as long as at least one institution came through).
    REPORT_PATH=$(grep "^Wrote report: " "$LOG_FILE" | tail -1 | sed 's/^Wrote report: //')
    "$UV_BIN" run python skills/finance-report/scripts/notify_email.py \
      --status success --label "$LABEL" --currency "$CURRENCY" \
      --out-dir "$OUT_DIR" --report-path "$REPORT_PATH" \
      --from-account "$FROM_ACCOUNT" --to-address "$TO_ADDRESS"
  else
    "$UV_BIN" run python skills/finance-report/scripts/notify_email.py \
      --status failure --label "$LABEL" --currency "$CURRENCY" \
      --out-dir "$OUT_DIR" --log-path "$LOG_FILE" \
      --from-account "$FROM_ACCOUNT" --to-address "$TO_ADDRESS" \
      --detail "generate_report.py exited $REPORT_EXIT — see log for the actual error"
  fi

  echo "=== done: $(date) ==="
} >> "$LOG_FILE" 2>&1

# Report generation's own exit code is what launchd should see as this
# job's result — the notification step succeeding or failing shouldn't
# mask (or fake) that signal either way.
exit "$REPORT_EXIT"
