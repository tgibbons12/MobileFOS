"""
Parser for the NAC crew pairing builder's PBS bid-pack text export.
Turns the fixed-width report into structured sequence/duty-day/leg data,
then flattens that into FOS-schema "leg" dicts (see server.py DEFAULT_LEG).
"""
import re

META_RE = {
    "operator_fleet": re.compile(r'OPERATOR / FLEET\s+(\S+)\s+(\S+)'),
    "base": re.compile(r'BASE\s+(\S+)\s+(.+)'),
    "bid_period": re.compile(r'BID PERIOD\s+(\S+)\s*-\s*(\S+)'),
}

SEQ_HEADER_RE = re.compile(
    r'^SEQ\s+(?P<seq>\d+)\s+(?P<ops>\d+)\s+OPS\s+POSN\s+(?P<positions>[A-Z ]+?)\s{2,}'
)
RPT_RE = re.compile(r'^\s*RPT\s+(?P<out>\d{4})/(?P<zout>\d{4})')
# No per-day TAFB field actually exists in this format — TAFB (time away
# from base) only makes sense for the whole pairing, and is captured by
# TTL_RE below. What used to be captured here as a trailing "tafp" group
# was really just eating the sequence's own calendar day-of-month usage
# marker (a bare integer following the duty/FDP figures, e.g. "10" in
# "RLS 2100/0000  5.57  0.00  5.57  7.27  7.27 10 -- -- -- -- -- --") —
# same misreading bug class as _parse_leg_line's ground-time fix above,
# just one line format over. Every day of a trip shares that marker, which
# is why it silently showed the exact same bogus "TAFB" on every day card.
RLS_RE = re.compile(
    r'^\s*RLS\s+(?P<in_>\d{4})/(?P<zin>\d{4})\s+'
    r'(?P<block>[\d.]+)\s+(?P<grnd>[\d.]+)\s+(?P<tpay>[\d.]+)\s+'
    r'(?P<duty>[\d.]+)(?:\s+(?P<fdp>[\d.]+))?'
)
HOTEL_RE = re.compile(r'^\s*(?P<sta>[A-Z]{3})\s+HOTEL\s+(?P<hotel>\S+)(?:\s+(?P<rest>[\d.]+))?')
# The pairing-total row — same four columns as RLS (block/ground/tpay/tafb)
# but cumulative for the whole sequence, no duty/fdp column. Previously
# just detected-and-skipped; now captured so the library can show a
# per-pairing TAFB/TPAY figure instead of only per-day ones.
TTL_RE = re.compile(
    r'^TTL\s+(?P<block>[\d.]+)\s+(?P<grnd>[\d.]+)\s+(?P<tpay>[\d.]+)\s+(?P<tafb>[\d.]+)'
)

LEG_TOKEN_RE = re.compile(r'\S+')


def _parse_leg_line(line):
    """Token-walk a single flight-leg row. Returns None if it isn't one
    (release/report/hotel/TTL lines are handled by their own regexes)."""
    toks = LEG_TOKEN_RE.findall(line)
    if len(toks) < 7:
        return None
    if not re.match(r'^\d+$', toks[0]) or '/' not in toks[1]:
        return None  # not "DP D/A ..." — not a leg line
    da = toks[1]  # duty-day/calendar-day, e.g. "1/1" — differs ("3/4") when
    # a leg spills into the next calendar day (a red-eye).

    i = 2
    if i >= len(toks):
        return None
    eq = toks[i]; i += 1

    flt = ''
    # 1-5 digits, not 2-5: single-digit flight numbers are real (the
    # flagship transcons are AA 1/2/3/8/9) and requiring two digits didn't
    # just lose the number, it dropped the whole leg — the station check
    # below would then see "1" where it wanted "JFK" and bail out, so e.g.
    # SEQ 29856's JFK-LAX day-1 leg silently vanished from the pairing.
    # Unambiguous: the token after the equipment code is either the flight
    # number (digits) or the station (three letters), never both.
    if i < len(toks) and re.match(r'^\d{1,5}$', toks[i]):
        flt = toks[i]; i += 1

    if i >= len(toks) or not re.match(r'^[A-Z]{3}$', toks[i]):
        return None
    sta1 = toks[i]; i += 1

    if i >= len(toks):
        return None
    m = re.match(r'^(\d{4})/(\d{4})$', toks[i])
    if not m:
        return None
    dep_local, dep_z = m.groups(); i += 1

    meal = ''
    if i < len(toks) and re.match(r'^[A-Z]$', toks[i]):
        meal = toks[i]; i += 1

    if i >= len(toks) or not re.match(r'^[A-Z]{3}$', toks[i]):
        return None
    sta2 = toks[i]; i += 1

    if i >= len(toks):
        return None
    m = re.match(r'^(\d{4})/(\d{4})$', toks[i])
    if not m:
        return None
    arr_local, arr_z = m.groups(); i += 1

    # Decimal only (\d+\.\d+), not bare [\d.]+ — the last leg of a duty day
    # has no ground-time token at all, so the next thing on the line is the
    # calendar day-of-month usage marker (a bare integer like "24"), which
    # a looser digits-and-dots pattern would misread as a real ground time.
    dur_re = re.compile(r'^\d+\.\d+$')
    block = toks[i] if i < len(toks) and dur_re.match(toks[i]) else ''
    if i < len(toks):
        i += 1
    ground = toks[i] if i < len(toks) and dur_re.match(toks[i]) else ''

    return {
        "equipment": eq, "flight_number": flt, "da": da,
        "origin": sta1, "dep_local": dep_local, "dep_z": dep_z, "meal": meal,
        "destination": sta2, "arr_local": arr_local, "arr_z": arr_z,
        "block": block, "ground": ground,
    }


