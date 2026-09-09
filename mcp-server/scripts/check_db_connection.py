#!/usr/bin/env python
"""Confirm the MCP server can actually reach every EBS database you configured.

Run this after filling in connection details, before starting the server —
a bad DSN or password shows up here in seconds, instead of as a failed tool
call later.

Usage:
    python scripts/check_db_connection.py                    # reads the environment
    python scripts/check_db_connection.py --env-file ../.env # or a file
    python scripts/check_db_connection.py --instance PROD    # just one

Reads the same variables the server does: EBSMCP_EBS_INSTANCES for several
databases, or the singular EBS_DB_DSN / EBS_DB_USER / EBS_DB_PASSWORD for
one. Needs only python-oracledb, which runs in thin mode — no Oracle Instant
Client install required on the machine you are testing from.

Exit code is 0 only if every instance connected, so this is usable as a
deployment gate. Passwords are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# What each instance is asked once connected. Deliberately trivial — this
# script answers "can I reach this database as this user", not "does this
# account hold every grant the tools need". GV$INSTANCE is the same view
# server_health uses, and naming the database back is what catches the
# likeliest configuration mistake: a DSN that connects fine, to the wrong
# environment.
PROBE_SQL = """
    SELECT i.instance_name, i.host_name, i.version, i.status, d.name AS db_name
    FROM GV$INSTANCE i CROSS JOIN V$DATABASE d
    WHERE i.inst_id = (SELECT MIN(inst_id) FROM GV$INSTANCE)
