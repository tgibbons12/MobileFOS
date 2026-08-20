"""
fos_pages.py — FOS command-retrieved pages for the dispatch release
===================================================================

Renders the pages a dispatcher pulls out of FOS and staples to the
release, in the order they appear on the release index page:

    WBD*<flt>/<DDMMM>/<HHMM> <STA>   weight & balance data request
    NSC/<flt>/<DDMMM>/<STA>/<HHMM>   flight/crew status (crew list)
    FI<flt>/<DDMMM>/<HHMM> <STA>     flight information
    FIL<flt>/<DDMMM>/<HHMM> <STA>/APAX   flight information long / pax
    <fuel service record>            AA form 10012

Every builder takes the parsed SimBrief XML root plus a small context
dict and returns plain text with a leading ``[PAGEBREAK]`` so the PDF
writer starts each one on its own page.

Values FOS carries but SimBrief does not (employee numbers, seat/base,
sequence numbers, gate release quantities) are synthesised
deterministically from the flight identity so repeated runs of the same
flight produce the same page.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger(__name__)

PAGE_W  = 62

# FOS routing line carries the operator's two-letter code:
#   HDQ.HDQ-QLN.DS.A..MQ.DECS   ← MQ is Envoy, from the AA reference release
# SimBrief often omits <iata_airline>, so ICAO codes are mapped here.
AIRLINE_IATA = {
    # ── Northern Airways Group ──────────────────────────────────────────────
    'NAC': 'NC',    # Northern Airways (mainline)
    'PFT': 'P9',    # Pioneer
    'AAH': 'AQ',    # Aloha Airlines
    'SSX': 'L3',    # Cascadian
    'EMY': 'DC',    # Embassy          — provisional, confirm
    # 'AUR': '??',  # Aurora           — ICAO/IATA not assigned yet
    'RBD': 'YC',    # Cardinal         — Y-prefix per the RPA/YX model
    'RVF': '7H',    # Corvus
    # ── Real-world carriers ─────────────────────────────────────────────────
    'AAL': 'AA', 'ENY': 'MQ', 'HAL': 'HA', 'DAL': 'DL', 'UAL': 'UA',
    'SWA': 'WN', 'ASA': 'AS', 'JBU': 'B6', 'FFT': 'F9', 'NKS': 'NK',
    'SKW': 'OO', 'RPA': 'YX', 'QXE': 'QX', 'AWI': 'ZW', 'PDT': 'PT',
}

AIRLINE_NAME = {
    'NAC': 'NORTHERN AIRWAYS',
    'PFT': 'PIONEER',
    'AAH': 'ALOHA AIRLINES',
    'SSX': 'CASCADIAN',
    'EMY': 'EMBASSY',
    'RBD': 'CARDINAL',
    'RVF': 'CORVUS',
    'AAL': 'AMERICAN AIRLINES', 'ENY': 'ENVOY AIR', 'HAL': 'HAWAIIAN AIRLINES',
}


def airline_iata(icao, xml_iata=""):
    """Two-letter operator code: XML first, then the map, then a truncation."""
    if (xml_iata or "").strip():
        return xml_iata.strip().upper()[:2]
    icao = (icao or "").strip().upper()
    return AIRLINE_IATA.get(icao, icao[:2] or "XX")


def airline_name(icao, iata=""):
    icao = (icao or "").strip().upper()
    return AIRLINE_NAME.get(icao) or icao or (iata or "").upper()


def routing_line(iata):
    return f"HDQ.HDQ-QLN.DS.A..{iata}.DECS"
_MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
           'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _txt(root, path, default=""):
    try:
        v = root.findtext(path)
        return (v or default).strip()
    except Exception:
        return default


def _num(root, path, default=0.0):
    try:
        return float((root.findtext(path) or default) or default)
    except Exception:
        return float(default)


STAR_RUN = 18   # fixed star run either side of the command, as FOS prints it


def banner(cmd):
    """****************** WBD*3936/25SEP/1427 ORF ******************"""
    return "*" * STAR_RUN + f" {cmd} " + "*" * STAR_RUN + "\n"


def _utc(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _hhmm(dt):
    return dt.strftime("%H%M")


def _ddmmm(dt):
    return f"{dt.day:02d}{_MONTHS[dt.month - 1]}"


def _rng(seed_text):
    """Deterministic RNG seeded on the flight identity."""
    return random.Random(sum(ord(c) * (i + 1) for i, c in enumerate(seed_text)))


def build_context(root):
    """Collect the identity/time fields every FOS page shares."""
    airline = _txt(root, 'general/icao_airline') or _txt(root, 'general/iata_airline')
    iata_al = airline_iata(airline, _txt(root, 'general/iata_airline'))
    fltnum  = _txt(root, 'general/flight_number') or "0000"
    orig    = _txt(root, 'origin/iata_code') or _txt(root, 'origin/icao_code')
    dest    = _txt(root, 'destination/iata_code') or _txt(root, 'destination/icao_code')
    tail    = _txt(root, 'aircraft/reg') or _txt(root, 'general/reg') or ""
    fin     = _txt(root, 'aircraft/fin') or (tail[-3:] if tail else "")

    try:
        otz = int(float(_txt(root, 'times/orig_timezone', '0') or 0))
    except Exception:
        otz = 0
    try:
        dtz = int(float(_txt(root, 'times/dest_timezone', '0') or 0))
    except Exception:
        dtz = 0

    sched_out = _utc(_txt(root, 'times/sched_out') or _txt(root, 'general/sched_out'))
    sched_in  = _utc(_txt(root, 'times/sched_in')  or _txt(root, 'times/sched_on'))
    est_out   = _utc(_txt(root, 'times/est_out')   or _txt(root, 'times/sched_out'))
    est_in    = _utc(_txt(root, 'times/est_in')    or _txt(root, 'times/sched_in'))
    now       = datetime.now(timezone.utc)

    ctx = {
        'airline':   airline,
        'iata_al':   iata_al,
        'al_name':   airline_name(airline, iata_al),
        'flt':       fltnum,
        'flt_disp':  fltnum.lstrip('0') or fltnum,
        'orig':      orig,
        'dest':      dest,
        'orig_icao': _txt(root, 'origin/icao_code'),
        'dest_icao': _txt(root, 'destination/icao_code'),
        'tail':      tail,
        'fin':       fin,
        'otz':       otz,
        'dtz':       dtz,
        'sched_out': sched_out,
        'sched_in':  sched_in,
        'est_out':   est_out,
        'est_in':    est_in,
        'now':       now,
        'ddmmm':     _ddmmm(sched_out),
        'ddMmm':     f"{sched_out.day:02d}{_MONTHS[sched_out.month - 1].title()}",
        'dep_hhmm':  _hhmm(est_out),
        'dd':        f"{sched_out.day:02d}",
        'stamp':     f"{now.day:02d}/{_hhmm(now)}Z",
    }
    ctx['rng'] = _rng(f"{airline}{fltnum}{orig}{dest}{ctx['ddmmm']}")
    return ctx


def _head(cmd, ctx, routing=True):
    out = "\n[PAGEBREAK]\n"
    out += banner(cmd)
    if routing:
        out += routing_line(ctx.get('iata_al', 'XX')) + "\n"
    return out


# ---------------------------------------------------------------------------
# WBD — weight and balance data request
# ---------------------------------------------------------------------------
def build_wbd_page(root, ctx=None, gate=""):
    ctx = ctx or build_context(root)
    c   = ctx
    cmd = f"WBD*{c['flt_disp']}/{c['ddmmm']}/{c['dep_hhmm']} {c['orig']}"

    mtow = int(round(_num(root, 'weights/max_tow_struct')))
    mlw  = int(round(_num(root, 'weights/max_ldw')))
    burn = int(round(_num(root, 'fuel/enroute_burn')))
    taxi = int(round(_num(root, 'fuel/taxi')))
    gate = gate or (_txt(root, 'origin/gate') or "")

    out  = _head(cmd, c, routing=False)
    out += (f"WBIU{c['flt_disp']} {c['dd']}/{c['dep_hhmm']} "
            f"{c['orig']}-{c['dest']} {c['fin']}"
            f"{'':8}DPTR {c['dep_hhmm']} GATE {gate or '--':<4}{c['stamp']:>9}\n\n")
    out += f" MTOW<{mtow:06d} I   MLW<{mlw:05d}  S    FUEL PLN<STD    ENVELOPE<STD\n"
    out += (" RUNWAY < 1-<0       < 2-<0       < 3-<0"
            "      < 4-<0      < 5-<0\n")
    out += " FLAPS<         TEMP DEG C <                          BALLAST\n"
    out += " WINDS N/A    A/C N/A          RTG N/A         QNH N/A    NON<0\n"
    out += " CRT ADDRESS<          AGENT<                     PHONE<\n\n"
    out += f" FUBO {burn:<10} AEB 0           TXBO {taxi:<12} AUTO R/C NO\n"
    out += "\n MTOWS   LANDING    DISPATCHER    LOAD AGENT    T-WEIGHT   STRUCT\n"
    out += f"       L-0         D-0            A-0           T-0        {mtow}\n"
    out += " LIMITS P-0        R-0            X-0        WNDS 0    A/C 0   <<<<\n"
    return out


# ---------------------------------------------------------------------------
# NSC — flight/crew status (the crew list page)
# ---------------------------------------------------------------------------
def build_nsc_page(root, ctx=None, cpt="", fo="", gate="", arr_gate=""):
    ctx = ctx or build_context(root)
    c   = ctx
    rng = c['rng']
    cmd = f"NSC/{c['flt_disp']}/{c['ddmmm']}/{c['orig']}/{c['dep_hhmm']}"

    base      = c['dest']
    gate      = gate or (_txt(root, 'origin/gate') or "A2")
    arr_gate  = arr_gate or (_txt(root, 'destination/gate') or f"L{rng.randint(2, 30)}")
    seq       = f"{rng.randint(20000, 22999)}/{c['sched_out'].strftime('%y')}"

    def _abbrev(name, seat):
        """'BACHAND BA' style — surname plus initials."""
        if not _has_name(name):
            return _flight_attendant(rng)
        parts = [p for p in str(name).replace(',', ' ').split() if p]
        if not parts:
            return f"CREW {seat}"
        if len(parts) == 1:
            return parts[0].upper()
        surname = parts[-1].upper()
        initials = "".join(p[0] for p in parts[:-1]).upper()
        return f"{surname} {initials}"

    dep_sked = f"{_hhmm(c['sched_out'])}/{c['dd']}"
    dep_act  = f"{_hhmm(c['est_out'])}/{c['dd']}"
    arr_sked = f"{_hhmm(c['sched_in'])}/{c['dd']}"
    arr_act  = f"{_hhmm(c['est_in'])}/{c['dd']}"
    mot      = (c['est_out'] + timedelta(hours=11)).strftime("%H%M")

    out  = _head(cmd, c)
    out += (f"{c['flt_disp']}/{c['dd']} 1A {c['tail'][-4:] or c['fin']}   "
            f"FROM {c['flt_disp']}/{c['dd']} IN {_hhmm(c['sched_out'] - timedelta(hours=3))}/{c['dd']} "
            f"AS OF {c['ddmmm']}{c['sched_out'].strftime('%y')}/{_hhmm(c['now'])}\n")
    out += "\n"
    out += f"     STA SKD      LATEST       GATE      S/R\n"
    out += f"DEP {c['orig']} {dep_sked}  OUT {dep_act}  {gate}\n"
    out += f"ARV {c['dest']} {arr_sked}  IN  {arr_act}  {arr_gate:<6}{'':16}TCAS OPN\n"
    out += "\n"
    _hdr = list(" " * 70)
    for _lbl, _col in (("BASE", 0), ("SEAT", 5), ("SEN", 10), ("NAME", 15),
                       ("EMP", 38), ("SEQ", 46), ("MOT", 56), ("LMT", 62)):
        _hdr[_col:_col + len(_lbl)] = list(_lbl)
    out += "".join(_hdr).rstrip() + "\n"

    crew = [
        (base, 'CA', f"{rng.randint(1000, 3999)}", _abbrev(cpt, 'CA'), '', seq, mot, mot),
        (base, 'FO', f"{rng.randint(1000, 3999)}", _abbrev(fo,  'FO'), '', seq, mot, mot),
        (base, '01', f"{rng.randint(1000, 3999)}", f"{_flight_attendant(rng)}", '', seq, 'N/A', ''),
        (base, '02', f"{rng.randint(1000, 3999)}", f"{_flight_attendant(rng)}", '01', seq, 'N/A', ''),
    ]
    for b, seat, sen, name, pos, sq, mt, lmt in crew:
        emp = f"{rng.randint(100000, 999999)}"
        out += (f"{b:<4} {seat:<4} {sen:<4} {name:<20}{pos:<3}{emp:<8}"
                f"{sq:<10}{mt:<6}{lmt}").rstrip() + "\n"

    out += "\nDEADHEADING\n"
    out += "BASE SEAT NAME                  EMP    SEQ         MOT\n"
    out += "NONE\n"
    out += "END\n"
    return out


_FA_SURNAMES = ["MAGNUSON", "CARR", "OKONKWO", "DELANEY", "HOLBROOK", "REYES",
                "VANCE", "PRUITT", "ASHFORD", "LINDQVIST", "TRUJILLO", "BEAUMONT"]


def _has_name(value):
    """SimBrief returns '....... .' when the crew field is unset."""
    return any(ch.isalpha() for ch in str(value or ""))


def _flight_attendant(rng):
    return f"{rng.choice(_FA_SURNAMES)} {chr(rng.randint(65, 90))}{chr(rng.randint(65, 90))}"


def synthesize_crew(flight_number, dep_date, base, cpt="", fo=""):
    """Crew roster for the app's Overview→Crew popup: CA/FO (real SimBrief
    names when the leg has them) plus two flight attendants, which SimBrief's
    OFP never carries at all — no OFP field or other verified source in this
    app has a cabin-crew roster, so FA identity/EMP# here is synthesized the
    same deterministic way build_nsc_page() already does for the printed NSC
    page (seeded off the flight's own identity, so re-opening the popup for
    the same flight always shows the same names/numbers). Seeded separately
    from build_nsc_page's rng (that one needs a parsed SimBrief root this
    popup doesn't have), so the two won't show identical draws — cosmetic
    only, not a correctness issue for simulated cabin crew.
    """
    rng = _rng(f"{flight_number}{dep_date}{base}")

    def _name(real, seat):
        if _has_name(real):
            parts = [p for p in str(real).replace(',', ' ').split() if p]
            surname = parts[-1].upper()
            initials = "".join(p[0] for p in parts[:-1]).upper()
            return f"{surname} {initials}" if initials else surname
        return _flight_attendant(rng) if seat not in ("CA", "FO") else f"CREW {seat}"

    roster = []
    for seat, real in (("CA", cpt), ("FO", fo), ("01", ""), ("02", "")):
        roster.append({
            "seat": seat,
            "name": _name(real, seat),
            "dom": base,
            "emp": f"{rng.randint(100000, 999999)}",
        })
    return roster


# ---------------------------------------------------------------------------
# FI — flight information
# ---------------------------------------------------------------------------
def build_fi_page(root, ctx=None, cpt="", gate="", arr_gate=""):
    ctx = ctx or build_context(root)
    c   = ctx
    rng = c['rng']
    cmd = f"FI{c['flt_disp']}/{c['ddMmm']}/{c['dep_hhmm']} {c['orig']}"

    gate     = gate or (_txt(root, 'origin/gate') or "A2")
    arr_gate = arr_gate or (_txt(root, 'destination/gate') or f"L{rng.randint(2, 30)}")
    payload  = int(round(_num(root, 'weights/payload')))
    tow      = int(round(_num(root, 'weights/est_tow')))
    fuel_ob  = int(round(_num(root, 'fuel/plan_ramp')))
    pax      = int(_num(root, 'weights/pax_count'))
    cargo    = int(round(_num(root, 'weights/cargo')))
    airline  = c.get('iata_al') or "XX"

    sked_out = _hhmm(c['sched_out'])
    plan_out = _hhmm(c['est_out'])
    sked_in  = _hhmm(c['sched_in'])
    plan_in  = _hhmm(c['est_in'])
    # AIR is airborne time, TOTAL is block: TXO + AIR + TXI = TOTAL
    _TXO, _TXI = 14, 20                     # minutes, matching the reference form
    blk        = c['sched_in'] - c['sched_out']
    _blk_min   = max(0, int(blk.total_seconds() // 60))
    _air_min   = max(0, _blk_min - _TXO - _TXI)

    def _hm(mins):
        return f"{mins // 60}.{mins % 60:02d}"

    blk_h = _hm(_blk_min)

    def _cap(name):
        if not _has_name(name):
            return _flight_attendant(rng)
        parts = [p for p in str(name).replace(',', ' ').split() if p]
        if not parts:
            return "N/A"
        return f"{parts[-1].upper()} {''.join(p[0] for p in parts[:-1]).upper()}"

    out  = _head(cmd, c)
    out += (f"FLT {airline} {c['flt_disp']} {c['orig']} -{c['dest']}"
            f"     2 {c['ddmmm']}{'':14}{c['stamp']:>9}\n")
    out += "\n"
    out += f"01 {c['fin']}  NONE N/R        CAPT {_cap(cpt)} C1\n"
    out += f"GRN OBJ  {rng.randint(20, 45)} D        **ACARS**  **PRINTER**\n"
    out += "\n"
    out += f"PAYLOAD   T/O WGT   FUEL OB    GATES {gate}  /{arr_gate}\n"
    out += (f"D {payload:<7} P {tow:<7} P{fuel_ob}/{fuel_ob}  "
            f"AE{rng.randint(10, 99)} {rng.randint(1000000000, 9999999999)}\n")
    out += f"  {payload:<7} A         A {cargo}\n"
    out += "\n"
    out += "PERF        SKED PLAN ACTL PL  AC  PLAN ACTL DISP DELAYS\n"
    out += (f"  {-abs(rng.randint(2, 12)):>3}  {c['orig']}   {sked_out} {plan_out} {plan_out}-"
            f"        {fuel_ob} {fuel_ob}  AE{rng.randint(10, 99)}\n")
    out += (f"  {-abs(rng.randint(2, 25)):>3}  {c['dest']}   {sked_in} {plan_in} {plan_in}-"
            f"        {int(fuel_ob * 0.5)}  {int(fuel_ob * 0.45)}\n")
    out += f"TAXI IN EST {int(_num(root, 'times/taxi_in') or 0) // 60 or 14:03d}\n"
    out += f"DOOR OPEN MINS 00 SECS  {rng.randint(30, 59)}\n"
    out += "\nON-TIME ANALYSIS--\n"
    # Each row totals its own taxi times: TXO + AIR + TXI = TOTAL
    for _lbl, _txo, _txi, _air_adj in (("SKDBLK", 14, 20, 1),
                                       ("FLIPLN", 14, 20, 0),
                                       ("ACTUAL", 12, 10, 0),
                                       ("PREOFF", 14, 20, 0)):
        _a = max(0, _air_min + _air_adj)
        _t = _txo + _a + _txi
        out += (f"{_lbl} - TXO  {_txo}--AIR  {_hm(_a)}--TXI  {_txi}"
                f"----------TOTAL  {_hm(_t)}\n")
    out += f"\nPAX BOARDED {pax:<4}\n"
    out += "END\n"
    out += "** FKY OWNED FLIGHT **\n"
    return out


# ---------------------------------------------------------------------------
# FIL / APAX — flight information long, passenger counts
# ---------------------------------------------------------------------------
def build_fil_page(root, ctx=None):
    ctx = ctx or build_context(root)
    c   = ctx
    rng = c['rng']
    cmd = f"FIL{c['flt_disp']}/{c['ddMmm']}/{c['dep_hhmm']} {c['orig']}/APAX"

    pax  = int(_num(root, 'weights/pax_count'))
    cnx  = rng.randint(0, max(1, pax // 4))
    seq  = f"{rng.randint(4000, 4999)}/{c['ddmmm']}"

    out  = _head(cmd, c)
    out += f"FIL {c['flt_disp']}/{c['ddmmm']} {c['orig']}  APAX{'':28}{c['stamp']:>9}\n"
    out += "\nDEP/ARV **DEPARTURE** ** ARRIVAL **           CREW SOURCE\n"
    out += "STA STA ACT TIME SKED ACT TIME SKED PSG CNX A/C COCKPIT  F/A\n"
    out += (f"{c['orig']} {c['dest']} OFF {_hhmm(c['est_out'])} {_hhmm(c['sched_out'])} "
            f"IN  {_hhmm(c['est_in'])} {_hhmm(c['sched_in'])} {pax:>3} {cnx:>3} "
            f"{c['fin']} HOTEL   HOTEL\n")
    out += f"\nFIL {seq}\n"
    _nxt_out = c['sched_in'] + timedelta(hours=1, minutes=30)
    _nxt_in  = _nxt_out + timedelta(hours=1, minutes=54)
    out += (f"{c['dest']} ICT SKD {_hhmm(_nxt_out)} {_hhmm(_nxt_out)} "
            f"SKD {_hhmm(_nxt_in)} {_hhmm(_nxt_in)}   0   0 "
            f"{c['fin']} ORIG    ORIG\n")
    out += "\nEND OF DISPLAY\n"
    return out


# ---------------------------------------------------------------------------
# Jet fuel service record — form 10012
# ---------------------------------------------------------------------------
# Tank layout per fleet: [(tank label, nominal usable capacity in lbs), ...] in
# fill order. Wings fill first and stay balanced, then centre, then any aux or
# centre tank. Capacities are nominal — they only decide how the planned ramp
# fuel is distributed across the form, not what is actually loaded.
FUEL_TANKS = {
    # Airbus narrowbody — L/C/R plus additional centre tanks
    'A319': [('L', 13800), ('R', 13800), ('C', 14200)],
    'A320': [('L', 13800), ('R', 13800), ('C', 14200)],
    'A20N': [('L', 13800), ('R', 13800), ('C', 14200)],
    'A321': [('L', 13800), ('R', 13800), ('C', 14555), ('A1', 4149), ('A2', 4149)],
    'A21N': [('L', 13800), ('R', 13800), ('C', 14555), ('A1', 4149), ('A2', 4149)],
    # Airbus widebody
    'A339': [('L', 43000), ('R', 43000), ('C', 50000), ('TRIM', 10000)],
    'A332': [('L', 43000), ('R', 43000), ('C', 50000), ('TRIM', 10000)],
    # Boeing narrowbody — numbered main tanks
    'B738': [('1', 8630), ('2', 8630), ('CTR', 28800)],
    'B38M': [('1', 8630), ('2', 8630), ('CTR', 28800)],
    'B737': [('1', 8630), ('2', 8630), ('CTR', 28800)],
    'B752': [('1', 11400), ('2', 11400), ('CTR', 52000)],
    # Boeing widebody
    'B763': [('1', 24000), ('2', 24000), ('CTR', 43000)],
    'B772': [('L', 47900), ('R', 47900), ('C', 82000)],
    'B788': [('L', 41000), ('R', 41000), ('C', 40000)],
    # Regional — wing tanks only
    'E170': [('L', 9300), ('R', 9300)],
    'E75L': [('L', 9335), ('R', 9335)],
    'E75S': [('L', 9335), ('R', 9335)],
    'E175': [('L', 9335), ('R', 9335)],
    'E190': [('L', 13000), ('R', 13000)],
    'E195': [('L', 13000), ('R', 13000)],
    'E145': [('L', 4600), ('R', 4600)],
    'E135': [('L', 4600), ('R', 4600)],
    'CRJ7': [('L', 6000), ('R', 6000), ('CTR', 7000)],
    'CRJ9': [('L', 6000), ('R', 6000), ('CTR', 7000)],
    'DH8D': [('L', 5700), ('R', 5700)],
    'B712': [('L', 6800), ('R', 6800), ('CTR', 11000)],
}


def tank_layout(ac_type):
    """Tank list for a type, falling back to a generic L/C/R wing-centre set."""
    key = (ac_type or "").strip().upper()
    if key in FUEL_TANKS:
        return FUEL_TANKS[key]
    for prefix, tanks in FUEL_TANKS.items():
        if key.startswith(prefix[:3]):
            return tanks
    return [('L', 15000), ('R', 15000), ('C', 20000)]


def distribute_fuel(total_lbs, tanks):
    """
    Spread the planned ramp fuel over the tanks: wings first and symmetric,
    then the centre and any auxiliary tanks in order. Returns the tank list in
    form order (wings either side of centre, as the form prints them).
    """
    wings = [t for t in tanks if t[0] in ('L', 'R', '1', '2')]
    rest  = [t for t in tanks if t not in wings]
    qty   = {name: 0 for name, _ in tanks}

    remaining = max(0, int(total_lbs))
    if wings:
        per_wing = min(wings[0][1], remaining // len(wings))
        for name, _cap in wings:
            qty[name] = per_wing
        remaining -= per_wing * len(wings)
    for name, cap in rest:
        take = min(cap, remaining)
        qty[name] = take
        remaining -= take
    # Anything still left goes back into the wings, up to capacity
    if remaining > 0 and wings:
        extra = remaining // len(wings)
        for name, cap in wings:
            qty[name] = min(cap, qty[name] + extra)

    # Print order: first wing, centre-ish tanks, second wing, then the rest
    if len(wings) == 2 and rest:
        ordered = [wings[0][0], rest[0][0], wings[1][0]] + [n for n, _ in rest[1:]]
    else:
        ordered = [n for n, _ in wings] + [n for n, _ in rest]
    return [(n, qty[n]) for n in ordered]

def build_fuel_service_page(root, ctx=None, gate="", tank_split=None):
    """
    Fuel service record. Gate release quantities are the planned ramp fuel
    distributed across the tanks in the usual wing/centre fill order.
    """
    ctx = ctx or build_context(root)
    c   = ctx

    ramp    = int(round(_num(root, 'fuel/plan_ramp')))
    density = 6.72
    gate    = gate or (_txt(root, 'origin/gate') or "--")
    ac_type = _txt(root, 'aircraft/icao_code') or _txt(root, 'aircraft/name')

    if tank_split is None:
        tank_split = distribute_fuel(ramp, tank_layout(ac_type))

    total = sum(q for _, q in tank_split)

    out  = "\n[PAGEBREAK]\n"
    out += "[INDEX_ENTRY:Fuel Service Record]\n"
    out += (f"      {c.get('al_name') or c['airline']} JET FUEL SERVICE RECORD"
            f"{'':10}{_hhmm(c['now'])}Z\n")
    out += (f"FLT   {c['flt_disp']:<6} {c['dd']} {c['orig']} - {c['dest']}"
            f"    ACFT {c['fin']:<6}      EQUIP {ac_type}\n")
    out += (f"FUEL DENSITY  {density:.2f} FUEL GRADE ...... "
            f"DPTR TIME {c['dep_hhmm']}    GATE {gate}\n\n")
    out += "*" * 62 + "\n"
    out += "* REFUEL VALVE SELECTION POSITION INFORMATION ON REFUEL PANEL *\n"
    out += "* OPEN - MANUAL MODE, NORM - AUTO MODE, SHUT - CLOSED         *\n"
    out += "* /USED FOR MANUAL MODE/                                      *\n"
    out += "* UNLESS SYSTEM IS INOP, REFUEL VALVE POSITION SELECTION      *\n"
    out += "* SWITCH MUST BE IN NORM POSITION /AUTO MODE/                 *\n"
    out += "* " + "*" * 58 + " *\n"
    out += "* MAXIMUM DIFFERENCE OF FUEL LOAD BETWEEN MAIN WING TANKS IS  *\n"
    out += "* 1000 LBS.                                                   *\n"
    out += "*" * 62 + "\n\n"
    out += "        GAUGE READING    GAUGE READING    GATE RELEASE\n"
    out += "        BEFORE FUELING   AFTER FUELING    QTY. REQUIRED\n"
    out += "TANK        -LBS-            -LBS-            -LBS-      TANK\n\n"
    for name, qty in tank_split:
        out += f"  {name:<6}----------       ----------       {qty:>6}      {name}\n\n"
    out += f"TOTAL   ----------       ----------       {total:>6}      TOTAL\n\n"
    out += "CALCULATED FUEL ADDED\n"
    out += "-AFTER TOTAL LBS MINUS BEFORE TOTAL LBS-        -------- POUNDS\n\n"
    out += "DIVIDED BY ACTUAL DENSITY                       -------- GALLONS\n\n"
    out += "GALLONS ADDED FROM EQUIPMENT METER              -------- GALLONS\n\n"
    out += "ACTUAL DIFFERENCE                               -------- GALLONS\n\n"
    out += "ALLOWABLE DIFFERENCE                                 290 GALLONS\n\n"
    out += "*" * 62 + "\n"
    out += "* FUELER--NOTIFY AIRCRAFT MAINTENANCE BEFORE FLIGHT DEPARTURE *\n"
    out += "*     WHEN ALLOWABLE DIFFERENCE IS EXCEEDED AFTER FUELING     *\n"
    out += "*" * 62 + "\n\n"
    out += "FUEL CAP INSTALLED     ----------\n"
    out += "                        -INITIAL-\n\n"
    out += "FUEL PANEL DOOR CLOSED ----------\n"
    out += "                        -INITIAL-\n\n"
    out += "SIGNATURE AND EMP NO. --------------------  EQUIP NO. -------\n\n"
    out += "REMARKS\n"
    out += "MEL ITEMS\n\n"
    out += f"              DISTRIBUTION: ORIG - {c.get('iata_al') or c['airline']} OPS\n"
    out += "                      FORM NO. 10012 REV. 8/1/2016\n"
    return out
