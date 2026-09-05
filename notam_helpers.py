#!/usr/bin/env python3
"""
notam_helpers.py  — NOTAM parsing, categorisation, and text rendering.

Exports used by MASTERLOG_Jepp.py:
    get_departure_notams_sorted(xml_root, section_name) -> str
    get_arrival_notams_sorted(xml_root, section_name)   -> str
    get_alternate_notams_sorted(xml_root, section_name) -> str
    get_enroute_notams(xml_root)                        -> str
"""

from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# NOTAM HELPERS — shared logic used by departure, arrival, alternate, enroute
# ═══════════════════════════════════════════════════════════════════════════════

import textwrap as _tw
import re as _re

# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_iso_date(raw):
    """Parse ISO-8601 date string → UTC-aware datetime, or None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except Exception:
        return None


def _parse_dtg(raw):
    """Parse SimBrief enroute DTG (YYYYMMDDHHmm) → UTC-aware datetime, or None."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if len(s) >= 12 and s[:12].isdigit():
            from datetime import timezone as _tz
            return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                            int(s[8:10]), int(s[10:12]), tzinfo=_tz.utc)
    except Exception:
        pass
    return None


def _fmt_ofp_date(dt):
    """Format datetime → '2026-Feb-19 06:59' (OFP style). None → 'UFN'."""
    if dt is None:
        return "UFN"
    # strftime %b gives 'Feb' on most platforms; ensure capitalised
    return f"{dt.year}-{dt.strftime('%b')}-{dt.strftime('%d')} {dt.strftime('%H:%M')}"


def _is_expired(eff_dt, exp_dt):
    """Return True if this NOTAM has definitively expired."""
    from datetime import timezone as _tz
    if exp_dt and exp_dt < datetime.now(_tz.utc):
        return True
    return False


def _expire_str(date_exp_raw, is_estimated):
    """
    Build the expiry display string and return the exp_dt used for expiry checking.

      '2026-Feb-21 14:30'           — known hard expiry  -> exp_dt returned for checking
      'UFN'                          — no expiry          -> None returned (never expired)
      'UFN(EST 2026-Feb-25 18:00)'  — UFN with estimate  -> None returned (UFN = still active)

    UFN NOTAMs (is_estimated=True, or no date at all) are NEVER considered expired:
    the estimated date is display-only and must not trigger the expired flag.
    """
    exp_dt = _parse_iso_date(date_exp_raw)
    if is_estimated:
        # UFN with optional estimated date — display it but never mark as expired
        if exp_dt:
            return f"UFN(EST {_fmt_ofp_date(exp_dt)})", None
        return "UFN", None
    if exp_dt:
        # Hard expiry date — use for both display and expiry checking
        return _fmt_ofp_date(exp_dt), exp_dt
    return "UFN", None


# ── Category mapping ──────────────────────────────────────────────────────────

# ── Airport NOTAM Q-code subject → display category ──────────────────────────
# notam_qcode_subject values come directly from SimBrief (e.g. "Apron", "Runway")
# Exact match first, then substring fallback via _QCODE_CAT_MAP
_QCODE_SUBJECT_EXACT = {
    # Movement & landing area
    'Runway':                   'RUNWAY',
    'Taxiway':                  'TAXIWAY',
    'Apron':                    'APRON',
    'Movement Area':            'APRON',
    'Parking Area':             'APRON',
    'Bearing Strength':         'RUNWAY',
    'Declared Distances':       'RUNWAY',
    'Threshold':                'RUNWAY',
    'Stopway':                  'RUNWAY',
    'Clearway':                 'RUNWAY',
    'Rapid Exit Taxiway':       'TAXIWAY',
    # Approach & landing
    'Approach Lighting':        'APPROACH AND LANDING',
    'PAPI':                     'APPROACH AND LANDING',
    'VASIS':                    'APPROACH AND LANDING',
    'ILS':                      'APPROACH AND LANDING',
    'Localizer':                'APPROACH AND LANDING',
    'Glide Path':               'APPROACH AND LANDING',
    'Instrument Approach':      'APPROACH AND LANDING',
    'Approach Procedures':      'APPROACH AND LANDING',
    'Landing':                  'APPROACH AND LANDING',
    'MLS':                      'APPROACH AND LANDING',
    # Departure
    'SID':                      'DEPARTURE PROCEDURES',
    'Standard Instrument Departure': 'DEPARTURE PROCEDURES',
    'Departure Procedures':     'DEPARTURE PROCEDURES',
    # Navigation aids
    'VOR':                      'NAVIGATION AIDS',
    'DME':                      'NAVIGATION AIDS',
    'NDB':                      'NAVIGATION AIDS',
    'TACAN':                    'NAVIGATION AIDS',
    'VORTAC':                   'NAVIGATION AIDS',
    'Navigation Aid':           'NAVIGATION AIDS',
    'GNSS':                     'NAVIGATION AIDS',
    # Communication
    'Communication':            'COMMUNICATION',
    'Radio':                    'COMMUNICATION',
    'SELCAL':                   'COMMUNICATION',
    'Radar':                    'COMMUNICATION',
    # Lighting
    'Approach Lights':          'APPROACH AND LANDING',
    'Runway Lights':            'RUNWAY',
    'Taxiway Lights':           'TAXIWAY',
    'Lighting':                 'GENERAL',
    # Services
    'Services':                 'GENERAL',
    'Fuel':                     'GENERAL',
    'De-icing':                 'GENERAL',
    'Fire and Rescue':          'GENERAL',
    'Customs':                  'GENERAL',
    # Obstacles / warnings
    'Obstacle':                 'GENERAL',
    'Warning':                  'WARNING',
    'Other':                    'GENERAL',
    'Airport':                  'GENERAL',
}

# Substring fallback (casefolded key must appear in casefolded subject)
_QCODE_CAT_MAP = {
    'approach':       'APPROACH AND LANDING',
    'landing':        'APPROACH AND LANDING',
    'ils':            'APPROACH AND LANDING',
    'runway':         'RUNWAY',
    'apron':          'APRON',
    'taxiway':        'TAXIWAY',
    'navigation aid': 'NAVIGATION AIDS',
    'vor':            'NAVIGATION AIDS',
    'dme':            'NAVIGATION AIDS',
    'ndb':            'NAVIGATION AIDS',
    'communication':  'COMMUNICATION',
    'radio':          'COMMUNICATION',
    'sid':            'DEPARTURE PROCEDURES',
    'departure proc': 'DEPARTURE PROCEDURES',
    'obstacle':       'GENERAL',
    'warning':        'WARNING',
    'services':       'GENERAL',
    'other':          'GENERAL',
    'airport':        'GENERAL',
}