"""

# Falls back to this when the account cannot read GV$INSTANCE — a connection
# that works but lacks SELECT_CATALOG_ROLE is a real and useful distinction,
# so it is reported as a warning rather than a failure.
FALLBACK_SQL = "SELECT USER AS db_user, SYSDATE AS db_time FROM DUAL"


def load_env_file(path: str) -> None:
    """Minimal .env reader — enough for KEY=VALUE lines, ignoring comments.

    Deliberately not python-dotenv: this script is meant to be droppable on
    a server that has nothing installed but oracledb.
    """
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def describe_env_state() -> str:
    """Why no instances were found, in terms the reader can act on.

    "Not configured" is three different situations with three different
    fixes, and collapsing them into one message sends people to the wrong
    file — which is exactly what happens when the variable is set in
    mcp-server/.env and the stack is run with docker compose.
    """
    raw = os.environ.get("EBSMCP_EBS_INSTANCES")

    if raw is not None and raw.strip() == "{}":
        return (
            "EBSMCP_EBS_INSTANCES is set, but to an empty object: {}\n\n"
            "That is docker-compose's DEFAULT, from ${EBSMCP_EBS_INSTANCES:-{}} in\n"
            "docker-compose.yml. It means Compose did not find the variable.\n\n"
            "Compose reads ONLY the file named exactly '.env' beside\n"
            "docker-compose.yml. It does NOT read mcp-server/.env — that file is\n"
            "used only when running the server directly on the host.\n\n"
            "Fix: put EBSMCP_EBS_INSTANCES in the ROOT .env, or point this script\n"
            "at the file that has it:\n"
            "    --env-file /path/to/mcp-server/.env"
        )

    if raw is not None and not raw.strip():
        return (
            "EBSMCP_EBS_INSTANCES is set but empty.\n\n"
            "A common cause is ${VAR:-} in docker-compose.yml with VAR unset —\n"
            "that passes an empty string, not 'unset'."
        )

    return (
        "No EBS connection configured — EBSMCP_EBS_INSTANCES is not set at all,\n"
        "and neither is EBS_DB_DSN.\n\n"
        "Set one of them, or pass --env-file pointing at the .env that holds them.\n"
        "Note that docker compose reads only the .env beside docker-compose.yml,\n"
        "not mcp-server/.env."
    )


def instances_from_env() -> dict[str, dict]:
    raw = os.environ.get("EBSMCP_EBS_INSTANCES", "").strip()
    if raw and raw != "{}":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(
                f"EBSMCP_EBS_INSTANCES is not valid JSON: {exc}\n"
                "It must be a single line — no newlines, no trailing commas. "
                "See ebs-instances.sample.env."
            )
        return {name.upper(): cfg for name, cfg in parsed.items()}

    dsn = os.environ.get("EBS_DB_DSN")
    if dsn:
        return {
            os.environ.get("EBSMCP_ENVIRONMENT", "DEFAULT").upper(): {
                "dsn": dsn,
                "user": os.environ.get("EBS_DB_USER", ""),
                "password": os.environ.get("EBS_DB_PASSWORD", ""),
            }
        }

    sys.exit(describe_env_state())


def check(name: str, cfg: dict, timeout: int) -> tuple[bool, str]:
    import oracledb

    dsn, user = cfg.get("dsn", ""), cfg.get("user", "")
    if not dsn or not user or not cfg.get("password"):
        return False, "incomplete config — needs dsn, user and password"

    try:
        # Without an explicit timeout a host that accepts packets but never
        # answers blocks for ~20s each. With a list of instances that is
        # minutes of waiting to discover something you already suspect.
        with oracledb.connect(
            user=user, password=cfg["password"], dsn=dsn, tcp_connect_timeout=timeout
        ) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(PROBE_SQL)
                    inst, host, version, status, db = cur.fetchone()
                    return True, f"{db} / {inst} on {host} — Oracle {version}, {status}"
                except oracledb.DatabaseError:
                    cur.execute(FALLBACK_SQL)
                    who, _when = cur.fetchone()
                    return True, (
                        f"connected as {who}, but GV$INSTANCE is not readable — "
                        "the account is missing SELECT_CATALOG_ROLE, so most "
                        "tools will fail. Connection itself is fine."
                    )
    except Exception as exc:  # noqa: BLE001 — report anything, never crash mid-run
        # str(exc) carries the ORA- code, which is the useful part. It never
        # contains the password.
        return False, str(exc).strip().splitlines()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="Read variables from this file first.")
    parser.add_argument("--instance", help="Check only this instance (case-insensitive).")
    parser.add_argument(
        "--timeout", type=int, default=10,
        help="Seconds to wait for each connection before giving up (default 10).",
    )
    parser.add_argument(
        "--serial", action="store_true",
        help="Check one at a time instead of concurrently. Slower, but the output "
             "streams as it goes — useful when one instance is hanging and you "
             "want to see which.",
    )
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    try:
        import oracledb  # noqa: F401
    except ImportError:
        sys.exit("python-oracledb is not installed. Run: pip install oracledb")

    instances = instances_from_env()
    if args.instance:
        wanted = args.instance.upper()
        if wanted not in instances:
            sys.exit(
                f"Unknown instance {args.instance!r}. Configured: "
                f"{', '.join(sorted(instances))}"
            )
        instances = {wanted: instances[wanted]}

    print(f"Checking {len(instances)} EBS instance(s), {args.timeout}s timeout each\n")

    # A duplicated DSN across two names is almost always a copy-paste slip,
    # and it would otherwise look like two healthy instances.
    seen: dict[str, str] = {}
    for name, cfg in sorted(instances.items()):
        dsn = cfg.get("dsn", "")
        if dsn in seen:
            print(f"  WARNING  {name} and {seen[dsn]} share the same DSN — likely a copy-paste slip\n")
        seen[dsn] = name
    # Checked concurrently, in PROCESSES rather than threads. Measured:
    # python-oracledb's thin-mode connect holds a lock, so five unreachable
    # hosts took 25s across a thread pool — no better than serial — and 5s
    # across a process pool. Raw sockets parallelise fine in threads, so the
    # serialisation is inside the driver, not the network.
    #
    # Results are collected then printed in name order, so output stays
    # deterministic regardless of which finished first.
    if args.serial or len(instances) == 1:
        results = {n: check(n, instances[n], args.timeout) for n in sorted(instances)}
    else:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=min(len(instances), 8)) as pool:
            futures = {
                name: pool.submit(check, name, cfg, args.timeout)
                for name, cfg in instances.items()
            }
            results = {name: fut.result() for name, fut in futures.items()}

    failures = 0
    for name in sorted(instances):
        cfg = instances[name]
        ok, detail = results[name]
        if ok:
            print(f"  PASS  {name:<10} {cfg.get('user','?')}@{cfg.get('dsn','?')}")
            print(f"        {detail}")
        else:
            failures += 1
            print(f"  FAIL  {name:<10} {cfg.get('user','?')}@{cfg.get('dsn','?')}")
            print(f"        {detail}")
        print()

    total = len(instances)
    print(f"{total - failures}/{total} reachable")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
