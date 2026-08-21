"""
Edit one leg within an already-stored PBS sequence (pbs_parser's native
duty_days[].legs[] shape) — e.g. swap its destination — with times/duty/
rest/legality recomputed. Not a port: nac_pairings.py's walk()/verify()
operate on index chains into the master route-network leg list, but a
stored sequence's legs are plain dicts with no such index, so this module
reimplements the same duty/rest/FDP math (reusing Rules/table_a/table_b/
mct_after_arrival/max_sit_at from pairing_engine) directly against the
stored shape.

Legs are contiguous — a leg's origin must equal the previous leg's
destination (see server.py's _sequence_routing) — so changing a leg's
destination invalidates whatever came after it. Decided behavior (see the
project plan): truncate the pairing at the edited leg rather than trying to
reconnect or reject. That also means this module never needs to ripple a
change through legs *after* the edit — there's nothing left afterward.

To get the exact PBS text-format field encodings right (the bid HH.MM
duration strings, the D/A calendar-day column, etc.) without re-deriving
pbs_format's/pbs_parser's column conventions by hand, the edited day is
rebuilt the same way pbs_build.to_pbs() would for a fresh pairing, rendered
through pbs_format.sequence_lines(), and re-parsed with the repo's own
pbs_parser.parse_pbs() — then only that one reparsed day dict is spliced
back into the (otherwise untouched) sequence. This is the same
build-then-reparse round trip /pairings/accept uses for a whole generated
sequence, just scoped to one day.
"""
from __future__ import annotations

import copy

import pbs_build
import pbs_format
import pbs_parser
from pairing_engine import Rules, mct_after_arrival, max_sit_at, table_a, table_b


def _hhmm_to_dec(s):
    s = (s or "").strip()
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"bad HHMM time: {s!r}")
    return int(s[:2]) + int(s[2:]) / 60


def find_network_leg(legs, origin, destination, flight_number=None):
    """Best match for a proposed destination swap: exact flight_number match
    if given, else the earliest-departing (origin, destination) match."""
    origin = (origin or "").strip().upper()
    destination = (destination or "").strip().upper()
    candidates = [l for l in legs if l["o"] == origin and l["d"] == destination]
    if flight_number:
        fn = str(flight_number).strip()
        exact = [l for l in candidates if l["f"] == fn]
        if exact:
            candidates = exact
    if not candidates:
        return None
    return min(candidates, key=lambda l: l["dep"])


