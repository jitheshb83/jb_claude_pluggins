"""Loan/financing details from a manually-maintained Google Sheet.

Enable Banking (this plugin's only bank data source) doesn't expose loan or
mortgage accounts — PSD2's Account Information Service scope is legally
limited to payment accounts, confirmed live against this user's own DNB/
Nordea consents. Loan details are tracked by hand in a Google Sheet instead
(see jb_gateway_mcp's project memory "Loan Tracker sheet" for the full
story) and read here, cached locally with a 1-day TTL so a report doesn't
re-fetch on every run.

Deliberately bypasses jb_gateway_mcp's `adapters.google_drive.read_file` —
the installed version hardcodes `text/plain` as the export mimetype for
every Google Workspace file type, but Drive's export API rejects
`text/plain` for spreadsheets specifically (`text/csv` is correct there).
Calling `build_google_client` + `files().export(mimeType="text/csv")`
directly here avoids depending on that fix landing upstream first.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jb_gateway_mcp.adapters.base import build_google_client
from jb_gateway_mcp.credentials import CredentialStore

_CACHE_FILENAME = "loan_tracker_cache.json"
_CACHE_KEY = "loan_tracker"
_TTL_SECONDS = 24 * 60 * 60
_EXPORT_MIMETYPE = "text/csv"

_AMOUNT_KEEP_RE = re.compile(r"[^0-9.,\-]")

_PARSED_NUMERIC_FIELDS = (
    "outstanding_balance",
    "monthly_payment",
    "original_amount",
)


def _cache_path(out_dir: Path) -> Path:
    return out_dir / "data" / _CACHE_FILENAME


def _parse_amount(raw: str | None) -> float | None:
    """Parse a currency-symbol-prefixed amount, tolerant of either
    thousands-separator convention (US "393,507.39" or EU "393.507,39").

    Whichever of '.'/',' appears last is treated as the decimal point only
    if it's followed by exactly 1-2 digits (a plausible fraction) — every
    other '.'/',' before it is a thousands separator and gets dropped.
    A 3-digit trailing group (e.g. "12.345") is treated as thousands, not a
    fraction — real currency fractions are 1-2 digits, thousands groups are
    exactly 3, so this is the safer default for the ambiguous case.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _AMOUNT_KEEP_RE.sub("", raw.strip())
    if not cleaned or cleaned in ("-", ".", ","):
        return None
    decimal_pos = max(cleaned.rfind("."), cleaned.rfind(","))
    trailing_digits = len(cleaned) - decimal_pos - 1
    if decimal_pos > 0 and trailing_digits in (1, 2):
        integer_part = re.sub(r"[.,]", "", cleaned[:decimal_pos])
        cleaned = f"{integer_part}.{cleaned[decimal_pos + 1:]}"
    else:
        cleaned = cleaned.replace(",", "").replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_rate(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    return _parse_amount(raw.replace("%", ""))


def _parse_rows(content: str) -> list[dict[str, Any]]:
    # Google's CSV export occasionally prefixes a UTF-8 BOM on the first
    # header cell, which would corrupt e.g. "institution" into
    # "﻿institution" and silently break that column's lookups.
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    rows: list[dict[str, Any]] = []
    for raw_row in reader:
        row = dict(raw_row)
        for field in _PARSED_NUMERIC_FIELDS:
            row[field] = _parse_amount(row.get(field))
        row["interest_rate_pct"] = _parse_rate(row.get("interest_rate_pct"))
        rows.append(row)
    return rows


def _load_cache(out_dir: Path) -> dict[str, Any] | None:
    path = _cache_path(out_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(_CACHE_KEY)


def _write_cache(out_dir: Path, account: str, file_id: str, content: str) -> None:
    path = _cache_path(out_dir)
    payload = {
        _CACHE_KEY: {
            "source_account": account,
            "source_file_id": file_id,
            "content": content,
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    }
    path.write_text(json.dumps(payload, indent=2))


def _cache_fresh_enough(entry: dict[str, Any]) -> bool:
    fetched_at = datetime.fromisoformat(entry["fetched_at"])
    return (datetime.now(UTC) - fetched_at).total_seconds() < _TTL_SECONDS


def fetch_loan_details(
    out_dir: Path,
    account: str | None = None,
    file_id: str | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Loan rows from the Loan Tracker sheet, cached locally for 1 day.

    Reuses `source_account`/`source_file_id` from the existing cache file
    when not passed explicitly. Returns `[]` (with a warning, never a raised
    exception) if nothing is available to fetch or the live call fails —
    a missing loan sheet should never block the rest of the report.
    """
    cached = _load_cache(out_dir)
    effective_account = account or (cached or {}).get("source_account")
    effective_file_id = file_id or (cached or {}).get("source_file_id")

    if effective_account is None or effective_file_id is None:
        print(
            "  [loan warning] no loan sheet account/file_id given and none cached — "
            "skipping Loan Details",
            file=sys.stderr,
        )
        return []

    if cached and not refresh and _cache_fresh_enough(cached):
        content = cached["content"]
    else:
        try:
            client = build_google_client(CredentialStore(), effective_account, "drive", "v3")
            content = (
                client.files()
                .export(fileId=effective_file_id, mimeType=_EXPORT_MIMETYPE)
                .execute()
            )
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - report, don't sink the rest of the report
            print(f"  [loan warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            return _parse_rows(cached["content"]) if cached else []
        _write_cache(out_dir, effective_account, effective_file_id, content)

    return _parse_rows(content)
