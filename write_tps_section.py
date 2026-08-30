"""
write_tps_section.py  —  Unified takeoff performance sheet renderer
====================================================================
Produces the TPS (Takeoff Performance Sheet) block from a SimBrief
XML root.  Canonical source: replaces the inline
``write_takeoff_performance_string`` function that previously lived
inside both MASTERLOG.py and MASTERLOG_FOS.py.

Usage
-----
    from write_tps_section import write_takeoff_performance_string

The function signature accepts several optional keyword arguments
beyond the original MASTERLOG inline version:

    cg_percent        : float | None  — CG as % MAC; enables TOW CG /
                        STAB trim line in the thrust block (all fleets).
    fuel_change_lbs   : float | None  — Actual vs planned fuel delta
                        (lbs).  When |delta| > 2000 the function returns
                        a fuel-rejection message instead of TPS data.
    weight_restricted : bool          — When True, prepends a WEIGHT
                        RESTRICTED FLIGHT banner before the STA header.
    limiting_restriction : str        — Label for the banner
                        (e.g. "ZFW", "LDW", "MTOW-L").
    new_tps_warning   : bool          — Prepends a NEW TPS REQUIRED
                        advisory when True.
    tlr_scenario_active : bool        — Suppresses the headwind/tailwind
                        annotation line (TLR scenarios shift baseline).
    is_ejet           : bool          — When True, suppresses intersection
                        rows and intersection airport notes (E-jet ops
                        do not permit intersection takeoffs).
    flt_display       : str | None    — Flight number string for the FLT
                        preamble line (e.g. "0234 LAX-SFO").  When
                        provided, a FLT … line is written above the STA
                        block.

Notes
-----
* Uses ``LOG = logging.getLogger(__name__)`` — integrates with the
  host process's logging config automatically.
* Depends on: SPEEDOTHER, ENGINEFAILPROC, TRIMSETTING, runway_index.dat
  (same runtime requirements as before plus TRIMSETTING).
"""

from __future__ import annotations

import collections
import logging
import math
import math as _math
import re
import textwrap
import traceback
from datetime import datetime, timezone

from SPEEDOTHER import get_speed_other, get_reduced_thrust_n1, get_takeoff_thrust
from calm_wind_tlr import (atow_delta_lbs, interpolate_to_atow,
                           parse_calm_wind_tables)
from ENGINEFAILPROC import get_airport_specific_altitudes
from TRIMSETTING import get_trim_setting

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Takeoff performance — wind adjustment coefficients
# ---------------------------------------------------------------------------
_HDWND_BASE_RATE = 0.00287   # lbs-per-lbs-MTOW per knot of headwind
_TLWND_BASE_RATE = 0.00671   # lbs-per-lbs-MTOW per knot of tailwind
_AI_SUB_RATE     = 0.0015    # anti-ice climb penalty (fraction of TOW per 1000 lbs)

# ---------------------------------------------------------------------------
# Intersection grouping — mirrors MASTERLOG.py exactly
# ---------------------------------------------------------------------------
import os

_runway_index_cache = None


