"""diagnose_order: one composite call that explains why a sales order is
late, in the spirit of diagnose_job and diagnose_request.

A late order is rarely one thing. This runs five reviewed queries — the
order header, its lines with on-hand at the shipping warehouse, active holds
(order and line level), shipping delivery lines, and errored Order
Management workflow activities — then decides the findings in Python
(diagnose_order_findings) so the model narrates a verdict instead of
inferring one from five result sets. Findings are grouped by cause with the
affected line numbers, most blocking first.

Scoping: only the header query takes the caller's Org IDs; every follow-up
is keyed by the header_id and org_id lifted from that already-scoped row,
never from caller input. An order number can repeat across operating units,
so more than one match is an error asking the caller to narrow with org_ids,
not a guess.

Confidence: written from the documented R12 data model and unit-tested; NOT
yet run against a live instance, because the EBS connector was unavailable
when this was written. Assumptions to verify on first live access, in the
order they are most likely to fail with ORA-00904/00942: WSH_DELIVERY_DETAILS
.SOURCE_HEADER_ID / SOURCE_LINE_ID / LAST_UPDATE_DATE; OE_ORDER_LINES_ALL
.SCHEDULE_SHIP_DATE / PROMISE_DATE / ACTUAL_SHIPMENT_DATE / SHIP_FROM_ORG_ID /
FLOW_STATUS_CODE; WF_ITEM_ACTIVITY_STATUSES.ERROR_NAME / ERROR_MESSAGE with
item types OEOH (item_key = header_id) and OEOL (item_key = line_id);
WSH_DELIVERY_DETAILS.RELEASED_STATUS codes (R ready, B backordered, S
released to warehouse, Y staged, C shipped, N not ready, D cancelled).
Reservations are deliberately not consulted: the DEMAND_SOURCE_* columns on
MTL_RESERVATIONS differ by demand type and were not worth guessing, so stock
is compared as raw on-hand at the line's warehouse and says so. The hold join
chain (OE_ORDER_HOLDS_ALL -> OE_HOLD_SOURCES_ALL -> OE_HOLD_DEFINITIONS) was
executed live on 2026-10-15 by orders_on_hold. An empty findings list means
none of these checks fired, not that the order is healthy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import ROW_CAP, SCM_DOMAINS, org_in_clause

RELEASED_STATUS_LABELS = {
    "R": "Ready to Release",
    "B": "Backordered",
    "S": "Released to Warehouse",
    "Y": "Staged",
    "C": "Shipped",
    "N": "Not Ready to be Released",
    "D": "Cancelled",
    "X": "Not Applicable",
}

_TERMINAL_HEADER = {"CLOSED", "CANCELLED"}
_MAX_LISTED_LINES = 10

_CUSTOMER_JOINS = (
    "LEFT JOIN APPS.HZ_CUST_ACCOUNTS hca ON hca.cust_account_id = ooh.sold_to_org_id "
    "LEFT JOIN APPS.HZ_PARTIES hp ON hp.party_id = hca.party_id "
)


def build_order_header_query(
    org_ids: tuple[str, ...], order_number: int
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Up to five matches, so an order
    number reused across operating units is detected rather than guessed."""
    scope, binds = org_in_clause("ooh.org_id", org_ids)
    binds["order_number"] = order_number
    sql = (
        "SELECT ooh.header_id, ooh.org_id, ooh.order_number, ooh.flow_status_code, "
        "ooh.booked_flag, ooh.open_flag, ooh.cancelled_flag, ooh.ordered_date, "
        "ooh.request_date, hp.party_name AS customer_name "
        "FROM APPS.OE_ORDER_HEADERS_ALL ooh "
        f"{_CUSTOMER_JOINS}"
        f"WHERE {scope} AND ooh.order_number = :order_number "
        "FETCH FIRST 5 ROWS ONLY"
    )
    return sql, binds