def sequence_calendar_days(s):
    """Real calendar-day span of the trip, including any fully "dead" day
    with no duty at all. A duty_days entry only exists where there's an
    RPT line, so a layover long enough to swallow a whole calendar day
    (24+ hours — the crew simply has a reserve day at the layover
    station with nothing scheduled) leaves no trace in duty_days itself:
    len(duty_days) silently undercounts the trip by one or more days.

    Each leg's own "da" field ("duty_day/calendar_day", e.g. "2/3" — see
    _parse_leg_line) already carries the true calendar day the airline's
    own pairing builder assigned that leg to, dead days included, since
    it jumps (2 -> 3, not 2 -> 2) across one. This is just the max of
    that second number across every leg in the trip — falls back to
    len(duty_days) only if no leg has a parseable "da" (shouldn't happen
    with real data, but a duty day with zero legs is technically
    possible if a day's legs failed to parse)."""
    max_day = 0
    for day in s["duty_days"]:
        for leg in day["legs"]:
            da = leg.get("da") or ""
            if "/" in da:
                try:
                    max_day = max(max_day, int(da.split("/")[1]))
                except ValueError:
                    pass
    return max_day or len(s["duty_days"])


def parse_pbs(text):
    """Returns a list of sequence dicts:
    {seq, ops_per_period, positions, tafb, tpay, block, duty_days: [
        {duty_day, report, legs: [...], release, hotel, block, duty, tpay, tafb}
    ]}
    tafb/tpay/block at the top level are the sequence's own TTL row —
    cumulative for the whole pairing, not just its last day.
    """
    marker = '===' * 3
    idx = text.find(marker)
    detail = text[idx:] if idx != -1 else text

    blocks = re.split(r'\r?\n-{20,}[ \t]*\r?\n', detail)
    sequences = []

    for block in blocks:
        header_match = None
        for line in block.splitlines():
            header_match = SEQ_HEADER_RE.match(line)
            if header_match:
                break
        if not header_match:
            continue

        seq = {
            "seq": header_match.group("seq"),
            "ops_per_period": header_match.group("ops"),
            "positions": header_match.group("positions").split(),
            "block": None, "tpay": None, "tafb": None,
            "duty_days": [],
        }
        current_day = None
        duty_day_num = 0

        for line in block.splitlines():
            if SEQ_HEADER_RE.match(line):
                continue
            m = RPT_RE.match(line)
            if m:
                duty_day_num += 1
                current_day = {
                    "duty_day": duty_day_num,
                    "report": m.group("out"), "report_hbt": m.group("zout"),
                    "legs": [],
                    "release": None, "release_hbt": None, "hotel": None, "hotel_rest": None,
                    "block": None, "duty": None, "tpay": None,
                }
                seq["duty_days"].append(current_day)
                continue
            m = RLS_RE.match(line)
            if m and current_day is not None:
                current_day["release"] = m.group("in_")
                current_day["release_hbt"] = m.group("zin")
                current_day["block"] = m.group("block")
                current_day["duty"] = m.group("duty")
                current_day["tpay"] = m.group("tpay")
                continue
            m = HOTEL_RE.match(line)
            if m and current_day is not None:
                current_day["hotel"] = f'{m.group("sta")} {m.group("hotel")}'
                current_day["hotel_rest"] = m.group("rest")
                continue
            m = TTL_RE.match(line)
            if m:
                seq["block"] = m.group("block")
                seq["tpay"] = m.group("tpay")
                seq["tafb"] = m.group("tafb")
                continue
            leg = _parse_leg_line(line)
            if leg and current_day is not None:
                current_day["legs"].append(leg)

        if seq["duty_days"]:
            sequences.append(seq)

    return sequences


