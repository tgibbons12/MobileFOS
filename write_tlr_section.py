"""
write_tlr_section.py  —  Unified takeoff/landing performance renderer
======================================================================
Produces both the legacy TPS block and the Jeppesen-format TLR block
from a SimBrief XML root.

Usage
-----
    from write_tlr_section import write_tlr_section
    howgozit += write_tlr_section(xml_root)

Returns "" if no <tlr> element is present.

Integration point (in parse_simbrief_data_to_howgozit_with_ofp):
    # replace both legacy calls with this single line:
    howgozit += write_tlr_section(root)
"""

from __future__ import annotations
import os
import json
import textwrap
import random
import xml.etree.ElementTree as ET
import re
import math as _math
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone
from typing import Optional

from SPEEDOTHER import get_speed_other, get_reduced_thrust_n1
from ENGINEFAILPROC import get_airport_specific_altitudes

_SEQ_CONFIG = "config.json"

def _get_seq_id(token: str) -> tuple:
    """Return (seq_id, revision) for the given tracking token.

    Token format: {ORIG}{DEST}{FLTNUM}{DATE}  e.g. TJSJKMCO46021MAY26
    - seq_id : stable 6-digit number assigned on first run, never changes
    - revision: increments by 1 on every call (TLR-1, TLR-2, TLR-3 ...)

    Both values are persisted in config.json → seq_registry.
    """
    try:
        with open(_SEQ_CONFIG) as _f:
            _cfg = json.load(_f)
    except Exception:
        _cfg = {}

    registry = _cfg.setdefault("seq_registry", {})
    entry = registry.get(token)

    if entry is None:
        # First time this flight is generated
        seq_id = str(random.randint(100000, 999999))
        revision = 1
    elif isinstance(entry, str):
        # Migrate old plain-string format → new dict format
        seq_id   = entry
        revision = 2
    else:
        seq_id   = entry["seq_id"]
        revision = entry["revision"] + 1

    registry[token] = {"seq_id": seq_id, "revision": revision}
    try:
        with open(_SEQ_CONFIG, "w") as _f:
            json.dump(_cfg, _f, indent=4)
    except Exception as _e:
        print(f"Warning: could not save seq registry: {_e}")

    return seq_id, revision



# ---------------------------------------------------------------------------
# Helpers ported from MASTERLOG_Jepp.py
# ---------------------------------------------------------------------------

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_numeric(val):
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def safe_weight(value):
    """Convert weight value (lbs) to thousands with 1 decimal. Returns None on failure or
    zero input so callers can distinguish 'no data' from a genuine 0-lb weight."""
    try:
        result = float(value) / 1000.0
        return result if result != 0.0 else None
    except (ValueError, TypeError):
        return None


def is_valid_runway(runway, struct_wt_limit=0.0):
    """
    Returns False if company policy excludes this runway.
    Policy: if STRUCT WT LIMIT > 100,000 lbs (100.0 thousands),
    exclude runways shorter than 4,800 ft.
    """
    if struct_wt_limit > 100.0:
        try:
            length = int(float(runway.findtext('length', '0') or 0))
            if length < 4800:
                print(f"[DEBUG] Runway {runway.findtext('identifier','?')} excluded: "
                      f"length={length}ft < 4800ft (struct_wt={struct_wt_limit:.1f}k)")
                return False
        except Exception:
            pass
    return True


def extract_runway_data(xml_root):
    """
    Extract runway data, flight info, and anti-ice status from SimBrief XML.
    Returns: (valid_runways:list, flight_info:dict, anti_ice_on:bool, runway_lines:list)
    """
    try:
        import json
        from datetime import datetime

        # --- Parse acdata_parsed JSON ---
        # --- Parse aircraft JSON ---
        acdata_tag = xml_root.find('.//acdata_parsed')
        if acdata_tag is not None and acdata_tag.text and acdata_tag.text.strip():
            try:
                acdata = json.loads(acdata_tag.text.strip())
                aircraft_name = acdata.get('name', 'UNKNOWN')
                engine_type   = acdata.get('comments', 'UNKNOWN')
                maxcargo = acdata.get('maxcargo', 0)
                print(f"DEBUG: Loaded acdata for {acdata.get('reg','UNKNOWN')} - {aircraft_name} / {engine_type}")
            except json.JSONDecodeError as e:
                print(f"ERROR parsing acdata_parsed: {e}")
                aircraft_name = 'UNKNOWN'
                engine_type = 'UNKNOWN'
        else:
            print("WARNING: acdata_parsed not found or empty")
            aircraft_name = 'UNKNOWN'
            engine_type = 'UNKNOWN'



        # --- Extract takeoff conditions ---
        conditions = xml_root.find('.//tlr/takeoff/conditions')
        if conditions is not None:
            surface_condition = conditions.findtext('surface_condition', 'dry').lower()
            flight_info = {
                'temp': conditions.findtext('temperature', '0'),
                'qnh': conditions.findtext('altimeter', '0'),
                'engine': engine_type,
                'aircraft': aircraft_name,
                'surface_condition': surface_condition,
                'wind': f"{conditions.findtext('wind_direction', '0')}/{conditions.findtext('wind_speed', '0')}",
                'tow': conditions.findtext('planned_weight', '0')
            }
        else:
            print("WARNING: Takeoff conditions missing, using defaults.")
            flight_info = {
                'temp': '0',
                'qnh': '0',
                'engine': engine_type,
                'aircraft': aircraft_name,
                'surface_condition': 'dry',
                'wind': '0/0',
                'tow': '0'
            }

        # --- Extract header fields ---
        flt_node = xml_root.find('.//flight_number')
        dte_node = xml_root.find('.//date_time')
        fin_node = xml_root.find('.//fin')
        airport_node = xml_root.find('.//airport_icao')
        alt_node = xml_root.find('.//departure_airport/elevation') or xml_root.find('.//departure_airport/altitude')
        max_elevation = float(alt_node.text) if alt_node is not None and alt_node.text else 0

        # --- Process runways ---
        valid_runways = []
        anti_ice_on = False
        runways = xml_root.findall('.//tlr/takeoff/runway')
        print(f"DEBUG: Found {len(runways)} runway elements in XML")

        # --- V-speed sanitizer ---
        def sanitize_vspeed(val):
            if not val or val.upper() in ["ERR", "XXX"]:
                return "XXX"
            try:
                num = int(val)
                return str(num) if num > 0 else "XXX"
            except:
                return "XXX"

        # Struct weight limit for runway length filtering (in thousands lbs)
        try:
            _struct_wt_limit = float(xml_root.findtext('weights/max_tow_struct', '0') or 0) / 1000.0
        except Exception:
            _struct_wt_limit = 0.0
        print(f"[DEBUG] struct_wt_limit={_struct_wt_limit:.1f}k — runway length filter {'ACTIVE' if _struct_wt_limit > 100.0 else 'inactive'}")

        for runway in runways:
            anti_ice_setting = runway.findtext('anti_ice_setting', 'OFF').upper().strip()
            if anti_ice_setting and anti_ice_setting != 'OFF':
                anti_ice_on = True

            if is_valid_runway(runway, _struct_wt_limit):
                v1 = sanitize_vspeed(runway.findtext('speeds_v1', '0'))
                vr = sanitize_vspeed(runway.findtext('speeds_vr', '0'))
                v2 = sanitize_vspeed(runway.findtext('speeds_v2', '0'))

                runway_data = {
                    'other': runway.findtext('speeds_other', '0'),
                    'other_label': runway.findtext('speeds_other_id', 'XX'),
                    'id': runway.findtext('identifier', 'XX'),
                    'v1': v1,
                    'vr': vr,
                    'v2': v2,
                    'flex': runway.findtext('flex_temperature', 'XXX'),
                    'thr': runway.findtext('thrust_setting', 'TOGA'),
                    'flaps': runway.findtext('flap_setting', 'XX'),
                    'length': runway.findtext('length', '0000'),
                    'bleed': runway.findtext('bleed_setting', '0.0'),
                    'HD': runway.findtext('headwind_component', 'XX'),
                    'gradient': safe_float(runway.findtext('gradient', 'x.x')),
                    'max_weight': safe_float(runway.findtext('max_weight', 0)),
                    'max_temp': safe_float(runway.findtext('max_temperature', 'XX')),
                    'asdr': safe_float(runway.findtext('distance_margin','0.0')),
                    'elevation': safe_float(runway.findtext('elevation')) or 'ERR',
                    'limit_code': runway.findtext('limit_code', ''),
                    'runway_message': runway.findtext('runway_message', '') or runway.findtext('message', ''),
                    'est_tow': safe_float(xml_root.findtext("weights/est_tow", 0)),
                    'atow': safe_float(xml_root.findtext("fuel/plan_takeoff", 0)),
                    'est_zfw': safe_float(xml_root.findtext("weights/est_zfw", 0)),
                    'fuel': safe_float(xml_root.findtext("fuel/plan_ramp", 0)),
                    'taxi_fuel': safe_float(xml_root.findtext("fuel/taxi", 0)),
                    'airport': conditions.findtext('airport_icao', 'ERR') if conditions is not None else 'ERR',
                    'qnh': flight_info.get('qnh','ERR'),
                    'temp': flight_info.get('temp','ERR'),
                    'surface_condition': flight_info.get('surface_condition','ERR').upper(),
                    'engine': flight_info.get('engine','ERR'),
                    'aircraft': flight_info.get('aircraft','ERR'),
                    'icaocode': flight_info.get('icaocode','ERR'),
                    'wind': flight_info.get('wind','ERR'),
                    'tow': flight_info.get('tow','ERR'),
                    'flight_number': flt_node.text.strip() if flt_node is not None and flt_node.text else 'ERR',
                    'fin': fin_node.text.strip() if fin_node is not None and fin_node.text else 'ERR',
                    'dte_time': dte_node.text.strip() if dte_node is not None and dte_node.text else datetime.utcnow().strftime("%d/%H%MZ"),
                }

                valid_runways.append(runway_data)
                print(f"DEBUG: Added runway {runway_data['id']} to valid_runways")
            else:
                print(f"DEBUG: Runway {runway.findtext('identifier', 'XX')} filtered out by is_valid_runway()")

        # --- Build runway_lines for takeoff performance section ---
        runway_lines = []
        for r in valid_runways:
            line = (
                f"{r['id']:<4} {r['flaps']:<4} {r['bleed']:<4} "
                f"{r['v1']:<4} {r['vr']:<4} {r['v2']:<4} "
                f"{r['thr']:<6} {r['flex']:<6} {r['max_weight']}{r['limit_code']:<3} "
                f"{r['other_label']:<8}{r['other']:<6}"
            )
            runway_lines.append(line)

        print(f"DEBUG: runway_lines built with {len(runway_lines)} entries")
        print(f"DEBUG: Final valid_runways count: {len(valid_runways)}")

        return valid_runways, flight_info, anti_ice_on, runway_lines

    except Exception as e:
        print(f"Error in extract_runway_data: {e}")
        import traceback
        traceback.print_exc()
        return [], {}, False, []


