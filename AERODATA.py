import xml.etree.ElementTree as ET
import json
import tkinter as tk
from tkinter import Tk, filedialog, simpledialog, messagebox
import tkinter.ttk as ttk
from datetime import datetime
import random
import pytz
import urllib.request
import urllib.error
import ssl
import os
import platform
import subprocess
import textwrap
import re
from SPEEDOTHER import get_speed_other, get_reduced_thrust_n1

from TRIMSETTING import get_trim_setting
from ENGINEFAILPROC import get_airport_specific_altitudes

# Config file to store last used folder
CONFIG_FILE = "takeoff_perf_config.json"

# OOOI log path (written by flight_logger.lua)
OOOI_LOG_PATH = os.path.expanduser("~/Dropbox/ACARS/oooi_log.txt")

def read_oooi_log(path=None):
    """Read oooi_log.txt and return off_block time, total_fuel_lbs, and zfw_lbs."""
    if path is None:
        path = OOOI_LOG_PATH
    result = {"off_block": None, "total_fuel_lbs": None, "zfw_lbs": None}
    if not os.path.exists(path):
        return result
    with open(path) as f:
        text = f.read()
    m = re.search(r"Off Block:\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    if m:
        result["off_block"] = m.group(1)
    m = re.search(r"Fuel\s*\n-+\nTotal:\s+(\d+)\s+lbs", text)
    if m:
        result["total_fuel_lbs"] = float(m.group(1))
    m = re.search(r"ZFW:\s+(\d+)\s+lbs", text)
    if m:
        result["zfw_lbs"] = float(m.group(1))
    return result

# ====================================================================================
# TLR RAW-TEXT PARSER & INTERPOLATION ENGINE
#
# Parses the full multi-weight performance tables out of the raw TLR text block
# (the text that accompanies the <tlr_section> XML).  Tables look like:
#
#   ---- DRY RWY - PTOW - CALM WIND ----
#   RWY  MTOW  MT  CONFIG  FLP  V1  VR  V2  LIMIT
#   08R  1763  45  D-TO1 - BLEEDS ON  5  147  148  155  CLB
#   ...
#   ---- DRY RWY - PTOW PLUS 4000 - CALM WIND ----
#   ...
#   ---- WET RWY - PTOW - CALM WIND ----
#   ...
#
# After parsing, call interpolate_tlr_speeds(tlr_tables, runway_id, surface, tow_lbs)
# to get interpolated V1/VR/V2/MTOW for any arbitrary TOW.
# ====================================================================================

def parse_tlr_raw_text(raw_text):
    """
    Parse the raw TLR text block into a nested dict:

        tlr_tables[surface][condition][runway_id] = list of row dicts

    Where:
        surface    : 'DRY' | 'WET'
        condition  : 'PTOW' | 'PTOW+4000'   (the two weight columns)
        runway_id  : e.g. '08R', '27', etc.
        row dict   : { 'mtow': float (lbs),  # MTOW in lbs (raw value × 1000)
                       'mt':   int,           # assumed/flex temp
                       'config': str,
                       'flaps': str,
                       'v1': int, 'vr': int, 'v2': int,
                       'limit': str }

    The MTOW column in the TLR is in thousands of lbs (e.g. 1763 → 1,763,000 lbs).
    We keep it in those units to match the rest of the script (weights in lbs × 1000).
    """
    if not raw_text:
        return {}

    tables = {}

    section_re = re.compile(
        r'-+\s*(DRY|WET)\s+RWY\s*[-–]\s*PTOW(\s+PLUS\s+(\d+))?\s*[-–]\s*CALM\s+WIND\s*-+',
        re.IGNORECASE
    )

    row_re = re.compile(
        r'^(\w{2,3})\s+'
        r'(\d{3,4})\s+'
        r'(\d{1,3})\s+'
        r'(FLEX|D-TO\d?|TO\d?)'
        r'(\s*-\s*BLEEDS\s+ON)?'
        r'\s+(\d{1,2})\s+'
        r'(\d{2,3})\s+'
        r'(\d{2,3})\s+'
        r'(\d{2,3})\s+'
        r'(\w+)',
        re.IGNORECASE
    )

    lines = raw_text.splitlines()
    current_surface = None
    current_condition = None

    for line in lines:
        line = line.strip()

        m = section_re.search(line)
        if m:
            current_surface = m.group(1).upper()
            offset = m.group(3)  # e.g. '4000', '2000', or None
            current_condition = f'PTOW+{offset}' if offset else 'PTOW'
            tables.setdefault(current_surface, {})
            tables[current_surface].setdefault(current_condition, {})
            continue

        if current_surface is None:
            continue

        m = row_re.match(line)
        if m:
            rwy_id     = m.group(1).upper()
            mtow_raw   = int(m.group(2))
            mt         = int(m.group(3))
            config     = m.group(4).upper()
            bleeds_on  = m.group(5) is not None
            flaps      = m.group(6)
            v1         = int(m.group(7))
            vr         = int(m.group(8))
            v2         = int(m.group(9))
            limit      = m.group(10).upper()

            row = {
                'mtow':      mtow_raw,
                'mt':        mt,
                'config':    config,
                'bleeds_on': bleeds_on,
                'flaps':     flaps,
                'v1':        v1,
                'vr':        vr,
                'v2':        v2,
                'limit':     limit,
            }
            tables[current_surface][current_condition].setdefault(rwy_id, []).append(row)

    return tables


def interpolate_tlr_speeds(tlr_tables, runway_id, surface, tow, force_condition=None):
    """
    Interpolate V1/VR/V2 and pick the right config/limit for a given TOW.

    Args:
        tlr_tables  : dict returned by parse_tlr_raw_text()
        runway_id   : e.g. '08R'
        surface     : 'DRY' or 'WET'  (case-insensitive)
        tow         : planned takeoff weight in the SAME units as MTOW in the table
                      (i.e. the raw 4-digit value, e.g. 1538 for 1,538,000 lb)
        force_condition : 'PTOW' | 'PTOW+4000' | None

    Returns:
        dict with keys: v1, vr, v2, mtow, config, flaps, limit, mt, condition
        or None if lookup fails.
    """
    if not tlr_tables:
        return None

    surface = surface.upper()
    runway_id = runway_id.upper()

    surface_data = tlr_tables.get(surface)
    if not surface_data:
        surface_data = tlr_tables.get('DRY') or tlr_tables.get('WET')
    if not surface_data:
        return None

    def _lookup(condition_key):
        cond_data = surface_data.get(condition_key, {})
        rows = cond_data.get(runway_id)
        if not rows:
            for key in cond_data:
                if key.lstrip('0') == runway_id.lstrip('0'):
                    rows = cond_data[key]
                    break
        return rows

    def _interpolate_rows(rows, tow_val):
        rows_sorted = sorted(rows, key=lambda r: r['mtow'])

        if tow_val <= rows_sorted[0]['mtow']:
            result = dict(rows_sorted[0])
            result['_extrapolated'] = False
            return result

        if tow_val >= rows_sorted[-1]['mtow']:
            result = dict(rows_sorted[-1])
            result['_over_mtow'] = (tow_val > rows_sorted[-1]['mtow'])
            result['_extrapolated'] = False
            return result

        for i in range(len(rows_sorted) - 1):
            lo = rows_sorted[i]
            hi = rows_sorted[i + 1]
            if lo['mtow'] <= tow_val <= hi['mtow']:
                span = hi['mtow'] - lo['mtow']
                frac = (tow_val - lo['mtow']) / span if span else 0.0

                def interp_int(key):
                    return int(round(lo[key] + frac * (hi[key] - lo[key])))

                result = {
                    'mtow':      tow_val,
                    'mt':        interp_int('mt'),
                    'config':    hi['config'],
                    'bleeds_on': hi.get('bleeds_on', True),
                    'flaps':     hi['flaps'],
                    'v1':        interp_int('v1'),
                    'vr':        interp_int('vr'),
                    'v2':        interp_int('v2'),
                    'limit':     hi['limit'],
                    '_extrapolated': False,
                    '_over_mtow': False,
                }
                return result

        return None

    # --- Main selection logic ---
    # Discover the actual PTOW+N key (could be PTOW+2000, PTOW+4000, etc.)
    ptow_plus_key = next((k for k in surface_data if k.startswith('PTOW+')), None)

    ptow_rows      = _lookup('PTOW')
    ptow_plus_rows = _lookup(ptow_plus_key) if ptow_plus_key else None

    chosen_rows = None
    chosen_condition = None

    if force_condition:
        forced_rows = _lookup(force_condition)
        if forced_rows:
            chosen_rows = forced_rows
            chosen_condition = force_condition
        else:
            chosen_rows = ptow_rows or ptow_plus_rows
            chosen_condition = 'PTOW' if ptow_rows else ptow_plus_key
    elif ptow_rows:
        max_ptow_mtow = max(r['mtow'] for r in ptow_rows)
        if tow <= max_ptow_mtow:
            chosen_rows = ptow_rows
            chosen_condition = 'PTOW'
        elif ptow_plus_rows:
            chosen_rows = ptow_plus_rows
            chosen_condition = ptow_plus_key
        else:
            chosen_rows = ptow_rows
            chosen_condition = 'PTOW'
    elif ptow_plus_rows:
        chosen_rows = ptow_plus_rows
        chosen_condition = ptow_plus_key

    if not chosen_rows:
        return None

    result = _interpolate_rows(chosen_rows, tow)
    if result:
        result['condition'] = chosen_condition
        result['surface']   = surface
        result['runway']    = runway_id
    return result


def get_tlr_speeds_for_runway(tlr_tables, runway_id, surface, tow, force_condition=None):
    """
    Convenience wrapper. Returns a dict ready to plug into the runway dict fields,
    or None if the TLR tables are empty / the runway is not found.
    """
    result = interpolate_tlr_speeds(tlr_tables, runway_id, surface, tow, force_condition=force_condition)
    if result is None:
        return None

    if result.get('_over_mtow'):
        print(f"[TLR] WARNING: TOW {tow} exceeds highest MTOW in {surface} {result['condition']} "
              f"table for runway {runway_id}. Using max-weight row — verify with dispatch.")

    return {
        'v1':         result['v1'],
        'vr':         result['vr'],
        'v2':         result['v2'],
        'max_weight': result['mtow'],
        'limit_code': result['limit'],
        'flex':       str(result['mt']),
        'flaps':      result['flaps'],
        'config':     result['config'],
        'thr':        result['config'],
        'bleed':      'ON' if result.get('bleeds_on', True) else 'OFF',
        '_tlr_condition': result['condition'],
        '_tlr_surface':   result['surface'],
    }


# ====================================================================================
# UTILITY HELPERS
# ====================================================================================

def safe_float(value, default=0.0):
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ====================================================================================
# RUNWAY INDEX — intersection data from runway_index.dat
# Format: ICAO;RWY[_TXWY];TORA_m;TODA_m;ASDA_m;LDA_m;elev;slope
# ====================================================================================

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


def get_intersection_groups(icao, rwy_id, full_tora_ft, distance_reject_ft, index_data):
    """Return up to 3 intersection groups (X/Y/Z) for a runway."""
    if not index_data or full_tora_ft <= 0:
        return []

    rwy_base = rwy_id.upper()
    entries  = index_data.get((icao.upper(), rwy_base), [])
    if not entries:
        return []

    valid = entries
    band_width   = full_tora_ft * 0.10
    MAX_PER_BAND = 3
    SPLIT_GAP_FT = 400
    valid_sorted = sorted(valid, key=lambda e: e['tora_ft'], reverse=True)

    bands = []
    for entry in valid_sorted:
        placed = False
        for band in bands:
            band_ceil = band[0]['tora_ft']
            if (band_ceil - entry['tora_ft']) <= band_width and len(band) < MAX_PER_BAND:
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

def save_last_folder(folder):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({'last_folder': folder}, f)
    except Exception as e:
        print(f"Error saving folder: {e}")

def select_output_folder():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                saved_folder = config.get('last_folder')
                if saved_folder and os.path.exists(saved_folder):
                    print(f"Using saved output folder: {saved_folder}")
                    return saved_folder
        except Exception as e:
            print(f"Error reading saved folder: {e}")

    root = Tk()
    root.withdraw()
    folder = filedialog.askdirectory(
        title="Select Output Folder (will be saved for future runs)",
        initialdir=os.getcwd()
    )
    root.destroy()

    if not folder:
        folder = os.getcwd()
        print(f"No folder selected. Using current directory: {folder}")

    save_last_folder(folder)
    print(f"Folder saved for future runs: {folder}")
    return folder

def fetch_xml_from_api(username):
    url = f"https://www.simbrief.com/api/xml.fetcher.php?username={username}"
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, context=context) as response:
            return ET.parse(response)
    except urllib.error.URLError as e:
        print(f"Error fetching data: {e}")
        return None
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        return None

def get_xml_value(element, default="0"):
    return int(element.text) if element is not None and element.text.isdigit() else int(default)

def is_valid_runway(runway):
    max_weight = runway.findtext('max_weight', '0')
    try:
        float(max_weight) if max_weight else 0
        return True
    except ValueError:
        return False

def calculate_cargo_distribution(total_cargo):
    cargo_per_section = round(total_cargo / 2 / 200) * 200
    fwd_cargo = cargo_per_section
    aft_cargo = total_cargo - fwd_cargo
    return fwd_cargo, aft_cargo

def get_utc_time():
    return datetime.now(pytz.UTC).strftime('%H%M')

def extract_text(xml_root, tag, default=None):
    elem = xml_root.find(tag)
    if elem is not None and elem.text is not None:
        return elem.text.strip()
    return default

def get_text_global(parent, tag, default="XXX"):
    elem = parent.find(tag) if parent is not None else None
    return elem.text.strip() if elem is not None and elem.text else default


# ====================================================================================
# XML RAW PARSER — pure data extraction, no UI
# ====================================================================================

