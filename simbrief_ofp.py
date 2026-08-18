"""
Fetch a pilot's current SimBrief OFP and extract the day-of-ops fields a PBS
bid-pack pairing can never carry: which calendar date this pairing actually
operates on, tail number, named crew, and passenger load. A bid pack is a
schedule pattern, not a specific day's flight — those fields only exist once
someone has actually dispatched the leg in SimBrief.

Deliberately independent of release_engine/MASTERLOG_FOS: this only needs a
handful of XML fields, not the whole TPS/performance pipeline, and
MASTERLOG_FOS.fetch_simbrief_data() calls sys.exit(1) on failure, which would
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

    crew = [n for n in (text("crew/cpt"), text("crew/fo")) if n]

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
        "customer_load": text("general/passengers"),
        "crew": crew,
    }
    return {k: v for k, v in fields.items() if v}
