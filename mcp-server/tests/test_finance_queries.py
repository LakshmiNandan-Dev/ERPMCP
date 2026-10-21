"""AP and AR: the pure query builders and annotators directly, shared SQL
properties asserted for every query in one place, and the tools end to end
through MCP (finance-domain pinning, org scope, no leaked total_count)."""

from __future__ import annotations

import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import (
    ACCOUNTS_PAYABLE_TOOLSET,
    ACCOUNTS_RECEIVABLE_TOOLSET,
    ToolContext,
    mount_toolsets,
)
from ebsmcp.tools.finance import accounts_payable as ap
from ebsmcp.tools.finance import accounts_receivable as ar

ORGS = ("204", "207")

ALL_QUERIES = [
    ap.build_invoice_status_query(ORGS, "INV-1", "acme", "UNPAID"),
    ap.build_holds_query(ORGS, "qty rec", 5),
    ap.build_aging_query(ORGS, "acme"),
    ap.build_payment_history_query(ORGS, "acme", 1001, "INV-1", 30),
    ap.build_interface_rejections_query(ORGS),
    ar.build_invoice_status_query(ORGS, "5001", "acme", True),
    ar.build_aging_query(ORGS, "acme"),
    ar.build_overdue_invoices_query(ORGS, 30, "acme"),
    ar.build_unapplied_receipts_query(ORGS, "acme", 10),
    ar.build_interface_errors_query(ORGS),
]


@pytest.mark.parametrize("sql,binds", ALL_QUERIES)
def test_every_query_is_org_scoped_capped_and_conventional(sql, binds):
    validate_sql_conventions(sql)
    assert ":scm_org_0" in sql and ":scm_org_1" in sql
    assert binds["scm_org_0"] == 204 and binds["scm_org_1"] == 207
    assert "FETCH FIRST 50 ROWS ONLY" in sql
    assert "COUNT(*) OVER () AS total_count" in sql
    assert "DISTINCT" not in sql
    assert "APPLSYS." not in sql


@pytest.mark.parametrize("sql,binds", ALL_QUERIES)
def test_no_caller_value_reaches_sql_text(sql, binds):
    for name, value in binds.items():
        # payment_status_flag is a one-letter code from a fixed map, not caller text
        if isinstance(value, str) and name != "payment_status_flag":
            assert value.strip("%") not in sql


@pytest.mark.parametrize("sql,_", ALL_QUERIES)
def test_no_bank_or_tax_column_is_selected(sql, _):
    low = sql.lower()
    for forbidden in ("bank_account", "bank_num", "vat_registration", "tax_reference",
                      "num_1099", "iban", "taxpayer", "credit_card", "card_number",
                      "cc_", "ssn", "national_identifier", "payment_trxn_extension"):
        assert forbidden not in low


def test_optional_filters_only_appear_when_given():
    _, binds = ap.build_invoice_status_query(ORGS, None, None, None)
    assert set(binds) == {"scm_org_0", "scm_org_1"}
    _, binds = ar.build_overdue_invoices_query(ORGS, None, None)
    assert set(binds) == {"scm_org_0", "scm_org_1"}


def test_ap_payment_status_maps_to_flag_through_a_bind():
    sql, binds = ap.build_invoice_status_query(ORGS, None, None, "PARTIALLY_PAID")
    assert "ai.payment_status_flag = :payment_status_flag" in sql
    assert binds["payment_status_flag"] == "P"


def test_ap_validation_is_read_from_distributions_not_lines():
    sql, _ = ap.build_invoice_status_query(ORGS, None, None, None)
    assert "AP_INVOICE_DISTRIBUTIONS_ALL" in sql and "AP_INVOICE_LINES_ALL" not in sql


def test_ap_holds_read_only_unreleased_holds_on_live_invoices():
    sql, binds = ap.build_holds_query(ORGS, "qty rec", None)
    assert "ah.release_lookup_code IS NULL" in sql
    assert "ai.cancelled_date IS NULL" in sql
    assert binds["hold_code"] == "QTY REC"


def test_aging_groups_by_currency_and_reports_all_supplier_total():
    for sql, _ in (ap.build_aging_query(ORGS, None), ar.build_aging_query(ORGS, None)):
        assert "GROUP BY" in sql
        assert "PARTITION BY" in sql and "currency_total_outstanding" in sql
        for bucket in ("not_yet_due", "days_1_30", "days_31_60", "days_61_90", "over_90"):
            assert bucket in sql


def test_ar_unapplied_receipts_read_the_open_receipt_schedule():
    sql, _ = ar.build_unapplied_receipts_query(ORGS, None, None)
    assert "ps.class = 'PMT'" in sql and "ps.amount_due_remaining < 0" in sql
    assert "LEFT JOIN APPS.HZ_CUST_ACCOUNTS" in sql  # unidentified receipts kept


def test_ar_overdue_excludes_credits_and_paid_items():
    sql, _ = ar.build_overdue_invoices_query(ORGS, None, None)
    assert "ps.amount_due_remaining > 0" in sql and "'CM'" not in sql


def test_like_wildcards_in_names_are_escaped():
    _, binds = ap.build_aging_query(ORGS, "50%_off")
    assert binds["supplier_name"] == "%50\\%\\_OFF%"
    _, binds = ar.build_aging_query(ORGS, "50%_off")
    assert binds["customer_name"] == "%50\\%\\_OFF%"


# ── card / SSN redaction ─────────────────────────────────────────────────────

from ebsmcp.tools.finance._common import REDACTED, redact_rows, redact_sensitive_text  # noqa: E402


