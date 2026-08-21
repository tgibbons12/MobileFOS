"""
Turn pairing_engine output into PBS day/leg structures, then text.
Bridges pairing_engine.py (search) and pbs_format.py (layout). Ported from
the standalone pbs_build.py spike — build_pack() (CLI-only file/PDF writer)
is dropped; to_pbs() itself is pure, zero I/O, and used per-request.
"""
from __future__ import annotations

from collections import defaultdict

import pbs_format as F
from pairing_engine import Rules, walk


def meal_code(dep_local):
    """B/L/D/M by departure local time, matching the .gs mealCode_."""
    h = dep_local % 24
    if 5 <= h < 10:
        return "B"
    if 10 <= h < 16:
        return "L"
    if 16 <= h < 21:
        return "D"
    return "M"


def days_from_steps(ap, steps, rests, dom, eq_of=None):
    """Groups a (steps, rests) pair — from either walk() or walk_from() —
    into the day-dict list pbs_format.sequence_lines() wants. Extracted out
    of to_pbs() so a spliced prefix+continuation step list (mid-trip
    recovery, pairing_edit.apply_recovery) can go through the exact same
    rendering path a fresh chain does, instead of duplicating it."""
    L = lambda utc, stn: utc + ap.off(stn)
    byday = defaultdict(list)
    for s in steps:
        byday[s["day"]].append(s)

    days = []
    first_rpt = steps[0]["dep"] - Rules.BRIEF
    # D/A is dutyDay/calendarDay. They diverge whenever a duty period crosses
    # midnight in HOME BASE time.
    day0 = first_rpt + ap.off(dom)
    day0 -= day0 % 24

    def cal_day(utc):
        return 1 + int(((utc + ap.off(dom)) - day0) // 24)

    for di, dn in enumerate(sorted(byday)):
        ss = byday[dn]
        rpt_utc = ss[0]["dep"] - Rules.BRIEF
        rls_utc = ss[-1]["arr"] + Rules.DEBRIEF
        rows = []
        for k, s in enumerate(ss):
            g = s["leg"]
            grnd = None
            if k + 1 < len(ss):
                grnd = ss[k + 1]["dep"] - s["arr"]
            rows.append(dict(
                dp=dn, da=f'{dn}/{cal_day(s["dep"])}',
                eq=(eq_of or {}).get(g["f"], g.get("fleet", "")[:2] or "32"),
                flt=g["f"][:4],
                orig=g["o"], dest=g["d"],
                dep=f"{F.hhmm(L(s['dep'], g['o']))}/{F.hhmm(L(s['dep'], dom))}",
                arr=f"{F.hhmm(L(s['arr'], g['d']))}/{F.hhmm(L(s['arr'], dom))}",
                blk=g["blk"], ml=meal_code(L(s["dep"], g["o"])), grnd=grnd,
            ))
        blk = sum(s["leg"]["blk"] for s in ss)
        d = dict(
            rpt=f"{F.hhmm(L(rpt_utc, ss[0]['leg']['o']))}/{F.hhmm(L(rpt_utc, dom))}",
            rls=f"{F.hhmm(L(rls_utc, ss[-1]['leg']['d']))}/{F.hhmm(L(rls_utc, dom))}",
            legs=rows, block=blk, synth=0.0, tpay=blk,
            duty=rls_utc - rpt_utc,
            fdp=(ss[-1]["arr"] + Rules.DEBRIEF) - rpt_utc,
        )
        if di < len(rests):
            d["hotel"] = ss[-1]["leg"]["d"]
            d["rest"] = rests[di]
        days.append(d)
    days[-1]["tafb"] = (steps[-1]["arr"] + Rules.DEBRIEF) - first_rpt
    return days


def to_pbs(legs, ap, chain, dom, seq_no, start_dow, period, eq_of=None):
    """One pairing -> the dict shape pbs_format.sequence_lines wants.
    Returns (sequence_text_lines, ops_count)."""
    steps, rests = walk(legs, ap, chain)
    days = days_from_steps(ap, steps, rests, dom, eq_of)
    cal, ops = F.calendar_rows(start_dow, period)
    return F.sequence_lines(seq_no, days, ops, cal), ops
