"""Generate a local personal-finance report (JSON data + HTML) for one or
more linked bank institutions, and cache both under ~/Documents/MyFinance/.

Fetches transactions directly via stored Enable Banking credentials — the
same path `check_bank_status.py --live` and the `bank.*` MCP tools use — so
it works standalone, without an MCP client session. Read-only: never touches
onboarding, consent, or payment-initiation code paths.

Run from this plugin's root directory (two levels above this file):
    uv run python skills/finance-report/scripts/generate_report.py \\
        --from 2026-07-01 --to 2026-07-31

Options:
    --institutions dnb,nordea   default: every institution with a valid,
                                 non-expired stored session
    --currency NOK               which currency's accounts to chart in the
                                 HTML report (default NOK); other currencies
                                 still get fetched, cached, and a footnote
                                 stat line, just not full charts — mixing
                                 currencies into one number is never done
    --out-dir PATH                default ~/Documents/MyFinance
    --refresh                    ignore an existing cache file for this
                                 exact institutions+range and re-fetch live

See SKILL.md in this directory for the full write-up (categorization rules,
cache-reuse policy, known gotchas).
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from categories import categorize, dedupe_transactions  # noqa: E402
from forecast import predicted_expense_total, update_and_predict  # noqa: E402

from jb_gateway_mcp.adapters import enable_banking
from jb_gateway_mcp.cli.onboard_bank import _INSTITUTION_COUNTRY
from jb_gateway_mcp.credentials_bank import (
    BankCredentialNotFoundError,
    BankCredentialStore,
    NeedsReconsentError,
)

OUT_DIR_DEFAULT = Path.home() / "Documents" / "MyFinance"
CATEGORY_LABELS = {
    "mortgage": "Mortgage",
    "credit_card": "Credit card",
    "car_finance": "Car finance",
    "international_transfer": "Int'l transfer",
    "school_fees": "School fees",
    "insurance": "Insurance",
    "housing_fee": "Housing fee",
    "electricity": "Electricity",
    "telecom": "Telecom",
    "toll": "Toll/ferry",
    "parking": "Parking",
    "municipal_charge": "Municipal charge",
    "bank_fee": "Bank fee",
    "uncategorized": "Uncategorized",
    "salary": "Salary",
    "pension_benefit": "Pension/benefit",
    "dividend": "Dividend",
    "income_other": "Other income",
}

# Preference order for which balance line to report as "balance in hand" —
# Enable Banking returns several types per account; these usually agree
# exactly for a simple checking account, but prefer the most current one.
_BALANCE_TYPE_PREFERENCE = ["ITAV", "XPCD", "OPBD", "ITBD", "OTHR"]


# ---------------------------------------------------------------- fetching --


def _month_windows(date_from: str, date_to: str) -> list[tuple[str, str]]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = min(date(cursor.year, cursor.month, last_day), end)
        windows.append((cursor.isoformat(), month_end.isoformat()))
        cursor = date.fromordinal(month_end.toordinal() + 1)
    return windows


def fetch_deduped(
    store: BankCredentialStore, institution: str, account_uid: str, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """list_transactions_detailed, with a monthly-chunk fallback + dedupe.

    Enable Banking has occasionally 422'd on continuation pages for a wide
    date range on this account set (cause unconfirmed, upstream). Chunking
    by calendar month works around it but reintroduces duplicate boundary
    rows (date_to is inclusive on both adjacent windows) — dedupe_transactions
    cleans that up either way, so it's applied unconditionally.
    """
    try:
        raw = enable_banking.list_transactions_detailed(
            store, institution, account_uid, date_from, date_to
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422:
            raise  # only 422 (pagination quirk) is worth retrying by chunking; a
            # 429 rate-limit would just get worse from more requests, not better
        raw = []
        for w_from, w_to in _month_windows(date_from, date_to):
            raw.extend(
                enable_banking.list_transactions_detailed(
                    store, institution, account_uid, w_from, w_to
                )
            )
    return dedupe_transactions(raw)


def resolve_institutions(store: BankCredentialStore, requested: list[str] | None) -> list[str]:
    candidates = requested or sorted(_INSTITUTION_COUNTRY)
    resolved = []
    for institution in candidates:
        try:
            store.get_valid_session(institution)
        except (BankCredentialNotFoundError, NeedsReconsentError) as exc:
            print(f"  [skip] {institution}: {exc}", file=sys.stderr)
            continue
        resolved.append(institution)
    return resolved


def fetch_balance_total(
    store: BankCredentialStore, dataset: dict[str, Any], currency: str
) -> tuple[float, list[str]]:
    """Live balance lookup (always fresh, never cached) for every account of
    `currency` across the institutions in `dataset`. One account failing
    (expired session, rate limit, ...) doesn't block the rest — it's
    reported as a warning and skipped."""
    total = 0.0
    warnings: list[str] = []
    for institution, accounts in dataset["accounts"].items():
        for account in accounts:
            if account["currency"] != currency:
                continue
            try:
                balances = enable_banking.get_balance(store, institution, account["uid"])
            except Exception as exc:  # noqa: BLE001 - report, don't let one account sink the report
                warnings.append(f"{institution}/{account['name']}: {type(exc).__name__}: {exc}")
                continue
            chosen = None
            for btype in _BALANCE_TYPE_PREFERENCE:
                chosen = next((b for b in balances if b["type"] == btype), None)
                if chosen:
                    break
            if chosen is None and balances:
                chosen = balances[0]
            if chosen and chosen.get("amount") is not None:
                total += float(chosen["amount"])
    return round(total, 2), warnings


# ---------------------------------------------------------------- compute --


def month_key(date_str: str) -> str:
    return date_str[:7]


def next_month_key(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    mon += 1
    if mon > 12:
        mon, year = 1, year + 1
    return f"{year:04d}-{mon:02d}"


def build_dataset(
    store: BankCredentialStore, institutions: list[str], date_from: str, date_to: str
) -> dict[str, Any]:
    accounts_by_institution: dict[str, list[dict[str, Any]]] = {}
    for institution in institutions:
        accounts_by_institution[institution] = enable_banking.list_accounts(store, institution)

    own_names = {
        acc["name"].strip().lower()
        for accs in accounts_by_institution.values()
        for acc in accs
        if acc.get("name")
    }

    transactions_by_institution: dict[str, list[dict[str, Any]]] = {}
    for institution, accounts in accounts_by_institution.items():
        txns: list[dict[str, Any]] = []
        for account in accounts:
            fetched = fetch_deduped(store, institution, account["uid"], date_from, date_to)
            for txn in fetched:
                amount = float(txn["amount"])
                category = categorize(
                    txn["direction"],
                    txn.get("counterparty_name"),
                    txn.get("description"),
                    own_names,
                )
                txns.append(
                    {
                        "account_uid": account["uid"],
                        "account_name": account["name"],
                        "currency": account["currency"],
                        "date": txn["date"],
                        "amount": amount,
                        "direction": txn["direction"],
                        "counterparty_name": txn.get("counterparty_name"),
                        "description": txn.get("description"),
                        "category": category,
                    }
                )
        transactions_by_institution[institution] = sorted(txns, key=lambda t: t["date"])

    return {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "source": "Enable Banking via jb_gateway_mcp (direct adapter call, not MCP protocol)",
        "period": {"from": date_from, "to": date_to},
        "institutions": institutions,
        "accounts": accounts_by_institution,
        "transactions": transactions_by_institution,
    }


def monthly_summaries_by_currency(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """currency -> month ("YYYY-MM") ->
    {income, true_expense, net, category_breakdown, income_breakdown}"""

    def _new_month() -> dict[str, Any]:
        return {
            "income": 0.0,
            "true_expense": 0.0,
            "category_breakdown": defaultdict(float),
            "income_breakdown": defaultdict(float),
        }

    by_currency: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_new_month))
    for txns in dataset["transactions"].values():
        for txn in txns:
            bucket = by_currency[txn["currency"]][month_key(txn["date"])]
            if txn["category"] == "internal_transfer":
                continue  # a move between own accounts, never income or spend
            if txn["direction"] == "CRDT":
                bucket["income"] += txn["amount"]
                bucket["income_breakdown"][txn["category"]] += txn["amount"]
            else:
                bucket["true_expense"] += txn["amount"]
                bucket["category_breakdown"][txn["category"]] += txn["amount"]

    result: dict[str, dict[str, Any]] = {}
    for currency, months in by_currency.items():
        result[currency] = {}
        for month, agg in sorted(months.items()):
            income, expense = round(agg["income"], 2), round(agg["true_expense"], 2)
            result[currency][month] = {
                "income": income,
                "true_expense": expense,
                "net": round(income - expense, 2),
                "category_breakdown": {
                    k: round(v, 2) for k, v in agg["category_breakdown"].items()
                },
                "income_breakdown": {
                    k: round(v, 2) for k, v in agg["income_breakdown"].items()
                },
            }
    return result


def build_signals(monthly: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule-based month-over-month flags: stopped, new, or >15% moved categories."""
    months = sorted(monthly)
    if len(months) < 2:
        return []
    prev_key, cur_key = months[-2], months[-1]
    prev_cats = monthly[prev_key]["category_breakdown"]
    cur_cats = monthly[cur_key]["category_breakdown"]
    signals = []
    for cat in sorted(set(prev_cats) | set(cur_cats)):
        prev_amt, cur_amt = prev_cats.get(cat, 0.0), cur_cats.get(cat, 0.0)
        label = CATEGORY_LABELS.get(cat, cat)
        if prev_amt > 0 and cur_amt == 0:
            signals.append(
                {"type": "stopped", "category": label, "from_month": prev_key, "amount": prev_amt}
            )
        elif prev_amt == 0 and cur_amt > 0:
            signals.append(
                {"type": "new", "category": label, "month": cur_key, "amount": cur_amt}
            )
        elif prev_amt > 0 and abs(cur_amt - prev_amt) / prev_amt >= 0.15:
            pct = round((cur_amt - prev_amt) / prev_amt * 100)
            signals.append(
                {"type": "up" if pct > 0 else "down", "category": label, "pct": pct,
                 "from": prev_amt, "to": cur_amt}
            )
    return signals