def build_order_lines_query(header_id: int, org_id: int) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Takes ids lifted from the scoped
    header row. Includes on-hand at the line's own shipping warehouse; not
    netted for reservations."""
    sql = (
        "SELECT oel.line_id, oel.line_number, oel.shipment_number, oel.ordered_item, "
        "oel.ship_from_org_id, oel.flow_status_code, oel.ordered_quantity, "
        "NVL(oel.shipped_quantity, 0) AS shipped_quantity, oel.schedule_ship_date, "
        "oel.promise_date, oel.request_date, oel.actual_shipment_date, oel.open_flag, "
        "(SELECT NVL(SUM(moq.transaction_quantity), 0) "
        "FROM APPS.MTL_ONHAND_QUANTITIES_DETAIL moq "
        "WHERE moq.inventory_item_id = oel.inventory_item_id "
        "AND moq.organization_id = oel.ship_from_org_id) AS onhand_quantity "
        "FROM APPS.OE_ORDER_LINES_ALL oel "
        "WHERE oel.header_id = :header_id AND oel.org_id = :org_id "
        "AND NVL(oel.cancelled_flag, 'N') <> 'Y' "
        "ORDER BY oel.line_number, oel.shipment_number "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, {"header_id": header_id, "org_id": org_id}


def build_order_holds_query(header_id: int, org_id: int) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Active holds at order level
    (line_id NULL) and line level, longest-held first."""
    sql = (
        "SELECT oh.line_id, ohd.name AS hold_name, oh.creation_date AS hold_date, "
        "TRUNC(SYSDATE) - TRUNC(oh.creation_date) AS days_on_hold "
        "FROM APPS.OE_ORDER_HOLDS_ALL oh "
        "JOIN APPS.OE_HOLD_SOURCES_ALL ohs ON ohs.hold_source_id = oh.hold_source_id "
        "JOIN APPS.OE_HOLD_DEFINITIONS ohd ON ohd.hold_id = ohs.hold_id "
        "WHERE oh.header_id = :header_id AND oh.org_id = :org_id "
        "AND oh.released_flag = 'N' "
        "ORDER BY oh.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, {"header_id": header_id, "org_id": org_id}


def build_order_shipping_query(header_id: int) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Shipping delivery lines for the
    order, with the delivery each is on (if any)."""
    sql = (
        "SELECT wdd.source_line_id AS line_id, wdd.released_status, "
        "wdd.requested_quantity, wnd.name AS delivery_name, "
        "wnd.status_code AS delivery_status, wdd.creation_date, "
        "TRUNC(SYSDATE) - TRUNC(wdd.last_update_date) AS days_since_update "
        "FROM APPS.WSH_DELIVERY_DETAILS wdd "
        "LEFT JOIN APPS.WSH_DELIVERY_ASSIGNMENTS wda "
        "ON wda.delivery_detail_id = wdd.delivery_detail_id "
        "LEFT JOIN APPS.WSH_NEW_DELIVERIES wnd ON wnd.delivery_id = wda.delivery_id "
        "WHERE wdd.source_code = 'OE' AND wdd.source_header_id = :header_id "
        "ORDER BY wdd.source_line_id "
        f"FETCH FIRST {ROW_CAP * 2} ROWS ONLY"
    )
    return sql, {"header_id": header_id}


def build_order_workflow_errors_query(header_id: int) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Errored Order Management
    workflow activities on the header (OEOH) or any of its lines (OEOL)."""
    sql = (
        "SELECT wias.item_type, wias.item_key, wias.error_name, wias.error_message, "
        "wias.begin_date "
        "FROM APPS.WF_ITEM_ACTIVITY_STATUSES wias "
        "WHERE wias.activity_status = 'ERROR' AND ("
        "(wias.item_type = 'OEOH' AND wias.item_key = TO_CHAR(:header_id)) "
        "OR (wias.item_type = 'OEOL' AND wias.item_key IN ("
        "SELECT TO_CHAR(oel.line_id) FROM APPS.OE_ORDER_LINES_ALL oel "
        "WHERE oel.header_id = :header_id))) "
        "ORDER BY wias.begin_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, {"header_id": header_id}


def _days(later: datetime, earlier: Any) -> int | None:
    if not isinstance(earlier, datetime):
        return None
    return (later - earlier).days


def _line_label(line: dict) -> str:
    number = line.get("line_number")
    shipment = line.get("shipment_number")
    return f"{number}.{shipment}" if shipment not in (None, "") else str(number)


def diagnose_order_findings(
    header: dict,
    lines: list[dict],
    holds: list[dict],
    shipping: list[dict],
    workflow_errors: list[dict],
    now: datetime,
) -> dict:
    """Pure function, unit-tested directly. Turns the raw result sets into
    findings grouped by cause (most blocking first), a lateness summary, and
    the lines annotated with their due date and days late.

    Severity: blocking (stops the order moving), warning (needs attention),
    info (context)."""
    grouped: dict[str, dict] = {}

    def add(
        code: str, severity: str, message: str, line: str | None = None, key: str | None = None
    ) -> None:
        # Findings that share a cause and a message group under one entry with
        # the affected line numbers. Causes that differ in substance — two
        # different holds, two different workflow errors — pass a key so one
        # does not swallow the other.
        entry = grouped.setdefault(
            key or code, {"severity": severity, "code": code, "message": message, "lines": []}
        )
        if line is not None and line not in entry["lines"]:
            entry["lines"].append(line)

    flow = (header.get("flow_status_code") or "").upper()
    if flow in _TERMINAL_HEADER or header.get("cancelled_flag") == "Y":
        add("terminal_status", "info",
            f"Order is {flow.title() or 'cancelled'}; no further fulfilment is expected.")
    elif header.get("booked_flag") != "Y":
        add("not_booked", "blocking",
            "Order is not booked; it will not be scheduled or released until it is.")

    lines_by_id = {ln.get("line_id"): ln for ln in lines}
    ship_by_line: dict[Any, list[dict]] = {}
    for row in shipping:
        ship_by_line.setdefault(row.get("line_id"), []).append(row)

    for hold in holds:
        target = lines_by_id.get(hold.get("line_id"))
        age = hold.get("days_on_hold")
        add(
            "active_hold", "blocking",
            f"Active hold on {'a line' if target else 'the order'}: {hold.get('hold_name')}"
            + (f" ({age} day(s))." if age is not None else "."),
            _line_label(target) if target else None,
            key=f"active_hold:{hold.get('hold_name')}:{bool(target)}",
        )

    for err in workflow_errors:
        text = err.get("error_message") or err.get("error_name") or "no error text recorded"
        add("workflow_error", "blocking",
            f"Order Management workflow activity in error ({err.get('item_type')}): {text}",
            key=f"workflow_error:{err.get('item_type')}:{text}")

    annotated: list[dict] = []
    max_days_late = 0
    late_lines = 0
    shipped_late = 0

    for line in lines:
        label = _line_label(line)
        due = line.get("promise_date") or line.get("request_date")
        shipped_on = line.get("actual_shipment_date")
        outstanding = (line.get("ordered_quantity") or 0) - (line.get("shipped_quantity") or 0)
        is_open = line.get("open_flag") == "Y" and outstanding > 0
        days_late = _days(now, due) if (is_open and due) else None
        if days_late is not None and days_late <= 0:
            days_late = None

        if not is_open and isinstance(shipped_on, datetime) and isinstance(due, datetime):
            if (shipped_on - due).days > 0:
                shipped_late += 1
        if days_late:
            late_lines += 1
            max_days_late = max(max_days_late, days_late)

        annotated.append({**line, "line": label, "due_date": due, "days_late": days_late,
                          "quantity_outstanding": outstanding if is_open else 0})
        if not is_open:
            continue

        if not line.get("schedule_ship_date"):
            add("unscheduled", "blocking",
                "Line has no schedule ship date; scheduling has not run or failed.", label)
        elif _days(now, line["schedule_ship_date"]) and _days(now, line["schedule_ship_date"]) > 0:
            add("past_schedule_ship", "warning",
                "Scheduled ship date has passed and the line has not shipped.", label)

        rows = ship_by_line.get(line.get("line_id"), [])
        statuses = {r.get("released_status") for r in rows}
        if "B" in statuses:
            add("backordered", "blocking",
                "Backordered at pick release: stock could not be allocated.", label)
        if "R" in statuses:
            add("awaiting_pick_release", "warning",
                "Delivery line is ready to release but pick release has not run.", label)
        if "S" in statuses:
            add("awaiting_pick_confirm", "warning",
                "Released to the warehouse; the pick has not been confirmed.", label)
        if "Y" in statuses:
            add("awaiting_ship_confirm", "warning",
                "Picked and staged; ship confirm has not been done.", label)
        if "N" in statuses:
            add("shipping_not_ready", "warning",
                "Delivery line is not ready to be released (shipping prerequisites missing).",
                label)
        if not rows and (line.get("flow_status_code") or "").upper() == "AWAITING_SHIPPING":
            add("no_delivery_line", "warning",
                "Line is awaiting shipping but has no delivery line; the shipping "
                "transfer has not happened.", label)

        onhand = line.get("onhand_quantity")
        picking_started = bool(statuses & {"S", "Y"})
        if onhand is not None and onhand < outstanding and not picking_started:
            add("insufficient_onhand", "warning",
                "On-hand at the shipping warehouse is below the quantity still to ship "
                "(raw on-hand, not netted for reservations).", label)

    if shipped_late:
        add("shipped_late", "info", f"{shipped_late} line(s) already shipped after their due date.")

    order = {"blocking": 0, "warning": 1, "info": 2}
    findings = sorted(
        grouped.values(), key=lambda f: (order[f["severity"]], f["code"], f["message"])
    )
    for f in findings:
        listed = f["lines"][:_MAX_LISTED_LINES]
        more = len(f["lines"]) - len(listed)
        f["lines"] = listed
        if more > 0:
            f["more_lines"] = more

    return {
        "late": late_lines > 0,
        "late_lines": late_lines,
        "max_days_late": max_days_late,
        "blocked": any(f["severity"] == "blocking" for f in findings),
        "findings": findings,
        "lines": annotated,
    }


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True
        )
    )
    def diagnose_order(
        order_number: int,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Explain why one sales order is late, in a single call. Checks
        whether it is booked, every line's schedule ship date against its
        promise/request date, active holds (order and line level), errored
        Order Management workflow activities, shipping status per line
        (backordered, awaiting pick release, pick confirm or ship confirm,
        or never transferred to shipping), and on-hand at each line's
        warehouse. Returns findings grouped by cause with the affected line
        numbers, blocking first, plus how many lines are late and by how
        many days. An empty findings list means none of those checks fired.
        An order number can repeat across operating units; if it does, pass
        org_ids to pick one. For a list of orders on hold use orders_on_hold;
        for plain status use sales_order_status. Call list_ebs_instances
        first if unsure which EBS instance names this deployment has.
        """
        params = {"order_number": order_number, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="diagnose_order", target_system="ebs", domains=SCM_DOMAINS,
            params=params, requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            headers = connector.run(*build_order_header_query(effective_org_ids, order_number))
            if not headers:
                raise ToolError(
                    f"No sales order {order_number} found within the caller's permitted "
                    "operating units."
                )
            if len(headers) > 1:
                raise ToolError(
                    f"Order number {order_number} exists in more than one operating unit "
                    f"({', '.join(str(h['org_id']) for h in headers)}); pass org_ids to "
                    "choose one."
                )
            header = headers[0]
            header_id, org_id = header["header_id"], header["org_id"]

            def run(built: tuple[str, dict[str, Any]]) -> list[dict]:
                rows, _ = split_total_count(connector.run(*built))
                return rows

            lines = run(build_order_lines_query(header_id, org_id))
            holds = run(build_order_holds_query(header_id, org_id))
            shipping = run(build_order_shipping_query(header_id))
            workflow_errors = run(build_order_workflow_errors_query(header_id))

            diagnosis = diagnose_order_findings(
                header, lines, holds, shipping, workflow_errors, datetime.now()
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "order": {
                    k: header.get(k)
                    for k in (
                        "order_number", "org_id", "customer_name", "flow_status_code",
                        "booked_flag", "ordered_date", "request_date",
                    )
                },
                **diagnosis,
                "holds": holds,
                "shipping": [
                    {**r, "status": RELEASED_STATUS_LABELS.get(r.get("released_status"), "Unknown")}
                    for r in shipping
                ],
                "workflow_errors": workflow_errors,
            }


DIAGNOSE_ORDER_TOOLSET = ToolSet(name="scm_diagnose_order", register=_register)
