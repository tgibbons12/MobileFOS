"""
Publishes every PDF in data/DOCS/ to a running MobileCCI server's Docs tab
(POST /docs/import), one call per file — no redeploy needed, since the
documents live in the database, not in the deployed code.

Deliberately parallel to bulk_import_packs.py (same --url/--username/--dir
flags, same interactive getpass, same per-file OK/FAIL report), so there's
one upload idiom to remember rather than two.

Two things differ from pack import, both because a document forces work on
every other pilot:
  * The account must be an admin (User.is_admin) — the server rejects
    anyone else with 403.
  * Re-uploading a file whose name matches an existing document bumps that
    document's revision and CLEARS every pilot's acknowledgement, so the
    new revision has to be acknowledged again. The script says so per file
    rather than letting that happen silently.

Title defaults to the filename without its .pdf extension; put a category
in the filename after a double underscore to group it in the Docs list,
e.g. "Ops Manual__Manuals.pdf" -> title "Ops Manual", category "Manuals".

Usage:
    .venv/bin/python3 upload_docs.py --url https://mobilefos-production.up.railway.app --username jetcentric
    .venv/bin/python3 upload_docs.py --url http://localhost:5050 --username pairingtest --dir data/DOCS

Prompts for the password interactively (getpass — never pass it on the
command line or in an env var, both leak into shell history/process
listings).
"""
import argparse
import base64
import getpass
import sys
from pathlib import Path

import requests


def parse_filename(name):
    """('Ops Manual', 'Manuals') from "Ops Manual__Manuals.pdf"; category
    is None when the double-underscore marker isn't present."""
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    if "__" in stem:
        title, _, category = stem.partition("__")
        return title.strip(), (category.strip() or None)
    return stem.strip(), None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Base URL of the running server, e.g. https://mobilefos-production.up.railway.app")
    ap.add_argument("--username", required=True, help="An admin account (User.is_admin)")
    ap.add_argument("--dir", default="data/DOCS", help="Directory of *.pdf files (default: data/DOCS)")
    args = ap.parse_args()

    base_url = args.url.rstrip("/")
    docs_dir = Path(args.dir)
    files = sorted(p for p in docs_dir.glob("*.pdf") if p.is_file())
    if not files:
        print(f"No *.pdf files found in {docs_dir}", file=sys.stderr)
        sys.exit(1)

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
        title, category = parse_filename(f.name)
        payload = {
            "filename": f.name,
            "title": title,
            "category": category or "",
            "pdf_b64": base64.b64encode(f.read_bytes()).decode("ascii"),
        }
        r = session.post(f"{base_url}/docs/import", json=payload)
        if r.ok:
            data = r.json()
            size_mb = data.get("size_bytes", 0) / (1024 * 1024)
            note = "  (revised — all acknowledgements cleared)" if data.get("acks_cleared") else ""
            print(f"OK    {f.name:44s} rev {data['revision']:<3d} {size_mb:5.1f} MB{note}")
            ok += 1
        else:
            # 403 is the common real-world failure here (non-admin account),
            # so surface the server's own message rather than just a code.
            detail = ""
            try:
                detail = r.json().get("error", "")
            except ValueError:
                detail = r.text[:200]
            print(f"FAIL  {f.name:44s} {r.status_code} {detail}")
            failed += 1

    print(f"\n{ok} document(s) published, {failed} failed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