# Text keyword scan → category (tried when q-code mapping fails or gives GENERAL)
_KEYWORD_CAT = [
    (['SID ', 'DEPARTURE (RNAV)', 'ODP ', 'OBSTACLE DEPARTURE',
      'STANDARD INSTRUMENT DEPARTURE'],             'DEPARTURE PROCEDURES'),
    (['ILS ', 'LOC ', 'IAP ', 'APPROACH', 'PAPI',
      'ALS ', 'RVR ', ' LANDING'],                 'APPROACH AND LANDING'),
    [['RWY ', 'RUNWAY '],                           'RUNWAY'],
    [['TWY ', 'TAXI', 'TAXIWAY'],                   'TAXIWAY'],
    [['APRON', ' RAMP', 'STAND ', 'GATE '],         'APRON'],
    [['COM ', 'COMM ', 'RADIO', 'FREQ '],            'COMMUNICATION'],
    [['VORTAC', 'VOR ', 'DME ', 'NDB ', 'NAVAID',
      'TACAN', ' ILS '],                            'NAVIGATION AIDS'],
]


def _map_notam_category(qcode_category, qcode_subject, text, valid_cats):
    """
    Map a NOTAM to a display category from valid_cats.
    Priority:
      1. Exact match on notam_qcode_subject  (SimBrief pre-decoded value e.g. "Apron")
      2. Exact match on notam_qcode_category (e.g. "Airport")
      3. Substring scan via _QCODE_CAT_MAP
      4. Keyword scan of NOTAM body text
      5. GENERAL / OTHER fallback
    """
    # 1. Exact subject lookup — highest fidelity
    exact = _QCODE_SUBJECT_EXACT.get(qcode_subject)
    if exact and exact in valid_cats:
        return exact

    # 2. Exact category lookup
    exact_cat = _QCODE_SUBJECT_EXACT.get(qcode_category)
    if exact_cat and exact_cat in valid_cats:
        return exact_cat

    # 3. Substring fallback on both fields
    for raw in (qcode_subject, qcode_category):
        key = raw.casefold()
        for pat, cat in _QCODE_CAT_MAP.items():
            if pat in key and cat in valid_cats:
                return cat

    # 4. Keyword scan of body text
    text_upper = " " + text.upper() + " "
    for entry in _KEYWORD_CAT:
        keywords, cat = entry[0], entry[1]
        if cat in valid_cats and any(kw in text_upper for kw in keywords):
            return cat

    return 'GENERAL' if 'GENERAL' in valid_cats else 'OTHER'


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render_notam_entry(loc_icao, loc_iata, nid, eff_dt, exp_str, cre_dt, text):
    """
    Render one NOTAM matching OFP layout exactly:

    KPHX PHX   A0339/26   2026-Feb-17 06:59 - 2026-Feb-21 14:30
    CREATED:2026-Feb-15 10:12   FL: SFC - UNL

    PHX RWY 07L/25R CLSD
    """
    iata  = loc_iata if loc_iata else ""
    eff_s = _fmt_ofp_date(eff_dt) if eff_dt else "UFN"

    # Column-align: "KPHX PHX" left-padded to 9 chars, NID to 10, then dates
    id_col  = f"{loc_icao} {iata}"        # e.g. "KPHX PHX"
    hdr     = f"{id_col:<9} {nid:<12} {eff_s} - {exp_str}"

    cre_s   = f"CREATED:{_fmt_ofp_date(cre_dt)}   " if cre_dt else "   "
    line2   = f"{cre_s}FL: SFC - UNL"

    body_lines = []
    for para in text.splitlines():
        para = para.strip()
        if para:
            body_lines.append(_tw.fill(para, width=72))
    body = "\n".join(body_lines)

    return f"{hdr}\n{line2}\n\n{body}\n"


def _cat_banner(label):
    """
    Dark-banner category label matching OFP style.
    Rendered as a solid border line with centred label, e.g.:

    ═══════════════════════════════ GENERAL ════════════════════════════════
    """
    BW = 72
    inner = f" {label} "
    pad   = BW - len(inner)
    left  = pad // 2
    right = pad - left
    return f"\n{'═' * left}{inner}{'═' * right}\n"


# ── Collection ────────────────────────────────────────────────────────────────

def _get_airport_code_name(section):
    """Extract ICAO code and name from an XML section element."""
    code, name = "", ""
    for f in ('icao_code', 'icao', 'code', 'airport_code'):
        code = (section.findtext(f) or "").strip()
        if code:
            break
    for f in ('name', 'airport_name', 'location_name'):
        name = (section.findtext(f) or "").strip()
        if name:
            break
    return code, name


def _collect_airport_notams(notams_list, category_order):
    """
    Parse <notam> elements → {category: [(nid, rendered, is_expired), ...]}
    Expired NOTAMs are included and flagged; renderer puts them at the bottom.
    """
    categorized = {cat: [] for cat in category_order}

    for n in notams_list:
        qcode_cat  = (n.findtext("notam_qcode_category") or "").strip()
        qcode_subj = (n.findtext("notam_qcode_subject")  or "").strip()
        text       = (n.findtext("notam_text")           or "").strip()
        nid        = (n.findtext("notam_id")             or "---").strip()
        date_eff   = (n.findtext("date_effective")       or "").strip()
        date_exp   = (n.findtext("date_expire")          or "").strip()
        date_cre   = (n.findtext("date_created")
                      or n.findtext("date_modified")     or "").strip()
        loc_icao   = (n.findtext("location_icao")        or "").strip()
        loc_iata   = (n.findtext("location_id")          or "").strip()
        # date_expire_is_estimated is an empty tag — present means estimated
        is_est     = n.find("date_expire_is_estimated") is not None

        if not text:
            continue

        eff_dt         = _parse_iso_date(date_eff)
        cre_dt         = _parse_iso_date(date_cre)
        exp_s, exp_dt  = _expire_str(date_exp, is_est)
        expired        = _is_expired(eff_dt, exp_dt)

        cat = _map_notam_category(qcode_cat, qcode_subj, text, category_order)
        if cat not in category_order:
            cat = 'GENERAL' if 'GENERAL' in category_order else 'OTHER'

        rendered = _render_notam_entry(loc_icao, loc_iata, nid,
                                        eff_dt, exp_s, cre_dt, text)
        categorized[cat].append((nid, rendered, expired))

    return categorized


# ── Section builder ───────────────────────────────────────────────────────────

def _build_notam_section(category_order, categorized, page_break_after=False):
    """
    Render categorised NOTAMs matching OFP layout.
    Active/future first, expired at the bottom of each category section.
    Empty categories are omitted entirely.
    """
    result = ""

    for cat in category_order:
        items = categorized.get(cat, [])
        if not items:
            continue

        active  = [(nid, r) for nid, r, exp in items if not exp]
        expired = [(nid, r) for nid, r, exp in items if exp]

        if not active and not expired:
            continue

        result += _cat_banner(cat)

        for nid, rendered in sorted(active, key=lambda x: x[0], reverse=True):
            result += rendered + "\n"

        # Only emit the expired divider and expired entries when expired NOTAMs
        # actually exist in this category — never emit an empty expired section.
        if expired:
            result += f"\n{'--- EXPIRED ---':^72}\n\n"
            for nid, rendered in sorted(expired, key=lambda x: x[0], reverse=True):
                result += rendered + "\n"

    if page_break_after and result:
        result += "\n[PAGEBREAK]\n"

    return result


