"""Shared pieces for the finance tools: the response envelope and the
aging-bucket SQL both AP and AR build their reports from."""

from __future__ import annotations

import re
from typing import Any

from ebsmcp.tools.scoping import ROW_CAP
from ebsmcp.tools.sensitive import SensitiveScope


# Free-text columns the finance tools return (reasons, messages, the value an
# interface rejected). Nobody should have keyed a card number or SSN into
# them, but nothing stops it, and the cost of a miss is a card number in a
# model transcript. Typed columns (invoice numbers, amounts) are not scanned:
# a 16-digit invoice number would be a false positive.
FREE_TEXT_COLUMNS = frozenset({
    "hold_reason", "reject_reason", "message_text", "invalid_value",
    "description", "interface_line_attribute1",
})
REDACTED = "[REDACTED]"
_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
_CARD_CANDIDATE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def redact_sensitive_text(value: str) -> tuple[str, int]:
    """Replace US SSNs and Luhn-valid 13-19 digit card numbers in `value`.
    Returns (text, replacements). Pure function, unit-tested directly."""
    count = 0

    def card(m: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            count += 1
            return REDACTED
        return m.group()

    out = _CARD_CANDIDATE.sub(card, value)
    out, n = _SSN.subn(REDACTED, out)
    return out, count + n


def redact_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Apply redact_sensitive_text to the free-text columns of each row."""
    total = 0
    clean: list[dict] = []
    for row in rows:
        new = dict(row)
        for col in FREE_TEXT_COLUMNS & row.keys():
            if isinstance(row[col], str):
                new[col], n = redact_sensitive_text(row[col])
                total += n
        clean.append(new)
    return clean, total


def finance_envelope(
    ctx,
    scope: SensitiveScope,
    filters: dict[str, Any],
    rows: list[dict],
    total: int | None,
    key: str,
    withheld: list[dict[str, str]] | None = None,
) -> dict:
    """The response shape every finance list tool returns: the scoped
    envelope plus the disclosure block saying what was withheld. Free-text
    columns are scrubbed of card numbers and SSNs on the way out.

    `disclosure.domain` is a contract with host applications: OraEBSAgent
    reads it to keep this payload away from language models its admin has not
    trusted with finance data (app/core/data_sensitivity.py there). Do not
    rename it or move it out of `disclosure`."""
    rows, redacted = redact_rows(rows)
    return {
        "environment": ctx.environment,
        "mapped_role": scope.identity.mapped_role,
        "effective_org_ids": list(scope.effective_org_ids),
        "filters": filters,
        "row_cap": ROW_CAP,
        "total_count": total,
        key: rows,
        "disclosure": {**scope.disclosure(withheld or []), "values_redacted": redacted},
    }


def aging_buckets(days_past_due: str, amount: str) -> str:
    """SELECT-list fragment: `amount` summed into aging buckets by
    `days_past_due` (an expression; positive = overdue). `not_yet_due` is
    everything due today or later."""
    d, a = days_past_due, amount
    return (
        f"SUM(CASE WHEN {d} <= 0 THEN {a} ELSE 0 END) AS not_yet_due, "
        f"SUM(CASE WHEN {d} BETWEEN 1 AND 30 THEN {a} ELSE 0 END) AS days_1_30, "
        f"SUM(CASE WHEN {d} BETWEEN 31 AND 60 THEN {a} ELSE 0 END) AS days_31_60, "
        f"SUM(CASE WHEN {d} BETWEEN 61 AND 90 THEN {a} ELSE 0 END) AS days_61_90, "
        f"SUM(CASE WHEN {d} > 90 THEN {a} ELSE 0 END) AS over_90"
    )
