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


def anchor_available(ap, disrupted_leg, kept_legs, available_local):
    """The other shape a disruption takes: the planned leg is NOT flown —
    it cancelled, or it is going so late that what actually gets flown has
    to be searched for rather than assumed. The crew is still standing at
    the leg's ORIGIN, free from `available_local` (HHMM local there), and
    nothing about that leg is added to the duty day.

    Like anchor_arrival, a bare HHMM has no day component: it is anchored
    forward off the leg's own scheduled departure (a delay past midnight
    reads as an earlier clock time), and past whatever the crew last flew.
    Returns (resume_station, resume_utc)."""
    origin = disrupted_leg["origin"]
    scheduled_dep_utc = _hhmm_to_dec(disrupted_leg["dep_local"]) - ap.off(origin)
    resume_utc = _hhmm_to_dec(available_local) - ap.off(origin)
    while resume_utc < scheduled_dep_utc:
        resume_utc += 24
    if kept_legs:
        last = kept_legs[-1]
        last_arr_utc = _hhmm_to_dec(last["arr_local"]) - ap.off(last["destination"])
        while resume_utc < last_arr_utc:
            resume_utc += 24
    return origin, resume_utc


def recover_from_disruption(seq, dom, ap, legs, duty_day, leg_index,
                             actual_destination, actual_arrival_local, budget=8.0,
                             max_extra_days=2, drop_disrupted=False):
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
        if drop_disrupted:
            # Never flown, so it costs the duty day nothing and leaves the
            # crew where they started.
            actual_destination, actual_arrival_utc = anchor_available(
                ap, disrupted_leg, kept_legs, actual_arrival_local,
            )
            original_dep_utc, actual_leg_block = actual_arrival_utc, 0.0
        else:
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
    dlegs_so_far = len(kept_legs) + (0 if drop_disrupted else 1)
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
                    duty_report_utc, total_days, drop_disrupted=False):
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
        if drop_disrupted:
            actual_destination, actual_arrival_utc = anchor_available(
                ap, disrupted_leg, kept_legs, actual_arrival_local,
            )
            original_dep_utc, actual_leg_block = actual_arrival_utc, 0.0
        else:
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
    # A leg that never flew leaves no trace on the rebuilt day — writing it
    # in anyway is what produced the ORIGIN-ORIGIN phantom leg.
    if not drop_disrupted:
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


def find_network_leg_after(legs, origin, destination, earliest_utc, flight_number=None):
    """Like find_network_leg, but only considers a departure at/after
    earliest_utc — wrapping the leg's own dep forward by 24h as many times
    as needed, since a leg's dep is a bare hour-of-day with no day
    component of its own. Returns (leg, wrapped_dep_utc) or (None, None)."""
    origin = (origin or "").strip().upper()
    destination = (destination or "").strip().upper()
    candidates = [l for l in legs if l["o"] == origin and l["d"] == destination]
    if flight_number:
        fn = str(flight_number).strip()
        exact = [l for l in candidates if l["f"] == fn]
        if exact:
            candidates = exact
    if not candidates:
        return None, None
    best_leg, best_dep = None, None
    for l in candidates:
        dep = l["dep"]
        while dep < earliest_utc:
            dep += 24
        if best_dep is None or dep < best_dep:
            best_leg, best_dep = l, dep
    return best_leg, best_dep


