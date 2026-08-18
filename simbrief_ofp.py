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
        ("origin", "origin/icao_code", "orig"),
        ("destination", "destination/icao_code", "dest"),
        ("alternate", "alternate/icao_code", "altn"),
    ]
    briefing = []
    for role, icao_path, wx_prefix in stations:
        icao = text(icao_path)
        metar = text(f"weather/{wx_prefix}_metar")
        taf = text(f"weather/{wx_prefix}_taf")
        if icao and (metar or taf):
            briefing.append({"role": role, "icao": icao, "metar": metar, "taf": taf})
    return briefing
