"""
masterlog_core.py — the data layer the three release formats share.

MASTERLOG, JetPlan and FOS are meant to read the same values and differ
only in how they draw them. They had drifted: three copies of get_text
(the XML accessor itself), three of extract_runway_data, three of the
time maths. A fix in one never reached the others.

These are MASTERLOG.py's implementations, which are the newest — it is
the generation that moved from print() to structured logging. Rendering
stays in each format: save_as_pdf, the write_*_section functions, the
nav log and the page builders are what makes a JetPlan release look like
a JetPlan release, and must not be shared.

Per-script config (_load_config/CONFIG_FILE) and the tkinter prompts stay
per-format too — each script reads its own .config file, and sharing that
would give JetPlan MASTERLOG's notam_style.
"""
import json
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone

import requests

LOG = logging.getLogger("masterlog_core")

def pad_if_number(s):
    """Zero-pad single/double-digit numeric strings; pass non-numeric strings through unchanged."""
    try:
        n = int(s)
        if n < 100:
            return f"{n:02d}"
        return str(n)
    except ValueError:
        return s  # return as-is if not a number (like '---')

def seconds_to_hhmm(seconds):
    """Convert a raw seconds integer to a zero-padded HHMM string."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}{m:02d}"

def format_time_elapsed(seconds):
    """Convert a seconds integer (or numeric string) to a zero-padded HHMM string."""
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
        if seconds <= 0:
            return ""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:02d}{minutes:02d}"
    except Exception:
        return ""

def format_fuel(fuel_value):
    """Format fuel value consistently - no trailing zeros for whole numbers"""
    try:
        if fuel_value is None or fuel_value == "":
            return "0"
        val = float(fuel_value)
        # Return integer format if it's a whole number, otherwise 1 decimal place
        return f"{val:.0f}" if val == int(val) else f"{val:.1f}"
    except (ValueError, TypeError):
        return "0"

def format_off_time(t):
    """Strip colons from a time string (e.g. '14:30' → '1430')."""
    return t.replace(":", "") if ":" in t else t

def add_times(t1, t2):
    """Add two HHMM-format time strings, returning a new HHMM string (wraps at 24 h)."""
    h1, m1 = int(t1[:2]), int(t1[2:])
    h2, m2 = int(t2[:2]), int(t2[2:])
    total_m = m1 + m2
    total_h = h1 + h2 + total_m // 60
    total_m = total_m % 60
    total_h = total_h % 24
    return f"{total_h:02d}{total_m:02d}"

def calculate_time_difference(scheduled_seconds, planned_seconds):
    """Return the absolute time difference as 'HHMMx' where x is L (late) or E (early)."""
    try:
        diff = int(planned_seconds) - int(scheduled_seconds)
        abs_diff = abs(diff)
        hours = abs_diff // 3600
        minutes = (abs_diff % 3600) // 60
        return f"{hours:02d}{minutes:02d}{'L' if diff > 0 else 'E'}"
    except Exception:
        return "0000E"

def add_time_to_takeoff(takeoff_time, seconds_elapsed):
    """Add *seconds_elapsed* to a HHMM takeoff string; returns a HHMM result string."""
    try:
        if isinstance(takeoff_time, str):
            hours, minutes = int(takeoff_time[:2]), int(takeoff_time[2:])
            base_time = datetime.now(timezone.utc).replace(hour=hours, minute=minutes, second=0, microsecond=0)
        else:
            base_time = takeoff_time
        return (base_time + timedelta(seconds=int(seconds_elapsed))).strftime("%H%M")
    except Exception:
        return "----"

def fetch_simbrief_data(user_id):
    """Fetch the SimBrief XML flight plan for *user_id* via the public API; exits on failure."""
    url = f"https://www.simbrief.com/api/xml.fetcher.php?username={user_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except Exception as e:
        LOG.error(f"Error fetching SimBrief data: {e}")
        sys.exit(1)

def format_time_endurance(seconds):
    """Format an endurance value in seconds to a 'HH+MM'-style string."""
    if not isinstance(seconds, int) or seconds <= 0:
        return "00+00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}{m:02d}"

def get_element_text(parent, tag_name, default=""):
    """Safely extract text from XML elements, with optional default fallback."""
    element = parent.find(tag_name)
    return element.text.strip() if element is not None and element.text else default

def format_out_time(time_str):
    """Format a 4-digit HHMM string as 'HH:MM'; returns '0000' if the input is invalid."""
    if not time_str or len(time_str) != 4 or not time_str.isdigit():
        return "0000"
    return f"{time_str[:2]}:{time_str[2:]}"

def prompt_for_takeoff_time_str(default_hhmm):
    """Disabled - automatically use scheduled departure time"""
    # Simply return the default time formatted correctly
    return format_out_time(default_hhmm)

def calculate_tldr(root, current_fix_index):
    """Calculate the remaining route distance (TLDR) in nautical miles from the current fix."""
    try:
        all_fixes = root.findall("navlog/fix")
        cumulative_distance = 0.0

        for i in range(current_fix_index + 1):  # include current fix
            distance_str = all_fixes[i].findtext("distance", "0")
            try:
                distance_nm = float(distance_str)
                cumulative_distance += distance_nm
            except ValueError:
                continue  # skip invalid distances

        total_route_distance = sum(
            float(fix.findtext("distance", "0") or 0) for fix in all_fixes
        )

        tldr = total_route_distance - cumulative_distance
        return str(int(tldr)) if tldr.is_integer() else f"{tldr:.1f}"

    except Exception as e:
        LOG.error(f"Error calculating TLDR: {e}")
        return "0"

def safe_float(value, default=0.0):
    """Safely coerce *value* to float; returns *default* on any error."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

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
                LOG.debug(f"[DBG] Runway {runway.findtext('identifier','?')} excluded: "
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
        # import json  # top-level import

        # --- Parse acdata_parsed JSON ---
        # --- Parse aircraft JSON ---
        acdata_tag = xml_root.find('.//acdata_parsed')
        if acdata_tag is not None and acdata_tag.text and acdata_tag.text.strip():
            try:
                acdata = json.loads(acdata_tag.text.strip())
                aircraft_name = acdata.get('name', 'UNKNOWN')
                # comments is free text ("A321 -A5 SHARKLET") and is what the
                # sheet header prints, so it stays the display string. The
                # actual engine designation comes from aircraft/engines --
                # the same XML path simbrief_ofp.py already reads -- because
                # matching an engine family off a free-text comment is
                # guesswork (and silently failed for the IAE A321).
                engine_type   = acdata.get('comments', 'UNKNOWN')
                LOG.debug(f"[DBG: Loaded acdata for {acdata.get('reg','UNKNOWN')} - {aircraft_name} / {engine_type}")
            except json.JSONDecodeError as e:
                LOG.error(f"ERROR parsing acdata_parsed: {e}")
                aircraft_name = 'UNKNOWN'
                engine_type = 'UNKNOWN'
        else:
            LOG.warning("acdata_parsed not found or empty")
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
                'engine_designation': (xml_root.findtext('aircraft/engines') or '').strip(),
                'aircraft': aircraft_name,
                'surface_condition': surface_condition,
                'wind': f"{conditions.findtext('wind_direction', '0')}/{conditions.findtext('wind_speed', '0')}",
                'tow': conditions.findtext('planned_weight', '0')
            }
        else:
            LOG.warning("Takeoff conditions missing, using defaults.")
            flight_info = {
                'temp': '0',
                'qnh': '0',
                'engine': engine_type,
                'engine_designation': (xml_root.findtext('aircraft/engines') or '').strip(),
                'aircraft': aircraft_name,
                'surface_condition': 'dry',
                'wind': '0/0',
                'tow': '0'
            }

        # --- Extract header fields ---
        flt_node = xml_root.find('.//flight_number')
        dte_node = xml_root.find('.//date_time')
        fin_node = xml_root.find('.//fin')
        # --- Process runways ---
        valid_runways = []
        anti_ice_on = False
        runways = xml_root.findall('.//tlr/takeoff/runway')
        LOG.debug(f"[DBG: Found {len(runways)} runway elements in XML")

        # --- V-speed sanitizer ---
        def sanitize_vspeed(val):
            if not val or val.upper() in ["ERR", "XXX"]:
                return "XXX"
            try:
                num = int(val)
                return str(num) if num > 0 else "XXX"
            except Exception:
                return "XXX"

        # Struct weight limit for runway length filtering (in thousands lbs)
        try:
            _struct_wt_limit = float(xml_root.findtext('weights/max_tow_struct', '0') or 0) / 1000.0
        except Exception:
            _struct_wt_limit = 0.0
        LOG.debug(f"[DBG] struct_wt_limit={_struct_wt_limit:.1f}k — runway length filter {'ACTIVE' if _struct_wt_limit > 100.0 else 'inactive'}")

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
                    'distance_reject': safe_float(runway.findtext('distance_reject', '0')),
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
                    'engine_designation': flight_info.get('engine_designation',''),
                    'aircraft': flight_info.get('aircraft','ERR'),
                    'icaocode': flight_info.get('icaocode','ERR'),
                    'wind': flight_info.get('wind','ERR'),
                    'tow': flight_info.get('tow','ERR'),
                    'flight_number': flt_node.text.strip() if flt_node is not None and flt_node.text else 'ERR',
                    'fin': fin_node.text.strip() if fin_node is not None and fin_node.text else 'ERR',
                    'dte_time': dte_node.text.strip() if dte_node is not None and dte_node.text else datetime.now(timezone.utc).strftime("%d/%H%MZ"),
                }

                valid_runways.append(runway_data)
                LOG.debug(f"[DBG: Added runway {runway_data['id']} to valid_runways")
            else:
                LOG.debug(f"[DBG: Runway {runway.findtext('identifier', 'XX')} filtered out by is_valid_runway()")

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

        LOG.debug(f"[DBG: runway_lines built with {len(runway_lines)} entries")
        LOG.debug(f"[DBG: Final valid_runways count: {len(valid_runways)}")

        return valid_runways, flight_info, anti_ice_on, runway_lines

    except Exception as e:
        LOG.error(f"Error in extract_runway_data: {e}")
        traceback.print_exc()
        return [], {}, False, []