# ── Airport block header ──────────────────────────────────────────────────────

def _airport_notam_header(airport_code, airport_name, role, runways="", iata=""):
    """
    Render the airport block header matching OFP badge style.
    iata is the real IATA code passed in from the XML section.
    """
    BW    = 72
    right = f"{iata} - {airport_name}" if iata and airport_name else airport_name
    left_part  = f"{airport_code}   {role}"
    line1 = f"{left_part:<42}{right}"
    indent = " " * (len(airport_code) + 3)

    out = f"\n{'═' * BW}\n"
    out += f"{line1}\n"
    out += f"{indent}IA\n"
    if runways:
        # Group runway pairs: max 2 pairs per line
        rwy_parts = runways.split()
        lines_rwy = []
        for j in range(0, len(rwy_parts), 2):
            lines_rwy.append(" ".join(rwy_parts[j:j+2]))
        out += f"{indent}RWYS: {lines_rwy[0]}\n"
        for extra in lines_rwy[1:]:
            out += f"{indent}      {extra}\n"
    out += f"{'═' * BW}\n\n"
    return out


def _make_runway_pair(rwy_id):
    """
    Given a single runway end identifier (e.g. '07L'), return the full paired
    designator (e.g. '07L/25R'). Returns None if the input is not a valid runway.
    Always puts the lower-numbered end first.
    """
    import re as _re3
    m = _re3.match(r'^(\d{1,2})([LRC]?)$', rwy_id.strip().upper())
    if not m:
        return None
    num    = int(m.group(1))
    suffix = m.group(2)
    if not (1 <= num <= 36):
        return None
    recip_num    = num + 18 if num <= 18 else num - 18
    flip         = {'L': 'R', 'R': 'L', 'C': 'C', '': ''}
    recip_suffix = flip.get(suffix, '')
    end1 = f"{num:02d}{suffix}"
    end2 = f"{recip_num:02d}{recip_suffix}"
    return f"{end1}/{end2}" if num <= recip_num else f"{end2}/{end1}"


def _get_runways(xml_root, section):
    """
    Build the full airport runway list from TLR data (most reliable source).
    Each TLR <runway><identifier> gives one end; we compute the reciprocal pair
    and deduplicate so '07L' and '25R' both resolve to '07L/25R' (once).

    TLR has full runway data for both departure (tlr/takeoff) and destination
    (tlr/landing). The correct sub-section is chosen by matching the airport
    ICAO code in <conditions><airport_icao>.
    Alternates fall back to plan_rwy (no TLR data available for them).

    Returns a space-separated string like "07L/25R 07R/25L 08/26".
    """
    import re as _re3

    def sort_key(r):
        n = _re3.search(r'\d+', r)
        return int(n.group()) if n else 99

    def collect_runways(rwy_elements):
        seen_pairs = set()
        ordered    = []
        for rwy in rwy_elements:
            rwy_id = (rwy.findtext('identifier') or "").strip().upper()
            if not rwy_id:
                continue
            pair = _make_runway_pair(rwy_id)
            if pair and pair not in seen_pairs:
                seen_pairs.add(pair)
                ordered.append(pair)
        if ordered:
            return " ".join(sorted(ordered, key=sort_key))
        return ""

    # Get this section's airport ICAO
    airport_icao = (section.findtext("icao_code") or
                    section.findtext("icao")      or
                    section.findtext("icao_id")   or "").strip().upper()

    # Try tlr/takeoff — matches if conditions/airport_icao == this airport
    takeoff_node = xml_root.find('.//tlr/takeoff')
    if takeoff_node is not None:
        to_icao = (takeoff_node.findtext('conditions/airport_icao') or "").strip().upper()
        if to_icao and (to_icao == airport_icao or not airport_icao):
            result = collect_runways(takeoff_node.findall('runway'))
            if result:
                return result

    # Try tlr/landing — matches if conditions/airport_icao == this airport
    landing_node = xml_root.find('.//tlr/landing')
    if landing_node is not None:
        ld_icao = (landing_node.findtext('conditions/airport_icao') or "").strip().upper()
        if ld_icao and (ld_icao == airport_icao or not airport_icao):
            result = collect_runways(landing_node.findall('runway'))
            if result:
                return result

    # Alternate (no TLR data): fall back to plan_rwy
    plan = (section.findtext("plan_rwy") or "").strip().upper()
    if plan:
        pair = _make_runway_pair(plan)
        return pair if pair else plan
    return ""


# ── Departure NOTAMs ──────────────────────────────────────────────────────────

