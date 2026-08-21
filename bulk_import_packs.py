"""
Pushes every bid-pack .txt file in data/PBS/ into a running MobileCCI
server's Pairing Library (POST /pbs/packs/import), one call per file —
no redeploy needed, since the packs live in the database, not in the
deployed code.

Filenames encode opr/base/fleet themselves — e.g.
"NACLGAB756AUG-26-PBS.txt" -> opr=NAC, base=LGA, fleet=B756 — validated
against every file currently in data/PBS/ with zero exceptions before
this script was written; see FILENAME_RE below.

Usage:
    .venv/bin/python3 bulk_import_packs.py --url https://mobilefos-production.up.railway.app --username pairingtest
    .venv/bin/python3 bulk_import_packs.py --url http://localhost:5050 --username pairingtest --dir data/PBS

Prompts for the password interactively (getpass — never pass it on the
command line or in an env var, both leak into shell history/process
listings).
"""
import argparse
import getpass
import re
import sys
from pathlib import Path

import requests

FILENAME_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})([A-Z0-9]+)([A-Z]{3})-(\d{2})-PBS\.txt$")


def parse_filename(name):
    m = FILENAME_RE.match(name)
    if not m:
        return None
    opr, base, fleet, _month, _yy = m.groups()
    return opr, base, fleet


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Base URL of the running server, e.g. https://mobilefos-production.up.railway.app")
    ap.add_argument("--username", required=True)
    ap.add_argument("--dir", default="data/PBS", help="Directory of *-PBS.txt files (default: data/PBS)")
    args = ap.parse_args()

    base_url = args.url.rstrip("/")
    pbs_dir = Path(args.dir)
    files = sorted(pbs_dir.glob("*-PBS.txt"))
    if not files:
        print(f"No *-PBS.txt files found in {pbs_dir}", file=sys.stderr)
        sys.exit(1)

    skipped = [f for f in files if not parse_filename(f.name)]
    files = [f for f in files if parse_filename(f.name)]
    for f in skipped:
        print(f"SKIP  {f.name} — doesn't match the expected OPRBASEFLEETMON-YY-PBS.txt pattern")

    password = getpass.getpass(f"Password for {args.username} on {base_url}: ")

    session = requests.Session()
    login_resp = session.post(
        f"{base_url}/login",
        data={"username": args.username, "password": password},
        allow_redirects=False,
    )
    if login_resp.status_code not in (302, 303) or "/login" in login_resp.headers.get("Location", ""):
        print("Login failed — check username/password.", file=sys.stderr)
        sys.exit(1)
    print(f"Logged in as {args.username}.\n")

    ok, failed = 0, 0
    for f in files:
        opr, base, fleet = parse_filename(f.name)
        text = f.read_text()
        r = session.post(
            f"{base_url}/pbs/packs/import",
            json={"opr": opr, "base": base, "fleet": fleet, "text": text},
        )
        if r.ok:
            data = r.json()
            print(f"OK    {f.name:40s} {opr}/{base}/{fleet:6s} — {data['sequences_parsed']:4d} sequences, {data['legs_parsed']:5d} legs")
            ok += 1
        else:
            print(f"FAIL  {f.name:40s} {opr}/{base}/{fleet:6s} — {r.status_code} {r.text[:200]}")
            failed += 1

    print(f"\n{ok} pack(s) imported, {failed} failed, {len(skipped)} skipped.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