def parse_xml_string(xml_string):
    """Parse XML string into ElementTree object"""
    import xml.etree.ElementTree as ET
    try:
        if isinstance(xml_string, str):
            return ET.fromstring(xml_string)
        return xml_string  # Already parsed
    except ET.ParseError as e:
        LOG.error(f"Error parsing XML: {e}")
        return None

def get_text(xpath, root, default=""):
    """
    Safely extract text from XML element using xpath.
    Handles string XML roots by parsing them first.
    """
    try:
        if isinstance(root, str):
            root = parse_xml_string(root)
            if root is None:
                return default

        if hasattr(root, 'find'):
            elem = root.find(xpath)
            if elem is None:
                elem = root.find(".//" + xpath.split('/')[-1])
        else:
            LOG.warning(f"Unexpected XML root type: {type(root)}")
            return default

        if elem is None:
            return default
        if not hasattr(elem, 'text'):
            return default
        return elem.text.strip() if elem.text else default

    except Exception as e:
        LOG.error(f"Error extracting text from xpath '{xpath}': {e}")
        return default

def _pick_atc_freq(freq_list, *type_codes):
    """
    Return frequency_mhz for the first matching type code from freq_list, else ''.
    Multiple codes tried in priority order, e.g.:
      _pick_atc_freq(freqs, 'ATIS', 'ASOS', 'AWOS')
    """
    for code in type_codes:
        for entry in freq_list:
            if entry['type'] == code:
                return entry['freq']
    return ''

