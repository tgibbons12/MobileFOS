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

import pairing_engine
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


def anchor_arrival(ap, disrupted_leg, actual_destination, actual_arrival_local):
    """A bare HHMM arrival time has no day component, so it can land
    numerically *before* the departure it followed (dep 23:00, arr 01:30 the
    next day) — anchor it forward off the real departure instead of trusting
    it as an absolute instant on its own. Returns
    (original_dep_utc, actual_arrival_utc, actual_leg_block), all mutually
    consistent (arrival = departure + block, block >= 0). Shared by
    recover_from_disruption, apply_recovery, and server.py's re-verification
    of an accepted candidate before splicing it in."""
    original_dep_utc = _hhmm_to_dec(disrupted_leg["dep_local"]) - ap.off(disrupted_leg["origin"])
    raw_arrival_utc = _hhmm_to_dec(actual_arrival_local) - ap.off(actual_destination)
    actual_leg_block = raw_arrival_utc - original_dep_utc
    if actual_leg_block < 0:
        actual_leg_block += 24
    return original_dep_utc, original_dep_utc + actual_leg_block, actual_leg_block


def recover_from_disruption(seq, dom, ap, legs, duty_day, leg_index,
                             actual_destination, actual_arrival_local, budget=8.0,
                             max_extra_days=2):
    """The leg at (duty_day, leg_index) is the one that diverted/overran —
    actual_destination/actual_arrival_local (HHMM local at that station) is
    where the pilot really ended up and when. Unlike apply_leg_edit, this
    doesn't validate a single hypothetical replacement leg — it treats the
    disruption as an established fact, then searches FORWARD from that real
    point in space/time for a legal multi-leg path back to `dom`.

    Widens the target total-duty-period count one day at a time (up to
    max_extra_days beyond the original pairing's own length) if the
    original length yields nothing — a disruption can legitimately cost an
    extra day to recover from.

    Returns (candidates, violations). Every candidate is already
    verify_from()-clean by construction — there's no illegal-but-
    confirmable state here the way a manual leg edit can produce, so callers
    don't need a confirm/reject step, just "pick one." candidates carry the
    seed state (day_number/dlegs_today/dblk_today/duty_report_utc/
    total_days) needed later by apply_recovery to correctly splice/render
    whichever one gets accepted.
    """
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return [], ["duty_day not found"]
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day):
        return [], ["leg_index out of range"]

    actual_destination = (actual_destination or "").strip().upper()
    if not actual_destination or not ap.known(actual_destination):
        return [], [f"unknown station: {actual_destination!r}"]

    disrupted_leg = legs_this_day[leg_index]
    kept_legs = legs_this_day[:leg_index]
    try:
        original_dep_utc, actual_arrival_utc, actual_leg_block = anchor_arrival(
            ap, disrupted_leg, actual_destination, actual_arrival_local,
        )
    except (ValueError, KeyError) as e:
        return [], [f"invalid disruption data: {e}"]

    if kept_legs:
        first = kept_legs[0]
        rpt_utc = _hhmm_to_dec(first["dep_local"]) - ap.off(first["origin"]) - Rules.BRIEF
    else:
        rpt_utc = original_dep_utc - Rules.BRIEF

    dblk_so_far = sum(_bid_or_hhmm_span_to_dec(l) for l in kept_legs) + actual_leg_block
    dlegs_so_far = len(kept_legs) + 1
    day_number = duty_day
    original_total_days = days[-1]["duty_day"] if days else duty_day
    floor_days = max(original_total_days, duty_day)

    candidates, tried = [], []
    for total_days in range(floor_days, floor_days + max_extra_days + 1):
        tried.append(total_days)
        search = pairing_engine.Search(legs, ap, total_days, budget)
        chains = search.run_from(actual_destination, actual_arrival_utc, dom, day_number,
                                  dlegs_so_far, dblk_so_far, rpt_utc, total_days)
        for chain in chains:
            bad = pairing_engine.verify_from(
                legs, ap, chain, dom, actual_destination, actual_arrival_utc,
                day_number, dlegs_so_far, dblk_so_far, rpt_utc, total_days,
            )
            if bad:
                continue
            steps, _ = pairing_engine.walk_from(
                legs, ap, chain, actual_destination, actual_arrival_utc,
                day_number, dlegs_so_far, dblk_so_far, rpt_utc,
            )
            block = sum(legs[i]["blk"] for i in chain)
            lpd = pairing_engine.legs_per_day(steps)
            candidates.append({
                "chain": list(chain), "block": round(block, 2),
                "dacv": round(block / total_days, 3) if total_days else 0,
                "legs_per_day": lpd,
                "routing": [actual_destination] + [legs[i]["d"] for i in chain],
                "total_days": total_days, "day_number": day_number,
                "dlegs_today": dlegs_so_far, "dblk_today": dblk_so_far,
                "duty_report_utc": rpt_utc,
            })
        if candidates:
            break

    if not candidates:
        return [], [
            f"no legal way back to {dom} found from {actual_destination} within "
            f"{'/'.join(map(str, tried))} total duty period(s)"
        ]
    candidates.sort(key=lambda c: -c["dacv"])
    return candidates, []