def parse_pbs_meta(text):
    """Pulls the RUN block at the top of the file: operator/fleet, base, bid period."""
    meta = {}
    m = META_RE["operator_fleet"].search(text)
    if m:
        meta["operator"], meta["fleet"] = m.groups()
    m = META_RE["base"].search(text)
    if m:
        meta["base"] = m.group(1)
        meta["base_name"] = m.group(2).strip()
    m = META_RE["bid_period"].search(text)
    if m:
        meta["bid_start"], meta["bid_end"] = m.groups()
    return meta


def _fmt_time(hhmm):
    return f"{hhmm[:2]}:{hhmm[2:]}" if hhmm and len(hhmm) == 4 else (hhmm or "")


def _hhmm_to_dec(hhmm):
    return int(hhmm[:2]) + int(hhmm[2:]) / 60.0


def _dec_to_hhmm(dec):
    dec = dec % 24
    h = int(dec)
    m = round((dec - h) * 60)
    if m == 60:
        h, m = h + 1, 0
    return f"{h % 24:02d}{m:02d}"


def mot_for_leg(day, leg_index, report_override=None):
    """Mandatory Off Time for one leg — the latest the CURRENT duty day
    could still release and stay inside the legal FAR 117 duty period,
    computed backward from the day's own report time:

        FDP_end        = report + table_b(report_hbt, legs_today)
        time_used       = sum(block + ground) for every leg BEFORE this one
        time_remaining  = day["duty"] - time_used
        MOT             = FDP_end - time_remaining

    day["duty"] is the bid pack's own published scheduled duty-period
    length (report to release, straight from the RLS line) — using it as
    the anchor, not a re-summed block+ground total, matters: block+ground
    alone omits BRIEF/DEBRIEF and any other overhead the airline already
    baked into that figure, which is exactly why an 8-hour planned duty
    day with only ~4 hours of raw block time was showing MOT four hours
    later than it should (report 1200, 12hr FDP avail, 8hr planned duty
    should show MOT 1600 — FDP_end 0000 minus the full 8hr remaining, not
    minus just the flight's own block time). Subtracting only what's
    ALREADY ELAPSED (legs before this one) rather than resumming what's
    left is what makes this update leg by leg through the day, while
    every leg still anchors to the same authoritative day-total.

    `report_override` re-anchors the whole calculation to a report time
    other than the published one. The FDP clock starts at report, so a
    first leg planned later moves report later and every MOT in the day
    with it. See shifted_report_for_day() for when that is legitimate and
    when it very much is not.

    Falls back to the older block+ground-only estimate when day["duty"]
    is missing (stale data parsed before that field was captured) rather
    than returning nothing. table_b is pairing_engine's own FAR 117
    duty-period-length table — reused as-is, not reimplemented. Returns
    an HHMM string, or None if this day has no report/report_hbt or
    leg_index is out of range.
    """
    report = report_override or day.get("report")
    legs = day.get("legs") or []
    if not report or leg_index >= len(legs):
        return None
    fdp_end = fdp_end_for_day(day, report_override=report_override)
    if fdp_end is None:
        return None
    try:
        duty_total = float(day.get("duty"))
    except (TypeError, ValueError):
        duty_total = None

    if duty_total is not None:
        # Slack is what the day has spare: the FDP it is allowed, less the
        # duty it is planned to use. It is the same for every leg, so each
        # leg's MOT is simply its own scheduled departure plus that slack.
        #
        # Anchoring on the leg's DEPARTURE is the whole point. Counting
        # back from the FDP deadline by the duty still to come lands on the
        # latest REPORT, not the latest push — the report-to-departure
        # brief never got credited — so every MOT came out one brief early,
        # and on a day planned tight against its FDP that put MOT before
        # the departure it was supposed to be a limit on.
        # `report` is the override when one applies, so slack is measured
        # against the report actually in force — measuring it from the
        # published one while shifting the departure counted the delay
        # twice.
        slack = fdp_end - _hhmm_to_dec(report) - duty_total
        dep = legs[leg_index].get("dep_local")
        if dep:
            shift = 0.0
            if report_override and day.get("report"):
                shift = _hhmm_to_dec(report_override) - _hhmm_to_dec(day["report"])
            return _dec_to_hhmm(_hhmm_to_dec(dep) + shift + slack)
        # No published departure for this leg — fall through to the older
        # count-back rather than returning nothing.
        time_used = sum(
            float(l.get("block") or 0) + float(l.get("ground") or 0)
            for l in legs[:leg_index]
        )
        return _dec_to_hhmm(fdp_end - (duty_total - time_used))

    time_remaining = sum(
        float(l.get("block") or 0) + float(l.get("ground") or 0)
        for l in legs[leg_index:]
    )
    return _dec_to_hhmm(fdp_end - time_remaining)


