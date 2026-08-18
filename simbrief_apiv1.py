"""
Independent Python/JS implementation of SimBrief's APIv1 protocol (VA
Dispatch integration) — not a port of their simbrief.apiv1.php/js files.
Those files ship under "you may not distribute this package, with or
without modifications, without our prior consent"; their own README
explicitly anticipates developers on non-PHP stacks writing their own
client against the same protocol, which is what this is.

Protocol, as reverse-engineered from the (still-live, verified against
production) endpoints:
  - A signed request is authorized by api_code = md5(api_key + api_req),
    where api_req is server-defined per call (here: orig+dest+type+timestamp
    +outputpage, concatenated — matches the reference implementation).
  - The actual generation happens in a SimBrief-hosted popup
    (https://www.simbrief.com/ofp/ofp.loader.api.php) that the *pilot* logs
    into and completes themselves — this is inherently interactive, SimBrief
    account-holder actions, not something a server can do headlessly. Our
    job is just to build the signed, pre-filled request and later confirm
    the result landed.
  - The resulting OFP's id is deterministic: f"{timestamp}_{md5(orig+dest+type)
    [:10].upper()}" — not returned by the server, computed client-side by
    both sides from the same inputs.
  - Once generated, the OFP XML appears at a fixed URL keyed by that id;
    its existence is the completion signal.
"""

import hashlib
import os

import requests

WORKER_URL = "https://www.simbrief.com/ofp/ofp.loader.api.php"
XML_URL = "https://www.simbrief.com/ofp/flightplans/xml/{ofp_id}.xml"


def _api_key():
    return os.environ.get("SIMBRIEF_API_KEY", "")


def is_configured():
    return bool(_api_key())


def sign(api_req):
    """api_code for a request — full lowercase-hex md5, unlike the truncated
    ofp_id hash below (the reference PHP does not truncate this one)."""
    return hashlib.md5((_api_key() + api_req).encode()).hexdigest()


def compute_ofp_id(orig, dest, actype, timestamp):
    digest = hashlib.md5((orig + dest + actype).encode()).hexdigest()[:10].upper()
    return f"{timestamp}_{digest}"


def check_ofp_ready(ofp_id, timeout=10):
    """True once SimBrief has published the generated OFP's XML."""
    try:
        resp = requests.head(XML_URL.format(ofp_id=ofp_id), timeout=timeout, allow_redirects=True)
        return resp.status_code == 200
    except requests.RequestException:
        return False
