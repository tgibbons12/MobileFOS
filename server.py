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
import html
import io
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import airportsdata
import requests
from flask import Flask, request, jsonify, Response, redirect, url_for
from flask_login import LoginManager, current_user, login_user, logout_user
from string import Template
import aeroapi
import pbs_parser
import release_engine
from fos_pages import AIRLINE_IATA, synthesize_crew
from models import db, User, Leg, PbsImport, SignatureLog, ReleaseCache
import simbrief_ofp

# ICAO -> IANA timezone name (e.g. "America/Phoenix"), used to convert a PBS
# leg's bare local departure time to zulu before sending it to SimBrief — a
# small bundled dataset (~1MB), safe to load eagerly unlike the OurAirports
# CSV lookup below.
_AIRPORT_TZ = airportsdata.load("ICAO")

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


# PBS's equipment codes are keyed by exact sub-fleet code, not a shared
# prefix — a prefix scheme can't work here since it collides on real codes
# (21A/21C/21D/21S are A321ceo but 21Q is A321neo; 73G is a 737-700 but
# 73S/73W are 737-800; 39E is a 767 but 39N is an A330; E70 is an E170 but
# E7L/E7S/E7W are E175s; E90 is an E190 but E95 is an E195). Table below is
# straight off the operator's own OPERATOR/FLEET sub-fleet reference
# (equipment code -> aircraft), not guessed. Extend as more codes get
# confirmed; anything unmapped (including already-ICAO codes like "B738"
# from a SimBrief-loaded leg) passes through unchanged.
_FLEET_TYPE_ICAO = {
    "19E": "A319", "19F": "A319", "19S": "A319",
    "1NX": "A21N",
    "20N": "A20N",
    "21A": "A321", "21C": "A321", "21D": "A321", "21S": "A321",
    "21Q": "A21N",
    "2NX": "A21N", "2NY": "A21N",
    "32A": "A320", "32B": "A320", "32C": "A320", "32S": "A320",
    "32N": "A20N",
    "39E": "B739",
    "39N": "A339",
    "738": "B738",
    "73G": "B737",
    "73S": "B738", "73W": "B738",
    "75E": "B753",
    "75F": "B752", "75P": "B752", "75S": "B752",
    "76F": "B763", "76J": "B763", "76T": "B763",
    "77E": "B772",
    "7M8": "B38M",
    "DH4": "DH8D",
    "E70": "E170",
    "E7L": "E175", "E7S": "E175", "E7W": "E175",
    "E90": "E190",
    "E95": "E195",
}


def _fleet_type_icao(code):
    code = (code or "").strip().upper()
    return _FLEET_TYPE_ICAO.get(code, code)


def _station_time_html(sched, est):
    """Overview's flight card, mobileCCI-style: the current (estimated)
    time in front, the original scheduled time struck through next to it
    — but only when they actually differ, rather than always showing two
    identical numbers. Color is a real signal, not decoration: sched_out/
    sched_in come from the PBS pairing, est_out/est_in from the SimBrief
    OFP fetch — both "%H:%M" 24h strings already on the leg schema, so a
    later estimate than scheduled (red) vs on-time/earlier/no-estimate-yet
    (green) costs nothing to compute, it's just a comparison this app
    wasn't running before."""
    sched, est = (sched or "").strip(), (est or "").strip()
    shown = est or sched
    if not shown:
        return ""
    late = bool(sched and est and est > sched)
    css_class = "est-time late" if late else "est-time"
    html_out = f'<span class="{css_class}">{html.escape(shown)}</span>'
    if sched and est and sched != est:
        html_out += f'<span class="sched-time">{html.escape(sched)}</span>'
    return html_out


