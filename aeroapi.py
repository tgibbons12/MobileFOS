"""
Thin client for FlightAware's AeroAPI (v4) — suggests a filed route and a
likely gate for a flight number, derived from that flight ident's recent
history. Each pilot brings their own AeroAPI key (a paid FlightAware
subscription); this module is a stateless pass-through for it — the key is
never written to disk here, only sent on to FlightAware per request.

Schema (route/gate_origin/gate_destination/terminal_origin/
terminal_destination/actual_in on the flight object) verified 2026-08-19
against a live key hitting GET /flights/AAL100 — field names below match
the real response, not just AeroAPI's docs.
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


def _first_present(flights, key):
    """First truthy `key` walking `flights` in order, or ""."""
    return next((f.get(key) for f in flights if f.get(key)), "")


def _suggest_field(flown, flights, key, use_mode):
    """Best-effort value for `key`, in order:
    1) the modal value across the recent-flown sample (only for gates —
       route has no "most common" concept worth computing);
    2) failing that, the single most recently *flown* flight that has it
       (walks the full flown history, not just the capped sample);
    3) failing that — this ident has never actually flown — whatever's on
       the current/scheduled record, e.g. a pre-assigned gate.
    Always resolves to the last-flown value when one exists; only reaches
    for scheduled/filed data when there's no flown history to draw from.
    """
    if use_mode:
        modal = _mode(f.get(key) for f in flown[:10])
        if modal:
            return modal
    val = _first_present(flown, key)
    if val:
        return val
    return _first_present(flights, key)


def get_suggestions(api_key, ident):
    """Route + gate suggestion for `ident` (e.g. "AAL1234"). Raises
    AeroApiError on any failure — callers should treat that as "suggestion
    unavailable," not fatal."""
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

    if not flights:
        raise AeroApiError(f"no flights on file for {ident}")

    # AeroAPI returns this ident's flights newest-first (upcoming/current
    # ahead of history), so filtering to actual_in and taking them in order
    # already walks from most-recently-landed backwards through time.
    flown = [f for f in flights if f.get("actual_in")]

    result = {
        "ident": ident,
        "route": _suggest_field(flown, flights, "route", use_mode=False),
        "gate_origin": _suggest_field(flown, flights, "gate_origin", use_mode=True),
        "gate_destination": _suggest_field(flown, flights, "gate_destination", use_mode=True),
        "terminal_origin": _suggest_field(flown, flights, "terminal_origin", use_mode=True),
        "terminal_destination": _suggest_field(flown, flights, "terminal_destination", use_mode=True),
        "sample_size": len(flown[:10]),
        "never_flown": len(flown) == 0,
    }
    _cache[ident] = (time.time(), result)
    return result