def load_runway_index():
    """Load runway_index.dat from script directory. Cached after first load."""
    global _runway_index_cache
    if _runway_index_cache is not None:
        return _runway_index_cache
    dat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runway_index.dat')
    index = {}
    if not os.path.exists(dat_path):
        _runway_index_cache = index
        return index
    with open(dat_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(';')
            if len(parts) < 3:
                continue
            icao    = parts[0].upper()
            rwy_raw = parts[1].upper()
            try:
                tora_m = float(parts[2])
            except ValueError:
                continue
            tora_ft = tora_m * 3.28084
            if '_' in rwy_raw:
                rwy_base, taxiway = rwy_raw.split('_', 1)
            else:
                continue
            key = (icao, rwy_base)
            index.setdefault(key, []).append({'taxiway': taxiway, 'tora_ft': tora_ft})
    _runway_index_cache = index
    return index


# ---------------------------------------------------------------------------
# Takeoff Analysis Advisories
# ---------------------------------------------------------------------------
# A TAA is a published, airport-specific advisory — most airports do not have
# one, so this table is deliberately sparse and a station with no entry gets
# no TAA block. Add entries as:
#
#   'KJFK': {'number': '18-071',
#            'lines': ['RW 13L/31R - UNGROOVED',
#                      'RWY 13L/31R - DATA BASED ON',
#                      '               SMOOTH RUNWAY']},
#
# An optional taa_index.dat in the script directory is merged over this table
# at load time, one advisory per line:  ICAO;NUMBER;LINE|LINE|LINE
AIRPORT_TAA = {}

# Rendering is off by default — the table and loader stay live so advisories
# can be populated now and switched on later. Set True to print the block.
SHOW_TAA = False

_taa_cache = None


def load_taa_index():
    """AIRPORT_TAA overlaid with taa_index.dat when that file exists."""
    global _taa_cache
    if _taa_cache is not None:
        return _taa_cache
    index = dict(AIRPORT_TAA)
    dat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taa_index.dat')
    if os.path.exists(dat_path):
        try:
            with open(dat_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(';')
                    if len(parts) < 2:
                        continue
                    icao = parts[0].strip().upper()
                    index[icao] = {
                        'number': parts[1].strip(),
                        'lines': [seg.strip() for seg in parts[2].split('|')]
                                 if len(parts) > 2 and parts[2].strip() else [],
                    }
        except Exception as e:
            LOG.debug(f"[TAA] index load failed: {e}")
    _taa_cache = index
    return index


_full_runway_cache = None


def load_full_runway_index():
    """
    Whole-runway geometry from runway_index.dat (the rows WITHOUT a taxiway
    suffix, which load_runway_index skips).

    Returns {ICAO: {RWY: {'tora_ft', 'elev', 'slope'}}} — used to fill the
    takeoff analysis out to five runways when the SimBrief TLR only analysed
    one or two.
    """
    global _full_runway_cache
    if _full_runway_cache is not None:
        return _full_runway_cache
    dat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runway_index.dat')
    index = {}
    if not os.path.exists(dat_path):
        _full_runway_cache = index
        return index
    with open(dat_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('ICAO;'):
                continue
            parts = line.split(';')
            if len(parts) < 3:
                continue
            rwy_raw = parts[1].upper()
            if '_' in rwy_raw:          # intersection row — handled elsewhere
                continue
            try:
                tora_ft = float(parts[2]) * 3.28084
            except ValueError:
                continue
            def _f(idx, default=0.0):
                try:
                    return float(parts[idx])
                except (ValueError, IndexError):
                    return default
            index.setdefault(parts[0].upper(), {})[rwy_raw] = {
                'tora_ft': tora_ft,
                'elev':    _f(6),
                'slope':   _f(7),
            }
    _full_runway_cache = index
    return index


def get_intersection_groups(icao, rwy_id, full_tora_ft, distance_reject_ft, index_data):
    """
    Return up to 3 intersection groups (X/Y/Z) for a runway.
    Algorithm mirrors MASTERLOG.py exactly.
    """
    if not index_data or full_tora_ft <= 0:
        return []
    rwy_base = rwy_id.upper()
    entries  = index_data.get((icao.upper(), rwy_base), [])
    if not entries:
        return []
    LOG.info(f"[INTXN] {icao} {rwy_id}: {len(entries)} entries, reject={distance_reject_ft:.0f}ft: "
             f"{[(e['taxiway'], int(e['tora_ft'])) for e in sorted(entries, key=lambda x: x['tora_ft'], reverse=True)]}")
    valid = [e for e in entries if e['tora_ft'] >= distance_reject_ft]
    if not valid:
        LOG.info(f"[INTXN] {icao} {rwy_id}: no entries clear reject distance — no intersections")
        return []

    band_width   = full_tora_ft * 0.10
    MAX_PER_BAND = 3
    SPLIT_GAP_FT = 400
    valid_sorted = sorted(valid, key=lambda e: e['tora_ft'], reverse=True)

    bands = []
    for entry in valid_sorted:
        placed = False
        for band in bands:
            if (band[0]['tora_ft'] - entry['tora_ft']) <= band_width and len(band) < MAX_PER_BAND:
                band.append(entry)
                placed = True
                break
        if not placed:
            bands.append([entry])

    final_bands = []
    for band in bands:
        if len(band) == MAX_PER_BAND:
            gap = band[-2]['tora_ft'] - band[-1]['tora_ft']
            if gap > SPLIT_GAP_FT:
                final_bands.append(band[:-1])
                final_bands.append([band[-1]])
                continue
        final_bands.append(band)
    bands = final_bands[:3]

    suffixes = ['X', 'Y', 'Z']
    groups   = []
    for i, band in enumerate(bands):
        most_restrictive = min(e['tora_ft'] for e in band)
        taxiways = [e['taxiway'] for e in sorted(band, key=lambda e: e['tora_ft'], reverse=True)]
        groups.append({
            'suffix':   suffixes[i],
            'id':       rwy_base + suffixes[i],
            'tora_ft':  most_restrictive,
            'taxiways': taxiways,
            'valid':    most_restrictive >= distance_reject_ft,
        })
    return groups


_INTXN_AVAILABLE = True

# ---------------------------------------------------------------------------
# safe_weight helper
# ---------------------------------------------------------------------------

def safe_weight(value):
    """Convert weight (lbs) to thousands with 1 decimal.
    Returns None on failure or zero so callers can distinguish 'no data'
    from a genuine 0-lb weight."""
    try:
        result = float(value) / 1000.0
        return result if result != 0.0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

def write_takeoff_performance_string(
    flight_info, valid_runways, anti_ice_on,
    runway_lines, airport_altitudes=None, max_elevation=0, icao_code="XXXX",
    xml_root=None, atis_text=None, field_condition_text=None,
    # --- new optional parameters ---
    cg_percent=None,
    fuel_change_lbs=None,
    weight_restricted=False,
    limiting_restriction="",
    new_tps_warning=False,
    tlr_scenario_active=False,
    is_ejet=False,
    flt_display=None,
):
    """
    Returns the takeoff performance section as a string, including TAKEOFF DATA,
    runway table, airport analysis data, and optional airport notes.

    Weights/fuel are divided by 1000 with 1 decimal, flex temp gets C, ALT gets ft.
    Emits ``[PAGEBREAK]\\n[TPS_START]\\n`` at the start so the caller's PDF
    split logic can isolate this block.

    When ``fuel_change_lbs`` is supplied and |delta| > 2000 lbs the function
    returns a fuel-rejection advisory string instead of the normal TPS output.
    """
    try:
        # Aircraft UI name mapping - add registrations here
        AIRCRAFT_UI_NAMES = {
            "N123NA": {"name": "A319 CFM", "engine": "CFM56-5B5"},
        }

        # Special airports requiring MAX-SPCL designation
        SPECIAL_AIRPORTS = {
            "KSNA", "MROC", "KEGE", "KJAC", "KGUC", "KDRO",
            "PAJN", "PAWG", "PAPG", "MHTG", "TNCM", "TIST", "KEYW", "KASE"
        }

        # Airbus aircraft detection
        AIRBUS_TYPES = {'A318', 'A319', 'A320', 'A20N', 'A321', 'A21N',
                        'A332', 'A333', 'A339', 'A346'}
        is_airbus = icao_code.upper() in AIRBUS_TYPES

        # ERJ detection — needed here for label block below
        ERJ_TYPES = {'E135', 'E140', 'E145', 'E45X'}
        _icao_norm = icao_code.upper().replace('-', '').replace(' ', '')
        is_erj = _icao_norm in ERJ_TYPES

        # Set labels based on aircraft type
        if is_airbus:
            flap_label = "CONF"
            ac_label   = "APU"
        elif is_erj:
            flap_label = "FLAP"
            ac_label   = ""       # no BLD column for ERJ
        else:
            flap_label = "FLAP"
            ac_label   = "BLD"

        output = ""

        # ===================================================================
        # E-JET FLEET DETECTION
        # ===================================================================
        EJET_ICAOS = {'E170', 'E175', 'E190', 'E195', 'E290', 'E295', 'E17X', 'E19X'}
        _ejet_icao = icao_code.upper().replace('-', '').replace(' ', '')
        _is_ejet = is_ejet or (_ejet_icao in EJET_ICAOS
                               or _ejet_icao.startswith('E1')
                               or _ejet_icao.startswith('E2'))

        # ===================================================================
        # FUEL VARIANCE GATE  (skip when TLR scenario shifts baseline)
        # ===================================================================
        if not tlr_scenario_active and fuel_change_lbs is not None:
            try:
                if abs(float(fuel_change_lbs)) > 2000:
                    LOG.warning(f"[FUEL-VAR] Delta {fuel_change_lbs:.0f} lbs > 2000 — issuing rejection")
                    return (
                        "\n[PAGEBREAK]\n[TPS_START]\n"
                        "**** THIS TPS DOES NOT SATISFY THE ****\n"
                        "*** REQUIREMENTS OF A LOAD CLOSEOUT ***\n\n"
                        "*** NOTIFICATION MESSAGE ***\n"
                        "TAKEOFF DATA REJECTED BY FMC, ACTUAL FUEL\n"
                        "ONBOARD DIFFERS FROM PLANNED AND EXCEEDS\n"
                        "TOLERANCES. REQUEST TAKEOFF DATA WHEN\n"
                        "FUELING IS COMPLETE\n"
                        "AUTOMATED FLT OPS MESSAGE\n"
                    )
            except (TypeError, ValueError):
                pass

        # ===================================================================
        # TRIM / CG SETUP
        # ===================================================================
        icaocode_for_trim = icao_code.upper().replace('-', '').replace(' ', '')
        try:
            trim_data  = get_trim_setting(icaocode_for_trim, cg_percent)
        except Exception:
            trim_data  = None
        cg_display = f"{cg_percent:.1f}" if cg_percent is not None else ""
        # ===================================================================
        # Parse field condition text for closed runways
        # Accepts lines like:  04L/22R CLOSED DRY   09C/27C OPEN DRY
        # Also accepts single-end format:  RWY 04L CLOSED
        _fc_closed_rwys = set()
        if field_condition_text:
            _FC_PAIR_RE   = re.compile(
                r'(\d{1,2}[LRC]?)/(\d{1,2}[LRC]?)\s+(CLOSED|OPEN)', re.IGNORECASE)
            _FC_SINGLE_RE = re.compile(
                r'\bRW?Y\s+(\d{1,2}[LRC]?)\s+CLOSED\b', re.IGNORECASE)
            for _m in _FC_PAIR_RE.finditer(field_condition_text):
                if _m.group(3).upper() == 'CLOSED':
                    _fc_closed_rwys.add(_m.group(1).upper())
                    _fc_closed_rwys.add(_m.group(2).upper())
            for _m in _FC_SINGLE_RE.finditer(field_condition_text):
                _fc_closed_rwys.add(_m.group(1).upper())
            LOG.info(f"[FC] Closed runways from field condition text: {_fc_closed_rwys}")

        # Harvest runway closures from SimBrief NOTAMs (Q-code MR / RWY CLSD)
        if xml_root is not None and not _fc_closed_rwys:
            _NOTAM_CLSD_RE = re.compile(
                r'RW?Y\s+(\d{1,2}[LRC]?)(?:/(\d{1,2}[LRC]?))?\s+CLSD\b', re.IGNORECASE)
            _NOTAM_MR_RE   = re.compile(r'Q\)\s*\w+/QMR\b', re.IGNORECASE)

            # Some closures are only in effect on a daily recurring schedule,
            # e.g. "DLY 0659-1330" -- that's not a continuous closure, so it
            # shouldn't blanket-exclude the runway for a flight departing
            # outside those Zulu hours. Resolve the scheduled departure time
            # so windowed NOTAMs can be checked against it.
            _DLY_WINDOW_RE = re.compile(r'\bDLY\s+(\d{4})\s*-\s*(\d{4})\b')
            _dep_min = None
            try:
                _sched_out_raw = (xml_root.findtext('times/sched_out') or '').strip()
                if _sched_out_raw:
                    _dep_dt  = datetime.fromtimestamp(int(_sched_out_raw), tz=timezone.utc)
                    _dep_min = _dep_dt.hour * 60 + _dep_dt.minute
            except Exception:
                _dep_min = None

            def _in_daily_window(dep_min, start_hhmm, end_hhmm):
                """True if dep_min (0-1439, Zulu) falls inside a DLY
                HHMM-HHMM window. Handles windows that wrap past midnight
                (e.g. 2200-0600)."""
                _to_min = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[2:])
                start_m, end_m = _to_min(start_hhmm), _to_min(end_hhmm)
                if start_m <= end_m:
                    return start_m <= dep_min <= end_m
                return dep_min >= start_m or dep_min <= end_m

            for _n in xml_root.findall('.//notam'):
                _ntxt = (_n.findtext('notam_text') or '').upper()
                if 'CLSD' not in _ntxt:
                    continue

                _dly_m = _DLY_WINDOW_RE.search(_ntxt)
                if _dly_m and _dep_min is not None:
                    if not _in_daily_window(_dep_min, _dly_m.group(1), _dly_m.group(2)):
                        LOG.info(f"[FC] NOTAM DLY {_dly_m.group(1)}-{_dly_m.group(2)}Z "
                                 f"doesn't cover dep {_dep_min // 60:02d}{_dep_min % 60:02d}Z "
                                 f"-- closure not applied")
                        continue

                qraw  = (_n.findtext('notam_qcode') or '').upper()
                is_mr = qraw.startswith('QMR') or qraw == 'MR' or bool(_NOTAM_MR_RE.search(_ntxt))
                for _cm in _NOTAM_CLSD_RE.finditer(_ntxt):
                    if is_mr or 'RWY' in _ntxt:
                        _fc_closed_rwys.add(_cm.group(1).upper())
                        if _cm.group(2):
                            _fc_closed_rwys.add(_cm.group(2).upper())
            if _fc_closed_rwys:
                LOG.info(f"[FC] Closed runways from NOTAMs: {_fc_closed_rwys}")

        # Parse ATIS for wind and active departure runway
        _atis_wind_dir  = None
        _atis_wind_spd  = None
        _atis_wind_gust = None
        _atis_dep_rwy   = None
        _atis_dep_rwys  = set()

        _atis_src = atis_text or ''
        if not _atis_src and xml_root is not None:
            _atis_src = (xml_root.findtext('weather/orig_atis') or '')
        if _atis_src:
            _wm = re.search(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT', _atis_src, re.IGNORECASE)
            if _wm and _wm.group(1).upper() != 'VRB':
                try:
                    _atis_wind_dir  = int(_wm.group(1))
                    _atis_wind_spd  = int(_wm.group(2))
                    _atis_wind_gust = int(_wm.group(3)) if _wm.group(3) else None
                except ValueError:
                    pass
            elif _wm:
                _atis_wind_spd = int(_wm.group(2)) if _wm.group(2) else 0
            _DEP_RE = re.compile(
                r'(?:DEPART(?:ING|URE)S?)(?:\s+(?:AND\s+)?)?(?:RWYS?|RUNWAYS?)?\s+'
                r'((?:\d{1,2}[LRC]?\s*(?:,|AND)?\s*)+)',
                re.IGNORECASE)
            _RWY_TOK_RE = re.compile(r'\d{1,2}[LRC]?')
            _dm = _DEP_RE.search(_atis_src)
            if _dm:
                _atis_dep_rwys = set(_RWY_TOK_RE.findall(_dm.group(1).upper()))
                if _atis_dep_rwys:
                    _atis_dep_rwy = sorted(_atis_dep_rwys)[0]  # kept for back-compat logging/callers
                    LOG.info(f"[ATIS] Active departure runway(s) from ATIS: {sorted(_atis_dep_rwys)}")
            LOG.info(f"[ATIS] Wind: dir={_atis_wind_dir} spd={_atis_wind_spd} gust={_atis_wind_gust}")

        def _rwy_base_num(rwy_id):
            """Strip L/R/C suffix so a bare ATIS callout ('RWY 23') can be
            compared against a specific parallel identifier ('23L', '23R')."""
            if not rwy_id:
                return None
            _m = re.match(r'^(\d{1,2})', rwy_id.upper().strip())
            return _m.group(1) if _m else None

        def _atis_confirms_rwy(rwy_id):
            """True if rwy_id (exact, e.g. '05L') or its bare base number is
            among the runway(s) ATIS actually called active. Handles both a
            suffix-less single callout ('RWY 23') and an explicit list
            ('RUNWAYS 05R, 05L')."""
            if not rwy_id or not _atis_dep_rwys:
                return False
            _rid = rwy_id.upper().strip()
            if _rid in _atis_dep_rwys:
                return True
            _base = _rwy_base_num(_rid)
            return any(_base == _rwy_base_num(_a) for _a in _atis_dep_rwys)

        # ATIS is the live, authoritative field state — it outranks NOTAM/field
        # condition text parsing, which can be stale, ambiguous, or mis-parsed.
        # Any runway ATIS actually calls active is treated as open, period,
        # even if a NOTAM regex matched it as closed.
        if _fc_closed_rwys and _atis_dep_rwys:
            _atis_overridden = {r for r in _fc_closed_rwys if _atis_confirms_rwy(r)}
            if _atis_overridden:
                _fc_closed_rwys -= _atis_overridden
                LOG.warning(f"[FC] ATIS overrides NOTAM/field-condition closure for "
                            f"{sorted(_atis_overridden)} (ATIS active={sorted(_atis_dep_rwys)}) "
                            f"— treating as OPEN")

        def _wind_components(rwy_id, wind_dir_deg, wind_spd_kt):
            if wind_dir_deg is None or wind_spd_kt is None:
                return None, None
            try:
                _hdg_m = re.search(r'(\d{1,2})', rwy_id)
                if not _hdg_m:
                    return None, None
                hdg_deg = int(_hdg_m.group(1)) * 10
                angle   = math.radians((wind_dir_deg - hdg_deg + 180) % 360 - 180)
                hwc     = round(wind_spd_kt * math.cos(angle))
                xwc     = round(abs(wind_spd_kt * math.sin(angle)))
                return hwc, xwc
            except Exception:
                return None, None

        # ===================================================================
        # AIRCRAFT TYPE DETECTION
        # ===================================================================
        icaocode      = icao_code.upper().replace('-', '').replace(' ', '')
        is_737_ng     = icaocode in ['B736', 'B737', 'B738', 'B739']
        is_737_max    = icaocode == 'B38M'
        is_boeing_737 = is_737_ng or is_737_max
        is_md8x       = icaocode.startswith('MD8')
        # is_erj already set above from icao_code; icaocode is the same normalised form

        # Extract and format basic flight data
        if valid_runways:
            first_runway  = valid_runways[0]
            aircraft_reg  = first_runway.get('fin', 'N/A')

            if aircraft_reg in AIRCRAFT_UI_NAMES:
                aircraft_display = AIRCRAFT_UI_NAMES[aircraft_reg]["name"]
                engine_type      = AIRCRAFT_UI_NAMES[aircraft_reg]["engine"]
                LOG.debug(f"[DBG: Using custom name for {aircraft_reg}: {aircraft_display}")
            else:
                aircraft_display = first_runway.get('aircraft', 'UNKNOWN')
                engine_type      = first_runway.get('engine', 'UNKNOWN')
                LOG.debug(f"[DBG: Aircraft {aircraft_reg} not in mapping, using XML: {aircraft_display}")

            sta      = (xml_root.findtext('origin/iata_code') or first_runway.get('airport', 'ERR')).strip().upper() \
                       if xml_root is not None else first_runway.get('airport', 'ERR')
            pres     = first_runway.get('qnh', 'ERR')
            alt      = first_runway.get('elevation', 'ERR')
            flt_dte  = first_runway.get('flight_number', 'ERR')
            airpl    = first_runway.get('fin', 'ERR')
            dte_time = first_runway.get('dte_time', 'ERR')
            surface  = first_runway.get('surface_condition', 'ERR').upper()
            temp     = first_runway.get('temp', 'ERR')

            est_tow = safe_weight(first_runway.get('est_tow', 0))

            try:
                _struct_mtow = safe_weight(float(xml_root.findtext('weights/max_tow_struct', '0') or 0))
            except Exception:
                _struct_mtow = None

            if isinstance(est_tow, float):
                # ATOW sits +1000 / +2000 / +3000 above PTOW depending on
                # aircraft size, not a flat +2000.
                _atow_delta = atow_delta_lbs(
                    _struct_mtow * 1000.0 if isinstance(_struct_mtow, float) else 0)
                atow = est_tow + (_atow_delta / 1000.0)
                if isinstance(_struct_mtow, float) and _struct_mtow > 0:
                    atow = min(atow, _struct_mtow)
            else:
                atow = est_tow

            est_zfw       = safe_weight(first_runway.get('est_zfw', 0))
            plan_takeoff  = safe_weight(first_runway.get('fuel', 0))
            taxi_fuel     = safe_weight(first_runway.get('taxi_fuel', 0))
            weight_lbs    = first_runway.get('est_tow', 0)
        else:
            engine_type = aircraft_display = sta = pres = alt = 'ERR'
            flt_dte = airpl = dte_time = surface = temp = 'ERR'
            est_tow = atow = est_zfw = plan_takeoff = taxi_fuel = 'ERR'
            weight_lbs = 0

        # ===================================================================
        # ALTITUDE & BLEED ADJUSTMENT
        # ===================================================================
        alt_val = 0
        if alt != 'ERR':
            try:
                alt_val = float(alt)
            except (ValueError, TypeError):
                alt_val = 0

        if alt_val <= 8000:
            pack_off_adj = 0.8
        elif alt_val <= 9000:
            pack_off_adj = 0.9
        else:
            pack_off_adj = 1.0

        # ===================================================================
        # THRUST TABLE (Boeing 737)
        # ===================================================================
        THRUST_TABLE = {
            "B736":     {"TO": 22, "TO1": 20, "TO2": 18},
            "B737":     {"TO": 24, "TO1": 22, "TO2": 20},
            "B738":     {"TO": 26, "TO1": 24, "TO2": 22},
            "B738_SFP": {"TO-B": 27, "BUMP": 27},
            "B739":     {"TO": 27, "TO1": 25, "TO2": 23},
            "B38M":     {"TO": 26, "TO1": 24, "TO2": 22},
        }

        rwy          = valid_runways[0] if valid_runways else {}
        derate_label = rwy.get('thr', '').upper().strip()

        # SFP detection: B738 with "SFP" in engine/aircraft name
        _ac_name     = (first_runway.get('aircraft', '') if valid_runways else '')
        _engine_name = (first_runway.get('engine', '')   if valid_runways else '')
        is_sfp = (icaocode == 'B738') and (
            'SFP' in _ac_name.upper() or 'SFP' in _engine_name.upper()
        )
        sfp_bump = False
        if is_sfp:
            sfp_bump = derate_label in ('TO-B', 'BUMP')
            lookup_icaocode = 'B738_SFP' if sfp_bump else icaocode
        else:
            lookup_icaocode = icaocode

        effective_thrust = None
        thrust_label     = "N/A"

        if is_boeing_737 and lookup_icaocode in THRUST_TABLE:
            key = derate_label.replace("D-", "", 1) if derate_label.startswith("D-") else derate_label
            effective_thrust = THRUST_TABLE[lookup_icaocode].get(key) \
                            or list(THRUST_TABLE[lookup_icaocode].values())[0]
            if sfp_bump:
                thrust_label = "27K BUMP"
            elif icaocode == "B738":
                thrust_label = f"{effective_thrust}K" if effective_thrust is not None else key
            else:
                thrust_label = key or "TO"

        # ===================================================================
        # GLOBAL THRUST/SPEED DATA
        # ===================================================================
        n1_pack_on    = "XXX"
        n1_pack_off   = "XXX"
        epr_max       = "XXX"
        speed_data_dict = None

        # ===================================================================
        # TAKEOFF DATA PAGE HEADER
        # ===================================================================
        output += "\n[PAGEBREAK]\n[TPS_START]\n"

        # ===================================================================
        # NEW TPS WARNING  (prepended before everything else)
        # ===================================================================
        if new_tps_warning:
            output += "**** NEW TPS REQUIRED ****\n\n"

        # ===================================================================
        # WEIGHT RESTRICTED FLIGHT BANNER
        # ===================================================================
        if weight_restricted:
            _box = "*" * 39 + "\n"
            def _boxline(text):
                pad_total = 37 - len(text)
                pad_left  = _math.ceil(pad_total / 2)
                pad_right = pad_total - pad_left
                return f"*{' ' * pad_left}{text}{' ' * pad_right}*\n"
            output += _box
            output += _boxline("****** WEIGHT RESTRICTED FLIGHT *****")
            if limiting_restriction:
                output += _boxline(f"LIMITING RESTRICTION -- {limiting_restriction}")
            output += _boxline("PLEASE UPDATE ACTUAL FOB IMMEDIATELY")
            output += _boxline("AFTER FUELING VIA ACARS")
            output += _boxline("OR CONTACT LOAD AGENT")
            output += _box + "\n"

        # ===================================================================
        # FLT PREAMBLE LINE  (when caller supplies flight/route string)
        # ===================================================================
        if flt_display:
            output += f"FLT {flt_display}\n\n"
            output += "**** THIS TPS DOES NOT SATISFY THE ****\n"
            output += "*** REQUIREMENTS OF A LOAD CLOSEOUT ***\n\n"

        # ===================================================================
        # STA HEADER
        # ===================================================================
        # Use pressure altitude when QNH is available; fall back to field elev.
        try:
            _qnh = float(xml_root.findtext('origin/qnh', '29.92') or '29.92') \
                   if xml_root is not None else 29.92
            _alt_raw = float(alt) if alt not in ('ERR', '', None) else 0.0
            _presalt = int(_alt_raw + (29.92 - _qnh) * 1000)
            alt_disp = str(_presalt)
        except Exception:
            try:
                alt_disp = str(int(float(alt))) if alt not in ('ERR', '') else alt
            except Exception:
                alt_disp = str(alt)

        output += f"{'STA':<6} {'PRES ALT':<11} {'FLT':<8} {'AIRPL':<9} {'DTE/TIME':<10}\n"
        output += f"{sta:<6} {alt_disp:<10}  {flt_dte:<8} {airpl:<9} {dte_time:<10}\n\n"

        if is_boeing_737 and thrust_label and thrust_label != "N/A":
            output += f"*** {engine_type} {thrust_label} {surface} ***\n\n"
        else:
            output += f"*** {engine_type} {surface} ***\n\n"

        output += f"{'TEMP':<6} {'PTOW':>6} {'ATOW':>6} {'ZFW':>6} {'FUEL':>7} {'TXI FUEL':>8}\n"

        est_tow_str      = f"{est_tow:.1f}"      if isinstance(est_tow, float)      else 'ERR'
        est_zfw_str      = f"{est_zfw:.1f}"      if isinstance(est_zfw, float)      else 'ERR'
        plan_takeoff_str = f"{plan_takeoff:.1f}" if isinstance(plan_takeoff, float) else 'ERR'
        taxi_fuel_str    = f"{taxi_fuel:.1f}"    if isinstance(taxi_fuel, float)    else 'ERR'

        # ATOW cap: clamp down to first runway MTOW if it's binding
        _atow_capped = atow
        if isinstance(atow, float) and valid_runways:
            try:
                _rwy0_mtow = float(valid_runways[0].get('max_weight', 0))
                if _rwy0_mtow > 0 and _rwy0_mtow < _atow_capped:
                    LOG.debug(f"[ATOW] MTOW {_rwy0_mtow} < ATOW {_atow_capped} → clamping")
                    _atow_capped = _rwy0_mtow
            except Exception:
                pass
        atow_str = f"{_atow_capped:.1f}" if isinstance(_atow_capped, float) else 'ERR'

        output += f"{temp}C    {est_tow_str:>7}{atow_str:>7} {est_zfw_str:>7} "
        output += f"{plan_takeoff_str + 'P':>6}{taxi_fuel_str:>5}\n\n"

        # ===================================================================
        # THRUST / V-SPEED SECTION
        # ===================================================================
        output += "************* THRUST / V-SPEED ****************************\n\n"
        if anti_ice_on:
            output += "  *****************\n"
            output += "   * ANTI-ICE ON *\n"
            output += "  *****************\n\n"

        LOG.debug(f"[DBG] ICAO from parameter: {icao_code}")
        LOG.debug(f"[DBG] Final ICAO for speed lookup: {icaocode}")
        LOG.debug(f"[DBG] Weight for speed lookup: {weight_lbs} lbs")

        if is_boeing_737:
            speed_data_dict = get_speed_other(lookup_icaocode, oat=temp, altitude=alt)
            LOG.debug(f"[DBG] N1 lookup with OAT={temp}, ALT={alt}: {speed_data_dict}")
            n1_pack_on  = speed_data_dict.get('n1', 'XXX') if speed_data_dict else "XXX"
            # BLD OFF: read directly from speed dict (matches TAKEOFF_PERF behaviour)
            n1_pack_off = speed_data_dict.get('n1_pack_off', 'XXX') if speed_data_dict else "XXX"
            if n1_pack_off == "XXX" and n1_pack_on != "XXX":
                # Fallback: derive from pack-on value (subtract pack_off_adj)
                try:
                    n1_pack_off = round(float(n1_pack_on) - pack_off_adj, 1)
                except (ValueError, TypeError):
                    n1_pack_off = "XXX"
        elif is_erj:
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            LOG.debug(f"[DBG] ERJ VFS lookup weight={weight_lbs}: {speed_data_dict}")
        elif is_md8x:
            epr_max_data = get_speed_other(icaocode, oat=temp, altitude=alt_val)
            if epr_max_data and 'epr' in epr_max_data:
                epr_max = epr_max_data['epr']
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            LOG.debug(f"[DBG] MD83 EPR/speed lookup: {speed_data_dict}")
        else:
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            LOG.debug(f"[DBG] Custom speed data lookup result: {speed_data_dict}")

        # ------------------------------------------------------------------
        # Per-fleet thrust / V-speed block (with CG / STAB trim)
        # ------------------------------------------------------------------
        if is_md8x and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            # MD-8x: CG comes from weight-based speed lookup; epr_packs_off = epr_max + 0.02
            _md_cg = speed_data_dict.get('cg', cg_display or 'XX.X')
            vsr_val = speed_data_dict['speed'].get('VsR', 'XXX')
            vmm_val = speed_data_dict['speed'].get('VMM', 'XXX')
            epr_max_str = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)
            if trim_data:
                trim_display = trim_data.get('trim', 'X.X')
                output += f"         *MAX* EPR    TOW CG  STAB\n"
                output += f"      A/C ON  {epr_max_str}    {_md_cg:<6}     {trim_display}\n"
            else:
                output += f"         *MAX* EPR     TOW CG\n"
                output += f"      A/C ON  {epr_max_str}    {_md_cg}\n"
            try:
                epr_packs_off = round(float(epr_max) + 0.02, 2)
                output += f"      A/C OFF {epr_packs_off:.2f}\n\n"
            except Exception:
                output += f"      A/C OFF XXX\n\n"
            output += f"      TOW CG  O/RET    MM\n"
            output += f"       {_md_cg:<6} {vsr_val:<7} {vmm_val}\n\n"

        elif icaocode in ['A318', 'A319', 'A320', 'A321', 'A20N', 'A21N'] \
                and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            # Airbus: CG from cg_percent; trim from TRIMSETTING
            # Max takeoff thrust block, mirroring the 737's *MAX* N1 and the
            # MD-8x's *MAX* EPR. Which parameter appears is a property of the
            # engine (IAE V2500 is EPR-rated, CFM/LEAP/PW are N1-rated), so
            # it's driven off the OFP's engine string rather than the type.
            # Omitted entirely when there's no grid for that engine — an
            # absent block is honest, a substituted number is not.
            _ab_thrust = get_takeoff_thrust(icaocode, engine_type, temp, alt_val,
                                            packs_off=False)
            if _ab_thrust:
                _tp = _ab_thrust['param']
                _tv = (f"{_ab_thrust['value']:.2f}" if _tp == 'EPR'
                       else f"{_ab_thrust['value']:.1f}")
                _ab_thrust_off = get_takeoff_thrust(icaocode, engine_type, temp, alt_val,
                                                    packs_off=True)
                output += f"         *MAX* {_tp}\n"
                output += f"      A/C ON  {_tv}\n"
                if _ab_thrust_off and _ab_thrust_off['value'] != _ab_thrust['value']:
                    _tvo = (f"{_ab_thrust_off['value']:.2f}" if _tp == 'EPR'
                            else f"{_ab_thrust_off['value']:.1f}")
                    output += f"      A/C OFF {_tvo}\n"
                output += "\n"
            _ab_cg = cg_display or speed_data_dict.get('cg', 'XX.X')
            _ab_trim = trim_data.get('trim', 'X.X') if trim_data else \
                       speed_data_dict.get('trim', 'X.X')
            if _ab_trim and _ab_trim != 'X.X':
                output += f"      TOW CG       STAB\n"
                output += f"       {_ab_cg:<10} {_ab_trim}\n"
            else:
                output += f"      TOW CG\n"
                output += f"       {_ab_cg}\n"
            f_val  = speed_data_dict['speed'].get('F', 'XXX')
            s_val  = speed_data_dict['speed'].get('S', 'XXX')
            gd_val = speed_data_dict['speed'].get('GRN DOT', 'XXX')
            output += f"       F     S    GRN DOT\n"
            output += f"      {f_val:<5} {s_val:<5} {gd_val:^8}\n\n"

        elif speed_data_dict and 'n1' in speed_data_dict:
            # Boeing 737: *MAX* N1 block with BLD ON / BLD OFF + CG / STAB
            if trim_data:
                output += f"*MAX*    N1      TOW CG    STAB\n"
                output += f"BLD ON   {n1_pack_on:<6}   {cg_display:<6}  {trim_data.get('trim', 'X.X')}\n"
            else:
                output += f"*MAX*    N1      TOW CG\n"
                output += f"BLD ON   {n1_pack_on:<6}   {cg_display}\n"
            output += f"BLD OFF  {n1_pack_off}\n\n"

        elif not is_erj and speed_data_dict and 'name' in speed_data_dict and 'speed' in speed_data_dict:
            # Generic / other fleet
            if trim_data:
                output += (f"      {speed_data_dict['name']} {speed_data_dict['speed']}"
                           f"   TOW CG  {cg_display}   STAB {trim_data.get('trim', 'X.X')}\n\n")
            else:
                output += f"      {speed_data_dict['name']} {speed_data_dict['speed']}"
                if cg_display:
                    output += f"   TOW CG  {cg_display}"
                output += "\n\n"

        # ===================================================================
        # RUNWAY TABLE WITH PER-RUNWAY N1/EPR
        # ===================================================================
        if is_md8x:
            thr_column_label = "EPR"
        elif is_boeing_737:
            thr_column_label = "N1"
        elif is_erj:
            thr_column_label = "THR"
        else:
            thr_column_label = "THR"

        # ===================================================================
        # WIND ADJUSTMENT
        # ===================================================================
        # The TPS is computed at calm wind unless a wind actually has to be
        # entered, which happens when either:
        #   * performance dictates it — the primary runway cannot take the
        #     planned weight without the headwind credit, or
        #   * the primary runway has more than 5 kt of tailwind.
        # Once a wind is entered, TPS only computes the primary runway and any
        # parallel runway; everything else carries the WIND ADJM message. The
        # airport analysis section stays no-wind either way — HDWND ADD / KT
        # and TLWND SUB / KT are the corrections for manual computation.
        def _wind_rate_lbs(r):
            """
            Headwind-add and tailwind-subtract rates in lbs per knot for a
            runway — the same figures printed as HDWND ADD / KT and
            TLWND SUB / KT in the airport analysis section.
            """
            try:
                mw = safe_weight(r.get('max_weight', 0))
                if not isinstance(mw, float) or mw <= 0:
                    return 0.0, 0.0
                mtow_lbs  = mw * 1000.0
                atow_l    = (atow * 1000.0) if isinstance(atow, float) else 0.0
                wt_margin = max(0.0, mtow_lbs - atow_l) / mtow_lbs
                _len      = float(r.get('length', 0) or 0)
                dist_ratio = (float(r.get('asdr', 0) or 0) / _len) if _len > 0 else 0.0
                combined  = (wt_margin * dist_ratio) ** 0.5
                hw = max(100, round(mtow_lbs * _HDWND_BASE_RATE
                                    * (0.60 + 0.40 * min(combined * 3.0, 1.0))))
                tw = 0 if dist_ratio >= 0.30 else \
                     max(200, round(mtow_lbs * _TLWND_BASE_RATE
                                    * (1.40 - 0.40 * min(combined * 3.0, 1.0))))
                return float(hw), float(tw)
            except Exception:
                return 0.0, 0.0

        def _no_wind_lbs(r):
            """
            SimBrief's max weight with its wind allowance taken back out.

            SimBrief computes each runway against the forecast wind, so a
            headwind runway arrives already credited. The airport analysis
            section is defined as no-wind data, and the TPS itself plans calm
            unless a wind is entered, so the credit has to be removed rather
            than merely relabelled.
            """
            mw = safe_weight(r.get('max_weight', 0))
            if not isinstance(mw, float) or mw <= 0:
                return None
            lbs = mw * 1000.0
            try:
                hd = int(round(float(r.get('HD'))))
            except (TypeError, ValueError):
                return lbs
            # Only the headwind credit is removed. Adding a tailwind penalty
            # back would use the synthetic TLWND SUB rate, which on an 11 kt
            # tailwind pushes the weight past the structural limit — an
            # invented number in the unconservative direction. A tailwind
            # runway keeps SimBrief's figure, which already carries the
            # penalty and is the safe side to be on.
            if hd > 0:
                hw, _tw = _wind_rate_lbs(r)
                lbs -= hd * hw
            return max(0.0, lbs)

        WIND_ADJM_MSG   = "WIND ADJM-CALL LOADS OR COMPUTE DATA"
        _TAILWIND_LIMIT = 5          # kt on the primary before a wind is entered

        _primary_rwy = ""
        if xml_root is not None:
            _primary_rwy = (xml_root.findtext('origin/plan_rwy') or '').strip().upper()
        if not _primary_rwy and valid_runways:
            _primary_rwy = str(valid_runways[0].get('id', '')).strip().upper()

        def _rwy_base_num(rid):
            """Leading digits of a runway designator: '28RZ' -> 28."""
            m = re.match(r'^(\d{1,2})', str(rid).strip().upper())
            return int(m.group(1)) if m else None

        def _is_parallel_rwy(rid, primary):
            """
            Parallel is designation +/- 1, covering every L/C/R and any
            intersection off those runways. The reciprocal end is NOT parallel
            (10L and 28R are 18 apart), which is why it takes the message.
            """
            a, b = _rwy_base_num(rid), _rwy_base_num(primary)
            if a is None or b is None:
                return True                  # unparseable — never suppress
            d = abs(a - b)
            return min(d, 36 - d) <= 1

        _wind_applied  = False
        _wind_kt       = 0
        _wind_kind     = ""
        try:
            # SimBrief publishes <headwind_component> per runway, already
            # signed (+ headwind, - tailwind) and computed against the real
            # magnetic course. Prefer it over deriving the component from the
            # ATIS wind and the runway number, which is only accurate to 10
            # degrees — LAX 25R is course 251, not 250.
            _pri_hwc = None
            _pri_row_w = next((r for r in valid_runways
                               if str(r.get('id', '')).upper() == _primary_rwy), None)
            if _pri_row_w is not None:
                try:
                    _pri_hwc = int(round(float(_pri_row_w.get('HD'))))
                except (TypeError, ValueError):
                    _pri_hwc = None
            if _pri_hwc is None and _atis_wind_dir is not None \
                    and _atis_wind_spd is not None and _primary_rwy:
                _pri_spd = _atis_wind_gust if _atis_wind_gust else _atis_wind_spd
                _pri_hwc, _ = _wind_components(_primary_rwy, _atis_wind_dir, _pri_spd)

            # Trigger 1 — tailwind on the primary beyond the limit
            if _pri_hwc is not None and _pri_hwc < -_TAILWIND_LIMIT:
                _wind_applied, _wind_kind, _wind_kt = True, "TAILWIND", abs(int(_pri_hwc))

            # Trigger 2 — performance: calm weight will not take the aeroplane
            if not _wind_applied and _pri_hwc is not None and _pri_hwc > 0:
                _pri_row = next((r for r in valid_runways
                                 if str(r.get('id', '')).upper() == _primary_rwy), None)
                if _pri_row is not None and isinstance(atow, float):
                    _pri_nw = _no_wind_lbs(_pri_row)
                    _pri_mw = (_pri_nw / 1000.0) if _pri_nw else None
                    if isinstance(_pri_mw, float) and 0 < _pri_mw < atow:
                        _wind_applied, _wind_kind, _wind_kt = True, "HEADWIND", int(_pri_hwc)

            if _wind_applied:
                _banner = f"***** {_wind_kt:02d} KT {_wind_kind} APPLIED *****"
                output += " " * max(0, (58 - len(_banner)) // 2) + _banner + "\n"
                LOG.info(f"[WIND] {_wind_kt} kt {_wind_kind} applied — primary {_primary_rwy}; "
                         f"non-parallel runways get '{WIND_ADJM_MSG}'")
            else:
                LOG.info(f"[WIND] calm-wind TPS (primary {_primary_rwy}, "
                         f"component {_pri_hwc if _pri_hwc is not None else 'n/a'})")
        except Exception as _w_e:
            LOG.debug(f"[WIND] adjustment check skipped: {_w_e}")

        # ERJ header: no BLD col, no AT col, extra V215 and VFS cols
        if is_erj:
            output += f"{'RWY':<5} {flap_label:<5} {'V1':>3} {'VR':>3} {'V2':>3} {'V215':>4} {'VFS':>4}   {thr_column_label:<6}   {'MTOW':<6}\n"
        else:
            output += f"{'RWY':<5} {flap_label:<5} {ac_label:<4} {'V1':>3} {'VR':>3} {'V2':>3}   {thr_column_label:<7} {'AT':<8}   {'MTOW':<6}\n"
        output += "-" * 58 + "\n"

        airport_code = sta.strip().upper() if sta != 'ERR' else ""
        LOG.debug(f"[DBG] Airport code for special airport check: {airport_code}")

        oat = None
        try:
            oat = float(temp)
        except (ValueError, TypeError):
            pass



        # ===================================================================
        # WEIGHT OVERRIDE LIMITS
        # ===================================================================
        _l_limit_tow = None
        _e_limit_tow = None   # placeholder — not yet implemented

        if xml_root is not None:
            try:
                _mlw_lbs  = float(xml_root.findtext('weights/max_ldw',   '0') or 0)
                _fbrn_lbs = float(xml_root.findtext('fuel/enroute_burn', '0') or 0)
                _ptow_lbs = est_tow * 1000.0 if isinstance(est_tow, float) else 0.0
                if _mlw_lbs > 0 and _fbrn_lbs > 0 and _ptow_lbs > 0:
                    _plw_lbs  = _ptow_lbs - _fbrn_lbs
                    _mlw_ceil = _mlw_lbs * 1.01
                    if _plw_lbs >= _mlw_lbs:
                        _l_limit_tow = (_mlw_ceil + _fbrn_lbs) / 1000.0
                        LOG.debug(f"[DBG L-LIMIT] PLW={_plw_lbs:.0f} >= MLW={_mlw_lbs:.0f} "
                                  f"-> L_limit={_l_limit_tow:.1f}k lbs (MLW*1.01 + burn)")
                    else:
                        LOG.debug(f"[DBG L-LIMIT] PLW={_plw_lbs:.0f} < MLW={_mlw_lbs:.0f} "
                                  f"-> L override not applicable")
            except Exception as _e:
                LOG.debug(f"[DBG L-LIMIT] Calculation failed: {_e}")

        # ===================================================================
        # BUILD DISPLAY RUNWAY LIST
        # primary + best/worst intxn + 2 others (NOTAM closure applied last)
        # ===================================================================
        _plan_rwy = ''
        if xml_root is not None:
            _plan_rwy = (xml_root.findtext('origin/plan_rwy') or '').strip().upper()

        if _plan_rwy and valid_runways:
            _orig_icao_r  = (xml_root.findtext('origin/icao_code') or '').strip().upper()
            _plan_entries = [r for r in valid_runways if r.get('id', '').upper() == _plan_rwy]
            _base_rwy     = _plan_entries[0] if _plan_entries else None

            _intxn_rows = []
            if _plan_entries and _orig_icao_r and not _is_ejet:
                # Use _full_tora_ft when present (injected by caller for intersection runways)
                _full_tora_r = float(_base_rwy.get('_full_tora_ft', _base_rwy.get('length', 0)) or 0)
                _dist_rej_r  = min(float(r.get('distance_reject', 0) or 0) for r in _plan_entries)
                _idx_r       = load_runway_index()
                _grps_r      = get_intersection_groups(
                    _orig_icao_r, _plan_rwy, _full_tora_r, _dist_rej_r, _idx_r)

                # Only the most restrictive usable intersection for the
                # primary runway — the shortest TORA that still clears the
                # reject distance. Listing several eats runway slots that
                # belong to whole runways.
                _selected = []
                if _grps_r:
                    _selected.append(min(_grps_r, key=lambda g: float(g.get('tora_ft', 0) or 0)))

                for _g in _selected:
                    for _base in _plan_entries:
                        _syn = dict(_base)
                        _syn['id']              = _g['id']
                        _syn['length']          = str(int(_g['tora_ft']))
                        _syn['_intxn_taxiways'] = _g['taxiways']
                        _syn['_intxn_valid']    = _g.get('valid', True)
                        _syn['_synthetic']      = True
                        _syn['_intxn_flap_src'] = _base.get('flaps', '')
                        _intxn_rows.append(_syn)

            _n_intxn_ids      = len(set(_r['id'] for _r in _intxn_rows))
            _slots_for_others = max(0, 5 - 1 - _n_intxn_ids)

            def _reciprocal_rwy(rwy_id):
                s = rwy_id.upper().strip()
                m = re.match(r'^(\d{1,2})([LRC]?)$', s)
                if not m:
                    return None
                num       = int(m.group(1))
                suf       = m.group(2)
                recip_num = (num + 18) if num <= 18 else (num - 18)
                opp       = {'L': 'R', 'R': 'L', 'C': 'C', '': ''}.get(suf, '')
                return f"{recip_num:02d}{opp}"

            _plan_recip = _reciprocal_rwy(_plan_rwy)
            LOG.info(f"[RWY-PICK] plan={_plan_rwy} reciprocal={_plan_recip}")

            _other_candidates = [r for r in valid_runways if r.get('id', '').upper() != _plan_rwy]

            # A runway SimBrief could not produce speeds for is unusable —
            # it must never displace a runway that works.
            def _unusable(r):
                def _blank(v):
                    if v is None:
                        return True
                    sv = str(v).strip()
                    if sv in ('', '0', 'XX', 'XXX', '---'):
                        return True
                    try:
                        return float(sv) == 0.0
                    except (ValueError, TypeError):
                        return False
                if _blank(r.get('v1')) and _blank(r.get('v2')):
                    return True
                try:
                    _mw_lbs = float(r.get('max_weight', 0) or 0) * 1000.0
                    _pt_lbs = (atow * 1000.0) if isinstance(atow, float) else 0.0
                    if _mw_lbs > 0 and _pt_lbs > 0 and _mw_lbs < _pt_lbs:
                        return True
                except Exception:
                    pass
                return False

            # ---------------------------------------------------------------
            # Fill out to five WHOLE runways.
            #
            # SimBrief only analyses the runways it planned for, so a station
            # like PDX (28L/28R, 10L/10R, 03/21) can arrive here with just the
            # planned runway plus a short crosswind strip that cannot take the
            # aeroplane at all. Any remaining whole runway at the field is
            # synthesised from runway_index.dat geometry, with the max weight
            # scaled off the planned runway by field length, so the table shows
            # real alternatives instead of unusable ones.
            # ---------------------------------------------------------------
            _WHOLE_TARGET = 5
            try:
                _have_ids = {r.get('id', '').upper() for r in valid_runways}
                _full_idx = load_full_runway_index().get(_orig_icao_r.upper(), {})
                _plan_len = float(_base_rwy.get('_full_tora_ft', _base_rwy.get('length', 0)) or 0) \
                            if _base_rwy is not None else 0.0
                _plan_mw  = float(_base_rwy.get('max_weight', 0) or 0) if _base_rwy is not None else 0.0

                if _full_idx and _plan_len > 0 and _plan_mw > 0 and _base_rwy is not None:
                    try:
                        # Raw lbs, matching _plan_mw's own convention below
                        # (read straight off max_weight, not through
                        # safe_weight()) — this used to divide by 1000
                        # here while _plan_mw stayed raw, so min(_mw,
                        # _struct_cap) always picked the ~1000x-too-small
                        # _struct_cap, capping every synthesized runway's
                        # weight down to a near-zero "thousands" value that
                        # then got misread as raw lbs everywhere else in
                        # this file (max_weight is raw lbs universally —
                        # see safe_weight()'s callers at lines ~894/922/1425).
                        _struct_cap = float(xml_root.findtext('weights/max_tow_struct', '0') or 0) \
                                      if xml_root is not None else 0.0
                    except Exception:
                        _struct_cap = 0.0

                    _missing = [(rid, geo) for rid, geo in _full_idx.items()
                                if rid not in _have_ids and geo.get('tora_ft', 0) > 0]
                    # Longest runways first — they are the useful alternatives.
                    _missing.sort(key=lambda kv: -kv[1]['tora_ft'])

                    # Unusable runways must not consume fill slots — that is
                    # how PDX ended up showing 28L plus two 6000ft strips the
                    # aeroplane cannot depart from.
                    _usable_have = {r.get('id', '').upper() for r in valid_runways
                                    if not _unusable(r)}
                    _slots = max(0, _WHOLE_TARGET - max(1, len(_usable_have)))
                    for _rid, _geo in _missing[:_slots]:
                        _syn = dict(_base_rwy)
                        _ratio = _geo['tora_ft'] / _plan_len
                        _mw    = _plan_mw * (_ratio ** 0.5)
                        if _struct_cap > 0:
                            _mw = min(_mw, _struct_cap)
                        _syn['id']              = _rid
                        _syn['length']          = str(int(_geo['tora_ft']))
                        _syn['max_weight']      = round(_mw, 1)
                        _syn['gradient']        = _geo.get('slope', 0.0)
                        if _geo.get('elev'):
                            _syn['elevation']   = _geo['elev']
                        _syn.pop('_full_tora_ft', None)
                        _syn['_synthetic_whole'] = True
                        _other_candidates.append(_syn)
                        _have_ids.add(_rid)
                        LOG.info(f"[RWY-FILL] Added whole runway {_rid} "
                                 f"tora={_geo['tora_ft']:.0f}ft mtow={_mw:.1f}")
            except Exception as _fill_e:
                LOG.debug(f"[RWY-FILL] skipped: {_fill_e}")

            _other_scores = {}
            for _r in _other_candidates:
                _bid = _r.get('id', '').upper()
                try:
                    _mw_k         = float(_r.get('max_weight', 0) or 0)
                    atow_lbs_tmp  = (atow * 1000.0) if isinstance(atow, float) else 0.0
                    _margin_score = max(0.0, (_mw_k * 1000.0 - atow_lbs_tmp) / (_mw_k * 1000.0) * 100.0) \
                                    if _mw_k > 0 else 0.0
                except Exception:
                    _margin_score = 0.0
                _hwc_score = 0.0
                if _atis_wind_dir is not None and _atis_wind_spd is not None:
                    _spd = _atis_wind_gust if _atis_wind_gust else _atis_wind_spd
                    _hwc, _ = _wind_components(_bid, _atis_wind_dir, _spd)
                    if _hwc is not None:
                        _hwc_score = float(_hwc)
                _atis_score  = 50.0 if _atis_confirms_rwy(_bid) else 0.0
                _recip_score = (200.0 if (_plan_recip and _bid == _plan_recip and not _atis_dep_rwys)
                                else 200.0 if (_plan_recip and _bid == _plan_recip
                                               and _atis_confirms_rwy(_plan_recip))
                                else -50.0 if (_plan_recip and _bid == _plan_recip and _atis_dep_rwys)
                                else 0.0)
                _composite = _margin_score + _hwc_score + _atis_score + _recip_score
                if _unusable(_r):
                    _composite -= 1000.0        # keep, but only as a last resort
                if _r.get('_synthetic_whole'):
                    _composite -= 10.0          # prefer real analysed runways
                if _bid not in _other_scores or _composite > _other_scores[_bid]:
                    _other_scores[_bid] = _composite
            _top_other_ids = {_bid for _bid, _ in
                              sorted(_other_scores.items(), key=lambda x: -x[1])[:_slots_for_others]}
            _others = [r for r in _other_candidates if r.get('id', '').upper() in _top_other_ids]

            LOG.info(f"[RWY-PICK] plan={_plan_rwy} intxn_ids={_n_intxn_ids} "
                     f"slots_for_others={_slots_for_others} others={sorted(_top_other_ids)}")

            _all_rows       = _plan_entries + _intxn_rows + _others
            _seen_ids_final = []
            _final_rows     = []
            for _r in _all_rows:
                _rid = _r.get('id', '').upper()
                if _rid not in _seen_ids_final:
                    _seen_ids_final.append(_rid)
                if len(_seen_ids_final) <= 5:
                    _final_rows.append(_r)
            valid_runways = _final_rows

            # NOTAM/field-condition closure — final layer. ATIS-confirmed
            # runways were already stripped out of _fc_closed_rwys upstream
            # (ATIS takes precedence), so anything left here is genuinely
            # closed and NOT ATIS-confirmed. plan_rwy/reciprocal are still
            # always retained in valid_runways (downstream row-building
            # assumes the primary runway is present), but flagged loudly.
            if _fc_closed_rwys:
                _protected = {_plan_rwy}
                if _plan_recip:
                    _protected.add(_plan_recip)
                _closable = _fc_closed_rwys - _protected
                if _closable:
                    _before = len(valid_runways)
                    valid_runways = [r for r in valid_runways
                                     if r.get('id', '').upper() not in _closable]
                    LOG.info(f"[FC] Removing closed runways {_closable} "
                             f"({_before - len(valid_runways)} entries dropped)")
                if _fc_closed_rwys & _protected:
                    _atis_display = sorted(_atis_dep_rwys) if _atis_dep_rwys else _atis_dep_rwy
                    LOG.warning(f"[FC] Planned/reciprocal in NOTAM closed set "
                                f"{_fc_closed_rwys & _protected} — retained anyway, but ATIS "
                                f"does NOT confirm it (ATIS active={_atis_display}); "
                                f"plan_rwy={_plan_rwy} may be stale — verify before use")

            # Rebuild runway_lines for synthetic rows
            _line_map = {}
            for ln in runway_lines:
                _lid = ln.split()[0] if ln.split() else ''
                _line_map.setdefault(_lid, []).append(ln)

            _plan_lines_by_flap = {}
            for ln in _line_map.get(_plan_rwy, []):
                _lp = ln.split()
                if len(_lp) >= 2:
                    _plan_lines_by_flap.setdefault(_lp[1], ln)

            runway_lines = []
            for _r in valid_runways:
                _rid = _r.get('id', '')
                if _r.get('_synthetic'):
                    _flap_key  = str(_r.get('flaps', '')).strip()
                    _base_line = _plan_lines_by_flap.get(_flap_key)
                    if _base_line is None and _line_map.get(_plan_rwy):
                        _base_line = _line_map[_plan_rwy][0]
                    if _base_line:
                        _parts    = _base_line.split()
                        _parts[0] = _rid
                        runway_lines.append(' '.join(_parts))
                        LOG.info(f"[INTXN] Synthetic runway_line for {_rid} flap={_flap_key}: {' '.join(_parts)}")
                    else:
                        LOG.warning(f"[INTXN] No base line found for {_plan_rwy} to clone {_rid}")
                elif _rid in _line_map and _line_map[_rid]:
                    runway_lines.append(_line_map[_rid].pop(0))

            runway_lines = runway_lines[:len(valid_runways)]
        else:
            valid_runways = valid_runways[:5]
            runway_lines  = runway_lines[:5]

        # ===================================================================
        # CALM-WIND REBASE FROM THE TLR TABLES
        # ===================================================================
        # The <takeoff><runway> blocks carry SimBrief's wind-adjusted figures.
        # <tlr_section> carries the calm-wind tables the TPS is built from, at
        # PTOW and at a size-dependent bracket above it. Interpolate those to
        # ATOW and use them; fall back to the estimated credit removal only
        # when a runway is absent from the tables.
        _calm_rows, _calm_frac = {}, 0.0
        try:
            _tlr_text = ""
            if xml_root is not None:
                for _el in xml_root.iter():
                    if _el.text and 'CALM WIND' in _el.text:
                        _tlr_text = _el.text
                        break
            if _tlr_text:
                _surf_now = 'WET' if 'WET' in str(surface).upper() else 'DRY'
                _calm_rows, _calm_frac = interpolate_to_atow(
                    parse_calm_wind_tables(_tlr_text), _surf_now,
                    atow_delta_lbs(_struct_mtow * 1000.0
                                   if isinstance(_struct_mtow, float) else 0))
                if _calm_rows:
                    LOG.info(f"[CALM-TLR] {_surf_now} tables interpolated to ATOW "
                             f"at fraction {_calm_frac:.2f}: {sorted(_calm_rows)}")
        except Exception as _ct_e:
            LOG.debug(f"[CALM-TLR] unavailable: {_ct_e}")

        _line_by_id = {}
        for _ln in runway_lines:
            _p = _ln.split()
            if _p:
                _line_by_id.setdefault(_p[0].upper(), []).append(_ln)

        for _r in valid_runways:
            if '_mw_simbrief' in _r:
                continue
            _r['_mw_simbrief'] = _r.get('max_weight')
            _rid_c = str(_r.get('id', '')).upper()
            # Intersections inherit their parent runway's calm row
            _row_c = _calm_rows.get(_rid_c) or _calm_rows.get(_rid_c[:-1])
            if _row_c:
                _r['max_weight'] = float(_row_c['mtow_lb'])
                _r['flex']       = str(_row_c['mt'])
                _r['max_temp']   = str(_row_c['mt'])
                _r['v1']         = str(_row_c['v1'])
                _r['vr']         = str(_row_c['vr'])
                _r['v2']         = str(_row_c['v2'])
                _r['flaps']      = str(_row_c['flp'])
                _r['limit_code'] = _row_c['limit'][:1]
                _r['_calm_tlr']  = True
                # Keep the printed line in step with the row
                for _i, _ln in enumerate(runway_lines):
                    _p = _ln.split()
                    if _p and _p[0].upper() == _rid_c and len(_p) >= 8:
                        _p[1], _p[3], _p[4], _p[5], _p[7] = (
                            str(_row_c['flp']), str(_row_c['v1']),
                            str(_row_c['vr']), str(_row_c['v2']), str(_row_c['mt']))
                        runway_lines[_i] = ' '.join(_p)
            else:
                _nw = _no_wind_lbs(_r)
                if _nw is not None:
                    _r['max_weight'] = _nw

        # ===================================================================
        # FIRST PASS: collect runway row data
        # ===================================================================
        runway_rows = []
        for line in runway_lines:
            parts = line.split()
            rwy, flap, bld, v1, vr, v2, thr, at = (parts + [""] * 8)[:8]

            v1 = v1 if v1.isdigit() and int(v1) > 0 else "XXX"
            vr = vr if vr.isdigit() and int(vr) > 0 else "XXX"
            v2 = v2 if v2.isdigit() and int(v2) > 0 else "XXX"

            apu_status = ('OFF' if bld.upper() == 'ON' else 'ON') if is_airbus else bld

            # ERJ: compute V2+15 and VFS from weight-based lookup
            v215_display = "XXX"
            vfs_display  = "XXX"
            if is_erj:
                try:
                    _v2_int = int(v2)
                    v215_display = str(_v2_int + 15)
                except (ValueError, TypeError):
                    v215_display = "XXX"
                _erj_speed_row = get_speed_other(icaocode, weight=weight_lbs)
                if _erj_speed_row and 'speed' in _erj_speed_row:
                    vfs_display = str(_erj_speed_row['speed'])
                LOG.debug(f"[DBG ERJ] rwy={rwy} V2={v2} V215={v215_display} VFS={vfs_display}")

            # With a wind entered, only the primary runway and its parallels
            # are computed — the rest carry the WIND ADJM message.
            _wind_adjm_row = bool(_wind_applied) and not _is_parallel_rwy(rwy, _primary_rwy)

            matched_rwy        = next((r for r in valid_runways if r['id'] == rwy), None)
            mtow_val           = None
            original_limit_code = ''
            rwy_message        = ""
            if matched_rwy:
                # Calm by default; the runways the entered wind applies to
                # (primary and parallels) print SimBrief's wind-corrected value.
                if _wind_applied and not _wind_adjm_row:
                    max_w_raw = matched_rwy.get('_mw_simbrief',
                                                matched_rwy.get('max_weight', 0))
                else:
                    max_w_raw = matched_rwy.get('max_weight', 0)
                raw_code    = str(matched_rwy.get('limit_code', '') or '').strip().upper()
                rwy_message = str(matched_rwy.get('runway_message', '') or '').strip()

                _CODE_MAP = {
                    'T': 'T', 'F': 'T', 'S': 'S', 'A': 'S',
                    'L': 'L', 'D': 'D', 'E': 'E', 'X': 'X', '': '',
                }
                _lookup_code    = raw_code.strip()
                display_code    = _CODE_MAP.get(_lookup_code, _lookup_code[:1] if _lookup_code else '')
                original_limit_code = display_code
                LOG.debug(f"[DBG MTOW] rwy={rwy!r} max_w_raw={max_w_raw!r} "
                          f"raw_code={raw_code!r} -> display_code={display_code!r}")

                try:
                    mtow_val = safe_weight(max_w_raw)
                except Exception:
                    mtow_val = None

                if _l_limit_tow is not None and isinstance(mtow_val, float):
                    if _l_limit_tow < mtow_val:
                        _orig_mtow   = mtow_val
                        mtow_val     = round(_l_limit_tow, 1)
                        display_code = 'L'
                        LOG.debug(f"[DBG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} < mtow={_orig_mtow:.1f} -> override to L")
                    else:
                        LOG.debug(f"[DBG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} >= mtow={mtow_val:.1f} -> no L override")

                try:
                    mtow = f"{mtow_val:.1f}{display_code}" if isinstance(mtow_val, float) else ""
                except Exception:
                    mtow = ""
            else:
                mtow = ""

            _MSG_MAP = {
                'PTOW EXCEEDS MTOW ALL FLAPS': 'PTOW EXCEEDS MTOW ALL FLAPS',
                'PTOW EXCEEDS MTOW':           'PTOW EXCEEDS MTOW - RQST NEW TPS',
                'FLAP N/A':                    'FLAP N/A THIS RWY - RQST NEW TPS',
                'WIND ADJ':                    'WIND ADJM-CALL LOADS OR COMPUTE DATA',
                'WIND ADJM':                   'WIND ADJM-CALL LOADS OR COMPUTE DATA',
            }
            if rwy_message:
                for key, display in _MSG_MAP.items():
                    if key in rwy_message.upper():
                        rwy_message = display
                        break

            at_display           = ""
            at_override_occurred = False
            at_numeric           = None
            if at and "TOGA" not in str(at).upper():
                try:
                    temp_val = float(re.sub(r"[^0-9.-]", "", str(at)))
                    if temp_val < 100:
                        at_numeric = temp_val
                except (ValueError, TypeError):
                    pass

            _max_thrust_codes    = {'T', 'S', 'A'}
            _code_requires_max_thrust = original_limit_code in _max_thrust_codes
            _is_l_limited        = (display_code == 'L') if matched_rwy else False

            if _is_l_limited:
                at_display           = f"{int(at_numeric)}C" if at_numeric is not None else at
                at_override_occurred = False
            elif _code_requires_max_thrust and mtow_val and isinstance(atow, float) and atow > mtow_val:
                at_display           = "MAX-WT"
                at_override_occurred = True
            elif "TOGA" in str(at).upper():
                at_display           = "MAX-WT"
                at_override_occurred = True
            elif airport_code in SPECIAL_AIRPORTS:
                at_display           = "MAX-SPCL"
                at_override_occurred = True
                LOG.debug(f"[DBG] Special airport detected: {airport_code}")
            elif oat is not None and at_numeric is not None and abs(oat - at_numeric) <= 5:
                at_display           = "MAX-TEMP"
                at_override_occurred = True
            elif at_numeric is not None:
                at_display = f"{int(at_numeric)}C"
            else:
                at_display           = "MAX-WT"
                at_override_occurred = True

            thr_display = thr
            # ERJ thrust label mapping: SimBrief XML value -> TPS display
            _ERJ_THR_MAP = {
                'TO':       'TO1',
                'ATO':      'TO1',
                'ALT TO-1': 'ATO',
                'ALT TO1':  'ATO',
                'ALT TO':   'ATO',
                'TO-1':     'TO1',
                'TO1':      'TO1',
                'TO-2':     'TO2',
                'TO2':      'TO2',
            }
            _thr_upper = thr.upper().strip()
            if is_erj and _thr_upper in _ERJ_THR_MAP:
                thr_display = _ERJ_THR_MAP[_thr_upper]

            if is_md8x:
                if at_override_occurred:
                    thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)
                else:
                    if at_numeric is not None:
                        epr_takeoff_data = get_speed_other(
                            icaocode, oat=temp, altitude=alt_val, assumed_temp=int(at_numeric))
                        if epr_takeoff_data and 'epr' in epr_takeoff_data:
                            thr_display = f"{epr_takeoff_data['epr']:.2f}"
                        else:
                            thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)
                    else:
                        thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)

            elif is_boeing_737:
                if at_override_occurred:
                    thr_display = str(n1_pack_on)
                else:
                    if at_numeric is not None and effective_thrust is not None and not sfp_bump:
                        reduced_n1_data = get_reduced_thrust_n1(
                            lookup_icaocode, effective_thrust, int(at_numeric), alt_val)
                        thr_display = str(reduced_n1_data['n1']) \
                                      if reduced_n1_data and 'n1' in reduced_n1_data \
                                      else str(n1_pack_on)
                    else:
                        thr_display = str(n1_pack_on)

            elif is_airbus:
                thr_display = "TOGA" if (at_override_occurred or at_display.startswith("MAX")) else "FLEX"
                # FLEX is the same grid read at the assumed temperature —
                # that's what reduced thrust means, and it's how the MD-8x
                # branch above already derives its takeoff EPR. Falls back
                # to the bare "FLEX"/"TOGA" label when there's no grid for
                # this engine rather than inventing a setting.
                _fx_temp = int(at_numeric) if (thr_display == "FLEX" and at_numeric is not None) else None
                _fx = get_takeoff_thrust(icaocode, engine_type, temp, alt_val,
                                         assumed_temp=_fx_temp)
                if _fx:
                    _fv = f"{_fx['value']:.2f}" if _fx['param'] == 'EPR' else f"{_fx['value']:.1f}"
                    thr_display = f"{thr_display} {_fv}"

            elif is_erj:
                # ERJ: apply same thrust label mapping as raw passthrough above
                thr_display = _ERJ_THR_MAP.get(_thr_upper, thr_display)

            else:
                # Normalise TO1/TO2/TO3 → TO-1/TO-2/TO-3
                _thr_norm = re.sub(r'^(TO)([123])$', r'\1-\2', thr.upper().strip())
                if _thr_norm != thr:
                    thr_display = _thr_norm
                _derate_values = {"TO", "TO1", "TO2", "TO3", "TO-1", "TO-2", "TO-3", "ATO"}
                if at_override_occurred and thr.upper().strip() not in _derate_values:
                    thr_display = "TOGA"

            def _is_no_data(val):
                if val is None:
                    return True
                if str(val).strip() in ('', '0', 'XXX', 'XX', '---'):
                    return True
                try:
                    return float(val) == 0.0
                except (ValueError, TypeError):
                    return False

            if not rwy_message:
                v1_bad   = _is_no_data(v1)
                v2_bad   = _is_no_data(v2)
                mtow_bad = _is_no_data(mtow_val)
                if v1_bad and v2_bad and mtow_bad:
                    rwy_message = 'PTOW EXCEEDS MTOW ALL FLAPS'
                elif v1_bad and v2_bad:
                    rwy_message = 'PTOW EXCEEDS MTOW - RQST NEW TPS'
                elif mtow_bad:
                    rwy_message = 'FLAP N/A THIS RWY - RQST NEW TPS'

            if not mtow and rwy_message and xml_root is not None:
                _struct_raw = xml_root.findtext('weights/max_tow_struct', '0') or '0'
                _struct_w   = safe_weight(float(_struct_raw))
                if isinstance(_struct_w, float):
                    mtow = f"{_struct_w:.1f}S"
                    LOG.debug(f"[DBG MTOW-FALLBACK] {rwy}: using max_tow_struct={_struct_w:.1f}S")

            try:
                flap_int = int(float(flap))
                flap_fmt = f"{flap_int:02d}" if 0 < flap_int <= 9 else str(flap_int)
            except Exception:
                flap_fmt = flap

            runway_rows.append({
                'rwy': rwy, 'flap': flap, 'flap_fmt': flap_fmt,
                'apu_status': apu_status, 'v1': v1, 'vr': vr, 'v2': v2,
                'wind_adjm': _wind_adjm_row,
                'v215_display': v215_display, 'vfs_display': vfs_display,
                'thr_display': thr_display, 'at_display': at_display,
                'mtow': mtow, 'rwy_message': rwy_message,
            })

        # ===================================================================
        # SECOND PASS: flag improved-performance runways with *
        # ===================================================================
        flap_vr_map = collections.defaultdict(list)
        for row in runway_rows:
            if not row['rwy_message']:
                try:
                    flap_vr_map[row['flap']].append(int(row['vr']))
                except (ValueError, TypeError):
                    pass

        for row in runway_rows:
            if not row['rwy_message'] and row['flap'] in flap_vr_map:
                vr_list = flap_vr_map[row['flap']]
                if len(vr_list) > 1:
                    try:
                        if int(row['vr']) - min(vr_list) > 5:
                            row['flap_fmt'] = row['flap_fmt'] + '*'
                    except (ValueError, TypeError):
                        pass

        # ===================================================================
        # OUTPUT PASS
        # ===================================================================
        for row in runway_rows:
            rwy          = row['rwy']
            flap_fmt     = row['flap_fmt']
            apu_status   = row['apu_status']
            v1           = row['v1']
            vr           = row['vr']
            v2           = row['v2']
            v215_display = row['v215_display']
            vfs_display  = row['vfs_display']
            thr_display  = row['thr_display']
            at_display   = row['at_display']
            mtow         = row['mtow']
            rwy_message  = row['rwy_message']

            if row.get('wind_adjm'):
                output += f"{rwy:<5} {WIND_ADJM_MSG}\n"
                output += "-" * 58 + "\n"
                continue

            if rwy_message:
                msg_field   = f"{rwy_message:<33}"
                mtow_suffix = f"{mtow:<6}" if mtow else ""
                if is_erj:
                    output += f"{rwy:<5} {flap_fmt:<5} {msg_field}{mtow_suffix}\n"
                else:
                    output += f"{rwy:<5} {flap_fmt:<5} {apu_status:<4} {msg_field}{mtow_suffix}\n"
            elif is_erj:
                # ERJ: V1 VR V2 V215 VFS   ATO   MTOW  (no BLD, no AT)
                output += (f"{rwy:<5} {flap_fmt:<5} {v1:>3} {vr:>3} {v2:>3} {v215_display:>4} "
                           f"{vfs_display:>4}   {thr_display:<6}   {mtow:<6}\n")
            else:
                output += f"{rwy:<5} {flap_fmt:<5} {apu_status:<4} {v1:>3} {vr:>3} {v2:>3}   {thr_display:<7} {at_display:<8}   {mtow:<6}\n"
            output += "-" * 58 + "\n"

        # ===================================================================
        # AIRPORT NOTES (intersections + EFP)
        # ===================================================================
        _notes_lines = []

        # ---- Engine-out (special engine-failure) procedures -----------------
        # ENGINEFAILPROC is keyed on ICAO; `sta` is the IATA code, so the
        # lookup must be done with the origin ICAO or it silently never
        # matches and the EFP note is dropped from AIRPORT NOTES.
        _efp_icao = ""
        if xml_root is not None:
            _efp_icao = (xml_root.findtext('origin/icao_code') or '').strip().upper()
        if not _efp_icao and sta and sta != 'ERR':
            _efp_icao = ('K' + sta) if len(sta) == 3 else sta

        airport_altitudes = get_airport_specific_altitudes(_efp_icao, max_elevation)
        if not airport_altitudes or 'EFP' not in airport_altitudes:
            _alt_try = get_airport_specific_altitudes(sta, max_elevation)
            if _alt_try and 'EFP' in _alt_try:
                airport_altitudes = _alt_try

        _efp_raw = (airport_altitudes or {}).get('EFP', "") or ""
        if _efp_raw:
            _efp_raw = _efp_raw.replace('ENGINE FAILURE', 'ENGINE-FAILURE')
            _EFP_HDR = 'SPECIAL ENGINE-FAILURE PROCEDURES'
            for _seg in [x.strip() for x in _efp_raw.split('\n') if x.strip()]:
                if _seg.upper().startswith(_EFP_HDR):
                    _rest = _seg[len(_EFP_HDR):].strip()
                    _notes_lines.append(f" {_EFP_HDR}\n")
                    if _rest:
                        if not _rest.endswith('.'):
                            _rest += '.'
                        for _w in textwrap.wrap(_rest, 56):
                            _notes_lines.append(f" {_w}\n")
                else:
                    for _w in textwrap.wrap(_seg, 56):
                        _notes_lines.append(f" {_w}\n")

        # Intersection notes are suppressed for E-jet family
        if xml_root is not None and _INTXN_AVAILABLE and not _is_ejet:
            _orig_icao  = (xml_root.findtext('origin/icao_code') or '').strip().upper()
            _idx        = load_runway_index()
            _seen_rwy_notes = set()
            _rwy_note_order = []
            for _r in valid_runways:
                _rid = _r.get('id', '').upper()
                if _r.get('_synthetic'):
                    continue
                if _rid not in _seen_rwy_notes:
                    _seen_rwy_notes.add(_rid)
                    _rwy_note_order.append(_rid)

            for _note_rwy in _rwy_note_order:
                _rwy_entries = [r for r in valid_runways
                                if r.get('id', '').upper() == _note_rwy and not r.get('_synthetic')]
                if not _rwy_entries:
                    continue
                # Use _full_tora_ft if the caller injected it (intersection departure case)
                _full_tora   = float(_rwy_entries[0].get('_full_tora_ft',
                                     _rwy_entries[0].get('length', 0)) or 0)
                _dist_reject = min(float(r.get('distance_reject', 0) or 0) for r in _rwy_entries)
                LOG.info(f"[INTXN-NOTES] {_note_rwy} tora={_full_tora:.0f} dist_reject={_dist_reject:.0f}")
                if _full_tora <= 0:
                    continue
                _grps = get_intersection_groups(_orig_icao, _note_rwy, _full_tora, _dist_reject, _idx)
                if _grps:
                    _notes_lines.append(f" RWY {_note_rwy} INTXN TAKEOFFS...\n")
                    for _g in _grps:
                        _txwy = '/'.join(_g['taxiways'])
                        _notes_lines.append(f"  {_g['id']} FROM TXWY {_txwy}.\n")
                    _notes_lines.append("\n")

        if _notes_lines:
            output += "\n"
            output += "************* AIRPORT NOTES ********************************\n"
            for _nl in _notes_lines:
                output += _nl
            output += "\n"

        # ===================================================================
        # AIRPORT ANALYSIS DATA
        # ===================================================================
        if valid_runways:
            output += "********* AIRPORT ANALYSIS DATA ****************************\n"

            PRE      = 20      # label field width (CONF APU LIMIT C)
            CW       = 6       # per-runway column width
            MAX_RWYS = 5
            SEPW     = 55      # fixed rule width used by the FOS printout

            def _fmt_conf(flap_val):
                s = str(flap_val).strip()
                try:
                    n = int(float(s))
                    return f"{n:02d}" if 0 < n <= 9 else str(n)
                except Exception:
                    return s

            def _sep(n):
                return "-" * SEPW + "\n"

            def _div(n):
                return ("   " + "- " * ((SEPW - 3) // 2)).rstrip() + "\n"

            def _fmt_slope(sv):
                try:
                    sf = float(sv) if sv is not None else 0.0
                    if sf == 0.0:
                        return ".0"
                    s = f"{sf:.1f}"
                    if s.startswith("0."):    s = s[1:]
                    elif s.startswith("-0."): s = "-" + s[2:]
                    return s
                except Exception:
                    return "x.x"

            try:
                _struct_raw      = xml_root.findtext('weights/max_tow_struct', '0') or '0'
                struct_wt_limit  = safe_weight(float(_struct_raw))
                struct_wt_limit  = struct_wt_limit if isinstance(struct_wt_limit, float) else 0.0
            except Exception:
                struct_wt_limit  = 0.0
            struct_str = f"{struct_wt_limit:.1f}" if struct_wt_limit > 0 else "0.0"
            output += f"\n   STRUCT WT LIMIT {struct_str}\n\n"

            def _cap_wt(val_thou):
                if isinstance(val_thou, float) and struct_wt_limit > 0:
                    return min(val_thou, struct_wt_limit)
                return val_thou

            eo_afl_int = 0
            if airport_altitudes:
                try:
                    eo_afl_int = int(float(airport_altitudes.get('eo_acc', '0')))
                except Exception:
                    pass

            TEMP_PENALTY = 0.3
            _OD = collections.OrderedDict

            unique_rwy_by_id = {}
            for r in valid_runways:
                rid = r.get('id', 'XX')
                if rid not in unique_rwy_by_id:
                    unique_rwy_by_id[rid] = r

            conf_groups = _OD()
            for r in valid_runways:
                conf_k  = _fmt_conf(r.get('flaps', 'XX'))
                bleed_k = str(r.get('bleed', '')).upper()
                apu_k   = ('OFF' if bleed_k == 'ON' else 'ON') if is_airbus \
                          else (bleed_k if bleed_k in ('ON', 'OFF') else 'OFF')
                key = (conf_k, apu_k)
                if key not in conf_groups:
                    conf_groups[key] = {}
                conf_groups[key][r.get('id', 'XX')] = r

            def _conf_sort_key(c):
                try:
                    return int(float(c))
                except Exception:
                    return 999

            conf_groups = _OD(
                sorted(conf_groups.items(),
                       key=lambda kv: (_conf_sort_key(kv[0][0]), 0 if kv[0][1] == 'OFF' else 1))
            )
            unique_confs = sorted(dict.fromkeys(ck for (ck, _) in conf_groups), key=_conf_sort_key)

            atow_lbs = (atow * 1000.0) if isinstance(atow, float) else 0.0

            # ---------------------------------------------------------------
            # One table: every runway is a column, each configuration gets a
            # block beneath, LENGTH/SLOPE once at the foot. SimBrief only
            # analyses one configuration per runway, so the others are
            # estimated from the analysed one.
            # ---------------------------------------------------------------
            all_ids = list(dict.fromkeys(
                r.get('id', 'XX') for r in valid_runways))[:MAX_RWYS]

            _rwy_native = {}
            for (_ck, _ak), _rid_map in conf_groups.items():
                for _rid, _r in _rid_map.items():
                    if _rid not in _rwy_native:
                        _w = safe_weight(_r.get('max_weight', 0))
                        _rwy_native[_rid] = (_ck, _ak,
                                             _w if isinstance(_w, float) else 0.0)

            _CONF_STEP = 0.0085     # climb-limit delta per configuration step

            def _conf_pos(cval):
                try:
                    return unique_confs.index(cval)
                except ValueError:
                    return 0

            def _estimate_mw(cid, conf_target, apu_target):
                """Weight for a runway/config SimBrief did not analyse."""
                native = _rwy_native.get(cid)
                if not native:
                    return None
                _nc, _na, _nw = native
                if _nw <= 0:
                    return None
                steps = _conf_pos(conf_target) - _conf_pos(_nc)
                est   = _nw * (1.0 - _CONF_STEP * steps)
                if apu_target == 'ON' and _na == 'OFF':
                    est *= 1.022
                elif apu_target == 'OFF' and _na == 'ON':
                    est /= 1.022
                return round(est, 1)

            _n_all = len(all_ids)
            output += (f"{'':4} {'':3} {'CLIMB':>5} {'TEMP':>4} "
                       + "".join(f"{'RWY':>{CW}}" for _ in all_ids) + "\n")
            output += (f"{flap_label:<4} {ac_label:<3} {'LIMIT':>5} {'C':>4} "
                       + "".join(f"{rid:>{CW}}" for rid in all_ids) + "\n")
            output += _sep(_n_all)

            for conf_only in unique_confs:
                conf_id_chunks = [all_ids]

                off_rid_map  = conf_groups.get((conf_only, 'OFF'), {})
                on_rid_map   = conf_groups.get((conf_only, 'ON'),  {})
                base_rid_map = off_rid_map if off_rid_map else on_rid_map

                try:
                    first_r  = base_rid_map[next(iter(base_rid_map))]
                    oat_base = int(float(first_r.get('max_temp', first_r.get('temp', 23))))
                except Exception:
                    oat_base = 23

                APU_ON_BOOST = 0.022

                for chunk_idx, conf_chunk_ids in enumerate(conf_id_chunks):
                    nc = len(conf_chunk_ids)

                    if chunk_idx > 0:
                        output += "\n****** AIRPORT ANALYSIS DATA (CONT.) **********************\n"
                        output += f"\n   STRUCT WT LIMIT {struct_str}\n\n"

                    _climb_by_row = {}
                    _oat_by_row    = {}
                    for apu_row, t_off in [('OFF', 0), ('OFF', 2), ('ON', 0), ('ON', 2)]:
                        conf_lbl    = conf_only if (apu_row == 'OFF' and t_off == 2) else ""
                        apu_lbl     = apu_row
                        penalty     = TEMP_PENALTY if t_off == 2 else 0.0
                        try:
                            oat_display = int(float(temp)) + t_off
                        except Exception:
                            oat_display = oat_base + t_off

                        src_map = off_rid_map if apu_row == 'OFF' else on_rid_map

                        adj_wts, rwy_cols = [], []
                        for cid in conf_chunk_ids:
                            if cid in src_map:
                                mw = _cap_wt(safe_weight(src_map[cid].get('max_weight', 0)))
                            elif apu_row == 'ON' and cid in off_rid_map:
                                mw_off = safe_weight(off_rid_map[cid].get('max_weight', 0))
                                mw = _cap_wt(round((mw_off if isinstance(mw_off, float) else 0.0)
                                                   * (1 + APU_ON_BOOST), 1))
                            elif apu_row == 'OFF' and cid in on_rid_map:
                                mw_on = safe_weight(on_rid_map[cid].get('max_weight', 0))
                                mw = _cap_wt(round((mw_on if isinstance(mw_on, float) else 0.0)
                                                   / (1 + APU_ON_BOOST), 1))
                            else:
                                mw = _cap_wt(_estimate_mw(cid, conf_only, apu_row))
                            if mw is not None:
                                adj = round(max(0.0, mw - penalty), 1)
                                adj_wts.append(adj)
                                rwy_cols.append(f"{adj:>{CW}.1f}")
                            else:
                                rwy_cols.append(" " * CW)

                        if not adj_wts:
                            continue
                        climb_limit = min(adj_wts)
                        _climb_by_row[(apu_row, t_off)] = climb_limit
                        _oat_by_row[(apu_row, t_off)]   = oat_display
                        output += f"{conf_lbl:<4} {apu_lbl:<3} {climb_limit:>5.1f} {oat_display:>4} " + "".join(rwy_cols) + "\n"

                    # HDWND / TLWND
                    primary_rid_map = conf_groups.get((conf_only, 'OFF'),
                                      conf_groups.get((conf_only, 'ON'), {}))
                    hw_cols, tl_cols = [], []
                    for cid in conf_chunk_ids:
                        r = primary_rid_map.get(cid) or unique_rwy_by_id.get(cid)
                        if r:
                            mw_thou = _cap_wt(safe_weight(r.get('max_weight', 0)))
                            if isinstance(mw_thou, float) and mw_thou > 0:
                                mtow_lbs   = mw_thou * 1000.0
                                margin_lbs = max(0.0, mtow_lbs - atow_lbs)
                                wt_margin  = margin_lbs / mtow_lbs if mtow_lbs > 0 else 0.0
                                try:
                                    rwy_length  = float(unique_rwy_by_id[cid].get('length', 0) or 0)
                                    dist_margin = float(r.get('asdr', 0) or 0)
                                    dist_ratio  = (dist_margin / rwy_length) if rwy_length > 0 else 0.0
                                except Exception:
                                    dist_ratio  = 0.0
                                combined    = (wt_margin * dist_ratio) ** 0.5
                                hw_scale    = 0.60 + 0.40 * min(combined * 3.0, 1.0)
                                hw_lbs      = max(100, round(mtow_lbs * _HDWND_BASE_RATE * hw_scale))
                                tlwnd_zeroed = dist_ratio >= 0.30
                                tw_scale    = 1.40 - 0.40 * min(combined * 3.0, 1.0)
                                tw_lbs      = 0 if tlwnd_zeroed else max(200, round(mtow_lbs * _TLWND_BASE_RATE * tw_scale))
                            else:
                                hw_lbs = 1
                                tw_lbs = 0
                        else:
                            hw_lbs = 1
                            tw_lbs = 0
                        hw_cols.append(f"{hw_lbs:>{CW}}")
                        tl_cols.append(f"{tw_lbs:>{CW}}")
                    output += f"{'HDWND ADD / KT':>{PRE}}" + "".join(hw_cols) + "\n"
                    output += f"{'TLWND SUB / KT':>{PRE}}" + "".join(tl_cols) + "\n"
                    output += _div(nc)

                    # E/O ACCEL AFL / MSL
                    afl_cols, msl_cols = [], []
                    for cid in conf_chunk_ids:
                        r = unique_rwy_by_id.get(cid)
                        afl_cols.append(f"{eo_afl_int:>{CW}}")
                        try:
                            raw_elev = r.get('elevation', None) if r else None
                            if raw_elev in (None, '', 'ERR', 0, '0'):
                                elev = float(max_elevation)
                            else:
                                elev = float(raw_elev)
                            if elev <= 0:
                                elev = float(max_elevation)
                            msl_rounded = int(_math.ceil((elev + eo_afl_int) / 10.0)) * 10
                            msl_cols.append(f"{msl_rounded:>{CW}}")
                        except Exception:
                            msl_cols.append(f"{int(max_elevation):>{CW}}")
                    output += f"{'E/O ACCEL /AFL/ FT':>{PRE}}" + "".join(afl_cols) + "\n"
                    output += f"{'/MSL/ FT':>{PRE}}" + "".join(msl_cols) + "\n"
                    # The divider only separates E/O from the improved-performance
                    # group, so later config blocks run straight into the rule.
                    if conf_only == unique_confs[0] and chunk_idx == 0:
                        output += _div(nc)

                    # ---------------------------------------------------
                    # IMPROVED PERFORMANCE (first configuration only)
                    # ---------------------------------------------------
                    if conf_only == unique_confs[0] and chunk_idx == 0:
                        _IMPROVED_GAIN = 0.0047     # ~0.5% climb-limit credit
                        try:
                            _struct_lbs_ip = float(xml_root.findtext('weights/max_tow_struct', '0') or '0') \
                                             if xml_root is not None else 0.0
                        except Exception:
                            _struct_lbs_ip = 0.0
                        if _struct_lbs_ip > 0:
                            _ai_ip = round(_struct_lbs_ip * 0.003 / 1000, 1)
                            _ai_ip_str = (f"{_ai_ip:.1f}".lstrip('0') or '.0')
                        else:
                            _ai_ip_str = ".0"

                        _zero_cols = "".join(f"{0:>{CW}}" for _ in conf_chunk_ids)
                        output += " " * 7 + "IMPROVED PERFORMANCE\n"
                        for _t_off in (0, 2):
                            _cl = _climb_by_row.get(('ON', _t_off))
                            if _cl is None:
                                continue
                            _imp      = round(_cl * (1 + _IMPROVED_GAIN), 1)
                            _conf_lbl = conf_only if _t_off == 2 else ""
                            _oat_i    = _oat_by_row.get(('ON', _t_off), 0)
                            output += (f"{_conf_lbl:<4} {'ON':<3} {_imp:>5.1f} {_oat_i:>4} "
                                       + _zero_cols + "\n")
                        output += f"{'A/I ON SUB':>{PRE - 6}}{_ai_ip_str:>6}" + _zero_cols + "\n"

                        # PLANNED WIND — part of the improved-performance group
                        # The wind actually used for each runway: calm unless a
                        # wind was entered, and then only on its parallels.
                        wind_cols = []
                        for cid in conf_chunk_ids:
                            r = unique_rwy_by_id.get(cid)
                            hw = 0
                            if _wind_applied and _is_parallel_rwy(cid, _primary_rwy):
                                try:
                                    hw = int(round(float(r.get('HD', 0)))) if r else 0
                                except (TypeError, ValueError):
                                    hw = 0
                            wind_cols.append(
                                f"{'H'+str(hw) if hw >= 0 else 'T'+str(abs(hw)):>{CW}}")
                        output += f"{'PLANNED WIND KT':>{PRE}}" + "".join(wind_cols) + "\n"

                    output += _sep(nc)

            # LENGTH / SLOPE — once, beneath every configuration block
            _len_cols   = "".join(f"{int(unique_rwy_by_id[cid].get('length', 0)):>{CW}}"
                                  for cid in all_ids if cid in unique_rwy_by_id)
            _slope_cols = "".join(f"{_fmt_slope(unique_rwy_by_id[cid].get('gradient')):>{CW}}"
                                  for cid in all_ids if cid in unique_rwy_by_id)
            output += f"{'LENGTH - FT':>{PRE}}" + _len_cols + "\n"
            output += f"{'SLOPE - PCT':>{PRE}}" + _slope_cols + "\n"
            output += _sep(_n_all)

            # A/I SUB — only when OAT <= 15°C; use structural weight for the penalty value
            try:
                _ai_oat = int(float(temp))
            except Exception:
                _ai_oat = 99
            if _ai_oat <= 15:
                try:
                    _struct_lbs_ai = float(xml_root.findtext('weights/max_tow_struct', '0') or '0') \
                                     if xml_root is not None else 0.0
                    if _struct_lbs_ai <= 0:
                        # Fallback: use est_tow from first runway
                        _struct_lbs_ai = float(
                            next(iter(unique_rwy_by_id.values())).get('est_tow', 0)) * 1000.0
                    ai_val = round(_struct_lbs_ai * 0.003 / 1000, 1)
                    ai_str = f"{ai_val:.1f}".lstrip('0') or '.0'
                except Exception:
                    ai_str = ".0"
                # Full-width separator for A/I line (matches TAKEOFF_PERF)
                _full_sep = "-" * SEPW + "\n"
                output += f" A/I ON SUB FROM CLB {ai_str} RWY {ai_str}\n"
                output += _full_sep

            output += "END\n"

            # ---------------------------------------------------------------
            # TAA (Takeoff Analysis Advisory) footer block
            # ---------------------------------------------------------------
            try:
                _taa_sta  = (sta or "").strip().upper()
                _taa_data = load_taa_index().get((_efp_icao or "").upper())
                if SHOW_TAA and _taa_data and _taa_sta and _taa_sta != 'ERR':
                    output += "*" * 63 + "\n"
                    output += f" ** {_taa_sta} TAA {_taa_data.get('number', '')} **\n"
                    for _tl in _taa_data.get('lines', []):
                        output += " .\n"
                        output += f" {_tl}\n"
                    output += " .\n"
                    output += " FOR CHANGES SEE RF 8001 ENG FOR EOD\n"
                    output += "*" * 63 + "\n"
            except Exception as _taa_e:
                LOG.debug(f"[TAA] footer skipped: {_taa_e}")

        return output

    except Exception as e:
        LOG.error(f"Error in write_takeoff_performance_string: {e}")
        traceback.print_exc()
        return "ERR"
