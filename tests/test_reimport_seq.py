"""Standalone proof that a re-import from SimBrief keeps a leg attached to
its pairing. Run it directly (no pytest, no new dependency):

    python3 tests/test_reimport_seq.py

The workflow: generate a leg from a sequence inside the app, edit it on
SimBrief, then re-import it (Home > Import from SimBrief, or the Confirm
view's sync). The re-import rebuilds the leg dict from the OFP alone, and a
SimBrief OFP has no `seq` field at all — so whenever the edit moved the leg
far enough that _find() no longer recognises it as the same flight, the
re-import landed on a brand-new row with no pairing linkage and the leg
showed up as standalone.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fos-test-"), "test.db")
os.environ.setdefault("SECRET_KEY", "test-only")

import server  # noqa: E402
import simbrief_ofp  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# One pairing, one leg of it — the shape pbs_leg_to_fos_leg emits: a real
# seq/position/base/hotel, and no calendar date at all (a bid pack is a
# schedule pattern, not a specific day).
PAIRED_LEG = {
    **server.DEFAULT_LEG,
    "seq": "4412", "position": "CA", "base": "PHX",
    "flight_number": "1180", "origin": "PHX", "destination": "LAX",
    "dep_date": "", "pairing_sched_out": "08:15", "pairing_sched_in": "09:35",
    "hotel_details": "HILTON LAX 310-555-0100", "duty_time": "9.15",
}

# What SimBrief hands back after the pilot edits the leg on its dispatch
# page: ICAO codes, a real date, real day-of-ops detail — and no seq.
EDITED_OFP = {
    "flight_number": "1180", "origin": "KPHX", "destination": "KLAX",
    "dep_date": "09/04/26", "sched_out": "08:40", "sched_in": "10:05",
    "tail_number": "N801NA", "customer_load": "168",
}

simbrief_ofp.fetch_ofp_leg_fields = lambda user, timeout=15: dict(EDITED_OFP)

client = server.app.test_client()
client.post("/register", data={"username": "tester", "password": "password123"})

# The pilot's active PBS import — the pairing this leg came out of. This is
# where the leg's `seq` came from in the first place (pbs_leg_to_fos_leg), and
# so it is where a re-import looks to put it back.
SEQUENCE = {
    "seq": "4412", "positions": ["CA", "FO"], "ops_per_period": "1", "block": "9.15",
    "duty_days": [{
        "duty_day": 1, "report": "0715", "release": "1935", "duty": "9.15", "hotel": "HILTON LAX",
        "legs": [
            {"flight_number": "1180", "origin": "PHX", "destination": "LAX",
             "equipment": "321", "dep_local": "0815", "arr_local": "0935", "block": "1.20", "ground": "1.05"},
            {"flight_number": "1181", "origin": "LAX", "destination": "PHX",
             "equipment": "321", "dep_local": "1040", "arr_local": "1315", "block": "1.35", "ground": ""},
        ],
    }],
}

with server.app.app_context():
    user = server.User.query.filter_by(username="tester").first()
    row = server.Leg(user_id=user.id, flight_number="1180", dep_date="", data=dict(PAIRED_LEG))
    server.db.session.add(row)
    server.db.session.add(server.PbsImport(
        user_id=user.id, meta={"operator": "NA", "base": "PHX", "fleet": "321"},
        sequences=[SEQUENCE],
    ))
    user.active_seq = "4412"
    server.db.session.commit()
    paired_id = row.id

print(f"leg {paired_id}: flight 1180 PHX-LAX, generated from sequence 4412\n")


def reimport(label, expect_same_row=True):
    """Home > Import from SimBrief: POST /generate with nothing but the
    username, exactly as loadFromSimbrief() does."""
    server._ofp_fetch_cache.clear()
    r = client.post("/generate", json={"simbrief_user": "tester"})
    body = r.get_json()
    check(f"{label}: re-import succeeds", r.status_code == 200, f"HTTP {r.status_code} {body}")
    if r.status_code != 200:
        return None
    with server.app.app_context():
        leg = server.Leg.query.get(body["id"])
        data = dict(leg.data)
    if expect_same_row:
        check(f"{label}: lands on the same leg row", body["id"] == paired_id,
              f"got leg {body['id']}, expected {paired_id}")
    check(f"{label}: seq survives", data.get("seq") == "4412", f"seq={data.get('seq')!r}")
    check(f"{label}: position survives", data.get("position") == "CA", f"position={data.get('position')!r}")
    check(f"{label}: base survives", data.get("base") == "PHX", f"base={data.get('base')!r}")
    check(f"{label}: hotel survives", bool(data.get("hotel_details")), f"hotel={data.get('hotel_details')!r}")
    check(f"{label}: pairing's published times survive",
          data.get("pairing_sched_out") == "08:15", f"pairing_sched_out={data.get('pairing_sched_out')!r}")
    check(f"{label}: SimBrief's own data landed", data.get("tail_number") == "N801NA",
          f"tail_number={data.get('tail_number')!r}")
    return data


# 1. The straightforward re-import: the edit only added day-of-ops detail, so
#    _find() still recognises the flight and merges onto the same row. This
#    already worked — it's here so a change to the merge can't quietly break it.
reimport("edit adds day-of-ops detail")

# 2. The one that actually detached: the pilot also moved the flight on
#    SimBrief, far enough that _find()'s tolerant date match no longer sees it
#    as the same flight. That is a genuinely new row — but it is still a leg of
#    sequence 4412, and it must say so.
with server.app.app_context():
    leg = server.Leg.query.get(paired_id)
    leg.dep_date = "09/04/26"
    leg.data = {**leg.data, "dep_date": "09/04/26"}
    server.db.session.commit()
EDITED_OFP["dep_date"] = "09/09/26"  # five days out — well past _dates_match's tolerance
reimport("edit slips the flight to a new date", expect_same_row=False)

# 3. A flight that is genuinely NOT part of any of this pilot's sequences must
#    stay standalone — re-attachment is a lookup in the pilot's own pairings,
#    never a guess.
EDITED_OFP.update({"flight_number": "9999", "origin": "KDEN", "destination": "KORD",
                   "dep_date": "09/10/26"})
server._ofp_fetch_cache.clear()
r = client.post("/generate", json={"simbrief_user": "tester"})
with server.app.app_context():
    stray = server.Leg.query.get(r.get_json()["id"])
    stray_seq = stray.data.get("seq")
check("an unrelated flight stays standalone", not stray_seq, f"seq={stray_seq!r}")

print("\n" + ("FAILED: " + ", ".join(FAILURES) if FAILURES else "All checks passed."))
sys.exit(1 if FAILURES else 0)
