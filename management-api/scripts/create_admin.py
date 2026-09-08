#!/usr/bin/env python
"""Create (or reset the password of) a local admin_accounts row.

Deliberately a standalone script, not an API endpoint: creating the very
first admin account can't go through admin_subject's own authentication
(there's nothing to authenticate against yet), and even once accounts
exist, minting new ones isn't exposed over HTTP at all — see
identity-service's admin_accounts migration for why mgmt_identity_writer
only has SELECT on this table, not INSERT/UPDATE. Run this with a more
privileged connection instead (the same superuser/provisioning credential
used to apply migrations, not the app's own runtime role).

Usage:
    python scripts/create_admin.py --username admin@corp.com
    python scripts/create_admin.py --username admin@corp.com --reset-password

Reads the database URL from IDENTITY_DB_URL (or --db-url), and prompts for
the password interactively (never accepted as a CLI argument, so it never
ends up in shell history or process listings).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, insert, select, update  # noqa: E402

from app.auth.local import hash_password  # noqa: E402
from app.models.identity import admin_accounts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", required=True, help="e.g. admin@corp.com")
    parser.add_argument("--reset-password", action="store_true", help="Update an existing account instead of creating a new one.")
    parser.add_argument("--db-url", default=os.environ.get("IDENTITY_DB_URL"), help="Defaults to IDENTITY_DB_URL.")
    args = parser.parse_args()

    if not args.db_url:
        parser.error("--db-url or IDENTITY_DB_URL is required.")

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        parser.error("Passwords did not match.")

    engine = create_engine(args.db_url)
    with engine.begin() as conn:
        existing = conn.execute(
            select(admin_accounts.c.id).where(admin_accounts.c.username == args.username)
        ).scalar_one_or_none()

        if existing is not None and not args.reset_password:
            parser.error(f"Account {args.username!r} already exists — pass --reset-password to update it.")
        if existing is None and args.reset_password:
            parser.error(f"No existing account {args.username!r} to reset — omit --reset-password to create one.")

        if existing is None:
            conn.execute(
                insert(admin_accounts).values(
                    username=args.username,
                    password_hash=hash_password(password),
                    is_active=True,
                )
            )
            print(f"Created admin account {args.username!r}.")
        else:
            conn.execute(
                update(admin_accounts)
                .where(admin_accounts.c.id == existing)
                .values(password_hash=hash_password(password))
            )
            print(f"Reset password for {args.username!r}.")


if __name__ == "__main__":
    main()