def shifted_report_for_day(day, actual_first_dep, duty_started):
    """The day's report time re-anchored to a first leg that is now planned
    later than the pairing published it.

    The FDP clock starts at report, so if the day has not begun and the
    first departure moves an hour later, report moves with it and every
    MOT in that day moves an hour later too.

    It only works in that direction, and only before the day starts:

      * duty_started (FFD signed) -> None. The crew has already reported;
        the clock is running from the real report time and a later
        departure buys nothing. Shifting MOT later here would show more
        legal duty than actually exists, which is the one error worth
        being careful about.
      * an EARLIER departure -> None. Going early does not shorten the
        FDP a crew is entitled to.

    Returns an HHMM string, or None when the published report still
    stands."""
    if duty_started:
        return None
    legs = day.get("legs") or []
    report = day.get("report")
    if not legs or not report or not actual_first_dep:
        return None
    published = legs[0].get("dep_local")
    if not published:
        return None
    try:
        delay = _hhmm_to_dec(actual_first_dep) - _hhmm_to_dec(published)
    except (ValueError, TypeError):
        return None
    if delay <= 0:
        # A departure that reads earlier is far more likely to have crossed
        # midnight than to have genuinely moved up, and either way there is
        # nothing to extend.
        return None
    return _dec_to_hhmm(_hhmm_to_dec(report) + delay)


def fdp_end_for_day(day, report_override=None):
    """The day's own legal duty-period deadline (report + table_b's max
    FDP for this report hour/leg count) as decimal hours — the shared
    anchor mot_for_leg() counts backward from, also useful on its own for
    "how much FDP is left right now" style displays."""
    published = day.get("report")
    report = report_override or published
    report_hbt = day.get("report_hbt") or published
    legs = day.get("legs") or []
    if not report or not report_hbt:
        return None
    # The HBT half shifts by the same amount, so the table is read for the
    # hour the crew actually reports at — a report pushed from 0500 to 0800
    # can land in a different FDP band.
    if report_override and published:
        try:
            report_hbt = _dec_to_hhmm(
                _hhmm_to_dec(report_hbt) + (_hhmm_to_dec(report) - _hhmm_to_dec(published)))
        except (ValueError, TypeError):
            pass
    from pairing_engine import table_b
    return _hhmm_to_dec(report) + table_b(_hhmm_to_dec(report_hbt), len(legs))


def pbs_leg_to_fos_leg(meta, seq, day, leg, position):
    """Maps one parsed PBS leg onto the FOS 'leg' schema (server.py DEFAULT_LEG).

    Known gap: the report shows a day-of-week/day-of-month grid per sequence
    (which calendar dates this pattern actually operates on) that isn't parsed
    here, so dep_date/arr_date/date come back blank rather than a real date.
    Everything time-of-day, route, and duty/hotel related is real.
    """
    meta = meta or {}
    leg_index = next((i for i, l in enumerate(day.get("legs") or []) if l is leg), 0)
    return {
        "seq": seq["seq"],
        "date": "",
        "base": meta.get("base", ""),
        # The bid-pack's own "OPERATOR / FLEET" line — real AA PBS exports
        # give the carrier's 2-letter IATA code there (e.g. "AA"), not ICAO.
        "airline_iata": meta.get("operator", ""),
        "flight_number": leg["flight_number"],
        "origin": leg["origin"], "destination": leg["destination"],
        "dep_date": "", "arr_date": "",
        "sched_out": _fmt_time(leg["dep_local"]), "sched_in": _fmt_time(leg["arr_local"]),
        # The pairing's own published times, kept separate from sched_out/
        # sched_in so a later SimBrief merge (which writes its own
        # sched_out/sched_in) can never clobber them — see DEFAULT_LEG in
        # server.py for why this field exists.
        "pairing_sched_out": _fmt_time(leg["dep_local"]), "pairing_sched_in": _fmt_time(leg["arr_local"]),
        "est_out": _fmt_time(leg["dep_local"]), "est_in": _fmt_time(leg["arr_local"]),
        "dep_gate": "", "arr_gate": "",
        "fleet_type": leg["equipment"], "equipment_type": meta.get("fleet", ""),
        "tail_number": "", "tail_routing": "",
        "status": "", "customer_load": "",
        "position": position, "crew": [],
        "flight_time": leg["block"], "odl_time": "",
        "duty_time": day.get("duty") or "", "ground_time": leg.get("ground") or "",
        "mot": mot_for_leg(day, leg_index) or "",
        "tz_diff": "",
        "hotel_details": day.get("hotel") or "", "limo_details": "",
    }