def write_takeoff_performance_string(flight_info, valid_runways, anti_ice_on, 
                                     runway_lines, airport_altitudes=None, max_elevation=0, icao_code="XXXX",
                                     xml_root=None):
    """
    Returns the takeoff performance section as a string, including TAKEOFF DATA, 
    runway table, airport analysis data, and optional airport notes. 
    
    Weights/fuel are divided by 1000 with 1 decimal, flex temp gets C, ALT gets ft.
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
        # AIRCRAFT TYPE DETECTION
        # ===================================================================
        icaocode = icao_code.upper().replace('-', '').replace(' ', '')
        is_737_ng  = icaocode in ['B736', 'B737', 'B738', 'B739']
        is_737_max = icaocode == 'B38M'
        is_boeing_737 = is_737_ng or is_737_max
        is_md8x = icaocode.startswith('MD8')   # MD80, MD83, MD88, MD87 etc.

        # ===================================================================
        # E-JET FLEET DETECTION
        # ===================================================================
        EJET_ICAOS = {'E170', 'E175', 'E190', 'E195', 'E290', 'E295', 'E17X', 'E19X'}
        _ejet_icao = icao_code.upper().replace('-', '').replace(' ', '')
        _is_ejet = (_ejet_icao in EJET_ICAOS
                    or _ejet_icao.startswith('E1')
                    or _ejet_icao.startswith('E2'))
        
        # Extract and format basic flight data
        if valid_runways:
            first_runway = valid_runways[0]
            aircraft_reg = first_runway.get('fin', 'N/A')
            
            # Get aircraft display name and engine type
            if aircraft_reg in AIRCRAFT_UI_NAMES:
                aircraft_display = AIRCRAFT_UI_NAMES[aircraft_reg]["name"]
                engine_type = AIRCRAFT_UI_NAMES[aircraft_reg]["engine"]
                print(f"DEBUG: Using custom name for {aircraft_reg}: {aircraft_display}")
            else:
                aircraft_display = first_runway.get('aircraft', 'UNKNOWN')
                engine_type = first_runway.get('engine', 'UNKNOWN')
                print(f"DEBUG: Aircraft {aircraft_reg} not in mapping, using XML: {aircraft_display}")
            
            # Extract flight parameters
            sta = first_runway.get('airport', 'ERR')
            pres = first_runway.get('qnh', 'ERR')
            alt = first_runway.get('elevation', 'ERR')
            flt_dte = first_runway.get('flight_number', 'ERR')
            airpl = first_runway.get('fin', 'ERR')
            dte_time = first_runway.get('dte_time', 'ERR')
            surface = first_runway.get('surface_condition', 'ERR').upper()
            temp = first_runway.get('temp', 'ERR')
            
            # Convert weights and fuel
            # ATOW (Assumed Takeoff Weight) = PTOW + 2.0 (2,000 lb margin),
            # capped at structural MTOW only — not runway-specific MTOW.
            est_tow = safe_weight(first_runway.get('est_tow', 0))

            try:
                _struct_mtow = safe_weight(float(xml_root.findtext('weights/max_tow_struct', '0') or 0))
            except Exception:
                _struct_mtow = None

            if isinstance(est_tow, float):
                atow = est_tow + 2.0
                if isinstance(_struct_mtow, float) and _struct_mtow > 0:
                    atow = min(atow, _struct_mtow)
            else:
                atow = est_tow
            est_zfw = safe_weight(first_runway.get('est_zfw', 0))
            plan_takeoff = safe_weight(first_runway.get('fuel', 0))
            taxi_fuel = safe_weight(first_runway.get('taxi_fuel', 0))
            weight_lbs = first_runway.get('est_tow', 0)  # Keep in lbs for lookups
        else:
            # Fallback values if no runways
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
        # THRUST TABLE (for Boeing 737)
        # ===================================================================
        THRUST_TABLE = {
            "B736": {"TO": 22, "TO1": 20, "TO2": 18},
            "B737": {"TO": 24, "TO1": 22, "TO2": 20},
            "B738": {"TO": 26, "TO1": 24, "TO2": 22},
            "B739": {"TO": 27, "TO1": 25, "TO2": 23},
            "B38M": {"TO": 26, "TO1": 24, "TO2": 22},
        }
        
        rwy = valid_runways[0] if valid_runways else {}
        derate_label = rwy.get('thr', '').upper().strip()
        
        effective_thrust = None
        thrust_label = "N/A"
        
        if is_boeing_737 and icaocode in THRUST_TABLE:
            # Normalise key: SimBrief sends TO/TO1/TO2 or D-TO/D-TO1/D-TO2
            # Strip D- prefix so lookup works for both variants
            key = derate_label.lstrip("D-") if derate_label.startswith("D-") else derate_label

            effective_thrust = THRUST_TABLE[icaocode].get(key) \
                            or list(THRUST_TABLE[icaocode].values())[0]

            # B738: show rated thrust in lbs (26K/24K/22K)
            # All others: show setting as-is (TO / TO1 / TO2)
            if icaocode == "B738":
                thrust_label = f"{effective_thrust}K" if effective_thrust is not None else key
            else:
                thrust_label = key or "TO"
        
        # ===================================================================
        # GLOBAL THRUST/SPEED DATA (for header section)
        # ===================================================================
        n1_pack_on = "XXX"
        n1_pack_off = "XXX"
        epr_max = "XXX"
        speed_data_dict = None
        
        # === TAKEOFF DATA PAGE ===
        output += "\n[PAGEBREAK]\n[TPS_START]\n"
        output += f"{'STA':<6} {'PRES ALT':<11} {'FLT':<8} {'AIRPL':<9} {'DTE/TIME':<10}\n"
        # Format pressure alt as integer (no .0)
        try:
            alt_disp = str(int(float(alt))) if alt not in ('ERR', '') else alt
        except Exception:
            alt_disp = alt
        output += f"{sta:<6} {alt_disp:<10}  {flt_dte:<8} {airpl:<9} {dte_time:<10}\n\n"
        _ai_label = ""
        if is_boeing_737 and thrust_label and thrust_label != "N/A":
            output += f"*** {engine_type} {thrust_label} {surface} ***\n\n"
        else:
            output += f"*** {engine_type} {surface} ***\n\n"
        
        # Weight and fuel data
        output += f"{'TEMP':<6} {'PTOW':>6} {'ATOW':>6} {'ZFW':>6} {'FUEL':>7} {'TXI FUEL':>8}\n"
        
        # Format each value with 1 decimal if float
        est_tow_str = f"{est_tow:.1f}" if isinstance(est_tow, float) else 'ERR'
        atow_str = f"{atow:.1f}" if isinstance(atow, float) else 'ERR'
        est_zfw_str = f"{est_zfw:.1f}" if isinstance(est_zfw, float) else 'ERR'
        plan_takeoff_str = f"{plan_takeoff:.1f}" if isinstance(plan_takeoff, float) else 'ERR'
        taxi_fuel_str = f"{taxi_fuel:.1f}" if isinstance(taxi_fuel, float) else 'ERR'
        
        output += f"{temp}C    {est_tow_str:>7}{atow_str:>7} {est_zfw_str:>7} "
        output += f"{plan_takeoff_str + 'P':>6}{taxi_fuel_str:>5}\n\n"
        
        # === THRUST / V-SPEED SECTION ===
        banner = "************* THRUST / V-SPEED ****************************"
        output += banner + "\n\n"
        if anti_ice_on:
            output += "  *****************\n"
            output += "   * ANTI-ICE ON *\n"
            output += "  *****************\n\n"
        
        # --- Use ICAO passed from caller ---
        print(f"[DEBUG] ICAO from parameter: {icao_code}")
        print(f"[DEBUG] Final ICAO for speed lookup: {icaocode}")
        
        # --- Weight setup ---
        print(f"[DEBUG] Weight for speed lookup: {weight_lbs} lbs")
        
        # --- Custom speed data (check if available first) ---
        if is_boeing_737:
            # Boeing uses OAT and altitude for N1 lookup
            speed_data_dict = get_speed_other(icaocode, oat=temp, altitude=alt)
            print(f"[DEBUG] N1 lookup with OAT={temp}, ALT={alt}: {speed_data_dict}")
            
            n1_pack_on = speed_data_dict.get('n1', 'XXX') if speed_data_dict else "XXX"
            if n1_pack_on != "XXX":
                try:
                    n1_pack_off = round(float(n1_pack_on) + pack_off_adj, 1)
                except (ValueError, TypeError):
                    n1_pack_off = "XXX"
        elif is_md8x:
            # MD-80 series (MD83/MD88 etc.): Get MAX EPR and speed data
            epr_max_data = get_speed_other(icaocode, oat=temp, altitude=alt_val)
            if epr_max_data and 'epr' in epr_max_data:
                epr_max = epr_max_data['epr']
            
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            print(f"[DEBUG] MD83 EPR/speed lookup: {speed_data_dict}")
        else:
            # Other aircraft use weight
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            print(f"[DEBUG] Custom speed data lookup result: {speed_data_dict}")
        
        # --- MD-80 VsR/VMM SPEEDS ---
        if is_md8x and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            vsr_val = speed_data_dict['speed'].get('VsR', 'XXX')
            vmm_val = speed_data_dict['speed'].get('VMM', 'XXX')
            
            # Get CG from speed_data_dict if available
            cg_display = speed_data_dict.get('cg', 'XX.X')
            
            output += f"      TOW CG  O/RET    MM\n"
            output += f"       {cg_display:<6} {vsr_val:<7} {vmm_val}\n\n"

        # --- AIRBUS F/S/GRN DOT (A318/A319/A320 ceo+neo, A321 ceo+neo) ---
        elif icaocode in ['A318', 'A319', 'A320', 'A321', 'A20N', 'A21N'] and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            # Get CG and trim from speed_data_dict if available
            cg_display = speed_data_dict.get('cg', 'XX.X')
            trim_display = speed_data_dict.get('trim', 'X.X')
            
            # First line: TOW CG and STAB header
            if trim_display and trim_display != 'X.X':
                output += f"      TOW CG       STAB\n"
                output += f"       {cg_display:<10} {trim_display}\n"
            else:
                output += f"      TOW CG\n"
                output += f"       {cg_display}\n"
            
            # Second section: F/S/GRN DOT speeds
            f_val = speed_data_dict['speed'].get('F', 'XXX')
            s_val = speed_data_dict['speed'].get('S', 'XXX')
            gd_val = speed_data_dict['speed'].get('GRN DOT', 'XXX')
            output += f"       F     S    GRN DOT\n"
            output += f"      {f_val:<5} {s_val:<5} {gd_val:^8}\n\n"

        # --- BOEING N1 ---
        elif speed_data_dict and 'n1' in speed_data_dict:
            # Display N1 only (no CG/trim in flight plan)
            output += f"         *MAX* N1\n"
            output += f"      BLD ON  {n1_pack_on}\n"
            output += f"      BLD OFF {n1_pack_off}\n\n"
        
        # --- OTHER AIRCRAFT (E-Jets, etc.) ---
        elif speed_data_dict and 'name' in speed_data_dict and 'speed' in speed_data_dict:
            output += f"      {speed_data_dict['name']} {speed_data_dict['speed']}\n\n"
        
        # ===================================================================
        # RUNWAY TABLE WITH PER-RUNWAY N1/EPR
        # ===================================================================
        
        # Determine THR column label based on aircraft type
        if is_md8x:
            thr_column_label = "EPR"
        elif is_boeing_737:
            thr_column_label = "N1"
        elif is_erj:
            thr_column_label = "THR"
        else:
            thr_column_label = "THR"

        # ERJ header: no BLD col, no AT col, extra V215 and VFS cols
        if is_erj:
            output += f"{'RWY':<5} {flap_label:<5} {'V1':>3} {'VR':>3} {'V2':>3} {'V215':>4} {'VFS':>4}   {thr_column_label:<6}   {'MTOW':<6}\n"
        else:
            output += f"{'RWY':<5} {flap_label:<5} {ac_label:<4} {'V1':>3} {'VR':>3} {'V2':>3}   {thr_column_label:<7} {'AT':<8}   {'MTOW':<6}\n"
        output += "-" * 58 + "\n"
        
        # Get airport code for special airport check
        airport_code = sta.strip().upper() if sta != 'ERR' else ""
        print(f"[DEBUG] Airport code for special airport check: {airport_code}")
        
        # Get OAT for temperature comparison
        oat = None
        try:
            oat = float(temp)
        except (ValueError, TypeError):
            pass

        # ===================================================================
        # WEIGHT OVERRIDE LIMITS  (computed once, applied per runway)
        # ===================================================================
        #
        # L — Landing Weight Limit
        #   The maximum TOW that will not cause the estimated landing weight
        #   to exceed the destination MLW.
        #
        #   Formula:  L_limit = MLW_dest + enroute_fuel_burn
        #
        #   Where:
        #     MLW_dest       = weights/max_ldw   (lbs, from SimBrief)
        #     enroute_burn   = fuel/enroute_burn  (lbs, trip fuel consumed
        #                       from brake release to touchdown)
        #
        #   If atow > L_limit → cap mtow at L_limit, suffix = 'L'
        #   This overrides SimBrief's limit_code regardless of what it sends.
        #
        # E — Enroute Limit  (placeholder — logic to be added)
        #   Reserved for dispatcher enroute constraints (driftdown, ETOPS,
        #   depressurisation, fuel). Will override S/T/L when applicable.
        #   suffix = 'E'
        # ===================================================================

        _l_limit_tow = None   # L limit in thousands lbs — set when PTOW - burn hits MLW ceiling
        _e_limit_tow = None   # E limit in thousands lbs (placeholder)

        if xml_root is not None:
            try:
                _mlw_lbs     = float(xml_root.findtext('weights/max_ldw',   '0') or 0)
                _fbrn_lbs    = float(xml_root.findtext('fuel/enroute_burn', '0') or 0)
                _ptow_lbs    = est_tow * 1000.0 if isinstance(est_tow, float) else 0.0
                if _mlw_lbs > 0 and _fbrn_lbs > 0 and _ptow_lbs > 0:
                    _plw_lbs = _ptow_lbs - _fbrn_lbs
                    _mlw_ceil = _mlw_lbs * 1.01
                    if _plw_lbs >= _mlw_lbs:
                        # PTOW - burn is at or above MLW → L limited
                        # L_limit_TOW uses MLW×1.01 to give the 1% dispatch margin
                        _l_limit_tow = (_mlw_ceil + _fbrn_lbs) / 1000.0
                        print(f"[DEBUG L-LIMIT] PLW={_plw_lbs:.0f} ≥ MLW={_mlw_lbs:.0f} "
                              f"→ L_limit={_l_limit_tow:.1f}k lbs (MLW×1.01 + burn)")
                    else:
                        print(f"[DEBUG L-LIMIT] PLW={_plw_lbs:.0f} < MLW={_mlw_lbs:.0f} "
                              f"→ L override not applicable")
            except Exception as _e:
                print(f"[DEBUG L-LIMIT] Calculation failed: {_e}")

        # E limit — groundwork only, no override logic yet
        # _e_limit_tow = ...  (to be implemented)

        # ── First pass: collect all runway row data ──────────────────────────
        runway_rows = []
        for line in runway_lines:
            parts = line.split()
            rwy, flap, bld, v1, vr, v2, thr, at = (parts + [""] * 8)[:8]
            
            # If any of the v-speeds came through unsanitized, force XXX
            v1 = v1 if v1.isdigit() and int(v1) > 0 else "XXX"
            vr = vr if vr.isdigit() and int(vr) > 0 else "XXX"
            v2 = v2 if v2.isdigit() and int(v2) > 0 else "XXX"

            # For Airbus, invert bleed to APU logic (bleed ON = APU OFF)
            if is_airbus:
                apu_status = 'OFF' if bld.upper() == 'ON' else 'ON'
            else:
                apu_status = bld

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
            
            matched_rwy = next((r for r in valid_runways if r['id'] == rwy), None)
            mtow_val = None
            original_limit_code = ''
            rwy_message = ""
            if matched_rwy:
                max_w_raw  = matched_rwy.get('max_weight', 0)
                raw_code   = str(matched_rwy.get('limit_code', '') or '').strip().upper()
                rwy_message = str(matched_rwy.get('runway_message', '') or '').strip()

                _CODE_MAP = {
                    'T': 'T', 'F': 'T', 'S': 'S', 'A': 'S',
                    'L': 'L', 'D': 'D', 'E': 'E', 'X': 'X', '': '',
                }
                _lookup_code = raw_code.strip()
                display_code = _CODE_MAP.get(_lookup_code, _lookup_code[:1] if _lookup_code else '')
                original_limit_code = display_code
                print(f"[DEBUG MTOW] raw_code={raw_code!r} → display_code={display_code!r}")

                try:
                    mtow_val = safe_weight(max_w_raw)
                except Exception:
                    mtow_val = None

                if _l_limit_tow is not None and isinstance(mtow_val, float):
                    if _l_limit_tow < mtow_val:
                        _orig_mtow   = mtow_val
                        mtow_val     = round(_l_limit_tow, 1)
                        display_code = 'L'
                        print(f"[DEBUG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} < mtow={_orig_mtow:.1f} → override to L")
                    else:
                        print(f"[DEBUG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} ≥ mtow={mtow_val:.1f} → no L override")

                try:
                    mtow = f"{mtow_val:.1f}{display_code}" if isinstance(mtow_val, float) else ""
                except Exception:
                    mtow = ""
            else:
                mtow = ""

            _MSG_MAP = {
                'PTOW EXCEEDS MTOW ALL FLAPS':    'PTOW EXCEEDS MTOW ALL FLAPS',
                'PTOW EXCEEDS MTOW':              'PTOW EXCEEDS MTOW - RQST NEW TPS',
                'FLAP N/A':                       'FLAP N/A THIS RWY - RQST NEW TPS',
                'WIND ADJ':                       'WIND ADJM-CALL LOADS OR COMPUTE DATA',
                'WIND ADJM':                      'WIND ADJM-CALL LOADS OR COMPUTE DATA',
            }
            if rwy_message:
                for key, display in _MSG_MAP.items():
                    if key in rwy_message.upper():
                        rwy_message = display
                        break

            at_display = ""
            at_override_occurred = False
            at_numeric = None
            if at and "TOGA" not in str(at).upper():
                try:
                    temp_val = float(re.sub(r"[^0-9.-]", "", str(at)))
                    if temp_val < 100:
                        at_numeric = temp_val
                except (ValueError, TypeError):
                    pass

            _max_thrust_codes = {'T', 'S', 'A'}
            _code_requires_max_thrust = original_limit_code in _max_thrust_codes
            # L is a planning/dispatch limit only — never affects thrust or AT display
            _is_l_limited = (display_code == 'L')

            if _is_l_limited:
                # Pass SimBrief's AT straight through unchanged
                at_display = f"{int(at_numeric)}C" if at_numeric is not None else at
                at_override_occurred = False
            elif _code_requires_max_thrust and mtow_val and isinstance(atow, float) and atow > mtow_val:
                at_display = "MAX-WT"
                at_override_occurred = True
            elif "TOGA" in str(at).upper():
                at_display = "MAX-WT"
                at_override_occurred = True
            elif airport_code in SPECIAL_AIRPORTS:
                at_display = "MAX-SPCL"
                at_override_occurred = True
                print(f"[DEBUG] Special airport detected: {airport_code}")
            elif oat is not None and at_numeric is not None and abs(oat - at_numeric) <= 5:
                at_display = "MAX-TEMP"
                at_override_occurred = True
            elif at_numeric is not None:
                at_display = f"{int(at_numeric)}C"
            else:
                at_display = "MAX-WT"
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
                            icaocode, oat=temp, altitude=alt_val, assumed_temp=int(at_numeric)
                        )
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
                    if at_numeric is not None and effective_thrust is not None:
                        reduced_n1_data = get_reduced_thrust_n1(
                            icaocode, effective_thrust, int(at_numeric), alt_val
                        )
                        if reduced_n1_data and 'n1' in reduced_n1_data:
                            thr_display = str(reduced_n1_data['n1'])
                        else:
                            thr_display = str(n1_pack_on)
                    else:
                        thr_display = str(n1_pack_on)

            elif is_airbus:
                if at_override_occurred or at_display.startswith("MAX"):
                    thr_display = "TOGA"
                else:
                    thr_display = "FLEX"

            elif is_erj:
                # ERJ: apply same thrust label mapping as raw passthrough above
                thr_display = _ERJ_THR_MAP.get(_thr_upper, thr_display)

            else:
                if at_override_occurred and thr not in ["TO-1", "TO-2", "TO-3"]:
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
                v1_bad = _is_no_data(v1)
                v2_bad = _is_no_data(v2)
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
                    print(f"[DEBUG MTOW-FALLBACK] {rwy}: using max_tow_struct={_struct_w:.1f}S")

            try:
                flap_int = int(float(flap))
                flap_fmt = f"{flap_int:02d}" if 0 < flap_int <= 9 else str(flap_int)
            except Exception:
                flap_fmt = flap

            runway_rows.append({
                'rwy': rwy, 'flap': flap, 'flap_fmt': flap_fmt,
                'apu_status': apu_status, 'v1': v1, 'vr': vr, 'v2': v2,
                'v215_display': v215_display, 'vfs_display': vfs_display,
                'thr_display': thr_display, 'at_display': at_display,
                'mtow': mtow, 'rwy_message': rwy_message,
            })

        # ── Second pass: flag improved-performance runways ───────────────────
        # SimBrief can assign the same flap setting on multiple runways but bump
        # the V-speeds upward to achieve a better climb gradient — trading runway
        # margin for climb performance. The MTOW may be identical so there is no
        # other visible indicator. We detect this by comparing VR within each flap
        # group: if a runway's VR is >5 kts above the lowest VR for that flap
        # setting, it is using improved performance and its flap label gets an X.
        from collections import defaultdict
        flap_vr_map = defaultdict(list)
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
                        this_vr = int(row['vr'])
                        min_vr  = min(vr_list)
                        if this_vr - min_vr > 5:
                            row['flap_fmt'] = row['flap_fmt'] + '*'
                    except (ValueError, TypeError):
                        pass

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

            # Write runway data row
            if rwy_message:
                # No perf data — show message spanning the speed/thr columns, but still print MTOW
                # Fixed width of 33 chars matches: V1(3) VR(3) V2(3) + spaces + THR(7) + AT(8) + spaces
                msg_field = f"{rwy_message:<33}"
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
        
        # === OPTIONAL AIRPORT NOTES ===
        airport_altitudes = get_airport_specific_altitudes(sta, max_elevation)
        if airport_altitudes:
            efp_text = airport_altitudes.get('EFP', "")
            if efp_text:
                output += "\n"
                output += "************* AIRPORT NOTES ********************************\n"
                output += f"{efp_text}\n\n"

        # === AIRPORT ANALYSIS DATA SECTION ===
        if valid_runways:
            output += "********* AIRPORT ANALYSIS DATA ****************************\n"

            from collections import OrderedDict

            PRE      = 22   # prefix width: " CONF/FLAP(4) APU/BLD(5) LIMIT(5) C(4) " = 1+4+1+5+1+5+1+4=22
            CW       = 7    # each runway column width
            MAX_RWYS = 5    # max runways per block

            def _fmt_conf(flap_val):
                s = str(flap_val).strip()
                try:
                    n = int(float(s))
                    return f"{n:02d}" if 0 < n <= 9 else str(n)
                except Exception:
                    return s

            def _sep(n):
                return "-" * (PRE + CW * n) + "\n"

            def _div(n):
                total = PRE + CW * n
                d = (" - -" * ((total // 4) + 2))[:total]
                return d + "\n"

            def _fmt_slope(sv):
                try:
                    sf = float(sv) if sv is not None else 0.0
                    if sf == 0.0:
                        return ".0"
                    s = f"{sf:.1f}"
                    if s.startswith("0."):   s = s[1:]
                    elif s.startswith("-0."): s = "-" + s[2:]
                    return s
                except Exception:
                    return "x.x"

            # Struct weight — from SimBrief's certified structural limit (weights/max_tow_struct)
            try:
                _struct_raw = xml_root.findtext('weights/max_tow_struct', '0') or '0'
                struct_wt_limit = safe_weight(float(_struct_raw))
                struct_wt_limit = struct_wt_limit if isinstance(struct_wt_limit, float) else 0.0
            except Exception:
                struct_wt_limit = 0.0
            struct_str = f"{struct_wt_limit:.1f}" if struct_wt_limit > 0 else "0.0"
            output += f"\n STRUCT WT LIMIT {struct_str}\n\n"

            def _cap_wt(val_thou):
                """Cap a weight (in thousands) at the structural weight limit."""
                if isinstance(val_thou, float) and struct_wt_limit > 0:
                    return min(val_thou, struct_wt_limit)
                return val_thou

            # E/O ACCEL height (same for all)
            eo_afl_int = 0
            if airport_altitudes:
                try:
                    eo_afl_int = int(float(airport_altitudes.get('eo_acc', '0')))
                except Exception:
                    pass

            import math as _math
            TEMP_PENALTY = 0.3
            from collections import OrderedDict as _OD

            # Build unique-runway lookup dict
            unique_rwy_by_id = {}
            for r in valid_runways:
                rid = r.get('id', 'XX')
                if rid not in unique_rwy_by_id:
                    unique_rwy_by_id[rid] = r

            # Build conf_groups once across all runways
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

            # Sort: by flap setting numerically, then APU OFF before ON
            def _conf_sort_key(c):
                try: return int(float(c))
                except: return 999
            conf_groups = _OD(
                sorted(conf_groups.items(),
                       key=lambda kv: (_conf_sort_key(kv[0][0]), 0 if kv[0][1] == 'OFF' else 1))
            )
            unique_confs = sorted(dict.fromkeys(ck for (ck, _) in conf_groups), key=_conf_sort_key)

            atow_lbs = (atow * 1000.0) if isinstance(atow, float) else 0.0

            for conf_only in unique_confs:
                pairs = [(ck, ak) for (ck, ak) in conf_groups if ck == conf_only]
                conf_rids_all = set()
                for pair in pairs:
                    conf_rids_all.update(conf_groups[pair].keys())

                # All unique runway IDs for this conf, in valid_runways order
                conf_all_ids = list(dict.fromkeys(
                    r.get('id', 'XX') for r in valid_runways if r.get('id', 'XX') in conf_rids_all
                ))

                # Chunk this conf's runways if > MAX_RWYS
                conf_id_chunks = [conf_all_ids[i:i+MAX_RWYS]
                                  for i in range(0, len(conf_all_ids), MAX_RWYS)]

                off_rid_map = conf_groups.get((conf_only, 'OFF'), {})
                on_rid_map  = conf_groups.get((conf_only, 'ON'),  {})
                base_rid_map = off_rid_map if off_rid_map else on_rid_map

                try:
                    first_r  = base_rid_map[next(iter(base_rid_map))]
                    oat_base = int(float(first_r.get('max_temp', first_r.get('temp', 23))))
                except Exception:
                    oat_base = 23

                for chunk_idx, conf_chunk_ids in enumerate(conf_id_chunks):
                    nc = len(conf_chunk_ids)

                    if chunk_idx > 0:
                        # Only print CONT. when a single flap setting overflows MAX_RWYS columns
                        output += "\n****** AIRPORT ANALYSIS DATA (CONT.) **********************\n"
                        output += f"\n STRUCT WT LIMIT {struct_str}\n\n"

                    # Per-conf column header
                    rwy_label_row = "".join(f"{'RWY':>{CW}}" for _ in conf_chunk_ids)
                    rwy_id_row    = "".join(f"{rid:>{CW}}" for rid in conf_chunk_ids)
                    output += f" {'':4} {'':5} {'CLIMB':>5} {'TEMP':>4}" + rwy_label_row + "\n"
                    output += f" {flap_label:<4} {ac_label:<5} {'LIMIT':>5} {'C':>4}" + rwy_id_row + "\n"
                    output += _sep(nc)

                    APU_ON_BOOST = 0.022

                    for apu_row, t_off in [('OFF', 0), ('OFF', 2), ('ON', 0), ('ON', 2)]:
                        conf_lbl = conf_only if (apu_row == 'OFF' and t_off == 2) else ""
                        apu_lbl  = apu_row
                        penalty  = TEMP_PENALTY if t_off == 2 else 0.0
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
                                mw = None
                            if mw is not None:
                                adj = round(max(0.0, mw - penalty), 1)
                                adj_wts.append(adj)
                                rwy_cols.append(f"{adj:>{CW}.1f}")
                            else:
                                rwy_cols.append(" " * CW)

                        if not adj_wts:
                            continue
                        climb_limit = min(adj_wts)
                        output += f" {conf_lbl:<4} {apu_lbl:<5} {climb_limit:>5.1f} {oat_display:>4}" + "".join(rwy_cols) + "\n"

                    # HDWND / TLWND
                    primary_rid_map = conf_groups.get((conf_only, 'OFF'),
                                      conf_groups.get((conf_only, 'ON'), {}))
                    hw_cols, tl_cols = [], []
                    for cid in conf_chunk_ids:
                        r = primary_rid_map.get(cid)
                        if r:
                            mw_thou = _cap_wt(safe_weight(r.get('max_weight', 0)))
                            if isinstance(mw_thou, float) and mw_thou > 0:
                                mtow_lbs   = mw_thou * 1000.0
                                margin_lbs = max(0.0, mtow_lbs - atow_lbs)

                                # Weight margin ratio (0=at limit, 1=empty runway)
                                wt_margin = margin_lbs / mtow_lbs if mtow_lbs > 0 else 0.0

                                # Distance margin ratio
                                try:
                                    rwy_length  = float(unique_rwy_by_id[cid].get('length', 0) or 0)
                                    dist_margin = float(r.get('asdr', 0) or 0)
                                    dist_ratio  = (dist_margin / rwy_length) if rwy_length > 0 else 0.0
                                except Exception:
                                    dist_ratio  = 0.0

                                # Combined margin: geometric mean of weight and distance margins.
                                # Curved with sqrt so mid-range margins don't collapse too fast.
                                combined = (wt_margin * dist_ratio) ** 0.5

                                # HDWND: base rate 0.00287 of MTOW per knot (chosen to produce
                                # naturally non-round numbers). Scales up with margin — more
                                # margin means more headwind credit available.
                                # Curve: linear between 0.6× (tight) and 1.0× (loose) of base.
                                hw_scale = 0.60 + 0.40 * min(combined * 3.0, 1.0)
                                hw_lbs   = max(100, round(mtow_lbs * 0.00287 * hw_scale))

                                # TLWND: base rate 0.00671 of MTOW per knot.
                                # Scales inversely — tight margins mean bigger tailwind penalty.
                                # Curve: linear between 1.4× (tight) and 1.0× (loose) of base.
                                # Zero if ≥30% distance margin (ample runway).
                                tlwnd_zeroed = dist_ratio >= 0.30
                                tw_scale = 1.40 - 0.40 * min(combined * 3.0, 1.0)
                                tw_lbs   = 0 if tlwnd_zeroed else max(200, round(mtow_lbs * 0.00671 * tw_scale))
                            else:
                                hw_lbs = 1
                                tw_lbs = 0
                        else:
                            hw_lbs = 1
                            tw_lbs = 0
                        hw_cols.append(f"{hw_lbs:>{CW}}")
                        tl_cols.append(f"{tw_lbs:>{CW}}")
                    output += f" {'HDWND ADD / KT':<{PRE - 1}}" + "".join(hw_cols) + "\n"
                    output += f" {'TLWND SUB / KT':<{PRE - 1}}" + "".join(tl_cols) + "\n"
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
                    output += f" {'E/O ACCEL /AFL/ FT':<{PRE - 1}}" + "".join(afl_cols) + "\n"
                    output += f" {'          /MSL/ FT':<{PRE - 1}}" + "".join(msl_cols) + "\n"
                    output += _div(nc)

                    # PLANNED WIND
                    wind_cols = []
                    for cid in conf_chunk_ids:
                        r = unique_rwy_by_id.get(cid)
                        try:
                            hw = int(round(float(r.get('HD', r.get('headwind_component', 0))))) if r else 0
                            wind_cols.append(f"{'H'+str(hw) if hw >= 0 else 'T'+str(abs(hw)):>{CW}}")
                        except Exception:
                            wind_cols.append(f"{'H0':>{CW}}")
                    output += f" {'PLANNED WIND KT':<{PRE - 1}}" + "".join(wind_cols) + "\n"
                    output += _sep(nc)

                    # LENGTH / SLOPE
                    len_cols   = "".join(f"{int(unique_rwy_by_id[cid].get('length', 0)):>{CW}}" for cid in conf_chunk_ids)
                    slope_cols = "".join(f"{_fmt_slope(unique_rwy_by_id[cid].get('gradient')):>{CW}}" for cid in conf_chunk_ids)
                    output += f" {'LENGTH - FT':<{PRE - 1}}" + len_cols + "\n"
                    output += f" {'SLOPE - PCT':<{PRE - 1}}" + slope_cols + "\n"
                    output += _sep(nc)

                # ── A/I SUB — only when temp ≤ 15°C ──────────────────────
                try:
                    first_any = next(iter(unique_rwy_by_id.values()))
                    base_temp_ai = int(float(first_any.get('max_temp', first_any.get('temp', 23))))
                except Exception:
                    base_temp_ai = 23
                if base_temp_ai <= 15:
                    try:
                        tow_lbs = float(first_any.get('est_tow', 0))
                        ai_val  = round(tow_lbs * 0.0015 / 1000, 1)
                        ai_str  = f"{ai_val:.1f}".lstrip('0') or '.0'
                    except Exception:
                        ai_str = ".0"
                    output += f" A/I ON SUB FROM CLB {ai_str} RWY {ai_str}\n"
                    output += _sep(len(unique_rwy_by_id))

            output += "END\n"

        return output

    except Exception as e:
        print(f"Error in write_takeoff_performance_string: {e}")
        import traceback
        traceback.print_exc()
        return "ERR"


# ---------------------------------------------------------------------------
# runway_index / intersection data (self-contained, no AERODATA import needed)
# ---------------------------------------------------------------------------

_runway_index_cache = None

def load_runway_index():
    """Load runway_index.dat from script directory. Cached after first load."""
    global _runway_index_cache
    if _runway_index_cache is not None:
        return _runway_index_cache

    dat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runway_index.dat')
    index = {}

    if not os.path.exists(dat_path):
        print(f"[INTXN] runway_index.dat not found at {dat_path}")
        _runway_index_cache = index
        return index

    with open(dat_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(';')
            if len(parts) < 4:
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
                rwy_base = rwy_raw
                taxiway  = None

            if taxiway is None:
                continue

            key = (icao, rwy_base)
            index.setdefault(key, []).append({'taxiway': taxiway, 'tora_ft': tora_ft})

    print(f"[INTXN] Loaded runway_index.dat: {len(index)} runway entries")
    _runway_index_cache = index
    return index


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _t(node: Optional[ET.Element], path: str, default: str = "") -> str:
    """Safe findtext with strip."""
    if node is None:
        return default
    val = node.findtext(path)
    return val.strip() if val else default


def _fmt_wind(direction: str, speed: str) -> str:
    """Format wind as DDDMxx  e.g. '216M06'."""
    try:
        return f"{int(direction):03d}M{int(speed):02d}"
    except (ValueError, TypeError):
        return f"{direction}M{speed}"


def _inhg(raw: str) -> str:
    """Return altimeter as xx.xx inches Hg."""
    try:
        val = float(raw)
        if val > 200:          # hPa → inHg
            val = val * 0.02953
        return f"{val:.2f}"
    except (ValueError, TypeError):
        return raw


def _w100(lbs_str: str) -> str:
    """Return weight as 4-digit hundreds string  e.g. 115300 → 1153."""
    try:
        return f"{int(round(float(lbs_str) / 100)):04d}"
    except (ValueError, TypeError):
        return "????"


def _ctr(label: str, width: int = 60) -> str:
    """Centred dashed header, no trailing newline."""
    label = f" {label} "
    dashes = max(0, width - len(label))
    left = dashes // 2
    right = dashes - left
    return "-" * left + label + "-" * right


# ---------------------------------------------------------------------------
# Sub-blocks
# ---------------------------------------------------------------------------

def _parse_efp_for_runway(efp_text: str, rwy_ident: str, is_first_rwy: bool):
    """Return (col_label, procedure_text) for the ACARS EFP column and special section.

    col_label values:
      ""        — no EFP applies to this runway (use DT Hxxx fallback)
      "SPECIAL" — runway has a printable procedure text (shown in special section)
      "COMPLEX" — runway has a REF 10-7 or vague SPECIAL notice (no printable detail)

    procedure_text:
      The raw per-runway procedure string when col_label == "SPECIAL", else "".

    Two EFP patterns:
      Simple/vague — airport-wide text (REF 10-7 PAGES, SPECIAL ENGINE FAILURE…, ALL RWYS:…)
                     → COMPLEX on first runway row only (or named runway); no section entry.
      Specific     — contains one or more RWxx: tokens with actual procedure text
                     → SPECIAL on matching runway rows; text printed in special section.

    Runway matching handles the shared-suffix shorthand:
        RW10L/10R:  matches 10L and 10R
        RW04L/R:    matches 04L and 04R (number re-used from left side)
        RW16L/R:    matches 16L and 16R
    """
    import re

    efp = efp_text.strip()
    if not efp:
        return "", ""

    def _is_complex(text: str) -> bool:
        """True if the EFP text is a REF 10-7 or truly vague airport-wide SPECIAL notice."""
        t = text.upper()
        if "REF 10-7" in t:
            return True
        # "SPECIAL ... EXIST ALL RUNWAYS" — truly vague, no specific runway named
        if re.search(r'\bSPECIAL\b', t) and re.search(r'\bEXIST', t):
            # If a specific runway number is named, it's a named-runway notice — not complex
            if re.search(r'\b(?:RWY?|RUNWAY)\s+\d{1,2}', t):
                return False
            return True
        return False

    # Detect whether this is a runway-specific EFP (has at least one RWxx: token)
    rw_token_re = re.compile(
        r'\bR(?:WY?)?[\s]?\d{1,2}[LRC]?(?:[/,](?:R(?:WY?)?[\s]?)?\d{0,2}[LRC])?[\s]*:',
        re.IGNORECASE
    )

    if not rw_token_re.search(efp):
        # Check for "SPECIAL ... EXIST[S] RWY xx" / "RUNWAY xx" pattern —
        # these name a specific runway without a colon, e.g.:
        #   SPECIAL ENGINE-FAILURE PROCEDURES EXIST RWY 26
        #   SPECIAL ENGINE FAILURE PROCEDURE EXIST FOR RUNWAY 07
        #   SPECIAL ENGINE FAILURE PROCEDURES EXIST RWY 31L/R
        def _norm_s(ident):
            m2 = re.match(r'^(\d+)([LRC]?)$', ident.upper())
            return (str(int(m2.group(1))) + m2.group(2)) if m2 else ident.upper()

        def _extract_named_idents(text):
            """Extract all normalised runway idents from a notice string.

            Handles:
              RWY 26         → {26}
              RWY 31L/R      → {31L, 31R}
              EXIST 01L/R, 19L/R, 28L/R   → {1L, 1R, 19L, 19R, 28L, 28R}
            """
            idents = set()
            # Pattern 1: optional RWY/RUNWAY prefix, then num+suf[/suf or /num+suf]
            token_re = re.compile(
                r'(?:\b(?:RWY?|RUNWAY)\s*)?\b(\d{1,2})([LRC]?)(?:[/](\d{1,2})?([LRC]))?(?=[,\s.]|$)',
                re.IGNORECASE
            )
            for m in token_re.finditer(text):
                n1, s1, n2r, s2 = m.groups()
                n1n = n1.lstrip("0") or "0"
                idents.add(_norm_s(n1n + s1.upper()))
                if s2:
                    n2n = (n2r.lstrip("0") or "0") if n2r else n1n
                    idents.add(_norm_s(n2n + s2.upper()))
            return idents

        # Check for named-runway notice — with or without RWY prefix
        # e.g. "EXIST RWY 26", "EXIST 01L/R, 19L/R, 28L/R"
        _exist_match = re.search(
            r'\bEXIST(?:S)?(?:\s+(?:FOR\s+)?(?:RWY?|RUNWAY))?\s+([\d][\d/LRC,\s]+)',
            efp, re.IGNORECASE
        )
        _rwy_prefix_match = re.search(
            r'\b(?:RWY?|RUNWAY)\s+\d{1,2}[LRC]?', efp, re.IGNORECASE
        )

        if _exist_match or _rwy_prefix_match:
            # Extract the runway list from whichever pattern matched
            _search_text = _exist_match.group(1) if _exist_match else efp
            named_idents = _extract_named_idents(_search_text)

            if named_idents:
                target_s = _norm_s(rwy_ident)
                if target_s in named_idents:
                    return "SPECIAL", efp.replace("\n", " ").strip().rstrip(".")
                return "", ""

        # Truly airport-wide text
        if _is_complex(efp):
            # REF 10-7 / vague notice — show on ALL runways (crew must ref manual)
            return "COMPLEX", ""
        else:
            # Airport-wide procedure text (e.g. ALL RWYS: TRK DCT ODK)
            # Show on first runway only
            if is_first_rwy:
                return "SPECIAL", efp.replace("\n", " ").strip().rstrip(".")
            return "", ""

    # ── Runway-specific (RWxx: tokens present) ───────────────────────────────
    def _norm(ident):
        m = re.match(r'^(\d+)([LRC]?)$', ident.upper())
        if m:
            return str(int(m.group(1))) + m.group(2)
        return ident.upper()

    target = _norm(rwy_ident)

    segments = re.split(
        r'(?<![/,])(?=\bR(?:WY?)?[\s]?\d{1,2}[LRC]?(?:[/,](?:R(?:WY?)?[\s]?)?\d{0,2}[LRC])?[\s]*:)',
        efp, flags=re.IGNORECASE
    )

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        token_match = re.match(
            r'\bR(?:WY?)?[\s]?(\d{1,2})([LRC]?)(?:[/,](?:R(?:WY?)?[\s]?)?(\d{1,2})?([LRC]))?[\s]*:',
            seg, re.IGNORECASE
        )
        if not token_match:
            continue

        num1, suf1, num2_raw, suf2 = token_match.groups()
        num1 = num1.lstrip("0") or "0"

        idents_in_token = [num1 + suf1.upper()]
        if suf2:
            num2 = (num2_raw.lstrip("0") or "0") if num2_raw else num1
            idents_in_token.append(num2 + suf2.upper())

        if target in idents_in_token:
            procedure = re.sub(
                r'^\bR(?:WY?)?[\s]?\d{1,2}[LRC]?(?:[/,](?:R(?:WY?)?[\s]?)?\d{0,2}[LRC]?)?[\s]*:\s*',
                "", seg, flags=re.IGNORECASE
            ).strip().rstrip(".")
            if _is_complex(procedure):
                return "COMPLEX", ""
            return "SPECIAL", procedure

    return "", ""


def _acars_runway_block(runways: list, mode: str,
                        icao: str = "",
                        intersection_data: Optional[dict] = None,
                        airport_altitudes: Optional[dict] = None) -> str:
    """ACARS RUNWAYS table.

    When intersection_data (from load_runway_index()) and icao are supplied,
    each runway is expanded to show one row per intersection group (X/Y/Z)
    plus the FULL-length row.

    RWY column  : base/suffix  e.g. "17L/X"   (FULL row shows bare ident)
    ACARS column: ACARS name   e.g. "17LX"     (FULL row shows bare ident)
    LENGTH      : available TORA in feet (intersection) or runway length (full)

    Invalid intersections (too short for rejected-takeoff stop) are shown
    with a trailing "!" in NOTES so crews can see them but know they need
    a waiver.
    """
    # ── Intersection band-grouping (mirrors AERODATA.get_intersection_groups) ─
    def _intxn_groups(icao_code: str, rwy_base: str, full_tora_ft: float,
                       stop_margin_ft: float = 0.0):
        """Return list of group dicts or [] if no data / no intersections.

        Only intersections with tora_ft >= (full_tora_ft - stop_margin_ft)
        are valid — enough runway remains to reject and stop.
        """
        if not intersection_data or full_tora_ft <= 0 or not icao_code:
            return []
        entries = intersection_data.get((icao_code.upper(), rwy_base.upper()), [])
        if not entries:
            return []

        # Drop intersections that don't leave enough stop distance
        if stop_margin_ft > 0:
            min_valid_tora = full_tora_ft - stop_margin_ft
            entries = [e for e in entries if e["tora_ft"] >= min_valid_tora]
        if not entries:
            return []

        BAND_WIDTH   = full_tora_ft * 0.10
        MAX_PER_BAND = 3
        SPLIT_GAP_FT = 400
        sorted_e = sorted(entries, key=lambda e: e["tora_ft"], reverse=True)

        bands = []
        for entry in sorted_e:
            placed = False
            for band in bands:
                if (band[0]["tora_ft"] - entry["tora_ft"]) <= BAND_WIDTH and len(band) < MAX_PER_BAND:
                    band.append(entry)
                    placed = True
                    break
            if not placed:
                bands.append([entry])

        # split oversized bands
        final = []
        for band in bands:
            if len(band) == MAX_PER_BAND:
                gap = band[-2]["tora_ft"] - band[-1]["tora_ft"]
                if gap > SPLIT_GAP_FT:
                    final.append(band[:-1])
                    final.append([band[-1]])
                    continue
            final.append(band)
        bands = final[:3]

        suffixes = ["X", "Y", "Z"]
        groups = []
        for i, band in enumerate(bands):
            most_restrictive = min(e["tora_ft"] for e in band)
            taxiways = [e["taxiway"] for e in sorted(band, key=lambda e: e["tora_ft"], reverse=True)]
            groups.append({
                "suffix":   suffixes[i],
                "id":       rwy_base.upper() + suffixes[i],
                "tora_ft":  most_restrictive,
                "taxiways": taxiways,
            })
        return groups

    # ── Build rows ─────────────────────────────────────────────────────────────
    lines = [_ctr("ACARS RUNWAYS") + "\n"]
    is_takeoff = (mode == "takeoff")

    efp_text = (airport_altitudes or {}).get("EFP", "").strip() if airport_altitudes else ""

    # special_section_rows: list of (rwy_ident, procedure_text) for SPECIAL runways only.
    # These are printed after the ACARS table in the SPECIAL ENG FAIL section.
    # COMPLEX runways (REF 10-7, vague SPECIAL notices) get "COMPLEX" in EFP col
    # but produce no section entry — crew must consult manual.
    special_section_rows: list = []   # (rwy_ident, procedure_text)
    complex_efp_text: str = ""        # airport-wide COMPLEX notice (emitted once in section)

    if is_takeoff:
        lines.append(f"{'RWY':<7} {'ACARS':<6} {'LENGTH':<7} {'PMTOW':<6} {'EFP':<10} NOTES\n")
    else:
        lines.append(f"{'RWY':<7} {'ACARS':<6} {'LENGTH':<7} {'EFP':<10} NOTES\n")

    _first_rendered = True   # tracks the first runway that actually produces a row

    for rwy_idx, rwy in enumerate(runways):
        ident = _t(rwy, "identifier")
        is_first_rwy = _first_rendered

        if is_takeoff:
            full_length_ft = _t(rwy, "length")
            pmwt           = _w100(_t(rwy, "max_weight"))
            if "?" in pmwt:
                continue  # no valid performance data — omit entire runway

            # ── EFP column ────────────────────────────────────────────────────
            # _parse_efp_for_runway returns (col_label, procedure_text):
            #   "SPECIAL" + text  → printable procedure; shown in special section
            #   "COMPLEX" + ""    → REF 10-7 or vague SPECIAL notice; crew refs manual
            #   "" + ""           → no EFP for this runway; fall back to DT Hxxx
            mag_course = _t(rwy, "magnetic_course") or ""
            try:
                _mc = int(round(float(mag_course))) % 360
                _dt_fallback = f"DT H{360 if _mc == 0 else _mc:03d}"
            except (ValueError, TypeError):
                _dt_fallback = ""

            if efp_text:
                _col_label, _proc_text = _parse_efp_for_runway(efp_text, ident, is_first_rwy)
                if _col_label == "SPECIAL":
                    efp_col = "SPECIAL"
                    # Only add to special section if not already present for this ident
                    if _proc_text and not any(r[0] == ident for r in special_section_rows):
                        special_section_rows.append((ident, _proc_text))
                elif _col_label == "COMPLEX":
                    efp_col = "SPECIAL"
                    if not complex_efp_text:   # capture full text once
                        complex_efp_text = efp_text.replace("\n", " ").strip()
                else:
                    efp_col = _dt_fallback
            else:
                efp_col = _dt_fallback

            notes = ""  # taxiway notes added on intersection rows below

            line = f"{ident:<7} {ident:<6} {full_length_ft:<7} {pmwt:<6} {efp_col:<10} {notes}"
            lines.append(line.rstrip() + "\n")
            _first_rendered = False

            # Intersection rows — only show intersections with enough stop margin
            try:
                full_tora_ft = float(full_length_ft) if full_length_ft else 0.0
            except (ValueError, TypeError):
                full_tora_ft = 0.0

            try:
                stop_margin_ft = float(_t(rwy, "distance_margin") or 0)
            except (ValueError, TypeError):
                stop_margin_ft = 0.0

            for g in _intxn_groups(icao, ident, full_tora_ft, stop_margin_ft=stop_margin_ft):
                primary_txwy = g["taxiways"][0]
                rwy_col   = f"{ident}/{primary_txwy}"
                acars_col = g["id"]
                tora_disp = str(int(round(g["tora_ft"])))
                txwy_note = "/".join(g["taxiways"]) if len(g["taxiways"]) > 1 else ""
                lines.append(
                    f"{rwy_col:<7} {acars_col:<6} {tora_disp:<7} {pmwt:<6} {efp_col:<10} {txwy_note}\n".rstrip() + "\n"
                )

        else:
            # Landing — EFP column + ILS in NOTES
            full_length_ft = _t(rwy, "length_lda") or _t(rwy, "length")
            pmwt           = _w100(_t(rwy, "max_weight_dry") or _t(rwy, "max_weight"))
            if "?" in pmwt:
                continue  # no valid performance data — omit entire runway

            # ── EFP column (same logic as takeoff) ───────────────────────────
            mag_course = _t(rwy, "magnetic_course") or ""
            try:
                _mc = int(round(float(mag_course))) % 360
                _dt_fallback = f"DT H{360 if _mc == 0 else _mc:03d}"
            except (ValueError, TypeError):
                _dt_fallback = ""

            if efp_text:
                _col_label, _proc_text = _parse_efp_for_runway(efp_text, ident, is_first_rwy)
                if _col_label == "SPECIAL":
                    efp_col = "SPECIAL"
                    if _proc_text and not any(r[0] == ident for r in special_section_rows):
                        special_section_rows.append((ident, _proc_text))
                elif _col_label == "COMPLEX":
                    efp_col = "SPECIAL"
                    if not complex_efp_text:   # capture full text once
                        complex_efp_text = efp_text.replace("\n", " ").strip()
                else:
                    efp_col = _dt_fallback
            else:
                efp_col = _dt_fallback

            ils = _t(rwy, "ils_frequency")
            notes = f"ILS {ils}" if ils else ""
            line = f"{ident:<7} {ident:<6} {full_length_ft:<7} {pmwt:<6} {efp_col:<10} {notes}"
            lines.append(line.rstrip() + "\n")
            _first_rendered = False

    # ── SPECIAL ENG FAIL TAKEOFF/MISSED APPROACH PROCEDURES section ──────────
    # Printed only when at least one runway has a printable SPECIAL procedure.
    # Format mirrors the example:
    #   ------------ SPECIAL ENG FAIL TAKEOFF PROCEDURES ------------
    #   RWY CLB VIA REACHING OR TURN FRA HOLD
    #   02L 900 LT H210 1056
    # Each runway's raw procedure text is printed as-is on its own line under
    # the header, prefixed by the runway ident, one runway per line.
    if special_section_rows or complex_efp_text:
        box_hdr = "SPECIAL ENG FAIL TAKEOFF PROCEDURES" if is_takeoff else "SPECIAL ENG FAIL MISSED APPROACH PROCEDURES"
        lines.append(_ctr(box_hdr) + "\n")
        _wrap_width = 72

        # Deduplicate: group runway idents that share identical procedure text.
        from collections import OrderedDict as _OD2
        _seen = _OD2()
        for rwy_ident, proc_text in special_section_rows:
            _key = " ".join(proc_text.split())
            _seen.setdefault(_key, []).append(rwy_ident)

        for proc_text, rwy_idents in _seen.items():
            _prefix = f"{'/ '.join(rwy_idents)} " if len(rwy_idents) > 1 else f"{rwy_idents[0]:<5} "
            _indent = " " * len(_prefix)
            _wrapped = textwrap.wrap(proc_text, width=_wrap_width - len(_prefix))
            if _wrapped:
                lines.append(_prefix + _wrapped[0] + "\n")
                for _cont in _wrapped[1:]:
                    lines.append(_indent + _cont + "\n")

        # Airport-wide COMPLEX notice — printed once, full text wrapped
        if complex_efp_text and not special_section_rows:
            for _line in textwrap.wrap(complex_efp_text, width=_wrap_width):
                lines.append(_line + "\n")

        lines.append("\n")

    return "".join(lines)


def _to_perf_table(runways: list, label: str) -> str:
    """
    One DRY/WET performance table for takeoff.
    For +2000 tables the per-runway XML already carries the correct adjusted
    speeds; we fall back to +1 kt only when no separate field exists.
    """
    is_plus2000 = "PLUS 2000" in label.upper()
    lines = [_ctr(label) + "\n"]
    lines.append(
        f"{'RWY':<4} {'MTOW':<5} {'MT':<4} {'CONFIG':<22} {'FLP':<4} "
        f"{'V1':>3} {'VR':>3} {'V2':>3} LIMIT\n"
    )

    lmap = {"A":"AFM","S":"STRUCT","T":"TIRE","B":"BRAKE",
             "C":"CLIMB","O":"OBS","E":"ENRTE","L":"LDW","F":"FLD"}

    for rwy in runways:
        ident   = _t(rwy, "identifier")
        mtow    = _w100(_t(rwy, "max_weight"))
        flex    = _t(rwy, "flex_temperature") or _t(rwy, "max_temperature")
        thrust  = _t(rwy, "thrust_setting")
        bleed   = _t(rwy, "bleed_setting")
        flap    = _t(rwy, "flap_setting")
        limit   = _t(rwy, "limit_code")
        limit_d = lmap.get(limit.upper(), limit or "AFM")
        if "?" in mtow:
            continue

        # Config string
        parts = []
        if thrust:
            parts.append(thrust.upper())
        if bleed and bleed.upper() not in ("", "N/A", "0", "0.0"):
            b = bleed.upper()
            if b in ("1", "1.0", "TRUE", "YES", "ON"):
                b = "ON"
            parts.append(f"BLEEDS {b}")
        config_str = " - ".join(parts) if parts else "TO"

        # V-speeds: use XML-provided values directly.
        # For +2000 tables SimBrief puts the corrected speeds in speeds_v1/vr/v2
        # on the same runway node; fall back to base+1 if identical to base.
        try:
            v1 = int(_t(rwy, "speeds_v1") or "0")
            vr = int(_t(rwy, "speeds_vr") or "0")
            v2 = int(_t(rwy, "speeds_v2") or "0")
        except ValueError:
            v1 = vr = v2 = 0

        if is_plus2000:
            # Try explicit +2000 speed fields first; fall back to +1
            try:
                v1p = int(_t(rwy, "speeds_v1_plus2000") or "0")
                vrp = int(_t(rwy, "speeds_vr_plus2000") or "0")
                v2p = int(_t(rwy, "speeds_v2_plus2000") or "0")
                if v1p > 0: v1 = v1p
                else: v1 += 1
                if vrp > 0: vr = vrp
                else: vr += 1
                if v2p > 0: v2 = v2p
                else: v2 += 1
            except ValueError:
                v1 += 1; vr += 1; v2 += 1

        lines.append(
            f"{ident:<4} {mtow:<5} {flex:<4} {config_str:<22} {flap:<4} "
            f"{'---' if v1<=1 else v1:>3} {'---' if vr<=1 else vr:>3} {'---' if v2<=1 else v2:>3} {limit_d}\n"
        )

    return "".join(lines)


def _to_perf_table_wet_plus2000(runways: list) -> str:
    """
    WET RWY - PTOW PLUS 2000 table.
    SimBrief stores per-runway wet+2000 speeds in speeds_v1_wet / speeds_vr_wet /
    speeds_v2_wet (or similar). Since the exact field names vary, we read the
    XML node's speeds and use them directly — the differences (e.g. 145 vs 147
    for V1 on 17L/35R) come from the XML, not from our arithmetic.
    If the XML has a <wet_runway> child element per runway, read from there;
    otherwise fall back to base speeds + 1.
    """
    label = "WET RWY - PTOW PLUS 2000 - CALM WIND"
    lines = [_ctr(label) + "\n"]
    lines.append(
        f"{'RWY':<4} {'MTOW':<5} {'MT':<4} {'CONFIG':<22} {'FLP':<4} "
        f"{'V1':>3} {'VR':>3} {'V2':>3} LIMIT\n"
    )

    lmap = {"A":"AFM","S":"STRUCT","T":"TIRE","B":"BRAKE",
             "C":"CLIMB","O":"OBS","E":"ENRTE","L":"LDW","F":"FLD"}

    for rwy in runways:
        ident   = _t(rwy, "identifier")
        mtow    = _w100(_t(rwy, "max_weight"))
        flex    = _t(rwy, "flex_temperature") or _t(rwy, "max_temperature")
        thrust  = _t(rwy, "thrust_setting")
        bleed   = _t(rwy, "bleed_setting")
        flap    = _t(rwy, "flap_setting")
        limit   = _t(rwy, "limit_code")
        limit_d = lmap.get(limit.upper(), limit or "AFM")
        if "?" in mtow:
            continue

        parts = []
        if thrust:
            parts.append(thrust.upper())
        if bleed and bleed.upper() not in ("", "N/A", "0", "0.0"):
            b = bleed.upper()
            if b in ("1", "1.0", "TRUE", "YES", "ON"):
                b = "ON"
            parts.append(f"BLEEDS {b}")
        config_str = " - ".join(parts) if parts else "TO"

        # Try explicit wet+2000 fields
        v1 = int(_t(rwy, "speeds_v1_wet_plus2000") or
                 _t(rwy, "wet_speeds_v1_plus2000")  or "0")
        vr = int(_t(rwy, "speeds_vr_wet_plus2000") or
                 _t(rwy, "wet_speeds_vr_plus2000")  or "0")
        v2 = int(_t(rwy, "speeds_v2_wet_plus2000") or
                 _t(rwy, "wet_speeds_v2_plus2000")  or "0")

        if v1 == 0:
            # Fall back: base speeds + 1 (wet+2000 is always at least base+1)
            try:
                v1 = int(_t(rwy, "speeds_v1") or "0") + 1
                vr = int(_t(rwy, "speeds_vr") or "0") + 1
                v2 = int(_t(rwy, "speeds_v2") or "0") + 1
            except ValueError:
                v1 = vr = v2 = 0

        lines.append(
            f"{ident:<4} {mtow:<5} {flex:<4} {config_str:<22} {flap:<4} "
            f"{'---' if v1<=1 else v1:>3} {'---' if vr<=1 else vr:>3} {'---' if v2<=1 else v2:>3} {limit_d}\n"
        )

    return "".join(lines)


def _mlw_envelope_block(landing_node: ET.Element) -> str:
    """FLAPS FULL - PACKS ON - NO ENROUTE ICING weight/OAT grid.

    Runways are laid out in chunks of 4 columns.  Each chunk repeats
    the runway-ID header, the OAT/length row, the three weight rows,
    and the HW/TW correction rows.  A blank line separates chunks.
    """
    COLS_PER_CHUNK = 4
    COL_WIDTH = 14   # characters per runway column

    cond    = landing_node.find("conditions")
    runways = landing_node.findall("runway")
    if not runways:
        return ""

    try:
        oat = int(float(_t(cond, "temperature") or "28"))
    except ValueError:
        oat = 28

    oat_rows = [oat - 5, oat, oat + 5]

    lines = [_ctr("FLAPS FULL - PACKS ON - NO ENROUTE ICING") + "\n"]
    lines.append("DRY RWY / WET RWY\n")

    # Split runways into chunks of COLS_PER_CHUNK
    chunks = [
        runways[i : i + COLS_PER_CHUNK]
        for i in range(0, len(runways), COLS_PER_CHUNK)
    ]

    for chunk_idx, chunk in enumerate(chunks):
        if chunk_idx > 0:
            lines.append("\n")   # blank line between chunks

        # Runway-ID header row
        col_hdr = f"{'':8}"
        for rwy in chunk:
            col_hdr += f"{_t(rwy, 'identifier'):<{COL_WIDTH}}"
        lines.append(col_hdr.rstrip() + "\n")

        # OAT label + length row
        len_row = f" {'OAT':<7}"
        for rwy in chunk:
            lda = _t(rwy, "length_lda") or _t(rwy, "length")
            len_row += f"{(lda + ' FT'):<{COL_WIDTH}}"
        lines.append(len_row.rstrip() + "\n")

        # Weight rows (OAT-5, OAT, OAT+5)
        for oat_val in oat_rows:
            marker = "/ " if oat_val == oat else "  "
            row = f"{marker}{oat_val:<6}"
            for rwy in chunk:
                mrlw = _w100(
                    _t(rwy, "max_weight_dry") or _t(rwy, "max_weight") or "0"
                )
                row += f"{mrlw}A/{mrlw}A   "
            lines.append(row.rstrip() + "\n")

        # Wind corrections — repeated for each chunk
        hw = "HW/10KT"
        tw = "TW/10KT"
        for _ in chunk:
            hw += "      0/ 0"
            tw += "      0/ 0"
        lines.append(hw.rstrip() + "\n")
        lines.append(tw.rstrip() + "\n")

    return "".join(lines)

def _landing_distance_block(landing_node: ET.Element) -> str:
    """LANDING DISTANCE table with accurate Vref interpolation."""
    lines = [_ctr("LANDING DISTANCE - FLAPS FULL - MAX MANUAL BRAKING") + "\n"]

    dry_node = landing_node.find("distance_dry")
    wet_node = landing_node.find("distance_wet")

    if dry_node is None and wet_node is None:
        lines.append("  NO LANDING DISTANCE DATA\n")
        return "".join(lines)

    cond   = landing_node.find("conditions")
    ref_wt = int(_t(dry_node, "weight") or _t(cond, "planned_weight") or "0")

    dry_act = int(_t(dry_node, "actual_distance")   or "0")
    dry_fct = int(_t(dry_node, "factored_distance") or "0")
    wet_act = int(_t(wet_node, "actual_distance")   or "0") if wet_node is not None else 0
    wet_fct = int(_t(wet_node, "factored_distance") or "0") if wet_node is not None else 0
    vref    = int(_t(dry_node, "speeds_vref")        or "0")

    # Per-1000-lb distance increments (E195 standard)
    D_DA = 23; D_WA = 31; D_DF = 36; D_WF = 45

    # Wind corrections
    HW_DA = -17; HW_WA = -22; HW_DF = -18; HW_WF = -25
    TW_DA =  49; TW_WA =  66; TW_DF =  77; TW_WF =  97

    # Build ±2 rows around ref weight
    base = (ref_wt // 1000) * 1000
    rows = []
    for delta_k in range(-2, 3):
        wt = base + delta_k * 1000
        if wt <= 0:
            continue
        steps = (wt - ref_wt) // 1000      # negative = lighter = shorter dist
        da = dry_act + steps * D_DA
        wa = wet_act + steps * D_WA
        df = dry_fct + steps * D_DF
        wf = wet_fct + steps * D_WF
        # Vref: -1 kt per 1000 lb lighter; +1 kt per 2000 lb heavier
        if steps < 0:
            vr = vref + steps
        else:
            vr = vref + steps // 2
        marker = "/" if wt == ref_wt else " "
        rows.append((marker, wt, vr, da, wa, df, wf))

    lines.append(f"{'':20} {'ACTUAL':^15} {'FACTORED':^15}\n")
    lines.append(f" {'LDW':<5} {'VREF':<5} {'DRY':<8} {'WET':<8} {'DRY':<8} WET\n")

    for marker, wt, vr, da, wa, df, wf in rows:
        wt_disp = f"{wt // 100:04d}"
        lines.append(f"{marker} {wt_disp:<4} {vr:<5} {da:<8} {wa:<8} {df:<8} {wf}\n")

    lines.append(f"HW/KT  {HW_DA:>4}    {HW_WA:>4}    {HW_DF:>4}    {HW_WF}\n")
    lines.append(f"TW/KT  {TW_DA:>4}    {TW_WA:>4}    {TW_DF:>4}    {TW_WF}\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_tlr_section(xml_root: ET.Element) -> str:
    """
    Unified TPS + TLR renderer.  Single call produces both blocks.

    Replaces the two legacy calls in parse_simbrief_data_to_howgozit_with_ofp:

        # OLD:
        #   howgozit += write_takeoff_performance_string(...)
        #   howgozit += write_tlr_section(root)
        # NEW:
        howgozit += write_tlr_section(root)

    Returns "" when the XML contains no <tlr> element.
    """
    tlr_node = xml_root.find(".//tlr")
    if tlr_node is None:
        return ""

    # ── Load intersection index once (cached after first call) ────────────────
    intersection_data = load_runway_index()

    # ── TPS block (legacy takeoff performance summary) ────────────────────────
    try:
        _origin_icao = (xml_root.findtext("origin/icao_code") or "").strip().upper()
        _max_elev    = float(xml_root.findtext(".//departure_airport/elevation") or
                             xml_root.findtext(".//departure_airport/altitude") or "0")
        _icao_code   = (xml_root.findtext("aircraft/icaocode") or "XXXX").strip()
        _valid_rwys, _flight_info, _anti_ice, _rwy_lines = extract_runway_data(xml_root)
        _apt_alts    = get_airport_specific_altitudes(_origin_icao, _max_elev)
        tps_out = write_takeoff_performance_string(
            _flight_info, _valid_rwys, _anti_ice, _rwy_lines,
            airport_altitudes=_apt_alts,
            max_elevation=_max_elev,
            icao_code=_icao_code,
            xml_root=xml_root,
        )
    except Exception as _e:
        import traceback
        print(f"[write_tlr_section] TPS block failed: {_e}")
        traceback.print_exc()
        tps_out = ""

    to_node = tlr_node.find("takeoff")
    ld_node = tlr_node.find("landing")

    # ── Flight identity ────────────────────────────────────────────────────────
    airline   = (xml_root.findtext("general/icao_airline") or "").strip()
    flt_num   = (xml_root.findtext("general/flight_number") or "").strip()
    orig_icao = (xml_root.findtext("origin/icao_code")      or "").strip()
    dest_icao = (xml_root.findtext("destination/icao_code") or "").strip()
    ac_reg    = (xml_root.findtext("aircraft/reg")          or "").strip()
    ac_name   = (xml_root.findtext("aircraft/name")         or "").strip()
    engines   = (xml_root.findtext("aircraft/engines")      or "").strip()
    bew_lbs   = (xml_root.findtext("weights/oew")           or "").strip()

    # Date/time from scheduled departure
    try:
        ts       = int(xml_root.findtext("times/sched_out") or "0")
        dep_dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_str = dep_dt.strftime("%d%b%y").upper()   # 21MAY26
        time_str = dep_dt.strftime("%H%MZ")            # 1752Z
    except Exception:
        date_str = "??MAY??"
        time_str = "????Z"

    # Sequence ID — stable numeric, tracked by flight token
    _seq_token = f"{orig_icao}{dest_icao}{flt_num}{date_str}".replace(" ", "").upper()
    seq_id, tlr_rev = _get_seq_id(_seq_token)

    bew_disp = bew_lbs if bew_lbs else "?????"
    route    = f"{orig_icao}-{dest_icao}"

    # ── Page header (single spaces — matches PDF exactly) ─────────────────────
    out  = "\n[PAGEBREAK]\n[TLR_START]\n"
    out += f"TAKEOFF AND LANDING REPORT {airline} {flt_num} {route} {date_str}\n"
    out += f"TLR-{tlr_rev} SEQ-{seq_id} {date_str} {time_str}\n"
    out += f"A/C {ac_reg} {ac_name} {engines} BEW/CG {bew_disp}/.....\n"
    out += "\n"

    # ==========================================================================
    # TAKEOFF DATA
    # ==========================================================================
    if to_node is not None:
        to_cond    = to_node.find("conditions")
        to_runways = to_node.findall("runway")

        plan_rwy  = _t(to_cond, "planned_runway")
        oat_to    = _t(to_cond, "temperature")
        wind_dir  = _t(to_cond, "wind_direction")
        wind_spd  = _t(to_cond, "wind_speed")
        qnh_to    = _inhg(_t(to_cond, "altimeter"))
        ptow_lbs  = _t(to_cond, "planned_weight")

        # MFPTW = PTOW + 3300 lbs (33 hundreds), capped at structural MTOW
        try:
            _mfptw_raw = float(ptow_lbs) + 3300
            # cap at structural max
            _struct = float(xml_root.findtext("weights/max_tow_struct") or "0")
            if _struct > 0:
                _mfptw_raw = min(_mfptw_raw, _struct)
            mfptw_disp = _w100(str(_mfptw_raw))
        except (ValueError, TypeError):
            mfptw_disp = "????"

        plan_rwy_elem = next(
            (r for r in to_runways if _t(r, "identifier") == plan_rwy),
            to_runways[0] if to_runways else None,
        )

        if plan_rwy_elem is not None:
            mrtw    = _w100(_t(plan_rwy_elem, "max_weight"))
            flap    = _t(plan_rwy_elem, "flap_setting")
            mt      = _t(plan_rwy_elem, "flex_temperature") or _t(plan_rwy_elem, "max_temperature")
            v1      = _t(plan_rwy_elem, "speeds_v1")
            vr      = _t(plan_rwy_elem, "speeds_vr")
            v2      = _t(plan_rwy_elem, "speeds_v2")
            limit_r = _t(plan_rwy_elem, "limit_code")
            thrust  = _t(plan_rwy_elem, "thrust_setting")
            bleed   = _t(plan_rwy_elem, "bleed_setting")
            flex    = _t(plan_rwy_elem, "flex_temperature")
        else:
            mrtw = flap = mt = v1 = vr = v2 = "---"
            limit_r = thrust = bleed = flex = ""

        lmap       = {"A":"AFM","S":"STRUCT","T":"TIRE","B":"BRAKE",
                      "C":"CLIMB","O":"OBS","E":"ENRTE","L":"LDW"}
        limit_disp = lmap.get(limit_r.upper(), "AFM")

        ptow_disp = _w100(ptow_lbs)
        wind_disp = _fmt_wind(wind_dir, wind_spd)

        out += "/// TAKEOFF DATA ///\n\n"
        out += (
            f"{'APT':<5} {'PRWY':<5} {'POAT':<5} {'PWIND':<8} {'PQNH':<6} "
            f"{'PMRTW':<6} {'FLP':<4} {'MT':<3} {'V1':>3} {'VR':>3} {'V2':>3} "
            f"{'PTOW':<5} {'MFPTW':<6} LIMIT\n"
        )
        out += (
            f"{orig_icao:<5} {plan_rwy:<5} {oat_to:<5} {wind_disp:<8} {qnh_to:<6} "
            f"{mrtw:<6} {flap:<4} {mt:<3} {v1:>3} {vr:>3} {v2:>3} "
            f"{ptow_disp:<5} {mfptw_disp:<6} {limit_disp}\n"
        )

        # RMKS — thrust/flex on line 1, BLEEDS ON indented to line 2 (matches PDF)
        out += "\n"
        rmk_l1 = []
        if thrust:
            rmk_l1.append(thrust.upper())
        if flex:
            rmk_l1.append(f"SEL TEMP {flex}")
        if rmk_l1:
            out += f"RMKS {' - '.join(rmk_l1)}\n"
        # Bleeds on its own indented line
        if bleed and bleed.upper() not in ("", "N/A", "0", "0.0"):
            b = bleed.upper()
            if b in ("1", "1.0", "TRUE", "YES", "ON"):
                b = "ON"
            out += f"     BLEEDS {b}\n"

        # Blank / label separator rows
        out += "\n"
        out += "---- ---- ------ ----- ------- --- --- --- --- --- -----------------\n"
        out += "RWY  OAT  WIND   QNH   MRTW    FLP V1  VR  V2  PWR CONFIG/CONDITION\n"
        out += "\n"

        # Tables — blank line between each section
        _apt_alts_to = get_airport_specific_altitudes(orig_icao, 0)
        out += _acars_runway_block(to_runways, "takeoff", icao=orig_icao, intersection_data=intersection_data, airport_altitudes=_apt_alts_to)
        out += "\n"
        out += _to_perf_table(to_runways, "DRY RWY - PTOW - CALM WIND")
        out += "\n"
        out += _to_perf_table(to_runways, "DRY RWY - PTOW PLUS 2000 - CALM WIND")
        out += "\n"
        out += _to_perf_table(to_runways, "WET RWY - PTOW - CALM WIND")
        out += "\n"
        out += _to_perf_table_wet_plus2000(to_runways)
        out += "\n"

    # ==========================================================================
    # LANDING DATA
    # ==========================================================================
    if ld_node is not None:
        ld_cond    = ld_node.find("conditions")
        ld_runways = ld_node.findall("runway")

        plan_rwy_ld = _t(ld_cond, "planned_runway")
        oat_ld      = _t(ld_cond, "temperature")
        wind_dir_ld = _t(ld_cond, "wind_direction")
        wind_spd_ld = _t(ld_cond, "wind_speed")
        raw_qnh_ld  = _t(ld_cond, "altimeter")
        pldw_lbs    = _t(ld_cond, "planned_weight")
        surface_ld  = _t(ld_cond, "surface_condition").upper()

        plan_ld_elem = next(
            (r for r in ld_runways if _t(r, "identifier") == plan_rwy_ld),
            ld_runways[0] if ld_runways else None,
        )
        mrlw_ld   = _w100(_t(plan_ld_elem, "max_weight_dry") or
                          _t(plan_ld_elem, "max_weight") or "0") \
                    if plan_ld_elem is not None else "????"
        pldw_disp = _w100(pldw_lbs)
        wind_disp_ld = _fmt_wind(wind_dir_ld, wind_spd_ld)

        # QNH display: hPa stays as integer; inHg shown as xx.xx
        try:
            qnh_val = float(raw_qnh_ld)
            qnh_disp = str(int(round(qnh_val))) if qnh_val > 200 else _inhg(raw_qnh_ld)
        except (ValueError, TypeError):
            qnh_disp = raw_qnh_ld

        flap_ld = (_t(ld_cond, "flap_setting") or "FULL").upper() or "FULL"

        out += "/// LANDING DATA ///\n\n"
        out += (
            f"{'APT':<5} {'PRWY':<5} {'POAT':<5} {'PWIND':<8} {'PQNH':<6} "
            f"{'PMRLW':<6} {'FLP':<5} {'PLDW':<5} LIMIT\n"
        )
        out += (
            f"{dest_icao:<5} {plan_rwy_ld:<5} {oat_ld:<5} {wind_disp_ld:<8} {qnh_disp:<6} "
            f"{mrlw_ld:<6} {flap_ld:<5} {pldw_disp:<5} AFM\n"
        )

        # RMKS — all caps
        if surface_ld and surface_ld not in ("DRY", ""):
            out += f"\nRMKS {surface_ld} RUNWAY\n"
        out += "\n"

        # Blank / label rows
        out += "-------- ---- ------ ----- ------- --- ----- --- -------------------\n"
        out += "RWY      OAT  WIND   QNH   MRLW    FLP VREF  PWR CONFIG/CONDITION\n"
        out += "\n"

        _apt_alts_ld = get_airport_specific_altitudes(dest_icao, 0)
        out += _acars_runway_block(ld_runways, "landing", icao=dest_icao, intersection_data=intersection_data, airport_altitudes=_apt_alts_ld)
        out += "\n"
        out += _mlw_envelope_block(ld_node)
        out += "\n"
        out += _landing_distance_block(ld_node)
        out += "\n"

    # ==========================================================================
    # End marker
    # ==========================================================================
    out += f"END TAKEOFF AND LANDING REPORT {airline} {flt_num} {route} {date_str}\n"
    out += "\n[PAGEBREAK]\n"

    return tps_out + out
