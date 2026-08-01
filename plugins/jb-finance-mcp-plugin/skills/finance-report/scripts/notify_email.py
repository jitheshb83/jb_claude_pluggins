"""Email notification for the finance-report monthly automation.

Standalone from generate_report.py on purpose: ad-hoc/manual report runs
(e.g. Claude generating a report mid-conversation) should never trigger an
email — only the scheduled monthly automation (run_monthly.sh) calls this,
after generate_report.py has already run and either succeeded or failed.

Sends via the Gmail adapter directly (jb_gateway_mcp.adapters.google_gmail),
the same direct-call pattern generate_report.py uses for the bank adapter —
not through the MCP protocol, so this runs standalone with no client
session. Requires gmail.send_message already granted to the relevant caller
in policy.yaml (see the jb-google-notify-plugin's connect-google-account
skill) — this script reuses that same authorization, not a new grant.

Deliberately a SHORT status email (headline numbers on success, the
failure reason + remediation hint on failure) — not the full report or any
transaction-level detail, so sensitive financial detail doesn't get
duplicated into an email inbox beyond what's already necessary. The full
report always stays local; the email just says a new one exists and
whether it worked.

--from-account/--to-address are required — no default account is baked in
here, since a published plugin has no way to know which Google account
you've onboarded or where you want notifications sent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_report import monthly_summaries_by_currency  # noqa: E402

from jb_gateway_mcp.adapters.google_gmail import send_message
from jb_gateway_mcp.credentials import CredentialNotFoundError, CredentialStore
from jb_gateway_mcp.token_lifecycle import NeedsReconsentError


def build_success_body(out_dir: Path, label: str, currency: str, report_path: str) -> str:
    data_path = out_dir / "data" / f"{label}-transactions.json"
    dataset = json.loads(data_path.read_text())
    monthly = monthly_summaries_by_currency(dataset)
    month = sorted(monthly.get(currency, {}))[-1]
    figures = monthly[currency][month]
    savings_rate = figures["net"] / figures["income"] * 100 if figures["income"] else 0.0

    return (
        f"Finance report generated for {month} ({currency}).\n\n"
        f"Income:       {figures['income']:,.0f}\n"
        f"Expenses:     {figures['true_expense']:,.0f}\n"
        f"Net:          {figures['net']:,.0f}\n"
        f"Savings rate: {savings_rate:+.1f}%\n\n"
        f"Full report (local file on this machine): {report_path}\n"
        "Not attached or inlined here by design — open it locally for the\n"
        "full category breakdown, income splits, and next-month forecast."
    )


def build_failure_body(label: str, detail: str, log_path: str | None) -> str:
    return (
        f"The automated finance report for {label} did not complete.\n\n"
        f"Reason: {detail or 'unknown — see log'}\n\n"
        f"Log: {log_path or '(not provided)'}\n\n"
        "If this is a bank consent expiry (90-day Enable Banking consent),\n"
        "re-run (from anywhere, jb_gateway_mcp installed standalone):\n"
        "  onboard-bank --institution dnb\n"
        "  onboard-bank --institution nordea"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", choices=["success", "failure"], required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--currency", default="NOK")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--from-account", required=True, help="Onboarded Gmail sender account")
    parser.add_argument("--to-address", required=True, help="Notification recipient address")
    parser.add_argument("--report-path", default=None, help="required if --status success")
    parser.add_argument("--log-path", default=None)
    parser.add_argument("--detail", default="")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()

    if args.status == "success":
        if not args.report_path:
            print("--report-path is required for --status success", file=sys.stderr)
            return 1
        subject = f"[jb_gateway_mcp] Finance report ready — {args.label}"
        body = build_success_body(out_dir, args.label, args.currency, args.report_path)
    else:
        subject = f"[jb_gateway_mcp] Finance report FAILED — {args.label}"
        body = build_failure_body(args.label, args.detail, args.log_path)

    store = CredentialStore()
    try:
        send_message(store, args.from_account, args.to_address, subject, body)
    except CredentialNotFoundError:
        print(
            f"No Google credential stored for {args.from_account} — email not sent. "
            "Run the connect-google-account skill.",
            file=sys.stderr,
        )
        return 1
    except NeedsReconsentError:
        # The one failure this whole notification system can't email about:
        # if Gmail's own refresh token is what's broken, there's no channel
        # left but the log. Print something unambiguous instead of a raw
        # traceback, so `grep` on the log file finds it immediately.
        print(
            f"Google refresh token for {args.from_account} needs re-consent — email not sent. "
            f"Run: onboard-google --account {args.from_account} --client-secrets <path> "
            "--scopes https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send "
            "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/drive.readonly",
            file=sys.stderr,
        )
        return 1
    print(f"Notification email sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