# ----------------------------------------------------------------- render --

_CSS = """
.viz-root {
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --text-muted:#898781; --grid:#e1e0d9;
  --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-income:#2a78d6; --series-expense:#eb6834;
  --series-net-pos:#2a78d6; --series-net-neg:#e34948;
  --status-good:#0ca30c; --status-warning:#fab219;
  --status-serious:#ec835a; --good-text:#006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#ffffff;
    --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a;
    --baseline:#383835; --border:rgba(255,255,255,0.10);
    --series-income:#3987e5; --series-expense:#d95926;
    --series-net-pos:#3987e5; --series-net-neg:#e66767;
    --good-text:#0ca30c;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#ffffff;
  --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a;
  --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-income:#3987e5; --series-expense:#d95926;
  --series-net-pos:#3987e5; --series-net-neg:#e66767;
  --good-text:#0ca30c;
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--page); color:var(--text-primary);
  font-family: system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
}
.wrap { max-width:980px; margin:0 auto; padding:32px 20px 64px; }
header.report-head { margin-bottom:28px; }
header.report-head h1 { font-size:26px; margin:0 0 4px; }
header.report-head p { margin:0; color:var(--text-secondary); font-size:14px; }
.card {
  background:var(--surface-1); border:1px solid var(--border);
  border-radius:12px; padding:20px 22px; margin-bottom:20px;
}
.card h2 { font-size:15px; margin:0 0 4px; }
.card .sub { color:var(--text-secondary); font-size:12.5px; margin:0 0 18px; }
.kpi-row {
  display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));
  gap:14px; margin-bottom:20px;
}
.kpi {
  background:var(--surface-1); border:1px solid var(--border);
  border-radius:12px; padding:16px 18px;
}
.kpi .label { font-size:12px; color:var(--text-secondary); margin-bottom:8px; }
.kpi .value { font-size:26px; font-weight:600; }
.kpi .delta { font-size:12.5px; margin-top:6px; color:var(--text-secondary); }
.gbar-chart {
  display:flex; align-items:flex-end; gap:36px; height:220px; padding:0 8px;
  border-bottom:1px solid var(--baseline); position:relative;
}
.gbar-chart .grid-line { position:absolute; left:0; right:0; height:1px; background:var(--grid); }
.gbar-group {
  display:flex; align-items:flex-end; gap:6px; flex:1;
  justify-content:center; height:100%; position:relative; z-index:1;
}
.gbar { width:34px; border-radius:4px 4px 0 0; position:relative; }
.gbar .val {
  position:absolute; top:-18px; left:50%; transform:translateX(-50%);
  font-size:11px; color:var(--text-secondary); white-space:nowrap;
}
.gbar.income { background:var(--series-income); }
.gbar.expense { background:var(--series-expense); }
.gbar-labels { display:flex; gap:36px; padding:8px 8px 0; }
.gbar-labels > div { flex:1; text-align:center; font-size:12.5px; color:var(--text-secondary); }
.legend-row {
  display:flex; gap:18px; margin-top:14px; font-size:12.5px; color:var(--text-secondary);
}
.legend-row .sw {
  display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:6px; vertical-align:-1px;
}
.divbar-row { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.divbar-row .m-label { width:64px; font-size:12.5px; color:var(--text-secondary); flex-shrink:0; }
.divbar-track {
  flex:1; height:22px; position:relative; background:var(--grid);
  border-radius:4px; overflow:hidden;
}
.divbar-mid { position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--baseline); }
.divbar-fill { position:absolute; top:2px; bottom:2px; border-radius:3px; }
.divbar-fill.pos { left:50%; background:var(--series-net-pos); }
.divbar-fill.neg { right:50%; background:var(--series-net-neg); }
.divbar-val { width:100px; text-align:right; font-size:12.5px; flex-shrink:0; }
.divbar-val.neg { color:var(--series-net-neg); }
.divbar-val.pos { color:var(--good-text); }
.hbar-row { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.hbar-label { width:150px; font-size:12.5px; color:var(--text-secondary); flex-shrink:0; }
.hbar-track { flex:1; height:18px; background:var(--grid); border-radius:3px; overflow:hidden; }
.hbar-fill { height:100%; background:var(--series-income); border-radius:3px 0 0 3px; }
.hbar-val { width:130px; text-align:right; font-size:12.5px; flex-shrink:0; }
.hbar-pct { color:var(--text-muted); font-size:11.5px; margin-left:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--grid); }
th { color:var(--text-secondary); font-weight:500; font-size:12px; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tr:last-child td { border-bottom:none; }
.callout {
  display:flex; gap:12px; padding:12px 14px; border-radius:10px;
  margin-bottom:10px; border:1px solid var(--border);
}
.callout .icon {
  width:20px; height:20px; border-radius:50%; flex-shrink:0; display:flex;
  align-items:center; justify-content:center; font-size:12px;
  font-weight:700; color:#fff; margin-top:1px;
}
.callout.good .icon { background:var(--status-good); }
.callout.warning .icon { background:var(--status-warning); color:#3a2c00; }
.callout .body b { display:block; font-size:13.5px; margin-bottom:2px; }
.callout .body span { font-size:12.5px; color:var(--text-secondary); }
footer.report-foot { color:var(--text-muted); font-size:11.5px; margin-top:8px; line-height:1.6; }
@media (max-width:640px) {
  .kpi-row { grid-template-columns:repeat(2,1fr); }
  .hbar-label { width:100px; }
}
@media print {
  body { background:#fff; }
  .card { break-inside:avoid; }
  .wrap { max-width:100%; padding:0 12px; }
}
"""