def get_departure_notams_sorted(xml_root, section_name):
    """Get NOTAMs relevant for departure operations, always showing all categories"""
    section = xml_root.find(f".//{section_name}")
    if not section:
        return ""

    notams_container = section.find("notams")
    if notams_container is not None:
        notams_list = notams_container.findall("notam")
    else:
        notams_list = section.findall(".//notam")

    airport_code = ""
    airport_name = ""
    for field in ['icao', 'code', 'airport_code', 'location_icao']:
        airport_code = section.findtext(field, "").strip()
        if airport_code:
            break
    for field in ['name', 'airport_name', 'location_name']:
        airport_name = section.findtext(field, "").strip()
        if airport_name:
            break
    if not airport_code and notams_list:
        first_notam = notams_list[0]
        airport_code = first_notam.findtext('location_icao', '') or first_notam.findtext('account_id', '')
        airport_name = first_notam.findtext('location_name', '')

    # Define full departure category order
    departure_category_order = [
        'RUNWAY',       # RWY
        'TAXIWAY',      # TWY
        'APRON',        # APR
        'COMMUNICATION',# COM
        'SID/ODP',      # SID/ODP
        'APPROACH AND LANDING', # Approach
        'SERVICES',
        'WARNING',
        'OTHER'
    ]

    # Initialize categories
    categorized = {cat: [] for cat in departure_category_order}

    # Categorization logic
    qcode_category_mapping = {
        'Approach Procedures': 'APPROACH AND LANDING',
        'approach procedures': 'APPROACH AND LANDING',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Landing': 'APPROACH AND LANDING',
        'landing': 'APPROACH AND LANDING',
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON', 
        'apron': 'APRON',
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Navigation Aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS',
        'VOR': 'NAVIGATION AIDS',
        'vor': 'NAVIGATION AIDS',
        'DME': 'NAVIGATION AIDS',
        'dme': 'NAVIGATION AIDS',
        'ILS': 'APPROACH AND LANDING',
        'ils': 'APPROACH AND LANDING',
        'Communication': 'COMMUNICATION',
        'communication': 'COMMUNICATION',
        'Radio': 'COMMUNICATION',
        'radio': 'COMMUNICATION',
        'Services': 'SERVICES',
        'services': 'SERVICES',
        'Warning': 'WARNING',
        'warning': 'WARNING',
        'Other': 'OTHER',
        'other': 'OTHER'
    }
    subject_mapping = {
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON',
        'apron': 'APRON', 
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Navigation aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS'
    }

    for n in notams_list:
        qcode_category = n.findtext("notam_qcode_category", "").strip()
        qcode_subject = n.findtext("notam_qcode_subject", "").strip()
        text = (n.findtext("notam_text") or "").strip()
        nid = n.findtext("notam_id", "---")
        date_effective = n.findtext("date_effective", "")
        date_expire = n.findtext("date_expire", "")

        if not text:
            continue

        category = "OTHER"
        if qcode_category:
            mapped_category = qcode_category_mapping.get(qcode_category)
            if mapped_category is None:
                category = subject_mapping.get(qcode_subject, "OTHER")
            elif mapped_category:
                category = mapped_category
        if category == "OTHER" and qcode_subject:
            category = subject_mapping.get(qcode_subject, "OTHER")
        if category == "OTHER":
            text_upper = text.upper()
            if any(word in text_upper for word in ['SID', 'STANDARD INSTRUMENT DEPARTURE', 'ODP', 'OBSTACLE DEPARTURE PROCEDURE']):
                category = "SID/ODP"
            elif any(word in text_upper for word in ['RWY', 'RUNWAY']):
                category = "RUNWAY"
            elif any(word in text_upper for word in ['ILS', 'LOC', 'APPROACH', 'ALS', 'PAPI', 'RVR', 'LANDING', 'IAP']):
                category = "APPROACH AND LANDING"
            elif any(word in text_upper for word in ['TWY','SIDE','TAXI', 'TAXIWAY']):
                category = "TAXIWAY"
            elif any(word in text_upper for word in ['APRON', 'STAND', 'GATE', 'RAMP']):
                category = "APRON"
            elif any(word in text_upper for word in ['COMM', 'RADIO', 'FREQ']):
                category = "COMMUNICATION"
        if category not in departure_category_order:
            category = "OTHER"

        categorized[category].append((nid, text, date_effective, date_expire))

    # Build output
    result = "******************************************************\n"
    result += "                    DEPARTURE NOTAMs\n" 
    result += "******************************************************\n"
    result += "------------------------------------------------------\n"
    if airport_code and airport_name:
        result += f"- {airport_code}/{airport_code[1:]} - {airport_name}\n"
    elif airport_code:
        result += f"- {airport_code}\n"
    else:
        result += f"- {section_name.upper()}\n"
    result += "-----------------------------------------------------/\n"

    for category in departure_category_order:
        result += f"--------- {category} ---------\n"
        if categorized[category]:
            for nid, text, date_eff, date_exp in sorted(categorized[category], key=lambda x: x[0]):
                status = ""
                if date_eff and date_exp:
                    try:
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        eff_dt = datetime.fromisoformat(date_eff.replace('Z', '+00:00'))
                        exp_dt = datetime.fromisoformat(date_exp.replace('Z', '+00:00'))
                        status = " [ACTIVE]" if eff_dt <= now <= exp_dt else " [FUTURE]" if now < eff_dt else " [EXPIRED]"
                        date_range = f"{eff_dt.strftime('%d%b%y/%H%M').upper()} {exp_dt.strftime('%d%b%y/%H%M').upper()}"
                    except:
                        date_range = ""
                        status = " [UNKNOWN]"
                else:
                    date_range = ""
                    status = " [NO DATES]"
                result += f"- {text}\n"

                if date_range and airport_code:
                    result += f"{date_range} {airport_code} {nid}{status}\n"
                else:
                    result += f"{nid}{status}\n"

                result += "\n"  # add an extra line for spacing between NOTAMs



        else:
            result += "No NOTAMs in this category.\n\n"

    return result



def get_arrival_notams_sorted(xml_root, section_name):
    """Get NOTAMs relevant for arrival operations, always showing all categories"""
    section = xml_root.find(f".//{section_name}")
    if not section:
        return ""

    notams_container = section.find("notams")
    if notams_container is not None:
        notams_list = notams_container.findall("notam")
    else:
        notams_list = section.findall(".//notam")

    airport_code = ""
    airport_name = ""
    for field in ['icao', 'code', 'airport_code', 'location_icao']:
        airport_code = section.findtext(field, "").strip()
        if airport_code:
            break
    for field in ['name', 'airport_name', 'location_name']:
        airport_name = section.findtext(field, "").strip()
        if airport_name:
            break
    if not airport_code and notams_list:
        first_notam = notams_list[0]
        airport_code = first_notam.findtext('location_icao', '') or first_notam.findtext('account_id', '')
        airport_name = first_notam.findtext('location_name', '')

    # Define full arrival category order
    arrival_category_order = [
        'APPROACH AND LANDING', # Approach
        'RUNWAY',       # RWY
        'NAVIGATION AIDS',
        'TAXIWAY',      # TWY
        'APRON',        # APR
        'COMMUNICATION',# COM
        'SID/ODP',      # SID/ODP
        'SERVICES',
        'WARNING',
        'OTHER'
    ]

    # Initialize categories
    categorized = {cat: [] for cat in arrival_category_order}

    # Same categorization logic
    qcode_category_mapping = {
        'Approach Procedures': 'APPROACH AND LANDING',
        'approach procedures': 'APPROACH AND LANDING',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Landing': 'APPROACH AND LANDING',
        'landing': 'APPROACH AND LANDING',
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON', 
        'apron': 'APRON',
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Navigation Aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS',
        'VOR': 'NAVIGATION AIDS',
        'vor': 'NAVIGATION AIDS',
        'DME': 'NAVIGATION AIDS',
        'dme': 'NAVIGATION AIDS',
        'ILS': 'APPROACH AND LANDING',
        'ils': 'APPROACH AND LANDING',
        'Communication': 'COMMUNICATION',
        'communication': 'COMMUNICATION',
        'Radio': 'COMMUNICATION',
        'radio': 'COMMUNICATION',
        'Services': 'SERVICES',
        'services': 'SERVICES',
        'Warning': 'WARNING',
        'warning': 'WARNING',
        'Other': 'OTHER',
        'other': 'OTHER'
    }
    subject_mapping = {
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON',
        'apron': 'APRON', 
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Navigation aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS'
    }

    for n in notams_list:
        qcode_category = n.findtext("notam_qcode_category", "").strip()
        qcode_subject = n.findtext("notam_qcode_subject", "").strip()
        text = (n.findtext("notam_text") or "").strip()
        nid = n.findtext("notam_id", "---")
        date_effective = n.findtext("date_effective", "")
        date_expire = n.findtext("date_expire", "")

        if not text:
            continue

        category = "OTHER"
        if qcode_category:
            mapped_category = qcode_category_mapping.get(qcode_category)
            if mapped_category is None:
                category = subject_mapping.get(qcode_subject, "OTHER")
            elif mapped_category:
                category = mapped_category
        if category == "OTHER" and qcode_subject:
            category = subject_mapping.get(qcode_subject, "OTHER")
        if category == "OTHER":
            text_upper = text.upper()
            if any(word in text_upper for word in ['SID', 'STANDARD INSTRUMENT DEPARTURE', 'ODP', 'OBSTACLE DEPARTURE PROCEDURE']):
                category = "SID/ODP"
            elif any(word in text_upper for word in ['RWY', 'RUNWAY']):
                category = "RUNWAY"
            elif any(word in text_upper for word in ['ILS', 'LOC', 'APPROACH', 'ALS', 'PAPI', 'RVR', 'LANDING', 'IAP']):
                category = "APPROACH AND LANDING"
            elif any(word in text_upper for word in ['TWY','SIDE','TAXI', 'TAXIWAY']):
                category = "TAXIWAY"
            elif any(word in text_upper for word in ['APRON', 'STAND', 'GATE', 'RAMP']):
                category = "APRON"
            elif any(word in text_upper for word in ['COMM', 'RADIO', 'FREQ']):
                category = "COMMUNICATION"

        if category not in arrival_category_order:
            category = "OTHER"

        categorized[category].append((nid, text, date_effective, date_expire))

    # Build output
    result = "******************************************************\n"
    result += "                     ARRIVAL NOTAMs\n"
    result += "******************************************************\n"
    result += "------------------------------------------------------\n"
    if airport_code and airport_name:
        result += f"- {airport_code}/{airport_code[1:]} - {airport_name}\n"
    elif airport_code:
        result += f"- {airport_code}\n"
    else:
        result += f"- {section_name.upper()}\n"
    result += "-----------------------------------------------------/\n"

    for category in arrival_category_order:
        result += f"--------- {category} ---------\n"
        if categorized[category]:
            for nid, text, date_eff, date_exp in sorted(categorized[category], key=lambda x: x[0]):
                status = ""
                if date_eff and date_exp:
                    try:
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        eff_dt = datetime.fromisoformat(date_eff.replace('Z', '+00:00'))
                        exp_dt = datetime.fromisoformat(date_exp.replace('Z', '+00:00'))
                        status = " [ACTIVE]" if eff_dt <= now <= exp_dt else " [FUTURE]" if now < eff_dt else " [EXPIRED]"
                        date_range = f"{eff_dt.strftime('%d%b%y/%H%M').upper()} {exp_dt.strftime('%d%b%y/%H%M').upper()}"
                    except:
                        date_range = ""
                        status = " [UNKNOWN]"
                else:
                    date_range = ""
                    status = " [NO DATES]"
                result += f"-{text}\n"
                if date_range and airport_code:
                    result += f"{date_range} {airport_code} {nid}{status}\n"
                else:
                    result += f"{nid}{status}\n"
                result += "\n"  
        else:
            result += "No NOTAMs in this category.\n\n"
    return result


