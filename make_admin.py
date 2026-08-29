"""
Grants (or revokes) document-publishing rights on an account.

There's deliberately no in-app UI for this — admin is a standing property
of an account, not something to toggle from a phone mid-trip, and the
first admin has to be created out-of-band anyway (nothing in the app can
grant it before one exists). Run this against the same database the server
uses: locally that's the default SQLite file, on Railway set DATABASE_URL
to the Postgres URL first.

Usage:
    .venv/bin/python3 make_admin.py --list
    .venv/bin/python3 make_admin.py --username jetcentric
    .venv/bin/python3 make_admin.py --username someone --revoke

On Railway (from the project shell, where DATABASE_URL is already set):
    python3 make_admin.py --username jetcentric
"""
import argparse
import sys

import server
from models import db, User


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="Account to grant/revoke admin on")
    ap.add_argument("--revoke", action="store_true", help="Revoke instead of grant")
    ap.add_argument("--list", action="store_true", help="List accounts and their admin flag")
    args = ap.parse_args()

    if not args.username and not args.list:
        ap.error("pass --username, or --list to see accounts")

    with server.app.app_context():
        if args.list:
            users = User.query.order_by(User.username).all()
            if not users:
                print("No accounts yet.")
                return
            for u in users:
                print(f"  {'ADMIN' if u.is_admin else '     '}  {u.username}")
            return

        user = User.query.filter_by(username=args.username).first()
        if not user:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            print("Run with --list to see the accounts on this database.", file=sys.stderr)
            sys.exit(1)
        user.is_admin = not args.revoke
        db.session.commit()
        verb = "revoked on" if args.revoke else "granted to"
        print(f"Admin {verb} {user.username}.")


if __name__ == "__main__":
    main()
