"""
Thin client for FlightAware's AeroAPI (v4) — suggests a filed route and a
likely gate for a flight number, derived from that flight ident's recent
history. Each pilot brings their own AeroAPI key (a paid FlightAware
subscription); this module is a stateless pass-through for it — the key is
never written to disk here, only sent on to FlightAware per request.

Schema below (route/gate_origin/gate_destination/terminal_origin/
terminal_destination on the flight object) matches AeroAPI v4's documented
/flights/{ident} response: https://www.flightaware.com/aeroapi/portal/documentation
Not independently verified against a live key from this repo — if
FlightAware's real response shapes these differently, treat that as a
doc/reality drift to fix here, not a bug in the caching/suggestion logic
around it.
"""
import time
from collections import Counter

import requests

AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"

# Gate/route assignments don't change by the second, and AeroAPI bills per
# call — cache by ident (not by caller's key) since it's the same public
# flight data no matter who's asking.
_CACHE_TTL = 30 * 60
_cache = {}


class AeroApiError(Exception):
    pass


def _mode(values):
    """Most common truthy value in an iterable, or "" if none are truthy."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def get_suggestions(api_key, ident):
    """Route + gate suggestion for `ident` (e.g. "AAL1234"), derived from up
    to its last 10 completed flights. Raises AeroApiError on any failure —
    callers should treat that as "suggestion unavailable," not fatal."""
    cached = _cache.get(ident)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(
            f"{AEROAPI_BASE}/flights/{ident}",
            headers={"x-apikey": api_key},
            timeout=15,
        )
    except requests.RequestException as e:
        raise AeroApiError(f"couldn't reach AeroAPI: {e}")

    if resp.status_code == 401:
        raise AeroApiError("AeroAPI rejected that key (401 Unauthorized)")
    if resp.status_code == 404:
        raise AeroApiError(f"no flights on file for {ident}")
    if not resp.ok:
        raise AeroApiError(f"AeroAPI error {resp.status_code}: {resp.text[:200]}")

    try:
        flights = resp.json().get("flights", [])
    except ValueError:
        raise AeroApiError("AeroAPI returned unparseable data")

    # Prefer flights that actually landed (a real gate was used, not just
    # filed) but fall back to whatever's on file if none have completed yet.
    completed = [f for f in flights if f.get("actual_in")] or flights
    completed = completed[:10]

    if not completed:
        raise AeroApiError(f"no flights on file for {ident}")

    result = {
        "ident": ident,
        "route": next((f.get("route") for f in completed if f.get("route")), ""),
        "gate_origin": _mode(f.get("gate_origin") for f in completed),
        "gate_destination": _mode(f.get("gate_destination") for f in completed),
        "terminal_origin": _mode(f.get("terminal_origin") for f in completed),
        "terminal_destination": _mode(f.get("terminal_destination") for f in completed),
        "sample_size": len(completed),
    }
    _cache[ident] = (time.time(), result)
    return result