def apply_leg_edit(seq, dom, ap, legs, duty_day, leg_index,
                    new_destination=None, flight_number=None, manual=None):
    """Returns (new_seq, violations, meta).
    new_seq is None (with violations explaining why) if the edit couldn't be
    resolved at all (bad indices, no route found, missing input) — that's a
    hard 400, not something to stage for confirm/reject. A non-empty
    violations list on a non-None new_seq means the edit resolved but broke
    a legality rule — the caller stages it and lets the user confirm/reject.
    """
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return None, ["duty_day not found"], {}
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day):
        return None, ["leg_index out of range"], {}

    new_destination = (new_destination or "").strip().upper()
    if not new_destination:
        return None, ["new_destination is required"], {}

    kept_legs = legs_this_day[:leg_index]
    edited_leg = legs_this_day[leg_index]
    origin = kept_legs[-1]["destination"] if kept_legs else edited_leg["origin"]

    violations = []

    if manual:
        try:
            dep_local = _hhmm_to_dec(manual.get("dep_local"))
            arr_local = _hhmm_to_dec(manual.get("arr_local"))
            blk = float(manual.get("block"))
        except (TypeError, ValueError) as e:
            return None, [f"invalid manual leg data: {e}"], {}
        dep_utc = dep_local - ap.off(origin)
        arr_utc = dep_utc + blk
        fn = flight_number or edited_leg.get("flight_number", "")
        equip = edited_leg.get("equipment", "")
    else:
        net_leg = find_network_leg(legs, origin, new_destination, flight_number)
        if not net_leg:
            detail = f"{origin}-{new_destination}"
            if flight_number:
                detail += f" flight {flight_number}"
            return None, [f"no network route found for {detail} — supply manual times instead"], {}
        dep_utc, arr_utc, blk = net_leg["dep"], net_leg["arr"], net_leg["blk"]
        fn = net_leg["f"]
        equip = net_leg.get("fleet", "")[:2] or edited_leg.get("equipment", "")

    # ---- chain the new leg onto whatever precedes it ----
    if kept_legs:
        prev = kept_legs[-1]
        prev_arr_utc = _hhmm_to_dec(prev["arr_local"]) - ap.off(prev["destination"])
        m = mct_after_arrival(ap, prev["origin"], origin)
        d = dep_utc
        while d < prev_arr_utc + m:
            d += 24
        if d - prev_arr_utc > max_sit_at(ap, origin):
            violations.append(
                f"connection at {origin} is {d - prev_arr_utc:.2f}h — exceeds the "
                f"{max_sit_at(ap, origin):.2f}h max sit for a same-day continuation"
            )
        shift = d - dep_utc
        dep_utc += shift
        arr_utc += shift
        first_leg_dep_local = kept_legs[0]["dep_local"]
        first_leg_origin = kept_legs[0]["origin"]
        rpt_utc = _hhmm_to_dec(first_leg_dep_local) - ap.off(first_leg_origin) - Rules.BRIEF
    else:
        rpt_utc = dep_utc - Rules.BRIEF
        if day_idx > 0:
            prev_day = days[day_idx - 1]
            prev_legs = prev_day.get("legs") or []
            if prev_legs:
                prev_last = prev_legs[-1]
                prev_release_utc = (
                    _hhmm_to_dec(prev_last["arr_local"]) - ap.off(prev_last["destination"]) + Rules.DEBRIEF
                )
                d = dep_utc
                while (d - Rules.BRIEF) - prev_release_utc < Rules.MIN_REST:
                    d += 24
                rest = (d - Rules.BRIEF) - prev_release_utc
                if rest > Rules.MAX_REST:
                    violations.append(f"rest before this day is {rest:.2f}h — exceeds the {Rules.MAX_REST}h max")
                shift = d - dep_utc
                dep_utc += shift
                arr_utc += shift
                rpt_utc = dep_utc - Rules.BRIEF

    # ---- assemble this day's leg sequence in engine-native (UTC decimal) form ----
    ss = []
    for l in kept_legs:
        ss.append(dict(
            leg=dict(f=l["flight_number"], o=l["origin"], d=l["destination"],
                      blk=_bid_or_hhmm_span_to_dec(l), fleet=l.get("equipment", "")),
            dep=_hhmm_to_dec(l["dep_local"]) - ap.off(l["origin"]),
            arr=_hhmm_to_dec(l["arr_local"]) - ap.off(l["destination"]),
        ))
    ss.append(dict(leg=dict(f=fn, o=origin, d=new_destination, blk=blk, fleet=equip),
                    dep=dep_utc, arr=arr_utc))

    rls_utc = ss[-1]["arr"] + Rules.DEBRIEF
    block = sum(s["leg"]["blk"] for s in ss)
    fdp = rls_utc - rpt_utc
    rep_hbt = rpt_utc + ap.off(dom)
    if block > min(Rules.MAX_DUTY_BLOCK, table_a(rep_hbt)) + 1e-6:
        violations.append(f"day block {block:.2f}h exceeds the Table A limit ({table_a(rep_hbt):.2f}h)")
    if fdp > table_b(rep_hbt, len(ss)) + 1e-6:
        violations.append(f"FDP {fdp:.2f}h exceeds the Table B limit ({table_b(rep_hbt, len(ss)):.2f}h)")
    if len(ss) > Rules.MAX_LEGS_DAY:
        violations.append(f"{len(ss)} legs exceeds the {Rules.MAX_LEGS_DAY}/day max")

    # ---- render this one day the same way to_pbs() would, then reparse it
    # with the app's own pbs_parser so every field lands in exactly the
    # format a real import/generate would have produced ----
    L = lambda utc, stn: utc + ap.off(stn)
    rows = []
    for k, s in enumerate(ss):
        g = s["leg"]
        grnd = ss[k + 1]["dep"] - s["arr"] if k + 1 < len(ss) else None
        rows.append(dict(
            dp=duty_day, da=f"{duty_day}/{duty_day}",
            eq=(g["fleet"] or "")[:2] or "32", flt=g["f"][:4],
            orig=g["o"], dest=g["d"],
            dep=f"{pbs_format.hhmm(L(s['dep'], g['o']))}/{pbs_format.hhmm(L(s['dep'], dom))}",
            arr=f"{pbs_format.hhmm(L(s['arr'], g['d']))}/{pbs_format.hhmm(L(s['arr'], dom))}",
            blk=g["blk"], ml=pbs_build.meal_code(L(s["dep"], g["o"])), grnd=grnd,
        ))
    day_dict = dict(
        rpt=f"{pbs_format.hhmm(L(rpt_utc, ss[0]['leg']['o']))}/{pbs_format.hhmm(L(rpt_utc, dom))}",
        rls=f"{pbs_format.hhmm(L(rls_utc, ss[-1]['leg']['d']))}/{pbs_format.hhmm(L(rls_utc, dom))}",
        legs=rows, block=block, synth=0.0, tpay=block, duty=fdp, fdp=fdp,
    )
    lines = pbs_format.sequence_lines(9999, [day_dict], 1, [])
    text = "\n".join(lines) + "\n"
    reparsed = pbs_parser.parse_pbs(text)
    if not reparsed or not reparsed[0]["duty_days"]:
        return None, ["internal error: could not reconstruct the edited day"], {}
    new_day = reparsed[0]["duty_days"][0]
    new_day["duty_day"] = duty_day

    new_seq = copy.deepcopy(seq)
    new_seq["duty_days"] = days[:day_idx] + [new_day]

    meta = {
        "truncated_legs_same_day": len(legs_this_day) - leg_index - 1,
        "truncated_days": len(days) - day_idx - 1,
    }
    return new_seq, violations, meta


def _bid_or_hhmm_span_to_dec(leg):
    """A kept (already-stored) leg's block field is a PBS bid-format string
    ("H.MM", e.g. "4.06" = 4h06m) written by pbs_format.bid(). Falls back to
    deriving it from dep/arr local times if block is missing or malformed —
    shouldn't happen for anything this app itself generated, but a
    hand-typed/edited PBS import is free-form text."""
    raw = (leg.get("block") or "").strip()
    if raw:
        try:
            h, m = raw.split(".")
            return int(h) + int(m) / 60
        except ValueError:
            pass
    try:
        dep = _hhmm_to_dec(leg["dep_local"])
        arr = _hhmm_to_dec(leg["arr_local"])
        span = arr - dep
        return span if span >= 0 else span + 24
    except (KeyError, ValueError):
        return 0.0