def _prefix_rest_state(days, day_idx, duty_day, leg_index, ap, rest_start_local,
                       report_local=None):
    """Shared setup for retry_shifted_plan/day_scoped_recovery/
    apply_day_patch: this day's still-valid legs before leg_index, and the
    earliest legal report time for whatever comes next, anchored off
    rest_start_local (when duty actually ended) wrapped forward to be no
    earlier than the last thing that actually flew before it.

    report_local is the other way in, and it exists for the most ordinary
    disruption there is: the pilot has not signed in yet and is simply
    reporting late. There is no completed duty to rest off, so
    rest_start + MIN_REST is the wrong question — it would answer a 14:45
    report with "earliest report 00:45 tomorrow" and scrub a day that is
    perfectly legal to fly. When given, it IS the earliest report, used
    directly.

    The two are mutually exclusive: rest_start_local says duty ended and
    rest is being served, report_local says duty has not begun.
    """
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    kept_legs = legs_this_day[:leg_index]
    if kept_legs:
        last = kept_legs[-1]
        rest_station = last["destination"]
        last_arr_utc = _hhmm_to_dec(last["arr_local"]) - ap.off(last["destination"])
    elif day_idx > 0 and (days[day_idx - 1].get("legs") or []):
        prev_last = days[day_idx - 1]["legs"][-1]
        rest_station = prev_last["destination"]
        last_arr_utc = _hhmm_to_dec(prev_last["arr_local"]) - ap.off(prev_last["destination"])
    else:
        pending0 = legs_this_day[leg_index]
        rest_station = pending0["origin"]
        last_arr_utc = _hhmm_to_dec(pending0["dep_local"]) - ap.off(pending0["origin"])

    if report_local is not None:
        # Reporting late off a rest that is already complete. Still wrapped
        # forward past whatever last flew, so a report time that reads
        # earlier in the clock than the previous arrival lands on the right
        # day rather than in the past.
        earliest_report_utc = _hhmm_to_dec(report_local) - ap.off(rest_station)
        while earliest_report_utc < last_arr_utc:
            earliest_report_utc += 24
        return kept_legs, rest_station, earliest_report_utc

    raw_rest_start_utc = _hhmm_to_dec(rest_start_local) - ap.off(rest_station)
    rest_start_utc = raw_rest_start_utc
    while rest_start_utc < last_arr_utc:
        rest_start_utc += 24
    earliest_report_utc = rest_start_utc + Rules.MIN_REST
    return kept_legs, rest_station, earliest_report_utc


def _step_matched_leg(ap, legs, pl, t, prev_origin, dlegs, dblk, rep, hbt):
    """Try to place originally-planned leg `pl` next in a shifted replay,
    given engine state (t=prev arrival UTC, prev_origin, and the current
    duty period's dlegs/dblk/rep). Prefers a same-day continuation at the
    leg's next real occurrence; falls back to overnight if the same-day
    connection doesn't fit legally — mirrors Search._search's own dfs
    branch logic, just picking a real leg via find_network_leg_after
    instead of exploring every candidate.

    Returns (net_leg, dep, arr, new_dlegs, new_dblk, new_rep, is_overnight)
    or None if no legal placement exists at all."""
    if dlegs < Rules.MAX_LEGS_DAY:
        m = mct_after_arrival(ap, prev_origin, pl["origin"])
        net_leg, dep = find_network_leg_after(legs, pl["origin"], pl["destination"], t + m, pl.get("flight_number"))
        if net_leg is not None and dep - t <= max_sit_at(ap, pl["origin"]):
            nb = dblk + net_leg["blk"]
            if nb <= min(Rules.MAX_DUTY_BLOCK, table_a(rep + hbt)):
                arr = dep + net_leg["blk"]
                if (arr + Rules.DEBRIEF) - rep <= table_b(rep + hbt, dlegs + 1):
                    return net_leg, dep, arr, dlegs + 1, nb, rep, False
    net_leg, dep = find_network_leg_after(
        legs, pl["origin"], pl["destination"],
        t + Rules.DEBRIEF + Rules.MIN_REST + Rules.BRIEF, pl.get("flight_number"),
    )
    if net_leg is None:
        return None
    nrep = dep - Rules.BRIEF
    if net_leg["blk"] > min(Rules.MAX_DUTY_BLOCK, table_a(nrep + hbt)):
        return None
    arr = dep + net_leg["blk"]
    if (arr + Rules.DEBRIEF) - nrep > table_b(nrep + hbt, 1):
        return None
    return net_leg, dep, arr, 1, net_leg["blk"], nrep, True


