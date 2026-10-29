"""diagnose_invoice: the verdict logic directly (every stage, ordering, the
not_checked rule), the query builders, and the tool end to end through MCP —
including a stage whose query fails and an ambiguous invoice number."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import DIAGNOSE_INVOICE_TOOLSET, ToolContext, mount_toolsets
from ebsmcp.tools.finance import diagnose_invoice as di

NOW = datetime(2026, 10, 27, 12, 0)
ORGS = ("204", "207")

HEADER = {"invoice_id": 11, "invoice_number": "INV-1", "org_id": 204, "vendor_id": 5,
          "vendor_site_id": 9, "supplier_name": "Acme", "invoice_type": "STANDARD",
          "invoice_amount": 100, "amount_paid": 0, "currency": "USD",
          "invoice_date": datetime(2026, 9, 1), "gl_date": datetime(2026, 9, 1),
          "creation_date": datetime(2026, 9, 1), "cancelled_date": None,
          "payment_status_flag": "N", "ledger_id": 1}

TRANSFERRED = [{"event_type_code": "INVOICE VALIDATED", "event_status_code": "P",
                "process_status_code": "P", "gl_transfer_status_code": "Y", "event_date": datetime(2026, 9, 2)}]

CLEAN = dict(
    accounting=TRANSFERRED,
    interface=[], distributions=[{"flag": "A", "n": 2}], holds=[], approval=[{"status": "APPROVED", "n": 2}],
    supplier=[{"supplier_enabled": "Y", "supplier_end_date": None, "supplier_hold_all_payments": "N",
               "site_inactive_date": None, "site_hold_all_payments": "N"}],
    schedules=[{"payment_num": 1, "due_date": datetime(2026, 9, 30), "amount_remaining": 100,
                "hold_flag": "N", "payment_method_code": "CHECK", "checkrun_id": None}],
    batches=[], period=[{"period_name": "SEP-26", "closing_status": "O"}], errors={},
)


def diagnose(header=HEADER, **over):
    kw = {**CLEAN, **over}
    return di.diagnose_invoice_findings(header, now=NOW, **kw)


def codes(r):
    """Finding codes, ignoring the overdue note that the fixture's past-due
    schedule (due 2026-09-30) raises in every otherwise-clean case."""
    return [f["code"] for f in r["findings"] if f["code"] != "overdue_unpaid"]


def stage_status(r):
    return {s["stage"]: s["status"] for s in r["stages"]}


# ── queries ──────────────────────────────────────────────────────────────────


def test_header_query_is_scoped_bound_and_conventional():
    sql, binds = di.build_invoice_header_query(ORGS, "INV-1", "acme")
    validate_sql_conventions(sql)
    assert ":scm_org_0" in sql and binds["scm_org_0"] == 204
    assert binds["invoice_number"] == "INV-1" and binds["supplier_name"] == "%ACME%"
    assert "INV-1" not in sql and "ACME" not in sql


@pytest.mark.parametrize("sql,binds", [
    di.build_interface_query(ORGS, "INV-1"),
    di.build_distributions_query(11), di.build_holds_query(11), di.build_approval_query(11),
    di.build_supplier_query(5, 9), di.build_schedules_query(11),
    di.build_payment_batch_query([3, 4]), di.build_accounting_query(11),
    di.build_period_query(1, datetime(2026, 9, 1)),
])
def test_every_stage_query_is_conventional_and_bound(sql, binds):
    validate_sql_conventions(sql)
    assert binds
    assert "APPLSYS." not in sql
    low = sql.lower()
    for forbidden in ("bank_account", "bank_num", "vat_registration", "tax_reference", "iban",
                      "num_1099", "taxpayer"):
        assert forbidden not in low


def test_followup_queries_are_keyed_by_the_invoice_not_by_org_scope():
    for sql, binds in (di.build_holds_query(11), di.build_schedules_query(11),
                       di.build_accounting_query(11)):
        assert binds == {"invoice_id": 11} and "scm_org" not in sql


# ── verdicts, stage by stage ─────────────────────────────────────────────────


def test_a_healthy_waiting_invoice_is_not_stuck():
    r = diagnose(schedules=[{**CLEAN["schedules"][0], "due_date": datetime(2026, 11, 30)}],
                 accounting=[{"event_type_code": "X", "event_status_code": "P", "process_status_code": "P",
                              "gl_transfer_status_code": "Y", "event_date": NOW}])
    assert not r["stuck"] and r["blocking_stage"] is None
    assert "Waiting, not stuck" in r["verdict"] and "2026-11-30" in r["verdict"]
    assert stage_status(r)["payment_eligibility"] == "waiting"


def test_import_rejected_when_the_invoice_never_got_created():
    rows = [{"status": "REJECTED", "reject_reason": "Invoice number already exists",
             "reject_code": "DUP", "creation_date": datetime(2026, 10, 1)}]
    r = diagnose(None, interface=rows)
    assert r["stuck"] and r["blocking_stage"] == "import"
    assert "Invoice number already exists" in r["verdict"]


def test_line_level_rejections_are_named_and_later_stages_are_not_judged():
    rows = [{"status": "REJECTED", "reject_code": "ACCT DATE NOT IN OPEN PD", "reject_level": "LINE",
             "reject_reason": "Accounting date is not in an open period", "creation_date": datetime(2026, 10, 1)},
            {"status": "REJECTED", "reject_code": "ACCT DATE NOT IN OPEN PD", "reject_level": "LINE",
             "reject_reason": "Accounting date is not in an open period", "creation_date": datetime(2026, 10, 1)}]
    r = diagnose(None, interface=rows)
    assert r["findings"][0]["message"].count("not in an open period") == 1   # de-duplicated
    assert "(on the invoice lines)" in r["verdict"]
    st = stage_status(r)
    assert st["import"] == "blocked"
    assert all(v == "not_applicable" for k, v in st.items() if k != "import")


def test_a_rejected_row_with_no_recorded_reason_says_so():
    r = diagnose(None, interface=[{"status": "REJECTED", "reject_code": None, "reject_reason": None,
                                   "creation_date": datetime(2026, 10, 1)}])
    assert "reason not recorded" in r["verdict"]


def test_interface_query_covers_header_and_line_rejections():
    sql, binds = di.build_interface_query(ORGS, "INV-1")
    validate_sql_conventions(sql)
    assert "UNION ALL" in sql and "AP_INVOICE_LINES_INTERFACE" in sql
    assert sql.count(":invoice_number") == 2 and binds["invoice_number"] == "INV-1"
    assert "FETCH FIRST 50 ROWS ONLY" in sql


def test_import_not_yet_processed():
    r = diagnose(None, interface=[{"status": "NEW", "creation_date": datetime(2026, 10, 20)}])
    assert codes(r) == ["import_not_processed"]


def test_validation_blocks_and_reports_each_flag():
    r = diagnose(distributions=[{"flag": "A", "n": 1}, {"flag": "T", "n": 2}, {"flag": "NULL", "n": 1}])
    f = r["findings"][0]
    assert r["blocking_stage"] == "validation" and f["code"] == "not_validated"
    assert "3 of 4" in f["message"] and "needs revalidation" in f["message"]
    assert f["days"] == 56 and r["days_since_created"] == 56


def test_no_distributions_cannot_be_validated():
    assert codes(diagnose(distributions=[])) == ["no_distributions"]


def test_open_holds_block_with_owner_and_age():
    r = diagnose(holds=[{"hold_code": "QTY REC", "hold_reason": "Quantity received exceeded", "days_on_hold": 40}])
    f = r["findings"][0]
    assert r["blocking_stage"] == "holds" and f["owner"] == "Buyer / receiving" and f["days"] == 40
    assert "Next: Buyer / receiving" in r["verdict"]


@pytest.mark.parametrize("status", ["INITIATED", "REJECTED", "REQUIRED", "STOPPED", "NEEDS WFREAPPROVAL"])
def test_approval_states_that_block(status):
    r = diagnose(approval=[{"status": status, "n": 1}])
    assert r["blocking_stage"] == "approval" and codes(r) == [f"approval:{status}"]


@pytest.mark.parametrize("status", ["APPROVED", "NOT REQUIRED", "MANUALLY APPROVED", "WFAPPROVED", "NULL"])
def test_approval_states_that_do_not_block(status):
    assert not diagnose(approval=[{"status": status, "n": 1}])["stuck"]


def test_supplier_and_site_flags_block_payment():
    sup = {"supplier_enabled": "N", "supplier_end_date": None, "supplier_hold_all_payments": "Y",
           "site_inactive_date": datetime(2026, 1, 1), "site_hold_all_payments": "Y"}
    r = diagnose(supplier=[sup])
    assert set(codes(r)) == {"supplier_inactive", "supplier_payment_hold", "site_inactive",
                             "site_payment_hold"}
    assert r["blocking_stage"] == "payment_eligibility"


def test_schedule_hold_and_missing_payment_method_block():
    s = {**CLEAN["schedules"][0], "hold_flag": "Y", "payment_method_code": None}
    assert set(codes(diagnose(schedules=[s]))) == {"schedule_hold:1", "no_payment_method:1"}


def test_selected_into_a_payment_batch_that_did_not_finish():
    s = {**CLEAN["schedules"][0], "checkrun_id": 77}
    r = diagnose(schedules=[s], batches=[{"checkrun_id": 77, "checkrun_name": "QUICK-77", "status": "SELECTED"}])
    f = [x for x in r["findings"] if x["stage"] == "payment_run"][0]
    assert f["severity"] == "warning" and "QUICK-77" in f["message"]
    assert "selected but not yet built" in f["message"]
    assert "needs attention" in r["verdict"]


@pytest.mark.parametrize("status", ["CANCELED", "CANCELING", "CANCELLED NO PAYMENTS"])
def test_an_invoice_still_tied_to_a_cancelled_batch_is_blocked(status):
    s = {**CLEAN["schedules"][0], "checkrun_id": 77}
    r = diagnose(schedules=[s], batches=[{"checkrun_id": 77, "checkrun_name": "B", "status": status}],
                 accounting=[{"event_type_code": "X", "event_status_code": "P", "process_status_code": "P",
                              "gl_transfer_status_code": "Y", "event_date": NOW}])
    assert r["blocking_stage"] == "payment_run" and codes(r) == ["in_cancelled_batch:77"]


def test_ready_overdue_with_nothing_blocking_is_called_out_not_called_clear():
    ev = [{"event_type_code": "X", "event_status_code": "P", "process_status_code": "P",
           "gl_transfer_status_code": "Y", "event_date": NOW}]
    r = diagnose(accounting=ev)   # CLEAN schedule fell due 2026-09-30
    assert not r["stuck"] and [f["code"] for f in r["findings"]] == ["overdue_unpaid"]
    assert "27 day(s) overdue" in r["findings"][0]["message"]
    assert r["verdict"].startswith("No hard blocker, but needs attention")


def test_overdue_is_not_reported_when_something_else_blocks():
    r = diagnose(holds=[{"hold_code": "PRICE", "hold_reason": "x", "days_on_hold": 1}])
    assert "overdue_unpaid" not in codes(r)


def test_validated_invoice_with_no_accounting_events_is_not_called_clear():
    r = diagnose(accounting=[])
    assert "no_accounting_events" in codes(r)
    f = [x for x in r["findings"] if x["code"] == "no_accounting_events"][0]
    assert f["severity"] == "warning" and "cannot see Subledger Accounting" in f["message"]


def test_accounting_error_blocks_and_closed_period_is_named():
    ev = [{"event_type_code": "INVOICE VALIDATED", "event_status_code": "U",
           "process_status_code": "E", "event_date": datetime(2026, 9, 2), "gl_transfer_status_code": None}]
    r = diagnose(accounting=ev, period=[{"period_name": "SEP-26", "closing_status": "C"}])
    assert set(codes(r)) == {"accounting_error:INVOICE VALIDATED", "period_not_open"}
    assert r["blocking_stage"] == "accounting"
    assert "SEP-26" in " ".join(f["message"] for f in r["findings"])


def test_accounted_but_not_transferred_is_a_warning():
    ev = [{"event_type_code": "INVOICE VALIDATED", "event_status_code": "P",
           "process_status_code": "P", "event_date": datetime(2026, 9, 2), "gl_transfer_status_code": "N"}]
    r = diagnose(accounting=ev)
    assert codes(r) == ["not_transferred:INVOICE VALIDATED"] and not r["stuck"]


def test_accounting_is_not_judged_for_an_unvalidated_invoice():
    r = diagnose(distributions=[{"flag": "N", "n": 1}], accounting=[])
    assert stage_status(r)["accounting"] == "not_applicable"


def test_cancelled_invoice_is_not_stuck():
    r = diagnose({**HEADER, "cancelled_date": datetime(2026, 9, 5)})
    assert not r["stuck"] and "cancelled" in r["verdict"]
    assert set(stage_status(r).values()) == {"not_applicable"}


def test_paid_invoice_is_not_stuck_and_skips_payment_stages():
    s = {**CLEAN["schedules"][0], "amount_remaining": 0, "hold_flag": "Y"}
    r = diagnose({**HEADER, "payment_status_flag": "Y", "amount_paid": 100}, schedules=[s])
    assert not r["stuck"] and r["verdict"].startswith("Not stuck: the invoice is paid")


# ── ordering and honesty ─────────────────────────────────────────────────────


def test_first_stage_wins_and_later_findings_are_marked_behind_the_blocker():
    r = diagnose(distributions=[{"flag": "N", "n": 1}],
                 holds=[{"hold_code": "PRICE", "hold_reason": "x", "days_on_hold": 3}],
                 approval=[{"status": "REJECTED", "n": 1}])
    assert r["blocking_stage"] == "validation"
    assert [f["stage"] for f in r["findings"]] == ["validation", "holds", "approval"]
    assert "behind_blocker" not in r["findings"][0]
    assert all(f["behind_blocker"] for f in r["findings"][1:])


def test_a_query_that_failed_is_not_checked_never_clear():
    r = diagnose(approval=None, errors={"approval": "ORA-00904: invalid identifier"})
    assert stage_status(r)["approval"] == "not_checked"
    assert "ORA-00904" in [s for s in r["stages"] if s["stage"] == "approval"][0]["reason"]
    assert not r["stuck"] and "Approval" in r["verdict"] and "could not be checked" in r["verdict"]


def test_other_stages_still_answer_when_one_fails():
    r = diagnose(holds=[{"hold_code": "PRICE", "hold_reason": "x", "days_on_hold": 1}],
                 accounting=None, errors={"accounting": "ORA-00942"})
    assert r["blocking_stage"] == "holds" and stage_status(r)["accounting"] == "not_checked"


# ── end to end ───────────────────────────────────────────────────────────────


class Scripted(EBSConnector):
    """Answers each stage query by recognising its table; `fail` raises."""

    def __init__(self, *, headers, fail=(), **sets):
        self.headers, self.fail, self.sets, self.calls = headers, set(fail), sets, []

    def execute_query(self, sql, binds=None):
        self.calls.append(sql)
        for key, marker in (
            ("holds", "AP_HOLDS_ALL"), ("distributions", "AP_INVOICE_DISTRIBUTIONS_ALL"),
            ("approval", "AP_INVOICE_LINES_ALL"), ("supplier", "AP_SUPPLIERS sup\n") ,
            ("schedules", "AP_PAYMENT_SCHEDULES_ALL"), ("accounting", "XLA_TRANSACTION_ENTITIES"),
            ("period", "GL_PERIOD_STATUSES"), ("batches", "AP_INV_SELECTION_CRITERIA_ALL"),
            ("interface", "AP_INVOICES_INTERFACE"),
        ):
            if marker.strip() in sql and not (key == "supplier" and "AP_INVOICES_ALL" in sql):
                if key in self.fail:
                    raise RuntimeError("ORA-00904: \"X\": invalid identifier\nHelp: ...")
                return self.sets.get(key, [])
        if "FROM APPS.AP_INVOICES_ALL" in sql:
            return self.headers
        raise AssertionError(sql)


async def call(conn, args, domain="finance"):
    app = MCPServer("t")
    ctx = ToolContext(
        connectors={"TEST": conn},
        identity_resolver=StubIdentityResolver(ResolvedIdentity(
            subject="u@corp.com", environment="test", target_system="ebs", mapped_role="FIN",
            allowed_org_ids=ORGS, domain=domain)),
        entitlement=EntitlementFilter(), audit=AuditLogger(), environment="test",
        dev_subject="u@corp.com")
    mount_toolsets(app, ctx, [DIAGNOSE_INVOICE_TOOLSET])
    async with InMemoryTransport(app)._connect() as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool("diagnose_invoice", args)


def clean_sets():
    return {k: CLEAN[k] for k in ("distributions", "holds", "approval", "supplier", "schedules",
                                  "accounting", "period", "interface")}


@pytest.mark.asyncio
async def test_tool_reports_the_blocking_hold_with_disclosure():
    sets = clean_sets()
    sets["holds"] = [{"hold_code": "PRICE", "hold_reason": "Price exceeds PO 4111111111111111",
                      "hold_date": datetime(2026, 9, 3), "days_on_hold": 54}]
    res = await call(Scripted(headers=[HEADER], **sets), {"invoice_number": "INV-1"})
    assert not res.is_error, res.content[0].text
    body = json.loads(res.content[0].text)
    assert body["stuck"] and body["blocking_stage"] == "holds"
    assert body["disclosure"]["domain"] == "finance" and body["disclosure"]["values_redacted"] == 1
    assert "4111111111111111" not in res.content[0].text
    assert body["invoice"]["supplier_name"] == "Acme"


@pytest.mark.asyncio
async def test_a_failing_stage_degrades_instead_of_failing_the_call():
    res = await call(Scripted(headers=[HEADER], fail={"approval", "accounting"}, **clean_sets()),
                     {"invoice_number": "INV-1"})
    assert not res.is_error
    body = json.loads(res.content[0].text)
    st = {s["stage"]: s["status"] for s in body["stages"]}
    assert st["approval"] == "not_checked" and st["accounting"] == "not_checked"
    assert st["holds"] == "clear" and "could not be checked" in body["verdict"]


@pytest.mark.asyncio
async def test_ambiguous_invoice_number_asks_to_narrow():
    other = {**HEADER, "invoice_id": 12, "supplier_name": "Globex"}
    res = await call(Scripted(headers=[HEADER, other], **clean_sets()), {"invoice_number": "INV-1"})
    assert res.is_error and "more than one supplier" in res.content[0].text
    assert "Acme" in res.content[0].text and "Globex" in res.content[0].text


@pytest.mark.asyncio
async def test_unknown_invoice_is_an_error_but_an_interface_rejection_is_an_answer():
    res = await call(Scripted(headers=[], **clean_sets()), {"invoice_number": "NOPE"})
    assert res.is_error and "No invoice" in res.content[0].text

    sets = clean_sets()
    sets["interface"] = [{"status": "REJECTED", "reject_code": "DUP",
                          "reject_reason": "Duplicate invoice number", "creation_date": datetime(2026, 10, 1)}]
    res = await call(Scripted(headers=[], **sets), {"invoice_number": "INV-9"})
    body = json.loads(res.content[0].text)
    assert body["stuck"] and body["blocking_stage"] == "import" and body["invoice"] is None


@pytest.mark.asyncio
async def test_non_finance_caller_is_denied_before_any_query():
    conn = Scripted(headers=[HEADER], **clean_sets())
    res = await call(conn, {"invoice_number": "INV-1"}, domain="scm")
    assert res.is_error and "No identity mapping" in res.content[0].text and conn.calls == []
