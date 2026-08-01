"""Status check for Enable Banking credentials — no bank login required.

Reports, for the app-level credential and every institution alias known to
`onboard-bank` (`src/jb_gateway_mcp/cli/onboard_bank.py` in the jb_gateway_mcp
repo): not connected / connected+valid (with expiry and account count) /
expired (needs a re-run of `onboard-bank`). This is a local keychain read
only — no network call — so it's safe to run anytime, including as a first
step before deciding whether a "refresh" is actually needed.

Requires jb_gateway_mcp installed standalone (`onboard-bank` on PATH) and
this plugin's own dependency on the jb_gateway_mcp library (see
pyproject.toml) for the direct adapter imports below.

Run from this plugin's root directory (two levels above this file):
    uv run python skills/connect-bank-account/scripts/check_bank_status.py

Pass --live to additionally call `bank.list_accounts`/`bank.get_balance`
against the real Enable Banking API for every currently-valid institution —
a real (not just local) end-to-end check, at the cost of live API calls.
    uv run python skills/connect-bank-account/scripts/check_bank_status.py --live
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from jb_gateway_mcp.adapters import enable_banking
from jb_gateway_mcp.cli.onboard_bank import _INSTITUTION_COUNTRY
from jb_gateway_mcp.credentials_bank import BankCredentialNotFoundError, BankCredentialStore


def main() -> int:
    live = "--live" in sys.argv[1:]
    store = BankCredentialStore()

    try:
        app = store.get_app_credential()
        print(f"[app credential] present — application_id={app.application_id}")
    except BankCredentialNotFoundError:
        print("[app credential] NOT stored — run onboard-bank with --application-id/--private-key")
        print("\nNo app credential means no institution can be checked further. Stopping.")
        return 1

    any_expired = False
    for institution in sorted(_INSTITUTION_COUNTRY):
        try:
            session = store.get_session(institution)
        except BankCredentialNotFoundError:
            print(f"[{institution}] not connected — run: onboard-bank --institution {institution}")
            continue

        now = datetime.now(UTC)
        if now >= session.valid_until:
            any_expired = True
            print(
                f"[{institution}] EXPIRED at {session.valid_until.date().isoformat()} "
                f"— run: onboard-bank --institution {institution}"
            )
            continue

        days_left = (session.valid_until - now).days
        print(
            f"[{institution}] valid — {len(session.accounts)} account(s), "
            f"expires {session.valid_until.date().isoformat()} ({days_left} days left)"
        )

        if live:
            try:
                balances_by_account = {
                    account.uid: enable_banking.get_balance(store, institution, account.uid)
                    for account in session.accounts
                }
                for account in session.accounts:
                    balances = balances_by_account[account.uid]
                    summary = ", ".join(f"{b['amount']} {b['currency']}" for b in balances)
                    print(f"    live check OK — {account.name} ({account.currency}): {summary}")
            except Exception as exc:  # noqa: BLE001 - report any live-check failure, don't hide it
                print(f"    live check FAILED: {type(exc).__name__}: {exc}")

    if any_expired:
        print("\nAt least one institution needs re-consent — see EXPIRED lines above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