def retry_shifted_plan(seq, dom, ap, legs, duty_day, leg_index, rest_start_local,
                       report_local=None):
    """The pilot's disruption is: everything from (duty_day, leg_index)
    onward didn't fly as planned — duty ended instead, at rest_start_local.
    Tries to replay the ENTIRE remainder of the original sequence — every
    still-pending leg, on this day and every day after it — using the SAME
    flight numbers, each found at its next real legal occurrence, chained
    through the same day/overnight legality checks the original plan
    itself implied. This is the cheap, most-likely-correct fix: same plan,
    just later — matching the real-world pattern of a delayed flight timing
    out into rest, then flying the same flight number's next scheduled
    occurrence the following day.

    Returns (new_seq, None) on full success — every remaining leg matched
    and reconnected legally.
    Returns (None, failure) if some leg couldn't be matched or wouldn't
    connect legally, where failure = {"duty_day", "leg_index",
    "target_station"} — target_station is that ORIGINAL day's own final
    destination, for day_scoped_recovery to aim at instead.
    """
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return None, {"duty_day": duty_day, "leg_index": leg_index, "target_station": None}
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day):
        return None, {"duty_day": duty_day, "leg_index": leg_index, "target_station": None}

    try:
        kept_legs, _rest_station, earliest_report_utc = _prefix_rest_state(
            days, day_idx, duty_day, leg_index, ap, rest_start_local,
            report_local=report_local,
        )
    except (ValueError, KeyError):
        return None, {"duty_day": duty_day, "leg_index": leg_index, "target_station": None}

    day_end_station = {duty_day: legs_this_day[-1]["destination"]}
    pending = [(duty_day, i, l) for i, l in enumerate(legs_this_day) if i >= leg_index]
    for d in days[day_idx + 1:]:
        dl = d.get("legs") or []
        if dl:
            day_end_station[d["duty_day"]] = dl[-1]["destination"]
        pending += [(d["duty_day"], i, l) for i, l in enumerate(dl)]
    if not pending:
        return None, {"duty_day": duty_day, "leg_index": leg_index, "target_station": None}

    hbt = ap.off(dom)
    matched = []
    t = prev_origin = None
    dlegs, dblk, rep = 0, 0.0, earliest_report_utc

    for orig_day, orig_idx, pl in pending:
        target_station = day_end_station.get(orig_day)
        if t is None:
            net_leg, dep = find_network_leg_after(
                legs, pl["origin"], pl["destination"], earliest_report_utc + Rules.BRIEF, pl.get("flight_number"),
            )
            if net_leg is None:
                return None, {"duty_day": orig_day, "leg_index": orig_idx, "target_station": target_station}
            arr = dep + net_leg["blk"]
            dlegs, dblk, rep = 1, net_leg["blk"], earliest_report_utc
            overnight = False
        else:
            step = _step_matched_leg(ap, legs, pl, t, prev_origin, dlegs, dblk, rep, hbt)
            if step is None:
                return None, {"duty_day": orig_day, "leg_index": orig_idx, "target_station": target_station}
            net_leg, dep, arr, dlegs, dblk, rep, overnight = step
        matched.append(dict(leg=net_leg, dep=dep, arr=arr, overnight=overnight))
        t, prev_origin = arr, pl["origin"]

    # ---- render the fully-matched replay, then reparse through pbs_parser
    # so it lands in exactly the format a real generate/edit would produce ----
    kept_last_arr_utc = None
    if kept_legs:
        last = kept_legs[-1]
        kept_last_arr_utc = _hhmm_to_dec(last["arr_local"]) - ap.off(last["destination"])
    elif day_idx > 0 and (days[day_idx - 1].get("legs") or []):
        prev_last = days[day_idx - 1]["legs"][-1]
        kept_last_arr_utc = _hhmm_to_dec(prev_last["arr_local"]) - ap.off(prev_last["destination"])

    steps, rests, day_ctr = [], [], duty_day + 1
    for idx, m in enumerate(matched):
        if idx == 0:
            if kept_last_arr_utc is not None:
                rests.append((m["dep"] - Rules.BRIEF) - (kept_last_arr_utc + Rules.DEBRIEF))
        elif m["overnight"]:
            day_ctr += 1
            rests.append((m["dep"] - Rules.BRIEF) - (steps[-1]["arr"] + Rules.DEBRIEF))
        steps.append(dict(day=day_ctr, leg=m["leg"], dep=m["dep"], arr=m["arr"]))

    prefix_steps = []
    for l in kept_legs:
        prefix_steps.append(dict(
            day=duty_day,
            leg=dict(f=l["flight_number"], o=l["origin"], d=l["destination"],
                      blk=_bid_or_hhmm_span_to_dec(l), fleet=l.get("equipment", "")),
            dep=_hhmm_to_dec(l["dep_local"]) - ap.off(l["origin"]),
            arr=_hhmm_to_dec(l["arr_local"]) - ap.off(l["destination"]),
        ))
    all_steps = prefix_steps + steps
    true_day_numbers = sorted({s["day"] for s in all_steps})
    rendered_days = pbs_build.days_from_steps(ap, all_steps, rests, dom)

    lines = pbs_format.sequence_lines(9999, rendered_days, 1, [])
    text = "\n".join(lines) + "\n"
    reparsed = pbs_parser.parse_pbs(text)
    if not reparsed or not reparsed[0]["duty_days"]:
        return None, {"duty_day": duty_day, "leg_index": leg_index, "target_station": None}
    new_days = reparsed[0]["duty_days"]
    for true_dn, rday in zip(true_day_numbers, new_days):
        rday["duty_day"] = true_dn

    new_seq = copy.deepcopy(seq)
    new_seq["duty_days"] = days[:day_idx] + new_days
    return new_seq, None