def get_alternate_notams_sorted(xml_root, section_name):
    """Get NOTAMs relevant for alternate operations, always showing all categories"""
    section = xml_root.find(f".//{section_name}")
    if not section:
        return ""

    notams_container = section.find("notams")
    if notams_container is not None:
        notams_list = notams_container.findall("notam")
    else:
        notams_list = section.findall(".//notam")

    airport_code = ""
    airport_name = ""
    for field in ['icao', 'code', 'airport_code', 'location_icao']:
        airport_code = section.findtext(field, "").strip()
        if airport_code:
            break
    for field in ['name', 'airport_name', 'location_name']:
        airport_name = section.findtext(field, "").strip()
        if airport_name:
            break
    if not airport_code and notams_list:
        first_notam = notams_list[0]
        airport_code = first_notam.findtext('location_icao', '') or first_notam.findtext('account_id', '')
        airport_name = first_notam.findtext('location_name', '')

    # Define full category order
    category_order = [
        'APPROACH AND LANDING',
        'RUNWAY',
        'NAVIGATION AIDS',
        'TAXIWAY',
        'APRON',
        'COMMUNICATION',
        'SID/ODP',
        'SERVICES',
        'WARNING',
        'OTHER'
    ]

    categorized = {cat: [] for cat in category_order}

    # Same categorization logic
    qcode_category_mapping = {
        'Approach Procedures': 'APPROACH AND LANDING',
        'approach procedures': 'APPROACH AND LANDING',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Landing': 'APPROACH AND LANDING',
        'landing': 'APPROACH AND LANDING',
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON', 
        'apron': 'APRON',
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Navigation Aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS',
        'VOR': 'NAVIGATION AIDS',
        'vor': 'NAVIGATION AIDS',
        'DME': 'NAVIGATION AIDS',
        'dme': 'NAVIGATION AIDS',
        'ILS': 'APPROACH AND LANDING',
        'ils': 'APPROACH AND LANDING',
        'Communication': 'COMMUNICATION',
        'communication': 'COMMUNICATION',
        'Radio': 'COMMUNICATION',
        'radio': 'COMMUNICATION',
        'Services': 'SERVICES',
        'services': 'SERVICES',
        'Warning': 'WARNING',
        'warning': 'WARNING',
        'Other': 'OTHER',
        'other': 'OTHER'
    }
    subject_mapping = {
        'Runway': 'RUNWAY',
        'runway': 'RUNWAY',
        'Apron': 'APRON',
        'apron': 'APRON', 
        'Taxiway': 'TAXIWAY',
        'taxiway': 'TAXIWAY',
        'Instrument approach procedure': 'APPROACH AND LANDING',
        'instrument approach procedure': 'APPROACH AND LANDING',
        'Navigation aid': 'NAVIGATION AIDS',
        'navigation aid': 'NAVIGATION AIDS'
    }

    for n in notams_list:
        qcode_category = n.findtext("notam_qcode_category", "").strip()
        qcode_subject = n.findtext("notam_qcode_subject", "").strip()
        text = (n.findtext("notam_text") or "").strip()
        nid = n.findtext("notam_id", "---")
        date_effective = n.findtext("date_effective", "")
        date_expire = n.findtext("date_expire", "")

        if not text:
            continue

        category = "OTHER"
        if qcode_category:
            mapped_category = qcode_category_mapping.get(qcode_category)
            if mapped_category is None:
                category = subject_mapping.get(qcode_subject, "OTHER")
            elif mapped_category:
                category = mapped_category
        if category == "OTHER" and qcode_subject:
            category = subject_mapping.get(qcode_subject, "OTHER")
        if category == "OTHER":
            text_upper = text.upper()
            if any(word in text_upper for word in ['RWY', 'RUNWAY']):
                category = "RUNWAY"
            elif any(word in text_upper for word in ['ILS', 'LOC', 'APPROACH', 'ALS', 'PAPI', 'RVR', 'LANDING', 'IAP']):
                category = "APPROACH AND LANDING"
            elif any(word in text_upper for word in ['TWY','SIDE','TAXI', 'TAXIWAY']):
                category = "TAXIWAY"
            elif any(word in text_upper for word in ['APRON', 'STAND', 'GATE', 'RAMP']):
                category = "APRON"
            elif any(word in text_upper for word in ['COMM', 'RADIO', 'FREQ']):
                category = "COMMUNICATION"
            elif any(word in text_upper for word in ['SID', 'STANDARD INSTRUMENT DEPARTURE','DEPARTURE', 'ODP', 'OBSTACLE DEPARTURE PROCEDURE']):
                category = "SID/ODP"
        if category not in category_order:
            category = "OTHER"

        categorized[category].append((nid, text, date_effective, date_expire))

    # Build output
    result = "******************************************************\n"
    result += "                     ALTERNATE NOTAMs\n"
    result += "******************************************************\n"
    result += "------------------------------------------------------\n"
    if airport_code and airport_name:
        result += f"- {airport_code}/{airport_code[1:]} - {airport_name}\n"
    elif airport_code:
        result += f"- {airport_code}\n"
    else:
        result += f"- {section_name.upper()}\n"
    result += "-----------------------------------------------------/\n"

    for category in category_order:
        result += f"--------- {category} ---------\n"
        if categorized[category]:
            for nid, text, date_eff, date_exp in sorted(categorized[category], key=lambda x: x[0]):
                status = ""
                if date_eff and date_exp:
                    try:
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        eff_dt = datetime.fromisoformat(date_eff.replace('Z', '+00:00'))
                        exp_dt = datetime.fromisoformat(date_exp.replace('Z', '+00:00'))
                        status = " [ACTIVE]" if eff_dt <= now <= exp_dt else " [FUTURE]" if now < eff_dt else " [EXPIRED]"
                        date_range = f"{eff_dt.strftime('%d%b%y/%H%M').upper()} {exp_dt.strftime('%d%b%y/%H%M').upper()}"
                    except:
                        date_range = ""
                        status = " [UNKNOWN]"
                else:
                    date_range = ""
                    status = " [NO DATES]"
                result += f"-{text}\n"
                if date_range and airport_code:
                    result += f"{date_range} {airport_code} {nid}{status}\n"
                else:
                    result += f"{nid}{status}\n"
                result += "\n"  
        else:
            result += "No NOTAMs in this category.\n\n"
    result += "\n[PAGEBREAK]\n"
    return result



