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

from fos_pages import airline_iata

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

    def epoch_to_local_hhmm(path, tz_path):
        """sched_out/est_out/sched_in/est_in are zulu epoch timestamps —
        epoch_to() above formats them straight in UTC. Treating that as
        already-local (this function's predecessor) showed the wrong
        wall-clock time next to a PBS leg's genuinely-local scheduled time.
        Shifts by the OFP's own orig_timezone/dest_timezone hour offset,
        same approach MASTERLOG.py's convert_to_local_time() already uses
        for the printed release — not a new guess."""
        zulu = epoch_to("%H:%M", path)
        if not zulu:
            return ""
        offset_raw = text(tz_path)
        if not offset_raw:
            return zulu
        try:
            offset = int(float(offset_raw))
        except ValueError:
            return zulu
        h, m = zulu.split(":")
        local_h = (int(h) + offset) % 24
        return f"{local_h:02d}:{m}"

    # SimBrief's <crew> block: cpt/fo are the pilots, pu is the purser (lead
    # flight attendant — aboard the aircraft, unlike dx the dispatcher who
    # stays on the ground and is excluded here), and fa repeats once per
    # additional flight attendant. Real names for all of these come from
    # SimBrief — confirmed against a real OFP XML sample (2026-08-20) that
    # does carry a full <fa> roster, correcting an earlier assumption that
    # SimBrief had no cabin crew data at all. Only DOM/EMP# (synthesized in
    # fos_pages.synthesize_crew) aren't real.
    crew = [
        f"{role} {name}"
        for role, name in (
            ("CA", text("crew/cpt")),
            ("FO", text("crew/fo")),
            ("PU", text("crew/pu")),
        )
        if name
    ] + [
        f"FA {el.text.strip()}"
        for el in root.findall("crew/fa")
        if el is not None and el.text and el.text.strip()
    ]

    def fuel_lbs(path):
        raw = text(path)
        if not raw:
            return ""
        try:
            return f"{int(float(raw)):,}"
        except ValueError:
            return ""

    def tz_diff():
        """Destination minus origin UTC offset (hours), signed — same two
        OFP fields fos_pages.py already reads for the printed release, just
        never surfaced on the FOS leg schema until now."""
        orig_raw, dest_raw = text("times/orig_timezone"), text("times/dest_timezone")
        if not orig_raw or not dest_raw:
            return ""
        try:
            return f"{int(float(dest_raw)) - int(float(orig_raw)):+d}"
        except ValueError:
            return ""

    # general/icao_airline and general/iata_airline are the same two real
    # fields fos_pages.build_context() already reads for the printed
    # release's routing line, confirmed against the real OFP XML sample —
    # SimBrief often omits iata_airline, so airline_iata() falls back to
    # the AIRLINE_IATA table (fos_pages.py) keyed on the ICAO code.
    airline_icao = text("general/icao_airline")
    airline_iata_raw = text("general/iata_airline")
    fields = {
        "flight_number": text("general/flight_number"),
        "airline_icao": airline_icao,
        # airline_iata() falls back to a truncated ICAO code ("XX" if even
        # that's blank) when it has nothing better — only worth calling
        # when there's a real ICAO or IATA code to work from, otherwise
        # this field should stay blank so the caller's own fallback chain
        # (PBS pairing operator, then no prefix at all) can take over.
        "airline_iata": airline_iata(airline_icao, airline_iata_raw) if (airline_icao or airline_iata_raw) else "",
        "origin": text("origin/icao_code"),
        "destination": text("destination/icao_code"),
        # Enroute waypoints only (no orig/dest) — same general/route path
        # MASTERLOG.py already reads successfully for the printed release's
        # route line, not guessed. Used for the ForeFlight export URL.
        "route": text("general/route"),
        "tz_diff": tz_diff(),
        "dep_date": epoch_to("%m/%d/%y", "times/sched_out"),
        "arr_date": epoch_to("%m/%d/%y", "times/sched_in"),
        "sched_out": epoch_to_local_hhmm("times/sched_out", "times/orig_timezone"),
        "sched_in": epoch_to_local_hhmm("times/sched_in", "times/dest_timezone"),
        "est_out": epoch_to_local_hhmm("times/est_out", "times/orig_timezone"),
        "est_in": epoch_to_local_hhmm("times/est_in", "times/dest_timezone"),
        "tail_number": text("aircraft/reg"),
        "fleet_type": text("aircraft/icaocode"),
        # Aircraft detail popup fields — same paths fos_pages.py/MASTERLOG.py
        # already read successfully elsewhere, not guessed. aircraft/engines
        # and aircraft/max_passengers confirmed against a real OFP XML
        # sample (2026-08-20) — an earlier pass wrongly assumed SimBrief had
        # no engine field at all.
        "aircraft_name": text("aircraft/name"),
        "fin": text("aircraft/fin"),
        "engines": text("aircraft/engines"),
        "selcal": text("aircraft/selcal"),
        "seat_capacity": text("aircraft/max_passengers"),
        # Structural (airframe-certified) max weights, not this flight's own
        # operational/performance-limited figures (weights/max_tow, est_zfw,
        # etc.) — confirmed against the same real OFP XML sample's <weights>
        # block. fuel_lbs() is just generic int-with-commas formatting,
        # reused here for the same reason.
        "oew": fuel_lbs("weights/oew"),
        "max_zfw": fuel_lbs("weights/max_zfw"),
        "max_tow_struct": fuel_lbs("weights/max_tow_struct"),
        "max_ldw": fuel_lbs("weights/max_ldw"),
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


def fetch_prefile_links(simbrief_user, timeout=15):
    """
    VATSIM/IVAO prefile forms straight from the pilot's current SimBrief
    OFP — confirmed against a real OFP XML sample (2026-08-20) that SimBrief
    itself renders these as ready-to-submit HTML <form> snippets embedded in
    the XML (<vatsim_prefile>/<ivao_prefile>), each with the full ICAO
    flight-plan string (VATSIM's "raw" field) or base64 flight-plan blob
    (IVAO's "flightPlan" field) already built. That means this app never
    has to construct an ICAO FPL string itself — just forward what SimBrief
    already assembled. Returns {} for a service whose block isn't present
    (e.g. no OFP generated yet) rather than raising, so callers can grey out
    that specific external-app row instead of failing the whole popup.
    """
    resp = requests.get(OFP_URL, params={"username": simbrief_user}, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    def form_field(block_path, field_name):
        form = root.find(f"{block_path}/form")
        if form is None:
            return None
        action = form.get("action")
        if not action:
            return None
        for inp in form.findall("input"):
            if inp.get("name") == field_name:
                return action, inp.get("value") or ""
        return None

    links = {}

    vatsim = form_field("vatsim_prefile", "raw")
    if vatsim:
        action, raw = vatsim
        fuel_time = form_field("vatsim_prefile", "fuel_time")
        links["vatsim"] = {"action": action, "raw": raw, "fuel_time": fuel_time[1] if fuel_time else ""}

    ivao = form_field("ivao_prefile", "flightPlan")
    if ivao:
        action, flight_plan = ivao
        links["ivao"] = {"action": action, "flight_plan": flight_plan}

    return links


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