def apply_recovery(seq, dom, ap, legs, duty_day, leg_index, actual_destination,
                    actual_arrival_local, chain, day_number, dlegs_today, dblk_today,
                    duty_report_utc, total_days):
    """Splices an accepted recover_from_disruption() candidate onto the
    sequence: the disrupted leg is rewritten to its real outcome, everything
    after it that same day is dropped (same truncation apply_leg_edit
    already does), and the chosen continuation chain replaces it — spanning
    however many new duty days it needs, not just one."""
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return None, ["duty_day not found"]
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day):
        return None, ["leg_index out of range"]

    disrupted_leg = legs_this_day[leg_index]
    kept_legs = legs_this_day[:leg_index]
    actual_destination = (actual_destination or "").strip().upper()

    try:
        original_dep_utc, actual_arrival_utc, actual_leg_block = anchor_arrival(
            ap, disrupted_leg, actual_destination, actual_arrival_local,
        )
    except (ValueError, KeyError) as e:
        return None, [f"invalid disruption data: {e}"]

    prefix_steps = []
    for l in kept_legs:
        prefix_steps.append(dict(
            day=duty_day,
            leg=dict(f=l["flight_number"], o=l["origin"], d=l["destination"],
                      blk=_bid_or_hhmm_span_to_dec(l), fleet=l.get("equipment", "")),
            dep=_hhmm_to_dec(l["dep_local"]) - ap.off(l["origin"]),
            arr=_hhmm_to_dec(l["arr_local"]) - ap.off(l["destination"]),
        ))
    prefix_steps.append(dict(
        day=duty_day,
        leg=dict(f=disrupted_leg.get("flight_number", ""), o=disrupted_leg["origin"],
                  d=actual_destination, blk=actual_leg_block,
                  fleet=disrupted_leg.get("equipment", "")),
        dep=original_dep_utc, arr=actual_arrival_utc,
    ))

    continuation_steps, continuation_rests = pairing_engine.walk_from(
        legs, ap, chain, actual_destination, actual_arrival_utc,
        day_number, dlegs_today, dblk_today, duty_report_utc,
    )
    if not continuation_steps:
        return None, ["empty recovery chain"]

    all_steps = prefix_steps + continuation_steps
    true_day_numbers = sorted({s["day"] for s in all_steps})
    rendered_days = pbs_build.days_from_steps(ap, all_steps, continuation_rests, dom)

    lines = pbs_format.sequence_lines(9999, rendered_days, 1, [])
    text = "\n".join(lines) + "\n"
    reparsed = pbs_parser.parse_pbs(text)
    if not reparsed or not reparsed[0]["duty_days"]:
        return None, ["internal error: could not reconstruct the recovered days"]
    new_days = reparsed[0]["duty_days"]
    for true_dn, rday in zip(true_day_numbers, new_days):
        rday["duty_day"] = true_dn

    new_seq = copy.deepcopy(seq)
    new_seq["duty_days"] = days[:day_idx] + new_days
    return new_seq, []


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
