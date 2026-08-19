"""
Fetch a pilot's current SimBrief OFP and extract the day-of-ops fields a PBS
bid-pack pairing can never carry: which calendar date this pairing actually
operates on, tail number, named crew, and passenger load. A bid pack is a
schedule pattern, not a specific day's flight — those fields only exist once
someone has actually dispatched the leg in SimBrief.

Deliberately independent of release_engine/MASTERLOG: this only needs a
handful of XML fields, not the whole TPS/performance pipeline, and
MASTERLOG.fetch_simbrief_data() calls sys.exit(1) on failure, which would
be fatal to import for a merge step that should fail softly instead.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

OFP_URL = "https://www.simbrief.com/api/xml.fetcher.php"


def fetch_ofp_generated_at(simbrief_user, timeout=15):
    """
    The pilot's current OFP's <params><time_generated> — a Unix timestamp
    SimBrief stamps on every OFP it builds, whether the pilot dispatches via
    the old signed-popup API or their own dispatch.simbrief.com page. Used
    to tell "a new plan just got generated" apart from "there's an old one
    already sitting on the account" when we have no signed request of our
    own to correlate against. Returns "" (not raises) on any failure — this
    is a best-effort freshness check, not a required step.
    """
    try:
        resp = requests.get(OFP_URL, params={"username": simbrief_user}, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        el = root.find("params/time_generated")
        return el.text.strip() if el is not None and el.text else ""
    except (requests.RequestException, ET.ParseError):
        return ""


def fetch_ofp_leg_fields(simbrief_user, timeout=15):
    """
    Returns a dict of FOS-schema leg fields populated from the pilot's
    current SimBrief OFP. Only includes keys SimBrief actually has data for,
    so merging this into a pairing-derived leg never blanks out a field the
    pairing already supplied. Raises requests.RequestException / ValueError
    on fetch/parse failure — callers should treat that as "enrichment
    unavailable," not a hard failure of leg generation.
    """
    resp = requests.get(OFP_URL, params={"username": simbrief_user}, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    def text(path):
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else ""

    def epoch_to(fmt, path):
        raw = text(path)
        if not raw:
            return ""
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).strftime(fmt)
        except (ValueError, OSError, OverflowError):
            return ""

    def seconds_to_hhmm(path):
        raw = text(path)
        if not raw:
            return ""
        try:
            total = int(raw) % 86400
            return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
        except ValueError:
            return ""

    # SimBrief's <crew> block: cpt/fo are the pilots, dx is the dispatcher,
    # pu is the purser/lead flight attendant — cabin crew beyond the purser
    # aren't tracked by SimBrief at all, so this is the full roster it has.
    crew = [
        f"{role} {name}"
        for role, name in (
            ("CA", text("crew/cpt")),
            ("FO", text("crew/fo")),
            ("DX", text("crew/dx")),
            ("PU", text("crew/pu")),
        )
        if name
    ]

    def fuel_lbs(path):
        raw = text(path)
        if not raw:
            return ""
        try:
            return f"{int(float(raw)):,}"
        except ValueError:
            return ""

    fields = {
        "flight_number": text("general/flight_number"),
        "origin": text("origin/icao_code"),
        "destination": text("destination/icao_code"),
        "dep_date": epoch_to("%m/%d/%y", "times/sched_out"),
        "arr_date": epoch_to("%m/%d/%y", "times/sched_in"),
        "sched_out": epoch_to("%H:%M", "times/sched_out"),
        "sched_in": epoch_to("%H:%M", "times/sched_in"),
        "est_out": epoch_to("%H:%M", "times/est_out"),
        "est_in": epoch_to("%H:%M", "times/est_in"),
        "tail_number": text("aircraft/reg"),
        "fleet_type": text("aircraft/icaocode"),
        "customer_load": text("general/passengers"),
        "flight_time": seconds_to_hhmm("times/sched_block"),
        "crew": crew,
        # Fuel figures — paths match what MASTERLOG.py already reads
        # successfully from real OFPs (fuel/plan_ramp etc.), not guessed.
        "block_fuel": fuel_lbs("fuel/plan_ramp"),
        "takeoff_fuel": fuel_lbs("fuel/plan_takeoff"),
        "landing_fuel": fuel_lbs("fuel/plan_landing"),
        "trip_fuel": fuel_lbs("fuel/enroute_burn"),
        "taxi_fuel": fuel_lbs("fuel/taxi"),
        "reserve_fuel": fuel_lbs("fuel/reserve"),
        "alternate_fuel": fuel_lbs("fuel/alternate_burn"),
        "contingency_fuel": fuel_lbs("fuel/contingency"),
        "extra_fuel": fuel_lbs("fuel/extra"),
    }
    return {k: v for k, v in fields.items() if v}


def fetch_weather_briefing(simbrief_user, timeout=15):
    """
    Origin/destination/alternate METAR+TAF for the pilot's current SimBrief
    OFP. SimBrief already fetches and bundles these into the OFP XML as part
    of building it, so this needs no separate aviationweather.gov call (and
    sidesteps whatever network restriction blocked that route before).
    Returns a list of {icao, role, metar, taf} dicts, empty ones omitted.
    """
    resp = requests.get(OFP_URL, params={"username": simbrief_user}, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    def text(path):
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else ""

    stations = [
        ("origin", "origin", "orig"),
        ("destination", "destination", "dest"),
        ("alternate", "alternate", "altn"),
    ]
    briefing = []
    for role, elem, wx_prefix in stations:
        icao = text(f"{elem}/icao_code")
        metar = text(f"weather/{wx_prefix}_metar")
        taf = text(f"weather/{wx_prefix}_taf")
        if icao and (metar or taf):
            briefing.append({
                "role": role, "icao": icao, "name": text(f"{elem}/name"),
                "metar": metar, "taf": taf,
                "category": text(f"{elem}/metar_category").upper(),
                "visibility": text(f"{elem}/metar_visibility"),
                "ceiling": text(f"{elem}/metar_ceiling"),
            })
    return briefing