def parse_xml_raw(xml_root, date, aircraft_type):
    """Extract all raw values from SimBrief XML. No dialog, no weight calculation."""

    def get_text(parent, tag, default='0'):
        el = parent.find(tag) if parent is not None else None
        return el.text.strip() if el is not None and el.text else default

    def safe_int_local(val, default=0):
        try: return int(val)
        except: return default

    general     = xml_root.find('general')
    fuel        = xml_root.find('fuel')
    weights     = xml_root.find('weights')
    destination = xml_root.find('destination')
    alternate   = xml_root.find('alternate')
    aircraft    = xml_root.find('aircraft')
    conditions  = xml_root.find('.//conditions')

    # TLR raw text
    tlr_section_elem = xml_root.find('.//tlr_section')
    tlr_raw = tlr_section_elem.text.strip() if tlr_section_elem is not None and tlr_section_elem.text else ""
    if tlr_raw:
        print(f"[TLR] Found raw TLR text in XML ({len(tlr_raw)} chars)")
    else:
        print("[TLR] No <tlr_section> element found in XML")

    # XML planned values
    pax_count_xml  = safe_int_local(get_text(weights, 'pax_count'))
    pax_weight     = safe_int_local(get_text(weights, 'pax_weight', '190'))
    cargo_xml      = safe_int_local(get_text(weights, 'cargo'))
    plan_ramp_xml  = safe_int_local(get_text(fuel, 'plan_ramp'))
    taxi_fuel      = safe_int_local(get_text(fuel, 'taxi'))
    oew            = safe_int_local(get_text(weights, 'oew', '0'))
    enroute_burn   = safe_int_local(get_text(fuel, 'enroute_burn'))
    max_zfw        = safe_int_local(get_text(weights, 'max_zfw'))
    max_tow        = safe_int_local(get_text(weights, 'max_tow'))
    max_ldw        = safe_int_local(get_text(weights, 'max_ldw'))
    max_tow_struct = safe_int_local(get_text(weights, 'max_tow_struct'))
    est_tow_xml    = safe_int_local(get_text(weights, 'est_tow'))
    est_zfw_xml    = safe_int_local(get_text(weights, 'est_zfw'))
    plan_takeoff   = safe_int_local(get_text(fuel, 'plan_takeoff'))
    bag_count      = safe_int_local(get_text(weights, 'bag_count'))

    # Anti-ice
    first_runway = xml_root.find('.//tlr/takeoff//runway')
    anti_ice_setting = 'OFF'
    if first_runway is not None:
        ai_elem = first_runway.find('anti_ice_setting')
        if ai_elem is not None and ai_elem.text:
            anti_ice_setting = ai_elem.text.strip().upper()
    anti_ice_on = (anti_ice_setting not in ('OFF', ''))

    # Surface / conditions
    surface_condition = get_text(conditions, 'surface_condition', 'dry').lower()

    # Engine / acdata
    acdata_parsed_elem = xml_root.find('.//api_params/acdata_parsed')
    acdata = json.loads(acdata_parsed_elem.text.strip()) if acdata_parsed_elem is not None else {}
    engine_type = acdata.get('comments', 'UNKNOWN')

    # ICAO code
    icaocode = get_text(aircraft, 'icaocode', aircraft_type)

    # Runways — keep AERODATA's richer fields (v_other, v_other_id, magnetic_course, etc.)
    valid_runways = []
    for runway in xml_root.findall('.//tlr/takeoff//runway'):
        if is_valid_runway(runway):
            def get_val(tag, default='0', _rwy=runway):
                elem = _rwy.find(tag)
                return elem.text.strip() if elem is not None and elem.text is not None else default

            try:
                hd_value = float(get_val('headwind_component', '0'))
            except (ValueError, TypeError):
                hd_value = 0.0

            v_other    = get_val('v_other', None)
            v_other_id = get_val('v_other_id', None)

            if not v_other or v_other == '0' or v_other == 'None':
                try:
                    speed_result = get_speed_other(icaocode, weight=est_tow_xml)
                    if speed_result and isinstance(speed_result, dict):
                        v_other_id = speed_result.get('name', 'VFS')
                        v_other    = str(speed_result.get('speed', 'XXX'))
                    else:
                        v_other_id = 'VFS'
                        v_other    = 'XXX'
                except (TypeError, ValueError, AttributeError) as e:
                    print(f"[WARNING] SPEEDOTHER.py failed for {icaocode}: {e}")
                    v_other_id = 'VFS'
                    v_other    = 'XXX'

            v_other    = str(v_other)    if v_other    else 'XXX'
            v_other_id = str(v_other_id) if v_other_id else 'VFS'

            valid_runways.append({
                'id':             get_val('identifier', 'XX'),
                'slope':          get_val('gradient', '0'),
                'flaps':          get_val('flap_setting', ''),
                'v1':             get_val('speeds_v1', '0'),
                'vr':             get_val('speeds_vr', '0'),
                'v2':             get_val('speeds_v2', '0'),
                'v_other':        v_other,
                'v_other_id':     v_other_id,
                'vfs':            v_other,
                'headwind':       get_val('headwind_component', '0'),
                'crosswind':      get_val('crosswind_component', '0'),
                'wind_dir':       get_val('wind_direction', '0'),
                'thr':            get_val('thrust_setting', 'xxx'),
                'flex':           get_val('flex_temperature', '--'),
                'length':         get_val('length', '0'),
                'bleed':          get_val('bleed_setting', 'ON'),
                'max_weight':     int(float(get_val('max_weight', '0')) / 1000),
                'max_tow_struct': max_tow_struct / 1000,
                'elevation':      float(get_val('elevation', '0')),
                'limit_code':     get_val('limit_code', ''),
                'HD':             hd_value,
                'magnetic_course': get_val('magnetic_course', '0'),
                'internal_code':  get_val('limit_code', 'A'),
            })

    # Crew count
    crew_section = xml_root.find('crew')
    crew_count = 0
    if crew_section is not None:
        for et in ['cpt', 'fo', 'fa']:
            crew_count += len(crew_section.findall(et))
    if crew_count == 0:
        crew_count = 6

    # TLR tables
    tlr_tables = parse_tlr_raw_text(tlr_raw)
    if tlr_tables:
        total = sum(len(rd) for s in tlr_tables.values() for rd in s.values())
        print(f"[TLR] Parsed performance tables: {total} runways across surfaces {list(tlr_tables.keys())}")
    else:
        print("[TLR] No raw TLR text tables found — SimBrief XML speeds used as-is.")

    # Origin / destination / alternate
    origin_element      = xml_root.find('origin')
    destination_element = xml_root.find('destination')
    alternate_element   = xml_root.find('alternate')
    origin_icao   = get_text(origin_element,      'icao_code', 'XXX')
    origin_iata   = get_text(origin_element,      'iata_code', 'XXX')
    dest_icao     = get_text(destination_element, 'icao_code', 'XXX')
    dest_iata     = get_text(destination_element, 'iata_code', 'XXX')
    altn_icao     = get_text(alternate_element,   'icao_code', 'XXX')
    alternate_burn = safe_int_local(get_text(fuel, 'alternate_burn'))
    reserve        = safe_int_local(get_text(fuel, 'reserve'))

    xml_data = {
        # identity
        'flight_number':  get_text(general, 'flight_number', 'UNKNOWN'),
        'date':           date,
        'aircraft_type':  aircraft_type,
        'icaocode':       icaocode,
        'AC_name':        get_text(aircraft, 'name', 'XXX'),
        'base_type':      get_text(aircraft, 'base_type', 'XXX'),
        'registration':   get_text(aircraft, 'registration'),
        'fin':            get_text(aircraft, 'fin', 'UNKNOWN'),
        'engine_type':    engine_type,
        'RLS':            get_text(general, 'release', 'UNKNOWN'),
        'coroute':        origin_icao + dest_icao,
        # airports
        'origin_icao':    origin_icao,
        'origin_iata':    origin_iata,
        'dest_icao':      dest_icao,
        'dest_iata':      dest_iata,
        'altn_icao':      altn_icao,
        # weights (XML planned)
        'pax_count_xml':  pax_count_xml,
        'pax_weight':     pax_weight,
        'cargo_xml':      cargo_xml,
        'plan_ramp_xml':  plan_ramp_xml,
        'taxi_fuel':      taxi_fuel,
        'oew':            oew,
        'enroute_burn':   enroute_burn,
        'max_zfw':        max_zfw,
        'max_tow':        max_tow,
        'max_ldw':        max_ldw,
        'max_tow_struct': max_tow_struct,
        'est_tow_xml':    est_tow_xml,
        'est_zfw_xml':    est_zfw_xml,
        'plan_takeoff':   plan_takeoff,
        'bag_count':      bag_count,
        # conditions
        'surface':        surface_condition,
        'temp':           get_text(conditions, 'temperature', '0'),
        'qnh':            get_text(conditions, 'altimeter', '0'),
        'wind':           f"{get_text(conditions,'wind_direction')}/{get_text(conditions,'wind_speed')}",
        'airport_iata':   get_text(conditions, 'airport_iata', 'XXXX'),
        # nav/routing
        'cruise_fl':      safe_int_local(get_text(general, 'initial_altitude')) // 100,
        'cost_index':     safe_int_local(get_text(general, 'costindex')),
        'route':          get_text(general, 'route', ''),
        'avg_temp_dev':   get_text(general, 'avg_temp_dev', 'xx'),
        'wind_dir':       safe_int_local(get_text(general, 'avg_wind_dir')),
        'wind_spd':       safe_int_local(get_text(general, 'avg_wind_spd')),
        'wind_component': safe_int_local(get_text(general, 'avg_wind_comp')),
        'trip_distance':  safe_int_local(get_text(general, 'route_distance')),
        'altn_fuel':      alternate_burn,
        'reserve_fuel':   alternate_burn + reserve,
        'final_reserve':  reserve,
        'mtof':           safe_int_local(get_text(fuel, 'min_takeoff')),
        # runtime
        'anti_ice_on':    anti_ice_on,
        'valid_runways':  valid_runways,
        'tlr_tables':     tlr_tables,
        'crew_count':     crew_count,
        'acdata':         acdata,
    }
    return xml_data


# ====================================================================================
# BUILD WEIGHTS — compute ZFW/TOW/LDW from user inputs
# ====================================================================================

def build_weights(xml_data, pax_count, cargo, plan_ramp, cg_percent, zfw_override=None, zfw_cg_percent=None):
    """Compute ZFW/TOW/LDW from user inputs."""
    oew          = xml_data['oew']
    pax_weight   = xml_data['pax_weight']
    taxi_fuel    = xml_data['taxi_fuel']
    enroute_burn = xml_data['enroute_burn']
    max_zfw      = xml_data['max_zfw']
    max_tow      = xml_data['max_tow']
    max_ldw      = xml_data['max_ldw']
    max_tow_struct = xml_data['max_tow_struct']
    bag_count    = xml_data.get('bag_count', 0)

    if zfw_override is not None:
        zfw = zfw_override
    else:
        zfw = oew + (pax_count * pax_weight) + cargo

    tow = zfw + (plan_ramp - taxi_fuel)
    ldw = tow - enroute_burn

    lap_infants = random.randint(int(pax_count * 0.03), int(pax_count * 0.04))
    fwd_cargo, aft_cargo = calculate_cargo_distribution(cargo)

    # XML planned for delta comparison
    zfw_xml = xml_data['oew'] + (xml_data['pax_count_xml'] * pax_weight) + xml_data['cargo_xml']
    tow_xml = zfw_xml + (xml_data['plan_ramp_xml'] - taxi_fuel)

    print(f"\n=== CALCULATED WEIGHTS ===")
    print(f"ZFW: {zfw} (was {zfw_xml})")
    print(f"TOW: {tow} (was {tow_xml})")
    print(f"LDW: {ldw}")
    print(f"==================\n")

    uplink_data = {
        'flight_number': xml_data['flight_number'],
        'date':          xml_data['date'],
        'RLS':           xml_data.get('RLS', 'UNKNOWN'),
        'origin_icao':   xml_data['origin_icao'],
        'origin_iata':   xml_data['origin_iata'],
        'destination':   xml_data['dest_icao'],
        'dest_iata':     xml_data['dest_iata'],
        'altn':          xml_data['altn_icao'],
        'AC_name':       xml_data['AC_name'],
        'base_type':     xml_data.get('base_type', 'XXX'),
        'aircraft_type': xml_data['aircraft_type'],
        'registration':  xml_data['registration'],
        'icaocode':      xml_data['icaocode'],
        'coroute':       xml_data.get('coroute', ''),
        'cost_index':    xml_data['cost_index'],
        'cruise_fl':     xml_data['cruise_fl'],
        'wind_dir':      xml_data.get('wind_dir', 0),
        'wind_spd':      xml_data.get('wind_spd', 0),
        'wind_component': xml_data.get('wind_component', 0),
        'trip_distance': xml_data.get('trip_distance', 0),
        'ats_route':     xml_data['route'],
        'tc_oat':        xml_data['avg_temp_dev'],
        'taxi_fuel':     taxi_fuel,
        'trip_fuel':     enroute_burn,
        'altn_fuel':     xml_data['altn_fuel'],
        'reserve_fuel':  xml_data['reserve_fuel'],
        'final_reserve': xml_data['final_reserve'],
        'block_fuel':    plan_ramp,
        'ptof':          xml_data['plan_takeoff'],
        'mtof':          xml_data['mtof'],
        'ptow':          tow,
        'pzfw':          zfw,
        'pldw':          ldw,
        'airport':       xml_data['airport_iata'],
        'engine':        xml_data['engine_type'],
        'temp':          xml_data['temp'],
        'qnh':           xml_data['qnh'],
        'wind':          xml_data['wind'],
        'surface':       xml_data['surface'],
        'max_tows':      max_tow_struct,
    }

    loadsheet_data = {
        'Time Generated': get_utc_time(),
        'Flight Number':  xml_data['flight_number'],
        'Ship Number':    xml_data['fin'],
        'RLS':            xml_data.get('RLS', 'UNKNOWN'),
        'origin':         xml_data['origin_icao'],
        'origin_iata':    xml_data['origin_iata'],
        'Destination':    xml_data['dest_icao'],
        'destination_iata': xml_data['dest_iata'],
        'TOW':            tow,
        'MAX TOW':        max_tow,
        'MAX TOW STRUCT': max_tow_struct,
        'max_tows':       max_tow_struct,
        'FOB':            plan_ramp,
        'ZFW':            zfw,
        'OEW':            oew,
        'Passengers':     pax_count,
        'LAP':            lap_infants,
        'Bag Count':      bag_count,
        'FWD Cargo':      fwd_cargo,
        'AFT Cargo':      aft_cargo,
        'Total Cargo':    cargo,
        'ZFW AVAIL':      max_zfw - zfw,
        'TOW AVAIL':      max_tow - tow,
        'LDW AVAIL':      max_ldw - ldw,
        'PTOW':           xml_data['est_tow_xml'],
        'ZFW Change':     zfw - zfw_xml,
        'TOW Change':     tow - tow_xml,
        'PAX Change':     pax_count - xml_data['pax_count_xml'],
        'FUEL Change':    plan_ramp - xml_data['plan_ramp_xml'],
        'CARGO Change':   cargo - xml_data['cargo_xml'],
        'MAX ZFW':        max_zfw,
        'LDW':            ldw,
        'MAX LDW':        max_ldw,
        'Enroute Burn':   enroute_burn,
        'Passenger Weight': pax_weight,
        'Crew Count':     xml_data['crew_count'],
        'ZFW CG':         zfw_cg_percent if zfw_cg_percent is not None else cg_percent,
        'pax_wgt':        pax_weight,
    }

    return uplink_data, loadsheet_data


