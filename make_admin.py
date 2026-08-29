"""
Grants (or revokes) document-publishing rights on an account.

There's deliberately no in-app UI for this — admin is a standing property
of an account, not something to toggle from a phone mid-trip.

TO CREATE THE FIRST ADMIN ON A DEPLOYED SERVER, USE THE ENV VAR INSTEAD:
set ADMIN_USERNAMES=<username> in the Railway dashboard and redeploy. That
route can't target the wrong database, which this script easily can — see
below. This script is for local development, and for revoking.

This script talks to whatever DATABASE_URL points at, exactly like the
server does. With no DATABASE_URL set that's the LOCAL SQLite file, so
running it on a laptop to "fix production" silently grants admin to a
local account and changes nothing on the server — the failure this
script now refuses to let you walk into (pass --local to confirm you
really do mean the local database).

Usage:
    .venv/bin/python3 make_admin.py --list
    .venv/bin/python3 make_admin.py --username pairingtest --local
    .venv/bin/python3 make_admin.py --username someone --revoke --local

Against production, with the Railway CLI injecting the real DATABASE_URL:
    railway run .venv/bin/python3 make_admin.py --username jetcentric
"""
import argparse
import os
import sys

import server
from models import db, User


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="Account to grant/revoke admin on")
    ap.add_argument("--revoke", action="store_true", help="Revoke instead of grant")
    ap.add_argument("--list", action="store_true", help="List accounts and their admin flag")
    ap.add_argument("--local", action="store_true",
                    help="Confirm you mean the local database (required when DATABASE_URL is unset)")
    args = ap.parse_args()

    if not args.username and not args.list:
        ap.error("pass --username, or --list to see accounts")

    # Refuse to act on the local SQLite file unless that was asked for
    # explicitly. Without this the script's most likely misuse — running
    # it on a laptop expecting to change production — succeeds loudly and
    # accomplishes nothing, which is far worse than a refusal.
    if not os.environ.get("DATABASE_URL") and not args.local:
        print("No DATABASE_URL set, so this would act on the LOCAL database, not production.", file=sys.stderr)
        print(file=sys.stderr)
        print("  To make an admin on the deployed server, set ADMIN_USERNAMES in the", file=sys.stderr)
        print("  Railway dashboard and redeploy (see this file's docstring).", file=sys.stderr)
        print("  To act on the local database anyway, re-run with --local.", file=sys.stderr)
        sys.exit(1)

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