# ── Enroute NOTAMs ────────────────────────────────────────────────────────────


# ── FAA/ICAO Q-code subject decode (2nd+3rd letters) ─────────────────────────
# Source: FAA Order 7930.2 Appendix B — https://www.faa.gov/air_traffic/publications/atpubs/notam_html/appendix_b.html
_QCODE_SUBJECT_FULL = {
    # A — ATM Airspace Organisation
    "AA": "AIRSPACE",        "AC": "AIRSPACE",       "AD": "AIRSPACE",
    "AE": "AIRSPACE",        "AF": "AIRSPACE",       "AH": "AIRSPACE",
    "AL": "AIRSPACE",        "AN": "AIRSPACE",        "AO": "AIRSPACE",
    "AP": "AIRSPACE",        "AR": "AIRSPACE",        "AT": "AIRSPACE",
    "AU": "AIRSPACE",        "AV": "AIRSPACE",        "AX": "AIRSPACE",
    "AZ": "AIRSPACE",
    # C — CNS Communications & Surveillance
    "CA": "COMMUNICATION",   "CB": "COMMUNICATION",  "CC": "COMMUNICATION",
    "CD": "COMMUNICATION",   "CE": "COMMUNICATION",  "CG": "COMMUNICATION",
    "CL": "COMMUNICATION",   "CM": "COMMUNICATION",  "CP": "COMMUNICATION",
    "CR": "COMMUNICATION",   "CS": "COMMUNICATION",  "CT": "COMMUNICATION",
    # F — AGA Facilities & Services
    "FA": "SERVICES",        "FB": "SERVICES",        "FC": "SERVICES",
    "FD": "SERVICES",        "FE": "SERVICES",        "FF": "SERVICES",
    "FG": "SERVICES",        "FH": "SERVICES",        "FI": "SERVICES",
    "FJ": "SERVICES",        "FL": "SERVICES",        "FM": "SERVICES",
    "FO": "SERVICES",        "FP": "SERVICES",        "FS": "SERVICES",
    "FT": "SERVICES",        "FU": "SERVICES",        "FW": "SERVICES",
    "FZ": "SERVICES",
    # G — GNSS
    "GA": "NAVIGATION AIDS", "GW": "NAVIGATION AIDS",
    # I — ILS / MLS
    "IC": "APPROACH PROCEDURES",  "ID": "APPROACH PROCEDURES",
    "IG": "APPROACH PROCEDURES",  "II": "APPROACH PROCEDURES",
    "IL": "APPROACH PROCEDURES",  "IM": "APPROACH PROCEDURES",
    "IN": "APPROACH PROCEDURES",  "IO": "APPROACH PROCEDURES",
    "IS": "APPROACH PROCEDURES",  "IT": "APPROACH PROCEDURES",
    "IU": "APPROACH PROCEDURES",  "IW": "APPROACH PROCEDURES",
    "IX": "APPROACH PROCEDURES",  "IY": "APPROACH PROCEDURES",
    # L — AGA Lighting
    "LA": "LIGHTING",  "LB": "LIGHTING",  "LC": "LIGHTING",
    "LD": "LIGHTING",  "LE": "LIGHTING",  "LF": "LIGHTING",
    "LG": "LIGHTING",  "LH": "LIGHTING",  "LI": "LIGHTING",
    "LJ": "LIGHTING",  "LK": "LIGHTING",  "LL": "LIGHTING",
    "LM": "LIGHTING",  "LP": "LIGHTING",  "LR": "LIGHTING",
    "LS": "LIGHTING",  "LT": "LIGHTING",  "LU": "LIGHTING",
    "LV": "LIGHTING",  "LW": "LIGHTING",  "LX": "LIGHTING",
    "LY": "LIGHTING",  "LZ": "LIGHTING",
    # M — AGA Movement & Landing Area
    "MA": "MOVEMENT AREA",  "MB": "MOVEMENT AREA",  "MC": "MOVEMENT AREA",
    "MD": "MOVEMENT AREA",  "MG": "MOVEMENT AREA",  "MH": "MOVEMENT AREA",
    "MK": "MOVEMENT AREA",  "MM": "MOVEMENT AREA",  "MN": "MOVEMENT AREA",
    "MO": "MOVEMENT AREA",  "MP": "MOVEMENT AREA",  "MR": "RUNWAY",
    "MS": "MOVEMENT AREA",  "MT": "MOVEMENT AREA",  "MU": "MOVEMENT AREA",
    "MW": "MOVEMENT AREA",  "MX": "MOVEMENT AREA",  "MY": "MOVEMENT AREA",
    # N — Navigation Facilities
    "NA": "NAVIGATION AIDS",  "NB": "NAVIGATION AIDS",  "NC": "NAVIGATION AIDS",
    "ND": "NAVIGATION AIDS",  "NF": "NAVIGATION AIDS",  "NL": "NAVIGATION AIDS",
    "NM": "NAVIGATION AIDS",  "NN": "NAVIGATION AIDS",  "NO": "NAVIGATION AIDS",
    "NT": "NAVIGATION AIDS",  "NV": "NAVIGATION AIDS",
    # O — Other Information
    "OA": "SERVICES",   "OB": "OBSTACLE",  "OE": "SERVICES",
    "OL": "OBSTACLE",   "OR": "SERVICES",
    # P — ATM Air Traffic Procedures
    "PA": "PROCEDURES",  "PB": "PROCEDURES",  "PC": "PROCEDURES",
    "PD": "PROCEDURES",  "PE": "PROCEDURES",  "PF": "PROCEDURES",
    "PH": "PROCEDURES",  "PI": "APPROACH PROCEDURES",
    "PK": "PROCEDURES",  "PL": "PROCEDURES",  "PM": "PROCEDURES",
    "PN": "PROCEDURES",  "PO": "PROCEDURES",  "PR": "PROCEDURES",
    "PT": "PROCEDURES",  "PU": "APPROACH PROCEDURES",
    "PX": "PROCEDURES",  "PZ": "PROCEDURES",
    # R — Navigation Warnings: Airspace Restrictions
    "RA": "AIRSPACE RESTRICTIONS",  "RD": "AIRSPACE RESTRICTIONS",
    "RM": "AIRSPACE RESTRICTIONS",  "RO": "AIRSPACE RESTRICTIONS",
    "RP": "AIRSPACE RESTRICTIONS",  "RR": "AIRSPACE RESTRICTIONS",
    "RT": "AIRSPACE RESTRICTIONS",
    # S — ATM Air Traffic & VOLMET Services
    "SA": "SERVICES",  "SB": "SERVICES",  "SC": "SERVICES",
    "SE": "SERVICES",  "SF": "SERVICES",  "SL": "SERVICES",
    "SO": "SERVICES",  "SP": "SERVICES",  "SS": "SERVICES",
    "ST": "SERVICES",  "SU": "SERVICES",  "SV": "SERVICES",
    "SY": "SERVICES",
    # W — Navigation Warnings
    "WA": "WARNING",  "WB": "WARNING",  "WC": "WARNING",
    "WD": "WARNING",  "WE": "WARNING",  "WF": "WARNING",
    "WG": "WARNING",  "WH": "WARNING",  "WJ": "WARNING",
    "WL": "WARNING",  "WM": "WARNING",  "WP": "WARNING",
    "WR": "WARNING",  "WS": "WARNING",  "WT": "WARNING",
    "WU": "WARNING",  "WV": "WARNING",  "WW": "WARNING",
    "WY": "WARNING",  "WZ": "WARNING",
}