# ====================================================================================
# SANITIZE FOR iMESSAGE
# ====================================================================================

def sanitize_for_imessage(text):
    replacements = {
        '\u2019': "'", '\u2018': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\u00b0': 'deg',
        '\u00e9': 'e', '\u00e8': 'e',
        '\u00e0': 'a', '\u00e2': 'a',
        '\u00f4': 'o',
        '\u2022': '*',
        '\u00b1': '+/-',
        '\u2192': '->',
        '\xa0': ' ',
    }
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
    return text.encode('ascii', errors='replace').decode('ascii').replace(b'?'.decode(), '?')


# ====================================================================================
# GENERATE COMBINED OUTPUT — unchanged from AERODATA output format
# ====================================================================================

def generate_combined_output(loadsheet_data, uplink_data, valid_runways, anti_ice_on,
                              taxi_fuel, output_folder, cg_percent, acdata_parsed,
                              tlr_tables=None, tlr_scenario_active=False, force_tlr_condition=None, sc_extra_fuel=0):
    bag_weight = acdata_parsed['bagwgt']

    AIRCRAFT_UI_NAMES = {
        "N123NA": {"name": "A319 CFM", "engine": "CFM56-5B5"},
        "N456NA": {"name": "A319 CFM SHARKLET", "engine": "CFM56-5B5/P"},
        "N200NA": {"name": "A320 CFM", "engine": "CFM56-5B4/P"},
        "N210NA": {"name": "A320 IAE", "engine": "IAE V2527-A5"},
        "N300NA": {"name": "A320NEO", "engine": "PW1127G-JM"},
        "N400NA": {"name": "A321 CFM", "engine": "CFM56-5B3/P"},
        "N724NC": {"name": "A321", "engine": "IAE SHARKLET"},
        "N500NA": {"name": "A321NEO", "engine": "PW1133G-JM"},
        "N700NA": {"name": "B737-800", "engine": "CFM56-7B27"},
        "N800NA": {"name": "B737 MAX 8", "engine": "CFM LEAP-1B"},
        "N900NA": {"name": "E175", "engine": "GE CF34-8E"},
    }

    def safe_val(val, default="---"):
        try:
            return str(int(float(val)))
        except (ValueError, TypeError):
            return default

    # ── TLR interpolation (applied before writing) ──────────────────────────
    tow_for_interp     = loadsheet_data.get("TOW", 0) + sc_extra_fuel
    surface_for_interp = uplink_data.get("surface", "dry").upper()
    tow_scaled         = tow_for_interp / 100.0

    if tlr_tables and tlr_scenario_active:
        updated_runways = []
        for rwy in valid_runways:
            rwy_id = rwy.get("id", "")
            tlr_override = get_tlr_speeds_for_runway(
                tlr_tables, rwy_id, surface_for_interp, tow_scaled,
                force_condition=force_tlr_condition
            )
            if tlr_override:
                merged = dict(rwy)
                merged["v1"]         = tlr_override["v1"]
                merged["vr"]         = tlr_override["vr"]
                merged["v2"]         = tlr_override["v2"]
                merged["max_weight"] = tlr_override["max_weight"] / 100.0
                merged["limit_code"] = tlr_override["limit_code"]
                merged["flaps"]      = tlr_override["flaps"]
                merged["flex"]       = tlr_override["flex"]
                merged["thr"]        = tlr_override["thr"]
                merged["bleed"]      = tlr_override["bleed"]
                updated_runways.append(merged)
                print(f"[TLR] RWY {rwy_id}: V1={tlr_override['v1']} VR={tlr_override['vr']} "
                      f"V2={tlr_override['v2']} MTOW={tlr_override['max_weight']} "
                      f"LIMIT={tlr_override['limit_code']}")
            else:
                print(f"[TLR] RWY {rwy_id}: no TLR match — keeping SimBrief XML speeds.")
                updated_runways.append(rwy)
        valid_runways = updated_runways

    # Create file path
    base_filename = (
        f"{loadsheet_data['Flight Number']}_{uplink_data['origin_icao']}"
        f"_{datetime.now().strftime('%Y%m%d')}_COMBINED.txt"
    )
    combined_file = os.path.join(output_folder, base_filename)

    icaocode    = uplink_data.get("icaocode", "XXXX")
    trim_data   = get_trim_setting(icaocode, cg_percent)
    cg_display  = f"{cg_percent:.1f}" if cg_percent is not None else ""

    # Fuel variance check — tolerance comes from the PTOW+N offset in TLR tables
    try:
        fuel_change = abs(float(loadsheet_data.get('FUEL Change', 0)))
        # Derive tolerance from PTOW+N key (e.g. 'PTOW+12000' -> 12000); fall back to 2000
        _fuel_tol = 2000
        if tlr_tables:
            _surface_data = tlr_tables.get('DRY') or tlr_tables.get('WET') or {}
            _ptow_key = next((k for k in _surface_data if k.startswith('PTOW+')), None)
            if _ptow_key:
                try:
                    _fuel_tol = int(_ptow_key.split('+')[1])
                except (IndexError, ValueError):
                    pass
        _fuel_tol = max(_fuel_tol, 1000)
        if fuel_change > _fuel_tol:
            with open(combined_file, 'w') as file:
                file.write("**** THIS TPS DOES NOT SATISFY THE ****\n")
                file.write("*** REQUIREMENTS OF A LOAD CLOSEOUT ***\n\n")
                file.write("*** NOTIFICATION MESSAGE ***\n")
                file.write("TAKEOFF DATA REJECTED BY FMC, ACTUAL FUEL\n")
                file.write("ONBOARD DIFFERS FROM PLANNED AND EXCEEDS\n")
                file.write("TOLERANCES. REQUEST TAKEOFF DATA WHEN\n")
                file.write("FUELING IS COMPLETE\n")
                file.write("AUTOMATED FLT OPS MESSAGE\n\n")
            print(f"\n⚠️ FUEL VARIANCE EXCEEDS {_fuel_tol} LBS - DATA NOT GENERATED")
            return None
    except (ValueError, TypeError) as e:
        print(f"[DEBUG] Fuel variance check skipped: {e}")

    rwy = valid_runways[0] if valid_runways else {'id': 'XX', 'length': 'XXXX', 'elevation': 0}
    airport_elevation = float(rwy.get('elevation', 0))
    airport_icao      = uplink_data.get('origin_icao', 'XXXX')
    airport_altitudes = get_airport_specific_altitudes(airport_icao, airport_elevation)

    sta      = uplink_data.get('origin_icao', 'XXXX')
    temp     = uplink_data.get('temp', 'XX')
    trim_display = trim_data.get('trim', 'X.X') if trim_data else 'X.X'

    with open(combined_file, 'w') as f:
        # ==========================================
        # SECTION 1: TAKEOFF DATA
        # ==========================================

        fn       = str(loadsheet_data['Flight Number'])
        tail     = str(loadsheet_data['Ship Number'])
        rls_no   = str(loadsheet_data['RLS'])
        time_gen = loadsheet_data['Time Generated']
        wind     = uplink_data['wind']
        temp     = uplink_data['temp']
        qnh      = uplink_data['qnh']
        tow_lbs  = loadsheet_data['TOW']
        zfw_lbs  = loadsheet_data['ZFW']
        fob_lbs  = loadsheet_data['FOB']
        tow_k    = tow_lbs / 1000
        zfw_k    = zfw_lbs / 1000
        pax      = loadsheet_data['Passengers']
        lap      = loadsheet_data['LAP']
        total_cargo = loadsheet_data['Total Cargo']
        zfw_cg   = loadsheet_data.get('ZFW CG', cg_percent)

        _W = 24  # fixed right edge — all lines terminate at col 24

        def _rj(left, right, width=_W):
            """Right-justify: pad between left and right so total = width."""
            gap = width - len(str(left)) - len(str(right))
            return str(left) + ' ' * max(1, gap) + str(right)

        # ── Header: FLT / RLS / TIME ──────────────────────────────────────
        f.write(_rj("FLT      RLS",  "TIME") + "\n")
        f.write(_rj(f"{fn:<9}{rls_no}", f"{time_gen}Z") + "\n")
        # ── Wind / OAT / QNH ─────────────────────────────────────────────
        f.write(_rj("WIND     OAT C", "QNH") + "\n")
        try:
            oat_int = int(float(temp))
        except (ValueError, TypeError):
            oat_int = 0
        f.write(_rj(f"{wind:<9}{oat_int}", qnh) + "\n")
        # ── SECT A/B/C ────────────────────────────────────────────────────
        fwd_cargo = loadsheet_data['FWD Cargo']
        aft_cargo = loadsheet_data['AFT Cargo']
        fwd_pax = max(0, round(pax * 0.25))
        aft_pax = pax - fwd_pax
        f.write(_rj("SECT A   F CGO",  "GTOW/CG") + "\n")
        f.write(_rj(f"{fwd_pax:<9}{fwd_cargo}", f"{tow_k:.1f}/{cg_display}") + "\n")
        f.write(_rj("SECT B   A CGO",  "ZFW/CG") + "\n")
        f.write(_rj(f"{aft_pax:<9}{aft_cargo}", f"{zfw_k:.1f}/{zfw_cg:.1f}") + "\n")
        f.write(_rj("SECT C   FOB",    "TOT PAX") + "\n")
        f.write(_rj(f"{lap:<9}{fob_lbs}", pax) + "\n")
        # ── REMARKS block ─────────────────────────────────────────────────
        remarks = ["REMARKS"]
        ptow_xml     = loadsheet_data.get('PTOW', tow_lbs)
        pzfw_xml     = loadsheet_data.get('MAX ZFW', zfw_lbs)
        ptow_planned = uplink_data.get('ptow', tow_lbs)
        pzfw_planned = uplink_data.get('pzfw', zfw_lbs)
        zfw_pct_diff  = abs((zfw_lbs - pzfw_planned) / pzfw_planned * 100) if pzfw_planned else 0
        gtow_pct_diff = abs((tow_lbs - ptow_planned) / ptow_planned * 100) if ptow_planned else 0
        if zfw_lbs < pzfw_planned and zfw_pct_diff > 5:
            remarks.append("CAUTION - ZFW LESS THAN")
            remarks.append("PZFW BY MORE THAN 5 PCT")
        if tow_lbs < ptow_planned and gtow_pct_diff > 5:
            remarks.append("CAUTION - GTOW LESS THAN")
            remarks.append("PTOW BY MORE THAN 5 PCT")
        surface_raw = uplink_data.get('surface', 'dry').upper()
        if surface_raw and surface_raw != 'DRY':
            remarks.append(f"{surface_raw} RUNWAY")
        if anti_ice_on:
            remarks.append("ENGINE AND WING ANTI-ICE")
            remarks.append("ON")
        remarks.append("NO LIVE")
        remarks.append(f"TKT INF:0 * LAP INF:{lap}")
        for rl in remarks:
            f.write(rl + "\n")
        f.write("\n")
        f.write("\n")

        # ── Takeoff performance block ─────────────────────────────────────
        base_type       = uplink_data.get('base_type', 'XXXX')
        third_col_label = "ECS" if base_type in ("E170", "E175", "E190", "E195") else "BLD"
        efp_text        = (airport_altitudes.get('EFP', '') if airport_altitudes else '').strip()
        _icaocode  = uplink_data.get('icaocode', '')
        is_boeing  = _icaocode.startswith('B')
        is_airbus  = _icaocode.startswith('A')
        _ERJ_TYPES = {'E135', 'E140', 'E145', 'E45X'}
        is_erj     = _icaocode.upper().replace('-', '').replace(' ', '') in _ERJ_TYPES
        ptow_k         = round(loadsheet_data.get('PTOW', 0) / 1000, 1)
        max_tow_struct = loadsheet_data.get('MAX TOW STRUCT', 0) / 1000
        trim_str       = trim_data['trim'] if trim_data else '   '

        if trim_data:
            _trim_val = str(trim_data.get('trim', '')).strip()
            _trim_dir = str(trim_data.get('direction', '')).strip()
            trim_stab = f"{_trim_dir} {_trim_val}".strip() if _trim_dir else _trim_val
        else:
            trim_stab = '---'

        def _get_n1(rwy_data, uplink, loadsheet):
            try:
                tow_lbs_n1 = loadsheet.get('TOW', 0)
                temp_n1    = uplink.get('temp', '0')
                icao_n1    = uplink.get('icaocode', '')
                flex_n1    = rwy_data.get('flex', '')
                result_n1  = get_reduced_thrust_n1(icao_n1, tow_lbs_n1, temp_n1, flex_n1)
                if result_n1 and str(result_n1).replace('.', '').isdigit():
                    return f"{float(result_n1):.1f}"
            except Exception:
                pass
            return None

        def _ensure_msl(raw, elev, min_agl=800):
            v = int(raw)
            return v if v > int(elev) + min_agl else int(elev) + v

        for rwy_idx, rwy in enumerate(valid_runways):
            rwy_elevation = float(rwy.get('elevation', airport_elevation))
            rwy_fallback  = int(rwy_elevation + 1000)
            if airport_altitudes:
                _acc_raw = airport_altitudes.get('acc') or airport_altitudes.get('accel') or rwy_fallback
                _eo_raw  = airport_altitudes.get('eo_acc') or airport_altitudes.get('eo') or rwy_fallback
                _tr_raw  = airport_altitudes.get('tr') or rwy_fallback
                rwy['acc_alt'] = str(_ensure_msl(_acc_raw, rwy_elevation))
                rwy['eo_acc']  = str(_ensure_msl(_eo_raw,  rwy_elevation))
                rwy['tr']      = str(_ensure_msl(_tr_raw,  rwy_elevation))
            else:
                rwy.setdefault('acc_alt', str(rwy_fallback))
                rwy.setdefault('eo_acc',  str(rwy_fallback))
                rwy.setdefault('tr',      str(rwy_fallback))

            rwy_len = int(float(rwy.get("length", 0)))
            mc      = int(float(rwy.get("magnetic_course", 0)))
            slope   = float(rwy.get("slope", "0") or "0")
            print(f"[DEBUG] RWY {rwy_idx+1}: STA={sta}, ID={rwy['id']}, Len={rwy_len}, MC={mc}")

            if rwy_idx > 0:
                f.write("\n\n")

            # Common values
            v1_val       = int(float(rwy['v1']))
            vr_val       = int(float(rwy['vr']))
            v2_val       = safe_val(rwy.get('v2', 0))
            mrtw_k       = round(rwy['max_weight'], 1)
            mtow_str     = f"{max_tow_struct:.1f}"
            gtow_cg_str  = f"{ptow_k:.1f}/{cg_percent:.1f}"
            mrtw_lim_str = f"{mrtw_k:.1f}/{rwy['limit_code']:1s}"
            n1_str       = _get_n1(rwy, uplink_data, loadsheet_data)
            flex_str     = str(rwy['flex'])
            flap_str     = str(rwy['flaps'])
            bleed_str    = rwy.get('bleed', 'ON')
            thr_str      = rwy.get('thr', '')
            acc_alt      = rwy.get('acc_alt', 'XXXX')
            tr_alt       = rwy.get('tr', acc_alt)
            eo_alt       = rwy.get('eo_acc', acc_alt)

            if base_type == 'DH8D':
                v_label   = 'VCL'
                v_val_raw = rwy.get('v_other') or rwy.get('vfs') or 'XXX'
            else:
                v_label   = rwy.get('v_other_id', 'VFS')
                v_val_raw = rwy.get('v_other') or rwy.get('vfs') or 'XXX'
            try:
                v_val = int(float(v_val_raw)) if v_val_raw not in ['XXX', 'None', None, ''] else None
            except (TypeError, ValueError):
                v_val = None

            fra_code   = airport_altitudes.get('fra', '') if airport_altitudes else ''
            C1 = 8
            _hdr_width = 18

            # Runway header + intersection
            full_tora        = int(float(rwy.get('_full_tora_ft', rwy_len)))
            _intxn_txwy_list = rwy.get('_intxn_taxiways', [])
            _is_intxn        = (full_tora > rwy_len) and bool(_intxn_txwy_list)

            if is_airbus:
                _ab_txwy   = f"/{_intxn_txwy_list[0]}" if _is_intxn else ""
                _ab_rwy_id = f"{rwy['id']}{_ab_txwy}"
                _rt_label  = "SPECIAL" if efp_text else f"DT H{mc:03d}"
                f.write(f"{sta} {_ab_rwy_id:<{18 - len(sta) - 1 - len(_rt_label)}}{_rt_label}\n")
            else:
                _rwy_hdr_icao = airport_icao  # 4-char ICAO; rwy_len starts at col 19
                _rwy_id_pad   = max(1, 18 - len(_rwy_hdr_icao))
                f.write(f"{_rwy_hdr_icao} {rwy['id']:<{_rwy_id_pad}}{rwy_len}\n")
                if _is_intxn:
                    _txwy_str = '/'.join(_intxn_txwy_list)
                    f.write(f"INTXN TXWY {_txwy_str}\n" if _txwy_str else "INTXN\n")

            # ── AIRBUS FORMAT ─────────────────────────────────────────────
            if is_airbus:
                _is_toga   = flex_str.strip() in ('--', '', 'TOGA')
                _thr_label = "TOGA" if _is_toga else "FLEX"
                f.write(f"{_thr_label} - BLEEDS ON\n")

                trim_val = trim_stab.split()[-1] if trim_stab and trim_stab != '---' else '0.0'
                try:
                    flap_int     = int(flap_str)
                    flap_display = f"*{flap_int}*" if flap_int not in (1, 5) else str(flap_int)
                except (ValueError, TypeError):
                    flap_display = f"*{flap_str}*"
                flap_trim = f"{flap_display}/UP{trim_val}"
                flex_disp = f"FLEX {flex_str}"

                _hw = int(float(rwy.get('headwind', 0) or 0))
                _xw = int(float(rwy.get('crosswind', 0) or 0))
                try:
                    _wdir    = float(uplink_data.get('wind_dir', 0) or 0)
                    _mc      = float(rwy.get('magnetic_course', 0) or 0)
                    _rel     = (_wdir - _mc) % 360
                    _xw_side = 'L' if _rel > 180 else 'R'
                except (ValueError, TypeError):
                    _xw_side = 'R'
                if _hw != 0 or _xw != 0:
                    _hw_label   = 'H' if _hw >= 0 else 'T'
                    v_other_col = f"{_hw_label}{abs(_hw):02d} {_xw_side}{_xw:02d}"
                else:
                    v_other_col = ""

                shift_dist = full_tora - rwy_len if _is_intxn else None
                _W = 24  # fixed total width — matches "TR nnnn ACC nnnn EO nnnn" line width, same convention as Embraer's _ERJ_W

                def _rj_line(val, right):
                    v   = str(val)
                    gap = max(1, _W - len(v) - len(right))
                    return v + ' ' * gap + right

                _v1_right = f"TO SHIFT [{shift_dist:>4}]" if _is_intxn else "TO SHIFT [    ]"
                f.write("V1\n")
                f.write(_rj_line(v1_val, _v1_right) + "\n")
                _vr_right = f"FL/THS {flap_trim}"
                f.write("VR\n")
                f.write(_rj_line(vr_val, _vr_right) + "\n")
                _v2_right = f"{v_other_col}  {flex_disp}" if v_other_col else flex_disp
                f.write("V2\n")
                f.write(_rj_line(v2_val, _v2_right) + "\n")
                f.write(f"\nTR {tr_alt} ACC {tr_alt} EO {tr_alt}\n")

            # ── BOEING FORMAT ─────────────────────────────────────────────
            elif is_boeing:
                _B2 = 10  # Boeing middle col — wide enough for MRTW/LIM (8 chars) + space
                if efp_text:
                    fra_tag = f"FRA {fra_code}" if fra_code else ''
                    dt_line = f"{'SPECIAL':<{C1 + _B2 - len(fra_tag)}}{fra_tag}"
                    f.write(dt_line + "\n")
                else:
                    _dt_lt = 'LT' if slope >= 0 else 'DT'
                    f.write(f"{_dt_lt} H{mc:03d}  OAT{int(float(temp)):>4}   {slope: .2f}\n")

                to_label = thr_str.replace('D-TO', 'TO').strip() or 'TO'
                f.write(f" {to_label} - BLD ON\n")
                sel_oat = f"{flex_str}/{int(float(temp))}"
                f.write(f"{'FLAPS':<{C1}}{'MRTW/LIM':<{_B2}}V1 {v1_val}\n")
                f.write(f"  {flap_str:<{C1-2}}{mrtw_lim_str:<{_B2}}VR {vr_val}\n")
                f.write(f"{'STAB':<{C1}}{'MTOW':<{_B2}}V2 {v2_val}\n")
                if n1_str:
                    f.write(f"{trim_stab:<{C1}}{mtow_str:<{_B2}}\n")
                    f.write(f"{'SEL/OAT':<{C1}}{'PTOW/CG':<{_B2}}N1\n")
                    f.write(f"{sel_oat:<{C1}}{gtow_cg_str:<{_B2}}{n1_str}\n")
                else:
                    f.write(f"{trim_stab:<{C1}}{mtow_str:<{_B2}}\n")
                    f.write(f"{'SEL/OAT':<{C1}}{'PTOW/CG':<{_B2}}\n")
                    f.write(f"{sel_oat:<{C1}}{gtow_cg_str:<{_B2}}\n")
                f.write("\n")

                f.write("\n")

            # ── ERJ FORMAT ───────────────────────────────────────────────
            elif is_erj:
                if efp_text:
                    fra_tag = f"FRA {fra_code}" if fra_code else ''
                    dt_line = f"{'SPECIAL':<{_hdr_width - len(fra_tag)}}{fra_tag}"
                    f.write(dt_line + "\n")
                else:
                    _dt_lt = 'LT' if slope >= 0 else 'DT'
                    f.write(f"{_dt_lt} H{mc:03d}  OAT{int(float(temp)):>4}   {slope: .2f}\n")

                _ERJ_THR_MAP = {
                    'TO': 'TO1', 'ATO': 'TO1', 'ALT TO-1': 'ATO',
                    'ALT TO1': 'ATO', 'ALT TO': 'ATO',
                    'TO-1': 'TO1', 'TO1': 'TO1', 'TO-2': 'TO2', 'TO2': 'TO2',
                }
                thr_erj = _ERJ_THR_MAP.get(thr_str.upper().strip(), thr_str)
                f.write(f" {thr_erj}\n")

                _ERJ_W = 24  # fixed total width — all right-col values share the same right edge

                def _erj_rj(left, right, width=_ERJ_W):
                    gap = width - len(str(left)) - len(str(right))
                    return str(left) + ' ' * max(1, gap) + str(right)

                f.write(_erj_rj("FLEX    MRTW/LIM", f"V1 {v1_val}") + "\n")
                f.write(_erj_rj(f"{flex_str:<8}{mrtw_lim_str}", f"VR {vr_val}") + "\n")

                vfs_val = rwy.get('vfs') or rwy.get('v_other')
                try:
                    vfs_int = int(float(vfs_val)) if vfs_val not in (None, '', 'XXX', 'None') else None
                except (TypeError, ValueError):
                    vfs_int = None
                f.write(_erj_rj("FLAP      MTOW", f"V2 {v2_val}") + "\n")
                if vfs_int is not None:
                    f.write(_erj_rj(f"{flap_str:<10}{mtow_str}", f"VFS {vfs_int}") + "\n")
                else:
                    f.write(f"{flap_str:<10}{mtow_str}\n")

                f.write(_erj_rj("STAB    GTOW/CG", "ACCEL") + "\n")
                f.write(_erj_rj(f"{trim_stab:<8}{gtow_cg_str}", acc_alt) + "\n")

                f.write("\n")

            # ── FALLBACK FORMAT ───────────────────────────────────────────
            else:
                if efp_text:
                    fra_tag = f"FRA {fra_code}" if fra_code else ''
                    dt_line = f"{'SPECIAL':<{_hdr_width - len(fra_tag)}}{fra_tag}"
                    f.write(dt_line + "\n")
                else:
                    _dt_lt = 'LT' if slope >= 0 else 'DT'
                    f.write(f"{_dt_lt} H{mc:03d}  OAT{int(float(temp)):>4}   {slope: .2f}\n")

                f.write(f" FLEX - {thr_str:<5} - {third_col_label:<3} {bleed_str:<4}\n")

                f.write(f"{'FLEX':<8}{'MRTW/LIM':<10}V1 {v1_val}\n")
                f.write(f"{flex_str:<8}{mrtw_lim_str:<10}VR {vr_val}\n")

                f.write(f"{'FLAP':<10}{'MTOW':<8}V2 {v2_val}\n")
                if v_val is not None:
                    f.write(f"{flap_str:<10}{mtow_str}   {v_label} {v_val}\n")
                else:
                    f.write(f"{flap_str:<10}{mtow_str:<8}\n")

                f.write(f"{'STAB':<8}{'GTOW/CG':<11}ACCEL\n")
                f.write(f"{trim_stab:<8}{gtow_cg_str}   {acc_alt}\n")

                f.write("\n")

        # ── EFP block — written once, after all runways, regardless of format ──
        if efp_text and valid_runways:
            last_rwy = valid_runways[-1]

            if is_airbus:
                _special_tag = "SPECIAL"
                _id_pad2     = _hdr_width - len(sta) - 1 - len(_special_tag)
                _efp_hdr     = f"{sta} {last_rwy['id']:<{_id_pad2}}{_special_tag}"
                f.write("\n")
                f.write(_efp_hdr + "\n")
                for _ln in textwrap.fill(efp_text, width=_hdr_width).splitlines():
                    f.write(_ln + "\n")
                f.write("\n")

            elif is_boeing:
                _B2 = 10
                _special_tag = "SPECIAL"
                _efp_hdr = f"{sta} {last_rwy['id']:<{C1 + _B2 - len(sta) - 1 - len(_special_tag)}}{_special_tag}"
                f.write(_efp_hdr + "\n")
                for _ln in textwrap.fill(efp_text, width=C1 + _B2).splitlines():
                    f.write(_ln + "\n")
                f.write("\n")

            else:
                # ERJ and fallback formats: leading-space wrap, no separate header line
                for _ln in textwrap.fill(efp_text, width=_hdr_width).splitlines():
                    f.write(f" {_ln}\n")
                f.write("\n")

    print(f"Combined data generated: {combined_file}")

    # Sanitize for iMessage
    try:
        with open(combined_file, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
        clean = sanitize_for_imessage(raw)
        with open(combined_file, 'w', encoding='ascii', errors='replace') as f:
            f.write(clean)
    except Exception as e:
        print(f"[WARNING] iMessage sanitize step failed: {e}")

    return combined_file


# ====================================================================================
# MAIN APPLICATION WINDOW — XP-themed, persistent, matches TAKEOFF_PERF style
# ====================================================================================

class AppWindow:
    """
    Persistent XP-themed window for AERODATA generation.
    Inputs: TLR scenario, conditions, runway, weights, CG.
    Output: single COMBINED file (takeoff + loadsheet).
    No closeout mode.
    """

    def __init__(self, root, xml_data, output_folder):
        self.root          = root
        self.xml_data      = xml_data
        self.output_folder = output_folder

        self.root.title("AERODATA")
        self.root.geometry("780x900")
        self.root.resizable(True, True)
        self.root.configure(bg="#ECE9D8")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._on_runway_change()
        self._update_zfw_display()
        self._update_perf_preview()

    def _on_close(self):
        self.root.destroy()

    # ------------------------------------------------------------------
    def _build_ui(self):
        # ── XP PALETTE ───────────────────────────────────────────────────
        XP_BG       = "#ECE9D8"
        XP_PANEL    = "#D4D0C8"
        XP_TITLE_BG = "#0A246A"
        XP_TITLE_FG = "#FFFFFF"
        XP_FG       = "#000000"
        XP_ENTRY_BG = "#FFFFFF"
        XP_ENTRY_FG = "#000000"
        XP_GREEN    = "#007000"
        XP_RED      = "#CC0000"
        XP_DISABLED = "#808080"
        XP_BTN_BG   = "#D4D0C8"
        XP_MONO     = "Courier New"
        XP_FONT     = ("Tahoma", 9)
        XP_FONT_B   = ("Tahoma", 9, "bold")
        XP_FONT_SM  = ("Tahoma", 8)

        self.root.configure(bg=XP_BG)

        def xp_frame(parent, **kw):
            return tk.Frame(parent, bg=XP_PANEL, relief="raised", bd=2, **kw)

        def xp_label(parent, text, bold=False, fg=XP_FG, **kw):
            f = XP_FONT_B if bold else XP_FONT
            return tk.Label(parent, text=text, font=f, bg=XP_PANEL, fg=fg, **kw)

        def xp_entry(parent, width=14, **kw):
            return tk.Entry(parent, font=("Tahoma", 9), width=width,
                            bg=XP_ENTRY_BG, fg=XP_ENTRY_FG,
                            insertbackground=XP_ENTRY_FG, relief="sunken", bd=2, **kw)

        def xp_check(parent, text, var, cmd=None, **kw):
            return tk.Checkbutton(parent, text=text, variable=var, command=cmd,
                                  font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                                  activebackground=XP_PANEL, selectcolor=XP_ENTRY_BG,
                                  relief="flat", **kw)

        def xp_section_header(parent, title):
            hdr = tk.Frame(parent, bg=XP_TITLE_BG)
            hdr.pack(fill="x", padx=0, pady=(6, 0))
            tk.Label(hdr, text=f"  {title}", font=("Tahoma", 9, "bold"),
                     bg=XP_TITLE_BG, fg=XP_TITLE_FG, anchor="w").pack(fill="x", ipady=2)

        # ── ROOT OUTER LAYOUT ─────────────────────────────────────────────
        title_bar = tk.Frame(self.root, bg=XP_TITLE_BG, height=28)
        title_bar.pack(fill="x", side="top")
        tk.Label(title_bar, text="  ✈  AERODATA Generator",
                 font=("Tahoma", 11, "bold"), bg=XP_TITLE_BG, fg=XP_TITLE_FG,
                 anchor="w").pack(side="left", fill="y")

        self.status_var = tk.StringVar(value="Ready")
        status_bar = tk.Frame(self.root, bg=XP_PANEL, relief="sunken", bd=1)
        status_bar.pack(fill="x", side="bottom", padx=0, pady=0)
        tk.Label(status_bar, textvariable=self.status_var, font=XP_FONT_SM,
                 bg=XP_PANEL, fg=XP_FG, anchor="w", padx=6).pack(fill="x")

        # ── PANED WINDOW: left=form, right=preview ────────────────────────
        paned = tk.PanedWindow(self.root, orient="horizontal",
                               bg=XP_BG, sashrelief="raised", sashwidth=5)
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        left_outer = tk.Frame(paned, bg=XP_BG)
        paned.add(left_outer, minsize=360, width=390)

        canvas = tk.Canvas(left_outer, bg=XP_BG, highlightthickness=0)
        vsb = tk.Scrollbar(left_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        form = tk.Frame(canvas, bg=XP_BG)
        fw = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(fw, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        right_frame = tk.Frame(paned, bg=XP_BG)
        paned.add(right_frame, minsize=280)

        prev_hdr = tk.Frame(right_frame, bg=XP_TITLE_BG)
        prev_hdr.pack(fill="x")
        tk.Label(prev_hdr, text="  Output Preview (AERODATA)", font=("Tahoma", 9, "bold"),
                 bg=XP_TITLE_BG, fg=XP_TITLE_FG, anchor="w").pack(fill="x", ipady=2)

        preview_frame = tk.Frame(right_frame, bg=XP_PANEL, relief="sunken", bd=2)
        preview_frame.pack(fill="both", expand=True, padx=2, pady=2)
        preview_vsb = tk.Scrollbar(preview_frame, orient="vertical")
        preview_vsb.pack(side="right", fill="y")
        self.txt_preview = tk.Text(preview_frame, font=(XP_MONO, 10), bg="#FFFFF0",
                                   fg="#000080", relief="flat", bd=0, wrap="none",
                                   state="disabled", yscrollcommand=preview_vsb.set,
                                   width=38)
        self.txt_preview.pack(fill="both", expand=True)
        preview_vsb.config(command=self.txt_preview.yview)

        # ════════════════════════════════════════════════════════════════
        # SECTION 1 — TLR SCENARIO & CONDITIONS
        # ════════════════════════════════════════════════════════════════
        sec1 = xp_frame(form)
        sec1.pack(fill="x", padx=6, pady=4)
        xp_section_header(sec1, "TLR Scenario & Conditions")

        inner1 = tk.Frame(sec1, bg=XP_PANEL)
        inner1.pack(fill="x", padx=6, pady=4)

        # TLR scenario radios
        tlr_tables = self.xml_data.get('tlr_tables', {})
        self.scenario_var  = tk.StringVar(value="PLANNED")
        self.scenario_vars = {}

        scenario_lbl = tk.Frame(inner1, bg=XP_PANEL)
        scenario_lbl.pack(fill="x")
        tk.Label(scenario_lbl, text="TLR Scenario:", font=XP_FONT_B,
                 bg=XP_PANEL, fg=XP_FG, anchor="w").pack(side="left")

        tk.Radiobutton(inner1, text="Planned  (SimBrief XML speeds)",
                       variable=self.scenario_var, value="PLANNED",
                       command=self._on_scenario_change,
                       font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                       activebackground=XP_PANEL, selectcolor=XP_ENTRY_BG,
                       relief="flat").pack(anchor="w", padx=8)

        # Build scenario options dynamically from whatever conditions exist in TLR tables
        scenario_defs = []
        for surface in ('DRY', 'WET'):
            if surface not in tlr_tables:
                continue
            for condition in sorted(tlr_tables[surface].keys()):
                if condition == 'PTOW':
                    label = f"{surface} — PTOW  (calm wind)"
                else:
                    # e.g. 'PTOW+4000' → 'DRY — PTOW +4000 lbs'
                    offset = condition.split('+', 1)[1] if '+' in condition else ''
                    label = f"{surface} — PTOW +{offset} lbs"
                scenario_defs.append((surface, condition, label))
        # Fall back to the standard four if TLR tables are empty
        if not scenario_defs:
            scenario_defs = [
                ('DRY', 'PTOW',      'DRY — PTOW  (calm wind)'),
                ('DRY', 'PTOW+4000', 'DRY — PTOW +4000 lbs'),
                ('WET', 'PTOW',      'WET — PTOW  (calm wind)'),
                ('WET', 'PTOW+4000', 'WET — PTOW +4000 lbs'),
            ]
        for surface, condition, label in scenario_defs:
            available = surface in tlr_tables and condition in tlr_tables[surface]
            key   = f"{surface}_{condition}"
            state = "normal" if available else "disabled"
            txt   = label if available else label + "  (not in TLR)"
            tk.Radiobutton(inner1, text=txt, variable=self.scenario_var, value=key,
                           command=self._on_scenario_change,
                           font=XP_FONT, bg=XP_PANEL,
                           fg=XP_FG if available else XP_DISABLED,
                           activebackground=XP_PANEL, selectcolor=XP_ENTRY_BG,
                           relief="flat", state=state).pack(anchor="w", padx=8)
            self.scenario_vars[key] = (surface, condition)

        tk.Frame(inner1, height=1, bg="#A0A0A0", relief="sunken").pack(fill="x", pady=6)

        # Conditions override
        self.cond_override_var = tk.BooleanVar(value=False)
        xp_check(inner1, "Override Conditions (manual)", self.cond_override_var,
                 cmd=self._toggle_cond_overrides).pack(anchor="w")

        cond_grid = tk.Frame(inner1, bg=XP_PANEL)
        cond_grid.pack(fill="x", padx=16, pady=2)
        cond_fields = [
            ("OAT (°C)",      "temp", self.xml_data.get('temp', '')),
            ("QNH (in Hg)",   "qnh",  self.xml_data.get('qnh', '')),
            ("Wind (ddd/ss)", "wind", self.xml_data.get('wind', '')),
        ]
        self.cond_overrides = {}
        for i, (lbl_txt, key, defval) in enumerate(cond_fields):
            tk.Label(cond_grid, text=lbl_txt, font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                     width=14, anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            ent = xp_entry(cond_grid, width=12, state="disabled")
            ent.grid(row=i, column=1, sticky="w", padx=4)
            ent.insert(0, str(defval))
            ent.bind("<KeyRelease>", lambda e: self._update_perf_preview())
            self.cond_overrides[key] = ent

        self.anti_ice_var = tk.BooleanVar(value=self.xml_data.get('anti_ice_on', False))
        self.anti_ice_cb = xp_check(inner1, "Anti-Ice ON", self.anti_ice_var,
                                    cmd=self._update_perf_preview, state="disabled")
        self.anti_ice_cb.pack(anchor="w", padx=16, pady=2)

        # ════════════════════════════════════════════════════════════════
        # SECTION 2 — RUNWAY & V-SPEED DATA
        # ════════════════════════════════════════════════════════════════
        sec2 = xp_frame(form)
        # pack deferred — sec3.pack() calls sec2.pack() so Pax/Cargo appears first
        xp_section_header(sec2, "Runway, Speeds & V-Speed Data")

        inner2 = tk.Frame(sec2, bg=XP_PANEL)
        inner2.pack(fill="x", padx=6, pady=4)
        inner2.columnconfigure(1, weight=1)

        rwy_ids = [r['id'] for r in self.xml_data['valid_runways']]
        self.rwy_overrides = {}   # rwy_id → {field → str value}  (string cache, not Entry widgets)
        self.preview_vars  = {}
        self._intxn_groups = []
        self._intxn_invalid_choices = set()

        r2 = 0

        # ── RUNWAY CHECKBOXES (wrapping grid, max 4 per row) ─────────────
        cb_lbl = tk.Frame(inner2, bg=XP_PANEL)
        cb_lbl.grid(row=r2, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        tk.Label(cb_lbl, text="Include Runway(s):", font=XP_FONT_B,
                 bg=XP_PANEL, fg=XP_FG).pack(side="left", padx=(0, 6))
        tk.Button(cb_lbl, text="All", font=("Tahoma", 7), bg=XP_BTN_BG, fg=XP_FG,
                  relief="raised", bd=1, padx=3, pady=0,
                  command=self._rwy_select_all).pack(side="left", padx=(4, 2))
        tk.Button(cb_lbl, text="None", font=("Tahoma", 7), bg=XP_BTN_BG, fg=XP_FG,
                  relief="raised", bd=1, padx=3, pady=0,
                  command=self._rwy_select_none).pack(side="left")
        r2 += 1

        self.runway_checks = {}   # rwy_id → BooleanVar
        _COLS_PER_ROW = 4
        cb_grid = tk.Frame(inner2, bg=XP_PANEL)
        cb_grid.grid(row=r2, column=0, columnspan=2, sticky="ew", padx=8, pady=(2, 4))
        for ci, rid in enumerate(rwy_ids):
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(cb_grid, text=rid, variable=var,
                           font=("Tahoma", 9, "bold"), bg=XP_PANEL, fg=XP_FG,
                           activebackground=XP_PANEL, selectcolor=XP_ENTRY_BG,
                           relief="flat", command=self._on_runway_change
                           ).grid(row=ci // _COLS_PER_ROW, column=ci % _COLS_PER_ROW,
                                  sticky="w", padx=4, pady=1)
            self.runway_checks[rid] = var
        r2 += 1

        # Divider
        tk.Frame(inner2, height=1, bg="#A0A0A0").grid(
            row=r2, column=0, columnspan=2, sticky="ew", pady=(4, 4))
        r2 += 1

        # ── ACTIVE PERF RUNWAY dropdown ───────────────────────────────────
        tk.Label(inner2, text="Edit Runway Perf:", font=XP_FONT_B,
                 bg=XP_PANEL, fg=XP_FG, width=18, anchor="w").grid(
            row=r2, column=0, sticky="w", pady=2)
        self.runway_var = tk.StringVar(value=rwy_ids[0] if rwy_ids else "")
        self.runway_menu = ttk.Combobox(inner2, textvariable=self.runway_var,
                                        values=rwy_ids, state="readonly", width=10,
                                        font=("Tahoma", 9))
        self.runway_menu.grid(row=r2, column=1, sticky="w", padx=4, pady=2)
        self.runway_menu.bind("<<ComboboxSelected>>", self._on_runway_change)
        r2 += 1

        # ── INTERSECTION dropdown ─────────────────────────────────────────
        tk.Label(inner2, text="Intersection:", font=XP_FONT,
                 bg=XP_PANEL, fg=XP_FG, width=18, anchor="w").grid(
            row=r2, column=0, sticky="w", pady=2)
        self.intersection_var = tk.StringVar(value="FULL")
        self.intersection_menu = ttk.Combobox(inner2, textvariable=self.intersection_var,
                                              values=["FULL"], state="readonly", width=26,
                                              font=("Tahoma", 9))
        self.intersection_menu.grid(row=r2, column=1, sticky="w", padx=4, pady=2)
        self.intersection_var.trace_add('write', lambda *_: self._update_perf_preview())
        r2 += 1

        # ── COLLAPSIBLE "Edit Runway Perf" panel ──────────────────────
        self._active_rwy_overrides = {}   # key → Entry widget

        _perf_toggle_row = tk.Frame(inner2, bg=XP_PANEL)
        _perf_toggle_row.grid(row=r2, column=0, columnspan=2, sticky="ew", pady=(4,0))
        r2 += 1
        self._perf_toggle_btn = tk.Button(
            _perf_toggle_row, text="▶  Edit Runway Perf",
            font=XP_FONT_B, bg=XP_BTN_BG, fg=XP_FG, relief="raised", bd=1,
            anchor="w", cursor="hand2", pady=1,
            command=self._toggle_perf_panel
        )
        self._perf_toggle_btn.pack(fill="x")

        self._perf_panel_frame = tk.Frame(inner2, bg=XP_PANEL)
        self._perf_panel_frame.grid(row=r2, column=0, columnspan=2, sticky="ew")
        r2 += 1
        self._perf_panel_frame.grid_remove()  # hidden by default

        speed_fields = [("V1", "v1"), ("VR", "vr"), ("V2", "v2"),
                        ("FLEX / AT", "flex"), ("Flaps", "flaps"), ("THR", "thr")]
        _pf = self._perf_panel_frame
        _pf.columnconfigure(1, weight=1)
        for _r, (lbl_txt, key) in enumerate(speed_fields):
            tk.Label(_pf, text=lbl_txt, font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                     width=18, anchor="w").grid(row=_r, column=0, sticky="w",
                                                pady=1, padx=(8, 0))
            ent = xp_entry(_pf, width=12)
            ent.grid(row=_r, column=1, sticky="w", padx=4, pady=1)
            ent.bind("<KeyRelease>", lambda e: self._on_perf_field_edit())
            self._active_rwy_overrides[key] = ent

        # Divider
        tk.Frame(inner2, height=1, bg="#A0A0A0").grid(
            row=r2, column=0, columnspan=2, sticky="ew", pady=6)
        r2 += 1

        # Read-only computed perf fields
        ro_fields = [
            ("N1 / EPR",    "prev_n1"),
            ("MTOW",        "prev_mtow"),
            ("Limit Code",  "prev_limit"),
            ("PTOW (klbs)", "prev_ptow"),
            ("ATOW (klbs)", "prev_atow"),
            ("ZFW  (klbs)", "prev_zfw"),
            ("Fuel (klbs)", "prev_fuel"),
        ]
        for lbl_txt, ro_key in ro_fields:
            tk.Label(inner2, text=lbl_txt, font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                     width=18, anchor="w").grid(row=r2, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            tk.Label(inner2, textvariable=var, font=(XP_MONO, 9, "bold"),
                     bg=XP_PANEL, fg=XP_GREEN, anchor="w").grid(row=r2, column=1, sticky="w", padx=4)
            self.preview_vars[ro_key] = var
            r2 += 1

        # Pre-populate all runway override dicts from XML (used when saving edits per runway)
        for rid in rwy_ids:
            rwy_data = next((r for r in self.xml_data['valid_runways'] if r['id'] == rid), {})
            self.rwy_overrides[rid] = {k: str(rwy_data.get(k, ''))
                                       for k in ('v1', 'vr', 'v2', 'flex', 'flaps', 'thr')}

        # ════════════════════════════════════════════════════════════════
        # SECTION 3 — PAX / CARGO / FUEL / CG
        # ════════════════════════════════════════════════════════════════
        sec3 = xp_frame(form)
        sec3.pack(fill="x", padx=6, pady=4)   # Pax/Cargo/Fuel first
        sec2.pack(fill="x", padx=6, pady=4)   # Runway section below it
        xp_section_header(sec3, "Passengers, Cargo & Fuel")

        inner3 = tk.Frame(sec3, bg=XP_PANEL)
        inner3.pack(fill="x", padx=6, pady=4)
        inner3.columnconfigure(1, weight=1)
        r3 = 0

        # Pax
        tk.Label(inner3, text="Passenger Count", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.pax_entry = xp_entry(inner3, width=12)
        self.pax_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.pax_entry.insert(0, str(self.xml_data['pax_count_xml']))
        self.pax_entry.bind("<KeyRelease>", lambda e: (self._update_zfw_display(), self._update_perf_preview()))
        r3 += 1

        # Cargo
        tk.Label(inner3, text="Cargo Weight (lbs)", font=XP_PANEL, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.cargo_entry = xp_entry(inner3, width=12)
        self.cargo_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.cargo_entry.insert(0, str(self.xml_data['cargo_xml']))
        self.cargo_entry.bind("<KeyRelease>", lambda e: (self._update_zfw_display(), self._update_perf_preview()))
        r3 += 1

        # Ramp fuel
        tk.Label(inner3, text="Planned Ramp Fuel (lbs)", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.ramp_entry = xp_entry(inner3, width=12)
        self.ramp_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.ramp_entry.insert(0, str(self.xml_data['plan_ramp_xml']))
        self.ramp_entry.bind("<KeyRelease>", lambda e: self._update_perf_preview())
        r3 += 1

        # TOW CG
        tk.Label(inner3, text="TOW CG % MAC", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.cg_entry = xp_entry(inner3, width=12)
        self.cg_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.cg_entry.insert(0, "25.0")
        self.cg_entry.bind("<KeyRelease>", lambda e: self._update_perf_preview())
        r3 += 1

        # ZFW CG
        tk.Label(inner3, text="ZFW CG % MAC", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.zfw_cg_entry = xp_entry(inner3, width=12)
        self.zfw_cg_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.zfw_cg_entry.insert(0, "25.0")
        self.zfw_cg_entry.bind("<KeyRelease>", lambda e: self._update_perf_preview())
        r3 += 1

        tk.Frame(inner3, height=1, bg="#A0A0A0").grid(row=r3, column=0, columnspan=2, sticky="ew", pady=4)
        r3 += 1

        # Calculated ZFW display
        tk.Label(inner3, text="Calculated ZFW", font=XP_FONT_B, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.zfw_display_var = tk.StringVar()
        self.zfw_display_lbl = tk.Label(inner3, textvariable=self.zfw_display_var,
                                        font=(XP_MONO, 9, "bold"), bg=XP_PANEL, fg=XP_GREEN, anchor="w")
        self.zfw_display_lbl.grid(row=r3, column=1, sticky="w", padx=4)
        r3 += 1

        # ZFW override
        self.zfw_override_var = tk.BooleanVar(value=False)
        xp_check(inner3, "Override ZFW (manual)", self.zfw_override_var,
                 cmd=self._toggle_zfw_override).grid(row=r3, column=0, columnspan=2, sticky="w", pady=2)
        r3 += 1

        tk.Label(inner3, text="Override ZFW Value", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.zfw_override_entry = xp_entry(inner3, width=12, state="disabled")
        self.zfw_override_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.zfw_override_entry.bind("<KeyRelease>", lambda e: self._update_perf_preview())
        r3 += 1

        # Payload override
        self.payload_override_var = tk.BooleanVar(value=False)
        xp_check(inner3, "Override Payload ZFW (lbs)", self.payload_override_var,
                 cmd=self._toggle_payload_override).grid(row=r3, column=0, columnspan=2, sticky="w", pady=2)
        r3 += 1

        tk.Label(inner3, text="Override Payload (lbs)", font=XP_FONT, bg=XP_PANEL, fg=XP_FG,
                 width=20, anchor="w").grid(row=r3, column=0, sticky="w", pady=2)
        self.payload_override_entry = xp_entry(inner3, width=12, state="disabled")
        self.payload_override_entry.grid(row=r3, column=1, sticky="w", padx=4, pady=2)
        self.payload_override_entry.bind("<KeyRelease>", lambda e: (self._update_zfw_display(), self._update_perf_preview()))
        r3 += 1

        # ════════════════════════════════════════════════════════════════
        # GENERATE BUTTON
        # ════════════════════════════════════════════════════════════════
        # ════════ OOOI AUTOFILL SECTION ════════
        sec4 = xp_frame(form)
        sec4.pack(fill="x", padx=6, pady=4)
        xp_section_header(sec4, "OOOI Autofill (Sim Log)")
        inner4 = tk.Frame(sec4, bg=XP_PANEL)
        inner4.pack(fill="x", padx=6, pady=4)
        self.oooi_auto_var = tk.BooleanVar(value=False)
        self.oooi_fuel_var = tk.StringVar(value="")
        _oooi_row = tk.Frame(inner4, bg=XP_PANEL)
        _oooi_row.pack(fill="x")
        xp_check(_oooi_row, "Auto-fill from OOOI log", self.oooi_auto_var,
                 cmd=self._toggle_oooi).pack(side="left", padx=(0, 8))
        tk.Button(_oooi_row, text="Read Now", font=("Tahoma", 8),
                  bg=XP_BTN_BG, fg=XP_FG, relief="raised", bd=2, cursor="hand2",
                  command=self._apply_oooi_autofill).pack(side="left")
        _oooi_info = tk.Frame(inner4, bg=XP_PANEL)
        _oooi_info.pack(fill="x", padx=16, pady=(2, 0))
        tk.Label(_oooi_info, text="Fuel from log:", font=XP_FONT,
                 bg=XP_PANEL, fg=XP_FG, width=16, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(_oooi_info, textvariable=self.oooi_fuel_var,
                 font=(XP_MONO, 9, "bold"), bg=XP_PANEL, fg=XP_GREEN,
                 anchor="w").grid(row=0, column=1, sticky="w", padx=4)

        self.btn_area = btn_area = tk.Frame(form, bg=XP_BG)
        btn_area.pack(fill="x", padx=6, pady=8)

        self.submit_btn = tk.Button(
            btn_area, text="▶  GENERATE AERODATA", command=self._on_submit,
            font=("Tahoma", 10, "bold"), bg=XP_BTN_BG, fg=XP_FG,
            relief="raised", bd=2, cursor="hand2", width=22, height=2,
            activebackground="#BFBBAF"
        )
        self.submit_btn.pack(side="left", padx=(0, 6))

        # Folder button
        folder_btn = tk.Button(
            btn_area, text="📁 Folder", command=self._change_folder,
            font=("Tahoma", 9), bg=XP_BTN_BG, fg=XP_FG,
            relief="raised", bd=2, cursor="hand2", height=2,
            activebackground="#BFBBAF"
        )
        folder_btn.pack(side="left")

        self.folder_label = tk.Label(
            form, text=f"📂 {self.output_folder}",
            font=XP_FONT_SM, bg=XP_BG, fg="#666666",
            wraplength=380, anchor="w"
        )
        self.folder_label.pack(fill="x", padx=6, pady=(0, 4))

        # Seed speed fields from first runway
        self._on_runway_change()

    # ------------------------------------------------------------------
    def _toggle_perf_panel(self):
        """Show/hide the Edit Runway Perf collapsible panel."""
        if self._perf_panel_frame.winfo_ismapped():
            self._perf_panel_frame.grid_remove()
            self._perf_toggle_btn.config(text="▶  Edit Runway Perf")
        else:
            self._perf_panel_frame.grid()
            self._perf_toggle_btn.config(text="▼  Edit Runway Perf")

    def _toggle_oooi(self):
        if self.oooi_auto_var.get():
            self._apply_oooi_autofill()
        else:
            self.oooi_fuel_var.set("")

    def _apply_oooi_autofill(self):
        """Read OOOI log and populate fuel and ZFW fields."""
        oooi = read_oooi_log()
        if oooi["total_fuel_lbs"] is not None:
            fuel_int = int(round(oooi["total_fuel_lbs"]))
            self.oooi_fuel_var.set(f"{fuel_int:,} lbs")
            self.ramp_entry.delete(0, "end")
            self.ramp_entry.insert(0, str(fuel_int))
        else:
            self.oooi_fuel_var.set("Not found in log")
        if oooi["zfw_lbs"] is not None:
            sim_zfw = int(round(oooi["zfw_lbs"]))
            self.zfw_override_var.set(True)
            self.zfw_override_entry.config(state="normal")
            self.zfw_override_entry.delete(0, "end")
            self.zfw_override_entry.insert(0, str(sim_zfw))
            print(f"[OOOI] ZFW override set from sim: {sim_zfw:,} lbs")
        self._update_zfw_display()
        self._update_perf_preview()

    def _change_folder(self):
        new_folder = filedialog.askdirectory(
            title="Select New Output Folder",
            initialdir=self.output_folder,
            parent=self.root
        )
        if new_folder:
            self.output_folder = new_folder
            save_last_folder(new_folder)
            self.folder_label.config(text=f"📂 {new_folder}")

    def _toggle_cond_overrides(self):
        state = "normal" if self.cond_override_var.get() else "disabled"
        for ent in self.cond_overrides.values():
            ent.config(state=state)
        self.anti_ice_cb.config(state=state)
        self._update_perf_preview()

    def _on_scenario_change(self):
        self._update_perf_preview()

    def _toggle_zfw_override(self):
        if self.zfw_override_var.get():
            self.zfw_override_entry.config(state="normal")
            self.zfw_override_entry.delete(0, "end")
            self.zfw_override_entry.insert(0, str(self._calc_zfw()))
            self.zfw_override_entry.focus_set()
        else:
            self.zfw_override_entry.config(state="disabled")
            self.zfw_override_entry.delete(0, "end")

    def _toggle_payload_override(self):
        if self.payload_override_var.get():
            self.payload_override_entry.config(state="normal")
            try:
                current_payload = (int(self.pax_entry.get()) * self.xml_data['pax_weight']
                                   + int(self.cargo_entry.get()))
            except ValueError:
                current_payload = 0
            self.payload_override_entry.delete(0, "end")
            self.payload_override_entry.insert(0, str(current_payload))
        else:
            self.payload_override_entry.config(state="disabled")
            self.payload_override_entry.delete(0, "end")
        self._update_zfw_display()

    def _calc_zfw(self):
        try:
            if self.payload_override_var.get():
                payload = int(self.payload_override_entry.get())
                return self.xml_data['oew'] + payload
            return (self.xml_data['oew']
                    + int(self.pax_entry.get()) * self.xml_data['pax_weight']
                    + int(self.cargo_entry.get()))
        except ValueError:
            return 0

    def _update_zfw_display(self, event=None):
        zfw = self._calc_zfw()
        self.zfw_display_var.set(f"{zfw:,} lbs")
        over = zfw > self.xml_data['max_zfw']
        self.zfw_display_lbl.config(fg="#CC0000" if over else "#007000")

    def _rwy_select_all(self):
        for var in self.runway_checks.values():
            var.set(True)
        self._on_runway_change()

    def _rwy_select_none(self):
        for var in self.runway_checks.values():
            var.set(False)
        self._on_runway_change()

    def _on_runway_change(self, *_):
        """Save current speed edits, switch to selected runway, populate intersection dropdown."""
        # Save edits for previously active runway (if any)
        prev_rid = getattr(self, '_prev_edit_rwy', None)
        if prev_rid and prev_rid in self.rwy_overrides:
            for key, ent in self._active_rwy_overrides.items():
                self.rwy_overrides[prev_rid][key] = ent.get()

        sel = self.runway_var.get()
        self._prev_edit_rwy = sel

        # Load values for newly selected runway
        rwy_data = next((r for r in self.xml_data['valid_runways'] if r['id'] == sel), {})
        saved = self.rwy_overrides.get(sel, {})
        for key, ent in self._active_rwy_overrides.items():
            ent.delete(0, "end")
            val = saved.get(key) if saved.get(key, '') != '' else str(rwy_data.get(key, ''))
            ent.insert(0, val)

        # Populate intersection dropdown
        icao         = self.xml_data.get('origin_icao', '')
        full_tora_ft = float(rwy_data.get('length', 0))
        dist_reject  = int(rwy_data.get('distance_reject', 0))
        index_data   = load_runway_index()
        groups       = get_intersection_groups(icao, sel, full_tora_ft, dist_reject, index_data)
        self._intxn_groups          = groups
        self._intxn_invalid_choices = set()

        choices = ["FULL"]
        for g in groups:
            txwy_str = '/'.join(g['taxiways'])
            if g['valid']:
                label = f"{g['id']}  ({int(g['tora_ft'])}ft  TXWY {txwy_str})"
            else:
                label = f"⚠ {g['id']}  ({int(g['tora_ft'])}ft  TXWY {txwy_str})  [REJECT TOO SHORT]"
                self._intxn_invalid_choices.add(label)
            choices.append(label)
        self.intersection_menu['values'] = choices
        self.intersection_var.set("FULL")
        self.intersection_menu.bind("<<ComboboxSelected>>", self._on_intxn_selected)

        self._update_perf_preview()

    def _on_intxn_selected(self, event=None):
        """Block selection of intersections flagged as too short."""
        chosen  = self.intersection_var.get()
        invalid = getattr(self, '_intxn_invalid_choices', set())
        if chosen in invalid:
            self.intersection_var.set("FULL")
            messagebox.showwarning(
                "Intersection Unavailable",
                "That intersection is too short for a rejected takeoff stop.\n\n"
                "Select a valid intersection or use FULL length."
            )

    def _get_selected_runway(self):
        """Return the active (dropdown-selected) runway dict with edits + intersection applied."""
        sel = self.runway_var.get()
        rwy = next((dict(r) for r in self.xml_data['valid_runways'] if r['id'] == sel), None)
        if rwy is None and self.xml_data['valid_runways']:
            rwy = dict(self.xml_data['valid_runways'][0])
        if rwy is None:
            return None
        # Apply edits from the active entry widgets
        for key, ent in self._active_rwy_overrides.items():
            val = ent.get().strip()
            if val:
                rwy[key] = val
        # Apply intersection override
        rwy['_full_tora_ft'] = float(rwy.get('length', 0))
        intxn_sel = self.intersection_var.get() if hasattr(self, 'intersection_var') else "FULL"
        if intxn_sel and intxn_sel != "FULL" and hasattr(self, '_intxn_groups'):
            chosen_id = intxn_sel.split()[0]
            group = next((g for g in self._intxn_groups if g['id'] == chosen_id), None)
            if group:
                rwy['id']     = group['id']
                rwy['length'] = str(int(group['tora_ft']))
                rwy['_intxn_taxiways'] = group['taxiways']
        return rwy

    def _get_selected_runways(self):
        """Return all checked runways with saved overrides applied (intersection applied to active runway only)."""
        selected = []
        # Save current edits back to store for the active runway
        sel = self.runway_var.get()
        if sel and sel in self.rwy_overrides:
            for key, ent in self._active_rwy_overrides.items():
                self.rwy_overrides[sel][key] = ent.get()
        for rid, var in self.runway_checks.items():
            if var.get():
                rwy = next((dict(r) for r in self.xml_data['valid_runways'] if r['id'] == rid), None)
                if rwy:
                    saved = self.rwy_overrides.get(rid, {})
                    for key, val in saved.items():
                        if val:
                            rwy[key] = val
                    # Apply intersection only for the active dropdown runway
                    if rid == sel:
                        rwy['_full_tora_ft'] = float(rwy.get('length', 0))
                        intxn_sel = self.intersection_var.get() if hasattr(self, 'intersection_var') else "FULL"
                        if intxn_sel and intxn_sel != "FULL" and hasattr(self, '_intxn_groups'):
                            chosen_id = intxn_sel.split()[0]
                            group = next((g for g in self._intxn_groups if g['id'] == chosen_id), None)
                            if group:
                                rwy['id']     = group['id']
                                rwy['length'] = str(int(group['tora_ft']))
                                rwy['_intxn_taxiways'] = group['taxiways']
                    selected.append(rwy)
        return selected

    def _open_files(self, *paths):
        system = platform.system()
        for p in paths:
            if p and os.path.exists(p):
                if system == "Darwin":    subprocess.run(["open", p])
                elif system == "Windows": os.startfile(p)
                elif system == "Linux":   subprocess.run(["xdg-open", p])

    # ------------------------------------------------------------------
    def _on_perf_field_edit(self):
        """Handle a keystroke in any Edit Runway Perf field (V1/VR/V2/FLEX/FLAPS/THR).

        Refreshes the read-only N1/MTOW preview vars AND schedules the debounced
        txt preview refresh, so manual perf edits are actually visible in the
        generated-document preview pane, not just saved silently in the background.
        """
        self._update_n1_mtow_only()
        if hasattr(self, '_txt_preview_job'):
            try: self.root.after_cancel(self._txt_preview_job)
            except: pass
        self._txt_preview_job = self.root.after(400, self._update_txt_preview)

    # ------------------------------------------------------------------
    def _update_n1_mtow_only(self):
        """Refresh only computed read-only fields without rewriting entry widgets."""
        try:
            pax   = int(self.pax_entry.get())
            cargo = int(self.cargo_entry.get())
            ramp  = int(self.ramp_entry.get())
            oew        = self.xml_data['oew']
            pax_weight = self.xml_data['pax_weight']
            taxi_fuel  = self.xml_data['taxi_fuel']
            zfw = (int(self.zfw_override_entry.get()) if self.zfw_override_var.get()
                   else (self.xml_data['oew'] + int(self.payload_override_entry.get())
                         if self.payload_override_var.get()
                         else oew + pax * pax_weight + cargo))
            tow = zfw + (ramp - taxi_fuel)

            rwy = self._get_selected_runway() or {}

            temp       = (self.cond_overrides['temp'].get().strip()
                          if self.cond_override_var.get() else self.xml_data.get('temp', '15'))
            icaocode   = self.xml_data.get('icaocode', '')
            ac_name    = self.xml_data.get('AC_name', '')
            engine_name = self.xml_data.get('engine_type', '')
            is_737_ng   = icaocode in ['B736', 'B737', 'B738', 'B739']
            is_737_max  = icaocode == 'B38M'
            is_boeing_737 = is_737_ng or is_737_max
            is_md83     = icaocode == 'MD83'
            is_sfp      = is_boeing_737 and ("SFP" in ac_name.upper() or "SFP" in engine_name.upper())
            try: alt_val = float(rwy.get('elevation', 0))
            except: alt_val = 0.0

            THRUST_TABLE = {
                "B736": {"D-TO": 22, "D-TO1": 20, "D-TO2": 18},
                "B737": {"D-TO": 24, "D-TO1": 22, "D-TO2": 20},
                "B738": {"D-TO": 26, "D-TO1": 24, "D-TO2": 22},
                "B739": {"D-TO": 27, "D-TO1": 25, "D-TO2": 23},
                "B38M": {"TO": 26, "TO1": 24, "TO2": 22},
            }
            derate_label = rwy.get('thr', '').upper().strip()
            sfp_bump = is_sfp and derate_label in ('TO-B', 'BUMP')
            thrust_label = "N/A"; effective_thrust = None
            if is_boeing_737 and icaocode in THRUST_TABLE:
                key_t = derate_label.replace("D-", "") if is_737_max else derate_label
                effective_thrust = THRUST_TABLE[icaocode].get(key_t) or list(THRUST_TABLE[icaocode].values())[0]
                if sfp_bump:     effective_thrust = 27; thrust_label = "27K BUMP"
                elif is_737_max: thrust_label = key_t if key_t in ["TO","TO1","TO2"] else "TO"
                else:            thrust_label = f"{effective_thrust}K"

            n1_display = "—"
            if is_boeing_737:
                sod = get_speed_other(icaocode, oat=temp, altitude=alt_val, weight=tow,
                                      thrust_rating=effective_thrust if effective_thrust else 26)
                n1_pack_on = sod.get('n1', 'XXX') if sod else 'XXX'
                pack_off_adj = 0.8 if alt_val <= 8000 else (0.9 if alt_val <= 9000 else 1.0)
                flex_raw = rwy.get('flex', '')
                reduced_n1 = n1_pack_on; reduced_n1_valid = False
                if not sfp_bump and flex_raw and str(flex_raw).strip() not in ['', 'XX', 'XXX']:
                    try:
                        rnd = get_reduced_thrust_n1(icaocode, effective_thrust, int(flex_raw), alt_val)
                        if rnd and 'n1' in rnd: reduced_n1 = rnd['n1']; reduced_n1_valid = True
                    except: pass
                n1_display = str(reduced_n1) if reduced_n1_valid else str(n1_pack_on)
            elif is_md83:
                epr_data = get_speed_other(icaocode, oat=temp, altitude=alt_val)
                n1_display = f"{epr_data['epr']:.2f}" if epr_data and 'epr' in epr_data else "—"

            mtow_val  = rwy.get('max_weight', 0)
            limit_raw = rwy.get('limit_code', '')
            limit_display = {'CLB': 'C', 'OBS': 'O', 'PDR': 'P', 'AFM': 'S'}.get(str(limit_raw).upper(), str(limit_raw))
            self.preview_vars['prev_n1'].set(n1_display)
            try:    self.preview_vars['prev_mtow'].set(f"{float(mtow_val):.1f}")
            except: self.preview_vars['prev_mtow'].set("—")
            self.preview_vars['prev_limit'].set(limit_display if limit_display else "—")

            taxi_fuel2 = self.xml_data['taxi_fuel']
            ramp2 = int(self.ramp_entry.get())
            sc_key2 = self.scenario_var.get()
            extra2 = 0
            if sc_key2 and sc_key2 != "PLANNED" and sc_key2 in self.scenario_vars:
                _, _c2 = self.scenario_vars[sc_key2]
                extra2 = int(_c2.split('+')[1]) if '+' in _c2 else 0
            stow2 = tow + extra2; sramp2 = ramp2 + extra2
            atow2 = stow2 + 2000
            self.preview_vars['prev_ptow'].set(f"{stow2/1000:.1f}")
            self.preview_vars['prev_atow'].set(f"{atow2/1000:.1f}")
            self.preview_vars['prev_zfw'].set(f"{zfw/1000:.1f}")
            self.preview_vars['prev_fuel'].set(f"{(sramp2 - taxi_fuel2)/1000:.1f}P")
        except Exception as _e:
            print(f"[N1_ONLY] error: {_e}")

    def _update_perf_preview(self, event=None):
        """Recompute live perf values and populate preview labels."""
        try:
            try:
                pax   = int(self.pax_entry.get())
                cargo = int(self.cargo_entry.get())
                ramp  = int(self.ramp_entry.get())
            except ValueError:
                return

            oew        = self.xml_data['oew']
            pax_weight = self.xml_data['pax_weight']
            taxi_fuel  = self.xml_data['taxi_fuel']

            if self.payload_override_var.get():
                try:    zfw = oew + int(self.payload_override_entry.get())
                except: zfw = oew + pax * pax_weight + cargo
            elif self.zfw_override_var.get():
                try:    zfw = int(self.zfw_override_entry.get())
                except: zfw = oew + pax * pax_weight + cargo
            else:
                zfw = oew + pax * pax_weight + cargo

            tow = zfw + (ramp - taxi_fuel)

            scenario_key = self.scenario_var.get()
            tlr_tables   = self.xml_data.get('tlr_tables', {})
            surface = self.xml_data.get('surface', 'dry').lower()
            tlr_result = None

            if scenario_key and scenario_key != "PLANNED" and scenario_key in self.scenario_vars:
                sc_surface, sc_condition = self.scenario_vars[scenario_key]
                surface = sc_surface.lower()
                extra_fuel = int(sc_condition.split('+')[1]) if '+' in sc_condition else 0
                tow_scaled = tow / 100.0
                _preview_rwy = self._get_selected_runway()
                _preview_rwy_id = _preview_rwy['id'] if _preview_rwy else ''
                tlr_result = interpolate_tlr_speeds(
                    tlr_tables, _preview_rwy_id, sc_surface, tow_scaled,
                    force_condition=sc_condition
                )
            else:
                extra_fuel = 0

            rwy = self._get_selected_runway() or {}

            if tlr_result:
                rwy['v1']         = tlr_result['v1']
                rwy['vr']         = tlr_result['vr']
                rwy['v2']         = tlr_result['v2']
                rwy['flaps']      = tlr_result['flaps']
                rwy['flex']       = str(tlr_result['mt'])
                rwy['thr']        = tlr_result['config']
                rwy['limit_code'] = tlr_result['limit']
                rwy['max_weight'] = tlr_result['mtow'] / 100.0
                # Push TLR values into the editable fields (active runway only)
                rid = rwy.get('id', '').rstrip('XYZ') if rwy.get('id', '') else ''
                if rid == self.runway_var.get():
                    _tlr_map = {
                        'v1': str(tlr_result['v1']), 'vr': str(tlr_result['vr']),
                        'v2': str(tlr_result['v2']), 'flaps': str(tlr_result['flaps']),
                        'flex': str(tlr_result['mt']), 'thr': str(tlr_result['config']),
                    }
                    for fk, fv in _tlr_map.items():
                        ent = self._active_rwy_overrides.get(fk)
                        if ent:
                            ent.delete(0, "end")
                            ent.insert(0, fv)

            if self.cond_override_var.get():
                temp = self.cond_overrides['temp'].get().strip() or self.xml_data.get('temp', '15')
            else:
                temp = self.xml_data.get('temp', '15')

            icaocode   = self.xml_data.get('icaocode', '')
            ac_name    = self.xml_data.get('AC_name', '')
            engine_name = self.xml_data.get('engine_type', '')
            is_737_ng   = icaocode in ['B736', 'B737', 'B738', 'B739']
            is_737_max  = icaocode == 'B38M'
            is_boeing_737 = is_737_ng or is_737_max
            is_md83     = icaocode == 'MD83'
            is_sfp      = is_boeing_737 and ("SFP" in ac_name.upper() or "SFP" in engine_name.upper())
            try: alt_val = float(rwy.get('elevation', 0))
            except: alt_val = 0.0

            THRUST_TABLE = {
                "B736": {"D-TO": 22, "D-TO1": 20, "D-TO2": 18},
                "B737": {"D-TO": 24, "D-TO1": 22, "D-TO2": 20},
                "B738": {"D-TO": 26, "D-TO1": 24, "D-TO2": 22},
                "B739": {"D-TO": 27, "D-TO1": 25, "D-TO2": 23},
                "B38M": {"TO": 26, "TO1": 24, "TO2": 22},
            }
            derate_label     = rwy.get('thr', '').upper().strip()
            sfp_bump         = is_sfp and derate_label in ('TO-B', 'BUMP')
            thrust_label     = "N/A"
            effective_thrust = None

            if is_boeing_737 and icaocode in THRUST_TABLE:
                key_t = derate_label.replace("D-", "") if is_737_max else derate_label
                effective_thrust = THRUST_TABLE[icaocode].get(key_t) or list(THRUST_TABLE[icaocode].values())[0]
                if sfp_bump:     effective_thrust = 27; thrust_label = "27K BUMP"
                elif is_737_max: thrust_label = key_t if key_t in ["TO", "TO1", "TO2"] else "TO"
                else:            thrust_label = f"{effective_thrust}K"

            n1_display = "—"
            if is_boeing_737:
                sod = get_speed_other(icaocode, oat=temp, altitude=alt_val, weight=tow,
                                      thrust_rating=effective_thrust if effective_thrust else 26)
                n1_pack_on = sod.get('n1', 'XXX') if sod else 'XXX'
                pack_off_adj = 0.8 if alt_val <= 8000 else (0.9 if alt_val <= 9000 else 1.0)
                flex_raw = rwy.get('flex', '')
                reduced_n1_valid = False
                reduced_n1 = n1_pack_on
                if not sfp_bump and flex_raw and str(flex_raw).strip() not in ['', 'XX', 'XXX']:
                    try:
                        ft_int = int(flex_raw)
                        rnd = get_reduced_thrust_n1(icaocode, effective_thrust, ft_int, alt_val)
                        if rnd and 'n1' in rnd: reduced_n1 = rnd['n1']; reduced_n1_valid = True
                    except: pass
                n1_display = str(reduced_n1) if reduced_n1_valid else str(n1_pack_on)
            elif is_md83:
                epr_data = get_speed_other(icaocode, oat=temp, altitude=alt_val)
                n1_display = f"{epr_data['epr']:.2f}" if epr_data and 'epr' in epr_data else "—"

            mtow_val  = rwy.get('max_weight', 0)
            limit_raw = rwy.get('limit_code', '')
            limit_display = {'CLB': 'C', 'OBS': 'O', 'PDR': 'P', 'AFM': 'S'}.get(
                str(limit_raw).upper(), str(limit_raw))

            self.preview_vars['prev_n1'].set(n1_display)
            try:    self.preview_vars['prev_mtow'].set(f"{float(mtow_val):.1f}")
            except: self.preview_vars['prev_mtow'].set("")
            self.preview_vars['prev_limit'].set(limit_display if limit_display else "")

            scenario_key2 = self.scenario_var.get()
            extra_fuel2 = 0
            if scenario_key2 and scenario_key2 != "PLANNED" and scenario_key2 in self.scenario_vars:
                _, _cond2 = self.scenario_vars[scenario_key2]
                extra_fuel2 = int(_cond2.split('+')[1]) if '+' in _cond2 else 0
            scenario_tow  = tow + extra_fuel2
            scenario_ramp = ramp + extra_fuel2
            takeoff_fuel_lbs = scenario_ramp - taxi_fuel
            atow_lbs_prev = scenario_tow + 2000
            if tlr_result:
                tlr_mtow_lbs = tlr_result['mtow'] * 100.0
                if tlr_mtow_lbs > 0 and atow_lbs_prev > tlr_mtow_lbs:
                    atow_lbs_prev = tlr_mtow_lbs
            self.preview_vars['prev_ptow'].set(f"{scenario_tow/1000:.1f}")
            self.preview_vars['prev_atow'].set(f"{atow_lbs_prev/1000:.1f}")
            self.preview_vars['prev_zfw'].set(f"{zfw/1000:.1f}")
            self.preview_vars['prev_fuel'].set(f"{takeoff_fuel_lbs/1000:.1f}P")

        except Exception as _e:
            print(f"[PREVIEW] error: {_e}")

        # Debounced txt preview refresh
        if hasattr(self, '_txt_preview_job'):
            try: self.root.after_cancel(self._txt_preview_job)
            except: pass
        self._txt_preview_job = self.root.after(400, self._update_txt_preview)

    def _update_txt_preview(self):
        """Generate a lightweight text preview and show it in the right pane."""
        import tempfile
        try:
            pax   = int(self.pax_entry.get())
            cargo = int(self.cargo_entry.get())
            ramp  = int(self.ramp_entry.get())
            cg    = float(self.cg_entry.get())
            zfw_cg = float(self.zfw_cg_entry.get())
        except (ValueError, AttributeError):
            return

        try:
            # Resolve ZFW override / payload override
            zfw_override = None
            override_payload_lbs = None
            if self.payload_override_var.get():
                try: override_payload_lbs = int(self.payload_override_entry.get())
                except: pass
            elif self.zfw_override_var.get():
                try: zfw_override = int(self.zfw_override_entry.get())
                except: pass

            if override_payload_lbs is not None:
                zfw_for_build = self.xml_data['oew'] + override_payload_lbs
                zfw_override  = zfw_for_build

            scenario_key = self.scenario_var.get()
            sc_surface   = self.xml_data.get('surface', 'dry')
            sc_extra     = 0
            force_cond   = None
            if scenario_key and scenario_key != "PLANNED" and scenario_key in self.scenario_vars:
                _s, _c    = self.scenario_vars[scenario_key]
                sc_surface = _s.lower()
                sc_extra   = int(_c.split('+')[1]) if '+' in _c else 0
                force_cond = _c

            uplink_data, loadsheet_data = build_weights(
                self.xml_data, pax, cargo, ramp, cg, zfw_override,
                zfw_cg_percent=zfw_cg
            )
            if self.cond_override_var.get():
                for k in ['temp', 'qnh', 'wind']:
                    v = self.cond_overrides[k].get().strip()
                    if v: uplink_data[k] = v
                uplink_data['anti_ice_on'] = self.anti_ice_var.get()
            uplink_data['surface'] = sc_surface

            rwy = self._get_selected_runway()
            valid_runways = self._get_selected_runways()
            if not valid_runways and rwy:
                valid_runways = [rwy]

            tlr_active = (scenario_key and scenario_key != "PLANNED" and scenario_key in self.scenario_vars)

            with tempfile.TemporaryDirectory() as tmpdir:
                fpath = generate_combined_output(
                    loadsheet_data, uplink_data, valid_runways,
                    self.xml_data.get('anti_ice_on', False),
                    self.xml_data['taxi_fuel'],
                    tmpdir, cg, self.xml_data['acdata'],
                    tlr_tables=self.xml_data['tlr_tables'],
                    tlr_scenario_active=tlr_active,
                    force_tlr_condition=force_cond,
                    sc_extra_fuel=sc_extra
                )
                if fpath and os.path.exists(fpath):
                    with open(fpath, 'r') as f:
                        content = f.read()
                else:
                    content = "(preview unavailable — fuel variance or no runways)"
        except Exception as e:
            content = f"(preview error: {e})"

        self.txt_preview.config(state="normal")
        self.txt_preview.delete("1.0", "end")
        self.txt_preview.insert("1.0", content)
        self.txt_preview.config(state="disabled")

    # ------------------------------------------------------------------
    def _on_submit(self):
        try:
            pax_count  = int(self.pax_entry.get())
            cargo      = int(self.cargo_entry.get())
            plan_ramp  = int(self.ramp_entry.get())
            cg_percent = float(self.cg_entry.get())
            zfw_cg     = float(self.zfw_cg_entry.get())
        except ValueError as e:
            messagebox.showerror("Input Error", str(e))
            return

        # Resolve ZFW / payload overrides
        zfw_override         = None
        override_payload_lbs = None
        if self.payload_override_var.get():
            try: override_payload_lbs = int(self.payload_override_entry.get())
            except ValueError: pass
        if self.zfw_override_var.get():
            try: zfw_override = int(self.zfw_override_entry.get())
            except ValueError: pass

        if override_payload_lbs is not None:
            zfw_override = self.xml_data['oew'] + override_payload_lbs

        # TLR scenario
        scenario_key    = self.scenario_var.get()
        sc_surface      = self.xml_data.get('surface', 'dry')
        sc_extra_fuel   = 0
        force_condition = None
        tlr_active      = False
        if scenario_key and scenario_key != "PLANNED" and scenario_key in self.scenario_vars:
            _surf, _cond    = self.scenario_vars[scenario_key]
            sc_surface      = _surf.lower()
            sc_extra_fuel   = int(_cond.split('+')[1]) if '+' in _cond else 0
            force_condition = _cond
            tlr_active      = True

        uplink_data, loadsheet_data = build_weights(
            self.xml_data, pax_count, cargo, plan_ramp,
            cg_percent, zfw_override, zfw_cg_percent=zfw_cg
        )

        anti_ice_on = self.xml_data.get('anti_ice_on', False)
        if self.cond_override_var.get():
            for key in ['temp', 'qnh', 'wind']:
                val = self.cond_overrides[key].get().strip()
                if val:
                    uplink_data[key] = val
            anti_ice_on = self.anti_ice_var.get()
            uplink_data['anti_ice_on'] = anti_ice_on

        uplink_data['surface'] = sc_surface

        rwy = self._get_selected_runway()
        valid_runways = self._get_selected_runways()
        if not valid_runways and rwy:
            valid_runways = [rwy]

        combined_file = generate_combined_output(
            loadsheet_data, uplink_data, valid_runways,
            anti_ice_on, self.xml_data['taxi_fuel'],
            self.output_folder, cg_percent, self.xml_data['acdata'],
            tlr_tables=self.xml_data['tlr_tables'],
            tlr_scenario_active=tlr_active,
            force_tlr_condition=force_condition,
            sc_extra_fuel=sc_extra_fuel
        )

        if combined_file is None:
            self.status_var.set("⚠ Fuel variance exceeds 2000 lbs — rejected")
            return

        tow_lbs  = loadsheet_data['TOW']
        atow_lbs = tow_lbs + 2000
        self.status_var.set(f"✓ AERODATA generated — TOW {tow_lbs/1000:.1f}  ATOW {atow_lbs/1000:.1f}")
        self._open_files(combined_file)


# ====================================================================================
# MAIN
# ====================================================================================

def main():
    output_folder = select_output_folder()
    print(f"Output folder: {output_folder}\n")

    username = "tgibbons"
    tree = fetch_xml_from_api(username)
    if not tree:
        print("Failed to fetch XML from SimBrief.")
        return

    xml_root = tree.getroot()
    xml_data = parse_xml_raw(xml_root, datetime.now().strftime("%Y-%m-%d"), "B738")

    if not xml_data['valid_runways']:
        print("No valid runway data found in XML.")
        return

    root = tk.Tk()
    app = AppWindow(root, xml_data, output_folder)
    root.mainloop()


if __name__ == "__main__":
    main()