def day_scoped_recovery(seq, dom, ap, legs, duty_day, leg_index, rest_start_local,
                         budget=8.0, max_extra_days=1, report_local=None):
    """When retry_shifted_plan() fails on the SAME day the disruption itself
    happened on (the common case — the very next leg after the disruption
    can't be flown as planned), this searches for a legal way to reach that
    day's ORIGINAL final destination instead of replaying the exact plan —
    a narrower goal than recover_from_disruption (which searches all the
    way back to `dom`), so the rest of the original trip can still be
    reattached unchanged via apply_day_patch. Falls back to targeting `dom`
    itself if the original destination genuinely isn't reachable that day —
    landing somewhere sensible beats no answer at all.

    NOTE: scoped to duty_day == the original disruption day. If
    retry_shifted_plan instead fails on a LATER day (a rarer case — the
    shifted plan worked for a while, then broke further downstream), the
    caller should fall back to recover_from_disruption/apply_recovery (the
    whole-trip search) rather than calling this — reattaching "the rest of
    the trip unchanged" only makes sense when nothing after this exact day
    needed to shift in the first place.

    Returns (candidates, target_station, reached_target).
    """
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return [], None, False
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day) or not legs_this_day:
        return [], None, False
    target_station = legs_this_day[-1]["destination"]

    try:
        _kept_legs, _rest_station, earliest_report_utc = _prefix_rest_state(
            days, day_idx, duty_day, leg_index, ap, rest_start_local,
            report_local=report_local,
        )
    except (ValueError, KeyError):
        return [], target_station, False

    pending0 = legs_this_day[leg_index]
    day_number = duty_day + 1
    dlegs_today, dblk_today, duty_report_utc = 0, 0.0, earliest_report_utc
    resume_station = pending0["origin"]
    resume_utc = earliest_report_utc + Rules.BRIEF

    def _search_for(target, total_days):
        search = pairing_engine.Search(legs, ap, total_days, budget)
        chains = search.run_from(resume_station, resume_utc, dom, day_number,
                                  dlegs_today, dblk_today, duty_report_utc,
                                  total_days, target=target)
        out = []
        for chain in chains:
            bad = pairing_engine.verify_from(
                legs, ap, chain, target, resume_station, resume_utc,
                day_number, dlegs_today, dblk_today, duty_report_utc, total_days,
            )
            if bad:
                continue
            steps, _ = pairing_engine.walk_from(
                legs, ap, chain, resume_station, resume_utc,
                day_number, dlegs_today, dblk_today, duty_report_utc,
            )
            block = sum(legs[i]["blk"] for i in chain)
            lpd = pairing_engine.legs_per_day(steps)
            out.append({
                "chain": list(chain), "block": round(block, 2),
                "dacv": round(block / total_days, 3) if total_days else 0,
                "legs_per_day": lpd,
                "routing": [resume_station] + [legs[i]["d"] for i in chain],
                "total_days": total_days, "day_number": day_number,
                "dlegs_today": dlegs_today, "dblk_today": dblk_today,
                "duty_report_utc": duty_report_utc,
            })
        out.sort(key=lambda c: -c["dacv"])
        return out

    for extra in range(max_extra_days + 1):
        candidates = _search_for(target_station, day_number + extra)
        if candidates:
            return candidates, target_station, True

    for extra in range(max_extra_days + 2):
        candidates = _search_for(dom, day_number + extra)
        if candidates:
            return candidates, dom, False

    return [], target_station, False


