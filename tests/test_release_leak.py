"""Standalone proof for the OFP/docs leakage bug.

Two legs, a release generated for ONE of them, and the assertion that the
other one returns nothing. Run it directly (no pytest, no new dependency):

    python3 tests/test_release_leak.py

The leak it pins down: release_engine.generate_release_pdfs(user_id) renders
whatever OFP is sitting on the pilot's SimBrief ACCOUNT right now — it has no
idea which leg the request is for. /fos/<leg_id>/release used to hand that
straight back and cache it against <leg_id>, so opening Documents on a leg
nothing had ever been generated for silently minted (and then remembered)
another leg's release.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fos-test-"), "test.db")
os.environ.setdefault("SECRET_KEY", "test-only")

import release_engine  # noqa: E402
import server  # noqa: E402
import simbrief_ofp  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# The SimBrief account holds exactly one OFP at a time — here, leg A's.
ACCOUNT_OFP = {"flight_number": "101", "origin": "KPHX", "destination": "KLAX"}


# **kwargs deliberately: generate_release_pdfs has gained keyword arguments
# (gate, arr_gate, generation) as the release engine grew, and a stub pinned to
# today's exact signature turns the next one into a spurious 500 here.
def fake_generate_release_pdfs(user_id, **kwargs):
    return (b"%PDF-1.4 leg-A-release", b"%PDF-1.4 leg-A-wb", "AA101-KPHX-KLAX-RLS.pdf")


release_engine.is_available = lambda: True
release_engine.import_error = lambda: None
release_engine.generate_release_pdfs = fake_generate_release_pdfs
release_engine.extract_named_pages = lambda b: {}
simbrief_ofp.fetch_ofp_leg_fields = lambda user, timeout=15: dict(ACCOUNT_OFP)
server._ofp_fetch_cache.clear()

client = server.app.test_client()
client.post("/register", data={"username": "tester", "password": "password123"})

with server.app.app_context():
    user = server.User.query.filter_by(username="tester").first()
    leg_a = server.Leg(user_id=user.id, flight_number="101", dep_date="08/31/26",
                       data={**server.DEFAULT_LEG, "flight_number": "101", "origin": "KPHX",
                             "destination": "KLAX", "dep_date": "08/31/26", "fit_for_duty": True})
    leg_b = server.Leg(user_id=user.id, flight_number="202", dep_date="09/01/26",
                       data={**server.DEFAULT_LEG, "flight_number": "202", "origin": "KLAX",
                             "destination": "KSEA", "dep_date": "09/01/26", "fit_for_duty": True})
    server.db.session.add_all([leg_a, leg_b])
    server.db.session.commit()
    a_id, b_id = leg_a.id, leg_b.id

print(f"leg A = {a_id} (101 KPHX-KLAX, the flight on SimBrief)")
print(f"leg B = {b_id} (202 KLAX-KSEA, nothing ever generated for it)\n")

# 1. Generate a release for leg A. The account's OFP *is* leg A, so this is
#    the legitimate case and has to keep working.
r = client.post(f"/fos/{a_id}/release", json={"user_id": "tester"})
check("leg A generates its release", r.status_code == 200 and r.get_json().get("rls_pdf_b64"),
      f"HTTP {r.status_code} {r.get_json()}")

# 2. Leg B has had nothing generated. Asking for its release must not hand
#    back leg A's — this is the actual bug.
r = client.post(f"/fos/{b_id}/release", json={"user_id": "tester"})
body = r.get_json()
check("leg B returns no release", r.status_code != 200 and not body.get("rls_pdf_b64"),
      f"HTTP {r.status_code} {body}")

# 3. ...and nothing may have been cached against leg B as a side effect.
with server.app.app_context():
    cached_b = server.ReleaseCache.query.filter_by(leg_id=b_id).first()
    cached_a = server.ReleaseCache.query.filter_by(leg_id=a_id).first()
check("nothing cached against leg B", cached_b is None,
      "" if cached_b is None else f"cached {cached_b.filename!r}")
check("leg A's release is still cached", cached_a is not None and cached_a.filename == "AA101-KPHX-KLAX-RLS.pdf")

# 4. The read-only path the Documents view uses must report "nothing here"
#    for leg B rather than minting one.
r = client.post(f"/fos/{b_id}/release", json={"user_id": "tester", "cached_only": True})
body = r.get_json()
check("Documents view (cached_only) finds nothing on leg B", r.status_code == 404 and not body.get("rls_pdf_b64"),
      f"HTTP {r.status_code} {body}")

# 5. ...and still serves leg A's own cached release.
r = client.post(f"/fos/{a_id}/release", json={"user_id": "tester", "cached_only": True})
body = r.get_json()
check("Documents view (cached_only) serves leg A's own release",
      r.status_code == 200 and body.get("filename") == "AA101-KPHX-KLAX-RLS.pdf",
      f"HTTP {r.status_code}")

# 6. Once the pilot redispatches leg B on SimBrief, its own release generates
#    normally — the guard is an identity check, not a lockout.
ACCOUNT_OFP.update({"flight_number": "202", "origin": "KLAX", "destination": "KSEA"})
server._ofp_fetch_cache.clear()
release_engine.generate_release_pdfs = lambda user_id, **kwargs: (
    b"%PDF-1.4 leg-B-release", None, "AA202-KLAX-KSEA-RLS.pdf")
r = client.post(f"/fos/{b_id}/release", json={"user_id": "tester"})
body = r.get_json()
check("leg B generates once SimBrief actually holds it",
      r.status_code == 200 and body.get("filename") == "AA202-KLAX-KSEA-RLS.pdf",
      f"HTTP {r.status_code} {body}")

# 7. And leg A's cached release is untouched by leg B's generation.
r = client.post(f"/fos/{a_id}/release", json={"user_id": "tester", "cached_only": True})
check("leg A still serves its own release afterwards",
      r.get_json().get("filename") == "AA101-KPHX-KLAX-RLS.pdf", str(r.get_json().get("filename")))

# 8. Every call the page makes to the release URL uses a method that route
#    actually serves. A read path that GETs a POST-only route falls through
#    to a 404 HTML page, and the JSON parse then blows up with "Unexpected
#    token '<'" — which is exactly what happened when the route's read/
#    generate split (the whole fix above) was reshaped without its callers.
#    That has now happened twice, so it is worth a check rather than care.
#
#    Deliberately NOT a line-bounded regex over the call: these fetches are
#    written both inline and across several lines, and matching to end-of-line
#    silently saw only the inline one — passing while two multi-line callers,
#    including the one that actually broke, went unexamined. Locate every
#    occurrence of the URL instead, then read the method out of the call that
#    follows it.
RELEASE_URL = "'/fos/' + LEG_ID + '/release'"
methods = set()
for rule in server.app.url_map.iter_rules():
    if rule.rule == "/fos/<int:leg_id>/release":
        methods |= (rule.methods - {"HEAD", "OPTIONS"})

def _enclosing_call(tpl, url_start):
    """Just the fetch(...) call the URL at url_start belongs to, paren-matched
    from its own opening bracket. A fixed-size window instead of this reads
    past the end of the call: with two fetches side by side in a ternary, the
    GET branch picked up the POST branch's own method option and the check
    passed on a call that was plainly wrong."""
    open_paren = tpl.rindex("(", 0, url_start)
    depth = 0
    for i in range(open_paren, len(tpl)):
        if tpl[i] == "(":
            depth += 1
        elif tpl[i] == ")":
            depth -= 1
            if depth == 0:
                return tpl[open_paren:i + 1]
    return tpl[open_paren:]


called, uncovered = set(), 0
for name in ("LAUNCHER_TEMPLATE", "FOS_TEMPLATE"):
    tpl = getattr(server, name)
    for m in re.finditer(re.escape(RELEASE_URL), tpl):
        # Only fetch() calls carry an HTTP verb — anything else referencing the
        # URL is counted as uncovered rather than quietly assumed harmless.
        if not re.search(r"fetch\(\s*$", tpl[max(0, m.start() - 40):m.start()]):
            uncovered += 1
            continue
        verb = re.search(r"method\s*:\s*['\"](\w+)['\"]", _enclosing_call(tpl, m.start()))
        # No method option means fetch()'s own default, which is GET.
        called.add(verb.group(1).upper() if verb else "GET")

check("every /release fetch in the page uses a method the route serves",
      bool(called) and not uncovered and called <= methods,
      f"page calls {sorted(called)}, route serves {sorted(methods)}"
      + (f", {uncovered} call site(s) not recognised" if uncovered else ""))

print("\n" + ("FAILED: " + ", ".join(FAILURES) if FAILURES else "All checks passed."))
sys.exit(1 if FAILURES else 0)