def test_valid_card_numbers_and_ssns_are_redacted():
    for text in ("4111111111111111", "card 4111 1111 1111 1111 declined", "4111-1111-1111-1111"):
        out, n = redact_sensitive_text(text)
        assert n == 1 and "4111" not in out and REDACTED in out
    out, n = redact_sensitive_text("tax id 123-45-6789 invalid")
    assert n == 1 and "123-45-6789" not in out


def test_ordinary_numbers_survive():
    for text in ("Invoice 4111111111111112 not found", "PO 20260915-0042", "amount 1234567.89",
                 "order 12345678"):
        assert redact_sensitive_text(text) == (text, 0)


def test_only_free_text_columns_are_scanned():
    rows, n = redact_rows([{"invoice_number": "4111111111111111", "message_text": "4111111111111111"}])
    assert n == 1
    assert rows[0]["invoice_number"] == "4111111111111111"
    assert rows[0]["message_text"] == REDACTED


# ── annotators ───────────────────────────────────────────────────────────────


def _inv(**kw):
    base = dict(open_holds=0, distributions_total=2, distributions_validated=2,
                payment_status_flag="N", cancelled_date=None)
    return ap.annotate_invoice_status({**base, **kw})


def test_ap_status_precedence():
    assert _inv(cancelled_date="2026-01-01", open_holds=1)["invoice_status"] == "CANCELLED"
    assert _inv(payment_status_flag="Y", open_holds=1)["invoice_status"] == "PAID"
    assert _inv(open_holds=2)["invoice_status"] == "ON_HOLD"
    assert _inv(distributions_validated=1)["invoice_status"] == "NOT_VALIDATED"
    assert _inv(distributions_total=0, distributions_validated=0)["invoice_status"] == "NOT_VALIDATED"
    assert _inv(payment_status_flag="P")["invoice_status"] == "PARTIALLY_PAID"
    assert _inv()["invoice_status"] == "READY_FOR_PAYMENT"


def test_ap_blocker_only_where_something_blocks_payment():
    assert "2 open hold" in _inv(open_holds=2)["payment_blocker"]
    assert "1 of 2 distribution" in _inv(distributions_validated=1)["payment_blocker"]
    assert _inv()["payment_blocker"] is None


def _trx(**kw):
    base = dict(complete_flag="Y", amount_due_remaining=100, amount_in_dispute=0,
                days_past_due=-5)
    return ar.annotate_invoice_status({**base, **kw})["invoice_status"]


def test_ar_status_precedence():
    assert _trx(complete_flag="N") == "INCOMPLETE"
    assert _trx(amount_due_remaining=0) == "CLOSED"
    assert _trx(amount_due_remaining=None) == "CLOSED"
    assert _trx(amount_in_dispute=10, days_past_due=40) == "DISPUTED"
    assert _trx(days_past_due=1) == "OVERDUE"
    assert _trx() == "OPEN"


# ── end to end ───────────────────────────────────────────────────────────────


class Recording(EBSConnector):
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        return self.rows


def build_app(connector, domain="finance"):
    app = MCPServer("fin-test")
    ctx = ToolContext(
        connectors={"TEST": connector},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject="u@corp.com", environment="test", target_system="ebs",
                mapped_role="FIN_USER", allowed_org_ids=ORGS, domain=domain,
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject="u@corp.com",
    )
    mount_toolsets(app, ctx, [ACCOUNTS_PAYABLE_TOOLSET, ACCOUNTS_RECEIVABLE_TOOLSET])
    return app


async def call(app, name, args):
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


TOOLS = [
    "ap_invoice_status", "ap_invoices_on_hold", "ap_aging", "ap_payment_history",
    "ap_interface_rejections", "ar_invoice_status", "ar_aging", "ar_overdue_invoices",
    "ar_unapplied_receipts", "ar_interface_errors",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_finance_tools_deny_a_non_finance_caller(tool):
    conn = Recording([])
    result = await call(build_app(conn, domain="scm"), tool, {})
    assert result.is_error and "No identity mapping" in result.content[0].text
    assert conn.calls == []


@pytest.mark.asyncio
async def test_ap_invoice_status_annotates_lifts_total_and_discloses():
    conn = Recording([
        {"invoice_number": "A1", "open_holds": 1, "distributions_total": 1, "distributions_validated": 1,
         "payment_status_flag": "N", "cancelled_date": None, "total_count": 9},
    ])
    result = await call(build_app(conn), "ap_invoice_status", {"invoice_number": "A1"})
    assert not result.is_error, result.content[0].text
    body = json.loads(result.content[0].text)
    assert body["total_count"] == 9 and body["row_cap"] == 50
    assert body["effective_org_ids"] == ["204", "207"]
    assert body["invoices"][0]["invoice_status"] == "ON_HOLD"
    assert "total_count" not in body["invoices"][0]
    assert body["disclosure"]["domain"] == "finance"
    assert body["disclosure"]["values_redacted"] == 0
    assert conn.calls[0][1]["invoice_number"] == "A1"


@pytest.mark.asyncio
async def test_requested_orgs_narrow_but_never_widen():
    conn = Recording([])
    ok = await call(build_app(conn), "ar_aging", {"org_ids": ["207", "999"]})
    assert not ok.is_error
    _, binds = conn.calls[0]
    assert binds["scm_org_0"] == 207 and "scm_org_1" not in binds
    denied = await call(build_app(Recording([])), "ar_aging", {"org_ids": ["999"]})
    assert denied.is_error
