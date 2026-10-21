"""Accounts Receivable tools: invoice status, aging, overdue items,
unapplied receipts and failed AutoInvoice lines.

Scoping: RA_CUSTOMER_TRX_ALL, AR_PAYMENT_SCHEDULES_ALL, AR_CASH_RECEIPTS_ALL
and RA_INTERFACE_LINES_ALL carry the operating-unit ORG_ID, so the
entitlement scope is `org_id IN (...)`. Entry is through
resolve_sensitive_call(domain="finance"). No customer bank-account or tax
registration column is selected anywhere.

Open balances come from AR_PAYMENT_SCHEDULES_ALL (STATUS = 'OP'), the table
AR itself ages from. Debit items are INV/DM/CB/DEP/BR; credit memos (CM) are
included in aging so it nets to what is actually owed; receipts (PMT) are
not, and are reported by ar_unapplied_receipts.

Confidence: written from the documented R12 data model; NOT yet executed
against a live instance (the stack answers from a mock — see CLAUDE.md).
Assumptions to verify on first live access: RA_CUSTOMER_TRX_ALL.BILL_TO_
CUSTOMER_ID joining HZ_CUST_ACCOUNTS, AR_PAYMENT_SCHEDULES_ALL.TRX_NUMBER on
PMT rows, and RA_INTERFACE_ERRORS_ALL joining on INTERFACE_LINE_ID.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.finance._common import aging_buckets, finance_envelope
from ebsmcp.tools.registry import ToolContext, ToolSet, split_total_count
from ebsmcp.tools.scoping import ROW_CAP, escape_like, org_in_clause
from ebsmcp.tools.sensitive import resolve_sensitive_call

_DEBIT_AND_CREDIT_CLASSES = "('INV', 'DM', 'CB', 'DEP', 'BR', 'CM')"
_DEBIT_CLASSES = "('INV', 'DM', 'CB', 'DEP', 'BR')"
_AR_DAYS_PAST_DUE = "(TRUNC(SYSDATE) - TRUNC(ps.due_date))"

_CUSTOMER_JOINS = (
    "LEFT JOIN APPS.HZ_CUST_ACCOUNTS hca ON hca.cust_account_id = {cust} "
    "LEFT JOIN APPS.HZ_PARTIES hp ON hp.party_id = hca.party_id "
)


def _customer_like(binds: dict[str, Any], customer_name: str) -> str:
    binds["customer_name"] = "%" + escape_like(customer_name.upper()) + "%"
    return "UPPER(hp.party_name) LIKE :customer_name ESCAPE '\\'"


def build_invoice_status_query(
    org_ids: tuple[str, ...],
    invoice_number: str | None,
    customer_name: str | None,
    open_only: bool,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Payment schedules are summed per
    transaction so an invoice with instalments is still one row."""
    scope, binds = org_in_clause("rct.org_id", org_ids)
    where = [scope]
    if invoice_number:
        where.append("rct.trx_number = :invoice_number")
        binds["invoice_number"] = invoice_number
    if customer_name:
        where.append(_customer_like(binds, customer_name))
    if open_only:
        where.append("NVL(ps.amount_due_remaining, 0) <> 0")

    sql = (
        "SELECT rct.trx_number AS invoice_number, ctt.name AS transaction_type, "
        "ctt.type AS transaction_class, rct.trx_date, hp.party_name AS customer_name, "
        "hca.account_number AS customer_account, "
        "rct.invoice_currency_code AS currency, "
        "ps.amount_due_original, ps.amount_due_remaining, ps.amount_in_dispute, "
        "ps.earliest_open_due_date, "
        "CASE WHEN ps.amount_due_remaining <> 0 "
        "THEN TRUNC(SYSDATE) - TRUNC(ps.earliest_open_due_date) END AS days_past_due, "
        "rct.complete_flag, rct.org_id, COUNT(*) OVER () AS total_count "
        "FROM APPS.RA_CUSTOMER_TRX_ALL rct "
        "LEFT JOIN APPS.RA_CUST_TRX_TYPES_ALL ctt "
        "ON ctt.cust_trx_type_id = rct.cust_trx_type_id AND ctt.org_id = rct.org_id "
        "LEFT JOIN (SELECT customer_trx_id, SUM(amount_due_original) AS amount_due_original, "
        "SUM(amount_due_remaining) AS amount_due_remaining, "
        "SUM(NVL(amount_in_dispute, 0)) AS amount_in_dispute, "
        "MIN(CASE WHEN status = 'OP' THEN due_date END) AS earliest_open_due_date "
        "FROM APPS.AR_PAYMENT_SCHEDULES_ALL GROUP BY customer_trx_id) ps "
        "ON ps.customer_trx_id = rct.customer_trx_id "
        + _CUSTOMER_JOINS.format(cust="rct.bill_to_customer_id")
        + f"WHERE {' AND '.join(where)} "
        "ORDER BY rct.trx_date DESC, rct.customer_trx_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_invoice_status(row: dict) -> dict:
    """Decide in Python what state a receivables transaction is in, so the
    model reports a fact instead of inferring one from amounts and dates.
    Pure function, unit-tested directly."""
    remaining = row.get("amount_due_remaining")
    dispute = row.get("amount_in_dispute") or 0
    days = row.get("days_past_due")

    if row.get("complete_flag") != "Y":
        status = "INCOMPLETE"
    elif remaining is None or remaining == 0:
        status = "CLOSED"
    elif dispute > 0:
        status = "DISPUTED"
    elif days is not None and days > 0:
        status = "OVERDUE"
    else:
        status = "OPEN"
    return {**row, "invoice_status": status}


def build_aging_query(
    org_ids: tuple[str, ...],
    customer_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row per customer account and
    currency, largest balance first. GROUP BY, so total_count counts
    customers, and currency_total_outstanding covers every customer, not
    just the 50 returned."""
    scope, binds = org_in_clause("ps.org_id", org_ids)
    where = [scope, "ps.status = 'OP'", f"ps.class IN {_DEBIT_AND_CREDIT_CLASSES}"]
    if customer_name:
        where.append(_customer_like(binds, customer_name))

    sql = (
        "SELECT hp.party_name AS customer_name, hca.account_number AS customer_account, "
        "ps.invoice_currency_code AS currency, "
        f"{aging_buckets(_AR_DAYS_PAST_DUE, 'ps.amount_due_remaining')}, "
        "SUM(ps.amount_due_remaining) AS total_outstanding, "
        "SUM(SUM(ps.amount_due_remaining)) OVER (PARTITION BY ps.invoice_currency_code) "
        "AS currency_total_outstanding, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AR_PAYMENT_SCHEDULES_ALL ps "
        + _CUSTOMER_JOINS.format(cust="ps.customer_id")
        + f"WHERE {' AND '.join(where)} "
        "GROUP BY hp.party_name, hca.account_number, ps.invoice_currency_code "
        "ORDER BY SUM(ps.amount_due_remaining) DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_overdue_invoices_query(
    org_ids: tuple[str, ...],
    min_days_overdue: int | None,
    customer_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open debit items past due, most
    overdue first — the collections worklist."""
    scope, binds = org_in_clause("ps.org_id", org_ids)
    where = [
        scope,
        "ps.status = 'OP'",
        f"ps.class IN {_DEBIT_CLASSES}",
        "ps.amount_due_remaining > 0",
        "ps.due_date < TRUNC(SYSDATE)",
    ]
    if min_days_overdue is not None:
        where.append("TRUNC(SYSDATE) - TRUNC(ps.due_date) >= :min_days_overdue")
        binds["min_days_overdue"] = min_days_overdue
    if customer_name:
        where.append(_customer_like(binds, customer_name))

    sql = (
        "SELECT ps.trx_number AS invoice_number, ps.class AS transaction_class, "
        "hp.party_name AS customer_name, hca.account_number AS customer_account, "
        "ps.due_date, TRUNC(SYSDATE) - TRUNC(ps.due_date) AS days_overdue, "
        "ps.amount_due_remaining, NVL(ps.amount_in_dispute, 0) AS amount_in_dispute, "
        "ps.invoice_currency_code AS currency, ps.org_id, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.AR_PAYMENT_SCHEDULES_ALL ps "
        + _CUSTOMER_JOINS.format(cust="ps.customer_id")
        + f"WHERE {' AND '.join(where)} "
        "ORDER BY ps.due_date, ps.payment_schedule_id "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_unapplied_receipts_query(
    org_ids: tuple[str, ...],
    customer_name: str | None,
    min_days_unapplied: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Receipts with cash still sitting
    unapplied (including unidentified ones with no customer), oldest first.
    The unapplied amount is read from the receipt's open payment schedule,
    where it is held as a negative balance."""
    scope, binds = org_in_clause("cr.org_id", org_ids)
    where = [scope, "ps.status = 'OP'", "ps.amount_due_remaining < 0"]
    if customer_name:
        where.append(_customer_like(binds, customer_name))
    if min_days_unapplied is not None:
        where.append("TRUNC(SYSDATE) - TRUNC(cr.receipt_date) >= :min_days_unapplied")
        binds["min_days_unapplied"] = min_days_unapplied

    sql = (
        "SELECT cr.receipt_number, cr.receipt_date, cr.amount AS receipt_amount, "
        "-ps.amount_due_remaining AS unapplied_amount, cr.currency_code AS currency, "
        "cr.status, hp.party_name AS customer_name, "
        "hca.account_number AS customer_account, "
        "TRUNC(SYSDATE) - TRUNC(cr.receipt_date) AS days_unapplied, "
        "cr.org_id, COUNT(*) OVER () AS total_count "
        "FROM APPS.AR_CASH_RECEIPTS_ALL cr "
        "JOIN APPS.AR_PAYMENT_SCHEDULES_ALL ps "
        "ON ps.cash_receipt_id = cr.cash_receipt_id AND ps.class = 'PMT' "
        + _CUSTOMER_JOINS.format(cust="cr.pay_from_customer")
        + f"WHERE {' AND '.join(where)} "
        "ORDER BY cr.receipt_date, cr.cash_receipt_id "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_interface_errors_query(
    org_ids: tuple[str, ...],
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. AutoInvoice lines that failed
    validation, with the error message and the offending value, newest
    first."""
    scope, binds = org_in_clause("ril.org_id", org_ids)
    sql = (
        "SELECT ril.interface_line_id, ril.batch_source_name, "
        "ril.interface_line_context, ril.interface_line_attribute1, "
        "ril.description, ril.amount, ril.currency_code AS currency, "
        "rie.message_text, rie.invalid_value, ril.creation_date, ril.org_id, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.RA_INTERFACE_ERRORS_ALL rie "
        "JOIN APPS.RA_INTERFACE_LINES_ALL ril "
        "ON ril.interface_line_id = rie.interface_line_id "
        f"WHERE {scope} "
        "ORDER BY ril.creation_date DESC, ril.interface_line_id DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def ar_invoice_status(
        invoice_number: str | None = None,
        customer_name: str | None = None,
        open_only: bool = False,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Look up Accounts Receivable transactions (invoices, credit memos,
        debit memos) by exact transaction number and/or part of a customer
        name, newest first, with original and remaining amount, amount in
        dispute and days past due. Each carries a decided invoice_status
        (INCOMPLETE, CLOSED, DISPUTED, OVERDUE, OPEN) — report that, do not
        re-derive it. open_only=true hides fully paid items. For the whole
        collections worklist use ar_overdue_invoices; for a customer's total
        balance use ar_aging. Limited to the caller's permitted operating
        units; org_ids can only narrow that. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {
            "invoice_number": invoice_number,
            "customer_name": customer_name,
            "open_only": open_only,
            "org_ids": org_ids,
        }
        with resolve_sensitive_call(
            ctx, tool_name="ar_invoice_status", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_invoice_status_query(
                scope.effective_org_ids, invoice_number, customer_name, open_only
            )
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(
                ctx, scope, params, [annotate_invoice_status(r) for r in rows], total, "invoices"
            )

    @app.tool(annotations=read_only)
    def ar_aging(
        customer_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Accounts Receivable aging: what each customer owes, bucketed by
        days past due (not_yet_due, days_1_30, days_31_60, days_61_90,
        over_90), largest balance first, one row per customer account and
        currency — amounts in different currencies are never added together.
        Open invoices, debit memos, chargebacks, deposits, bills receivable
        and credit memos; unapplied receipts are NOT netted off (see
        ar_unapplied_receipts). currency_total_outstanding is the total for
        that currency across ALL customers, not just the rows returned.
        customer_name narrows to matching customers. Capped at 50 customers
        with total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"customer_name": customer_name, "org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="ar_aging", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_aging_query(scope.effective_org_ids, customer_name)
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "customers")

    @app.tool(annotations=read_only)
    def ar_overdue_invoices(
        min_days_overdue: int | None = None,
        customer_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List open Accounts Receivable invoices, debit memos and
        chargebacks past their due date, most overdue first, with customer,
        amount remaining, amount in dispute and days overdue — the
        collections worklist. min_days_overdue narrows to items at least that
        late. For a customer's balance by age bucket use ar_aging. Capped at
        50 rows with total_count. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {
            "min_days_overdue": min_days_overdue,
            "customer_name": customer_name,
            "org_ids": org_ids,
        }
        with resolve_sensitive_call(
            ctx, tool_name="ar_overdue_invoices", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_overdue_invoices_query(
                scope.effective_org_ids, min_days_overdue, customer_name
            )
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "invoices")

    @app.tool(annotations=read_only)
    def ar_unapplied_receipts(
        customer_name: str | None = None,
        min_days_unapplied: int | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List Accounts Receivable receipts with cash still unapplied —
        including unidentified receipts that have no customer — oldest first,
        with receipt amount, unapplied amount and days unapplied.
        min_days_unapplied narrows to receipts at least that old. Unapplied
        cash is why a customer's invoices look overdue when they have in fact
        paid; compare with ar_aging. Bank account details are never
        returned. Capped at 50 rows with total_count. Call list_ebs_instances
        first if unsure which EBS instance names this deployment has.
        """
        params = {
            "customer_name": customer_name,
            "min_days_unapplied": min_days_unapplied,
            "org_ids": org_ids,
        }
        with resolve_sensitive_call(
            ctx, tool_name="ar_unapplied_receipts", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_unapplied_receipts_query(
                scope.effective_org_ids, customer_name, min_days_unapplied
            )
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "receipts")

    @app.tool(annotations=read_only)
    def ar_interface_errors(
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List AutoInvoice interface lines that failed validation
        (RA_INTERFACE_ERRORS_ALL), newest first, with the error message and
        the invalid value, plus the batch source and order reference. Use
        when a batch of invoices did not make it into Receivables. Capped at
        50 rows with total_count. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {"org_ids": org_ids}
        with resolve_sensitive_call(
            ctx, tool_name="ar_interface_errors", domain="finance", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as scope:
            sql, binds = build_interface_errors_query(scope.effective_org_ids)
            rows, total = split_total_count(scope.connector.run(sql, binds))
            return finance_envelope(ctx, scope, params, rows, total, "errors")


ACCOUNTS_RECEIVABLE_TOOLSET = ToolSet(name="finance_accounts_receivable", register=_register)
