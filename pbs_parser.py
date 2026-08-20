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
RLS_RE = re.compile(
    r'^\s*RLS\s+(?P<in_>\d{4})/(?P<zin>\d{4})\s+'
    r'(?P<block>[\d.]+)\s+(?P<grnd>[\d.]+)\s+(?P<tpay>[\d.]+)\s+'
    r'(?P<duty>[\d.]+)(?:\s+(?P<fdp>[\d.]+))?\s+(?P<tafb>[\d.]+)'
)
HOTEL_RE = re.compile(r'^\s*(?P<sta>[A-Z]{3})\s+HOTEL\s+(?P<hotel>\S+)')
TTL_RE = re.compile(r'^TTL')

LEG_TOKEN_RE = re.compile(r'\S+')


def _parse_leg_line(line):
    """Token-walk a single flight-leg row. Returns None if it isn't one
    (release/report/hotel/TTL lines are handled by their own regexes)."""
    toks = LEG_TOKEN_RE.findall(line)
    if len(toks) < 7:
        return None
    if not re.match(r'^\d+$', toks[0]) or '/' not in toks[1]:
        return None  # not "DP D/A ..." — not a leg line

    i = 2
    if i >= len(toks):
        return None
    eq = toks[i]; i += 1

    flt = ''
    if i < len(toks) and re.match(r'^\d{2,5}$', toks[i]):
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

    block = toks[i] if i < len(toks) and re.match(r'^[\d.]+$', toks[i]) else ''
    if i < len(toks):
        i += 1
    ground = toks[i] if i < len(toks) and re.match(r'^[\d.]+$', toks[i]) else ''

    return {
        "equipment": eq, "flight_number": flt,
        "origin": sta1, "dep_local": dep_local, "dep_z": dep_z, "meal": meal,
        "destination": sta2, "arr_local": arr_local, "arr_z": arr_z,
        "block": block, "ground": ground,
    }


def parse_pbs(text):
    """Returns a list of sequence dicts:
    {seq, ops_per_period, positions, duty_days: [
        {duty_day, report, legs: [...], release, hotel, block, duty, tafb}
    ]}
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
                    "report": m.group("out"),
                    "legs": [],
                    "release": None, "hotel": None,
                    "block": None, "duty": None, "tafb": None,
                }
                seq["duty_days"].append(current_day)
                continue
            m = RLS_RE.match(line)
            if m and current_day is not None:
                current_day["release"] = m.group("in_")
                current_day["block"] = m.group("block")
                current_day["duty"] = m.group("duty")
                current_day["tafb"] = m.group("tafb")
                continue
            m = HOTEL_RE.match(line)
            if m and current_day is not None:
                current_day["hotel"] = f'{m.group("sta")} {m.group("hotel")}'
                continue
            if TTL_RE.match(line):
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


def pbs_leg_to_fos_leg(meta, seq, day, leg, position):
    """Maps one parsed PBS leg onto the FOS 'leg' schema (server.py DEFAULT_LEG).

    Known gap: the report shows a day-of-week/day-of-month grid per sequence
    (which calendar dates this pattern actually operates on) that isn't parsed
    here, so dep_date/arr_date/date come back blank rather than a real date.
    Everything time-of-day, route, and duty/hotel related is real.
    """
    meta = meta or {}
    return {
        "seq": seq["seq"],
        "date": "",
        "base": meta.get("base", ""),
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
        "tz_diff": "",
        "hotel_details": day.get("hotel") or "", "limo_details": "",
    }
