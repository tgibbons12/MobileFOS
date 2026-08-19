"""
FOS backend — same shape as Avio's server.py:
  - in-memory _store (current + archive + next_id), no DB, resets on restart
  - POST /generate builds a record and stashes it as current + archive
  - GET /fos/<id> renders it
  - dedupe on repeat generate, same as Avio's OFP archive logic

Difference from Avio: instead of pulling a SimBrief OFP, /generate takes an
already-merged "leg" — pairing-level fields (SEQ, crew, hotel/limo) from your
crew pairing builder + leg-level fields (times, tail, gate) from a SimBrief OFP.
Where that merge happens is up to you — paste it, curl it, or wire
Aviobook.fetch_xml_from_api() + your pairing builder's export in ahead of the
call to /generate. See build_leg_from_sources() below for the seam.
"""

import base64
import csv
import io
import logging
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, Response, redirect
from string import Template
import aeroapi
import pbs_parser
import release_engine
from fos_pages import AIRLINE_IATA
import simbrief_ofp

# PBS's "OPERATOR / FLEET" line carries the two-letter IATA code; SimBrief's
# generate-flight-plan form wants the three-letter ICAO code instead. Reuse
# fos_pages' ICAO->IATA map (the release pages' source of truth) rather than
# keeping a second, driftable copy here.
_IATA_TO_ICAO = {iata: icao for icao, iata in AIRLINE_IATA.items()}

# PBS station codes are 3-letter IATA (e.g. LAX); SimBrief's orig/dest params
# need the 4-letter ICAO identifier instead. Lazily built from OurAirports'
# public airports.csv (CC0) and disk-cached for a week; falls back to {} on
# any network hiccup so a stale/missing cache just means no conversion happens.
_AIRPORTS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ourairports_airports.csv")
_AIRPORTS_CACHE_MAX_AGE = 7 * 24 * 3600
_iata_to_icao_airport = None


def _load_iata_to_icao_airports():
    global _iata_to_icao_airport
    if _iata_to_icao_airport is not None:
        return _iata_to_icao_airport

    raw = None
    if os.path.exists(_AIRPORTS_CACHE_PATH):
        try:
            if time.time() - os.path.getmtime(_AIRPORTS_CACHE_PATH) < _AIRPORTS_CACHE_MAX_AGE:
                with open(_AIRPORTS_CACHE_PATH, "r", encoding="utf-8") as f:
                    raw = f.read()
        except OSError:
            raw = None

    if raw is None:
        try:
            resp = requests.get("https://davidmegginson.github.io/ourairports-data/airports.csv", timeout=12)
            resp.raise_for_status()
            raw = resp.text
            try:
                with open(_AIRPORTS_CACHE_PATH, "w", encoding="utf-8") as f:
                    f.write(raw)
            except OSError:
                pass
        except requests.RequestException as e:
            LOG.debug(f"[OurAirports] airports.csv download failed: {e}")
            _iata_to_icao_airport = {}
            return _iata_to_icao_airport

    db = {}
    try:
        for row in csv.DictReader(io.StringIO(raw)):
            iata = (row.get("iata_code") or "").strip().upper()
            ident = (row.get("ident") or "").strip().upper()
            if iata and ident and iata not in db:
                db[iata] = ident
    except csv.Error as e:
        LOG.warning(f"[OurAirports] airports.csv parse error: {e}")
        db = {}
    _iata_to_icao_airport = db
    return _iata_to_icao_airport


def _airport_icao(code):
    """Best-effort IATA->ICAO for a station code; passes through anything
    already 4 letters or not found in the map (some PBS stations are
    already ICAO, e.g. Canadian CYxx fields)."""
    code = (code or "").strip().upper()
    if not code or len(code) == 4:
        return code
    return _load_iata_to_icao_airports().get(code, code)


# PBS's A320-family equipment codes are keyed by sub-fleet prefix, not the
# aircraft's own type number — confirmed convention, not a guess: 31x is
# the A319 sub-fleet, 21x is the A321 sub-fleet. Extend as more prefixes
# get confirmed; anything unmapped (including already-ICAO codes like
# "B738" from a SimBrief-loaded leg) passes through unchanged.
_FLEET_TYPE_ICAO_PREFIX = {
    "31": "A319",
    "21": "A321",
}


def _fleet_type_icao(code):
    code = (code or "").strip().upper()
    return _FLEET_TYPE_ICAO_PREFIX.get(code[:2], code)

app = Flask(__name__)
LOG = logging.getLogger(__name__)

_store = {"current": None, "archive": [], "next_id": 1}
_pbs_store = {"meta": None, "sequences": []}

DEFAULT_LEG = {
    "seq": "", "date": "", "flight_number": "", "origin": "", "destination": "",
    "dep_date": "", "arr_date": "", "sched_out": "", "sched_in": "",
    "est_out": "", "est_in": "", "dep_gate": "", "arr_gate": "",
    "fleet_type": "", "equipment_type": "", "tail_number": "", "tail_routing": "",
    "status": "", "customer_load": "", "position": "", "crew": [],
    "flight_time": "", "odl_time": "", "duty_time": "", "ground_time": "",
    "tz_diff": "", "hotel_details": "", "limo_details": "",
    "signed_in": False, "fit_for_duty": False,
    "signature": "", "signed_at": "",
    "block_fuel": "", "takeoff_fuel": "", "landing_fuel": "", "trip_fuel": "",
    "taxi_fuel": "", "reserve_fuel": "", "alternate_fuel": "", "contingency_fuel": "", "extra_fuel": "",
}

_signature_log = []


# ---------------------------------------------------------------------------
# Integration seam. This is where your existing tools plug in. Left as a
# passthrough stub so this file runs standalone — replace the body once
# you're ready to wire it up, nothing else in this file needs to change.
# ---------------------------------------------------------------------------
def build_leg_from_sources(payload):
    """
    payload: whatever /generate (or the PBS-generate route) received, plus
    an optional "simbrief_user" key.

    A PBS bid-pack pairing is a schedule pattern, not one specific day's
    flight — it can never carry tail number, named crew, real calendar date,
    or passenger load (those only exist once someone has dispatched the leg).
    When simbrief_user is given, we fetch that pilot's current SimBrief OFP
    and use it to fill in exactly those gaps. Any field the pairing already
    supplied wins — this only populates what's still blank, it never
    overwrites real pairing data (SEQ, hotel/limo, duty time, route/times
    from the bid pack) with something less authoritative.
    """
    simbrief_user = payload.get("simbrief_user")
    if not simbrief_user:
        return payload

    payload = dict(payload)
    payload.pop("simbrief_user", None)
    try:
        ofp_fields = simbrief_ofp.fetch_ofp_leg_fields(simbrief_user)
    except Exception as e:
        LOG.warning(f"SimBrief OFP enrichment failed for {simbrief_user}: {e}")
        payload["_ofp_error"] = str(e)
        return payload

    known = {k: v for k, v in payload.items() if v not in (None, "", [])}
    return {**ofp_fields, **known}


def _find(flight_number, dep_date):
    for rec in _store["archive"]:
        if rec.get("flight_number") == flight_number and rec.get("dep_date") == dep_date:
            return rec
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _store_leg(leg):
    """Shared by /generate and the PBS import path: dedupe-or-insert + archive."""
    leg = {**DEFAULT_LEG, **leg}
    existing = _find(leg.get("flight_number"), leg.get("dep_date"))
    if existing:
        signed_in, ffd, rec_id = existing["signed_in"], existing["fit_for_duty"], existing["id"]
        existing.update(leg)
        existing["signed_in"], existing["fit_for_duty"], existing["id"] = signed_in, ffd, rec_id
        record = existing
    else:
        record = dict(leg)
        record["id"] = _store["next_id"]
        _store["next_id"] += 1
        _store["archive"].insert(0, record)
    _store["current"] = record
    return record


@app.route("/generate", methods=["POST"])
def generate():
    payload = request.get_json(silent=True) or {}
    leg = build_leg_from_sources(payload)
    ofp_error = leg.pop("_ofp_error", None)
    if ofp_error and not leg.get("flight_number"):
        return jsonify({"error": f"couldn't load SimBrief OFP: {ofp_error}"}), 502
    record = _store_leg(leg)
    return jsonify({"fos_url": f"/fos/{record['id']}", "id": record["id"]})


# ---------------------------------------------------------------------------
# PBS pairing import — parses your crew pairing builder's bid-pack export
# ---------------------------------------------------------------------------
@app.route("/import-pbs", methods=["POST"])
def import_pbs():
    text = request.get_data(as_text=True)
    if not text or not text.strip():
        return jsonify({"error": "empty body — POST the raw PBS bid-pack text"}), 400
    meta = pbs_parser.parse_pbs_meta(text)
    sequences = pbs_parser.parse_pbs(text)
    _pbs_store["meta"] = meta
    _pbs_store["sequences"] = sequences
    return jsonify({
        "meta": meta,
        "sequences_parsed": len(sequences),
        "legs_parsed": sum(len(d["legs"]) for s in sequences for d in s["duty_days"]),
    })


def _sequence_routing(seq):
    """Full station chain for a sequence — first leg's origin, then every
    leg's destination in order. Legs are contiguous (each leg's origin is
    the previous leg's destination, across duty-day boundaries too — a
    night stop just means a gap in time, not in the station chain), so this
    walk alone gives the whole routing without needing to dedupe anything."""
    stations = []
    for day in seq["duty_days"]:
        for leg in day["legs"]:
            if not stations:
                stations.append(leg["origin"])
            stations.append(leg["destination"])
    return stations


@app.route("/pbs/sequences")
def list_pbs_sequences():
    out = []
    for s in _pbs_store["sequences"]:
        first_day = s["duty_days"][0]
        last_day = s["duty_days"][-1]
        first_leg = first_day["legs"][0] if first_day["legs"] else None
        last_leg = last_day["legs"][-1] if last_day["legs"] else None
        out.append({
            "seq": s["seq"], "days": len(s["duty_days"]),
            "positions": s["positions"], "ops_per_period": s["ops_per_period"],
            "report": first_day["report"],
            "origin": first_leg["origin"] if first_leg else None,
            "final_destination": last_leg["destination"] if last_leg else None,
            "routing": _sequence_routing(s),
        })
    return jsonify(out)