_ENRT_CATEGORY_ORDER = [
    "AIRSPACE RESTRICTIONS", "AIRSPACE", "PROCEDURES",
    "APPROACH PROCEDURES", "NAVIGATION AIDS", "COMMUNICATION",
    "LIGHTING", "MOVEMENT AREA", "RUNWAY", "OBSTACLE",
    "WARNING", "SERVICES", "GENERAL",
]

def _decode_qcode_enrt(raw_text, qcode_str=""):
    """
    Decode Q-code subject category.
    Tries qcode_str first (from XML notam_qcode field), then parses Q) line from raw text.
    Returns one of the categories in _ENRT_CATEGORY_ORDER.
    """
    import re as _rq
    def _subj_from_code(code):
        code = code.upper().strip()
        # Strip leading Q if present: QMNXX -> MN, or just take first 2 chars
        if code.startswith("Q") and len(code) >= 3:
            code = code[1:3]
        elif len(code) >= 2:
            code = code[:2]
        return _QCODE_SUBJECT_FULL.get(code, "GENERAL")

    # Try the pre-parsed qcode field first
    if qcode_str:
        cat = _subj_from_code(qcode_str)
        if cat != "GENERAL":
            return cat

    # Parse Q) field from raw NOTAM text
    # Formats: "Q) KZOB/QRTCA/..." or "Q) KZZZ/QWLWS/..."
    m = _rq.search(r"Q\)\s*\w*/Q([A-Z]{2})", raw_text)
    if m:
        return _QCODE_SUBJECT_FULL.get(m.group(1).upper(), "GENERAL")

    return "GENERAL"