def format_coord_aviation(coord_str, is_lon=False):
    """
    Format a decimal degree coordinate into compact aviation style (legacy helper).
    Lat:  N3444.6  or  S0327.1
    Lon:  W12035.1 or  E01423.8
    """
    if not coord_str:
        return "-------"
    try:
        val = float(coord_str)
        if is_lon:
            hem = "W" if val < 0 else "E"
            deg = int(abs(val))
            mins = (abs(val) - deg) * 60
            return f"{hem}{deg:03d}{mins:04.1f}"
        else:
            hem = "N" if val >= 0 else "S"
            deg = int(abs(val))
            mins = (abs(val) - deg) * 60
            return f"{hem}{deg:02d}{mins:04.1f}"
    except Exception:
        return "-------"

def format_time_hhmm(seconds_str):
    """Convert seconds to HH:MM format"""
    if not seconds_str:
        return "00:00"
    try:
        total_seconds = int(seconds_str)
        minutes = total_seconds // 60
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"
    except Exception:
        return "00:00"

def format_coord(coord_str, width):
    """Format coordinate to specified width, removing decimal point (legacy helper)"""
    if not coord_str:
        return "0" * width
    try:
        val = abs(float(coord_str))
        formatted = f"{val:.10f}".replace('.', '')[:width]
        return formatted.ljust(width, '0')
    except Exception:
        return "0" * width

def get_suitability_window(etops_section, icao_code):
    """Extract suitability window for airport from suitable_airport elements"""
    if not icao_code:
        return ""
    try:
        suitable_airports = etops_section.findall("suitable_airport")
        for apt in suitable_airports:
            if apt.findtext("icao_code") == icao_code:
                start = apt.findtext("suitability_start", "")
                end = apt.findtext("suitability_end", "")
                if start and end:
                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                    return f"{start_dt.strftime('%H%M')}-{end_dt.strftime('%H%M')}Z"
        return ""
    except Exception:
        return ""

def debug_xml_structure(xml_root, max_depth=2, current_depth=0):
    """Helper function to debug XML structure"""
    if current_depth >= max_depth or xml_root is None:
        return
    if isinstance(xml_root, str):
        xml_root = parse_xml_string(xml_root)
        if xml_root is None:
            return
    if hasattr(xml_root, 'tag'):
        indent = "  " * current_depth
        LOG.debug(f"{indent}Tag: {xml_root.tag}")
        if xml_root.text and xml_root.text.strip():
            text_preview = xml_root.text.strip()[:50]
            LOG.debug(f"{indent}Text: {text_preview}{'...' if len(xml_root.text.strip()) > 50 else ''}")
        if xml_root.attrib:
            LOG.debug(f"{indent}Attributes: {xml_root.attrib}")
        children = list(xml_root)[:5]
        for child in children:
            debug_xml_structure(child, max_depth, current_depth + 1)
        if len(list(xml_root)) > 5:
            LOG.debug(f"{indent}... and {len(list(xml_root)) - 5} more children")