@app.route("/pbs/sequences/<seq_number>")
def get_pbs_sequence(seq_number):
    seq = next((s for s in _pbs_store["sequences"] if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404
    out = dict(seq)
    operator_iata = _pbs_store["meta"].get("operator", "").upper()
    out["operator"] = _IATA_TO_ICAO.get(operator_iata, operator_iata)
    out["duty_days"] = [
        {**day, "legs": [
            {**leg, "origin_icao": _airport_icao(leg["origin"]), "destination_icao": _airport_icao(leg["destination"])}
            for leg in day["legs"]
        ]}
        for day in seq["duty_days"]
    ]
    return jsonify(out)


@app.route("/pbs/sequences/<seq_number>/generate", methods=["POST"])
def generate_from_pbs(seq_number):
    seq = next((s for s in _pbs_store["sequences"] if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found — POST /import-pbs first"}), 404

    body = request.get_json(silent=True) or {}
    duty_day = int(body.get("duty_day", 1))
    leg_index = int(body.get("leg_index", 0))
    position = body.get("position") or (seq["positions"][0] if seq["positions"] else "")

    day = next((d for d in seq["duty_days"] if d["duty_day"] == duty_day), None)
    if not day or leg_index >= len(day["legs"]):
        return jsonify({"error": "duty_day/leg_index out of range"}), 400
    leg = day["legs"][leg_index]

    fos_leg = pbs_parser.pbs_leg_to_fos_leg(_pbs_store["meta"], seq, day, leg, position)
    if body.get("simbrief_user"):
        fos_leg["simbrief_user"] = body["simbrief_user"]
    fos_leg = build_leg_from_sources(fos_leg)
    fos_leg.pop("_ofp_error", None)  # non-fatal here — real pairing data already exists
    record = _store_leg(fos_leg)
    return jsonify({"fos_url": f"/fos/{record['id']}", "id": record["id"]})


@app.route("/fos/<int:leg_id>")
def view_fos(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return Response(
            f"No leg with id {leg_id}. POST a leg to /generate first.",
            status=404, mimetype="text/plain",
        )
    return Response(render_fos_html(record), mimetype="text/html")


@app.route("/fos/<int:leg_id>/signin", methods=["POST"])
def toggle_signin(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404
    record["signed_in"] = not record.get("signed_in", False)
    return jsonify({"signed_in": record["signed_in"]})


@app.route("/fos/<int:leg_id>/fit-for-duty", methods=["POST"])
def toggle_ffd(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404
    record["fit_for_duty"] = not record.get("fit_for_duty", False)
    return jsonify({"fit_for_duty": record["fit_for_duty"]})


@app.route("/fos/<int:leg_id>/gates", methods=["POST"])
def set_gates(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    if "dep_gate" in body:
        record["dep_gate"] = body["dep_gate"]
    if "arr_gate" in body:
        record["arr_gate"] = body["arr_gate"]
    return jsonify({"dep_gate": record.get("dep_gate", ""), "arr_gate": record.get("arr_gate", "")})


@app.route("/aeroapi/suggest", methods=["POST"])
def aeroapi_suggest():
    """Gate/route suggestion for an origin/destination city pair, using the
    caller's own AeroAPI key — we never store it, just relay it to
    FlightAware for this one request (see aeroapi.py for the per-city-pair
    response cache)."""
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    orig = (body.get("orig") or "").strip().upper()
    dest = (body.get("dest") or "").strip().upper()
    if not api_key:
        return jsonify({"error": "FlightAware AeroAPI key required"}), 400
    if not orig or not dest:
        return jsonify({"error": "origin and destination (ICAO) required"}), 400
    try:
        result = aeroapi.get_suggestions(api_key, orig, dest)
    except aeroapi.AeroApiError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


@app.route("/fos/<int:leg_id>/sign", methods=["POST"])
def sign_leg(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(silent=True) or {}
    signature = body.get("signature")
    if not signature or not signature.startswith("data:image/"):
        return jsonify({"error": "no signature image given"}), 400

    signed_at = datetime.now(timezone.utc).isoformat()
    record["signature"] = signature
    record["signed_at"] = signed_at

    entry = {
        "leg_id": leg_id, "flight_number": record.get("flight_number"),
        "dep_date": record.get("dep_date"), "signed_at": signed_at,
    }
    _signature_log.append(entry)
    LOG.info(f"SIGNATURE leg={leg_id} flight={record.get('flight_number')} at={signed_at}")

    return jsonify({"signed_at": signed_at})


@app.route("/signatures")
def list_signatures():
    """Audit log of every signing event this process has seen — separate
    from the leg records themselves, so it survives a leg being regenerated
    (re-signing overwrites record['signature'] but not this history)."""
    return jsonify(_signature_log)


@app.route("/fos/<int:leg_id>/weather", methods=["POST"])
def leg_weather(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id") or os.environ.get("SIMBRIEF_USER")
    if not user_id:
        return jsonify({"error": "no SimBrief user id — pass \"user_id\" or set SIMBRIEF_USER"}), 400

    try:
        briefing = simbrief_ofp.fetch_weather_briefing(user_id)
    except Exception as e:
        return jsonify({"error": f"couldn't load weather: {e}"}), 502

    return jsonify(briefing)


@app.route("/simbrief-api/generated-at")
def simbrief_api_generated_at():
    """Freshness probe for the pilot's current OFP — see
    simbrief_ofp.fetch_ofp_generated_at for why this replaces the old
    signed-request/deterministic-ofp_id check."""
    user_id = request.args.get("user", "")
    if not user_id:
        return jsonify({"error": "user required"}), 400
    return jsonify({"time_generated": simbrief_ofp.fetch_ofp_generated_at(user_id)})


@app.route("/release/status")
def release_status():
    return jsonify({
        "available": release_engine.is_available(),
        "error": release_engine.import_error(),
    })


@app.route("/fos/<int:leg_id>/release", methods=["POST"])
def generate_release(leg_id):
    record = next((r for r in _store["archive"] if r["id"] == leg_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404

    if not release_engine.is_available():
        return jsonify({"error": release_engine.import_error()}), 503

    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id") or os.environ.get("SIMBRIEF_USER")
    if not user_id:
        return jsonify({"error": "no SimBrief user id — pass \"user_id\" or set SIMBRIEF_USER"}), 400

    try:
        rls_bytes, wb_bytes, filename = release_engine.generate_release_pdfs(user_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    payload = {
        "filename": filename,
        "rls_pdf_b64": base64.b64encode(rls_bytes).decode("ascii"),
    }
    if wb_bytes:
        payload["wb_pdf_b64"] = base64.b64encode(wb_bytes).decode("ascii")

    try:
        named_pages = release_engine.extract_named_pages(rls_bytes)
    except Exception as e:
        LOG.warning(f"FI/FIL page extraction failed: {e}")
        named_pages = {}
    if named_pages.get("fi"):
        payload["fi_pdf_b64"] = base64.b64encode(named_pages["fi"]).decode("ascii")
    if named_pages.get("fil"):
        payload["fil_pdf_b64"] = base64.b64encode(named_pages["fil"]).decode("ascii")

    return jsonify(payload)


@app.route("/archive")
def archive():
    slim = [
        {
            "id": r["id"], "flight_number": r.get("flight_number"),
            "origin": r.get("origin"), "destination": r.get("destination"),
            "dep_date": r.get("dep_date"), "signed_in": r.get("signed_in"),
            "fit_for_duty": r.get("fit_for_duty"),
        }
        for r in _store["archive"]
    ]
    return jsonify(slim)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "archived_legs": len(_store["archive"])})


@app.route("/")
def index():
    return Response(render_launcher_html(), mimetype="text/html")


# ---------------------------------------------------------------------------
# Rendering — string.Template so the CSS's { } never fights Python's
# ---------------------------------------------------------------------------
def render_launcher_html():
    rows = "".join(
        f'<a class="arow" href="/fos/{r["id"]}">{r.get("flight_number","")} '
        f'{r.get("origin","")}\u2192{r.get("destination","")} '
        f'<span>{r.get("dep_date","")}</span></a>'
        for r in _store["archive"]
    ) or '<p class="empty">No legs generated yet.</p>'
    return Template(LAUNCHER_TEMPLATE).safe_substitute(rows=rows)


def render_fos_html(leg):
    ctx = {**DEFAULT_LEG, **leg}
    ctx["customer_load"] = str(ctx.get("customer_load") or "")
    crew = ctx.get("crew")
    ctx["crew_display"] = ", ".join(crew) if isinstance(crew, list) else str(crew or "")
    ctx["leg_id"] = str(leg.get("id", ""))
    ctx["fleet_type_icao"] = _fleet_type_icao(ctx.get("fleet_type", ""))
    # Neither PBS nor a SimBrief OFP ever carries a flight "status" — it's
    # not data either source has. Derive one locally from what this app
    # actually tracks rather than leaving it permanently blank.
    if not ctx.get("status"):
        if ctx.get("signature"):
            ctx["status"] = "Released & Signed"
        elif ctx.get("signed_in") and ctx.get("fit_for_duty"):
            ctx["status"] = "Ready for Departure"
        elif ctx.get("signed_in") or ctx.get("fit_for_duty"):
            ctx["status"] = "Checking In"
        else:
            ctx["status"] = "Scheduled"
    ctx["signed_in_class"] = "" if ctx.get("signed_in") else "inactive"
    ctx["ffd_class"] = "" if ctx.get("fit_for_duty") else "inactive"
    ctx["signed_class"] = "signed" if ctx.get("signature") else ""
    str_ctx = {k: ("" if v is None else str(v)) for k, v in ctx.items() if k != "signature"}
    return Template(FOS_TEMPLATE).safe_substitute(**str_ctx)


LAUNCHER_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#142c52">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="mobileFOS">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<title>FOS</title>
<style>
  html,body{height:100%;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#eef1f4;margin:0;padding:24px;color:#1a1f29;}
  h1{font-size:18px;color:#144e94;margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;}
  textarea, select, input[type=text]{width:100%;max-width:640px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid #e3e6ea;border-radius:5px;box-sizing:border-box;background:#fbfbfc;}
  textarea{height:160px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;}
  button{margin-top:10px;background:#1c63b7;color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  button.secondary{background:#2fa355;}
  .arow{display:block;background:#fff;border:1px solid #e3e6ea;border-radius:6px;padding:10px 14px;margin-bottom:8px;text-decoration:none;color:#1a1f29;font-size:13.5px;max-width:640px;}
  .arow span{color:#6b7380;float:right;}
  .empty{color:#6b7380;font-style:italic;}
  .msg{margin-top:8px;font-size:13px;}
  .panel{max-width:640px;padding:14px;background:#fff;border:1px solid #e3e6ea;border-radius:6px;margin-top:10px;}
  .tabs{display:flex;gap:8px;max-width:640px;border-bottom:1px solid #e3e6ea;margin-bottom:16px;}
  .tab-btn{margin:0;background:none;color:#6b7380;border:none;border-bottom:2px solid transparent;border-radius:0;padding:10px 4px;font-size:14px;font-weight:600;cursor:pointer;}
  .tab-btn.active{color:#144e94;border-bottom-color:#1c63b7;}
  .tab-panel{display:none;}
  .tab-panel.active{display:block;}
  hr{max-width:640px;margin:14px 0;border:none;border-top:1px solid #e3e6ea;}

  .sub-view{display:none;}
  .sub-view.active{display:block;}
  .subview-topbar{display:flex;align-items:center;gap:14px;margin-bottom:16px;max-width:640px;}
  .subview-topbar h1{margin:0;}
  .back-link{color:#1c63b7;font-size:14px;font-weight:600;text-decoration:none;background:none;border:none;padding:0;margin:0;cursor:pointer;}
  .home-tiles{display:flex;flex-direction:column;gap:12px;max-width:640px;}
  .home-tile{display:flex;align-items:center;gap:14px;width:100%;text-align:left;background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px;margin:0;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .home-tile:disabled{opacity:.55;cursor:default;}
  .home-tile svg{width:26px;height:26px;color:#1c63b7;flex:0 0 auto;}
  .home-tile .tile-title{font-size:15px;font-weight:700;color:#1a1f29;}
  .home-tile .tile-sub{font-size:12.5px;color:#6b7380;margin-top:2px;}
</style></head><body>

<div id="home-view" class="sub-view active">
  <h1>FOS</h1>
  <div class="home-tiles">
    <button class="home-tile" onclick="showHomeView('load-sequence')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
      <div><div class="tile-title">Load New Sequence</div><div class="tile-sub">Import a PBS bid pack, or start a flight manually</div></div>
    </button>
    <button class="home-tile" onclick="showHomeView('import-simbrief')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      <div><div class="tile-title">Import from SimBrief</div><div class="tile-sub">Load whatever OFP is on your account right now</div></div>
    </button>
    <button class="home-tile" id="current-flight-tile" onclick="goToCurrentFlight()" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.8 19.2L16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-1 .1-1.3.5l-.7.9c-.4.4-.2 1.1.3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 2.8 5.8c.3.5.9.7 1.4.3l.9-.7c.3-.3.5-.8.4-1.2z"/></svg>
      <div><div class="tile-title">Current Flight</div><div class="tile-sub" id="current-flight-sub">Checking…</div></div>
    </button>
  </div>

  <h1 style="margin-top:28px;font-size:15px;">Recent Flights</h1>
  <div id="archive-list">$rows</div>
</div>

<div id="load-sequence-view" class="sub-view">
  <div class="subview-topbar">
    <button class="back-link" onclick="showHomeView('home')">Back</button>
    <h1>Load New Sequence</h1>
  </div>
  <div class="tabs">
    <button class="tab-btn active" id="tab-pbs-btn" onclick="showTab('pbs')">Import PBS</button>
    <button class="tab-btn" id="tab-manual-btn" onclick="showTab('manual')">Fill In Manually</button>
  </div>

  <div id="tab-pbs" class="tab-panel active">
    <label for="pbs-file">Import from file</label>
    <input type="file" id="pbs-file" accept=".txt,text/plain" onchange="loadPbsFile(event)">
    <div style="margin:10px 0 4px;color:#6b7380;font-size:12px;">— or paste below —</div>
    <textarea id="pbs-text" placeholder="Paste your crew pairing builder's PBS bid-pack export here"></textarea><br>
    <button onclick="importPbs()">Import</button>
    <div id="import-msg" class="msg"></div>

    <h1 style="margin-top:28px;">Sequences</h1>
    <div id="seq-list"><p class="empty">No sequences imported yet.</p></div>
    <div id="seq-open-msg" class="msg"></div>
  </div>

  <div id="tab-manual" class="tab-panel">
    <div class="panel">
      <div style="font-size:13px;color:#6b7380;margin-bottom:4px;">Skips PBS entirely — just enough to identify the flight. Everything else (aircraft, times, fuel...) gets set on SimBrief's own dispatch page on the next screen.</div>
      <label for="manual-orig">Origin</label>
      <input id="manual-orig" type="text" placeholder="ICAO or IATA">
      <label for="manual-dest">Destination</label>
      <input id="manual-dest" type="text" placeholder="ICAO or IATA">
      <label for="manual-fltnum">Flight Number</label>
      <input id="manual-fltnum" type="text">
      <br><button onclick="submitManualEntry()">Continue to SimBrief</button>
      <div id="manual-msg" class="msg"></div>
    </div>
  </div>
</div>

<div id="import-simbrief-view" class="sub-view">
  <div class="subview-topbar">
    <button class="back-link" onclick="showHomeView('home')">Back</button>
    <h1>Import from SimBrief</h1>
  </div>
  <div class="panel">
    <div style="font-size:13px;color:#6b7380;margin-bottom:4px;">Loads whatever OFP is currently on this SimBrief account right now — for dispatching the flight you're on today, not for browsing a schedule.</div>
    <label for="sb-user">SimBrief Username</label>
    <input id="sb-user" type="text" placeholder="Your SimBrief username">
    <br><button onclick="loadFromSimbrief()">Load Current Flight</button>
    <div id="sb-msg" class="msg"></div>
  </div>
</div>

<script>
function showHomeView(view){
  document.getElementById('home-view').classList.toggle('active', view==='home');
  document.getElementById('load-sequence-view').classList.toggle('active', view==='load-sequence');
  document.getElementById('import-simbrief-view').classList.toggle('active', view==='import-simbrief');
}
function showTab(tab){
  document.getElementById('tab-pbs').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-manual').classList.toggle('active', tab==='manual');
  document.getElementById('tab-pbs-btn').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-manual-btn').classList.toggle('active', tab==='manual');
}

function loadArchive(){
  fetch('/archive').then(r=>r.json()).then(rows=>{
    document.getElementById('archive-list').innerHTML = rows.map(r =>
      `<a class="arow" href="/fos/${r.id}">${r.flight_number||''} ${r.origin||''}→${r.destination||''} <span>${r.dep_date||''}</span></a>`
    ).join('') || '<p class="empty">No legs generated yet.</p>';
  });
}

function loadPbsFile(e){
  const file = e.target.files[0];
  if(!file) return;
  const el = document.getElementById('import-msg');
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById('pbs-text').value = reader.result;
    importPbs();
  };
  reader.onerror = () => {
    el.textContent = 'Could not read that file.';
    el.style.color = '#c0392b';
  };
  reader.readAsText(file);
}
function importPbs(){
  const el = document.getElementById('import-msg');
  const text = document.getElementById('pbs-text').value;
  if(!text.trim()){ el.textContent = 'Paste bid-pack text first.'; el.style.color = '#c0392b'; return; }
  fetch('/import-pbs', {method:'POST', headers:{'Content-Type':'text/plain'}, body: text})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      if(!ok){ el.textContent = data.error || 'Import failed'; el.style.color = '#c0392b'; return; }
      el.textContent = `Imported ${data.sequences_parsed} sequence(s), ${data.legs_parsed} legs.`;
      el.style.color = '#2fa355';
      loadSequences();
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = '#c0392b'; });
}

function loadSequences(){
  fetch('/pbs/sequences').then(r=>r.json()).then(seqs=>{
    const list = document.getElementById('seq-list');
    list.innerHTML = seqs.map(s =>
      `<a class="arow" href="#" onclick="openSequence('${s.seq}');return false;">SEQ ${s.seq} — ${(s.routing||[]).join('-')} <span>${s.days} day(s)</span></a>`
    ).join('') || '<p class="empty">No sequences imported yet.</p>';
  });
}

async function openSequence(seq){
  const el = document.getElementById('seq-open-msg');
  el.textContent = 'Opening…';
  el.style.color = '';
  try {
    const seqR = await fetch('/pbs/sequences/' + seq);
    const seqData = await seqR.json();
    if(!seqR.ok){ el.textContent = seqData.error || 'Sequence not found'; el.style.color = '#c0392b'; return; }
    const position = (seqData.positions && seqData.positions[0]) || '';
    const genR = await fetch('/pbs/sequences/' + seq + '/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: 1, leg_index: 0, position: position}),
    });
    const genData = await genR.json();
    if(!genR.ok){ el.textContent = genData.error || 'Generate failed'; el.style.color = '#c0392b'; return; }
    window.location.href = genData.fos_url + '?view=release';
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = '#c0392b';
  }
}

async function submitManualEntry(){
  const el = document.getElementById('manual-msg');
  const origin = document.getElementById('manual-orig').value.trim().toUpperCase();
  const destination = document.getElementById('manual-dest').value.trim().toUpperCase();
  const flight_number = document.getElementById('manual-fltnum').value.trim();
  if(!origin || !destination || !flight_number){
    el.textContent = 'Origin, destination, and flight number are all required.';
    el.style.color = '#c0392b';
    return;
  }
  el.textContent = 'Starting…';
  el.style.color = '';
  try {
    const r = await fetch('/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({origin, destination, flight_number}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Could not start this flight'; el.style.color = '#c0392b'; return; }
    window.location.href = data.fos_url + '?view=release';
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = '#c0392b';
  }
}

function loadFromSimbrief(){
  const el = document.getElementById('sb-msg');
  const user = document.getElementById('sb-user').value.trim();
  if(!user){ el.textContent = 'Enter a SimBrief username first.'; el.style.color = '#c0392b'; return; }
  localStorage.setItem('fos_simbrief_user', user);
  el.textContent = 'Loading current flight…';
  el.style.color = '';
  fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      if(!ok){ el.textContent = data.error || 'Load failed'; el.style.color = '#c0392b'; return; }
      window.location.href = data.fos_url + '?view=confirm';
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = '#c0392b'; });
}

// "Current Flight" is deliberately not an auto-redirect — Home is always
// reachable and always shows all three options. This just checks whether
// there's something to resume and reports it right on the tile, verified
// against /archive so a stale id (leg gone after a backend restart, which
// currently wipes everything since there's no persistent store yet)
// disables the tile instead of leading somewhere broken.
function goToCurrentFlight(){
  const lastLeg = localStorage.getItem('fos_last_leg');
  if(lastLeg) window.location.href = '/fos/' + lastLeg;
}
(function(){
  const tile = document.getElementById('current-flight-tile');
  const sub = document.getElementById('current-flight-sub');
  const lastLeg = localStorage.getItem('fos_last_leg');
  if(!lastLeg){ sub.textContent = 'No active flight yet'; return; }
  fetch('/archive').then(r => r.json()).then(rows => {
    const match = rows.find(r => String(r.id) === lastLeg);
    if(match){
      sub.textContent = `${match.flight_number || ''} ${match.origin || ''}→${match.destination || ''}`.trim();
      tile.disabled = false;
    } else {
      localStorage.removeItem('fos_last_leg');
      sub.textContent = 'No active flight yet';
    }
  }).catch(() => { sub.textContent = 'No active flight yet'; });
})();

(function(){
  const saved = localStorage.getItem('fos_simbrief_user');
  if(saved) document.getElementById('sb-user').value = saved;
})();

loadSequences();
</script>
</body></html>"""


FOS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#142c52">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="mobileFOS">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<title>Flight $flight_number \u2013 FOS</title>
<style>
  :root{
    --navy:#142c52; --blue:#1c63b7; --blue-dark:#144e94; --green:#2fa355;
    --bg:#eef1f4; --card:#fff; --border:#e3e6ea; --label:#6b7380; --value:#1a1f29;
    --red:#e0393e; --inactive:#9aa1ab; --radius:6px;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;height:100%;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--value);-webkit-font-smoothing:antialiased;}
  button{font-family:inherit;}
  :focus-visible{outline:2px solid var(--blue-dark);outline-offset:2px;}
  @media (prefers-reduced-motion: reduce){ *{transition:none !important;animation:none !important;} }
  .app-shell{display:flex;min-height:100vh;min-height:100dvh;width:100%;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom);padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right);}
  .sidebar{width:64px;flex:0 0 64px;background:var(--navy);display:flex;flex-direction:column;align-items:center;padding:14px 0;gap:6px;}
  .side-btn{width:44px;height:44px;display:flex;align-items:center;justify-content:center;background:transparent;border:none;color:#9db3d6;border-radius:8px;cursor:pointer;position:relative;}
  .side-btn svg{width:22px;height:22px;}
  .side-btn.active{background:var(--blue-dark);color:#fff;}
  .side-btn .badge{position:absolute;top:2px;right:2px;width:15px;height:15px;border-radius:50%;background:var(--red);color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;}
  .main{flex:1;min-width:0;padding:14px 16px 44px;}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;margin-bottom:10px;}
  .back-link{order:1;color:var(--blue-dark);font-size:14px;font-weight:500;background:none;border:none;cursor:pointer;padding:4px 2px;}
  .topbar-actions{order:2;margin-left:auto;display:flex;align-items:center;gap:14px;}
  .topbar-title{order:3;flex:1 1 100%;text-align:center;margin-top:2px;}
  .topbar-title h1{font-size:17px;margin:0;font-weight:600;color:var(--blue-dark);}
  .topbar-title p{font-size:11px;margin:2px 0 0;color:var(--label);}
  .icon-btn{background:none;border:none;color:#5b6472;cursor:pointer;padding:2px;display:flex;}
  .icon-btn svg{width:19px;height:19px;}
  .status-bar{background:var(--green);color:#fff;display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-radius:var(--radius) var(--radius) 0 0;font-size:13px;font-weight:600;}
  .flight-summary{background:var(--card);display:flex;align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);font-size:13px;gap:18px;flex-wrap:wrap;}
  .flight-summary .fnum{font-size:15px;font-weight:700;}
  .flight-summary .col{display:flex;flex-direction:column;line-height:1.5;}
  .flight-summary .col.highlight{color:var(--green);font-weight:600;}
  .duty-badges{display:flex;justify-content:flex-end;gap:22px;padding:10px 14px 8px;background:var(--card);font-size:12px;font-weight:600;}
  .duty-badges span{display:flex;align-items:center;gap:5px;color:var(--green);cursor:pointer;}
  .duty-badges span.inactive{color:var(--inactive);}
  .duty-badges svg{width:15px;height:15px;}
  .docs-btn{display:block;width:100%;background:var(--blue);color:#fff;border:none;padding:11px;font-size:14px;font-weight:600;cursor:pointer;}
  .docs-btn:hover{background:var(--blue-dark);}
  .card{background:var(--card);border-radius:0 0 var(--radius) var(--radius);overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .content-grid{display:grid;grid-template-columns:1fr 1fr;}
  .col-divider{border-right:1px solid var(--border);}
  .info-row{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-bottom:1px solid var(--border);font-size:13.5px;gap:10px;}
  .info-row .lbl{color:var(--label);}
  .info-row .val{color:var(--value);font-weight:500;text-align:right;word-break:break-word;}
  .search-block{background:var(--card);padding:12px 14px;border-bottom:1px solid var(--border);}
  .search-block label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;}
  .search-block input,.search-block select{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:5px;font-size:13.5px;background:#fbfbfc;}
  .search-row{display:flex;gap:10px;background:var(--card);padding:12px 14px;border-bottom:1px solid var(--border);}
  .search-row .search-block{flex:1;border-bottom:none;padding:0;background:none;}
  .check-grid{display:flex;flex-wrap:wrap;gap:10px 18px;background:var(--card);padding:12px 14px;border-bottom:1px solid var(--border);}
  .check-grid label{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:600;margin:0;}
  .check-grid input{width:auto;}
  .section-bar{display:flex;align-items:center;justify-content:space-between;background:var(--blue);color:#fff;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer;border:none;width:100%;text-align:left;}
  .section-bar svg{width:16px;height:16px;transition:transform .15s ease;}
  .section-bar.collapsed svg.chevron{transform:rotate(180deg);}
  .no-prefs{padding:10px 14px;color:var(--label);font-size:13px;font-style:italic;background:var(--card);border-bottom:1px solid var(--border);}
  .doc-list{background:var(--card);}
  .doc-row{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-bottom:1px solid var(--border);gap:10px;}
  .doc-row .code{font-weight:700;font-size:13px;}
  .doc-row .desc{font-size:12px;color:var(--label);margin-top:1px;}
  .doc-row .actions{display:flex;align-items:center;gap:26px;flex:0 0 auto;}
  .doc-row .actions svg{width:19px;height:19px;color:#5b6472;cursor:pointer;padding:7px;margin:-7px;box-sizing:content-box;}
  .doc-row .check{color:var(--inactive,#9aa1ab);cursor:pointer;}
  .doc-row .check.signed{color:var(--green);}
  #sign-pad{touch-action:none;background:#fff;border:1px solid var(--border);border-radius:6px;width:100%;height:220px;}
  .placeholder-note{padding:12px 14px;color:var(--label);font-style:italic;font-size:13px;background:var(--card);}
  .view{display:none;}
  .view.active{display:block;}
  #toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(12px);background:#1a1f29;color:#fff;padding:9px 16px;border-radius:20px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .18s ease, transform .18s ease;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:10;}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
  @media (max-width:640px){
    .sidebar{width:52px;flex-basis:52px;padding:10px 0;}
    .side-btn{width:38px;height:38px;}
    .content-grid{grid-template-columns:1fr;}
    .col-divider{border-right:none;border-bottom:6px solid var(--bg);}
    .flight-summary{gap:12px;font-size:12px;}
  }
</style>
</head>
<body>
<div class="app-shell">
  <nav class="sidebar" aria-label="Primary">
    <button class="side-btn active" id="nav-home" title="Home" onclick="showView('overview')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg>
    </button>
    <button class="side-btn" id="nav-pairing" title="Pairing" onclick="showView('pairing')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/></svg>
    </button>
    <button class="side-btn" title="Messages" onclick="showToast('Messages')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 6l9 7 9-7"/></svg>
      <span class="badge">1</span>
    </button>
    <button class="side-btn" id="nav-docs" title="Documents" onclick="showView('documents')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M9.5 13h5M9.5 16h5"/></svg>
    </button>
    <button class="side-btn" id="nav-release" title="Release" onclick="showView('release')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M12 11v6"/><path d="M9.5 14.5L12 17l2.5-2.5"/></svg>
    </button>
    <button class="side-btn" title="Web" onclick="showToast('Web')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.6 2.5 15.4 0 18M12 3c-2.5 2.6-2.5 15.4 0 18"/></svg>
    </button>
  </nav>
  <main class="main">
    <section id="overview-view" class="view active">
      <div class="topbar">
        <a class="back-link" href="/" onclick="localStorage.removeItem('fos_last_leg')">Back</a>
        <div class="topbar-actions">
          <button class="icon-btn" title="Settings" onclick="showToast('Settings')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
          </button>
        </div>
        <div class="topbar-title">
          <h1>Flight $flight_number</h1>
          <p>Pull to Refresh</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="flight-summary">
        <div class="fnum">$flight_number</div>
        <div class="col"><span>$origin</span><span>$destination</span></div>
        <div class="col"><span>$dep_date</span><span>$arr_date</span></div>
        <div class="col"><span>$sched_out</span><span>$sched_in</span></div>
        <div class="col highlight"><span>$est_out</span><span>$est_in</span></div>
      </div>
      <div class="duty-badges">
        <span id="signin-badge" class="$signed_in_class" onclick="toggleStatus('signin','signin-badge')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M7.5 12.5l3 3 6-6"/></svg> Signed In
        </span>
        <span id="ffd-badge" class="$ffd_class" onclick="toggleStatus('fit-for-duty','ffd-badge')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M7.5 12.5l3 3 6-6"/></svg> Fit for Duty
        </span>
      </div>
      <button class="docs-btn" onclick="showView('documents')">Pre-Flight Documents</button>
      <button class="docs-btn" id="pairing-btn" style="background:var(--blue-dark);border-top:1px solid rgba(255,255,255,.2);" onclick="showView('pairing')">View Full Pairing</button>
      <div class="card">
        <div class="content-grid">
          <div class="col-divider">
            <div class="info-row"><span class="lbl">Arrival Date</span><span class="val">$arr_date</span></div>
            <div class="info-row"><span class="lbl">Departure Gate</span><span class="val" id="ov-dep-gate">$dep_gate</span></div>
            <div class="info-row"><span class="lbl">Arrival Gate</span><span class="val" id="ov-arr-gate">$arr_gate</span></div>
            <div class="info-row"><span class="lbl">Fleet Type</span><span class="val">$fleet_type</span></div>
            <div class="info-row"><span class="lbl">Tail Number</span><span class="val">$tail_number $tail_routing</span></div>
            <div class="info-row"><span class="lbl">Status</span><span class="val">$status</span></div>
            <div class="info-row"><span class="lbl">Customer Load</span><span class="val">$customer_load</span></div>
            <div class="info-row"><span class="lbl">Equipment Type</span><span class="val">$equipment_type</span></div>
            <div class="info-row"><span class="lbl">Duty Time</span><span class="val">$duty_time</span></div>
            <div class="info-row" style="border-bottom:none;"><span class="lbl">Ground Time</span><span class="val">$ground_time</span></div>
          </div>
          <div>
            <div class="info-row"><span class="lbl">Flight Time</span><span class="val">$flight_time</span></div>
            <div class="info-row"><span class="lbl">ODL Time</span><span class="val">$odl_time</span></div>
            <div class="info-row"><span class="lbl">Time Zone Difference</span><span class="val">$tz_diff</span></div>
            <div class="info-row"><span class="lbl">Position</span><span class="val">$position</span></div>
            <div class="info-row"><span class="lbl">Crew</span><span class="val">$crew_display</span></div>
            <div class="info-row"><span class="lbl">Hotel Details</span><span class="val">$hotel_details</span></div>
            <div class="info-row" style="border-bottom:none;"><span class="lbl">Limo Details</span><span class="val">$limo_details</span></div>
          </div>
        </div>
      </div>
    </section>
    <section id="documents-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-actions">
          <button class="icon-btn" title="Settings" onclick="showToast('Settings')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
          </button>
        </div>
        <div class="topbar-title">
          <h1>Flight $flight_number</h1>
          <p>Pull to Refresh</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="flight-summary">
        <div class="fnum">$flight_number</div>
        <div class="col"><span>$origin</span><span>$destination</span></div>
        <div class="col"><span>$dep_date</span><span>$arr_date</span></div>
        <div class="col"><span>$sched_out</span><span>$sched_in</span></div>
        <div class="col"><span id="doc-dep-gate">$dep_gate</span><span id="doc-arr-gate">$arr_gate</span></div>
      </div>
      <div class="search-block">
        <label for="doc-search">Find a Document</label>
        <input id="doc-search" type="text" placeholder="">
      </div>
      <button class="section-bar" onclick="showToast('Edit preferences')">
        My Preferences
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
      </button>
      <div class="no-prefs">No preferences saved</div>
      <button class="section-bar collapsed" id="crew-bar" onclick="toggleSection('crew')">
        Crew
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="placeholder-note" id="crew-body" style="display:none;">$crew_display</div>
      <button class="section-bar" id="flight-bar" onclick="toggleSection('flight')">
        Flight
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="doc-list" id="flight-body">
        <div class="doc-row">
          <div><div class="code">EFLIGHT PLAN</div><div class="desc">eFlight Plan</div></div>
          <div class="actions">
            <svg id="sign-check" class="check $signed_class" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="openSignPad()"><path d="M20 6L9 17l-5-5"/></svg>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('rls','eFlight Plan')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">FI</div><div class="desc">Flight Details \u2013 GMT</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('fi','Flight Details \u2013 GMT')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">FIL</div><div class="desc">Flight Details \u2013 Local</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('fil','Flight Details \u2013 Local')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">WBD</div><div class="desc">Weight &amp; Balance Data (TPS)</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('wb','Weight &amp; Balance Data')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row" style="border-bottom:none;">
          <div><div class="code">G*L/SS</div><div class="desc">Customers Requiring Special Services</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Not available \u2014 no data source for this document yet')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
      </div>
      <button class="section-bar collapsed" id="weather-bar" onclick="toggleWeatherSection()">
        Weather
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div id="weather-body" style="display:none;"><p class="placeholder-note">Tap to load current METAR/TAF (uses the SimBrief username from Release/Load-from-SimBrief)</p></div>
    </section>

    <section id="pdf-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="closePdfView()">Back</button>
        <div class="topbar-actions">
          <a id="pdf-export-link" style="font-size:14px;color:var(--blue-dark);text-decoration:none;font-weight:600;">Export</a>
        </div>
        <div class="topbar-title">
          <h1 id="pdf-view-title"></h1>
        </div>
      </div>
      <div id="pdf-pages" style="background:#525659;margin:0 -16px;padding:12px 12px 32px;display:flex;flex-direction:column;align-items:center;gap:12px;"></div>
    </section>

    <section id="sign-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('documents')">Back</button>
        <div class="topbar-title">
          <h1>Sign eFlight Plan</h1>
          <p id="sign-status-line">Acknowledges receipt of the current release</p>
        </div>
      </div>
      <div class="search-block">
        <label>Sign below</label>
        <canvas id="sign-pad"></canvas>
        <div style="display:flex;gap:10px;margin-top:10px;">
          <button onclick="clearSignPad()" style="margin:0;background:var(--label);color:#fff;border:none;padding:10px 16px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;flex:0 0 auto;">Clear</button>
          <button id="sign-submit-btn" onclick="submitSignature()" style="margin:0;background:var(--blue);color:#fff;border:none;padding:10px 16px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;flex:1;">Sign &amp; Submit</button>
        </div>
        <div id="sign-msg" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
      </div>
    </section>
    <section id="pairing-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title">
          <h1>Pairing</h1>
          <p>SEQ $seq — full trip</p>
        </div>
      </div>
      <div id="pairing-body"><p class="placeholder-note">Loading…</p></div>
    </section>
    <section id="release-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title">
          <h1>Flight $flight_number</h1>
          <p>Send to SimBrief</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="search-block">
        <label for="release-user">SimBrief Username</label>
        <input id="release-user" type="text" placeholder="Your SimBrief username">
      </div>

      <button class="section-bar" id="aero-bar" onclick="toggleSection('aero')">
        Route &amp; Gate Suggestions (FlightAware)
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div id="aero-body">
        <div class="search-block">
          <label for="aero-key">FlightAware AeroAPI Key</label>
          <input id="aero-key" type="password" placeholder="Your AeroAPI key" onchange="localStorage.setItem('fos_aeroapi_key', this.value.trim())">
        </div>
        <div style="padding:14px;background:var(--card);">
          <button id="aero-btn" onclick="fetchAeroSuggestions()" style="margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;">Get Suggestions</button>
          <div id="aero-msg" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
          <div id="aero-results" style="display:none;margin-top:10px;">
            <div class="info-row"><span class="lbl">Suggested Route</span><span class="val" id="aero-route-val">—</span></div>
            <button onclick="applyAeroRoute()" style="margin:8px 0 0;width:100%;background:var(--green);color:#fff;border:none;padding:9px;border-radius:5px;font-size:13px;font-weight:600;cursor:pointer;">Use This Route</button>
            <div class="info-row" style="margin-top:10px;"><span class="lbl">Suggested Gate — Origin</span><span class="val" id="aero-gate-orig">—</span></div>
            <div class="info-row"><span class="lbl">Suggested Gate — Destination</span><span class="val" id="aero-gate-dest">—</span></div>
            <button onclick="applyAeroGates()" style="margin:8px 0 0;width:100%;background:var(--green);color:#fff;border:none;padding:9px;border-radius:5px;font-size:13px;font-weight:600;cursor:pointer;">Apply Gates to This Flight</button>
            <p class="placeholder-note" id="aero-basis" style="margin-top:8px;"></p>
          </div>
        </div>
      </div>

      <button class="section-bar" id="sbgen-bar" onclick="toggleSection('sbgen')">
        Generate Flight Plan (SimBrief)
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div id="sbgen-body">
        <div class="search-row">
          <div class="search-block"><label for="sbgen-orig">Origin (ICAO)</label><input id="sbgen-orig" type="text" placeholder="ZZZZ"></div>
          <div class="search-block"><label for="sbgen-dest">Destination (ICAO)</label><input id="sbgen-dest" type="text" placeholder="ZZZZ"></div>
        </div>
        <div class="search-block">
          <label for="sbgen-type">Aircraft Type — code (b738) or your saved airframe's Internal ID (123456_1582090020)</label>
          <input id="sbgen-type" type="text" placeholder="b738 or 123456_1582090020">
        </div>
        <div class="search-block">
          <label for="sbgen-route">Route (optional — blank uses SimBrief's last-used route for this city pair)</label>
          <input id="sbgen-route" type="text" placeholder="optional">
        </div>
        <div class="search-row">
          <div class="search-block"><label for="sbgen-airline">Airline (ICAO)</label><input id="sbgen-airline" type="text"></div>
          <div class="search-block"><label for="sbgen-fltnum">Flight Number</label><input id="sbgen-fltnum" type="text"></div>
        </div>
        <div class="search-row">
          <div class="search-block"><label for="sbgen-date">Date (DDMMMYY)</label><input id="sbgen-date" type="text" placeholder="18AUG26"></div>
          <div class="search-block"><label for="sbgen-time">Dep Time, local (HHMM)</label><input id="sbgen-time" type="text"></div>
        </div>
        <div class="search-block">
          <label for="sbgen-reg">Tail Number (optional)</label>
          <input id="sbgen-reg" type="text" placeholder="optional">
        </div>
        <p class="placeholder-note">Everything else — fuel, alternates, crew, output options — is set on SimBrief's own dispatch page after you tap below.</p>
        <div style="padding:14px;background:var(--card);">
          <button id="sbgen-btn" onclick="submitSimbriefGen()" style="margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;">Open in SimBrief Dispatch</button>
          <div id="sbgen-msg" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
        </div>
      </div>

    </section>
    <section id="confirm-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title">
          <h1>Flight $flight_number</h1>
          <p>Confirm &amp; Generate Release</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="flight-summary">
        <div class="fnum">$flight_number</div>
        <div class="col"><span>$origin</span><span>$destination</span></div>
        <div class="col"><span>$dep_date</span><span>$arr_date</span></div>
        <div class="col"><span>$sched_out</span><span>$sched_in</span></div>
        <div class="col highlight"><span>$est_out</span><span>$est_in</span></div>
      </div>
      <p class="placeholder-note">Check this is the flight you just sent to SimBrief before generating the release.</p>
      <div class="card">
        <div class="content-grid">
          <div class="col-divider">
            <div class="info-row"><span class="lbl">Block Fuel</span><span class="val">$block_fuel</span></div>
            <div class="info-row"><span class="lbl">Taxi Fuel</span><span class="val">$taxi_fuel</span></div>
            <div class="info-row"><span class="lbl">Trip Fuel</span><span class="val">$trip_fuel</span></div>
            <div class="info-row"><span class="lbl">Takeoff Fuel</span><span class="val">$takeoff_fuel</span></div>
            <div class="info-row" style="border-bottom:none;"><span class="lbl">Landing Fuel</span><span class="val">$landing_fuel</span></div>
          </div>
          <div>
            <div class="info-row"><span class="lbl">Reserve Fuel</span><span class="val">$reserve_fuel</span></div>
            <div class="info-row"><span class="lbl">Alternate Fuel</span><span class="val">$alternate_fuel</span></div>
            <div class="info-row"><span class="lbl">Contingency Fuel</span><span class="val">$contingency_fuel</span></div>
            <div class="info-row" style="border-bottom:none;"><span class="lbl">Extra Fuel</span><span class="val">$extra_fuel</span></div>
          </div>
        </div>
      </div>
      <div style="padding:14px;background:var(--card);">
        <button class="docs-btn" id="release-gen-btn" style="border-radius:5px;" onclick="generateRelease()">Generate Release</button>
        <div id="release-status" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
        <div id="release-downloads" style="display:none;margin-top:10px;gap:10px;flex-wrap:wrap;">
          <a id="release-rls-link" style="display:none;background:var(--green);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download RLS PDF</a>
          <a id="release-wb-link" style="display:none;background:var(--blue-dark);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download W&amp;B PDF</a>
        </div>
        <button id="confirm-continue-btn" style="display:none;margin-top:10px;width:100%;background:var(--label);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;" onclick="showView('overview')">Continue to Flight</button>
      </div>
    </section>
  </main>
</div>
<div id="toast"></div>
<script>
const LEG_ID = "$leg_id";
if(LEG_ID) localStorage.setItem('fos_last_leg', LEG_ID);
const LEG_FLIGHT_NUMBER = "$flight_number";
const LEG_ORIGIN = "$origin";
const LEG_DESTINATION = "$destination";
const LEG_TAIL_NUMBER = "$tail_number";
const LEG_SCHED_OUT = "$sched_out";
const LEG_FLEET_TYPE = "$fleet_type";
const LEG_FLEET_TYPE_ICAO = "$fleet_type_icao";
const LEG_EQUIPMENT_TYPE = "$equipment_type";
function showView(view){
  document.getElementById('overview-view').classList.toggle('active', view==='overview');
  document.getElementById('documents-view').classList.toggle('active', view==='documents');
  document.getElementById('release-view').classList.toggle('active', view==='release');
  document.getElementById('confirm-view').classList.toggle('active', view==='confirm');
  document.getElementById('pdf-view').classList.toggle('active', view==='pdf');
  document.getElementById('sign-view').classList.toggle('active', view==='sign');
  document.getElementById('pairing-view').classList.toggle('active', view==='pairing');
  document.getElementById('nav-home').classList.toggle('active', view==='overview');
  document.getElementById('nav-docs').classList.toggle('active', view==='documents');
  document.getElementById('nav-release').classList.toggle('active', view==='release' || view==='confirm');
  document.getElementById('nav-pairing').classList.toggle('active', view==='pairing');
  window.scrollTo(0,0);
  if(view === 'release') initReleaseView();
  if(view === 'confirm') initConfirmView();
  if(view === 'sign') initSignPad();
  if(view === 'pairing') initPairingView();
}
function initReleaseView(){
  const input = document.getElementById('release-user');
  const saved = localStorage.getItem('fos_simbrief_user');
  if(saved && !input.value) input.value = saved;
  prefillSimbriefGen();
}
let _releaseStatusChecked = false;
function initConfirmView(){
  if(_releaseStatusChecked) return;
  _releaseStatusChecked = true;
  fetch('/release/status').then(r=>r.json()).then(data=>{
    if(!data.available){
      const status = document.getElementById('release-status');
      status.textContent = 'Release generation unavailable: ' + (data.error || 'unknown error');
      status.style.color = '#c0392b';
      document.getElementById('release-gen-btn').disabled = true;
    }
  }).catch(()=>{});
}
async function prefillSimbriefGen(){
  document.getElementById('aero-key').value = localStorage.getItem('fos_aeroapi_key') || '';
  // Leg-specific aircraft data wins over the last-remembered one: a
  // SimBrief-loaded leg's fleet_type is already a real ICAO type code
  // (aircraft/icaocode); a PBS-pairing leg's raw equipment token gets
  // normalized server-side (_fleet_type_icao) for the sub-fleet prefixes
  // that are confirmed (31x->A319, 21x->A321 today) and passed through
  // unchanged otherwise.
  document.getElementById('sbgen-type').value = LEG_FLEET_TYPE_ICAO || LEG_EQUIPMENT_TYPE || localStorage.getItem('fos_simbrief_airframe') || '';
  document.getElementById('sbgen-fltnum').value = LEG_FLIGHT_NUMBER || '';
  document.getElementById('sbgen-date').value = todayZuluDDMMMYY();
  document.getElementById('sbgen-time').value = (LEG_SCHED_OUT || '').replace(':', '');
  document.getElementById('sbgen-reg').value = LEG_TAIL_NUMBER || '';

  let orig = LEG_ORIGIN, dest = LEG_DESTINATION, airline = '';
  if(LEG_SEQ){
    try {
      const r = await fetch('/pbs/sequences/' + encodeURIComponent(LEG_SEQ));
      if(r.ok){
        const seqData = await r.json();
        airline = seqData.operator || '';
        outer:
        for(const day of (seqData.duty_days || [])){
          for(const l of (day.legs || [])){
            if(l.flight_number === LEG_FLIGHT_NUMBER && l.origin === LEG_ORIGIN && l.destination === LEG_DESTINATION){
              orig = l.origin_icao || l.origin;
              dest = l.destination_icao || l.destination;
              if(!document.getElementById('sbgen-time').value) document.getElementById('sbgen-time').value = l.dep_local || '';
              break outer;
            }
          }
        }
      }
    } catch(e) { /* best-effort — leave the leg's own fields in place */ }
  }
  document.getElementById('sbgen-orig').value = orig || '';
  document.getElementById('sbgen-dest').value = dest || '';
  document.getElementById('sbgen-airline').value = airline || '';
}

let _aeroSuggestion = null;
async function fetchAeroSuggestions(){
  const msg = document.getElementById('aero-msg');
  const btn = document.getElementById('aero-btn');
  const key = document.getElementById('aero-key').value.trim();
  const orig = document.getElementById('sbgen-orig').value.trim().toUpperCase();
  const dest = document.getElementById('sbgen-dest').value.trim().toUpperCase();
  document.getElementById('aero-results').style.display = 'none';
  if(!key){ msg.textContent = 'Enter your AeroAPI key first.'; msg.style.color = '#c0392b'; return; }
  if(!orig || !dest){ msg.textContent = 'Origin and Destination are required — set those in Generate Flight Plan below first.'; msg.style.color = '#c0392b'; return; }
  localStorage.setItem('fos_aeroapi_key', key);

  btn.disabled = true;
  msg.textContent = 'Looking up AA at ' + orig + ' / ' + dest + '…';
  msg.style.color = '';
  try {
    const r = await fetch('/aeroapi/suggest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_key: key, orig, dest}),
    });
    const data = await r.json();
    btn.disabled = false;
    if(!r.ok){ msg.textContent = data.error || 'Lookup failed'; msg.style.color = '#c0392b'; return; }
    _aeroSuggestion = data;
    document.getElementById('aero-route-val').textContent = data.route_found ? (data.route || '(no filed route on record)') : `AA doesn't fly ${orig}→${dest} nonstop — no route suggestion`;
    document.getElementById('aero-gate-orig').textContent = data.gate_origin || '—';
    document.getElementById('aero-gate-dest').textContent = data.gate_destination || '—';
    document.getElementById('aero-basis').textContent =
      `Gates based on ${data.sample_size_origin} AA departure(s) at ${orig} and ${data.sample_size_destination} AA arrival(s) at ${dest} in the last 10 days.`;
    document.getElementById('aero-results').style.display = 'block';
    msg.textContent = '';
  } catch(e) {
    btn.disabled = false;
    msg.textContent = 'Request failed: ' + e;
    msg.style.color = '#c0392b';
  }
}
function applyAeroRoute(){
  if(_aeroSuggestion && _aeroSuggestion.route) document.getElementById('sbgen-route').value = _aeroSuggestion.route;
}
async function applyAeroGates(){
  if(!_aeroSuggestion) return;
  try {
    const r = await fetch('/fos/' + LEG_ID + '/gates', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dep_gate: _aeroSuggestion.gate_origin || '', arr_gate: _aeroSuggestion.gate_destination || ''}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not apply gates'); return; }
    for(const id of ['ov-dep-gate', 'doc-dep-gate']) { const el = document.getElementById(id); if(el) el.textContent = data.dep_gate || ''; }
    for(const id of ['ov-arr-gate', 'doc-arr-gate']) { const el = document.getElementById(id); if(el) el.textContent = data.arr_gate || ''; }
    showToast('Gates applied');
  } catch(e) { showToast('Request failed: ' + e); }
}
function generateRelease(){
  const btn = document.getElementById('release-gen-btn');
  const status = document.getElementById('release-status');
  const userId = localStorage.getItem('fos_simbrief_user');
  if(!userId){ status.textContent = 'No SimBrief username on file — go back and send this flight to SimBrief first.'; status.style.color = '#c0392b'; return; }
  btn.disabled = true;
  status.style.color = '';
  status.textContent = 'Generating release — this can take up to a minute…';
  document.getElementById('release-downloads').style.display = 'none';
  fetch('/fos/' + LEG_ID + '/release', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id:userId})})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      btn.disabled = false;
      if(!ok){ status.textContent = 'Failed: ' + (data.error || 'unknown error'); status.style.color = '#c0392b'; return; }
      status.textContent = 'Release generated.';
      status.style.color = '#2fa355';
      const rlsLink = document.getElementById('release-rls-link');
      rlsLink.href = 'data:application/pdf;base64,' + data.rls_pdf_b64;
      rlsLink.download = data.filename;
      rlsLink.style.display = 'inline-block';
      const wbLink = document.getElementById('release-wb-link');
      if(data.wb_pdf_b64){
        wbLink.href = 'data:application/pdf;base64,' + data.wb_pdf_b64;
        wbLink.download = data.filename.replace('-RLS.pdf', '-WB.pdf');
        wbLink.style.display = 'inline-block';
      } else {
        wbLink.style.display = 'none';
      }
      document.getElementById('release-downloads').style.display = 'flex';
      document.getElementById('confirm-continue-btn').style.display = 'block';
    })
    .catch(e => { btn.disabled = false; status.textContent = 'Request failed: ' + e; status.style.color = '#c0392b'; });
}
function toggleSection(name){
  const bar = document.getElementById(name+'-bar');
  const body = document.getElementById(name+'-body');
  const collapsed = bar.classList.toggle('collapsed');
  body.style.display = collapsed ? 'none' : 'block';
}
function toggleStatus(kind, elId){
  fetch('/fos/' + LEG_ID + '/' + kind, {method:'POST'})
    .then(r=>r.json())
    .then(data=>{
      const key = kind === 'signin' ? 'signed_in' : 'fit_for_duty';
      document.getElementById(elId).classList.toggle('inactive', !data[key]);
    })
    .catch(()=>showToast('Update failed'));
}
let toastTimer;
function showToast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 1600);
}

let _releaseCache = null;
async function ensureRelease(){
  if(_releaseCache) return _releaseCache;
  const userId = localStorage.getItem('fos_simbrief_user');
  if(!userId){ showToast('Set a SimBrief username on the Release tab first'); return null; }
  showToast('Generating release…');
  let r, data;
  try {
    r = await fetch('/fos/' + LEG_ID + '/release', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id:userId})});
    data = await r.json();
  } catch(e) { showToast('Request failed: ' + e); return null; }
  if(!r.ok){ showToast('Failed: ' + (data.error || 'unknown error')); return null; }
  _releaseCache = data;
  return data;
}
function b64ToBytes(b64){
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for(let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
if(window.pdfjsLib){
  pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
}
let _pdfObjectUrl = null;
let _pdfRenderToken = 0;
async function renderPdfInline(bytes){
  const token = ++_pdfRenderToken;
  const container = document.getElementById('pdf-pages');
  container.innerHTML = '<p style="color:#fff;">Rendering…</p>';
  const pdf = await pdfjsLib.getDocument({data: bytes}).promise;
  if(token !== _pdfRenderToken) return; // a newer viewDoc() call superseded this one
  container.innerHTML = '';
  const targetWidth = Math.max(container.clientWidth - 24, 280);
  for(let pageNum = 1; pageNum <= pdf.numPages; pageNum++){
    if(token !== _pdfRenderToken) return;
    const page = await pdf.getPage(pageNum);
    const scale = targetWidth / page.getViewport({scale:1}).width;
    const viewport = page.getViewport({scale});
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    canvas.style.width = '100%';
    canvas.style.maxWidth = targetWidth + 'px';
    canvas.style.background = '#fff';
    canvas.style.boxShadow = '0 1px 4px rgba(0,0,0,.35)';
    container.appendChild(canvas);
    await page.render({canvasContext: canvas.getContext('2d'), viewport}).promise;
  }
}
async function viewDoc(kind, label){
  const data = await ensureRelease();
  if(!data) return;
  const field = {rls:'rls_pdf_b64', fi:'fi_pdf_b64', fil:'fil_pdf_b64', wb:'wb_pdf_b64'}[kind];
  const b64 = data[field];
  if(!b64){ showToast(label + ' not available in this release'); return; }
  document.getElementById('pdf-view-title').textContent = label;
  showView('pdf');
  // Export still uses a blob: URL (fine for downloads) — the inline VIEW uses
  // PDF.js on canvas instead of an iframe, since iOS Safari routinely refuses
  // to render PDFs inside an iframe at all (blob or data:, doesn't matter)
  // and silently kicks out to the system PDF viewer instead.
  if(_pdfObjectUrl) URL.revokeObjectURL(_pdfObjectUrl);
  _pdfObjectUrl = URL.createObjectURL(new Blob([b64ToBytes(b64)], {type:'application/pdf'}));
  const exportLink = document.getElementById('pdf-export-link');
  exportLink.href = _pdfObjectUrl;
  exportLink.download = kind === 'rls' ? data.filename : (data.filename || 'release.pdf').replace('-RLS.pdf', '-' + kind.toUpperCase() + '.pdf');
  try {
    await renderPdfInline(b64ToBytes(b64));
  } catch(e) {
    document.getElementById('pdf-pages').innerHTML = '<p style="color:#fff;padding:20px;">Failed to render this PDF: ' + e + '</p>';
  }
}
function closePdfView(){
  _pdfRenderToken++; // cancel any render still in flight
  document.getElementById('pdf-pages').innerHTML = '';
  if(_pdfObjectUrl){ URL.revokeObjectURL(_pdfObjectUrl); _pdfObjectUrl = null; }
  showView('documents');
}

function openSignPad(){
  showView('sign');
}
let _signCtx = null, _signDrawing = false, _signHasStrokes = false;
function initSignPad(){
  const canvas = document.getElementById('sign-pad');
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  _signCtx = canvas.getContext('2d');
  _signCtx.scale(dpr, dpr);
  _signCtx.lineWidth = 2.2;
  _signCtx.lineCap = 'round';
  _signCtx.lineJoin = 'round';
  _signCtx.strokeStyle = '#1a1f29';
  _signHasStrokes = false;
  document.getElementById('sign-msg').textContent = '';

  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return {x: e.clientX - r.left, y: e.clientY - r.top};
  };
  canvas.onpointerdown = (e) => {
    _signDrawing = true;
    _signHasStrokes = true;
    const p = pos(e);
    _signCtx.beginPath();
    _signCtx.moveTo(p.x, p.y);
    canvas.setPointerCapture(e.pointerId);
  };
  canvas.onpointermove = (e) => {
    if(!_signDrawing) return;
    const p = pos(e);
    _signCtx.lineTo(p.x, p.y);
    _signCtx.stroke();
  };
  const stop = () => { _signDrawing = false; };
  canvas.onpointerup = stop;
  canvas.onpointercancel = stop;
  canvas.onpointerleave = stop;
}
function clearSignPad(){
  const canvas = document.getElementById('sign-pad');
  _signCtx.clearRect(0, 0, canvas.width, canvas.height);
  _signHasStrokes = false;
}
async function submitSignature(){
  const el = document.getElementById('sign-msg');
  if(!_signHasStrokes){ el.textContent = 'Sign in the box first.'; el.style.color = '#c0392b'; return; }
  const btn = document.getElementById('sign-submit-btn');
  btn.disabled = true;
  el.textContent = 'Submitting…';
  el.style.color = '';
  const dataUrl = document.getElementById('sign-pad').toDataURL('image/png');
  try {
    const r = await fetch('/fos/' + LEG_ID + '/sign', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({signature: dataUrl})});
    const data = await r.json();
    btn.disabled = false;
    if(!r.ok){ el.textContent = data.error || 'Sign failed'; el.style.color = '#c0392b'; return; }
    const check = document.getElementById('sign-check');
    if(check) check.classList.add('signed');
    showToast('Signed');
    showView('documents');
  } catch(e) {
    btn.disabled = false;
    el.textContent = 'Request failed: ' + e;
    el.style.color = '#c0392b';
  }
}

const LEG_SEQ = "$seq";
const LEG_POSITION = "$position";
if(!LEG_SEQ){
  const btn = document.getElementById('pairing-btn');
  if(btn) btn.style.display = 'none';
}
async function initPairingView(){
  const body = document.getElementById('pairing-body');
  if(!LEG_SEQ){
    body.innerHTML = '<p class="placeholder-note">This leg has no SEQ — it wasn’t generated from a PBS pairing.</p>';
    return;
  }
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(LEG_SEQ));
    const data = await r.json();
    if(!r.ok){
      body.innerHTML = '<p class="placeholder-note">No pairing data for SEQ ' + LEG_SEQ + ' — re-import the PBS bid pack on Home.</p>';
      return;
    }
    renderPairing(data);
  } catch(e) {
    body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>';
  }
}
function renderPairing(seqData){
  const body = document.getElementById('pairing-body');
  body.innerHTML = '';
  const position = LEG_POSITION || (seqData.positions && seqData.positions[0]) || '';

  const dutyDays = seqData.duty_days || [];
  const totalLegs = dutyDays.reduce((n, d) => n + (d.legs || []).length, 0);
  const firstLeg = dutyDays[0] && dutyDays[0].legs && dutyDays[0].legs[0];
  const lastDay = dutyDays[dutyDays.length - 1];
  const lastLeg = lastDay && lastDay.legs && lastDay.legs[lastDay.legs.length - 1];
  const summary = document.createElement('div');
  summary.className = 'flight-summary';
  summary.style.flexWrap = 'wrap';
  const summaryBits = [
    dutyDays.length + ' day' + (dutyDays.length === 1 ? '' : 's'),
    totalLegs + ' leg' + (totalLegs === 1 ? '' : 's'),
    (seqData.positions || []).join('/'),
  ];
  if(firstLeg && lastLeg) summaryBits.unshift(firstLeg.origin + ' → ' + lastLeg.destination);
  summary.textContent = summaryBits.filter(Boolean).join('  ·  ');
  body.appendChild(summary);

  const cacheWrap = document.createElement('div');
  cacheWrap.style.cssText = 'padding:11px 14px;background:var(--card);border-bottom:1px solid var(--border);';
  const cacheBtn = document.createElement('button');
  cacheBtn.textContent = 'Generate & Cache All Legs in Sequence';
  cacheBtn.style.cssText = 'margin:0;width:100%;background:var(--green);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
  cacheBtn.onclick = () => cacheAllPairingLegs(seqData, position, cacheMsg);
  const cacheMsg = document.createElement('div');
  cacheMsg.style.cssText = 'margin-top:8px;font-size:12.5px;color:var(--label);';
  cacheWrap.appendChild(cacheBtn);
  cacheWrap.appendChild(cacheMsg);
  body.appendChild(cacheWrap);

  (seqData.duty_days || []).forEach(day => {
    const bar = document.createElement('div');
    bar.className = 'section-bar';
    bar.style.cursor = 'default';
    bar.textContent = 'Day ' + day.duty_day + ' — RPT ' + (day.report || '');
    body.appendChild(bar);

    const list = document.createElement('div');
    list.className = 'doc-list';
    (day.legs || []).forEach((leg, i) => {
      const row = document.createElement('div');
      row.className = 'doc-row';
      row.style.cursor = 'pointer';
      const left = document.createElement('div');
      const code = document.createElement('div');
      code.className = 'code';
      code.textContent = leg.flight_number || '—';
      const desc = document.createElement('div');
      desc.className = 'desc';
      desc.textContent = leg.origin + '→' + leg.destination + ' ' + leg.dep_local + '/' + leg.arr_local;
      left.appendChild(code);
      left.appendChild(desc);
      const actions = document.createElement('div');
      actions.className = 'actions';
      const sbIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      sbIcon.setAttribute('viewBox', '0 0 24 24');
      sbIcon.setAttribute('fill', 'none');
      sbIcon.setAttribute('stroke', 'currentColor');
      sbIcon.setAttribute('stroke-width', '2');
      sbIcon.setAttribute('stroke-linecap', 'round');
      sbIcon.setAttribute('stroke-linejoin', 'round');
      sbIcon.setAttribute('title', 'Generate via SimBrief');
      sbIcon.innerHTML = '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>';
      sbIcon.onclick = (e) => { e.stopPropagation(); generatePairingLeg(seqData.seq, day.duty_day, i, position, 'release'); };
      actions.appendChild(sbIcon);
      actions.insertAdjacentHTML('beforeend', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>');
      row.appendChild(left);
      row.appendChild(actions);
      row.onclick = () => generatePairingLeg(seqData.seq, day.duty_day, i, position);
      list.appendChild(row);
    });
    body.appendChild(list);

    const bits = [
      day.release ? ('RLS ' + day.release) : '',
      day.duty ? ('Duty ' + day.duty) : '',
      day.tafb ? ('TAFB ' + day.tafb) : '',
      day.hotel || '',
    ].filter(Boolean).join(' · ');
    if(bits){
      const note = document.createElement('p');
      note.className = 'placeholder-note';
      note.textContent = bits;
      body.appendChild(note);
    }
  });
  if(!body.children.length){
    body.innerHTML = '<p class="placeholder-note">No duty days on this sequence.</p>';
  }
}
async function generatePairingLeg(seq, dutyDay, legIndex, position, view){
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: dutyDay, leg_index: legIndex, position: position}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Generate failed'); return; }
    window.location.href = data.fos_url + (view ? '?view=' + view : '');
  } catch(e) { showToast('Request failed: ' + e); }
}
async function cacheAllPairingLegs(seqData, position, msgEl){
  let total = 0, ok = 0;
  const fails = [];
  for(const day of (seqData.duty_days || [])){
    for(let i = 0; i < day.legs.length; i++){
      total++;
      try {
        const r = await fetch('/pbs/sequences/' + encodeURIComponent(seqData.seq) + '/generate', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({duty_day: day.duty_day, leg_index: i, position: position}),
        });
        const data = await r.json();
        if(r.ok) ok++; else fails.push(`Day ${day.duty_day} leg ${i+1}: ${data.error}`);
      } catch(e) { fails.push(`Day ${day.duty_day} leg ${i+1}: ${e}`); }
      msgEl.textContent = `Caching… ${ok}/${total} done`;
      msgEl.style.color = '';
    }
  }
  msgEl.textContent = `Cached ${ok} of ${total} legs.` + (fails.length ? ' Failures: ' + fails.join('; ') : '');
  msgEl.style.color = fails.length ? '#c0392b' : '#2fa355';
}

function todayZuluDDMMMYY(){
  const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  const d = new Date();
  return String(d.getUTCDate()).padStart(2, '0') + months[d.getUTCMonth()] + String(d.getUTCFullYear()).slice(-2);
}

async function submitSimbriefGen(){
  const el = document.getElementById('sbgen-msg');
  const btn = document.getElementById('sbgen-btn');
  const user = document.getElementById('release-user').value.trim();
  const type = document.getElementById('sbgen-type').value.trim().toLowerCase();
  const orig = document.getElementById('sbgen-orig').value.trim().toUpperCase();
  const dest = document.getElementById('sbgen-dest').value.trim().toUpperCase();
  const route = document.getElementById('sbgen-route').value.trim();
  const airline = document.getElementById('sbgen-airline').value.trim().toUpperCase();
  const fltnum = document.getElementById('sbgen-fltnum').value.trim();
  const date = document.getElementById('sbgen-date').value.trim().toUpperCase();
  const time = document.getElementById('sbgen-time').value.trim();
  const reg = document.getElementById('sbgen-reg').value.trim().toUpperCase();

  if(!user){ el.textContent = 'Enter your SimBrief username first.'; el.style.color = '#c0392b'; return; }
  if(!orig || !dest){ el.textContent = 'Origin and destination are required.'; el.style.color = '#c0392b'; return; }
  if(!type){ el.textContent = 'Enter the SimBrief aircraft type code first.'; el.style.color = '#c0392b'; return; }
  localStorage.setItem('fos_simbrief_user', user);
  localStorage.setItem('fos_simbrief_airframe', type);

  // Everything not listed here (fuel, alternates, crew, output options...)
  // is set by the pilot on SimBrief's own dispatch page instead of
  // reimplemented as a control here — we only pre-fill what we actually
  // know from the pairing/leg data.
  const params = new URLSearchParams();
  const set = (k, v) => { if(v) params.set(k, v); };
  set('orig', orig); set('dest', dest); set('type', type); set('route', route);
  set('airline', airline); set('fltnum', fltnum); set('date', date); set('reg', reg);
  if(time && time.length === 4){ set('deph', time.slice(0, 2)); set('depm', time.slice(2, 4)); }
  const url = 'https://dispatch.simbrief.com/options/custom?' + params.toString();

  // Popup must open synchronously, before any await — Safari (and others)
  // stop treating window.open as user-initiated once you're a tick removed
  // from the actual click via an awaited fetch, and silently block it. We
  // have everything we need synchronously now (no signed request to fetch
  // first), so open straight to the real URL.
  const popup = window.open(url, 'SBworker', 'width=900,height=800');
  if(!popup){
    el.textContent = 'Please allow pop-ups for this site, then try again.';
    el.style.color = '#c0392b';
    return;
  }

  btn.disabled = true;
  el.textContent = 'Complete the flight plan on SimBrief’s dispatch page — this tab will pick it up once you’re done.';
  el.style.color = '';

  // No signed request means no deterministic ofp_id to poll for — instead,
  // snapshot the account's current OFP generation timestamp now, and after
  // the tab closes, wait for that timestamp to change before treating a
  // plan as "new" (rather than re-pulling whatever was already there).
  let beforeTs = '';
  try {
    const r = await fetch('/simbrief-api/generated-at?user=' + encodeURIComponent(user));
    beforeTs = (await r.json()).time_generated || '';
  } catch(e) { /* best-effort */ }

  const closeWatcher = setInterval(() => {
    if(!popup.closed) return;
    clearInterval(closeWatcher);
    el.textContent = 'Checking for the generated flight plan…';
    pollSimbriefReady(user, beforeTs, el, btn, 0);
  }, 500);
}

async function pollSimbriefReady(user, beforeTs, el, btn, attempt){
  if(attempt > 40){
    el.textContent = 'No new flight plan detected — if you generated one, try Load from SimBrief manually.';
    el.style.color = '#c0392b';
    btn.disabled = false;
    return;
  }
  let ts = '';
  try {
    const r = await fetch('/simbrief-api/generated-at?user=' + encodeURIComponent(user));
    ts = (await r.json()).time_generated || '';
  } catch(e) { /* keep polling */ }

  if(ts && ts !== beforeTs){
    el.textContent = 'Flight plan ready — loading it…';
    el.style.color = '#2fa355';
    try {
      const r2 = await fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})});
      const data2 = await r2.json();
      if(!r2.ok){ el.textContent = data2.error || 'Generated, but could not load it into FOS'; el.style.color = '#c0392b'; btn.disabled = false; return; }
      window.location.href = data2.fos_url + '?view=confirm';
    } catch(e) {
      el.textContent = 'Generated, but loading it failed: ' + e;
      el.style.color = '#c0392b';
      btn.disabled = false;
    }
    return;
  }
  setTimeout(() => pollSimbriefReady(user, beforeTs, el, btn, attempt + 1), 3000);
}

let _weatherLoaded = false;
function toggleWeatherSection(){
  const bar = document.getElementById('weather-bar');
  const body = document.getElementById('weather-body');
  const collapsed = bar.classList.toggle('collapsed');
  body.style.display = collapsed ? 'none' : 'block';
  if(!collapsed && !_weatherLoaded) loadWeather();
}
async function loadWeather(){
  const body = document.getElementById('weather-body');
  const userId = localStorage.getItem('fos_simbrief_user');
  if(!userId){
    body.innerHTML = '<p class="placeholder-note">Set a SimBrief username on the Release tab first.</p>';
    return;
  }
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/weather', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id:userId})});
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load weather') + '</p>'; return; }
    _weatherLoaded = true;
    renderWeather(data);
  } catch(e) {
    body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>';
  }
}
function renderWeather(stations){
  const body = document.getElementById('weather-body');
  body.innerHTML = '';
  const refresh = document.createElement('a');
  refresh.href = '#';
  refresh.textContent = 'Refresh';
  refresh.style.cssText = 'display:block;padding:8px 14px;font-size:12px;color:var(--blue-dark);text-decoration:none;background:var(--card);border-bottom:1px solid var(--border);';
  refresh.onclick = (e) => { e.preventDefault(); loadWeather(); };
  body.appendChild(refresh);
  if(!stations.length){
    const note = document.createElement('p');
    note.className = 'placeholder-note';
    note.textContent = 'No weather data on the current OFP.';
    body.appendChild(note);
    return;
  }
  const roleLabel = {origin: 'Origin', destination: 'Destination', alternate: 'Alternate'};
  const catColor = {VFR: '#2fa355', MVFR: '#1c63b7', IFR: '#e0393e', LIFR: '#8e2de2'};
  stations.forEach(s => {
    const card = document.createElement('div');
    card.style.cssText = 'padding:11px 14px;border-bottom:1px solid var(--border);background:var(--card);';
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;';
    const title = document.createElement('div');
    title.style.cssText = 'font-weight:700;font-size:13px;';
    title.textContent = (roleLabel[s.role] || s.role) + ' — ' + s.icao + (s.name ? ' (' + s.name + ')' : '');
    header.appendChild(title);
    if(s.category){
      const badge = document.createElement('span');
      badge.textContent = s.category;
      badge.style.cssText = 'font-size:10.5px;font-weight:700;color:#fff;padding:2px 7px;border-radius:10px;background:' + (catColor[s.category] || 'var(--label)') + ';flex:0 0 auto;';
      header.appendChild(badge);
    }
    card.appendChild(header);
    if(s.visibility || s.ceiling){
      const meta = document.createElement('div');
      meta.style.cssText = 'font-size:11.5px;color:var(--label);margin-bottom:6px;';
      const bits = [];
      if(s.visibility) bits.push('Vis ' + (s.visibility >= 9999 ? '10+SM' : s.visibility + 'm'));
      if(s.ceiling && s.ceiling < 9999) bits.push('Ceiling ' + s.ceiling + 'ft');
      meta.textContent = bits.join(' · ');
      if(bits.length) card.appendChild(meta);
    }
    if(s.metar){
      const m = document.createElement('div');
      m.style.cssText = 'font-family:ui-monospace,Menlo,monospace;font-size:11.5px;white-space:pre-wrap;color:var(--value);margin-bottom:6px;';
      m.textContent = s.metar;
      card.appendChild(m);
    }
    if(s.taf){
      const t = document.createElement('div');
      t.style.cssText = 'font-family:ui-monospace,Menlo,monospace;font-size:11.5px;white-space:pre-wrap;color:var(--label);';
      t.textContent = s.taf;
      card.appendChild(t);
    }
    body.appendChild(card);
  });
}

(function(){
  const params = new URLSearchParams(window.location.search);
  const view = params.get('view');
  if(view === 'pairing' || view === 'release' || view === 'confirm') showView(view);
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
