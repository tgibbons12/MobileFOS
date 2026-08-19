"""
Thin client for FlightAware's AeroAPI (v4) — suggests a likely gate at the
origin and destination airports, and a filed route for the exact city pair,
all scoped to American Airlines regardless of what a caller's VA livery
code might say. Each pilot brings their own AeroAPI key (a paid FlightAware
subscription); this module is a stateless pass-through for it — the key is
never written to disk here, only sent on to FlightAware per request.

Gates come from AA's *airport*-level recent departures/arrivals — AA's
typical gate at an airport doesn't depend on which city pair is being
flown, so this still finds one even for a city pair AA has never actually
connected nonstop. Route comes from a separate city-pair lookup and stays
blank in that case, which is the correct answer, not a failure.

Endpoints and field names (gate_origin, gate_destination, terminal_origin,
terminal_destination, route, operator_icao, actual_in/actual_out, the
`airline` filter param on the airport-flights endpoints) verified
2026-08-19 against FlightAware's public OpenAPI spec:
https://www.flightaware.com/commercial/aeroapi/resources/aeroapi-openapi.yml
"""
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
AA_ICAO = "AAL"

# Gate/route assignments don't change by the second, and AeroAPI bills per
# call — cache by city pair (not by caller's key) since it's the same
# public flight data no matter who's asking.
_CACHE_TTL = 30 * 60
_cache = {}

# AeroAPI's airport-flights endpoints cap "start" at 10 days in the past —
# stay a day inside that so request latency between us computing "now" and
# AeroAPI evaluating it server-side can't tip the value over the limit.
_LOOKBACK_DAYS = 9


class AeroApiError(Exception):
    pass


def _mode(values):
    """Most common truthy value in an iterable, or "" if none are truthy."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _first_present(flights, key):
    """First truthy `key` walking `flights` in order, or ""."""
    return next((f.get(key) for f in flights if f.get(key)), "")


def _get(path, api_key, params=None):
    try:
        resp = requests.get(f"{AEROAPI_BASE}{path}", headers={"x-apikey": api_key}, params=params, timeout=15)
    except requests.RequestException as e:
        raise AeroApiError(f"couldn't reach AeroAPI: {e}")
    if resp.status_code == 401:
        raise AeroApiError("AeroAPI rejected that key (401 Unauthorized)")
    if resp.status_code == 404:
        return {}
    if not resp.ok:
        raise AeroApiError(f"AeroAPI error {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        raise AeroApiError("AeroAPI returned unparseable data")


def _aa_airport_flights(api_key, airport, direction):
    """AA's flights that recently departed (direction='departures') or
    arrived ('arrivals') at a single airport — AeroAPI itself only returns
    completed ones for these two endpoints, newest first."""
    start = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _get(f"/airports/{airport}/flights/{direction}", api_key, {"airline": AA_ICAO, "start": start})
    return data.get("flights", [])


def _aa_route_between(api_key, orig, dest):
    """AA's nonstop flights filed between these two exact airports, if any
    — empty when AA doesn't connect them, which is a legitimate answer."""
    start = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _get(f"/airports/{orig}/flights/to/{dest}", api_key, {"connection": "nonstop", "start": start})
    segments = [entry["segments"][0] for entry in data.get("flights", []) if entry.get("segments")]
    return [s for s in segments if (s.get("operator_icao") or "").upper() == AA_ICAO]


def get_suggestions(api_key, orig, dest):
    """Gate + route suggestion for the orig/dest city pair (4-letter ICAO
    airport codes). Raises AeroApiError on any failure — callers should
    treat that as "suggestion unavailable," not fatal."""
    cache_key = f"{orig}-{dest}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    # Three independent calls — one hitting a transient/edge-case error
    # (rate limit, a validation quirk on one specific airport) shouldn't
    # sink suggestions the other two could still answer.
    errors = []

    def safe(fn, *args):
        try:
            return fn(*args)
        except AeroApiError as e:
            errors.append(str(e))
            return []

    departures = safe(_aa_airport_flights, api_key, orig, "departures")
    arrivals = safe(_aa_airport_flights, api_key, dest, "arrivals")
    route_flights = safe(_aa_route_between, api_key, orig, dest)

    if not departures and not arrivals and not route_flights:
        raise AeroApiError("; ".join(dict.fromkeys(errors)) or f"no AA flight data on file for {orig}/{dest}")

    # route_flights may mix completed and still-scheduled entries (the
    # to/{dest_id} endpoint doesn't guarantee only-completed the way the
    # airport departures/arrivals endpoints do) — prefer a completed one's
    # filed route over a merely-scheduled one's.
    completed_routes = [f for f in route_flights if f.get("actual_in") or f.get("actual_out")]

    result = {
        "route": _first_present(completed_routes, "route") or _first_present(route_flights, "route"),
        "route_found": bool(route_flights),
        "gate_origin": _mode(f.get("gate_origin") for f in departures[:10]) or _first_present(departures, "gate_origin"),
        "gate_destination": _mode(f.get("gate_destination") for f in arrivals[:10]) or _first_present(arrivals, "gate_destination"),
        "terminal_origin": _mode(f.get("terminal_origin") for f in departures[:10]) or _first_present(departures, "terminal_origin"),
        "terminal_destination": _mode(f.get("terminal_destination") for f in arrivals[:10]) or _first_present(arrivals, "terminal_destination"),
        "sample_size_origin": len(departures[:10]),
        "sample_size_destination": len(arrivals[:10]),
    }
    _cache[cache_key] = (time.time(), result)
    return result
