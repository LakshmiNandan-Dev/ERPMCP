#!/usr/bin/env python
"""Confirm a running MCP server is reachable, healthy, and talking to EBS.

check_db_connection.py answers "can this host reach the database". This
answers the larger question: is the deployed server actually serving, and
does a real tool call make it all the way through identity resolution,
entitlement filtering, and the connector to Oracle and back.

It calls server_health, which is the tool that exists for exactly this —
it resolves the caller, runs a real GV$INSTANCE query, and returns what
came back. A pass here means the whole request pipeline works.

Usage:
    python scripts/check_mcp_server.py                       # http://localhost:8080/mcp
    python scripts/check_mcp_server.py --url https://mcp.example.com/mcp
    python scripts/check_mcp_server.py --token "$BEARER"     # once Entra auth is on

Exit code is 0 only if every stage passed, so this is usable as a smoke
test after deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def run(url: str, token: str | None, timeout: float) -> int:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # Auth and timeout are configured on the HTTP client rather than passed
    # to streamable_http_client, which takes only a url and an http_client.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    failures = 0

    print(f"MCP endpoint: {url}\n")

    try:
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
          async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                # 1. Protocol handshake — proves the server is serving MCP,
                #    not merely that a port is open.
                init = await session.initialize()
                name = init.server_info.name
                print(f"  PASS  connected — server '{name}'")

                # 2. Tool listing — proves toolsets mounted.
                tools = (await session.list_tools()).tools
                print(f"  PASS  {len(tools)} tools exposed")

                # 3. A real call through the full pipeline. This is the one
                #    that matters: identity -> entitlement -> connector -> DB.
                if not any(t.name == "server_health" for t in tools):
                    print("  FAIL  server_health is not exposed — cannot verify the pipeline")
                    return 1

                result = await session.call_tool("server_health", {})
                if result.is_error:
                    print(f"  FAIL  server_health: {result.content[0].text}")
                    return 1

                payload = json.loads(result.content[0].text)
                instance = payload.get("instance")
                print(f"  PASS  server_health — pipeline reached the database")
                print(f"        environment : {payload.get('environment')}")
                print(f"        mapped role : {payload.get('mapped_role')}")
                if instance:
                    print(
                        f"        database    : {instance.get('instance_name')} "
                        f"({instance.get('status')}, {instance.get('database_status')})"
                    )
                    # A mock connector answers with a name that gives itself
                    # away. Worth saying out loud: the pipeline passing does
                    # NOT mean a real database was reached.
                    if str(instance.get("instance_name", "")).upper().startswith("MOCK"):
                        print(
                            "\n  NOTE  that is the MOCK connector, not a real database.\n"
                            "        The server is healthy but no EBS credentials are\n"
                            "        configured — set EBSMCP_EBS_INSTANCES (or EBS_DB_*)\n"
                            "        in the .env that docker compose reads."
                        )
                else:
                    print("        database    : query returned no rows")

    except BaseException as exc:  # noqa: BLE001 — report anything, never traceback
        # anyio nests real failures inside ExceptionGroups; printing the group
        # yields "unhandled errors in a TaskGroup", which says nothing. Walk
        # down to the actual leaves and report those.
        def leaves(e: BaseException) -> list[BaseException]:
            if isinstance(e, BaseExceptionGroup):
                return [leaf for sub in e.exceptions for leaf in leaves(sub)]
            return [e]

        for leaf in leaves(exc):
            text = str(leaf).strip().splitlines()[0] if str(leaf).strip() else ""
            print(f"  FAIL  {type(leaf).__name__}: {text}" if text else f"  FAIL  {type(leaf).__name__}")
        print()
        print("  Common causes:")
        print("    - server not running            docker compose ps mcp-server")
        print("    - wrong URL or path             the endpoint is /mcp, not /")
        print("    - EBSMCP_ALLOWED_HOSTS          defaults to [] and rejects EVERY")
        print("                                    request with 421 Invalid Host header.")
        print("                                    It matches the Host header verbatim, so")
        print("                                    the PORT must be included:")
        print("                                      [\"localhost:8080\"]  not  [\"localhost\"]")
        print("    - Entra auth on, no token       pass --token")
        return 1

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8080/mcp", help="MCP endpoint (default: %(default)s)")
    parser.add_argument("--token", help="Bearer token, once Entra auth is enabled.")
    parser.add_argument("--timeout", type=float, default=15.0, help="Seconds (default: %(default)s).")
    args = parser.parse_args()

    try:
        import mcp  # noqa: F401
    except ImportError:
        sys.exit("The mcp package is not installed. Run this inside the mcp-server image, or: pip install 'mcp[cli]'")

    rc = asyncio.run(run(args.url, args.token, args.timeout))
    print()
    print("all checks passed" if rc == 0 else "checks FAILED")
    sys.exit(rc)


if __name__ == "__main__":
    main()
