"""diagnose_invoice: one composite call that explains why an Accounts Payable
invoice is stuck, in the spirit of diagnose_order.

"Stuck" means something different at each stage of an invoice's life, so this
walks the stages in order and reports the first one that blocks:

  1. import            the invoice never left the open interface (rejected / not run)
  2. validation        distributions not validated
  3. holds             a hold is applied
  4. approval          waiting in, or rejected by, the approval workflow
  5. payment eligibility  supplier / site / schedule will not let it be paid
  6. payment run       selected into a payment batch that did not complete
  7. accounting        accounted-for incorrectly, not transferred to GL, or the
                       period is not open

Each stage is its own reviewed query, run independently: a stage whose query
fails is reported as `not_checked` with the reason, and the other stages still
answer. The verdict is then decided in Python (diagnose_invoice_findings) so the
model narrates a conclusion instead of inferring one from seven result sets.
Findings after the first blocker are marked `behind_blocker`: real, but they
will only matter once the first is fixed.

Scoping: only the header query takes the caller's Org IDs; every follow-up is
keyed by the invoice_id and org_id lifted from that already-scoped row. The same
invoice number can exist for several suppliers, so more than one match is an
error asking the caller to narrow with supplier_name, never a guess. Entry is
through resolve_sensitive_call(domain="finance"). No bank-account or
tax-registration column is read; payment eligibility looks at hold flags and
dates only.

Confidence: stages 1-3 use columns exercised against the live R12.2 instance on
2026-10-20 (AP_INVOICES_ALL, AP_INVOICE_DISTRIBUTIONS_ALL, AP_HOLDS_ALL,
AP_INVOICES_INTERFACE / AP_INTERFACE_REJECTIONS). Stages 4-7 were written from
the documented R12 data model and unit-tested, but NOT yet run against a live
instance (the database was unreachable when they were written). Assumptions to
verify first, in the order they are likely to fail with ORA-00904/00942:
AP_INVOICE_LINES_ALL.WFAPPROVAL_STATUS values; AP_SUPPLIERS.HOLD_ALL_PAYMENTS_FLAG
and AP_SUPPLIER_SITES_ALL.HOLD_ALL_PAYMENTS_FLAG / INACTIVE_DATE;
AP_PAYMENT_SCHEDULES_ALL.HOLD_FLAG / CHECKRUN_ID / PAYMENT_METHOD_CODE;
AP_INV_SELECTION_CRITERIA_ALL.CHECKRUN_NAME / STATUS; the XLA_* join
(application 200, entity AP_INVOICES, source_id_int_1 = invoice_id) and its
status codes; GL_PERIOD_STATUSES for application 200 on the invoice's ledger.
An empty findings list means none of the checks that ran fired; read `stages`
for which ones did not run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.finance._common import redact_rows
from ebsmcp.tools.registry import ToolContext, ToolSet, split_total_count
from ebsmcp.tools.scoping import ROW_CAP, escape_like, org_in_clause
from ebsmcp.tools.sensitive import resolve_sensitive_call

STAGES: tuple[tuple[str, str], ...] = (
    ("import", "Import (open interface)"),
    ("validation", "Validation"),
    ("holds", "Holds"),
    ("approval", "Approval"),
    ("payment_eligibility", "Payment eligibility"),
    ("payment_run", "Payment run"),
    ("accounting", "Accounting"),
)
_STAGE_INDEX = {key: i for i, (key, _) in enumerate(STAGES)}
_STAGE_LABEL = dict(STAGES)

# wfapproval_status values meaning the invoice is waiting on, or was refused by,
# the approval workflow. NOT REQUIRED / APPROVED / MANUALLY APPROVED /
# WFAPPROVED / NULL are all clear.
_APPROVAL_BLOCKING = {
    "REQUIRED": "approval is required but has not been submitted",
    "INITIATED": "sitting in the approval workflow, waiting for an approver",
    "REJECTED": "rejected by an approver",
    "STOPPED": "the approval workflow was stopped",
    "NEEDS WFREAPPROVAL": "changed after approval and needs to be approved again",
    "NEEDS REAPPROVAL": "changed after approval and needs to be approved again",
}

# Who usually clears each hold type. A starting point for the reader, not a
# routing rule; anything unlisted defaults to Accounts Payable.
_HOLD_OWNER = {
    "QTY REC": "Buyer / receiving", "QTY ORD": "Buyer", "PRICE": "Buyer",
    "PO REQUIRED": "Buyer", "AMOUNT": "AP supervisor (approval limit)",
    "INSUFFICIENT FUNDS": "Budget owner",
}

_NOT_VALIDATED_FLAGS = {
    "N": "never validated", "NULL": "never validated", "T": "needs revalidation",
    "S": "picked up by validation but not finished",
}
_PERIOD_NOT_OPEN = {"C": "closed", "N": "never opened", "P": "permanently closed"}

_MAX_LISTED = 10


# ── queries (pure functions, unit-tested directly) ───────────────────────────


def build_invoice_header_query(
    org_ids: tuple[str, ...], invoice_number: str, supplier_name: str | None
) -> tuple[str, dict[str, Any]]:
    scope, binds = org_in_clause("ai.org_id", org_ids)
    where = [scope, "ai.invoice_num = :invoice_number"]
    binds["invoice_number"] = invoice_number
    if supplier_name:
        where.append("UPPER(sup.vendor_name) LIKE :supplier_name ESCAPE '\\'")
        binds["supplier_name"] = "%" + escape_like(supplier_name.upper()) + "%"
    sql = (
        "SELECT ai.invoice_id, ai.invoice_num AS invoice_number, ai.org_id, "
        "ai.vendor_id, ai.vendor_site_id, sup.vendor_name AS supplier_name, "
        "ai.invoice_type_lookup_code AS invoice_type, ai.invoice_amount, "
        "NVL(ai.amount_paid, 0) AS amount_paid, ai.invoice_currency_code AS currency, "
        "ai.invoice_date, ai.gl_date, ai.creation_date, ai.cancelled_date, "
        "ai.payment_status_flag, ai.set_of_books_id AS ledger_id "
        "FROM APPS.AP_INVOICES_ALL ai "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = ai.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ai.invoice_id "
        "FETCH FIRST 5 ROWS ONLY"
    )
    return sql, binds


def build_interface_query(
    org_ids: tuple[str, ...], invoice_number: str
) -> tuple[str, dict[str, Any]]:
    """Open-interface rows for this invoice number, with any reject reasons."""
    scope, binds = org_in_clause("aii.org_id", org_ids)
    binds["invoice_number"] = invoice_number
    sql = (
        "SELECT aii.invoice_id AS interface_invoice_id, aii.status, aii.source, "
        "aii.creation_date, air.reject_lookup_code AS reject_code, "
        "alc.description AS reject_reason "
        "FROM APPS.AP_INVOICES_INTERFACE aii "
        "LEFT JOIN APPS.AP_INTERFACE_REJECTIONS air ON air.parent_id = aii.invoice_id "
        "AND air.parent_table = 'AP_INVOICES_INTERFACE' "
        "LEFT JOIN APPS.AP_LOOKUP_CODES alc ON alc.lookup_type = 'REJECT CODE' "
        "AND alc.lookup_code = air.reject_lookup_code "
        f"WHERE {scope} AND aii.invoice_num = :invoice_number "
        "ORDER BY aii.creation_date DESC, aii.invoice_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_distributions_query(invoice_id: int) -> tuple[str, dict[str, Any]]:
    """Distribution count per validation flag (A validated, T needs
    revalidation, S selected, N/NULL never validated)."""
    return (
        "SELECT NVL(aid.match_status_flag, 'NULL') AS flag, COUNT(*) AS n "
        "FROM APPS.AP_INVOICE_DISTRIBUTIONS_ALL aid "
        "WHERE aid.invoice_id = :invoice_id "
        "GROUP BY NVL(aid.match_status_flag, 'NULL')",
        {"invoice_id": invoice_id},
    )


def build_holds_query(invoice_id: int) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT ah.hold_lookup_code AS hold_code, ah.hold_reason, ah.hold_date, "
        "TRUNC(SYSDATE) - TRUNC(ah.hold_date) AS days_on_hold "
        "FROM APPS.AP_HOLDS_ALL ah "
        "WHERE ah.invoice_id = :invoice_id AND ah.release_lookup_code IS NULL "
        "ORDER BY ah.hold_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY",
        {"invoice_id": invoice_id},
    )


def build_approval_query(invoice_id: int) -> tuple[str, dict[str, Any]]:
    """Line count per approval-workflow status."""
    return (
        "SELECT NVL(ail.wfapproval_status, 'NULL') AS status, COUNT(*) AS n "
        "FROM APPS.AP_INVOICE_LINES_ALL ail "
        "WHERE ail.invoice_id = :invoice_id "
        "GROUP BY NVL(ail.wfapproval_status, 'NULL')",
        {"invoice_id": invoice_id},
    )


def build_supplier_query(vendor_id: int, vendor_site_id: int | None) -> tuple[str, dict[str, Any]]:
    """Supplier and pay-site flags that stop payment. Flags only — no bank or
    tax columns."""
    return (
        "SELECT sup.enabled_flag AS supplier_enabled, sup.end_date_active AS supplier_end_date, "
        "sup.hold_all_payments_flag AS supplier_hold_all_payments, "
        "site.inactive_date AS site_inactive_date, "
        "site.hold_all_payments_flag AS site_hold_all_payments "
        "FROM APPS.AP_SUPPLIERS sup "
        "LEFT JOIN APPS.AP_SUPPLIER_SITES_ALL site ON site.vendor_site_id = :vendor_site_id "
        "WHERE sup.vendor_id = :vendor_id",
        {"vendor_id": vendor_id, "vendor_site_id": vendor_site_id},
    )


def build_schedules_query(invoice_id: int) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT aps.payment_num, aps.due_date, aps.amount_remaining, aps.hold_flag, "
        "aps.payment_method_code, aps.checkrun_id "
        "FROM APPS.AP_PAYMENT_SCHEDULES_ALL aps "
        "WHERE aps.invoice_id = :invoice_id "
        "ORDER BY aps.payment_num "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY",
        {"invoice_id": invoice_id},
    )


def build_payment_batch_query(checkrun_ids: list[int]) -> tuple[str, dict[str, Any]]:
    binds = {f"checkrun_{i}": cid for i, cid in enumerate(checkrun_ids)}
    names = ", ".join(f":{k}" for k in binds)
    return (
        "SELECT asc_.checkrun_id, asc_.checkrun_name, asc_.status "
        "FROM APPS.AP_INV_SELECTION_CRITERIA_ALL asc_ "
        f"WHERE asc_.checkrun_id IN ({names})",
        binds,
    )


def build_accounting_query(invoice_id: int) -> tuple[str, dict[str, Any]]:
    """Subledger accounting events for this invoice (application 200 =
    Payables) and whether each reached the ledger."""
    return (
        "SELECT xe.event_type_code, xe.event_status_code, xe.process_status_code, "
        "xe.event_date, xah.gl_transfer_status_code "
        "FROM APPS.XLA_TRANSACTION_ENTITIES xte "
        "JOIN APPS.XLA_EVENTS xe ON xe.entity_id = xte.entity_id "
        "AND xe.application_id = xte.application_id "
        "LEFT JOIN APPS.XLA_AE_HEADERS xah ON xah.event_id = xe.event_id "
        "AND xah.application_id = xe.application_id "
        "WHERE xte.application_id = 200 AND xte.entity_code = 'AP_INVOICES' "
        "AND xte.source_id_int_1 = :invoice_id "
        "ORDER BY xe.event_date, xe.event_id "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY",
        {"invoice_id": invoice_id},
    )


def build_period_query(ledger_id: int, gl_date: date | datetime) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT gps.period_name, gps.closing_status "
        "FROM APPS.GL_PERIOD_STATUSES gps "
        "WHERE gps.application_id = 200 AND gps.ledger_id = :ledger_id "
        "AND gps.adjustment_period_flag = 'N' "
        "AND :gl_date BETWEEN gps.start_date AND gps.end_date",
        {"ledger_id": ledger_id, "gl_date": gl_date},
    )


# ── the verdict (pure function, unit-tested directly) ────────────────────────


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _days_since(now: datetime, earlier: Any) -> int | None:
    d = _as_date(earlier)
    return (now.date() - d).days if d else None


def diagnose_invoice_findings(
    header: dict | None,
    *,
    interface: list[dict] | None,
    distributions: list[dict] | None,
    holds: list[dict] | None,
    approval: list[dict] | None,
    supplier: list[dict] | None,
    schedules: list[dict] | None,
    batches: list[dict] | None,
    accounting: list[dict] | None,
    period: list[dict] | None,
    errors: dict[str, str],
    now: datetime,
) -> dict:
    """Turn the raw result sets into stage statuses, findings and a one-line
    verdict. `None` for a result set means its query did not run (it failed);
    that stage is reported `not_checked`, never `clear`.

    Severity: blocking (stops the invoice moving), warning (needs attention),
    info (context, including 'waiting, not stuck')."""
    findings: list[dict] = []

    def add(stage: str, code: str, severity: str, message: str, *, owner: str | None = None,
            days: int | None = None, **evidence: Any) -> None:
        f = {"stage": stage, "stage_label": _STAGE_LABEL[stage], "code": code,
             "severity": severity, "message": message}
        if owner:
            f["owner"] = owner
        if days is not None:
            f["days"] = days
        if evidence:
            f["evidence"] = evidence
        findings.append(f)

    # ── stage 1: import ──────────────────────────────────────────────────────
    if header is None:
        rows = interface or []
        rejected = [r for r in rows if (r.get("status") or "").upper() == "REJECTED"]
        if rejected:
            reasons = sorted({r.get("reject_reason") or r.get("reject_code") or "unknown"
                              for r in rejected})
            add("import", "import_rejected", "blocking",
                "Open Interface Import rejected this invoice, so it was never created: "
                + "; ".join(reasons) + ".", owner="Whoever submitted the invoice",
                days=_days_since(now, rejected[0].get("creation_date")))
        elif rows:
            add("import", "import_not_processed", "blocking",
                f"The invoice is still in the open interface (status "
                f"{rows[0].get('status') or 'not processed'}); Payables Open Interface Import "
                "has not created it.", owner="AP: run or reschedule Open Interface Import",
                days=_days_since(now, rows[0].get("creation_date")))
        return _result(findings, header, errors, now, created=False)

    # An invoice that exists but also has a rejected interface row for the same
    # number is a resubmission; worth knowing, not a blocker.
    if interface and any((r.get("status") or "").upper() == "REJECTED" for r in interface):
        add("import", "earlier_import_rejected", "info",
            "An earlier import of this invoice number was rejected; this invoice is a later "
            "submission.")

    if header.get("cancelled_date") is not None:
        add("validation", "cancelled", "info", "Invoice is cancelled; nothing further is expected.")
        return _result(findings, header, errors, now, created=True, terminal=True)

    paid_in_full = header.get("payment_status_flag") == "Y"

    # ── stage 2: validation ──────────────────────────────────────────────────
    validated = False
    if distributions is not None:
        total = sum(int(r["n"]) for r in distributions)
        by_flag = {str(r["flag"]): int(r["n"]) for r in distributions}
        good = by_flag.get("A", 0)
        if total == 0:
            add("validation", "no_distributions", "blocking",
                "Invoice has no distributions, so it cannot be validated.",
                owner="AP: add distributions", days=_days_since(now, header.get("creation_date")))
        elif good < total:
            detail = ", ".join(
                f"{n} {_NOT_VALIDATED_FLAGS.get(flag, 'not validated (' + flag + ')')}"
                for flag, n in sorted(by_flag.items()) if flag != "A")
            add("validation", "not_validated", "blocking",
                f"{total - good} of {total} distribution(s) are not validated ({detail}).",
                owner="AP: run Payables Invoice Validation and read its request log",
                days=_days_since(now, header.get("creation_date")), by_flag=by_flag)
        else:
            validated = True

    # ── stage 3: holds ───────────────────────────────────────────────────────
    for h in holds or []:
        code = str(h.get("hold_code") or "UNKNOWN")
        add("holds", f"hold:{code}", "blocking",
            f"Hold {code} is applied: {h.get('hold_reason') or 'no reason recorded'}.",
            owner=_HOLD_OWNER.get(code, "Accounts Payable"), days=h.get("days_on_hold"))

    # ── stage 4: approval ────────────────────────────────────────────────────
    for r in approval or []:
        status = str(r["status"]).upper()
        if status in _APPROVAL_BLOCKING:
            add("approval", f"approval:{status}", "blocking",
                f"{r['n']} line(s): {_APPROVAL_BLOCKING[status]}.",
                owner="The approver, or AP to resubmit; check the workflow notification",
                status=status)

    # ── stage 5: payment eligibility ─────────────────────────────────────────
    sup = (supplier or [{}])[0] if supplier else None
    if sup:
        end = _as_date(sup.get("supplier_end_date"))
        if sup.get("supplier_enabled") == "N" or (end and end <= now.date()):
            add("payment_eligibility", "supplier_inactive", "blocking",
                "The supplier is disabled or end-dated.", owner="Supplier maintenance")
        if sup.get("supplier_hold_all_payments") == "Y":
            add("payment_eligibility", "supplier_payment_hold", "blocking",
                "The supplier has Hold All Payments set.", owner="Accounts Payable")
        site_end = _as_date(sup.get("site_inactive_date"))
        if site_end and site_end <= now.date():
            add("payment_eligibility", "site_inactive", "blocking",
                "The supplier site on this invoice is inactive.", owner="Supplier maintenance")
        if sup.get("site_hold_all_payments") == "Y":
            add("payment_eligibility", "site_payment_hold", "blocking",
                "The supplier site has Hold All Payments set.", owner="Accounts Payable")

    open_schedules = [s for s in (schedules or []) if (s.get("amount_remaining") or 0) != 0]
    if not paid_in_full:
        for s in open_schedules:
            n = s.get("payment_num")
            if s.get("hold_flag") == "Y":
                add("payment_eligibility", f"schedule_hold:{n}", "blocking",
                    f"Scheduled payment {n} is on hold.", owner="Accounts Payable")
            if not s.get("payment_method_code"):
                add("payment_eligibility", f"no_payment_method:{n}", "blocking",
                    f"Scheduled payment {n} has no payment method.", owner="Accounts Payable")
        due = [d for d in (_as_date(s.get("due_date")) for s in open_schedules) if d]
        if validated and due and min(due) > now.date() and not findings_blocking(findings):
            add("payment_eligibility", "not_yet_due", "info",
                f"Waiting, not stuck: the earliest scheduled payment is due {min(due)}.")

    # ── stage 6: payment run ─────────────────────────────────────────────────
    if not paid_in_full:
        names = {b.get("checkrun_id"): b for b in (batches or [])}
        for s in open_schedules:
            cid = s.get("checkrun_id")
            if cid is not None:
                b = names.get(cid, {})
                add("payment_run", f"in_payment_batch:{cid}", "warning",
                    f"Scheduled payment {s.get('payment_num')} was selected into payment batch "
                    f"{b.get('checkrun_name') or cid} (status {b.get('status') or 'unknown'}) "
                    "and is still unpaid; the batch did not complete or was abandoned.",
                    owner="Treasury / AP: review the batch in the payment manager")

    # ── stage 7: accounting ──────────────────────────────────────────────────
    if validated and accounting is not None:
        pending = False
        for ev in accounting:
            ev_status = (ev.get("event_status_code") or "").upper()
            proc = (ev.get("process_status_code") or "").upper()
            gl = (ev.get("gl_transfer_status_code") or "").upper()
            kind = ev.get("event_type_code") or "event"
            if proc in ("E", "I", "R"):
                add("accounting", f"accounting_error:{kind}", "blocking",
                    f"The {kind} accounting event is in error.", owner="Accountant: see Subledger "
                    "Accounting errors", days=_days_since(now, ev.get("event_date")))
                pending = True
            elif proc == "D":
                add("accounting", f"accounting_draft:{kind}", "warning",
                    f"The {kind} accounting event is only draft-accounted.", owner="Accountant")
                pending = True
            elif ev_status == "U" or proc == "U":
                add("accounting", f"not_accounted:{kind}", "warning",
                    f"The {kind} event has not been accounted.", owner="Accountant: run Create "
                    "Accounting", days=_days_since(now, ev.get("event_date")))
                pending = True
            elif gl == "N":
                add("accounting", f"not_transferred:{kind}", "warning",
                    f"The {kind} event is accounted but not transferred to the general ledger.",
                    owner="Accountant: run Transfer to GL", days=_days_since(now, ev.get("event_date")))
                pending = True
        if pending and period:
            closing = str(period[0].get("closing_status") or "").upper()
            if closing in _PERIOD_NOT_OPEN:
                add("accounting", "period_not_open", "blocking",
                    f"Payables period {period[0].get('period_name')} is "
                    f"{_PERIOD_NOT_OPEN[closing]}, so this cannot be accounted until it is "
                    "opened or the invoice is moved to an open period.", owner="Controller / AP "
                    "period close")

    return _result(
        findings, header, errors, now, created=True,
        paid_in_full=paid_in_full, validated=validated,
    )


def findings_blocking(findings: list[dict]) -> bool:
    return any(f["severity"] == "blocking" for f in findings)


# Which query failures make which stage unreadable.
_STAGE_QUERIES = {
    "import": ("interface",),
    "validation": ("distributions",),
    "holds": ("holds",),
    "approval": ("approval",),
    "payment_eligibility": ("supplier", "schedules"),
    "payment_run": ("schedules", "batches"),
    "accounting": ("accounting",),
}


def _result(findings, header, errors, now, *, created, terminal=False,
            paid_in_full=False, validated=False) -> dict:
    sev = {"blocking": 0, "warning": 1, "info": 2}
    findings = sorted(findings, key=lambda f: (_STAGE_INDEX[f["stage"]], sev[f["severity"]], f["code"]))

    blocking = [f for f in findings if f["severity"] == "blocking"]
    primary = blocking[0] if blocking else None
    primary_idx = _STAGE_INDEX[primary["stage"]] if primary else None
    for f in findings:
        if primary_idx is not None and _STAGE_INDEX[f["stage"]] > primary_idx:
            f["behind_blocker"] = True

    stages = []
    for key, label in STAGES:
        failed = [q for q in _STAGE_QUERIES[key] if q in errors]
        here = [f for f in findings if f["stage"] == key]
        if terminal:
            status = "not_applicable"
        elif key == "import" and created and not here:
            status = "not_applicable"
        elif failed:
            status = "not_checked"
        elif any(f["severity"] == "blocking" for f in here):
            status = "blocked"
        elif any(f["severity"] == "warning" for f in here):
            status = "attention"
        elif any(f["code"] == "not_yet_due" for f in here):
            status = "waiting"
        elif key in ("accounting",) and not validated and created:
            status = "not_applicable"
        else:
            status = "clear"
        entry = {"stage": key, "label": label, "status": status}
        if failed:
            entry["reason"] = "; ".join(f"{q}: {errors[q]}" for q in failed)
        stages.append(entry)

    not_checked = [s["label"] for s in stages if s["status"] == "not_checked"]
    if not created and primary:
        verdict = f"Stuck at {primary['stage_label']}: {primary['message']}"
    elif not created:
        verdict = "No invoice with that number exists, and nothing was found in the open interface."
    elif terminal:
        verdict = "Not stuck: the invoice is cancelled."
    elif primary:
        verdict = f"Stuck at {primary['stage_label']}: {primary['message']}"
        if primary.get("days") is not None:
            verdict += f" (open {primary['days']} day(s))"
        if primary.get("owner"):
            verdict += f" Next: {primary['owner']}."
    elif any(f["code"] == "not_yet_due" for f in findings):
        verdict = next(f["message"] for f in findings if f["code"] == "not_yet_due")
    elif paid_in_full and not any(f["severity"] == "warning" for f in findings):
        verdict = "Not stuck: the invoice is paid" + (
            "." if not not_checked else f", but these stages could not be checked: "
            + ", ".join(not_checked) + ".")
    elif any(f["severity"] == "warning" for f in findings):
        verdict = "No hard blocker, but needs attention: " + findings_first_warning(findings)
    else:
        verdict = "No blocker found by the checks that ran."
        if not_checked:
            verdict += " These stages could not be checked: " + ", ".join(not_checked) + "."

    return {
        "stuck": primary is not None,
        "verdict": verdict,
        "blocking_stage": primary["stage"] if primary else None,
        "days_since_created": _days_since(now, (header or {}).get("creation_date")),
        "stages": stages,
        "findings": findings,
    }


def findings_first_warning(findings: list[dict]) -> str:
    return next(f["message"] for f in findings if f["severity"] == "warning")


# ── the tool ─────────────────────────────────────────────────────────────────


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True
        )
    )
    def diagnose_invoice(
        invoice_number: str,
        supplier_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Explain why one Accounts Payable invoice is stuck, in a single
        call. Walks the invoice's life in order and stops at the first
        blocker: import (rejected or never imported from the open interface),
        validation (distributions not validated), holds, approval (waiting in
        or rejected by the workflow), payment eligibility (supplier or site
        disabled or on payment hold, scheduled payment on hold, no payment
        method), payment run (selected into a batch that never completed) and
        accounting (accounting event in error, not accounted, not transferred
        to GL, or the period is not open). Returns a one-line `verdict`, the
        `blocking_stage`, every stage's status (clear, blocked, attention,
        waiting, not_applicable, or not_checked when that check could not
        run), and findings with an evidence trail, the usual owner of the next
        step and days open. Findings marked behind_blocker are real but will
        only matter once the first blocker is cleared. Bank-account and tax
        details are never read. The same invoice number can exist for several
        suppliers; if it does, pass supplier_name (or org_ids) to pick one.
        For a list of invoices use ap_invoice_status; for every hold across
        invoices use ap_invoices_on_hold; for what was paid use
        ap_payment_history. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"invoice_number": invoice_number, "supplier_name": supplier_name,
                  "org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="diagnose_invoice", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            connector = scope.connector
            errors: dict[str, str] = {}

            def stage(name: str, built: tuple[str, dict[str, Any]]) -> list[dict] | None:
                """One independent stage query. A failure is recorded and the
                stage reported not_checked; it never fails the whole call."""
                try:
                    rows, _ = split_total_count(connector.run(*built))
                    return rows
                except Exception as exc:  # noqa: BLE001 — isolate one stage's failure
                    errors[name] = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                    return None

            headers = connector.run(*build_invoice_header_query(
                scope.effective_org_ids, invoice_number, supplier_name))
            if len(headers) > 1:
                raise ToolError(
                    f"Invoice number {invoice_number!r} exists for more than one supplier or "
                    f"operating unit ({', '.join(sorted({str(h['supplier_name']) + ' / org ' + str(h['org_id']) for h in headers}))}); "
                    "pass supplier_name or org_ids to choose one."
                )

            interface = stage("interface", build_interface_query(
                scope.effective_org_ids, invoice_number))
            header = headers[0] if headers else None
            if header is None and interface is None:
                raise ToolError(
                    f"No invoice {invoice_number!r} found in Payables, and the open interface "
                    f"could not be checked ({errors['interface']})."
                )
            if header is None and not interface:
                raise ToolError(
                    f"No invoice {invoice_number!r} found in Payables or the open interface "
                    "within the caller's permitted operating units."
                )

            sets: dict[str, list[dict] | None] = {
                "distributions": None, "holds": None, "approval": None, "supplier": None,
                "schedules": None, "batches": None, "accounting": None, "period": None,
            }
            if header is not None:
                iid = header["invoice_id"]
                sets["distributions"] = stage("distributions", build_distributions_query(iid))
                sets["holds"] = stage("holds", build_holds_query(iid))
                sets["approval"] = stage("approval", build_approval_query(iid))
                sets["supplier"] = stage("supplier", build_supplier_query(
                    header["vendor_id"], header.get("vendor_site_id")))
                sets["schedules"] = stage("schedules", build_schedules_query(iid))
                batch_ids = sorted({s["checkrun_id"] for s in (sets["schedules"] or [])
                                    if s.get("checkrun_id") is not None})
                if batch_ids:
                    sets["batches"] = stage("batches", build_payment_batch_query(batch_ids))
                else:
                    sets["batches"] = []
                sets["accounting"] = stage("accounting", build_accounting_query(iid))
                if header.get("ledger_id") is not None and header.get("gl_date") is not None:
                    sets["period"] = stage("period", build_period_query(
                        header["ledger_id"], header["gl_date"]))

            # Scrub free text BEFORE the verdict is built: hold reasons and reject
            # reasons are quoted into finding messages, so redacting only the
            # evidence lists afterwards would still leak through the verdict.
            if sets["holds"] is not None:
                sets["holds"], n1 = redact_rows(sets["holds"])
            else:
                n1 = 0
            if interface is not None:
                interface, n2 = redact_rows(interface)
            else:
                n2 = 0

            diagnosis = diagnose_invoice_findings(
                header, interface=interface, errors=errors, now=datetime.now(), **sets
            )
            return {
                "environment": ctx.environment,
                "mapped_role": scope.identity.mapped_role,
                "effective_org_ids": list(scope.effective_org_ids),
                "invoice": {k: header.get(k) for k in (
                    "invoice_number", "supplier_name", "org_id", "invoice_type", "invoice_amount",
                    "amount_paid", "currency", "invoice_date", "gl_date", "creation_date",
                    "cancelled_date", "payment_status_flag")} if header else None,
                **diagnosis,
                "holds": sets["holds"] or [],
                "payment_schedules": sets["schedules"] or [],
                "accounting_events": sets["accounting"] or [],
                "interface_rows": interface or [],
                "disclosure": {**scope.disclosure([]), "values_redacted": n1 + n2},
            }


DIAGNOSE_INVOICE_TOOLSET = ToolSet(name="finance_diagnose_invoice", register=_register)
