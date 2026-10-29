"""Accounts Payable tools: invoice status, holds, aging, payment history and
rejected invoice imports.

Scoping: every AP_*_ALL table carries the operating-unit ORG_ID, so the
entitlement scope is `org_id IN (...)`. Entry is through
resolve_sensitive_call(domain="finance"): only a finance mapping runs these.
No bank-account or tax-registration column is selected anywhere — the
safest sensitive column is the one a tool never reads — so there is nothing
for select_fields to gate yet; add a column here only with a FieldSpec.

Confidence: executed against the live R12.2 instance on 2026-10-20 (DEV). The
four aging/holds/rejections/history queries ran clean and the AP aging
currency totals equal a direct SUM over AP_PAYMENT_SCHEDULES_ALL. The match
flag lives on AP_INVOICE_DISTRIBUTIONS_ALL, not on the lines (the lines table
has no MATCH_STATUS_FLAG: ORA-00904) — 'A' is "validated"; 'T', 'S', 'N' and
NULL are not. Still unchecked: payment history returned no rows for a 365-day
window because the newest AP_CHECKS_ALL row is 2022-07-14, so its row
semantics are unverified.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.finance._common import aging_buckets, finance_envelope
from ebsmcp.tools.registry import ToolContext, ToolSet, split_total_count
from ebsmcp.tools.scoping import ROW_CAP, escape_like, org_in_clause
from ebsmcp.tools.sensitive import resolve_sensitive_call

PaymentStatus = Literal["UNPAID", "PARTIALLY_PAID", "PAID"]
_PAYMENT_STATUS_FLAG = {"UNPAID": "N", "PARTIALLY_PAID": "P", "PAID": "Y"}

_AP_DAYS_PAST_DUE = "(TRUNC(SYSDATE) - TRUNC(aps.due_date))"


def _supplier_like(binds: dict[str, Any], supplier_name: str) -> str:
    binds["supplier_name"] = "%" + escape_like(supplier_name.upper()) + "%"
    return "UPPER(sup.vendor_name) LIKE :supplier_name ESCAPE '\\'"


def build_invoice_status_query(
    org_ids: tuple[str, ...],
    invoice_number: str | None,
    supplier_name: str | None,
    payment_status: PaymentStatus | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open holds and validated lines
    are counted in the query; what they mean is decided in
    annotate_invoice_status."""
    scope, binds = org_in_clause("ai.org_id", org_ids)
    where = [scope]
    if invoice_number:
        where.append("ai.invoice_num = :invoice_number")
        binds["invoice_number"] = invoice_number
    if supplier_name:
        where.append(_supplier_like(binds, supplier_name))
    if payment_status:
        where.append("ai.payment_status_flag = :payment_status_flag")
        binds["payment_status_flag"] = _PAYMENT_STATUS_FLAG[payment_status]

    sql = (
        "SELECT ai.invoice_num AS invoice_number, sup.vendor_name AS supplier_name, "
        "ai.invoice_type_lookup_code AS invoice_type, ai.invoice_date, "
        "ai.invoice_amount, NVL(ai.amount_paid, 0) AS amount_paid, "
        "ai.invoice_amount - NVL(ai.amount_paid, 0) AS amount_unpaid, "
        "ai.invoice_currency_code AS currency, ai.payment_status_flag, "
        "ai.cancelled_date, ai.org_id, "
        "(SELECT COUNT(*) FROM APPS.AP_INVOICE_DISTRIBUTIONS_ALL aid "
        "WHERE aid.invoice_id = ai.invoice_id) AS distributions_total, "
        "(SELECT COUNT(*) FROM APPS.AP_INVOICE_DISTRIBUTIONS_ALL aid "
        "WHERE aid.invoice_id = ai.invoice_id AND aid.match_status_flag = 'A') "
        "AS distributions_validated, "
        "(SELECT COUNT(*) FROM APPS.AP_HOLDS_ALL ah "
        "WHERE ah.invoice_id = ai.invoice_id AND ah.release_lookup_code IS NULL) "
        "AS open_holds, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AP_INVOICES_ALL ai "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = ai.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ai.invoice_date DESC, ai.invoice_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_invoice_status(row: dict) -> dict:
    """Decide in Python what state an invoice is in and what is blocking
    payment, so the model reports a fact instead of inferring one from
    counts. Pure function, unit-tested directly."""
    holds = int(row.get("open_holds") or 0)
    dists = int(row.get("distributions_total") or 0)
    validated = int(row.get("distributions_validated") or 0)
    flag = row.get("payment_status_flag")

    if row.get("cancelled_date") is not None:
        status, blocker = "CANCELLED", None
    elif flag == "Y":
        status, blocker = "PAID", None
    elif holds:
        status = "ON_HOLD"
        blocker = f"{holds} open hold(s) — use ap_invoices_on_hold for the hold type and age"
    elif dists == 0 or validated < dists:
        status = "NOT_VALIDATED"
        blocker = (
            f"{dists - validated} of {dists} distribution(s) not validated — "
            "run Payables Invoice Validation"
        )
    elif flag == "P":
        status, blocker = "PARTIALLY_PAID", None
    else:
        status, blocker = "READY_FOR_PAYMENT", None
    return {**row, "invoice_status": status, "payment_blocker": blocker}


def build_holds_query(
    org_ids: tuple[str, ...],
    hold_code: str | None,
    min_days_on_hold: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Holds still applied on
    non-cancelled invoices, oldest first: age is the point."""
    scope, binds = org_in_clause("ah.org_id", org_ids)
    where = [scope, "ah.release_lookup_code IS NULL", "ai.cancelled_date IS NULL"]
    if hold_code:
        where.append("ah.hold_lookup_code = :hold_code")
        binds["hold_code"] = hold_code.upper()
    if min_days_on_hold is not None:
        where.append("TRUNC(SYSDATE) - TRUNC(ah.hold_date) >= :min_days_on_hold")
        binds["min_days_on_hold"] = min_days_on_hold

    sql = (
        "SELECT ai.invoice_num AS invoice_number, sup.vendor_name AS supplier_name, "
        "ah.hold_lookup_code AS hold_code, ah.hold_reason, ah.hold_date, "
        "TRUNC(SYSDATE) - TRUNC(ah.hold_date) AS days_on_hold, "
        "ai.invoice_amount, ai.invoice_currency_code AS currency, ah.org_id, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AP_HOLDS_ALL ah "
        "JOIN APPS.AP_INVOICES_ALL ai ON ai.invoice_id = ah.invoice_id "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = ai.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ah.hold_date, ai.invoice_id "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_aging_query(
    org_ids: tuple[str, ...],
    supplier_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row per supplier and
    currency (amounts in different currencies are never added together),
    largest balance first. GROUP BY, so total_count counts suppliers, and
    currency_total_outstanding covers every supplier, not just the 50
    returned. Includes invoices not yet validated."""
    scope, binds = org_in_clause("aps.org_id", org_ids)
    where = [scope, "aps.amount_remaining <> 0", "ai.cancelled_date IS NULL"]
    if supplier_name:
        where.append(_supplier_like(binds, supplier_name))

    sql = (
        "SELECT sup.vendor_name AS supplier_name, sup.segment1 AS supplier_number, "
        "ai.invoice_currency_code AS currency, "
        f"{aging_buckets(_AP_DAYS_PAST_DUE, 'aps.amount_remaining')}, "
        "SUM(aps.amount_remaining) AS total_outstanding, "
        "SUM(SUM(aps.amount_remaining)) OVER (PARTITION BY ai.invoice_currency_code) "
        "AS currency_total_outstanding, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AP_PAYMENT_SCHEDULES_ALL aps "
        "JOIN APPS.AP_INVOICES_ALL ai ON ai.invoice_id = aps.invoice_id "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = ai.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY sup.vendor_name, sup.segment1, ai.invoice_currency_code "
        "ORDER BY SUM(aps.amount_remaining) DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_payment_history_query(
    org_ids: tuple[str, ...],
    supplier_name: str | None,
    check_number: int | None,
    invoice_number: str | None,
    days: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Payments (AP_CHECKS_ALL), newest
    first. The payee's bank account columns on that table are never
    selected."""
    scope, binds = org_in_clause("ac.org_id", org_ids)
    where = [scope]
    if supplier_name:
        where.append("UPPER(ac.vendor_name) LIKE :supplier_name ESCAPE '\\'")
        binds["supplier_name"] = "%" + escape_like(supplier_name.upper()) + "%"
    if check_number is not None:
        where.append("ac.check_number = :check_number")
        binds["check_number"] = check_number
    if invoice_number:
        where.append(
            "EXISTS (SELECT 1 FROM APPS.AP_INVOICE_PAYMENTS_ALL aip "
            "JOIN APPS.AP_INVOICES_ALL ai ON ai.invoice_id = aip.invoice_id "
            "WHERE aip.check_id = ac.check_id AND ai.invoice_num = :invoice_number)"
        )
        binds["invoice_number"] = invoice_number
    if days is not None:
        where.append("ac.check_date >= TRUNC(SYSDATE) - :days")
        binds["days"] = days

    sql = (
        "SELECT ac.check_number AS payment_number, ac.check_date AS payment_date, "
        "ac.amount, ac.currency_code AS currency, ac.status_lookup_code AS status, "
        "ac.payment_method_code, ac.vendor_name AS supplier_name, "
        "ac.cleared_date, ac.void_date, ac.org_id, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AP_CHECKS_ALL ac "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ac.check_date DESC, ac.check_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_interface_rejections_query(
    org_ids: tuple[str, ...],
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Rejections from the Payables Open
    Interface Import, newest first, one row per invoice per reject reason, at
    header level and at line level (on the live instance about half of all
    rejections are on the lines, e.g. ACCT DATE NOT IN OPEN PD). The two are
    UNIONed inside a subquery so total_count counts both."""
    scope, binds = org_in_clause("aii.org_id", org_ids)
    cols = (
        "SELECT aii.invoice_id AS interface_invoice_id, aii.invoice_num AS invoice_number, "
        "aii.vendor_name AS supplier_name, aii.invoice_amount, "
        "aii.invoice_currency_code AS currency, aii.source, "
        "air.reject_lookup_code AS reject_code, alc.description AS reject_reason, "
    )
    tail = (
        "LEFT JOIN APPS.AP_LOOKUP_CODES alc ON alc.lookup_type = 'REJECT CODE' "
        "AND alc.lookup_code = air.reject_lookup_code "
        f"WHERE {scope} AND aii.status = 'REJECTED' "
    )
    sql = (
        "SELECT u.*, COUNT(*) OVER () AS total_count FROM ("
        + cols + "'HEADER' AS reject_level, aii.creation_date, aii.org_id "
        "FROM APPS.AP_INVOICES_INTERFACE aii "
        "JOIN APPS.AP_INTERFACE_REJECTIONS air ON air.parent_id = aii.invoice_id "
        "AND air.parent_table = 'AP_INVOICES_INTERFACE' "
        + tail
        + "UNION ALL "
        + cols + "'LINE' AS reject_level, aii.creation_date, aii.org_id "
        "FROM APPS.AP_INVOICES_INTERFACE aii "
        "JOIN APPS.AP_INVOICE_LINES_INTERFACE ail ON ail.invoice_id = aii.invoice_id "
        "JOIN APPS.AP_INTERFACE_REJECTIONS air ON air.parent_id = ail.invoice_line_id "
        "AND air.parent_table = 'AP_INVOICE_LINES_INTERFACE' "
        + tail
        + ") u "
        "ORDER BY u.creation_date DESC, u.interface_invoice_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def ap_invoice_status(
        invoice_number: str | None = None,
        supplier_name: str | None = None,
        payment_status: PaymentStatus | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Look up Accounts Payable invoices by exact invoice number, part of
        a supplier name and/or payment status (UNPAID, PARTIALLY_PAID,
        PAID), newest first. Each invoice carries a decided invoice_status
        (CANCELLED, PAID, ON_HOLD, NOT_VALIDATED, PARTIALLY_PAID,
        READY_FOR_PAYMENT) and a payment_blocker saying why it is not being
        paid — report those, do not re-derive them. For the hold type and age
        use ap_invoices_on_hold; for the payment itself use
        ap_payment_history. Limited to the caller's permitted operating
        units; org_ids can only narrow that. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {
            "invoice_number": invoice_number,
            "supplier_name": supplier_name,
            "payment_status": payment_status,
            "org_ids": org_ids,
        }
        with resolve_sensitive_call(
            ctx, tool_name="ap_invoice_status", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_invoice_status_query(
                scope.effective_org_ids, invoice_number, supplier_name, payment_status
            )
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(
                ctx, scope, params, [annotate_invoice_status(r) for r in rows], total, "invoices"
            )

    @app.tool(annotations=read_only)
    def ap_invoices_on_hold(
        hold_code: str | None = None,
        min_days_on_hold: int | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List Accounts Payable invoices with a hold still applied (not
        released), oldest hold first, with hold type, reason and days on
        hold. hold_code narrows to one hold type (e.g. 'QTY REC', 'PRICE');
        min_days_on_hold to holds at least that old. For one invoice's
        overall status use ap_invoice_status. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"hold_code": hold_code, "min_days_on_hold": min_days_on_hold, "org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="ap_invoices_on_hold", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_holds_query(scope.effective_org_ids, hold_code, min_days_on_hold)
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "holds")

    @app.tool(annotations=read_only)
    def ap_aging(
        supplier_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Accounts Payable aging: what is owed to each supplier, bucketed by
        days past due (not_yet_due, days_1_30, days_31_60, days_61_90,
        over_90), largest balance first, one row per supplier and currency —
        amounts in different currencies are never added together.
        currency_total_outstanding is the total for that currency across ALL
        suppliers, not just the rows returned. Includes invoices not yet
        validated. supplier_name narrows to matching suppliers. Capped at 50
        suppliers with total_count. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {"supplier_name": supplier_name, "org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="ap_aging", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_aging_query(scope.effective_org_ids, supplier_name)
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "suppliers")

    @app.tool(annotations=read_only)
    def ap_payment_history(
        supplier_name: str | None = None,
        payment_number: int | None = None,
        invoice_number: str | None = None,
        days: int | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List Accounts Payable payments (checks and electronic payments),
        newest first, with amount, status (NEGOTIABLE, CLEARED, VOIDED, ...)
        and cleared/void dates. Filter by part of a supplier name, exact
        payment (check) number, the invoice a payment settled, and/or
        payments in the last `days` days. Bank account details are never
        returned. For why an invoice has NOT been paid use ap_invoice_status.
        Capped at 50 rows with total_count. Call list_ebs_instances first if
        unsure which EBS instance names this deployment has.
        """
        params = {
            "supplier_name": supplier_name,
            "payment_number": payment_number,
            "invoice_number": invoice_number,
            "days": days,
            "org_ids": org_ids,
        }
        with resolve_sensitive_call(
            ctx, tool_name="ap_payment_history", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_payment_history_query(
                scope.effective_org_ids, supplier_name, payment_number, invoice_number, days
            )
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "payments")

    @app.tool(annotations=read_only)
    def ap_interface_rejections(
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List invoices rejected by the Payables Open Interface Import
        (AP_INVOICES_INTERFACE, status REJECTED), newest first, one row per
        reject reason with its code and description, and reject_level saying
        whether the reason is on the invoice HEADER or on one of its LINE rows
        (an invoice rejected for two lines with the same reason appears twice).
        To see why one specific invoice is stuck, use diagnose_invoice. Use
        this when a batch of invoices did not make it into Payables. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="ap_interface_rejections", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_interface_rejections_query(scope.effective_org_ids)
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "rejections")


ACCOUNTS_PAYABLE_TOOLSET = ToolSet(name="finance_accounts_payable", register=_register)