def apply_day_patch(seq, dom, ap, legs, duty_day, leg_index, rest_start_local,
                     chain, day_number, dlegs_today, dblk_today, duty_report_utc, total_days,
                     reattach=True, report_local=None):
    """Splices an accepted day_scoped_recovery() candidate onto the
    sequence for just the disrupted day. When `reattach` is True (pass
    day_scoped_recovery's own `reached_target` here — only reattach when
    the patch actually made it to that day's original planned destination),
    every ORIGINAL day after it comes along unchanged, renumbered to stay
    contiguous — unlike apply_recovery, which discards the rest of the
    trip. When `reached_target` was False (the search had to settle for
    `dom` instead), reattaching is skipped — the original later days
    assumed starting from a city we never actually reached, so there's
    nothing legitimate left to preserve; the patched trip just ends here,
    same as apply_recovery's whole-trip behavior.

    Even when reattach=True, this re-checks that the first reattached
    day's own report time still leaves legal rest after the patch's new
    (later) release — a day-scoped patch can shift the release later than
    the original plan assumed, which can retroactively break the very
    connection it's trying to preserve. Returns (None, [...]) rather than
    silently splicing in an illegal connection if so — the caller should
    fall back to the whole-trip recovery search instead."""
    days = seq.get("duty_days") or []
    day_idx = next((i for i, d in enumerate(days) if d["duty_day"] == duty_day), None)
    if day_idx is None:
        return None, ["duty_day not found"]
    day = days[day_idx]
    legs_this_day = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(legs_this_day):
        return None, ["leg_index out of range"]

    try:
        kept_legs, _rest_station, earliest_report_utc = _prefix_rest_state(
            days, day_idx, duty_day, leg_index, ap, rest_start_local,
            report_local=report_local,
        )
    except (ValueError, KeyError) as e:
        return None, [f"invalid disruption data: {e}"]

    pending0 = legs_this_day[leg_index]
    resume_station = pending0["origin"]
    resume_utc = earliest_report_utc + Rules.BRIEF

    prefix_steps = []
    for l in kept_legs:
        prefix_steps.append(dict(
            day=duty_day,
            leg=dict(f=l["flight_number"], o=l["origin"], d=l["destination"],
                      blk=_bid_or_hhmm_span_to_dec(l), fleet=l.get("equipment", "")),
            dep=_hhmm_to_dec(l["dep_local"]) - ap.off(l["origin"]),
            arr=_hhmm_to_dec(l["arr_local"]) - ap.off(l["destination"]),
        ))

    patch_steps, patch_rests = pairing_engine.walk_from(
        legs, ap, chain, resume_station, resume_utc,
        day_number, dlegs_today, dblk_today, duty_report_utc,
    )
    if not patch_steps:
        return None, ["empty patch chain"]

    all_steps = prefix_steps + patch_steps
    true_day_numbers = sorted({s["day"] for s in all_steps})
    rendered_days = pbs_build.days_from_steps(ap, all_steps, patch_rests, dom)

    lines = pbs_format.sequence_lines(9999, rendered_days, 1, [])
    text = "\n".join(lines) + "\n"
    reparsed = pbs_parser.parse_pbs(text)
    if not reparsed or not reparsed[0]["duty_days"]:
        return None, ["internal error: could not reconstruct the patched day(s)"]
    new_days = reparsed[0]["duty_days"]
    for true_dn, rday in zip(true_day_numbers, new_days):
        rday["duty_day"] = true_dn

    patched_last_day = true_day_numbers[-1]
    day_shift = patched_last_day - duty_day
    reattached = []
    if reattach:
        remaining_original = days[day_idx + 1:]
        if remaining_original:
            patch_release_utc = patch_steps[-1]["arr"] + Rules.DEBRIEF
            first_next = remaining_original[0]
            first_next_legs = first_next.get("legs") or []
            if first_next_legs:
                origin0 = first_next_legs[0]["origin"]
                try:
                    report_utc = _hhmm_to_dec(first_next["report"]) - ap.off(origin0)
                except (ValueError, KeyError, TypeError):
                    report_utc = None
                if report_utc is not None:
                    while report_utc < patch_release_utc:
                        report_utc += 24
                    rest = report_utc - patch_release_utc
                    if rest < Rules.MIN_REST - 1e-6:
                        return None, [
                            f"reattaching the rest of the original trip would leave only "
                            f"{rest:.2f}h rest before day {first_next['duty_day']} (min {Rules.MIN_REST}h) — "
                            f"the patch pushed the release too late for it to still connect legally"
                        ]
        for d in remaining_original:
            rd = copy.deepcopy(d)
            rd["duty_day"] = d["duty_day"] + day_shift
            reattached.append(rd)

    new_seq = copy.deepcopy(seq)
    new_seq["duty_days"] = days[:day_idx] + new_days + reattached
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