def get_enroute_notams(xml_root):
    """
    Parse top-level <notams>/<notamdrec> enroute NOTAM block.
    Filters out any record whose icao_id matches the origin, destination,
    or alternate airport — those are already shown in the airport sections.
    Groups remaining NOTAMs by FIR, ordered by first appearance in navlog.
    """
    notams_root = xml_root.find("notams")
    if notams_root is None:
        return ""

    recs = notams_root.findall("notamdrec")
    if not recs:
        return ""

    # Build set of airport ICAOs to exclude — direct targeted lookups
    airport_icaos = set()
    for xpath in ("origin/icao_code", "destination/icao_code",
                  "alternate/icao_code", "altn1/icao_code"):
        v = (xml_root.findtext(xpath) or "").strip().upper()
        if v:
            airport_icaos.add(v)

    from datetime import timezone as _tz
    from collections import OrderedDict
    import textwrap as _tw

    # Build navlog-ordered FIR list
    navlog_fir_order = []
    seen_firs = set()
    for fix in xml_root.findall("navlog/fix"):
        fcode = (fix.findtext("fir") or "").strip().upper()
        if fcode and fcode not in seen_firs:
            navlog_fir_order.append(fcode)
            seen_firs.add(fcode)

    now = datetime.now(_tz.utc)
    by_facility = OrderedDict()  # facility → {'icao_id':..., 'active': [...], 'expired': [...]}

    import re as _re_enrt

    def _parse_notam_raw(raw, eff_dtg, exp_dtg, cre_dtg):
        """
        Parse a raw ICAO NOTAM text block. Returns (body, fl_str, eff_dt, exp_dt, cre_dt, is_est).
        Extracts fields directly from raw text when DTG attributes are missing/wrong.
        """
        # E) body — everything between E) and next field marker or end
        em = _re_enrt.search(r'\nE\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, _re_enrt.DOTALL)
        if not em:
            em = _re_enrt.search(r'^E\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, _re_enrt.DOTALL | _re_enrt.MULTILINE)
        body = em.group(1).strip() if em else raw.strip()

        # F)/G) flight levels
        f_m = _re_enrt.search(r'\nF\)\s*(\S+)', raw)
        g_m = _re_enrt.search(r'\nG\)\s*(\S+)', raw)
        fl_str = f"{f_m.group(1).upper() if f_m else 'SFC'} - {g_m.group(1).upper() if g_m else 'UNL'}"

        # B) effective — prefer DTG attribute, fall back to raw field
        eff_dt = _parse_dtg(eff_dtg)
        if not eff_dt:
            bm = _re_enrt.search(r'\nB\)\s*(\d{10})', raw)
            if bm:
                eff_dt = _parse_dtg(bm.group(1))

        # C) expiry + EST flag — parse from raw text for accuracy
        cm = _re_enrt.search(r'\nC\)\s*(\d{10}|PERM)\s*(EST)?', raw, _re_enrt.IGNORECASE)
        is_est = False
        exp_dt = None
        if cm:
            c_val = cm.group(1).upper()
            is_est = bool(cm.group(2))
            if c_val == "PERM":
                exp_dt = None     # permanent — never expires
            else:
                exp_dt = _parse_dtg(c_val) if not is_est else None  # EST = UFN, don't use for expiry
        else:
            # Fall back to DTG attribute
            exp_dt = _parse_dtg(exp_dtg)

        # CREATED — prefer DTG attribute
        cre_dt = _parse_dtg(cre_dtg)

        return body, fl_str, eff_dt, exp_dt, cre_dt, is_est

    for rec in recs:
        raw_text = (rec.findtext("notam_text") or "").strip()
        if not raw_text:
            continue

        nid      = (rec.findtext("notam_id")            or "---").strip()
        icao_id  = (rec.findtext("icao_id")             or "").strip().upper()

        # Skip if this NOTAM belongs to an airport already shown in airport sections
        if icao_id and icao_id in airport_icaos:
            continue

        facility = (rec.findtext("icao_name")           or
                    rec.findtext("cns_location_id")     or "ENROUTE").strip()
        eff_raw  = (rec.findtext("notam_effective_dtg") or "").strip()
        exp_raw  = (rec.findtext("notam_expire_dtg")    or "").strip()
        cre_raw  = (rec.findtext("notam_created_dtg")   or "").strip()

        body_text, fl_str, eff_dt, exp_dt, cre_dt, is_est = _parse_notam_raw(
            raw_text, eff_raw, exp_raw, cre_raw
        )
        is_expired = _is_expired(eff_dt, exp_dt)

        # Build expiry display string — match airport NOTAM style
        eff_str = _fmt_ofp_date(eff_dt) if eff_dt else "UFN"
        if is_est and exp_dt is None:
            # C) had EST — find the raw date for display only
            cm2 = _re_enrt.search(r'\nC\)\s*(\d{10})', raw_text)
            est_dt = _parse_dtg(cm2.group(1)) if cm2 else None
            exp_str = f"UFN(EST {_fmt_ofp_date(est_dt)})" if est_dt else "UFN"
        else:
            exp_str = _fmt_ofp_date(exp_dt) if exp_dt else "UFN"

        # Header: just ICAO + NOTAM ID + dates (no duplicate IATA code)
        header_line  = f"{icao_id}   {nid}   {eff_str} - {exp_str}"
        created_line = f"CREATED:{_fmt_ofp_date(cre_dt)}   FL: {fl_str}" if cre_dt else f"FL: {fl_str}"

        body_lines = []
        for para in body_text.splitlines():
            para = para.strip()
            if para:
                body_lines.append(_tw.fill(para, width=72))
        body = "\n".join(body_lines)

        rendered = f"{header_line}\n{created_line}\n\n{body}\n"

        # Decode Q-code category — use XML notam_qcode field if present, else parse raw text
        qcode_str = (rec.findtext("notam_qcode") or rec.findtext("notam_qcode_subject") or "").strip()
        qcat = _decode_qcode_enrt(raw_text, qcode_str)

        # Key on FIR code from the record's icao_id (which is the FIR code for enroute NOTAMs)
        fir_key = icao_id or facility
        if fir_key not in by_facility:
            by_facility[fir_key] = {
                'icao_id': icao_id, 'name': facility,
                'cats': {c: [] for c in _ENRT_CATEGORY_ORDER},
                'cats_exp': {c: [] for c in _ENRT_CATEGORY_ORDER},
            }
        if is_expired:
            by_facility[fir_key]['cats_exp'].setdefault(qcat, []).append((nid, rendered))
        else:
            by_facility[fir_key]['cats'].setdefault(qcat, []).append((nid, rendered))

    if not by_facility:
        return ""

    BW = 72
    result = ""

    # Render in navlog FIR order, then any remainder not on navlog
    ordered_keys = [k for k in navlog_fir_order if k in by_facility]
    remainder    = [k for k in by_facility if k not in seen_firs]
    for fir_key in ordered_keys + remainder:
        buckets  = by_facility[fir_key]
        fir_code = buckets.get('icao_id') or fir_key
        facility = buckets.get('name', fir_key)
        cats     = buckets['cats']
        cats_exp = buckets['cats_exp']

        # Skip FIR if truly empty
        has_any = any(v for v in cats.values()) or any(v for v in cats_exp.values())
        if not has_any:
            continue

        # FIR banner
        result += f"\n{'═' * BW}\n"
        result += f"{fir_code}   {facility}\n"
        result += f"{'═' * BW}\n\n"

        # Render active NOTAMs by category
        for cat in _ENRT_CATEGORY_ORDER:
            entries = sorted(cats.get(cat, []), key=lambda x: x[0], reverse=True)
            if not entries:
                continue
            inner = f" {cat} "
            pad   = BW - len(inner)
            result += f"{'═' * (pad//2)}{inner}{'═' * (pad - pad//2)}\n"
            for nid, rendered in entries:
                result += rendered + "\n"

        # Expired section
        exp_entries = []
        for cat in _ENRT_CATEGORY_ORDER:
            for item in sorted(cats_exp.get(cat, []), key=lambda x: x[0], reverse=True):
                exp_entries.append(item)
        if exp_entries:
            result += f"{'--- EXPIRED ---':^{BW}}\n"
            for nid, rendered in exp_entries:
                result += rendered + "\n"

    return result


def is_numeric(val):
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False

def safe_weight(weight):
    """
    Convert weight to thousands with proper handling.
    Returns float value divided by 1000, or 0.0 if conversion fails.
    """
    try:
        return float(weight) / 1000.0
    except (ValueError, TypeError):
        return 0.0
from SPEEDOTHER import get_speed_other
from ENGINEFAILPROC import get_airport_specific_altitudes
import re
from SPEEDOTHER import get_speed_other, get_reduced_thrust_n1
from ENGINEFAILPROC import get_airport_specific_altitudes

def safe_weight(value):
    """Convert weight value (lbs) to thousands with 1 decimal. Returns None on failure or
    zero input so callers can distinguish 'no data' from a genuine 0-lb weight."""
    try:
        result = float(value) / 1000.0
        return result if result != 0.0 else None
    except (ValueError, TypeError):
        return None

