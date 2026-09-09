#!/usr/bin/env python
"""Deactivate, reactivate, delete, or list local admin_accounts rows.

The companion to create_admin.py, and a standalone script for the same
reason: managing admin accounts is deliberately not exposed over HTTP —
mgmt_identity_writer holds only SELECT on this table (see identity-service's
admin_accounts migration), so run this with the same privileged
superuser/provisioning credential used for migrations, not the app's own
runtime role.

DEACTIVATE IS THE DEFAULT, AND USUALLY THE RIGHT CHOICE. Setting
is_active=false blocks sign-in immediately (auth.py rejects an inactive
row) while keeping the row, so the created_by / updated_by references that
every identity mapping this admin ever touched still resolve to a real
account. A hard --delete drops the row outright and breaks that history —
use it only for a mistaken account that never did anything worth auditing.

Usage:
    python scripts/remove_admin.py --list
    python scripts/remove_admin.py --username admin@corp.com               # deactivate
    python scripts/remove_admin.py --username admin@corp.com --reactivate
    python scripts/remove_admin.py --username admin@corp.com --delete      # prompts to confirm

Reads the database URL from IDENTITY_DB_URL (or --db-url).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, delete, select, update  # noqa: E402

from app.models.identity import admin_accounts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", help="The account to act on. Omit only with --list.")
    parser.add_argument("--list", action="store_true", help="List all accounts (username, active, created) and exit. Never prints hashes.")
    parser.add_argument("--reactivate", action="store_true", help="Re-enable a deactivated account (set is_active=true).")
    parser.add_argument("--delete", action="store_true", help="HARD delete the row. Breaks created_by/updated_by history — prefer the default deactivate.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt for --delete (for non-interactive use).")
    parser.add_argument("--by", default=None, help="Record this admin as updated_by on a deactivate/reactivate. Optional.")
    parser.add_argument("--db-url", default=os.environ.get("IDENTITY_DB_URL"), help="Defaults to IDENTITY_DB_URL.")
    args = parser.parse_args()

    if not args.db_url:
        parser.error("--db-url or IDENTITY_DB_URL is required.")
    if args.reactivate and args.delete:
        parser.error("--reactivate and --delete are mutually exclusive.")
    if not args.list and not args.username:
        parser.error("--username is required (or use --list).")

    engine = create_engine(args.db_url)
    with engine.begin() as conn:
        if args.list:
            rows = conn.execute(
                select(
                    admin_accounts.c.username,
                    admin_accounts.c.is_active,
                    admin_accounts.c.created_at,
                ).order_by(admin_accounts.c.id)
            ).all()
            if not rows:
                print("No admin accounts. Create one with scripts/create_admin.py.")
                return
            print(f"{'USERNAME':<40} {'ACTIVE':<7} CREATED")
            for r in rows:
                created = r.created_at.date().isoformat() if r.created_at else "-"
                print(f"{r.username:<40} {str(r.is_active):<7} {created}")
            return

        row = conn.execute(
            select(admin_accounts.c.id, admin_accounts.c.is_active).where(
                admin_accounts.c.username == args.username
            )
        ).first()
        if row is None:
            parser.error(f"No admin account {args.username!r}. Use --list to see what exists.")

        # Refuse to leave zero usable admins behind — locking yourself out of
        # the console entirely is almost never intended, and recovering means
        # dropping back to create_admin.py with a privileged credential anyway.
        if (args.delete or (not args.reactivate)) and row.is_active:
            active_count = conn.execute(
                select(admin_accounts.c.id).where(admin_accounts.c.is_active.is_(True))
            ).all()
            if len(active_count) <= 1:
                parser.error(
                    f"{args.username!r} is the only active admin account. Removing it would "
                    "lock everyone out of the console. Create another active admin first "
                    "(scripts/create_admin.py), then remove this one."
                )

        if args.delete:
            if not args.yes:
                confirm = input(
                    f"HARD DELETE admin {args.username!r}? This breaks created_by/updated_by "
                    "history. Type the username to confirm: "
                )
                if confirm != args.username:
                    parser.error("Confirmation did not match — nothing deleted.")
            conn.execute(delete(admin_accounts).where(admin_accounts.c.id == row.id))
            print(f"Deleted admin account {args.username!r}.")
            return

        new_active = bool(args.reactivate)
        if row.is_active == new_active:
            print(f"Admin account {args.username!r} is already {'active' if new_active else 'inactive'} — no change.")
            return
        conn.execute(
            update(admin_accounts)
            .where(admin_accounts.c.id == row.id)
            .values(is_active=new_active, updated_at=datetime.now(UTC), updated_by=args.by)
        )
        print(f"{'Reactivated' if new_active else 'Deactivated'} admin account {args.username!r}.")


if __name__ == "__main__":
    main()