def _js_str(s):
    """Escape for embedding inside a double-quoted JS string literal, for
    the "$var" pattern these templates' <script> blocks use throughout
    (e.g. const LEG_ID = "$leg_id";). html.escape() is the wrong escaping
    for this context — it's for HTML attributes/text, not JS string
    content — and would corrupt (not just fail to protect) any value
    containing a literal quote or backslash."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")

app = Flask(__name__)
LOG = logging.getLogger(__name__)

# Railway's Postgres plugin injects DATABASE_URL as "postgres://", which
# SQLAlchemy 1.4+/psycopg2 reject — they want "postgresql://". Falls back to
# a local SQLite file when DATABASE_URL isn't set at all, so local dev never
# needs a real Postgres instance running.
_db_url = os.environ.get("DATABASE_URL", "sqlite:///fos.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    app.secret_key = "dev-insecure-secret-change-me"
    LOG.warning("SECRET_KEY not set — using an insecure dev default. Set a real SECRET_KEY before deploying.")
db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def _load_user(user_id):
    return db.session.get(User, int(user_id))


def _ensure_columns():
    """Bare-bones migration for the columns this app has added since its
    first deploy — db.create_all() only creates tables that don't exist
    yet, it never alters an existing one, so a new model column needs this
    to actually show up on a database that's already running. Fine for this
    app's pace of schema change; a real migrations tool (Alembic) would
    replace this if that ever stops being true."""
    from sqlalchemy import inspect, text as sa_text
    inspector = inspect(db.engine)
    existing = {c["name"] for c in inspector.get_columns("users")}
    if "aeroapi_key" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN aeroapi_key VARCHAR(255)"))
        LOG.info("Migrated: added users.aeroapi_key")


with app.app_context():
    db.create_all()
    _ensure_columns()


# ---------------------------------------------------------------------------
# Auth — this is a personal/crew tool, not a public site, so the whole app
# sits behind a login rather than decorating each of the ~18 routes below.
# ---------------------------------------------------------------------------
_PUBLIC_ENDPOINTS = {"login", "register", "health", "static"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if current_user.is_authenticated:
        return None
    # A redirect would hand back an HTML login page to a fetch() call that
    # expects JSON — only the full-page GETs (opening a URL directly) should
    # redirect; POST/etc (all the in-app API calls) get a plain 401 instead.
    if request.method == "GET":
        return redirect(url_for("login", next=request.path))
    return jsonify({"error": "login required"}), 401


AUTH_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#142c52">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="mobileFOS">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<title>$title – FOS</title>
<style>
  html,body{overscroll-behavior:none;background:#eef1f4;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px;color:#1a1f29;}
  h1{font-size:18px;color:#144e94;margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;}
  input[type=text],input[type=password]{width:100%;max-width:320px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid #e3e6ea;border-radius:5px;box-sizing:border-box;background:#fbfbfc;}
  button{margin-top:16px;background:#1c63b7;color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  .panel{max-width:320px;padding:14px;background:#fff;border:1px solid #e3e6ea;border-radius:6px;margin-top:10px;}
  .msg{margin-top:8px;font-size:13px;color:#c0392b;}
  .switch{margin-top:14px;font-size:13px;}
  .switch a{color:#1c63b7;}
</style></head><body>
<h1>$title</h1>
<div class="panel">
  <form method="POST">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="$autocomplete" required>
    <br><button type="submit">$button_label</button>
  </form>
  $error_html
</div>
<div class="switch">$switch_html</div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong username or password."
    error_html = f'<div class="msg">{html.escape(error)}</div>' if error else ""
    return Response(Template(AUTH_TEMPLATE).safe_substitute(
        title="Sign In", autocomplete="current-password", button_label="Sign In",
        error_html=error_html, switch_html='New here? <a href="/register">Create an account</a>',
    ), mimetype="text/html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            error = "Username and password are both required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif User.query.filter_by(username=username).first():
            error = "That username is taken."
        else:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("index"))
    error_html = f'<div class="msg">{html.escape(error)}</div>' if error else ""
    return Response(Template(AUTH_TEMPLATE).safe_substitute(
        title="Create Account", autocomplete="new-password", button_label="Create Account",
        error_html=error_html, switch_html='Already have an account? <a href="/login">Sign in</a>',
    ), mimetype="text/html")


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("login"))

DEFAULT_LEG = {
    "seq": "", "date": "", "base": "", "flight_number": "", "origin": "", "destination": "",
    "route": "", "dep_date": "", "arr_date": "", "sched_out": "", "sched_in": "",
    "est_out": "", "est_in": "", "dep_gate": "", "arr_gate": "",
    "fleet_type": "", "equipment_type": "", "tail_number": "", "tail_routing": "",
    "aircraft_name": "", "fin": "", "engines": "", "seat_capacity": "",
    "bookmarked_docs": [],
    "status": "", "customer_load": "", "position": "", "crew": [],
    "flight_time": "", "odl_time": "", "duty_time": "", "ground_time": "",
    "tz_diff": "", "hotel_details": "", "limo_details": "",
    "signed_in": False, "fit_for_duty": False,
    "signature": "", "signed_at": "",
    "block_fuel": "", "takeoff_fuel": "", "landing_fuel": "", "trip_fuel": "",
    "taxi_fuel": "", "reserve_fuel": "", "alternate_fuel": "", "contingency_fuel": "", "extra_fuel": "",
}

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
    merged = {**ofp_fields, **known}
    # Loading a real OFP — whether via Import from SimBrief or the
    # generate-then-send round-trip — means the pilot is actively working
    # this flight right now; Signed In (no longer a manual tap) should
    # reflect that instead of starting every SimBrief-sourced leg blank.
    merged["signed_in"] = True
    return merged


def _find(flight_number, dep_date):
    """The ORM row (not a plain dict) — only consumer is _store_leg, which
    needs to mutate/insert it."""
    return Leg.query.filter_by(
        user_id=current_user.id, flight_number=flight_number, dep_date=dep_date,
    ).first()


def _get_leg_row(leg_id):
    return Leg.query.filter_by(id=leg_id, user_id=current_user.id).first()


def _get_leg(leg_id):
    """Plain dict (with "id") for a read-only route — see _get_leg_row for
    routes that need to mutate it."""
    row = _get_leg_row(leg_id)
    return {**row.data, "id": row.id} if row else None


def _save_leg(row, data):
    """Persist a leg dict back onto its row. JSON columns only pick up
    reassignment, not in-place mutation — always hand this a fresh dict
    built with {**row.data, ...}, never row.data[...] = ... directly."""
    data = dict(data)
    data.pop("id", None)
    row.data = data
    db.session.commit()
    return {**row.data, "id": row.id}


def _pbs_row():
    return PbsImport.query.filter_by(user_id=current_user.id).first()


def _pbs_sequences():
    row = _pbs_row()
    return row.sequences if row and row.sequences else []


def _pbs_meta():
    row = _pbs_row()
    return (row.meta if row else None) or {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _store_leg(leg):
    """Shared by /generate and the PBS import path: dedupe-or-insert + archive."""
    leg = {**DEFAULT_LEG, **leg}
    leg.pop("id", None)
    existing = _find(leg.get("flight_number"), leg.get("dep_date"))
    if existing:
        # OR, not overwrite: a regenerate (e.g. "Request New Data" re-sending
        # to SimBrief) must never flip a real attestation back off, but it
        # also shouldn't swallow build_leg_from_sources() setting signed_in
        # True on fresh OFP data — plain overwrite-from-old was doing exactly
        # that, silently discarding the auto-sign-in this same session added.
        signed_in = existing.data.get("signed_in") or leg.get("signed_in", False)
        ffd = existing.data.get("fit_for_duty") or leg.get("fit_for_duty", False)
        merged = {**existing.data, **leg, "signed_in": signed_in, "fit_for_duty": ffd}
        existing.data = merged
        existing.flight_number = merged.get("flight_number")
        existing.dep_date = merged.get("dep_date")
        row = existing
        # PBS-sourced legs always start with dep_date="" (a bid pack is a
        # schedule pattern, never a real calendar date — see
        # pbs_leg_to_fos_leg), so two genuinely different pairings sharing a
        # flight number dedupe onto this same row via _find() above. Its
        # cached release (keyed only on leg id) belongs to whatever content
        # was here before this overwrite — stale now, not just for this
        # flight_number/dep_date collision case but any time a leg's
        # content changes under an existing id.
        cached = ReleaseCache.query.filter_by(leg_id=row.id).first()
        if cached:
            db.session.delete(cached)
    else:
        row = Leg(
            user_id=current_user.id, flight_number=leg.get("flight_number"),
            dep_date=leg.get("dep_date"), data=leg,
        )
        db.session.add(row)
        db.session.flush()  # populate row.id before we use it below
    current_user.current_leg_id = row.id
    db.session.commit()
    return {**row.data, "id": row.id}


@app.route("/generate", methods=["POST"])
def generate():
    payload = request.get_json(silent=True) or {}
    # A SimBrief-import generate (simbrief_user set, nothing else) almost
    # always lands on a fresh record — the OFP's real dep_date practically
    # never matches the "" dep_date a PBS/manual leg started with, so
    # _find() below can't dedupe them into one. Gates applied via AeroAPI
    # before sending to SimBrief live only on that older record and would
    # otherwise be silently orphaned; carry them forward explicitly since
    # a SimBrief OFP never carries gate data of its own to conflict with.
    carry_from = payload.pop("carry_gates_from", None)
    leg = build_leg_from_sources(payload)
    ofp_error = leg.pop("_ofp_error", None)
    if ofp_error and not leg.get("flight_number"):
        return jsonify({"error": f"couldn't load SimBrief OFP: {ofp_error}"}), 502
    if carry_from:
        try:
            src_row = _get_leg_row(int(carry_from))
        except (TypeError, ValueError):
            src_row = None
        if src_row:
            for key in ("dep_gate", "arr_gate"):
                if src_row.data.get(key) and not leg.get(key):
                    leg[key] = src_row.data[key]
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
    row = _pbs_row()
    if not row:
        row = PbsImport(user_id=current_user.id)
        db.session.add(row)
    row.meta = meta
    row.sequences = sequences
    db.session.commit()
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
    for s in _pbs_sequences():
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


@app.route("/pbs/sequences", methods=["DELETE"])
def clear_pbs_sequences():
    """Clears this pilot's whole PBS import — the "start over" option
    alongside deleting one sequence at a time below."""
    row = _pbs_row()
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/pbs/sequences/<seq_number>", methods=["DELETE"])
def delete_pbs_sequence(seq_number):
    row = _pbs_row()
    if not row:
        return jsonify({"error": "not found"}), 404
    remaining = [s for s in (row.sequences or []) if s["seq"] != seq_number]
    if len(remaining) == len(row.sequences or []):
        return jsonify({"error": "not found"}), 404
    row.sequences = remaining
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/pbs/sequences/<seq_number>")
def get_pbs_sequence(seq_number):
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404
    out = dict(seq)
    operator_iata = _pbs_meta().get("operator", "").upper()
    out["operator"] = _IATA_TO_ICAO.get(operator_iata, operator_iata)
    out["duty_days"] = [
        {**day, "legs": [
            {
                **leg,
                "origin_icao": _airport_icao(leg["origin"]),
                "destination_icao": _airport_icao(leg["destination"]),
                "fleet_type_icao": _fleet_type_icao(leg.get("equipment", "")),
            }
            for leg in day["legs"]
        ]}
        for day in seq["duty_days"]
    ]
    return jsonify(out)


@app.route("/pbs/sequences/<seq_number>/generate", methods=["POST"])
def generate_from_pbs(seq_number):
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
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

    fos_leg = pbs_parser.pbs_leg_to_fos_leg(_pbs_meta(), seq, day, leg, position)
    if body.get("simbrief_user"):
        fos_leg["simbrief_user"] = body["simbrief_user"]
    fos_leg = build_leg_from_sources(fos_leg)
    fos_leg.pop("_ofp_error", None)  # non-fatal here — real pairing data already exists
    record = _store_leg(fos_leg)
    return jsonify({"fos_url": f"/fos/{record['id']}", "id": record["id"]})


@app.route("/fos/<int:leg_id>")
def view_fos(leg_id):
    record = _get_leg(leg_id)
    if not record:
        return Response(
            f"No leg with id {leg_id}. POST a leg to /generate first.",
            status=404, mimetype="text/plain",
        )
    # Viewing a leg (not just generating one) makes it "current" too —
    # matches the old fos_last_leg-on-every-page-load localStorage behavior.
    current_user.current_leg_id = leg_id
    db.session.commit()
    return Response(render_fos_html(record), mimetype="text/html")


@app.route("/fos/<int:leg_id>/signin", methods=["POST"])
def toggle_signin(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    data = _save_leg(row, {**row.data, "signed_in": not row.data.get("signed_in", False)})
    return jsonify({"signed_in": data["signed_in"]})


@app.route("/fos/<int:leg_id>/fit-for-duty", methods=["POST"])
def toggle_ffd(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    data = _save_leg(row, {**row.data, "fit_for_duty": not row.data.get("fit_for_duty", False)})
    return jsonify({"fit_for_duty": data["fit_for_duty"]})


@app.route("/fos/<int:leg_id>/gates", methods=["POST"])
def set_gates(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    changes = {}
    if "dep_gate" in body:
        changes["dep_gate"] = body["dep_gate"]
    if "arr_gate" in body:
        changes["arr_gate"] = body["arr_gate"]
    data = _save_leg(row, {**row.data, **changes})
    return jsonify({"dep_gate": data.get("dep_gate", ""), "arr_gate": data.get("arr_gate", "")})


@app.route("/fos/<int:leg_id>/bookmark", methods=["POST"])
def toggle_bookmark(leg_id):
    """Preflight Docs' bookmark toggle — persisted per leg in Leg.data
    (no dedicated table; this app already keeps a leg's whole state in
    that JSON blob, same as signed_in/fit_for_duty above)."""
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    doc = (body.get("doc") or "").strip()
    if not doc:
        return jsonify({"error": "doc code required"}), 400
    bookmarked = list(row.data.get("bookmarked_docs") or [])
    if doc in bookmarked:
        bookmarked.remove(doc)
    else:
        bookmarked.append(doc)
    data = _save_leg(row, {**row.data, "bookmarked_docs": bookmarked})
    return jsonify({"bookmarked_docs": data.get("bookmarked_docs", [])})


@app.route("/fos/<int:leg_id>/prefile")
def get_prefile_links(leg_id):
    """VATSIM/IVAO prefile forms straight from the pilot's current SimBrief
    OFP (see simbrief_ofp.fetch_prefile_links) — fetched fresh on demand
    rather than cached on the leg, since a prefile is tied to whatever OFP
    is live on the account right now, not to whenever this leg was last
    dispatched."""
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    simbrief_user = current_user.default_simbrief_user
    if not simbrief_user:
        return jsonify({"error": "Set your SimBrief username in Settings first"}), 400
    try:
        links = simbrief_ofp.fetch_prefile_links(simbrief_user)
    except (requests.RequestException, ET.ParseError) as e:
        return jsonify({"error": f"Could not fetch SimBrief OFP: {e}"}), 502
    return jsonify(links)


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
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(silent=True) or {}
    signature = body.get("signature")
    if not signature or not signature.startswith("data:image/"):
        return jsonify({"error": "no signature image given"}), 400

    signed_at = datetime.now(timezone.utc).isoformat()
    data = _save_leg(row, {**row.data, "signature": signature, "signed_at": signed_at})

    db.session.add(SignatureLog(
        user_id=current_user.id, leg_id=leg_id,
        flight_number=data.get("flight_number"), dep_date=data.get("dep_date"),
        signed_at=signed_at,
    ))
    db.session.commit()
    LOG.info(f"SIGNATURE leg={leg_id} flight={data.get('flight_number')} at={signed_at}")

    return jsonify({"signed_at": signed_at})


@app.route("/signatures")
def list_signatures():
    """Audit log of every signing event this pilot has seen — separate
    from the leg records themselves, so it survives a leg being regenerated
    (re-signing overwrites the leg's own signature field but not this
    history)."""
    rows = SignatureLog.query.filter_by(user_id=current_user.id).order_by(SignatureLog.id.desc()).all()
    return jsonify([
        {"leg_id": r.leg_id, "flight_number": r.flight_number, "dep_date": r.dep_date, "signed_at": r.signed_at}
        for r in rows
    ])


@app.route("/fos/<int:leg_id>/weather", methods=["POST"])
def leg_weather(leg_id):
    record = _get_leg(leg_id)
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
    """Generates (or, by far the common case, just returns) this leg's
    release PDFs. generate_release_pdfs() is a real SimBrief OFP fetch +
    PDF render that takes up to a minute — both the Confirm view's
    "Generate Release" button and the Documents PDF viewer hit this same
    route/cache now, instead of each independently re-running it on every
    click or page load. Pass {"force": true} to regenerate anyway (e.g.
    gates changed since the cached copy, or the weather's gone stale)."""
    record = _get_leg(leg_id)
    if not record:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(silent=True) or {}
    cached = ReleaseCache.query.filter_by(leg_id=leg_id).first()
    if cached and not body.get("force"):
        return jsonify({**cached.payload, "cached": True, "generated_at": cached.generated_at.isoformat()})

    if not release_engine.is_available():
        return jsonify({"error": release_engine.import_error()}), 503

    user_id = body.get("user_id") or os.environ.get("SIMBRIEF_USER")
    if not user_id:
        return jsonify({"error": "no SimBrief user id — pass \"user_id\" or set SIMBRIEF_USER"}), 400

    try:
        rls_bytes, wb_bytes, filename = release_engine.generate_release_pdfs(
            user_id, gate=record.get("dep_gate", ""), arr_gate=record.get("arr_gate", ""))
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
        LOG.warning(f"Named-page extraction failed: {e}")
        named_pages = {}
    for kind, field in (("fi", "fi_pdf_b64"), ("fil", "fil_pdf_b64"), ("weather", "weather_pdf_b64"),
                         ("notams", "notams_pdf_b64"), ("field_report", "field_report_pdf_b64")):
        if named_pages.get(kind):
            payload[field] = base64.b64encode(named_pages[kind]).decode("ascii")

    generated_at = datetime.now(timezone.utc)
    if cached:
        cached.filename, cached.payload, cached.generated_at = filename, payload, generated_at
    else:
        db.session.add(ReleaseCache(leg_id=leg_id, filename=filename, payload=payload, generated_at=generated_at))
    db.session.commit()

    return jsonify({**payload, "cached": False, "generated_at": generated_at.isoformat()})


def _archive_rows():
    """This pilot's legs, newest first, as plain dicts (each with "id")."""
    rows = Leg.query.filter_by(user_id=current_user.id).order_by(Leg.created_at.desc()).all()
    return [{**r.data, "id": r.id} for r in rows]


@app.route("/archive")
def archive():
    slim = [
        {
            "id": r["id"], "flight_number": r.get("flight_number"),
            "origin": r.get("origin"), "destination": r.get("destination"),
            "dep_date": r.get("dep_date"), "signed_in": r.get("signed_in"),
            "fit_for_duty": r.get("fit_for_duty"),
        }
        for r in _archive_rows()
    ]
    return jsonify(slim)


@app.route("/fos/<int:leg_id>", methods=["DELETE"])
def delete_leg(leg_id):
    """Removes one archived flight. Its cached release (tied 1:1 to this
    leg) goes with it; the signature log is a separate audit trail and is
    deliberately left alone — same reasoning as it already surviving a
    leg being regenerated."""
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    cached = ReleaseCache.query.filter_by(leg_id=leg_id).first()
    if cached:
        db.session.delete(cached)
    if current_user.current_leg_id == leg_id:
        current_user.current_leg_id = None
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/archive", methods=["DELETE"])
def clear_archive():
    """Removes every archived flight for this pilot."""
    ReleaseCache.query.filter(ReleaseCache.leg_id.in_(
        db.session.query(Leg.id).filter_by(user_id=current_user.id)
    )).delete(synchronize_session=False)
    Leg.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
    current_user.current_leg_id = None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/health")
def health():
    # Unauthenticated (Railway healthcheck) — a DB-wide count, not scoped to
    # any one pilot, just proves the process is up and the DB is reachable.
    return jsonify({"status": "ok", "archived_legs": Leg.query.count()})


@app.route("/settings/simbrief-user", methods=["POST"])
def set_default_simbrief_user():
    """Persists the pilot's SimBrief username on their account — the
    Settings view's release-user input is the only place this lives now,
    server-rendered from here on every page load."""
    body = request.get_json(silent=True) or {}
    current_user.default_simbrief_user = (body.get("simbrief_user") or "").strip() or None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/settings/aeroapi-key", methods=["POST"])
def set_aeroapi_key():
    """Persists the pilot's FlightAware AeroAPI key on their account, at
    their explicit request — see the note on models.User.aeroapi_key."""
    body = request.get_json(silent=True) or {}
    current_user.aeroapi_key = (body.get("aeroapi_key") or "").strip() or None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/airport-timezone/<icao>")
def airport_timezone(icao):
    """IANA timezone name for an ICAO airport code — lets the browser
    convert a PBS leg's bare local departure time to zulu (via
    Intl.DateTimeFormat, client-side) before sending it to SimBrief,
    without needing a timezone library over there."""
    airport = _AIRPORT_TZ.get((icao or "").strip().upper())
    if not airport or not airport.get("tz"):
        return jsonify({"error": "unknown airport/timezone"}), 404
    return jsonify({"tz": airport["tz"]})


@app.route("/")
def index():
    return Response(render_launcher_html(), mimetype="text/html")


# ---------------------------------------------------------------------------
# Rendering — string.Template so the CSS's { } never fights Python's
# ---------------------------------------------------------------------------
_HOME_TILE_ICONS = {
    "current_flight": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.8 19.2L16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-1 .1-1.3.5l-.7.9c-.4.4-.2 1.1.3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 2.8 5.8c.3.5.9.7 1.4.3l.9-.7c.3-.3.5-.8.4-1.2z"/></svg>',
    "request_data": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 3v6h-6"/></svg>',
}


def _home_tile(icon_key, title, sub, href, enabled):
    onclick = f"window.location.href='{href}'" if enabled else ""
    disabled = "" if enabled else " disabled"
    return (
        f'<button class="home-tile" onclick="{onclick}"{disabled}>'
        f'{_HOME_TILE_ICONS[icon_key]}'
        f'<div><div class="tile-title">{html.escape(title)}</div><div class="tile-sub">{html.escape(sub)}</div></div>'
        f'</button>'
    )


_TRASH_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>'


def render_launcher_html():
    archive_rows = _archive_rows()
    rows = "".join(
        f'<div class="arow"><a class="arow-link" href="/fos/{r["id"]}">{html.escape(r.get("flight_number") or "")} '
        f'{html.escape(r.get("origin") or "")}\u2192{html.escape(r.get("destination") or "")} '
        f'<span>{html.escape(r.get("dep_date") or "")}</span></a>'
        f'<button class="arow-del" title="Delete this flight" onclick="deleteLeg({r["id"]})">{_TRASH_ICON_SVG}</button></div>'
        for r in archive_rows
    ) or '<p class="empty">No legs generated yet.</p>'
    clear_flights_link = '<button class="clear-all-link" onclick="clearAllFlights()">Clear All</button>' if archive_rows else ''

    current_leg = _get_leg(current_user.current_leg_id) if current_user.current_leg_id else None
    if current_leg:
        desc = f'{current_leg.get("flight_number","")} {current_leg.get("origin","")}\u2192{current_leg.get("destination","")}'.strip()
        current_flight_button = _home_tile("current_flight", "Current Flight", desc, f'/fos/{current_leg["id"]}', True)
        request_data_button = _home_tile("request_data", "Request New Data", f'Re-send {desc} to SimBrief', f'/fos/{current_leg["id"]}?view=release', True)
    else:
        current_flight_button = _home_tile("current_flight", "Current Flight", "No active flight yet", "", False)
        request_data_button = _home_tile("request_data", "Request New Data", "No active flight yet", "", False)

    return Template(LAUNCHER_TEMPLATE).safe_substitute(
        rows=rows, current_flight_button=current_flight_button, request_data_button=request_data_button,
        username=html.escape(current_user.username), clear_flights_link=clear_flights_link,
        default_simbrief_user=_js_str(current_user.default_simbrief_user),
    )


def render_fos_html(leg):
    ctx = {**DEFAULT_LEG, **leg}
    ctx["customer_load"] = str(ctx.get("customer_load") or "")
    crew = ctx.get("crew")
    crew_list = crew if isinstance(crew, list) else ([crew] if crew else [])
    ctx["crew_display"] = ", ".join(crew_list)
    # DOM (domicile) is the pilot's actual crew base from the PBS bid-pack's
    # own title block (pbs_parser's "BASE <code> ..." line) — not the leg's
    # destination, which was wrong (a crew's domicile doesn't change leg to
    # leg). Falls back to destination only for a leg with no PBS origin at
    # all (e.g. a bare SimBrief-only leg never merged from a bid pack).
    roster = synthesize_crew(
        ctx.get("flight_number", ""), ctx.get("dep_date", ""),
        ctx.get("base") or ctx.get("destination", ""), crew_list,
    )
    # Overview's Crew accordion shows the full roster (CA/FO/PU/FA — real
    # SimBrief names when this leg's been dispatched) with DOM (crew
    # domicile/base) and employee number columns, same shape as the
    # printed NSC crew-list page.
    rows = []
    for member in roster:
        rows.append(
            f'<div class="stat-detail-row"><span class="lbl">{html.escape(member["seat"])} '
            f'{html.escape(member["name"])}</span>'
            f'<span class="val">DOM {html.escape(member["dom"])} &middot; EMP {html.escape(member["emp"])}</span></div>'
        )
    ctx["crew_rows"] = "".join(rows)
    ctx["crew_count"] = f"{len(roster)}/{len(roster)}"
    ctx["leg_id"] = str(leg.get("id", ""))
    ctx["origin_icao"] = _airport_icao(ctx.get("origin", ""))
    ctx["destination_icao"] = _airport_icao(ctx.get("destination", ""))
    ctx["dep_time_html"] = _station_time_html(ctx.get("sched_out"), ctx.get("est_out"))
    ctx["arr_time_html"] = _station_time_html(ctx.get("sched_in"), ctx.get("est_in"))
    ctx["fleet_type_icao"] = _fleet_type_icao(ctx.get("fleet_type", ""))
    # Overview shows two parallel readings of the same aircraft: Fleet Type
    # stays the raw PBS sub-fleet code (e.g. "32A"), Equipment Type is the
    # decoded ICAO type (e.g. "A320") — previously equipment_type held the
    # bid pack's own coarse "OPERATOR / FLEET" family string instead, which
    # is what was showing the internal code here rather than a real type.
    ctx["equipment_type"] = ctx["fleet_type_icao"] or ctx.get("equipment_type", "")
    # Overview's Aircraft stat row wants one glanceable value — the real
    # tail number if this leg's actually been dispatched (SimBrief-sourced),
    # falling back to the decoded type when only a PBS pairing exists yet.
    ctx["aircraft_display"] = ctx.get("fin") or ctx.get("tail_number") or ctx["equipment_type"] or "—"
    ctx["aircraft_name_html"] = html.escape(ctx.get("aircraft_name") or "") or "—"
    ctx["fin_html"] = html.escape(ctx.get("fin") or "") or "—"
    ctx["tail_number_html"] = html.escape(ctx.get("tail_number") or "") or "—"
    ctx["engines_html"] = html.escape(ctx.get("engines") or "") or "—"
    raw_seat_capacity = ctx.get("seat_capacity") or ""
    ctx["pax_display"] = (
        f'{ctx["customer_load"]} / {raw_seat_capacity}'
        if ctx["customer_load"] and raw_seat_capacity
        else (ctx["customer_load"] or "—")
    )
    ctx["seat_capacity"] = raw_seat_capacity or "—"
    bookmarked = ctx.get("bookmarked_docs") or []
    bookmarked = bookmarked if isinstance(bookmarked, list) else []
    ctx["saved_docs_count"] = str(len(bookmarked))
    ctx["bookmarked_docs_json"] = json.dumps(bookmarked)
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
    # Same fit_for_duty value, different CSS convention: the Overview badge
    # (ffd_class above) defaults to green and needs "inactive" to grey out;
    # the Documents row's check mark (.doc-row .check) is the opposite —
    # defaults grey, needs "signed" to go green, matching the sign-pad
    # check's own convention right below. Reusing ffd_class on that
    # checkmark meant it could only ever render grey, signed or not.
    ctx["ffd_check_class"] = "signed" if ctx.get("fit_for_duty") else ""
    ctx["signed_class"] = "signed" if ctx.get("signature") else ""
    # Raw user-typed account settings, not OFP/PBS data — escaped before
    # landing in an HTML attribute since, unlike the rest of ctx, a pilot
    # can type anything here.
    ctx["default_simbrief_user"] = html.escape(current_user.default_simbrief_user or "")
    ctx["aeroapi_key"] = html.escape(current_user.aeroapi_key or "")
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
<link rel="icon" href="/static/icon-192.png">
<title>FOS</title>
<style>
  html,body{height:100%;overscroll-behavior:none;background:#eef1f4;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px;color:#1a1f29;}
  h1{font-size:18px;color:#144e94;margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;}
  textarea, select, input[type=text]{width:100%;max-width:640px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid #e3e6ea;border-radius:5px;box-sizing:border-box;background:#fbfbfc;}
  textarea{height:160px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;}
  button{margin-top:10px;background:#1c63b7;color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  button.secondary{background:#2fa355;}
  .arow{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#fff;border:1px solid #e3e6ea;border-radius:6px;padding:10px 14px;margin-bottom:8px;max-width:640px;}
  .arow-link{flex:1;min-width:0;text-decoration:none;color:#1a1f29;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .arow-del{flex:0 0 auto;background:none;border:none;color:#9aa1ab;cursor:pointer;padding:6px;margin:-6px;display:flex;}
  .arow-del svg{width:16px;height:16px;}
  .arow-del:hover{color:#c0392b;}
  .clear-all-link{float:right;color:#1c63b7;font-size:12.5px;font-weight:600;background:none;border:none;cursor:pointer;padding:0;}
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
    <button class="home-tile" onclick="showHomeView('load-sequence');showTab('manual');">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
      <div><div class="tile-title">Create a Flight</div><div class="tile-sub">Skip PBS — just origin, destination, and flight number</div></div>
    </button>
    <button class="home-tile" onclick="showHomeView('import-simbrief')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      <div><div class="tile-title">Import from SimBrief</div><div class="tile-sub">Load whatever OFP is on your account right now</div></div>
    </button>
    $current_flight_button
    $request_data_button
  </div>

  <h1 style="margin-top:28px;font-size:15px;">Recent Flights $clear_flights_link</h1>
  <div id="archive-list">$rows</div>
  <form method="POST" action="/logout" style="margin-top:28px;max-width:640px;">
    <button type="submit" style="width:100%;background:#6b7380;">Sign Out ($username)</button>
  </form>
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
    <div id="import-msg" class="msg"></div>

    <h1 style="margin-top:28px;">Sequences <button class="clear-all-link" onclick="clearAllSequences()">Clear All</button></h1>
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

<div id="pick-leg-view" class="sub-view">
  <div class="subview-topbar">
    <button class="back-link" onclick="showHomeView('load-sequence')">Back</button>
    <h1>Pick a Leg</h1>
  </div>
  <div id="pick-leg-summary" style="font-size:13px;color:#6b7380;margin-bottom:10px;max-width:640px;"></div>
  <div id="pick-leg-list" style="max-width:640px;"></div>
  <div id="pick-leg-msg" class="msg"></div>
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
const SERVER_SIMBRIEF_USER = "$default_simbrief_user";
function showHomeView(view){
  document.getElementById('home-view').classList.toggle('active', view==='home');
  document.getElementById('load-sequence-view').classList.toggle('active', view==='load-sequence');
  document.getElementById('import-simbrief-view').classList.toggle('active', view==='import-simbrief');
  document.getElementById('pick-leg-view').classList.toggle('active', view==='pick-leg');
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
  reader.onload = () => importPbs(reader.result);
  reader.onerror = () => {
    el.textContent = 'Could not read that file.';
    el.style.color = '#c0392b';
  };
  reader.readAsText(file);
}
function importPbs(text){
  const el = document.getElementById('import-msg');
  if(!text || !text.trim()){ el.textContent = 'That file was empty.'; el.style.color = '#c0392b'; return; }
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

const TRASH_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>';
function loadSequences(){
  fetch('/pbs/sequences').then(r=>r.json()).then(seqs=>{
    const list = document.getElementById('seq-list');
    list.innerHTML = seqs.map(s => `<div class="arow">
      <a class="arow-link" href="#" onclick="openSequence('${s.seq}');return false;">SEQ ${s.seq} — ${(s.routing||[]).join('-')} <span>${s.days} day(s)</span></a>
      <button class="arow-del" title="Delete this sequence" onclick="deleteSequence('${s.seq}')">${TRASH_ICON_SVG}</button>
    </div>`).join('') || '<p class="empty">No sequences imported yet.</p>';
  });
}
function deleteSequence(seq){
  if(!confirm('Delete SEQ ' + seq + '? This cannot be undone.')) return;
  fetch('/pbs/sequences/' + encodeURIComponent(seq), {method:'DELETE'}).then(loadSequences);
}
function clearAllSequences(){
  if(!confirm('Delete every imported sequence? This cannot be undone.')) return;
  fetch('/pbs/sequences', {method:'DELETE'}).then(loadSequences);
}
function deleteLeg(id){
  if(!confirm('Delete this flight? This cannot be undone.')) return;
  fetch('/fos/' + id, {method:'DELETE'}).then(() => window.location.reload());
}
function clearAllFlights(){
  if(!confirm('Delete every recent flight? This cannot be undone.')) return;
  fetch('/archive', {method:'DELETE'}).then(() => window.location.reload());
}

async function openSequence(seq){
  const el = document.getElementById('seq-open-msg');
  el.textContent = 'Opening…';
  el.style.color = '';
  try {
    const seqR = await fetch('/pbs/sequences/' + seq);
    const seqData = await seqR.json();
    if(!seqR.ok){ el.textContent = seqData.error || 'Sequence not found'; el.style.color = '#c0392b'; return; }
    el.textContent = '';
    renderLegPicker(seqData);
    showHomeView('pick-leg');
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = '#c0392b';
  }
}

function renderLegPicker(seqData){
  const position = (seqData.positions && seqData.positions[0]) || '';
  const stations = [];
  (seqData.duty_days || []).forEach(day => (day.legs || []).forEach(leg => {
    if(!stations.length) stations.push(leg.origin);
    stations.push(leg.destination);
  }));
  document.getElementById('pick-leg-summary').textContent =
    'SEQ ' + seqData.seq + '  ·  ' + stations.join('-') +
    '  ·  ' + (seqData.positions || []).join('/');
  const list = document.getElementById('pick-leg-list');
  list.innerHTML = '';
  (seqData.duty_days || []).forEach(day => {
    const heading = document.createElement('div');
    heading.style.cssText = 'font-size:13px;font-weight:600;color:#144e94;margin:14px 0 6px;';
    heading.textContent = 'Day ' + day.duty_day + ' — RPT ' + (day.report || '');
    list.appendChild(heading);
    (day.legs || []).forEach((leg, i) => {
      const row = document.createElement('a');
      row.className = 'arow';
      row.href = '#';
      // fleet_type_icao falls back to the raw PBS code when unmapped, so
      // it only ever differs from leg.equipment when it actually decoded
      // to something (e.g. "A319" for "19E") — showing both here means
      // you know the real aircraft type before picking a leg, not just
      // its internal sub-fleet code.
      const fleet = leg.fleet_type_icao
        ? ' · ' + leg.fleet_type_icao + (leg.equipment && leg.equipment !== leg.fleet_type_icao ? ' (' + leg.equipment + ')' : '')
        : '';
      row.innerHTML = (leg.flight_number || '—') + ' ' + (leg.origin || '') + '→' + (leg.destination || '') + fleet +
        ' <span>' + (leg.dep_local || '') + '/' + (leg.arr_local || '') + '</span>';
      row.onclick = (e) => { e.preventDefault(); generateFromSequence(seqData.seq, day.duty_day, i, position); };
      list.appendChild(row);
    });
  });
  if(!list.children.length){
    list.innerHTML = '<p class="empty">No duty days on this sequence.</p>';
  }
}

async function generateFromSequence(seq, dutyDay, legIndex, position){
  const el = document.getElementById('pick-leg-msg');
  el.textContent = 'Starting…';
  el.style.color = '';
  try {
    const genR = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: dutyDay, leg_index: legIndex, position: position}),
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
  fetch('/settings/simbrief-user', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})});
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

// Current Flight / Request New Data are rendered server-side now (see
// render_launcher_html / current_user.current_leg_id) instead of a client
// fetch('/archive') + localStorage lookup — the server already knows which
// pilot is asking, so it can just say whether there's something to resume.

(function(){
  const saved = localStorage.getItem('fos_simbrief_user') || SERVER_SIMBRIEF_USER;
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
<meta name="theme-color" content="#1d1d1f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="mobileFOS">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<title>Flight $flight_number \u2013 FOS</title>
<style>
  :root{
    --navy:#1d1d1f; --blue:#0071e3; --blue-dark:#0058a8;
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --red:#ff3b30; --green:#34c759; --inactive:#9aa1ab; --radius:10px;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;height:100%;overscroll-behavior:none;background:var(--bg);}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--value);-webkit-font-smoothing:antialiased;}
  button{font-family:inherit;}
  :focus-visible{outline:2px solid var(--blue-dark);outline-offset:2px;}
  @media (prefers-reduced-motion: reduce){ *{transition:none !important;animation:none !important;} }
  .app-shell{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh;width:100%;padding-top:env(safe-area-inset-top);padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right);}
  .main{flex:1;min-width:0;padding:14px 16px calc(72px + env(safe-area-inset-bottom));}
  .tabbar{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);bottom:0;display:flex;background:var(--card);border-top:1px solid var(--border);padding:6px 0 calc(6px + env(safe-area-inset-bottom));z-index:20;}
  .tab-btn{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;background:transparent;border:none;color:var(--label);cursor:pointer;padding:4px 2px;position:relative;}
  .tab-btn svg{width:22px;height:22px;}
  .tab-btn span{font-size:10px;font-weight:600;}
  .tab-btn.active{color:var(--blue-dark);}
  .tab-btn .badge{position:absolute;top:0;left:50%;margin-left:6px;width:15px;height:15px;border-radius:50%;background:var(--red);color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;}
  .flight-card{display:flex;align-items:stretch;padding:12px 13px 4px;gap:8px;}
  .flight-card .station{flex:1;display:flex;flex-direction:column;gap:2px;min-width:0;}
  .flight-card .station.dest{align-items:flex-end;text-align:right;}
  .flight-card .station-date{font-size:10.5px;color:var(--label);text-transform:uppercase;letter-spacing:.02em;}
  .flight-card .station-code{font-size:19px;font-weight:700;}
  .flight-card .est-time{font-size:14px;font-weight:700;color:var(--green);}
  .flight-card .est-time.late{color:var(--red);}
  .flight-card .sched-time{font-size:11px;color:var(--label);text-decoration:line-through;margin-left:5px;}
  .flight-card .station-gate{font-size:15px;font-weight:700;color:var(--blue);margin-top:2px;}
  .flight-card-dur{flex:0 0 auto;align-self:center;font-size:11px;color:var(--label);white-space:nowrap;padding:0 6px;}
  .pill-strip{display:flex;gap:8px;overflow-x:auto;padding:8px 0 12px;scrollbar-width:none;}
  .pill-strip::-webkit-scrollbar{display:none;}
  .leg-pill{flex:0 0 auto;font-family:inherit;font-size:13px;font-weight:600;padding:7px 16px;border-radius:20px;border:none;background:transparent;color:var(--value);cursor:pointer;white-space:nowrap;}
  .leg-pill.selected{background:var(--blue);color:#fff;font-weight:700;}
  .split{display:flex;gap:0;align-items:flex-start;}
  .split-left{flex:0 0 auto;width:310px;display:flex;flex-direction:column;gap:12px;min-width:0;}
  .split-right{flex:0 1 400px;min-width:0;display:flex;flex-direction:column;gap:10px;margin-left:24px;}
  .panel-card{background:var(--card);border-radius:16px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.05);border:1px solid var(--border);}
  .panel-card-hdr{padding:11px 13px 8px;font-size:15px;font-weight:700;color:var(--value);background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;}
  .lead-icon{width:16px;height:16px;color:#5b6472;flex:0 0 auto;}
  .mot-row{padding:11px 13px;display:flex;justify-content:space-between;align-items:center;}
  .mot-time{font-size:15px;font-weight:700;}
  .mot-scorecard{padding:9px 13px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;color:var(--inactive);}
  .mot-scorecard .code{font-weight:700;font-size:12.5px;color:var(--inactive);}
  .mot-scorecard .desc{font-size:10.5px;color:var(--inactive);}
  .mot-view{font-size:13px;font-weight:600;color:var(--inactive);}
  .stat-row{background:var(--card);border-radius:16px;border:1px solid var(--border);overflow:hidden;}
  .stat-hdr{display:flex;justify-content:space-between;align-items:center;padding:13px 14px;cursor:pointer;font-size:15px;font-weight:700;background:none;border:none;width:100%;text-align:left;color:var(--value);}
  .stat-hdr .val{color:var(--label);font-weight:500;display:flex;align-items:center;gap:6px;font-size:15px;}
  .stat-hdr svg{width:15px;height:15px;color:var(--inactive);transition:transform .15s ease;}
  .stat-row.open .stat-hdr svg{transform:rotate(180deg);}
  .stat-body{display:none;border-top:1px solid var(--border);}
  .stat-row.open .stat-body{display:block;}
  .stat-detail-row{display:flex;justify-content:space-between;padding:9px 14px;font-size:12.5px;border-bottom:1px solid var(--border);}
  .stat-detail-row:last-child{border-bottom:none;}
  .stat-detail-row .lbl{color:var(--label);}
  .stat-detail-row .val{font-weight:600;font-variant-numeric:tabular-nums;}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;margin-bottom:10px;position:sticky;top:env(safe-area-inset-top);z-index:10;background:var(--bg);padding-top:6px;margin-top:-6px;margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px;}
  .back-link{order:1;color:var(--blue-dark);font-size:14px;font-weight:500;background:none;border:none;cursor:pointer;padding:4px 2px;}
  .topbar-actions{order:2;margin-left:auto;display:flex;align-items:center;gap:14px;}
  .topbar-title{order:3;flex:1 1 100%;text-align:center;margin-top:2px;}
  .topbar-title h1{font-size:17px;margin:0;font-weight:600;color:var(--blue-dark);}
  .topbar-title p{font-size:11px;margin:2px 0 0;color:var(--label);}
  .icon-btn{background:none;border:none;color:#5b6472;cursor:pointer;padding:2px;display:flex;}
  .icon-btn svg{width:19px;height:19px;}
  .status-bar{background:var(--navy);color:#fff;display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-radius:var(--radius) var(--radius) 0 0;font-size:13px;font-weight:600;}
  .flight-summary{background:var(--card);display:flex;align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);font-size:13px;gap:18px;flex-wrap:wrap;}
  .flight-summary .fnum{font-size:15px;font-weight:700;}
  .flight-summary .col{display:flex;flex-direction:column;line-height:1.5;}
  .flight-summary .col.highlight{color:var(--blue-dark);font-weight:600;}
  .duty-badges{display:flex;justify-content:flex-end;gap:22px;padding:10px 14px 8px;background:var(--card);font-size:12px;font-weight:600;}
  .duty-badges span{display:flex;align-items:center;gap:5px;color:var(--blue-dark);}
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
  .doc-row .check.signed{color:var(--blue-dark);}
  .doc-row .actions svg.bookmark-icon.bookmarked{color:var(--blue);}
  .doc-row .actions svg.ext-link{color:var(--blue);}
  #sign-pad{touch-action:none;background:#fff;border:1px solid var(--border);border-radius:6px;width:100%;height:220px;}
  .placeholder-note{padding:12px 14px;color:var(--label);font-style:italic;font-size:13px;background:var(--card);}
  .view{display:none;}
  .view.active{display:block;}
  #toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(12px);background:#1a1f29;color:#fff;padding:9px 16px;border-radius:20px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .18s ease, transform .18s ease;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:10;}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
  @media (max-width:640px){
    .content-grid{grid-template-columns:1fr;}
    .col-divider{border-right:none;border-bottom:6px solid var(--bg);}
    .flight-summary{gap:12px;font-size:12px;}
    .flight-card .station-code{font-size:19px;}
    .split{flex-direction:column;}
    .split-left{width:100%;}
    .split-right{margin-left:0;margin-top:2px;width:100%;flex-basis:auto;}
  }
</style>
</head>
<body>
<div class="app-shell">
  <main class="main">
    <section id="overview-view" class="view active">
      <div class="topbar">
        <a class="back-link" href="/">Back</a>
        <div class="topbar-actions">
          <button class="icon-btn" title="Settings" onclick="showToast('Settings')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
          </button>
        </div>
        <div class="topbar-title">
          <h1 style="color:var(--value);">SEQ $seq</h1>
        </div>
      </div>
      <div class="pill-strip" id="ov-pill-strip"></div>
      <div class="split">
        <div class="split-left">
          <div class="panel-card">
            <div class="flight-card">
              <div class="station">
                <div class="station-date">$dep_date</div>
                <div class="station-code">$origin</div>
                <div>$dep_time_html</div>
                <div class="station-gate" id="ov-dep-gate">$dep_gate</div>
              </div>
              <div class="flight-card-dur">$flight_time</div>
              <div class="station dest">
                <div class="station-date">$arr_date</div>
                <div class="station-code">$destination</div>
                <div>$arr_time_html</div>
                <div class="station-gate" id="ov-arr-gate">$arr_gate</div>
              </div>
            </div>
          </div>
          <div class="panel-card" id="ov-docs-card">
            <div class="panel-card-hdr">Preflight Docs</div>
            <div class="doc-row" style="cursor:pointer;" onclick="showView('doclocker')">
              <div style="display:flex;align-items:center;gap:10px;">
                <svg class="lead-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M12 11v6"/><path d="M9 14l3 3 3-3"/></svg>
                <div class="code">Saved Docs</div>
              </div>
              <div class="val" id="ov-saved-count" style="color:var(--label);font-size:12px;">$saved_docs_count</div>
            </div>
            <div class="doc-row" style="cursor:pointer;" onclick="openOvAllCommands()">
              <div style="display:flex;align-items:center;gap:10px;">
                <svg class="lead-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><circle cx="10.5" cy="14.5" r="2.5"/><path d="M12.3 16.3L14 18"/></svg>
                <div class="code">All Commands</div>
              </div>
              <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
            </div>
            <div class="doc-row" style="border-bottom:none;cursor:pointer;" onclick="showToast('Not available yet')">
              <div><div class="code">Favorite Groups</div></div>
              <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
            </div>
          </div>
          <div class="panel-card">
            <div class="panel-card-hdr">Mandatory Off Time</div>
            <div class="mot-row"><span class="mot-time">XX:XX (L)</span></div>
            <div class="mot-scorecard">
              <div><div class="code">HI117</div><div class="desc">FAR 117 scorecard</div></div>
              <span class="mot-view">View</span>
            </div>
          </div>
          <div class="panel-card">
            <div class="panel-card-hdr">External Apps</div>
            <div class="doc-row" style="cursor:pointer;" onclick="showView('release')">
              <div><div class="code">SimBrief</div><div class="desc">Send to Dispatch</div></div>
              <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
            </div>
            <div class="doc-row" style="cursor:pointer;" onclick="exportToForeFlight()">
              <div><div class="code">ForeFlight</div><div class="desc">Export Route</div></div>
              <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
            </div>
            <div class="doc-row" style="cursor:pointer;" onclick="prefileVatsim()">
              <div><div class="code">VATSIM</div><div class="desc">Pre-file Flight Plan</div></div>
              <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
            </div>
            <div class="doc-row" style="border-bottom:none;cursor:pointer;" onclick="prefileIvao()">
              <div><div class="code">IVAO</div><div class="desc">Pre-file Flight Plan</div></div>
              <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
            </div>
          </div>
        </div>
        <div class="split-right">
          <div class="stat-row" id="stat-timediff">
            <button class="stat-hdr" onclick="toggleStatRow('timediff')">
              <span>Time Difference</span><span class="val">$tz_diff <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span>
            </button>
            <div class="stat-body">
              <div class="stat-detail-row"><span class="lbl">$origin Local &middot; Departure</span><span class="val" id="td-orig-local">$sched_out</span></div>
              <div class="stat-detail-row"><span class="lbl">$origin Zulu &middot; Departure</span><span class="val" id="td-orig-zulu">&mdash;</span></div>
              <div class="stat-detail-row"><span class="lbl">$destination Local &middot; Arrival</span><span class="val" id="td-dest-local">$sched_in</span></div>
              <div class="stat-detail-row"><span class="lbl">$destination Zulu &middot; Arrival</span><span class="val" id="td-dest-zulu">&mdash;</span></div>
            </div>
          </div>
          <div class="stat-row" id="stat-crew">
            <button class="stat-hdr" onclick="toggleStatRow('crew')">
              <span>Crew</span><span class="val">$crew_count <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span>
            </button>
            <div class="stat-body">$crew_rows</div>
          </div>
          <div class="stat-row" id="stat-aircraft">
            <button class="stat-hdr" onclick="toggleStatRow('aircraft')">
              <span>Aircraft</span><span class="val">$aircraft_display <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span>
            </button>
            <div class="stat-body">
              <div class="stat-detail-row"><span class="lbl">Tail Number</span><span class="val">$tail_number_html</span></div>
              <div class="stat-detail-row"><span class="lbl">Fleet Number</span><span class="val">$fin_html</span></div>
              <div class="stat-detail-row"><span class="lbl">ICAO Type</span><span class="val">$fleet_type_icao</span></div>
              <div class="stat-detail-row"><span class="lbl">Aircraft</span><span class="val">$aircraft_name_html</span></div>
              <div class="stat-detail-row"><span class="lbl">Engine</span><span class="val">$engines_html</span></div>
            </div>
          </div>
          <div class="stat-row" id="stat-pax">
            <button class="stat-hdr" onclick="toggleStatRow('pax')">
              <span>Passengers</span><span class="val">$pax_display <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span>
            </button>
            <div class="stat-body">
              <div class="stat-detail-row"><span class="lbl">Booked</span><span class="val">$customer_load</span></div>
              <div class="stat-detail-row"><span class="lbl">Capacity</span><span class="val">$seat_capacity</span></div>
            </div>
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
      <div class="doc-list">
        <div class="doc-row" style="cursor:pointer;" onclick="showView('doclocker')">
          <div><div class="code">Saved Docs</div></div>
          <div class="val" style="color:var(--label);font-size:13px;">0</div>
        </div>
        <div class="doc-row" style="cursor:pointer;" onclick="openAllCommands()">
          <div><div class="code">All Commands</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
        <div class="doc-row" style="border-bottom:none;cursor:pointer;" onclick="showToast('Not available yet')">
          <div><div class="code">Favorite Groups</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
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
      <div class="card" id="crew-body" style="display:none;">$crew_rows</div>
      <button class="section-bar" id="flight-bar" onclick="toggleSection('flight')">
        Flight
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="doc-list" id="flight-body">
        <div class="doc-row">
          <div><div class="code">FFD</div><div class="desc">Fit for Duty Declaration</div></div>
          <div class="actions">
            <svg class="bookmark-icon" data-doc="FFD" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FFD', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
            <svg id="ffd-doc-check" class="check $ffd_check_class" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="toggleStatus('fit-for-duty','ffd-doc-check')"><path d="M20 6L9 17l-5-5"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">EFLIGHT PLAN</div><div class="desc">eFlight Plan</div></div>
          <div class="actions">
            <svg class="bookmark-icon" data-doc="EFLIGHT PLAN" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('EFLIGHT PLAN', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
            <svg id="sign-check" class="check $signed_class" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="openSignPad()"><path d="M20 6L9 17l-5-5"/></svg>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('rls','eFlight Plan')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">FI</div><div class="desc">Flight Details \u2013 GMT</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="FI" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FI', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('fi','Flight Details \u2013 GMT')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">FIL</div><div class="desc">Flight Details \u2013 Local</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="FIL" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FIL', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('fil','Flight Details \u2013 Local')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">WBD</div><div class="desc">Weight &amp; Balance Data (TPS)</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="WBD" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('WBD', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('wb','Weight &amp; Balance Data')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">AL*</div><div class="desc">Field Condition Report &amp; NOTAMs</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="AL*" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('AL*', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('notams','NOTAMs')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">FR</div><div class="desc">Field Reports</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="FR" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FR', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('field_report','Field Reports')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">WX*</div><div class="desc">Winds &amp; Weather</div></div>
          <div class="actions"><svg class="bookmark-icon" data-doc="WX*" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('WX*', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="viewDoc('weather','Winds &amp; Weather')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row">
          <div><div class="code">G*L/SS</div><div class="desc">Customers Requiring Special Services</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Not available \u2014 no data source for this document yet')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row" style="border-bottom:none;">
          <div><div class="code">MOT</div><div class="desc">Mandatory Off Time</div></div>
          <div class="val" style="color:var(--label);font-size:13px;font-style:italic;">Not available</div>
        </div>
      </div>
      <button class="section-bar collapsed" id="extapps-bar" onclick="toggleSection('extapps')">
        External Apps
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="doc-list" id="extapps-body" style="display:none;">
        <div class="doc-row" style="cursor:pointer;" onclick="showView('release')">
          <div><div class="code">SimBrief</div><div class="desc">Send to SimBrief Dispatch</div></div>
          <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
        </div>
        <div class="doc-row" style="border-bottom:none;cursor:pointer;" onclick="exportToForeFlight()">
          <div><div class="code">ForeFlight</div><div class="desc">Export Route</div></div>
          <div class="actions"><svg class="ext-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg></div>
        </div>
      </div>
    </section>
    <section id="weather-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('documents')">Back</button>
        <div class="topbar-title">
          <h1>Winds &amp; Weather</h1>
          <p>WX*$origin / WX*$destination</p>
        </div>
      </div>
      <div id="weather-body"><p class="placeholder-note">Loading\u2026</p></div>
    </section>

    <section id="doclocker-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title"><h1>Saved Docs</h1></div>
      </div>
      <div id="doclocker-body"><p class="placeholder-note">No saved documents yet.</p></div>
    </section>

    <section id="messages-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title"><h1>Messages</h1></div>
      </div>
      <p class="placeholder-note">No messages.</p>
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
      <p class="placeholder-note">SimBrief username and AeroAPI key are set once under <a href="#" onclick="showView('settings');return false;" style="color:var(--blue-dark);font-weight:600;">Settings</a> and apply to every flight.</p>

      <button class="section-bar" id="aero-bar" onclick="toggleSection('aero')">
        Route &amp; Gate Suggestions (FlightAware)
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div id="aero-body">
        <div style="padding:14px;background:var(--card);">
          <button id="aero-btn" onclick="fetchAeroSuggestions()" style="margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;">Get Suggestions</button>
          <div id="aero-msg" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
          <div id="aero-results" style="display:none;margin-top:10px;">
            <div class="info-row"><span class="lbl">Suggested Route</span><span class="val" id="aero-route-val">—</span></div>
            <button onclick="applyAeroRoute()" style="margin:8px 0 0;width:100%;background:var(--blue);color:#fff;border:none;padding:9px;border-radius:5px;font-size:13px;font-weight:600;cursor:pointer;">Use This Route</button>
            <div class="info-row" style="margin-top:10px;"><span class="lbl">Suggested Gate — Origin</span><span class="val" id="aero-gate-orig">—</span></div>
            <div class="info-row"><span class="lbl">Suggested Gate — Destination</span><span class="val" id="aero-gate-dest">—</span></div>
            <button onclick="applyAeroGates()" style="margin:8px 0 0;width:100%;background:var(--blue);color:#fff;border:none;padding:9px;border-radius:5px;font-size:13px;font-weight:600;cursor:pointer;">Apply Gates to This Flight</button>
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
          <label for="sbgen-type">Aircraft Type — ICAO code (B738) or your saved airframe's Internal ID (123456_1582090020)</label>
          <input id="sbgen-type" type="text" placeholder="B738 or 123456_1582090020">
          <div id="sbgen-type-hint" style="margin-top:4px;font-size:12px;color:var(--label);"></div>
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
          <div class="search-block"><label for="sbgen-date">Date</label><input id="sbgen-date" type="date"></div>
          <div class="search-block"><label for="sbgen-time">Dep Time, local</label><input id="sbgen-time" type="time"></div>
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
    <section id="settings-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title">
          <h1>Settings</h1>
          <p>Applies to every flight on this account</p>
        </div>
      </div>
      <div class="search-block">
        <label for="release-user">SimBrief Username</label>
        <input id="release-user" type="text" placeholder="Your SimBrief username" value="$default_simbrief_user" onchange="saveSimbriefUser(this.value)">
      </div>
      <div class="search-block">
        <label for="aero-key">FlightAware AeroAPI Key</label>
        <input id="aero-key" type="password" placeholder="Your AeroAPI key" value="$aeroapi_key" onchange="saveAeroApiKey(this.value)">
      </div>
      <div id="settings-msg" class="placeholder-note"></div>
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
          <a id="release-rls-link" style="display:none;background:var(--blue);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download RLS PDF</a>
          <a id="release-wb-link" style="display:none;background:var(--blue-dark);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download W&amp;B PDF</a>
        </div>
        <button id="confirm-continue-btn" style="display:none;margin-top:10px;width:100%;background:var(--label);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;" onclick="showView('overview')">Continue to Flight</button>
      </div>
    </section>
    <section id="more-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title"><h1>More</h1></div>
      </div>
      <div class="doc-list">
        <div class="doc-row" style="cursor:pointer;" onclick="showView('release')">
          <div><div class="code">SB</div><div class="desc">Send to SimBrief</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
        <div class="doc-row" style="cursor:pointer;" onclick="exportToForeFlight()">
          <div><div class="code">FF</div><div class="desc">Export to ForeFlight</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
        <div class="doc-row" style="cursor:pointer;" onclick="showToast('Web')">
          <div><div class="code">WEB</div><div class="desc">Web</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
        <div class="doc-row" style="cursor:pointer;" onclick="showView('settings')">
          <div><div class="code">SET</div><div class="desc">Settings</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
        </div>
      </div>
    </section>
  </main>
  <nav class="tabbar" aria-label="Primary">
    <button class="tab-btn active" id="tab-overview" onclick="showView('overview')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg>
      <span>Overview</span>
    </button>
    <button class="tab-btn" id="tab-schedule" onclick="showView('pairing')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/></svg>
      <span>Schedule</span>
    </button>
    <button class="tab-btn" id="tab-messages" onclick="showView('messages')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 6l9 7 9-7"/></svg>
      <span>Messages</span>
    </button>
    <button class="tab-btn" id="tab-docs" onclick="showView('doclocker')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M9.5 13h5M9.5 16h5"/></svg>
      <span>Docs</span>
    </button>
    <button class="tab-btn" id="tab-more" onclick="showView('more')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
      <span>More</span>
    </button>
  </nav>
</div>
<div id="toast"></div>
<script>
const LEG_ID = "$leg_id";
const LEG_FLIGHT_NUMBER = "$flight_number";
const LEG_ORIGIN = "$origin";
const LEG_DESTINATION = "$destination";
const LEG_ROUTE = "$route";
const LEG_TAIL_NUMBER = "$tail_number";
const LEG_SCHED_OUT = "$sched_out";
const LEG_FLEET_TYPE = "$fleet_type";
const LEG_FLEET_TYPE_ICAO = "$fleet_type_icao";
const LEG_EQUIPMENT_TYPE = "$equipment_type";
const LEG_ORIGIN_ICAO = "$origin_icao";
const LEG_DESTINATION_ICAO = "$destination_icao";
const LEG_DEP_DATE = "$dep_date";
const LEG_ARR_DATE = "$arr_date";
const LEG_SCHED_IN = "$sched_in";
const LEG_BOOKMARKED_DOCS = $bookmarked_docs_json;
function showView(view){
  document.getElementById('overview-view').classList.toggle('active', view==='overview');
  document.getElementById('documents-view').classList.toggle('active', view==='documents');
  document.getElementById('release-view').classList.toggle('active', view==='release');
  document.getElementById('confirm-view').classList.toggle('active', view==='confirm');
  document.getElementById('pdf-view').classList.toggle('active', view==='pdf');
  document.getElementById('sign-view').classList.toggle('active', view==='sign');
  document.getElementById('pairing-view').classList.toggle('active', view==='pairing');
  document.getElementById('weather-view').classList.toggle('active', view==='weather');
  document.getElementById('doclocker-view').classList.toggle('active', view==='doclocker');
  document.getElementById('messages-view').classList.toggle('active', view==='messages');
  document.getElementById('settings-view').classList.toggle('active', view==='settings');
  document.getElementById('more-view').classList.toggle('active', view==='more');
  document.getElementById('tab-overview').classList.toggle('active', view==='overview');
  document.getElementById('tab-schedule').classList.toggle('active', view==='pairing');
  document.getElementById('tab-docs').classList.toggle('active', view==='doclocker');
  document.getElementById('tab-messages').classList.toggle('active', view==='messages');
  // Release/Confirm/Settings/More are all reached through the More tab now
  // (mobileCCI's five-tab bar has no dedicated Release/Settings icon).
  document.getElementById('tab-more').classList.toggle('active', view==='release' || view==='confirm' || view==='settings' || view==='more');
  window.scrollTo(0,0);
  if(view === 'release') initReleaseView();
  if(view === 'confirm') initConfirmView();
  if(view === 'sign') initSignPad();
  if(view === 'doclocker') initDocLocker();
  if(view === 'pairing') initPairingView();
  if(view === 'overview') initOverviewPills();
  if(view === 'weather' && !_weatherLoaded) loadWeather();
}
function saveSimbriefUser(value){
  const user = value.trim();
  fetch('/settings/simbrief-user', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})})
    .then(()=>{ document.getElementById('settings-msg').textContent = 'Saved.'; })
    .catch(()=>{ document.getElementById('settings-msg').textContent = 'Could not save — try again.'; });
}
function saveAeroApiKey(value){
  const key = value.trim();
  fetch('/settings/aeroapi-key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({aeroapi_key: key})})
    .then(()=>{ document.getElementById('settings-msg').textContent = 'Saved.'; })
    .catch(()=>{ document.getElementById('settings-msg').textContent = 'Could not save — try again.'; });
}
function initReleaseView(){
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
  // aero-key and release-user are pre-filled server-side (Settings, under
  // current_user) via the input's value attribute — nothing to do here.
  // Leg-specific aircraft data wins over the last-remembered one: a
  // SimBrief-loaded leg's fleet_type is already a real ICAO type code
  // (aircraft/icaocode); a PBS-pairing leg's raw equipment token gets
  // decoded server-side (_fleet_type_icao) against the real sub-fleet
  // table and passed through unchanged if unmapped.
  document.getElementById('sbgen-type').value = LEG_FLEET_TYPE_ICAO || LEG_EQUIPMENT_TYPE || localStorage.getItem('fos_simbrief_airframe') || '';
  // "A319" alone doesn't say which A319 (e.g. 19E/19F/19S are all
  // different sub-fleets with different winglet/config variants) — show
  // the raw PBS code alongside the decoded type when they actually
  // differ, so that's visible before sending.
  document.getElementById('sbgen-type-hint').textContent =
    (LEG_FLEET_TYPE && LEG_FLEET_TYPE_ICAO && LEG_FLEET_TYPE !== LEG_FLEET_TYPE_ICAO) ? 'PBS code: ' + LEG_FLEET_TYPE : '';
  document.getElementById('sbgen-fltnum').value = LEG_FLIGHT_NUMBER || '';
  document.getElementById('sbgen-date').value = todayZuluISO();
  document.getElementById('sbgen-time').value = LEG_SCHED_OUT || '';
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
              if(!document.getElementById('sbgen-time').value && l.dep_local && l.dep_local.length === 4){
                document.getElementById('sbgen-time').value = l.dep_local.slice(0, 2) + ':' + l.dep_local.slice(2);
              }
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

  // Fetched now (well before the send button can be clicked) rather than
  // at submit time — submitSimbriefGen() has to open the SimBrief tab
  // synchronously with the click or Safari blocks it as not user-
  // initiated, so there's no room for an awaited fetch there.
  _origTzName = '';
  if(orig){
    try {
      const tzR = await fetch('/airport-timezone/' + encodeURIComponent(orig));
      if(tzR.ok) _origTzName = (await tzR.json()).tz || '';
    } catch(e) { /* best-effort — submit falls back to sending local time as-is */ }
  }
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
function generateRelease(force){
  const btn = document.getElementById('release-gen-btn');
  const status = document.getElementById('release-status');
  const userId = document.getElementById('release-user').value.trim();
  if(!userId){ status.textContent = 'No SimBrief username on file — set one in Settings first.'; status.style.color = '#c0392b'; return; }
  btn.disabled = true;
  status.style.color = '';
  status.textContent = force ? 'Regenerating — this can take up to a minute…' : 'Generating release — this can take up to a minute…';
  document.getElementById('release-downloads').style.display = 'none';
  fetch('/fos/' + LEG_ID + '/release', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id:userId, force: !!force})})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      btn.disabled = false;
      if(!ok){ status.textContent = 'Failed: ' + (data.error || 'unknown error'); status.style.color = '#c0392b'; return; }
      _releaseCache = data; // ensureRelease()/viewDoc() reuse this — one generation serves both paths
      status.innerHTML = (data.cached ? 'Release already on file (generated ' + new Date(data.generated_at).toLocaleString() + ').' : 'Release generated.')
        + ' <a href="#" onclick="generateRelease(true);return false;" style="color:inherit;">Regenerate</a>';
      status.style.color = 'var(--blue-dark)';
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
function openAllCommands(){
  showView('documents');
  document.getElementById('flight-bar').classList.remove('collapsed');
  document.getElementById('flight-body').style.display = 'block';
  document.getElementById('flight-bar').scrollIntoView({block: 'start'});
}
function toggleStatus(kind, elId){
  fetch('/fos/' + LEG_ID + '/' + kind, {method:'POST'})
    .then(r=>r.json())
    .then(data=>{
      const key = kind === 'signin' ? 'signed_in' : 'fit_for_duty';
      const badgeId = kind === 'signin' ? 'signin-badge' : 'ffd-badge';
      // Two elements can show this same status (the Overview badge, now
      // read-only, and wherever it's actually toggled from) — keep both
      // in sync rather than just the one that was tapped. They use
      // opposite CSS conventions though: the badge defaults green and
      // needs "inactive" to grey out, while a .doc-row .check (like
      // ffd-doc-check) defaults grey and needs "signed" to go green,
      // same as the sign-pad check — toggling "inactive" on that one
      // just left it permanently grey regardless of actual state.
      const badge = document.getElementById(badgeId);
      if(badge) badge.classList.toggle('inactive', !data[key]);
      if(elId !== badgeId){
        const check = document.getElementById(elId);
        if(check) check.classList.toggle('signed', !!data[key]);
      }
    })
    .catch(()=>showToast('Update failed'));
}
function exportToForeFlight(){
  if(!LEG_ORIGIN || !LEG_DESTINATION){ showToast('No route on this flight yet'); return; }
  // ForeFlight Mobile's maps/search deep link takes a space-separated
  // station string (orig, enroute waypoints, dest) — spaces become '+' in
  // the URL, same convention as https://foreflightmobile://maps/search.
  // LEG_ROUTE is SimBrief's general/route (enroute waypoints only, no
  // orig/dest), so orig + route + dest gets the full routing.
  //
  // Deliberately NOT appending a tail number: ForeFlight's docs only show
  // one working with a registration when it directly follows speed/
  // altitude tokens (e.g. "...14000ft+N12345") — those apparently tell
  // its parser "the route list just ended, aircraft data starts here."
  // We don't have cruise altitude/speed to supply that context, and a
  // bare tail number with nothing in front of it broke the whole import
  // in practice, not just the registration part.
  const parts = [LEG_ORIGIN, LEG_ROUTE, LEG_DESTINATION].filter(Boolean);
  const url = 'foreflightmobile://maps/search?q=' + encodeURIComponent(parts.join(' ')).replace(/%20/g, '+');
  const link = document.createElement('a');
  link.href = url;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  showToast('Opening in ForeFlight…');
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
  const userId = document.getElementById('release-user').value.trim();
  if(!userId){ showToast('Send this flight to SimBrief first to get a username on file'); return null; }
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
  const field = {rls:'rls_pdf_b64', fi:'fi_pdf_b64', fil:'fil_pdf_b64', wb:'wb_pdf_b64', weather:'weather_pdf_b64', notams:'notams_pdf_b64', field_report:'field_report_pdf_b64'}[kind];
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
  cacheBtn.style.cssText = 'margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
  cacheBtn.onclick = () => cacheAllPairingLegs(seqData, position, cacheMsg);
  const cacheMsg = document.createElement('div');
  cacheMsg.style.cssText = 'margin-top:8px;font-size:12.5px;color:var(--label);';
  cacheWrap.appendChild(cacheBtn);
  cacheWrap.appendChild(cacheMsg);
  body.appendChild(cacheWrap);

  // Each day is one clickable pane — tapping anywhere in it (except the
  // paper-plane "send via SimBrief" icon, which stops propagation) is the
  // only way into Overview from here now; individual leg rows are no
  // longer their own nav target, just information + that one action.
  (seqData.duty_days || []).forEach(day => {
    const pane = document.createElement('div');
    pane.style.cssText = 'margin-bottom:14px;border-radius:16px;overflow:hidden;border:1px solid var(--border);cursor:pointer;';
    pane.onclick = () => generatePairingLeg(seqData.seq, day.duty_day, 0, position);

    const bar = document.createElement('div');
    bar.className = 'section-bar';
    bar.style.cssText = 'background:var(--navy);cursor:pointer;';
    bar.textContent = 'Day ' + day.duty_day + ' — RPT ' + (day.report || '');
    pane.appendChild(bar);

    const list = document.createElement('div');
    list.className = 'doc-list';
    (day.legs || []).forEach((leg, i) => {
      const row = document.createElement('div');
      row.className = 'doc-row';
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
      row.appendChild(left);
      row.appendChild(actions);
      list.appendChild(row);
    });
    pane.appendChild(list);

    const bits = [
      day.release ? ('RLS ' + day.release) : '',
      day.duty ? ('Duty ' + day.duty) : '',
      day.tafb ? ('TAFB ' + day.tafb) : '',
      day.hotel || '',
    ].filter(Boolean).join(' · ');
    if(bits){
      const note = document.createElement('p');
      note.className = 'placeholder-note';
      note.style.margin = '0';
      note.textContent = bits;
      pane.appendChild(note);
    }
    body.appendChild(pane);
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
  msgEl.style.color = fails.length ? '#c0392b' : 'var(--blue-dark)';
}

const _DDMMMYY_MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
function todayZuluISO(){
  return new Date().toISOString().slice(0, 10);
}
// The date input hands back "YYYY-MM-DD"; SimBrief's URL param wants
// "DDMMMYY" (e.g. "18AUG26"). Split on '-' rather than parsing as a Date
// to avoid UTC/local timezone shifting the day.
function isoDateToDDMMMYY(iso){
  const m = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(iso || '');
  if(!m) return '';
  const [, year, month, day] = m;
  return day + _DDMMMYY_MONTHS[parseInt(month, 10) - 1] + year.slice(-2);
}

// Origin airport's IANA timezone name, fetched during prefillSimbriefGen()
// (see there for why not here — submit has to stay synchronous).
let _origTzName = '';

// Wall-clock hour/minute in an IANA zone -> zulu {h, m}, DST-aware, using
// only Intl.DateTimeFormat (every evergreen browser bundles IANA tz data
// via ICU — no timezone library needed client-side). Standard fixed-point
// trick: hold the wall-clock we WANT the zone to show as a constant
// target; guess an instant, check what wall-clock that guess actually
// shows in the target zone, and nudge the guess by exactly how far off
// that reading is from the fixed target — not from the previous guess,
// which is what a naive re-diff-each-iteration version gets wrong (it
// compounds instead of converging). Twice, to settle any edge case
// sitting exactly on a DST transition.
function localToZuluParts(dateISO, hour, minute, tzName){
  const [year, month, day] = dateISO.split('-').map(Number);
  const target = Date.UTC(year, month - 1, day, hour, minute);
  let guess = target;
  for(let i = 0; i < 2; i++){
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tzName, hourCycle: 'h23',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).formatToParts(new Date(guess)).reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
    const shown = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute);
    guess = guess - (shown - target);
  }
  const result = new Date(guess);
  return {h: String(result.getUTCHours()).padStart(2, '0'), m: String(result.getUTCMinutes()).padStart(2, '0')};
}

// Leg dates come back from the server as "MM/DD/YY" (simbrief_ofp.py's
// epoch_to format), not the "YYYY-MM-DD" localToZuluParts() needs. Falls
// back to today when a leg has no real date yet (a PBS pairing never
// carries one) — best-effort for the Time Difference popup, not exact.
function mmddyyToISO(s){
  const m = /^(\d{2})\/(\d{2})\/(\d{2})$/.exec(s || '');
  if(!m) return todayZuluISO();
  const [, mo, day, yr] = m;
  return '20' + yr + '-' + mo + '-' + day;
}
let _timeViewLoaded = false;
async function initTimeDiffAccordion(){
  if(_timeViewLoaded) return;
  _timeViewLoaded = true;
  const [origTz, destTz] = await Promise.all([
    LEG_ORIGIN_ICAO ? fetch('/airport-timezone/' + encodeURIComponent(LEG_ORIGIN_ICAO)).then(r=>r.ok?r.json():null).catch(()=>null) : null,
    LEG_DESTINATION_ICAO ? fetch('/airport-timezone/' + encodeURIComponent(LEG_DESTINATION_ICAO)).then(r=>r.ok?r.json():null).catch(()=>null) : null,
  ]);
  if(origTz && origTz.tz && LEG_SCHED_OUT){
    const [h, m] = LEG_SCHED_OUT.split(':').map(Number);
    const z = localToZuluParts(mmddyyToISO(LEG_DEP_DATE), h, m, origTz.tz);
    document.getElementById('td-orig-zulu').textContent = z.h + ':' + z.m + 'Z';
  }
  if(destTz && destTz.tz && LEG_SCHED_IN){
    const [h, m] = LEG_SCHED_IN.split(':').map(Number);
    const z = localToZuluParts(mmddyyToISO(LEG_ARR_DATE), h, m, destTz.tz);
    document.getElementById('td-dest-zulu').textContent = z.h + ':' + z.m + 'Z';
  }
}
function toggleStatRow(name){
  const row = document.getElementById('stat-' + name);
  const opening = !row.classList.contains('open');
  row.classList.toggle('open');
  if(opening && name === 'timediff') initTimeDiffAccordion();
}
async function toggleBookmark(doc, el){
  try {
    const r = await fetch('/fos/' + LEG_ID + '/bookmark', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({doc}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not update bookmark'); return; }
    const bookmarked = data.bookmarked_docs || [];
    document.querySelectorAll('.bookmark-icon[data-doc="' + doc.replace(/"/g, '\\"') + '"]').forEach(icon => {
      icon.classList.toggle('bookmarked', bookmarked.includes(doc));
    });
    const countEl = document.getElementById('ov-saved-count');
    if(countEl) countEl.textContent = bookmarked.length;
  } catch(e) { showToast('Request failed: ' + e); }
}
document.querySelectorAll('.bookmark-icon').forEach(icon => {
  if(LEG_BOOKMARKED_DOCS.includes(icon.dataset.doc)) icon.classList.add('bookmarked');
});
const DOC_CODE_TO_KIND = {
  'EFLIGHT PLAN': ['rls', 'eFlight Plan'], 'FI': ['fi', 'Flight Details – GMT'],
  'FIL': ['fil', 'Flight Details – Local'], 'WBD': ['wb', 'Weight & Balance Data'],
  'AL*': ['notams', 'NOTAMs'], 'FR': ['field_report', 'Field Reports'],
  'WX*': ['weather', 'Winds & Weather'],
};
function initDocLocker(){
  const body = document.getElementById('doclocker-body');
  if(!LEG_BOOKMARKED_DOCS.length){
    body.innerHTML = '<p class="placeholder-note">No saved documents yet. Bookmark one under All Commands to save it here.</p>';
    return;
  }
  body.innerHTML = '<div class="doc-list">' + LEG_BOOKMARKED_DOCS.map(code => {
    const mapped = DOC_CODE_TO_KIND[code];
    const action = mapped ? `viewDoc('${mapped[0]}','${mapped[1].replace(/'/g, "\\'")}')` : `showToast('${code} has no PDF to view')`;
    return `<div class="doc-row" style="cursor:pointer;" onclick="${action}">
      <div><div class="code">${code}</div></div>
      <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/></svg></div>
    </div>`;
  }).join('') + '</div>';
}
function openOvAllCommands(){
  showView('documents');
  document.getElementById('flight-bar').classList.remove('collapsed');
  document.getElementById('flight-body').style.display = 'block';
  document.getElementById('flight-bar').scrollIntoView({block: 'start'});
}
let _ovPillsLoaded = false;
async function initOverviewPills(){
  if(_ovPillsLoaded || !LEG_SEQ) return;
  _ovPillsLoaded = true;
  const strip = document.getElementById('ov-pill-strip');
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(LEG_SEQ));
    if(!r.ok) return;
    const seqData = await r.json();
    const position = LEG_POSITION || (seqData.positions && seqData.positions[0]) || '';
    for(const day of (seqData.duty_days || [])){
      const legs = day.legs || [];
      const idx = legs.findIndex(l => l.flight_number === LEG_FLIGHT_NUMBER && l.origin === LEG_ORIGIN && l.destination === LEG_DESTINATION);
      if(idx === -1) continue;
      legs.forEach((l, i) => {
        const pill = document.createElement('button');
        pill.className = 'leg-pill' + (i === idx ? ' selected' : '');
        pill.textContent = l.flight_number || '—';
        if(i !== idx) pill.onclick = () => generatePairingLeg(seqData.seq, day.duty_day, i, position);
        strip.appendChild(pill);
      });
      break;
    }
  } catch(e) { /* best-effort — leave the strip empty */ }
}
async function prefileVatsim(){
  showToast('Fetching VATSIM prefile…');
  try {
    const r = await fetch('/fos/' + LEG_ID + '/prefile');
    const data = await r.json();
    if(!r.ok || !data.vatsim){ showToast(data.error || 'VATSIM prefile not available — generate a SimBrief OFP first'); return; }
    submitPrefileForm(data.vatsim.action, {raw: data.vatsim.raw, fuel_time: data.vatsim.fuel_time});
  } catch(e) { showToast('Request failed: ' + e); }
}
async function prefileIvao(){
  showToast('Fetching IVAO prefile…');
  try {
    const r = await fetch('/fos/' + LEG_ID + '/prefile');
    const data = await r.json();
    if(!r.ok || !data.ivao){ showToast(data.error || 'IVAO prefile not available — generate a SimBrief OFP first'); return; }
    submitPrefileForm(data.ivao.action, {flightPlan: data.ivao.flight_plan});
  } catch(e) { showToast('Request failed: ' + e); }
}
function submitPrefileForm(action, fields){
  const form = document.createElement('form');
  form.method = 'GET';
  form.action = action;
  form.target = '_blank';
  for(const [name, value] of Object.entries(fields)){
    const input = document.createElement('input');
    input.type = 'hidden'; input.name = name; input.value = value || '';
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

async function submitSimbriefGen(){
  const el = document.getElementById('sbgen-msg');
  const btn = document.getElementById('sbgen-btn');
  const user = document.getElementById('release-user').value.trim();
  const type = document.getElementById('sbgen-type').value.trim().toUpperCase();
  const orig = document.getElementById('sbgen-orig').value.trim().toUpperCase();
  const dest = document.getElementById('sbgen-dest').value.trim().toUpperCase();
  const route = document.getElementById('sbgen-route').value.trim();
  const airline = document.getElementById('sbgen-airline').value.trim().toUpperCase();
  const fltnum = document.getElementById('sbgen-fltnum').value.trim();
  const isoDate = document.getElementById('sbgen-date').value;
  const date = isoDateToDDMMMYY(isoDate);
  const rawTime = document.getElementById('sbgen-time').value.trim(); // "HH:MM" local wall-clock
  const reg = document.getElementById('sbgen-reg').value.trim().toUpperCase();
  let depH = '', depM = '';
  if(rawTime){
    const [h, m] = rawTime.split(':').map(Number);
    // sched_out is always local wall-clock now — simbrief_ofp.py converts
    // SimBrief's zulu epoch fields to local before they ever reach the leg
    // schema (previously it left them as zulu, which this code used to
    // treat as a signal to skip conversion for an already-dispatched leg;
    // that's no longer correct now that the field itself is fixed).
    if(_origTzName && isoDate){
      const z = localToZuluParts(isoDate, h, m, _origTzName);
      depH = z.h; depM = z.m;
    } else {
      depH = String(h).padStart(2, '0'); depM = String(m).padStart(2, '0');
    }
  }

  if(!user){ el.textContent = 'Set your SimBrief username in Settings first.'; el.style.color = '#c0392b'; return; }
  if(!orig || !dest){ el.textContent = 'Origin and destination are required.'; el.style.color = '#c0392b'; return; }
  if(!type){ el.textContent = 'Enter the SimBrief aircraft type code first.'; el.style.color = '#c0392b'; return; }
  localStorage.setItem('fos_simbrief_user', user);
  localStorage.setItem('fos_simbrief_airframe', type);
  fetch('/settings/simbrief-user', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})});

  // Everything not listed here (fuel, alternates, crew, output options...)
  // is set by the pilot on SimBrief's own dispatch page instead of
  // reimplemented as a control here — we only pre-fill what we actually
  // know from the pairing/leg data.
  const params = new URLSearchParams();
  const set = (k, v) => { if(v) params.set(k, v); };
  set('orig', orig); set('dest', dest); set('type', type); set('route', route);
  set('airline', airline); set('fltnum', fltnum); set('date', date); set('reg', reg);
  if(depH && depM){ set('deph', depH); set('depm', depM); }
  const url = 'https://dispatch.simbrief.com/options/custom?' + params.toString();

  // Must fire synchronously, before any await — Safari (and others) stop
  // treating this as user-initiated once you're a tick removed from the
  // actual click via an awaited fetch, and silently block it. We have
  // everything we need synchronously now (no signed request to fetch
  // first), so open straight to the real URL.
  //
  // A real <a target="_blank"> click, not window.open() — on a home-screen
  // installed iPad/iPhone PWA (standalone display mode, no browser chrome
  // to open a new tab in), window.open() routinely no-ops instead of
  // handing off to Safari. WebKit's standalone-mode webview does honor a
  // genuine anchor click with target="_blank" as an external-link intent
  // the way window.open() isn't reliably treated as. This degrades to an
  // ordinary new tab everywhere else window.open() already worked.
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);

  btn.disabled = true;
  el.textContent = 'Complete the flight plan on SimBrief’s dispatch page — this tab will pick it up automatically once you’re done.';
  el.style.color = '';

  // No signed request means no deterministic ofp_id to poll for, and no
  // window handle to watch for .closed (an <a> click doesn't hand one
  // back the way window.open() did) — so snapshot the account's current
  // OFP generation timestamp and poll straight away for it to change,
  // rather than waiting on any "they came back" signal at all.
  let beforeTs = '';
  try {
    const r = await fetch('/simbrief-api/generated-at?user=' + encodeURIComponent(user));
    beforeTs = (await r.json()).time_generated || '';
  } catch(e) { /* best-effort */ }

  pollSimbriefReady(user, beforeTs, el, btn, 0);
}

async function pollSimbriefReady(user, beforeTs, el, btn, attempt){
  // Polling starts the moment the SimBrief tab opens now (no "tab closed"
  // signal to wait on — see submitSimbriefGen), so this has to cover
  // however long someone takes filling out fuel/alternates/etc. on
  // SimBrief's own page, not just a quick confirm. ~15 minutes at 5s.
  if(attempt > 180){
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
    el.style.color = 'var(--blue-dark)';
    try {
      const r2 = await fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user, carry_gates_from: LEG_ID})});
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
  setTimeout(() => pollSimbriefReady(user, beforeTs, el, btn, attempt + 1), 5000);
}

let _weatherLoaded = false;
async function loadWeather(){
  const body = document.getElementById('weather-body');
  const userId = document.getElementById('release-user').value.trim();
  if(!userId){
    body.innerHTML = '<p class="placeholder-note">Send this flight to SimBrief first to get a username on file.</p>';
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
  else initOverviewPills();
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