def render_html(
    dataset: dict[str, Any],
    currency: str,
    monthly: dict[str, Any],
    signals: list[dict[str, Any]],
    balance_total: float | None = None,
    balance_warnings: list[str] | None = None,
    forecast_model: dict[str, Any] | None = None,
) -> str:
    months = sorted(monthly.get(currency, {}))
    if not months:
        raise ValueError(f"no transactions found in currency {currency!r} for this period")
    focus = months[-1]
    focus_data = monthly[currency][focus]
    prev_data = monthly[currency][months[-2]] if len(months) > 1 else None
    balance_warnings = balance_warnings or []

    max_flow = max(max(m["income"], m["true_expense"]) for m in monthly[currency].values()) or 1
    gbar_groups = []
    for m in months:
        d = monthly[currency][m]
        income_h, expense_h = d["income"] / max_flow * 100, d["true_expense"] / max_flow * 100
        gbar_groups.append(f"""
      <div class="gbar-group">
        <div class="gbar income" style="height:{income_h:.1f}%">
          <span class="val">{d['income']:,.0f}</span>
        </div>
        <div class="gbar expense" style="height:{expense_h:.1f}%">
          <span class="val">{d['true_expense']:,.0f}</span>
        </div>
      </div>""")
    gbar_labels = "".join(f"<div>{m}</div>" for m in months)

    max_abs_net = max(abs(monthly[currency][m]["net"]) for m in months) or 1
    divbar_rows = []
    for m in months:
        net = monthly[currency][m]["net"]
        cls, width = ("pos", net) if net >= 0 else ("neg", -net)
        width_pct = width / max_abs_net * 100
        sign = "+" if net >= 0 else "−"
        divbar_rows.append(f"""
    <div class="divbar-row">
      <div class="m-label">{m}</div>
      <div class="divbar-track">
        <div class="divbar-mid"></div>
        <div class="divbar-fill {cls}" style="width:{width_pct:.1f}%"></div>
      </div>
      <div class="divbar-val {cls}">{sign}{abs(net):,.0f}</div>
    </div>""")

    cats = sorted(focus_data["category_breakdown"].items(), key=lambda kv: -kv[1])
    max_cat = cats[0][1] if cats else 1
    total_expense = focus_data["true_expense"] or 1
    hbar_rows = []
    for cat, amt in cats:
        label = CATEGORY_LABELS.get(cat, cat)
        width_pct = amt / max_cat * 100
        pct_of_total = amt / total_expense * 100
        hbar_rows.append(f"""
    <div class="hbar-row">
      <div class="hbar-label">{label}</div>
      <div class="hbar-track"><div class="hbar-fill" style="width:{width_pct:.1f}%"></div></div>
      <div class="hbar-val">{amt:,.0f} <span class="hbar-pct">{pct_of_total:.0f}%</span></div>
    </div>""")

    income_cats = sorted(focus_data["income_breakdown"].items(), key=lambda kv: -kv[1])
    max_income_cat = income_cats[0][1] if income_cats else 1
    total_income = focus_data["income"] or 1
    income_hbar_rows = []
    for cat, amt in income_cats:
        label = CATEGORY_LABELS.get(cat, cat)
        width_pct = amt / max_income_cat * 100
        pct_of_total = amt / total_income * 100
        income_hbar_rows.append(f"""
    <div class="hbar-row">
      <div class="hbar-label">{label}</div>
      <div class="hbar-track"><div class="hbar-fill" style="width:{width_pct:.1f}%"></div></div>
      <div class="hbar-val">{amt:,.0f} <span class="hbar-pct">{pct_of_total:.0f}%</span></div>
    </div>""")
    income_hbar_html = "".join(income_hbar_rows) if income_hbar_rows else (
        '<p style="font-size:12.5px;color:var(--text-secondary)">'
        "No categorized income this period.</p>"
    )

    all_cats = sorted({c for m in months for c in monthly[currency][m]["category_breakdown"]})
    table_header = "".join(f"<th class='num'>{m}</th>" for m in months)
    table_rows = []
    for cat in all_cats:
        label = CATEGORY_LABELS.get(cat, cat)
        cells = "".join(
            f"<td class='num'>{monthly[currency][m]['category_breakdown'].get(cat, 0):,.0f}</td>"
            for m in months
        )
        table_rows.append(f"<tr><td>{label}</td>{cells}</tr>")

    callouts = []
    for s in signals:
        if s["type"] == "stopped":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>{s["category"]} stopped after {s["from_month"]}</b>'
                f'<span>Was {s["amount"]:,.0f} {currency}/month — confirm this was'
                f" intentional.</span></div></div>"
            )
        elif s["type"] == "new":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>New: {s["category"]} in {s["month"]}</b>'
                f'<span>{s["amount"]:,.0f} {currency} — wasn\'t present the month'
                f" before.</span></div></div>"
            )
        elif s["type"] == "down":
            callouts.append(
                f'<div class="callout good"><div class="icon">✓</div>'
                f'<div class="body"><b>{s["category"]} down {abs(s["pct"])}%</b>'
                f'<span>{s["from"]:,.0f} → {s["to"]:,.0f} {currency} month over'
                f" month.</span></div></div>"
            )
        elif s["type"] == "up":
            callouts.append(
                f'<div class="callout warning"><div class="icon">!</div>'
                f'<div class="body"><b>{s["category"]} up {s["pct"]}%</b>'
                f'<span>{s["from"]:,.0f} → {s["to"]:,.0f} {currency} month over'
                f" month.</span></div></div>"
            )
    if not callouts:
        callouts.append(
            '<p style="font-size:12.5px;color:var(--text-secondary);margin:0;">'
            "No large month-over-month category swings detected.</p>"
        )

    savings_rate = focus_data["net"] / focus_data["income"] * 100 if focus_data["income"] else 0.0
    prev_income_delta = ""
    if prev_data and prev_data["income"]:
        pct = (focus_data["income"] - prev_data["income"]) / prev_data["income"] * 100
        prev_income_delta = f"{'↑' if pct >= 0 else '↓'} {abs(pct):.0f}% vs {months[-2]}"

    institutions_label = " & ".join(i.upper() for i in dataset["institutions"])
    net_color = "var(--good-text)" if focus_data["net"] >= 0 else "var(--series-net-neg)"
    net_sign = "+" if focus_data["net"] >= 0 else "−"
    savings_color = "var(--good-text)" if savings_rate >= 0 else "var(--series-net-neg)"
    hbar_html = "".join(hbar_rows) if hbar_rows else (
        '<p style="font-size:12.5px;color:var(--text-secondary)">'
        "No categorized expenses this period.</p>"
    )

    balance_kpi = ""
    if balance_total is not None:
        balance_kpi = f"""
    <div class="kpi">
      <div class="label">Balance in hand (now)</div>
      <div class="value">{balance_total:,.0f} {currency}</div>
      <div class="delta">Live, not from the {focus} snapshot</div>
    </div>"""
    balance_note = ""
    if balance_warnings:
        joined = "; ".join(balance_warnings)
        balance_note = f"""
  <p style="font-size:11.5px;color:var(--status-warning);margin:-6px 0 20px;">
    Balance unavailable for: {joined}
  </p>"""

    forecast_html = ""
    if forecast_model:
        next_month = next_month_key(focus)
        income_pred = forecast_model.get("income", {})
        predicted_income = income_pred.get("predicted_next", 0.0)
        predicted_expense = predicted_expense_total(forecast_model)
        predicted_net = round(predicted_income - predicted_expense, 2)
        pred_net_color = "var(--good-text)" if predicted_net >= 0 else "var(--series-net-neg)"
        pred_net_sign = "+" if predicted_net >= 0 else "−"

        method_notes = {
            "fixed": "flat recurring cost, low variance",
            "average": "based on recent trailing average",
            "stopped": "stopped recently, predicted 0",
            "single_observation": "only one data point so far, low confidence",
            "no_recent_data": "no recent data",
        }
        cat_rows = []
        for cat, entry in sorted(
            forecast_model.get("categories", {}).items(),
            key=lambda kv: -kv[1]["predicted_next"],
        ):
            if entry["predicted_next"] == 0 and entry["method"] not in ("stopped",):
                continue
            label = CATEGORY_LABELS.get(cat, cat)
            note = method_notes.get(entry["method"], entry["method"])
            cat_rows.append(
                f"<tr><td>{label}</td><td class='num'>{entry['predicted_next']:,.0f}"
                f" {currency}</td><td>{note}</td></tr>"
            )
        income_note = method_notes.get(income_pred.get("method", ""), income_pred.get("method", ""))

        forecast_html = f"""
  <div class="card">
    <h2>Predicted — {next_month}</h2>
    <p class="sub">
      Local rule-based forecast (fixed/average/stopped per category, no ML) —
      see forecast_model_{currency}.json, updated by this report each run
    </p>
    <div class="kpi-row" style="grid-template-columns:repeat(3,1fr);">
      <div class="kpi">
        <div class="label">Predicted income</div>
        <div class="value">{predicted_income:,.0f} {currency}</div>
        <div class="delta">{income_note}</div>
      </div>
      <div class="kpi">
        <div class="label">Predicted expenses</div>
        <div class="value">{predicted_expense:,.0f} {currency}</div>
      </div>
      <div class="kpi">
        <div class="label">Predicted net</div>
        <div class="value" style="color:{pred_net_color}">
          {pred_net_sign}{abs(predicted_net):,.0f} {currency}
        </div>
      </div>
    </div>
    <table>
      <tr><th>Category</th><th class="num">Predicted</th><th>Basis</th></tr>
      {''.join(cat_rows)}
    </table>
  </div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{focus} Financial Report — {institutions_label}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
  <header class="report-head">
    <h1>Financial Report — {focus}</h1>
    <p>Accounts: {institutions_label} &middot; Currency: {currency} &middot;
       Generated {dataset['generated_at']} from live Enable Banking data</p>
  </header>

  <div class="kpi-row">
    <div class="kpi">
      <div class="label">Income ({focus})</div>
      <div class="value">{focus_data['income']:,.0f} {currency}</div>
      <div class="delta">{prev_income_delta}</div>
    </div>
    <div class="kpi">
      <div class="label">True expenses ({focus})</div>
      <div class="value">{focus_data['true_expense']:,.0f} {currency}</div>
    </div>
    <div class="kpi">
      <div class="label">Net cash flow</div>
      <div class="value" style="color:{net_color}">
        {net_sign}{abs(focus_data['net']):,.0f} {currency}
      </div>
    </div>
    <div class="kpi">
      <div class="label">Savings rate</div>
      <div class="value" style="color:{savings_color}">{savings_rate:+.1f}%</div>
    </div>{balance_kpi}
  </div>
  <p style="font-size:11.5px;color:var(--text-muted);margin:-6px 0 20px;">
    "True" figures exclude transfers between your own accounts
    (category: internal_transfer) — those are moves, not income or spend.
  </p>{balance_note}

  <div class="card">
    <h2>Income vs. expenses by month</h2>
    <p class="sub">{institutions_label}, {currency}</p>
    <div class="gbar-chart">
      <div class="grid-line" style="bottom:0%"></div>
      <div class="grid-line" style="bottom:33.3%"></div>
      <div class="grid-line" style="bottom:66.6%"></div>
      {''.join(gbar_groups)}
    </div>
    <div class="gbar-labels">{gbar_labels}</div>
    <div class="legend-row">
      <span><span class="sw" style="background:var(--series-income)"></span>Income</span>
      <span><span class="sw" style="background:var(--series-expense)"></span>Expenses (true)</span>
    </div>
  </div>

  <div class="card">
    <h2>Net cash flow by month</h2>
    <p class="sub">Income minus true expenses</p>
    {''.join(divbar_rows)}
  </div>

  <div class="card">
    <h2>Where {focus}'s money went</h2>
    <p class="sub">True expenses &middot; total {focus_data['true_expense']:,.0f} {currency}</p>
    {hbar_html}
  </div>

  <div class="card">
    <h2>Where {focus}'s money came from</h2>
    <p class="sub">Income &middot; total {focus_data['income']:,.0f} {currency}</p>
    {income_hbar_html}
  </div>

  <div class="card">
    <h2>Category breakdown by month</h2>
    <table><tr><th>Category</th>{table_header}</tr>{''.join(table_rows)}</table>
  </div>
{forecast_html}

  <div class="card">
    <h2>Signals</h2>
    <p class="sub">
      Rule-based month-over-month flags (≥15% move, category stopped, or category new)
    </p>
    {''.join(callouts)}
  </div>

  <footer class="report-foot">
    Source: live transaction data via Enable Banking (jb_gateway_mcp), pulled
    {dataset['generated_at']}. Categorization is heuristic keyword matching —
    see categories.py. This report is a data summary, not financial advice.
  </footer>
</div>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------- io --


def _label_for_range(date_from: str, date_to: str) -> str:
    """"YYYY-MM" for one full calendar month, "YYYY-MM_to_YYYY-MM" for several
    full calendar months, else the literal dates — always unambiguous, but
    collapses to the readable form whenever the range is month-aligned."""
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    end_last_day = calendar.monthrange(end.year, end.month)[1]
    month_aligned = start.day == 1 and end.day == end_last_day
    if not month_aligned:
        return f"{date_from}_to_{date_to}"
    if (start.year, start.month) == (end.year, end.month):
        return start.strftime("%Y-%m")
    return f"{start.strftime('%Y-%m')}_to_{end.strftime('%Y-%m')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument(
        "--institutions", default=None, help="comma-separated, default: all connected"
    )
    parser.add_argument("--currency", default="NOK")
    parser.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--skip-balance",
        action="store_true",
        help="skip the live 'balance in hand' lookup (e.g. to avoid an extra API call)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()
    data_dir, reports_dir = out_dir / "data", out_dir / "reports"
    data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    label = _label_for_range(args.date_from, args.date_to)
    data_path = data_dir / f"{label}-transactions.json"

    store = BankCredentialStore()

    if data_path.exists() and not args.refresh:
        print(f"Using cached data: {data_path}")
        dataset = json.loads(data_path.read_text())
    else:
        try:
            store.get_app_credential()
        except BankCredentialNotFoundError:
            print(
                "No Enable Banking app credential stored — "
                "run the connect-bank-account skill first.",
                file=sys.stderr,
            )
            return 1
        requested = args.institutions.split(",") if args.institutions else None
        institutions = resolve_institutions(store, requested)
        if not institutions:
            print("No connected institutions with a valid session.", file=sys.stderr)
            return 1
        print(f"Fetching live: {', '.join(institutions)} for {args.date_from}..{args.date_to}")
        try:
            dataset = build_dataset(store, institutions, args.date_from, args.date_to)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                print(
                    "Enable Banking rate-limited this request (429). "
                    "Wait a bit and re-run — no data was written.",
                    file=sys.stderr,
                )
                return 1
            raise
        data_path.write_text(json.dumps(dataset, indent=2))
        print(f"Wrote data cache: {data_path}")

    monthly = monthly_summaries_by_currency(dataset)
    if args.currency not in monthly:
        available = ", ".join(sorted(monthly)) or "none"
        print(
            f"No {args.currency} transactions in this dataset. "
            f"Currencies present: {available}",
            file=sys.stderr,
        )
        return 1
    signals = build_signals(monthly[args.currency])
    forecast_model = update_and_predict(out_dir, args.currency, monthly[args.currency])
    print(f"Updated forecast model: {out_dir / 'data' / f'forecast_model_{args.currency}.json'}")

    balance_total: float | None = None
    balance_warnings: list[str] = []
    if not args.skip_balance:
        balance_total, balance_warnings = fetch_balance_total(store, dataset, args.currency)
        for warning in balance_warnings:
            print(f"  [balance warning] {warning}", file=sys.stderr)

    institutions_slug = "-".join(dataset["institutions"])
    report_path = reports_dir / f"{label}-{institutions_slug}-report.html"
    report_path.write_text(
        render_html(
            dataset,
            args.currency,
            monthly,
            signals,
            balance_total=balance_total,
            balance_warnings=balance_warnings,
            forecast_model=forecast_model,
        )
    )
    print(f"Wrote report: {report_path}")

    other_currencies = sorted(set(monthly) - {args.currency})
    if other_currencies:
        print(
            f"Note: also has data in {', '.join(other_currencies)} "
            "(not charted here — rerun with --currency to see them)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
