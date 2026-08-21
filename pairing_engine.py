"""
NAC pairing-search engine — ported from the standalone nac_pairings.py spike
(FAR117-style pairing builder for the fictional NAC route network) so
fos-backend can generate candidate pairings in-app instead of only importing
a PBS bid-pack text export.

Ported near-verbatim: Rules, US_DOM/PRECLEAR, Airports, table_a/table_b,
mct_after_arrival/max_sit_at, COL, _hhmm, expand_fleets, Search, walk, verify,
legs_per_day, shape_ok. The CLI (argparse/main/render/fmt/CSV output) is
dropped — this module is imported, never run directly. load_legs is adapted
to read the checked-in CSV export of the route network (data/route_network.csv)
via stdlib csv instead of openpyxl reading a live workbook, and raises
ValueError instead of calling sys.exit() (that's a CLI-only pattern, not
valid inside a request handler).

Time is held in one absolute frame (UTC decimal hours) throughout, exactly as
in the original — see that module's own docstring for why: computing arrivals
in local time and comparing them against a different station's local
departure silently breaks connections across timezones.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTE_CSV_PATH = os.path.join(HERE, "data", "route_network.csv")
AIRPORTS_JSON_PATH = os.path.join(HERE, "data", "airports.json")
DEFAULT_FLEET = "320"
DEFAULT_OPR = "NAC"


# ── RULES — mirror the Pairing Prefs sheet ──────────────────────────────────
class Rules:
    MIN_TURN = 0.45  # 27 min, domestic
    MIN_TURN_INTL = 0.90  # 54 min, arrival INTO the US from a foreign station
    MAX_SIT = 3.0
    MAX_SIT_INTL = 4.5
    MIN_REST = 10.0  # release -> report, NOT arrival -> departure
    MAX_REST = 30.0
    MAX_DUTY_BLOCK = 11.0
    MAX_LEGS_DAY = 4
    BRIEF = 1.0
    DEBRIEF = 0.5
    MIN_DACV = 0.0  # block/day floor; 0 disables


US_DOM = {"US", "CA", "MX", "PR", "VI", "GU", "AS", "MP"}
PRECLEAR = {"AW", "BS", "BM"}  # clear US customs at the foreign origin


# ── airports ────────────────────────────────────────────────────────────────
class Airports:
    def __init__(self, path, month):
        raw = json.load(open(path))
        self.d = {}
        for k, v in raw.items():
            try:
                o = month.replace(tzinfo=ZoneInfo(v["tz"])).utcoffset().total_seconds() / 3600
            except Exception:
                o = v.get("std", 0.0)
            self.d[k] = dict(cc=v["cc"], off=o, city=v.get("city", k))

    def cc(self, c):
        return self.d.get(c, {}).get("cc")

    def off(self, c):
        return self.d.get(c, {}).get("off", 0.0)

    def city(self, c):
        return self.d.get(c, {}).get("city", c)

    def known(self, c):
        return c in self.d


# ── FAR 117 ─────────────────────────────────────────────────────────────────
def table_a(report_hbt):
    """117.11 max flight time, indexed on acclimated (home-base) report time."""
    h = report_hbt % 24
    return 8 if (h < 5 or h >= 20) else 9


_TABLE_B = (
    (4, [9, 9, 9, 9, 9, 9, 9]),
    (5, [10, 10, 10, 10, 9, 9, 9]),
    (6, [12, 12, 12, 12, 11.5, 11, 10.5]),
    (7, [13, 13, 12, 12, 11.5, 11, 10.5]),
    (12, [14, 14, 13, 13, 12.5, 12, 11.5]),
    (13, [13, 13, 13, 13, 12.5, 12, 11.5]),
    (17, [12, 12, 12, 12, 11.5, 11, 10.5]),
    (22, [12, 12, 11, 11, 10, 9, 9]),
)


def table_b(report_hbt, segments):
    h = report_hbt % 24
    i = min(max(int(segments), 1), 7) - 1
    for lim, row in _TABLE_B:
        if h < lim:
            return row[i]
    return [11, 11, 10, 10, 9, 9, 9][i]


def mct_after_arrival(ap, frm, at):
    """Customs is cleared on ARRIVAL INTO THE US — not at a foreign turn station."""
    o, d = ap.cc(frm), ap.cc(at)
    if not o or not d:
        return Rules.MIN_TURN
    if d not in US_DOM:
        return Rules.MIN_TURN  # not landing US-side
    if o in US_DOM:
        return Rules.MIN_TURN  # domestic leg
    if o in PRECLEAR:
        return Rules.MIN_TURN  # cleared before departure
    return Rules.MIN_TURN_INTL


def max_sit_at(ap, stn):
    return Rules.MAX_SIT_INTL if ap.cc(stn) not in US_DOM else Rules.MAX_SIT


# ── timetable ───────────────────────────────────────────────────────────────
COL = dict(
    REGION=0, FLEET=1, AC=2, FLT_OB=3, FLT_IB=4, ORIG=5, DEST=6,
    DEP_OB=9, ARR_OB=10, BLK_OB=11, DEP_IB=12, ARR_IB=13, BLK_IB=14, OPR=15,
)


def _hhmm(x):
    if x is None:
        return None
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit():
        return None
    s = s.zfill(4)
    return int(s[:2]) + int(s[2:]) / 60


def expand_fleets(spec, legs_fleets):
    out = set()
    for tok in spec.split(","):
        t = tok.strip().upper()
        if not t:
            continue
        if "/" in t:
            out.add(t)
        else:
            out |= {f for f in legs_fleets if f.split("/")[0] == t}
    return out


def load_legs_csv(csv_path, ap, fleets, opr=None, region=None):
    """Same shape as the original load_legs (openpyxl -> Smart Routes sheet),
    reading data/route_network.csv (a pre-exported, column-compatible copy)
    via stdlib csv instead. Each row is one round-trip route; fans out into
    two directional legs (outbound + inbound)."""
    legs, skipped = [], defaultdict(int)
    want_r = region.strip().upper() if region else None
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            raise ValueError(f"empty route network file: {csv_path}")
        for r in reader:
            if not r or len(r) <= COL["OPR"]:
                continue
            fl = (r[COL["FLEET"]] or "").strip().upper()
            op = (r[COL["OPR"]] or "").strip().upper()
            reg = (r[COL["REGION"]] or "").strip()
            o = (r[COL["ORIG"]] or "").strip().upper()
            d = (r[COL["DEST"]] or "").strip().upper()
            if not o or not d:
                continue
            if fl not in fleets:
                continue
            if opr and op != opr.upper():
                continue
            if want_r and reg.upper() != want_r:
                continue
            for fn, dep, arr, blk, a, b in (
                (r[COL["FLT_OB"]], r[COL["DEP_OB"]], r[COL["ARR_OB"]], r[COL["BLK_OB"]], o, d),
                (r[COL["FLT_IB"]], r[COL["DEP_IB"]], r[COL["ARR_IB"]], r[COL["BLK_IB"]], d, o),
            ):
                bk, dp = _hhmm(blk), _hhmm(dep)
                if not bk or bk <= 0 or dp is None:
                    continue
                if not ap.known(a) or not ap.known(b):
                    skipped[a if not ap.known(a) else b] += 1
                    continue
                legs.append(dict(
                    f=str(fn or "").strip().replace(".0", ""), o=a, d=b,
                    blk=bk, reg=reg, opr=op, fleet=fl,
                    dep=dp - ap.off(a),
                ))
    for l in legs:
        l["arr"] = l["dep"] + l["blk"]
    if skipped:
        LOG.warning(
            f"{sum(skipped.values())} legs dropped, airport not in airports.json: "
            f"{', '.join(sorted(skipped)[:12])}"
        )
    return legs


# ── search ──────────────────────────────────────────────────────────────────
class Search:
    def __init__(self, legs, ap, days, budget=20.0):
        self.legs, self.ap, self.days, self.budget = legs, ap, days, budget
        self.by = defaultdict(list)
        for i, l in enumerate(legs):
            self.by[l["o"]].append(i)
        for k in self.by:
            self.by[k].sort(key=lambda i: legs[i]["dep"])

    def run(self, dom, must_touch=None, exact_days=True):
        """Fresh trip: seeds the search from every domicile departure,
        duty-period 1."""
        need = set(must_touch or ())
        seeds = []
        hbt = self.ap.off(dom)
        for i in self.by.get(dom, ()):
            g = self.legs[i]
            rep = g["dep"] - Rules.BRIEF
            if g["blk"] <= min(Rules.MAX_DUTY_BLOCK, table_a(rep + hbt)):
                seeds.append(dict(stn=g["d"], t=g["arr"], used={i}, day=1, dlegs=1,
                                   dblk=g["blk"], rep=rep, chain=[i],
                                   hit=(not need) or g["d"] in need))
        return self._search(dom, need, exact_days, seeds, target=dom)

    def run_from(self, station, earliest_utc, dom, day_number, dlegs_today, dblk_today,
                 duty_report_utc, remaining_days, must_touch=None, target=None):
        """Resume the search mid-trip — e.g. recovering from a disruption.
        `station`/`earliest_utc` is where the trip actually stands right now;
        `day_number`/`dlegs_today`/`dblk_today`/`duty_report_utc` describe the
        duty period already in progress at that point (dlegs_today=0,
        dblk_today=0 if the disruption starts a fresh duty period after a
        rest). `remaining_days` is measured the same way `run()`'s own
        `self.days` is — total duty periods from trip start, so it's
        typically the original pairing's own day count, unchanged.

        `target` is where a chain must END to be accepted — defaults to
        `dom` (get back to base), but can be any station, e.g. a day-scoped
        recovery search that just needs to reach the original pairing's
        planned overnight city rather than the whole way home. `dom` itself
        is always used for the home-base-time legality math (table_a/
        table_b) and for "am I sitting at base" checks, regardless of what
        `target` is — those are real crew-rest rules, not search goals."""
        need = set(must_touch or ())
        seed = dict(stn=station, t=earliest_utc, used=set(), day=day_number,
                     dlegs=dlegs_today, dblk=dblk_today, rep=duty_report_utc,
                     chain=[], hit=not need)
        self.days = remaining_days
        return self._search(dom, need, True, [seed], target=target or dom)

    def _search(self, dom, need, exact_days, seeds, target=None):
        legs, ap, R = self.legs, self.ap, Rules
        target = target or dom
        hbt = ap.off(dom)
        out, t0 = [], time.time()
        self.truncated = False

        def dfs(stn, t, used, day, dlegs, dblk, rep, chain, hit):
            if time.time() - t0 > self.budget:
                self.truncated = True
                return
            if stn == target and chain and hit:
                if (not exact_days and day <= self.days) or day == self.days:
                    out.append(tuple(chain))
            if day >= self.days:
                return
            for i in self.by.get(stn, ()):
                if i in used:
                    continue
                g = legs[i]
                # ---- continue the duty day ----
                if dlegs < R.MAX_LEGS_DAY:
                    dep = g["dep"]
                    m = mct_after_arrival(ap, legs[chain[-1]]["o"] if chain else stn, stn)
                    while dep < t + m:
                        dep += 24
                    if dep - t <= max_sit_at(ap, stn):
                        nb = dblk + g["blk"]
                        if nb <= min(R.MAX_DUTY_BLOCK, table_a(rep + hbt)):
                            arr = dep + g["blk"]
                            if (arr + R.DEBRIEF) - rep <= table_b(rep + hbt, dlegs + 1):
                                dfs(g["d"], arr, used | {i}, day, dlegs + 1, nb, rep,
                                    chain + [i], hit or g["d"] in need)
                # ---- overnight: rest is RELEASE -> REPORT ----
                if stn != dom and day < self.days:
                    dep = g["dep"]
                    while (dep - R.BRIEF) - (t + R.DEBRIEF) < R.MIN_REST:
                        dep += 24
                    if (dep - R.BRIEF) - (t + R.DEBRIEF) <= R.MAX_REST:
                        nrep = dep - R.BRIEF
                        if g["blk"] <= min(R.MAX_DUTY_BLOCK, table_a(nrep + hbt)):
                            arr = dep + g["blk"]
                            if (arr + R.DEBRIEF) - nrep <= table_b(nrep + hbt, 1):
                                dfs(g["d"], arr, used | {i}, day + 1, 1, g["blk"], nrep,
                                    chain + [i], hit or g["d"] in need)

        for seed in seeds:
            dfs(seed["stn"], seed["t"], seed["used"], seed["day"], seed["dlegs"],
                seed["dblk"], seed["rep"], seed["chain"], seed["hit"])
        return list(dict.fromkeys(out))


# ── replay / verify ─────────────────────────────────────────────────────────
def walk(legs, ap, chain):
    steps, rests = [], []
    day, t, prev = 1, None, None
    for i in chain:
        g = legs[i]
        if prev is None:
            dep = g["dep"]
        else:
            dep = g["dep"]
            m = mct_after_arrival(ap, legs[prev]["o"], g["o"])
            while dep < t + m:
                dep += 24
            if dep - t > max_sit_at(ap, g["o"]):
                dep = g["dep"]
                while (dep - Rules.BRIEF) - (t + Rules.DEBRIEF) < Rules.MIN_REST:
                    dep += 24
                rests.append((dep - Rules.BRIEF) - (t + Rules.DEBRIEF))
                day += 1
        arr = dep + g["blk"]
        steps.append(dict(day=day, leg=g, dep=dep, arr=arr))
        t, prev = arr, i
    return steps, rests


def verify(legs, ap, chain, dom, days):
    """Independent re-check — the search should never emit a violation."""
    steps, rests = walk(legs, ap, chain)
    bad = []
    if steps[-1]["leg"]["d"] != dom:
        bad.append("does not end at base")
    if steps[0]["leg"]["o"] != dom:
        bad.append("does not start at base")
    if steps[-1]["day"] != days:
        bad.append(f"{steps[-1]['day']} days, wanted {days}")
    for r in rests:
        if r < Rules.MIN_REST - 1e-6:
            bad.append(f"rest {r:.2f}h < {Rules.MIN_REST}")
    byday = defaultdict(list)
    for s in steps:
        byday[s["day"]].append(s)
    for dn, ss in byday.items():
        rep = ss[0]["dep"] - Rules.BRIEF + ap.off(dom)
        blk = sum(s["leg"]["blk"] for s in ss)
        fdp = (ss[-1]["arr"] + Rules.DEBRIEF) - (ss[0]["dep"] - Rules.BRIEF)
        if blk > min(Rules.MAX_DUTY_BLOCK, table_a(rep)) + 1e-6:
            bad.append(f"day {dn} block {blk:.2f} > Table A {table_a(rep)}")
        if fdp > table_b(rep, len(ss)) + 1e-6:
            bad.append(f"day {dn} FDP {fdp:.2f} > Table B {table_b(rep, len(ss))}")
        if len(ss) > Rules.MAX_LEGS_DAY:
            bad.append(f"day {dn} {len(ss)} legs")
    if len(set(chain)) != len(chain):
        bad.append("duplicate leg")
    return bad


def walk_from(legs, ap, chain, station, earliest_utc, day_number, dlegs_today,
              dblk_today, duty_report_utc):
    """walk()'s equivalent for a chain produced by Search.run_from — seeded
    from a mid-trip disruption point instead of a fresh domicile departure.
    The first leg's MCT check uses `station` itself as the previous-origin
    stand-in (the same fallback Search._search's own dfs uses for an empty
    seed chain), since the leg that actually got the pilot to `station`
    isn't part of `chain` — a minor approximation (it can only under- rather
    than over-estimate required connect time) that matches what the search
    itself already assumed when finding this chain."""
    steps, rests = [], []
    day, t, prev_o = day_number, earliest_utc, station
    dlegs, dblk, rep = dlegs_today, dblk_today, duty_report_utc
    for i in chain:
        g = legs[i]
        dep = g["dep"]
        m = mct_after_arrival(ap, prev_o, g["o"])
        while dep < t + m:
            dep += 24
        if dep - t > max_sit_at(ap, g["o"]):
            dep = g["dep"]
            while (dep - Rules.BRIEF) - (t + Rules.DEBRIEF) < Rules.MIN_REST:
                dep += 24
            rests.append((dep - Rules.BRIEF) - (t + Rules.DEBRIEF))
            day += 1
            dlegs, dblk, rep = 0, 0.0, dep - Rules.BRIEF
        arr = dep + g["blk"]
        dlegs += 1
        dblk += g["blk"]
        steps.append(dict(day=day, leg=g, dep=dep, arr=arr))
        t, prev_o = arr, g["o"]
    return steps, rests


def verify_from(legs, ap, chain, dom, station, earliest_utc, day_number, dlegs_today,
                 dblk_today, duty_report_utc, total_days):
    """verify()'s equivalent for a resumed/continuation chain: no "starts at
    base" check (it deliberately doesn't), and day numbering continues from
    the seed's own day_number instead of restarting at 1 the way a plain
    walk(chain) would."""
    if not chain:
        return ["empty continuation"]
    steps, rests = walk_from(legs, ap, chain, station, earliest_utc, day_number,
                              dlegs_today, dblk_today, duty_report_utc)
    bad = []
    if steps[-1]["leg"]["d"] != dom:
        bad.append("does not end at base")
    if steps[-1]["day"] != total_days:
        bad.append(f"{steps[-1]['day']} days, wanted {total_days}")
    for r in rests:
        if r < Rules.MIN_REST - 1e-6:
            bad.append(f"rest {r:.2f}h < {Rules.MIN_REST}")
    byday = defaultdict(list)
    for s in steps:
        byday[s["day"]].append(s)
    for dn, ss in byday.items():
        if dn == day_number:
            # the duty period already in progress at the seed — its report/
            # leg-count/block carry forward from the seed, not just this day's ss.
            rep = duty_report_utc + ap.off(dom)
            blk = dblk_today + sum(s["leg"]["blk"] for s in ss)
            fdp = (ss[-1]["arr"] + Rules.DEBRIEF) - duty_report_utc
            nlegs = dlegs_today + len(ss)
        else:
            rep = ss[0]["dep"] - Rules.BRIEF + ap.off(dom)
            blk = sum(s["leg"]["blk"] for s in ss)
            fdp = (ss[-1]["arr"] + Rules.DEBRIEF) - (ss[0]["dep"] - Rules.BRIEF)
            nlegs = len(ss)
        if blk > min(Rules.MAX_DUTY_BLOCK, table_a(rep)) + 1e-6:
            bad.append(f"day {dn} block {blk:.2f} > Table A {table_a(rep)}")
        if fdp > table_b(rep, nlegs) + 1e-6:
            bad.append(f"day {dn} FDP {fdp:.2f} > Table B {table_b(rep, nlegs)}")
        if nlegs > Rules.MAX_LEGS_DAY:
            bad.append(f"day {dn} {nlegs} legs")
    if len(set(chain)) != len(chain):
        bad.append("duplicate leg")
    return bad


def legs_per_day(steps):
    c = defaultdict(int)
    for s in steps:
        c[s["day"]] += 1
    return [c[k] for k in sorted(c)]


def shape_ok(lpd):
    """Day 1 ODD, middle days EVEN, closing day ODD — display heuristic only,
    not a legality rule."""
    if len(lpd) == 1:
        return lpd[0] % 2 == 1
    return lpd[0] % 2 == 1 and lpd[-1] % 2 == 1 and all(x % 2 == 0 for x in lpd[1:-1])


# ── route-network cache ──────────────────────────────────────────────────────
_ROUTE_CACHE = {}  # (year, month) -> (legs, ap)


def get_route_data(dt=None):
    """Loads + caches the checked-in route network + airport table, keyed by
    (year, month) since Airports' DST offsets are month-dependent. Never
    re-parses the CSV per request — Search/load_legs_csv are only invoked
    here, once per process per calendar month."""
    dt = dt or datetime.now(timezone.utc)
    key = (dt.year, dt.month)
    if key not in _ROUTE_CACHE:
        ap = Airports(AIRPORTS_JSON_PATH, datetime(dt.year, dt.month, 15, 12))
        with open(ROUTE_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            all_fleets = {
                row[COL["FLEET"]].strip().upper()
                for row in reader if len(row) > COL["FLEET"] and row[COL["FLEET"]]
            }
        fleets = expand_fleets(DEFAULT_FLEET, all_fleets)
        legs = load_legs_csv(ROUTE_CSV_PATH, ap, fleets, opr=DEFAULT_OPR)
        _ROUTE_CACHE[key] = (legs, ap)
    return _ROUTE_CACHE[key]
