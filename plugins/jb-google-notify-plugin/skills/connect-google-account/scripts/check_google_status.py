"""Status check for Google account credentials — no browser needed.

Unlike bank institutions (a fixed, enumerable list in onboard_bank.py),
Google accounts are arbitrary emails with no registry in this codebase, so
there's nothing to auto-discover — pass every account you care about via
--account (repeatable), or rely on the default below.

Reports, per account: not connected / valid (refreshing the access token
via the stored refresh token if it's near expiry — this IS what every
adapter call already does automatically; running this script just makes
that check visible without waiting for a real tool call to hit it) /
needs re-consent (refresh token itself is revoked or expired — the one
case that genuinely requires a human to redo the browser OAuth flow, see
this skill's "Refreshing vs. connecting" section).

Requires jb_gateway_mcp installed standalone (`onboard-google` on PATH) and
this plugin's own dependency on the jb_gateway_mcp library (see
pyproject.toml) for the direct credential-store imports below.

Run from this plugin's root directory (two levels above this file):
    uv run python skills/connect-google-account/scripts/check_google_status.py
    uv run python skills/connect-google-account/scripts/check_google_status.py \\
        --account someone@example.com --account another@example.com
"""

from __future__ import annotations

import argparse
import sys

from jb_gateway_mcp.credentials import CredentialNotFoundError, CredentialStore
from jb_gateway_mcp.token_lifecycle import NeedsReconsentError, TokenRefreshError

# No default account is baked in here (unlike the private original this was
# adapted from) — pass --account explicitly, since a published plugin has no
# way to know which Google account you've onboarded.
_DEFAULT_ACCOUNTS: list[str] = []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--account",
        action="append",
        dest="accounts",
        default=None,
        help="Google account email to check (repeatable). Required if no default is configured.",
    )
    args = parser.parse_args(argv)
    accounts = args.accounts or _DEFAULT_ACCOUNTS
    if not accounts:
        parser.error("pass at least one --account (no default account configured)")

    store = CredentialStore()
    any_needs_reconsent = False

    for account in accounts:
        try:
            store.get_valid_token("google", account)
        except CredentialNotFoundError:
            print(
                f"[{account}] not connected — run: onboard-google "
                f"--account {account} --client-secrets <path>"
            )
            continue
        except NeedsReconsentError:
            any_needs_reconsent = True
            print(
                f"[{account}] NEEDS RE-CONSENT — refresh token revoked/expired. "
                f"Run: onboard-google --account {account} --client-secrets <path>"
            )
            continue
        except TokenRefreshError as exc:
            print(f"[{account}] refresh FAILED (transient?): {exc}")
            continue

        print(f"[{account}] valid (access token fresh or refreshed successfully)")

    if any_needs_reconsent:
        print("\nAt least one account needs re-consent — see NEEDS RE-CONSENT lines above.")
        print(
            "This requires a real browser login; nothing can automate it away "
            "(Google's design, not this project's)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
