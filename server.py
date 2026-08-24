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
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import airportsdata
import requests
from flask import Flask, request, jsonify, Response, redirect, url_for
from flask_login import LoginManager, current_user, login_user, logout_user
from string import Template
import aeroapi
import pairing_edit
import pairing_engine
import pbs_build
import pbs_format
import pbs_parser
import release_engine
from fos_pages import AIRLINE_IATA, synthesize_crew
from models import db, User, Leg, PbsImport, PairingPack, SignatureLog, ReleaseCache, TripCheckIn
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


def _minutes_late(sched, est):
    """Signed minutes from sched to est on a 24h clock, without real
    calendar dates on both sides to fall back on. A flat HH:MM string/
    lexical compare broke on an overnight delay ("09:40" > "22:40" is
    False, so an 11h-late flight read as early/on-time); a symmetric
    +/-12h "shorter arc around the clock" fix broke again right at (and
    past) exactly 12h late, which wraps to reading as early instead —
    reported for an ~12h delay that still showed green. Real dispatch
    delays routinely run many hours; a flight silently running many hours
    *early* essentially never happens. So this window is deliberately
    asymmetric rather than +/-12h: up to 3h "early" (arbitrary but
    generous — a real early push is rarely more than minutes), and up to
    21h "late" before it wraps. Still a heuristic, not exact, given only
    time-of-day to work with — but strongly biased toward the shape real
    delays actually take instead of splitting the difference evenly."""
    try:
        sh, sm = (int(x) for x in sched.split(":"))
        eh, em = (int(x) for x in est.split(":"))
    except ValueError:
        return None
    diff = (eh * 60 + em) - (sh * 60 + sm)
    early_allowance = 180  # 3h
    return ((diff + early_allowance) % 1440) - early_allowance


def _station_time_html(pairing_sched, current_sched, est):
    """Overview's flight card, mobileCCI-style: the current known time in
    front, the pairing's original published time struck through next to it
    — but only when they actually differ. "Current known time" is a
    priority chain, not just est: est_out/est_in (a live SimBrief estimate)
    wins when present, otherwise current_sched (the generic sched_out/
    sched_in — whichever source wrote it last, so it reflects a redispatch
    even before SimBrief has a fresh estimate) wins over pairing_sched
    (the ORIGINAL PBS-pairing time, frozen the moment it was imported —
    see DEFAULT_LEG's pairing_sched_out/in comment). Getting this chain
    wrong is what silently kept showing the original pre-delay time after
    a redispatch: falling back straight to pairing_sched instead of
    current_sched skipped right past a newly-generated OFP's own updated
    schedule whenever it hadn't yet gotten a separate est_out."""
    pairing_sched = (pairing_sched or "").strip()
    current_sched = (current_sched or "").strip()
    est = (est or "").strip()
    shown = est or current_sched or pairing_sched
    if not shown:
        return ""
    minutes_late = _minutes_late(pairing_sched, shown) if (pairing_sched and shown) else None
    late = bool(minutes_late and minutes_late > 0)
    css_class = "est-time late" if late else "est-time"
    html_out = f'<span class="{css_class}">{html.escape(shown)}</span>'
    # Always render the second line when a pairing time exists — even when
    # it matches (on time) — instead of only when it differs. A card that
    # sometimes has one line and sometimes two threw off vertical alignment
    # against neighboring cards; struck-through only applies when the two
    # values actually diverge.
    if pairing_sched:
        superseded = " superseded" if shown != pairing_sched else ""
        html_out += f'<span class="sched-time{superseded}">{html.escape(pairing_sched)}</span>'
    return html_out


def _fmt_display_date(mmddyy):
    """"07/06/26" -> "JUL 06", mobileCCI-style. Blank in, blank out (a
    PBS-only leg has no real dep_date yet — see pbs_leg_to_fos_leg)."""
    mmddyy = (mmddyy or "").strip()
    if not mmddyy:
        return ""
    try:
        dt = datetime.strptime(mmddyy, "%m/%d/%y")
    except ValueError:
        return mmddyy
    return dt.strftime("%b %d").upper()


def _fmt_duration_hm(hhmm):
    """"02:27" -> "02h 27m". Blank/unparseable in, blank out."""
    hhmm = (hhmm or "").strip()
    try:
        h, m = hhmm.split(":")
        return f"{int(h):02d}h {int(m):02d}m"
    except ValueError:
        return hhmm


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


def _resolve_app_version():
    """Short git commit hash for whatever's actually running — Railway sets
    RAILWAY_GIT_COMMIT_SHA on every deploy automatically, so this updates
    itself with no manual bump. Falls back to asking git directly for local
    dev, where that env var isn't set."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    if not sha:
        try:
            import subprocess
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(__file__), stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            sha = ""
    return sha[:7] if sha else "dev"


APP_VERSION = _resolve_app_version()

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
    if "bid_shortcut" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN bid_shortcut JSON"))
        LOG.info("Migrated: added users.bid_shortcut")
    if "saved_pairings" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN saved_pairings JSON"))
        LOG.info("Migrated: added users.saved_pairings")
    if "bid_layers" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN bid_layers JSON"))
        LOG.info("Migrated: added users.bid_layers")
    if "timezone" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN timezone VARCHAR(64)"))
        LOG.info("Migrated: added users.timezone")
    if "active_seq" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN active_seq VARCHAR(32)"))
        LOG.info("Migrated: added users.active_seq")
    existing_pbs = {c["name"] for c in inspector.get_columns("pbs_imports")}
    if "pending_edits" not in existing_pbs:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE pbs_imports ADD COLUMN pending_edits JSON"))
        LOG.info("Migrated: added pbs_imports.pending_edits")


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
<script>(function(){var t=localStorage.getItem('fos_theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);})();</script>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<meta name="theme-color" content="#f5f5f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<title>$title – MobileCCI</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --blue:#0071e3; --blue-dark:#0058a8; --red:#ff3b30; --inactive:#9aa1ab;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7; --inactive:#636366; }
  }
  :root[data-theme="dark"]{ --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7; --inactive:#636366; }
  :root[data-theme="light"]{ --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f; --inactive:#9aa1ab; }
  html,body{overscroll-behavior:none;background:var(--bg);}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px;color:var(--value);}
  h1{font-size:18px;color:var(--blue-dark);margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:var(--value);}
  input[type=text],input[type=password]{width:100%;max-width:320px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid var(--border);border-radius:5px;box-sizing:border-box;background:var(--card);color:var(--value);}
  button{margin-top:16px;background:var(--blue);color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  .panel{max-width:320px;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:6px;margin-top:10px;}
  .msg{margin-top:8px;font-size:13px;color:var(--red);}
  .switch{margin-top:14px;font-size:13px;color:var(--value);}
  .switch a{color:var(--blue);}
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
    # pairing_sched_out/in are the PBS pairing's own published times — set
    # only by pbs_leg_to_fos_leg, never by SimBrief. sched_out/sched_in get
    # written by both sources, so once a SimBrief generate merges onto a
    # PBS-sourced leg (see _store_leg/_find), SimBrief's own self-reported
    # "sched_out" silently wins and the on-time comparison in
    # _station_time_html ends up checking SimBrief's estimate against
    # SimBrief's own schedule — never actually catching a real delay
    # against what was published. Keeping the pairing's time in a separate,
    # SimBrief-untouched field is what render_fos_html now compares against.
    "pairing_sched_out": "", "pairing_sched_in": "",
    "airline_iata": "", "airline_icao": "",
    "pending_date_slip": None,
    "est_out": "", "est_in": "", "dep_gate": "", "arr_gate": "",
    "fleet_type": "", "equipment_type": "", "tail_number": "", "tail_routing": "",
    "aircraft_name": "", "fin": "", "engines": "", "selcal": "", "seat_capacity": "",
    "oew": "", "max_zfw": "", "max_tow_struct": "", "max_ldw": "",
    "bookmarked_docs": [],
    "status": "", "customer_load": "", "position": "", "crew": [],
    "flight_time": "", "odl_time": "", "duty_time": "", "ground_time": "", "mot": "",
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


def _dates_match(a, b):
    """Same operational day, tolerating the ways this app's two leg sources
    disagree about "date": a PBS pairing leg has no real calendar date at
    all (pbs_leg_to_fos_leg leaves dep_date "" — see its own docstring), and
    a SimBrief-derived date can land a day off a PBS one across a
    zulu/local midnight rollover. Either side blank, or within a day, counts
    as the same flight."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return True
    try:
        da = datetime.strptime(a, "%m/%d/%y")
        db_ = datetime.strptime(b, "%m/%d/%y")
    except ValueError:
        return a == b
    return abs((da - db_).days) <= 1


def _find(flight_number, dep_date, origin=None, destination=None, seq=None):
    """The ORM row (not a plain dict) — only consumer is _store_leg, which
    needs to mutate/insert it. Exact flight_number+dep_date is the fast
    path; when that misses, fall back to flight_number+city pair with a
    tolerant date match (see _dates_match) so the same real flight imported
    from PBS (blank date) and then enriched via SimBrief (real date) syncs
    onto one row instead of leaving two "instances" of it around — the
    duplicate-flight bug this was built to close.

    seq disambiguates PBS-sourced legs specifically: a PBS leg's dep_date
    is always blank (no real calendar date exists in that data — see
    pbs_leg_to_fos_leg's own docstring), and the SAME flight number+route
    recurs across dozens of different SEQs for any daily-flown city pair
    (confirmed against real bid-pack data: flight 625 EWR-LAX alone
    appears in 150+ different SEQs in one pack). Without this, generating
    "flight 625" from one trip's SEQ would silently find and overwrite
    the Leg row already open for a DIFFERENT trip's "flight 625" a pilot
    had generated earlier — the exact "same page shows a different flight
    depending on how you got there" bug this closes. Only enforced when
    BOTH sides actually carry a seq (a SimBrief-only leg has none, and
    that pairing-with-SimBrief merge is exactly what this function exists
    to allow — see the docstring above)."""
    flight_number = (flight_number or "").strip()
    if not flight_number:
        return None
    query = Leg.query.filter_by(user_id=current_user.id, flight_number=flight_number)
    exact = query.filter_by(dep_date=dep_date).first()
    if exact and (not seq or not exact.data.get("seq") or exact.data.get("seq") == seq):
        return exact
    if not origin or not destination:
        return None
    # origin/destination live inside the JSON data blob, not as real
    # columns (see models.Leg) — filter in Python, not filter_by(). Compare
    # ICAO-normalized: a PBS-pairing leg's origin/destination are the
    # pairing's raw 3-letter station codes, while a SimBrief-enriched leg
    # carries the OFP's 4-letter ICAO codes — a raw string compare ("PHX"
    # vs "KPHX") never matches, so this fallback would silently never fire
    # for the exact case it exists to catch.
    origin_icao, dest_icao = _airport_icao(origin), _airport_icao(destination)
    for row in query.all():
        if _airport_icao(row.data.get("origin", "")) == origin_icao \
                and _airport_icao(row.data.get("destination", "")) == dest_icao \
                and _dates_match(row.dep_date, dep_date) \
                and (not seq or not row.data.get("seq") or row.data.get("seq") == seq):
            return row
    return None


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


_ofp_fetch_cache = {}  # simbrief_user -> (fetched_at, ofp_fields)
_OFP_CACHE_TTL = 15  # seconds — covers one "Generate & Cache All Legs" burst
# (several requests, same account, milliseconds apart) without re-fetching
# SimBrief's API once per leg for data that can't have changed between
# them; short enough that a genuinely fresh redispatch is still picked up
# well within the time it'd take to notice a stale card.


def _cached_ofp_fields(simbrief_user):
    now = time.monotonic()
    cached = _ofp_fetch_cache.get(simbrief_user)
    if cached and now - cached[0] < _OFP_CACHE_TTL:
        return cached[1]
    fields = simbrief_ofp.fetch_ofp_leg_fields(simbrief_user)
    _ofp_fetch_cache[simbrief_user] = (now, fields)
    return fields


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _store_leg(leg):
    """Shared by /generate and the PBS import path: dedupe-or-insert + archive."""
    leg = {**DEFAULT_LEG, **leg}
    leg.pop("id", None)
    existing = _find(leg.get("flight_number"), leg.get("dep_date"), leg.get("origin"), leg.get("destination"), leg.get("seq"))
    if existing:
        # Merge, don't overwrite: neither a PBS re-import nor a SimBrief
        # regenerate carries every field the other source does (PBS has
        # seq/position/hotel/gates-from-AeroAPI; SimBrief has tail/crew/
        # weights but no seq at all), and DEFAULT_LEG backfills whatever a
        # given source omits with "" — so a blank incoming value means
        # "this source doesn't know," not "clear it." A field the caller
        # actually meant to blank isn't a real scenario in this app: every
        # merge here only ever adds day-of-ops detail on top of a pairing,
        # never intentionally erases it. This previously bit signed_in/
        # fit_for_duty (a regenerate flipping a real attestation back off)
        # and gates (a re-import wiping an applied AeroAPI/SimBrief gate);
        # generalizing it here instead of re-special-casing field by field
        # every time this bug class resurfaces (most recently: seq).
        merged = dict(existing.data)
        for key, value in leg.items():
            if value not in ("", None, [], {}):
                merged[key] = value
        # signed_in/fit_for_duty are real booleans, not blankable strings —
        # False is meaningful, not "unknown" — so they need their own OR
        # instead of the generic rule above (which would just re-apply
        # existing.data's own value for a False leg.get() and never let a
        # fresh sign-in actually clear an old one, which is fine, but was
        # already explicit here before the refactor — kept explicit).
        merged["signed_in"] = existing.data.get("signed_in") or leg.get("signed_in", False)
        merged["fit_for_duty"] = existing.data.get("fit_for_duty") or leg.get("fit_for_duty", False)
        # A genuine date slip: this merge just moved dep_date off whatever
        # it was a moment ago (both sides real dates, not "" -> a date,
        # which is just the leg's first-ever date assignment, not a
        # slip). Flags it for the Overview lockout popup instead of
        # silently accepting it — a day-late reroute like the reported
        # PHX-RNO-after-a-diversion case shouldn't quietly become "normal"
        # without the pilot confirming it. Left alone (not re-derived) when
        # no new slip is found this round, so an already-pending one
        # survives an unrelated field update.
        old_dep_date = (existing.data.get("dep_date") or "").strip()
        new_dep_date = (merged.get("dep_date") or "").strip()
        if old_dep_date and new_dep_date and old_dep_date != new_dep_date:
            merged["pending_date_slip"] = {
                "old_dep_date": old_dep_date, "new_dep_date": new_dep_date,
                "new_sched_out": merged.get("sched_out", ""), "new_sched_in": merged.get("sched_in", ""),
            }
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
    # _find() below can't dedupe them into one. Everything the PBS pairing
    # carries that a SimBrief OFP has no field for at all — seq, position,
    # base, duty/ground time, hotel/limo, gates applied via AeroAPI, any
    # signature/attestation already given, bookmarks — would otherwise be
    # silently orphaned on the old row while current_leg_id moves to this
    # new one. This was the reported "Import PBS -> pick leg -> SimBrief ->
    # Generate" bug: Current Flight coming back later missing the pairing
    # (seq/schedule) because only gates were ever carried forward, not the
    # rest of the PBS-only context.
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
        # Only trust carry_from's fields (saved docs, gates, hotel, the
        # pairing baseline, etc.) when it's genuinely the SAME real flight
        # as what just got built — not just "whatever leg id the client
        # happened to be viewing." carry_gates_from is sent unconditionally
        # by every SimBrief sync/regenerate call; if the pilot's SimBrief
        # account turns out to have a different flight on it than the one
        # they were viewing (e.g. the flight number got changed on
        # SimBrief's own dispatch page before generating), blindly copying
        # src_row's fields would leak that other flight's saved docs onto
        # this one — the reported "stray docs from other legs" bug.
        if (
            src_row
            and src_row.data.get("flight_number") == leg.get("flight_number")
            and _airport_icao(src_row.data.get("origin", "")) == _airport_icao(leg.get("origin", ""))
            and _airport_icao(src_row.data.get("destination", "")) == _airport_icao(leg.get("destination", ""))
        ):
            for key in (
                "seq", "position", "base", "duty_time", "ground_time",
                "hotel_details", "limo_details", "dep_gate", "arr_gate",
                "bookmarked_docs", "signed_in", "fit_for_duty",
                "pairing_sched_out", "pairing_sched_in",
            ):
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


def _layover_indices(s):
    """Which positions in _sequence_routing(s)'s station chain are actual
    overnights — every day except the last ends in a layover. Indices, not
    station codes: a trip that overnights back at its own domicile (a real,
    if unusual, shape in this data — a duty period can end back at base
    with a HOTEL line before the next day's report) would otherwise have
    its domicile bolded everywhere the name recurs, including the trip's
    own origin/final destination, if this only matched by station name."""
    indices = []
    pos = 0  # stations[0] is the very first leg's origin; each leg after
    # that adds one more entry (its destination), so the cumulative leg
    # count through a day is exactly that day's last leg's index in
    # _sequence_routing(s)'s station list.
    days = s["duty_days"]
    for day in days[:-1]:
        pos += len(day["legs"])
        if day["legs"]:
            indices.append(pos)
    return indices


def _summarize_sequence(s):
    """The compact {seq, days, routing, ...} shape both /pbs/sequences and
    the pack-browsing routes list sequences with."""
    first_day = s["duty_days"][0]
    last_day = s["duty_days"][-1]
    first_leg = first_day["legs"][0] if first_day["legs"] else None
    last_leg = last_day["legs"][-1] if last_day["legs"] else None
    return {
        "seq": s["seq"], "days": len(s["duty_days"]),
        "positions": s["positions"], "ops_per_period": s["ops_per_period"],
        "report": first_day["report"],
        "origin": first_leg["origin"] if first_leg else None,
        "final_destination": last_leg["destination"] if last_leg else None,
        "routing": _sequence_routing(s),
        "layover_indices": _layover_indices(s),
        # Pairing totals from the bid-pack's own TTL row — cumulative for
        # the whole trip, distinct from any one day's own block/tpay/tafb.
        "block": s.get("block"), "tpay": s.get("tpay"), "tafb": s.get("tafb"),
    }


def _decorate_sequence(seq, operator_iata):
    """Full sequence detail with ICAO-decorated legs — the shape
    /pbs/sequences/<seq> and the pack sequence-detail route both return."""
    out = dict(seq)
    out["operator"] = _IATA_TO_ICAO.get(operator_iata, operator_iata)
    out["operator_iata"] = operator_iata
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
    return out


def _seq_max_legs_per_day(s):
    return max((len(d["legs"]) for d in s["duty_days"]), default=0)


def _seq_has_red_eye(s):
    """Any leg departing in the 22:00-04:00 local window — the same
    heuristic approved for this in the earlier bid-layers design pass."""
    for day in s["duty_days"]:
        for leg in day["legs"]:
            dep_local = leg.get("dep_local") or ""
            if len(dep_local) == 4 and dep_local.isdigit():
                hour = int(dep_local[:2])
                if hour >= 22 or hour < 4:
                    return True
    return False


def _layer_matches(seq, summary, properties):
    """True if `seq` (raw parsed sequence) satisfies a bid layer's saved
    filter criteria. `summary` is this seq's own _summarize_sequence()
    output, reused so routing/layover_indices aren't recomputed twice."""
    p = properties or {}
    days = summary["days"]
    if p.get("min_days") is not None and days < p["min_days"]:
        return False
    if p.get("max_days") is not None and days > p["max_days"]:
        return False
    if p.get("min_block") is not None and float(summary.get("block") or 0) < p["min_block"]:
        return False
    if p.get("min_tafb") is not None and float(summary.get("tafb") or 0) < p["min_tafb"]:
        return False
    if p.get("max_legs_per_day") is not None and _seq_max_legs_per_day(seq) > p["max_legs_per_day"]:
        return False
    # Tri-state: "any" (no filter, default) / "exclude" / "only". Reads
    # the new key first, falls back to the old boolean exclude_red_eye
    # for layers saved before this was a tri-state (still an "exclude"
    # meaning, so no data migration needed).
    red_eye_mode = p.get("red_eye")
    if red_eye_mode is None:
        red_eye_mode = "exclude" if p.get("exclude_red_eye") else "any"
    has_red_eye = _seq_has_red_eye(seq)
    if red_eye_mode == "exclude" and has_red_eye:
        return False
    if red_eye_mode == "only" and not has_red_eye:
        return False
    # Two independent axes: Layover Include is the strict, overnight-only
    # version ("I want to lay over in one of these cities"); Include/Avoid
    # are route-wide ("touches this station at all, same-day stop or
    # overnight, doesn't matter"). No route-wide "must overnight AND avoid"
    # pairing needed — Avoid already covers excluding a layover, since a
    # layover is itself a touch.
    layover_stations = {summary["routing"][i] for i in summary["layover_indices"]}
    layover_include = set(p.get("layover_include") or [])
    if layover_include and not (layover_stations & layover_include):
        return False
    route_stations = set(summary["routing"])
    include = set(p.get("include_stations") or [])
    if include and not (route_stations & include):
        return False
    avoid = set(p.get("avoid_stations") or [])
    if avoid and (route_stations & avoid):
        return False
    return True


def _bid_layer(layer_id):
    return next((l for l in (current_user.bid_layers or []) if l["id"] == layer_id), None)


# "(ALL)" as a value for opr/base/fleet — a layer (or a preview) can span
# every pack on that dimension instead of being pinned to one exact pack,
# e.g. every base for one operator/fleet, or genuinely every pack there
# is. Bare string sentinel (not None) so it round-trips through the same
# plain-string opr/base/fleet fields every other pack-scoped route uses.
ALL_SCOPE = "ALL"


def _packs_for_scope(opr, base, fleet):
    q = PairingPack.query
    if opr and opr != ALL_SCOPE:
        q = q.filter_by(opr=opr.upper())
    if base and base != ALL_SCOPE:
        q = q.filter_by(base=base.upper())
    if fleet and fleet != ALL_SCOPE:
        q = q.filter_by(fleet=fleet.upper())
    return q.all()


def _count_layer_matches(opr, base, fleet, properties):
    """Shared by the layer list (persisted layers) and the live preview
    (a not-yet-saved property set) — one pass over every pack the scope
    resolves to (one pack in the common case, more when any of
    opr/base/fleet is ALL_SCOPE)."""
    packs = _packs_for_scope(opr, base, fleet)
    if not packs:
        return None
    count = 0
    for pack in packs:
        for s in (pack.sequences or []):
            summary = _summarize_sequence(s)
            if _layer_matches(s, summary, properties):
                count += 1
    return count


@app.route("/pbs/layers/preview")
def preview_bid_layer():
    """Live count for the form, before a layer is saved — GET with query
    params so the frontend can debounce plain fetches, no JSON body typing
    needed for a handful of scalar/csv fields."""
    opr = (request.args.get("opr") or "").strip().upper()
    base = (request.args.get("base") or "").strip().upper()
    fleet = (request.args.get("fleet") or "").strip().upper()
    if not opr or not base or not fleet:
        return jsonify({"error": "opr, base, and fleet are all required"}), 400

    def _num(key):
        v = request.args.get(key)
        return float(v) if v not in (None, "") else None

    def _stations(key):
        v = request.args.get(key) or ""
        return [s.strip().upper() for s in v.split(",") if s.strip()]

    properties = {
        "min_days": _num("min_days"), "max_days": _num("max_days"),
        "min_block": _num("min_block"), "min_tafb": _num("min_tafb"),
        "max_legs_per_day": _num("max_legs_per_day"),
        "red_eye": request.args.get("red_eye") or "any",
        "layover_include": _stations("layover_include"),
        "include_stations": _stations("include_stations"),
        "avoid_stations": _stations("avoid_stations"),
    }
    count = _count_layer_matches(opr, base, fleet, properties)
    if count is None:
        return jsonify({"error": "no pack found for that operator/base/fleet"}), 404
    return jsonify({"count": count})


@app.route("/pbs/layers")
def list_bid_layers():
    layers = current_user.bid_layers or []
    out = []
    for layer in layers:
        count = _count_layer_matches(layer["opr"], layer["base"], layer["fleet"], layer.get("properties"))
        out.append({**layer, "count": count or 0})
    return jsonify(out)


@app.route("/pbs/layers", methods=["POST"])
def create_bid_layer():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    opr = (body.get("opr") or "").strip().upper()
    base = (body.get("base") or "").strip().upper()
    fleet = (body.get("fleet") or "").strip().upper()
    if not name or not opr or not base or not fleet:
        return jsonify({"error": "name, opr, base, and fleet are all required"}), 400
    if not _packs_for_scope(opr, base, fleet):
        return jsonify({"error": "no pack found for that operator/base/fleet"}), 404
    layer = {
        "id": uuid.uuid4().hex[:8], "name": name,
        "opr": opr, "base": base, "fleet": fleet,
        "properties": body.get("properties") or {},
    }
    layers = list(current_user.bid_layers or [])
    layers.append(layer)
    current_user.bid_layers = layers
    db.session.commit()
    return jsonify(layer)


@app.route("/pbs/layers/<layer_id>", methods=["PUT"])
def update_bid_layer(layer_id):
    layers = current_user.bid_layers or []
    idx = next((i for i, l in enumerate(layers) if l["id"] == layer_id), None)
    if idx is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    # Build a NEW dict rather than mutating layers[idx] in place — bid_layers
    # is a plain (unwrapped) JSON column, so SQLAlchemy only notices a
    # change via a genuinely different value on attribute assignment.
    # layers[idx] is the same dict object already referenced by
    # current_user.bid_layers; mutating its fields in place and then
    # reassigning current_user.bid_layers = layers left old/new comparing
    # equal (the "old" side had already been mutated too, same object) —
    # the update silently never persisted, even though this route's own
    # response looked correct (it just echoed the in-memory dict).
    updated = dict(layers[idx])
    if "name" in body:
        updated["name"] = (body.get("name") or "").strip() or updated["name"]
    if "properties" in body:
        updated["properties"] = body.get("properties") or {}
    new_layers = list(layers)
    new_layers[idx] = updated
    current_user.bid_layers = new_layers
    db.session.commit()
    return jsonify(updated)


@app.route("/pbs/layers/<layer_id>", methods=["DELETE"])
def delete_bid_layer(layer_id):
    layers = list(current_user.bid_layers or [])
    remaining = [l for l in layers if l["id"] != layer_id]
    if len(remaining) == len(layers):
        return jsonify({"error": "not found"}), 404
    current_user.bid_layers = remaining
    db.session.commit()
    return jsonify({"ok": True})


_SORT_KEYS = {"seq", "days", "block", "tafb", "tpay"}


def _sort_summaries(summaries, sort):
    """sort is a key name from _SORT_KEYS, optionally prefixed "-" for
    descending (e.g. "-block"). Missing/non-numeric values sort last
    regardless of direction, rather than crashing or landing first."""
    if not sort:
        return summaries
    desc = sort.startswith("-")
    key = sort[1:] if desc else sort
    if key not in _SORT_KEYS:
        return summaries

    def sort_val(s):
        v = s.get(key)
        try:
            fv = float(v)
            return (0, -fv if desc else fv)
        except (TypeError, ValueError):
            return (1, 0.0)
    return sorted(summaries, key=sort_val)


@app.route("/pbs/layers/<layer_id>/pairings")
def list_bid_layer_pairings(layer_id):
    layer = _bid_layer(layer_id)
    if not layer:
        return jsonify({"error": "not found"}), 404
    packs = _packs_for_scope(layer["opr"], layer["base"], layer["fleet"])
    if not packs:
        return jsonify({"error": "no pack found for that operator/base/fleet"}), 404
    matches = []
    for pack in packs:
        for s in (pack.sequences or []):
            summary = _summarize_sequence(s)
            if _layer_matches(s, summary, layer.get("properties")):
                # Tagged per-match rather than trusting the layer's own
                # opr/base/fleet — those can be ALL_SCOPE, spanning
                # several packs, so each result needs its own real pack
                # identity for detail/promote navigation.
                summary["opr"], summary["base"], summary["fleet"] = pack.opr, pack.base, pack.fleet
                matches.append(summary)
    matches = _sort_summaries(matches, request.args.get("sort"))
    return jsonify({"layer": layer, "pairings": matches})


@app.route("/pbs/sequences")
def list_pbs_sequences():
    return jsonify([
        {**_summarize_sequence(s), "active": s["seq"] == current_user.active_seq}
        for s in _pbs_sequences()
    ])


@app.route("/pbs/sequences", methods=["DELETE"])
def clear_pbs_sequences():
    """Clears this pilot's whole PBS import — the "start over" option
    alongside deleting one sequence at a time below."""
    row = _pbs_row()
    if row:
        db.session.delete(row)
        current_user.active_seq = None
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/pbs/sequences/tidy", methods=["POST"])
def tidy_pbs_sequences():
    """Bulk-declutters 'My Trips' — drops every promoted/pasted sequence
    from the active pool EXCEPT ones bookmarked in the Pairing Library
    (User.saved_pairings) or already flown at least one leg of (a real Leg
    row stamped with that seq number — "cached" via Generate/Generate &
    Cache All Legs). Leaves in-progress and saved-for-later trips alone,
    clears everything else a pilot promoted-then-forgot-about."""
    row = _pbs_row()
    if not row or not row.sequences:
        return jsonify({"ok": True, "removed": 0, "kept": 0})

    saved_seqs = {p.get("seq") for p in (current_user.saved_pairings or [])}
    if current_user.active_seq:
        saved_seqs.add(current_user.active_seq)
    cached_seqs = {
        leg.data.get("seq") for leg in Leg.query.filter_by(user_id=current_user.id).all()
        if leg.data.get("seq")
    }
    keep_seqs = saved_seqs | cached_seqs

    before = len(row.sequences)
    row.sequences = [s for s in row.sequences if s["seq"] in keep_seqs]
    db.session.commit()
    return jsonify({"ok": True, "removed": before - len(row.sequences), "kept": len(row.sequences)})


@app.route("/pbs/sequences/<seq_number>", methods=["DELETE"])
def delete_pbs_sequence(seq_number):
    row = _pbs_row()
    if not row:
        return jsonify({"error": "not found"}), 404
    remaining = [s for s in (row.sequences or []) if s["seq"] != seq_number]
    if len(remaining) == len(row.sequences or []):
        return jsonify({"error": "not found"}), 404
    row.sequences = remaining
    if current_user.active_seq == seq_number:
        current_user.active_seq = None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/pbs/sequences/<seq_number>")
def get_pbs_sequence(seq_number):
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404
    # A promoted-from-the-library sequence carries its own pack's operator
    # (see promote_pack_sequence) — prefer that over PbsImport.meta, which
    # is one shared value for the whole pool and goes stale the moment two
    # sequences from different packs/operators sit in that pool together.
    operator_iata = (seq.get("_pack_opr") or _pbs_meta().get("operator") or "").upper()
    out = _decorate_sequence(seq, operator_iata)
    out["active"] = (seq_number == current_user.active_seq)
    return jsonify(out)


@app.route("/pbs/sequences/<seq_number>/mot-log")
def sequence_mot_log(seq_number):
    """Every leg in this sequence with its own MOT, whether it's been
    generated yet, and — if so — when its FFD was signed (or not). Tapping
    the MOT time on Overview opens this."""
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404

    # Same identity key _duty_day_for_leg() already matches a flown Leg
    # row against its origin sequence with, just built the other way
    # around here (bulk keyed lookup instead of one at a time).
    rows_by_key = {}
    for row in Leg.query.filter_by(user_id=current_user.id).all():
        if row.data.get("seq") != seq_number:
            continue
        key = (
            (row.data.get("flight_number") or "").strip(),
            _airport_icao(row.data.get("origin", "")),
            _airport_icao(row.data.get("destination", "")),
        )
        rows_by_key[key] = row

    legs_out = []
    for day in seq.get("duty_days") or []:
        fdp_end = pbs_parser.fdp_end_for_day(day)
        fdp_remaining = _fdp_remaining_display(fdp_end, (day.get("legs") or [{}])[0].get("origin", ""))
        for i, leg in enumerate(day.get("legs") or []):
            key = (leg.get("flight_number", ""), _airport_icao(leg.get("origin", "")), _airport_icao(leg.get("destination", "")))
            row = rows_by_key.get(key)
            mot = pbs_parser.mot_for_leg(day, i)
            legs_out.append({
                "duty_day": day["duty_day"], "flight_number": leg.get("flight_number"),
                "origin": leg.get("origin"), "destination": leg.get("destination"),
                "mot_display": _mot_display(mot or "", leg.get("origin") or "", current_user.timezone),
                "fdp_remaining": fdp_remaining,
                "generated": row is not None,
                "signed_at": (row.data.get("signed_at") if row else "") or "",
                "fit_for_duty": bool(row.data.get("fit_for_duty")) if row else False,
            })
    return jsonify({"seq": seq_number, "legs": legs_out})


@app.route("/pbs/sequences/<seq_number>/pick-up", methods=["POST"])
def pick_up_sequence(seq_number):
    """Marks this sequence as the pilot's one active trip — a plain
    one-tap check-in, no signature (that requirement was cut: FFD already
    covers the real per-leg attestation, and Pick Up needs to work
    independently of whether legs have been generated to SimBrief yet, in
    either order). Refuses to pick up a second trip while one's already
    active. Still logs a TripCheckIn row for the audit trail."""
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404
    if current_user.active_seq and current_user.active_seq != seq_number:
        return jsonify({"error": f"Close SEQ {current_user.active_seq} before picking up a new trip"}), 400

    signed_at = datetime.now(timezone.utc).isoformat()
    db.session.add(TripCheckIn(user_id=current_user.id, seq=seq_number, signed_at=signed_at))
    current_user.active_seq = seq_number
    db.session.commit()
    LOG.info(f"TRIP CHECK-IN seq={seq_number} user={current_user.id} at={signed_at}")
    return jsonify({"ok": True, "active_seq": seq_number, "signed_at": signed_at})


@app.route("/pbs/sequences/<seq_number>/close", methods=["POST"])
def close_sequence(seq_number):
    if current_user.active_seq != seq_number:
        return jsonify({"error": "this isn't your active trip"}), 400
    current_user.active_seq = None
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Pairing Library — bulk-imported PBS bid-pack files, browsable
# opr -> base -> fleet -> sequence, distinct from the pilot's own active
# PbsImport.sequences (what they're actually flying). Picking one sequence
# "promotes" a copy into that active pool via the exact same append-based
# mechanism /pairings/accept already uses, so a promoted sequence is
# indistinguishable from any other imported/generated one downstream.
#
# Packs are shared across every pilot on this instance, not siloed per
# account — one admin (via bulk_import_packs.py) uploads the airline's bid
# packs once and every pilot's Pairing Library sees the same browsable
# set. `user_id` on the row records who uploaded it (audit trail only);
# nothing here filters by it.
# ---------------------------------------------------------------------------
def _pack_row(opr, base, fleet):
    return PairingPack.query.filter_by(
        opr=opr.upper(), base=base.upper(), fleet=fleet.upper(),
    ).first()


@app.route("/pbs/packs/import", methods=["POST"])
def import_pack():
    """Body: {opr, base, fleet, text}. One bulk-generated bid-pack file at
    a time — re-importing the same opr/base/fleet replaces just that pack,
    leaving every other pack untouched (unlike /import-pbs, which replaces
    the pilot's whole active sequence list)."""
    body = request.get_json(silent=True) or {}
    opr = (body.get("opr") or "").strip().upper()
    base = (body.get("base") or "").strip().upper()
    fleet = (body.get("fleet") or "").strip().upper()
    text = body.get("text") or ""
    if not opr or not base or not fleet:
        return jsonify({"error": "opr, base, and fleet are all required"}), 400
    if not text.strip():
        return jsonify({"error": "empty text — nothing to import"}), 400

    meta = pbs_parser.parse_pbs_meta(text)
    sequences = pbs_parser.parse_pbs(text)
    if not sequences:
        return jsonify({"error": "no sequences found in that text"}), 400

    row = _pack_row(opr, base, fleet)
    if not row:
        row = PairingPack(user_id=current_user.id, opr=opr, base=base, fleet=fleet)
        db.session.add(row)
    row.meta = meta
    row.sequences = sequences
    row.seq_count = len(sequences)
    db.session.commit()
    return jsonify({
        "opr": opr, "base": base, "fleet": fleet,
        "sequences_parsed": len(sequences),
        "legs_parsed": sum(len(d["legs"]) for s in sequences for d in s["duty_days"]),
    })


@app.route("/pbs/packs")
def list_packs():
    """Every pack on the instance, not just this pilot's own uploads —
    see the note above _pack_row. Column-limited on purpose — never
    touches any pack's own (large) `sequences` JSON column just to list
    opr/base/fleet/count."""
    rows = (
        db.session.query(PairingPack.opr, PairingPack.base, PairingPack.fleet, PairingPack.seq_count)
        .order_by(PairingPack.opr, PairingPack.base, PairingPack.fleet)
        .all()
    )
    return jsonify([{"opr": r[0], "base": r[1], "fleet": r[2], "seq_count": r[3]} for r in rows])


@app.route("/pbs/packs/<opr>/<base>/<fleet>/sequences")
def list_pack_sequences(opr, base, fleet):
    row = _pack_row(opr, base, fleet)
    if not row:
        return jsonify({"error": "pack not found"}), 404
    return jsonify([_summarize_sequence(s) for s in (row.sequences or [])])


@app.route("/pbs/packs/<opr>/<base>/<fleet>/sequences/<seq_number>")
def get_pack_sequence(opr, base, fleet, seq_number):
    row = _pack_row(opr, base, fleet)
    if not row:
        return jsonify({"error": "pack not found"}), 404
    seq = next((s for s in (row.sequences or []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404
    return jsonify(_decorate_sequence(seq, opr.upper()))


@app.route("/pbs/packs/<opr>/<base>/<fleet>/sequences/<seq_number>/promote", methods=["POST"])
def promote_pack_sequence(opr, base, fleet, seq_number):
    pack = _pack_row(opr, base, fleet)
    if not pack:
        return jsonify({"error": "pack not found"}), 404
    seq = next((s for s in (pack.sequences or []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404

    row = _pbs_row()
    if not row:
        row = PbsImport(user_id=current_user.id, meta={"operator": opr.upper(), "base": base.upper()})
        db.session.add(row)
    elif not row.meta:
        row.meta = {"operator": opr.upper(), "base": base.upper()}

    # Stamp this copy with the pack it actually came from — PbsImport.meta
    # is one shared operator/base/fleet for the whole pool, which goes
    # stale the moment a second promote comes from a *different* pack (the
    # airline code shown on a generated leg was silently inheriting
    # whichever pack happened to set row.meta first, not the one this
    # sequence was actually promoted from).
    stamped = {**seq, "_pack_opr": opr.upper(), "_pack_base": base.upper(), "_pack_fleet": fleet.upper()}
    # Re-promoting an already-present seq overwrites it rather than no-op —
    # deliberately, so re-promoting after the pack itself gets refreshed
    # (a newer pbs_parser fix, corrected upstream data, etc.) is how a
    # pilot un-stales a pairing they already promoted, not a dead end.
    existing = row.sequences or []
    row.sequences = [s for s in existing if s["seq"] != seq_number] + [stamped]
    db.session.commit()
    return jsonify(seq)


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

    # Prefer this sequence's own pack stamp (promote_pack_sequence) over
    # PbsImport.meta — see get_pbs_sequence's identical fix for why a
    # single pool-wide meta goes stale once sequences from more than one
    # operator/pack sit in the same active pool.
    meta = dict(_pbs_meta() or {})
    if seq.get("_pack_opr"):
        meta["operator"] = seq["_pack_opr"]
        meta["base"] = seq.get("_pack_base") or meta.get("base")
        meta["fleet"] = seq.get("_pack_fleet") or meta.get("fleet")
    fos_leg = pbs_parser.pbs_leg_to_fos_leg(meta, seq, day, leg, position)

    existing = _find(
        fos_leg.get("flight_number"), fos_leg.get("dep_date"),
        fos_leg.get("origin"), fos_leg.get("destination"), fos_leg.get("seq"),
    )

    # Before touching anything, check whether the pilot's CURRENT SimBrief
    # OFP genuinely *is* this pairing leg (same flight number + city pair
    # — "part of the sequence", not some unrelated flight sitting on the
    # account) rather than blindly trusting the raw bid-pack baseline.
    # This catches a redispatch done straight on SimBrief's own site
    # (never routed back through this app's own Generate button) even
    # when clicking in through the pairing/pill strip. _cached_ofp_fields
    # avoids re-fetching per leg during "Generate & Cache All Legs".
    simbrief_user = body.get("simbrief_user") or current_user.default_simbrief_user
    ofp_match = None
    if simbrief_user:
        try:
            ofp_fields = _cached_ofp_fields(simbrief_user)
        except Exception as e:
            ofp_fields = None
            LOG.warning(f"SimBrief OFP check failed for {simbrief_user}: {e}")
        if (
            ofp_fields
            and ofp_fields.get("flight_number") == fos_leg.get("flight_number")
            and _airport_icao(ofp_fields.get("origin", "")) == _airport_icao(fos_leg.get("origin", ""))
            and _airport_icao(ofp_fields.get("destination", "")) == _airport_icao(fos_leg.get("destination", ""))
        ):
            ofp_match = ofp_fields

    if ofp_match:
        fos_leg = {**fos_leg, **ofp_match}
        fos_leg["signed_in"] = True
    elif existing:
        # No fresher SimBrief data for this specific leg right now — don't
        # let the raw pairing baseline (which pbs_leg_to_fos_leg always
        # emits non-blank) stomp whatever richer data this leg already
        # has via _store_leg's non-blank-always-wins merge. Reported bug:
        # switching to a sibling pill and back (or re-running "Generate &
        # Cache All Legs") silently reverted an already-redispatched leg
        # to its original pre-delay schedule.
        current_user.current_leg_id = existing.id
        db.session.commit()
        return jsonify({"fos_url": f"/fos/{existing.id}", "id": existing.id})

    fos_leg.pop("_ofp_error", None)
    record = _store_leg(fos_leg)
    return jsonify({"fos_url": f"/fos/{record['id']}", "id": record["id"]})


# ---------------------------------------------------------------------------
# In-app pairing generation — searches the NAC route network for legal
# pairings (pairing_engine, ported from the standalone nac_pairings.py
# builder) instead of only importing a PBS bid-pack text export. Generation
# is a two-step propose/accept: /generate runs the (budget-limited) search
# and returns candidates without persisting anything; /accept re-verifies
# the chosen candidate and appends it to PbsImport.sequences via the same
# to_pbs() -> pbs_format text -> pbs_parser.parse_pbs() round trip a real
# PBS import goes through, so a generated sequence is byte-for-byte the same
# shape as an imported one everywhere downstream.
# ---------------------------------------------------------------------------
def _chain_routing(legs, chain):
    stations = [legs[chain[0]]["o"]]
    for i in chain:
        stations.append(legs[i]["d"])
    return stations


def _next_generated_seq_number(existing_seqs):
    """First free integer >= 9001 — visually distinct from a real bid pack's
    1-999 sequence numbers."""
    used = {int(s["seq"]) for s in existing_seqs if str(s.get("seq", "")).isdigit()}
    n = 9001
    while n in used:
        n += 1
    return str(n)


@app.route("/pairings/generate", methods=["POST"])
def generate_pairings():
    body = request.get_json(silent=True) or {}
    base = (body.get("base") or "").strip().upper()
    if not base:
        return jsonify({"error": "base is required"}), 400
    try:
        days = int(body.get("days", 4))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    if days < 1:
        return jsonify({"error": "days must be at least 1"}), 400
    try:
        budget = min(float(body.get("budget", 5)), 10.0)
    except (TypeError, ValueError):
        budget = 5.0
    try:
        limit = min(int(body.get("limit", 5)), 8)
    except (TypeError, ValueError):
        limit = 5

    legs, ap = pairing_engine.get_route_data()
    if not any(l["o"] == base for l in legs):
        return jsonify({"error": f"no departures from {base} in the route network"}), 400

    search = pairing_engine.Search(legs, ap, days, budget)
    chains = search.run(base, exact_days=True)
    candidates = []
    for chain in chains:
        if pairing_engine.verify(legs, ap, chain, base, days):
            continue
        steps, _ = pairing_engine.walk(legs, ap, chain)
        block = sum(legs[i]["blk"] for i in chain)
        lpd = pairing_engine.legs_per_day(steps)
        candidates.append({
            "chain": list(chain),
            "block": round(block, 2),
            "dacv": round(block / days, 3) if days else 0,
            "legs_per_day": lpd,
            "routing": _chain_routing(legs, chain),
            "shape_ok": pairing_engine.shape_ok(lpd),
        })
    # Score by efficiency (block/day), not raw block — maximizing raw block
    # is the diagnosed bug that made an older version of this builder always
    # pick the longest constructible trip regardless of how much dead time
    # it carried.
    candidates.sort(key=lambda c: -c["dacv"])
    return jsonify({"base": base, "days": days, "candidates": candidates[:limit]})


@app.route("/pairings/accept", methods=["POST"])
def accept_pairing():
    body = request.get_json(silent=True) or {}
    base = (body.get("base") or "").strip().upper()
    chain = body.get("chain")
    if not base or not isinstance(chain, list) or not chain:
        return jsonify({"error": "base and chain are required"}), 400
    try:
        chain = [int(i) for i in chain]
    except (TypeError, ValueError):
        return jsonify({"error": "chain must be a list of leg indices"}), 400

    legs, ap = pairing_engine.get_route_data()
    try:
        steps, _ = pairing_engine.walk(legs, ap, chain)
    except (IndexError, KeyError):
        return jsonify({"error": "chain references an unknown leg — route network may have changed"}), 400
    days_walked = steps[-1]["day"]
    problems = pairing_engine.verify(legs, ap, chain, base, days_walked)
    if problems:
        return jsonify({"error": "candidate failed re-verification: " + "; ".join(problems)}), 400

    row = _pbs_row()
    if not row:
        row = PbsImport(user_id=current_user.id, meta={"operator": "NAC", "fleet": "320", "base": base})
        db.session.add(row)
    elif not row.meta:
        row.meta = {"operator": "NAC", "fleet": "320", "base": base}

    seq_no = _next_generated_seq_number(row.sequences or [])
    now = datetime.now(timezone.utc)
    period = pbs_format.bid_period(now.year, now.month)
    lines, _ops = pbs_build.to_pbs(legs, ap, chain, base, int(seq_no), 0, period)
    text = pbs_format.build_text(
        [lines], base, ap.city(base).upper(), now.strftime("%B %Y").upper(), period,
    )
    parsed = pbs_parser.parse_pbs(text)
    if not parsed:
        return jsonify({"error": "internal error: could not build sequence text"}), 500
    new_sequence = parsed[0]
    row.sequences = (row.sequences or []) + [new_sequence]
    db.session.commit()
    return jsonify(new_sequence)


@app.route("/pbs/sequences/<seq_number>/legs/<int:duty_day>/<int:leg_index>/propose-edit", methods=["POST"])
def propose_leg_edit(seq_number, duty_day, leg_index):
    row = _pbs_row()
    seq = next((s for s in (row.sequences if row else []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    new_destination = body.get("new_destination")
    flight_number = body.get("flight_number") or None
    manual = body.get("manual") or None
    if not new_destination:
        return jsonify({"error": "new_destination is required"}), 400

    legs, ap = pairing_engine.get_route_data()
    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""

    new_seq, violations, meta = pairing_edit.apply_leg_edit(
        seq, dom, ap, legs, duty_day, leg_index,
        new_destination=new_destination, flight_number=flight_number, manual=manual,
    )
    if new_seq is None:
        return jsonify({"error": "; ".join(violations) or "could not apply that edit"}), 400

    pending = dict(row.pending_edits or {})
    pending[seq_number] = {"sequence": new_seq, "violations": violations, "meta": meta}
    row.pending_edits = pending
    db.session.commit()
    return jsonify({"legal": not violations, "violations": violations, "preview": new_seq, "meta": meta})


@app.route("/pbs/sequences/<seq_number>/legs/resolve-edit", methods=["POST"])
def resolve_leg_edit(seq_number):
    row = _pbs_row()
    if not row:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("confirm", "reject"):
        return jsonify({"error": "action must be 'confirm' or 'reject'"}), 400
    staged = (row.pending_edits or {}).get(seq_number)
    if not staged:
        return jsonify({"error": "no pending edit for this sequence"}), 404

    if action == "confirm":
        row.sequences = [
            staged["sequence"] if s["seq"] == seq_number else s
            for s in (row.sequences or [])
        ]
    pending = dict(row.pending_edits or {})
    pending.pop(seq_number, None)
    row.pending_edits = pending
    db.session.commit()
    return jsonify(staged["sequence"] if action == "confirm" else {"ok": True})


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


@app.route("/fos/<int:leg_id>/resolve-date-slip", methods=["POST"])
def resolve_date_slip(leg_id):
    """Answers the full-screen lockout popup _store_leg's date-slip
    detection triggers. Confirm hard-overwrites pairing_sched_out/in (the
    strikethrough/on-time baseline — see DEFAULT_LEG) with the new day's
    time, so the redispatched schedule becomes the accepted one instead of
    permanently reading as "late" against a schedule that no longer
    applies. Reject just dismisses the popup — dep_date/sched_out already
    reflect the new day either way (that happened at merge time, before
    this popup ever shows), only the pairing baseline is what's gated on
    this decision."""
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("confirm", "reject"):
        return jsonify({"error": "action must be 'confirm' or 'reject'"}), 400
    slip = row.data.get("pending_date_slip")
    new_data = dict(row.data)
    if action == "confirm" and slip:
        new_data["pairing_sched_out"] = slip.get("new_sched_out") or new_data.get("pairing_sched_out")
        new_data["pairing_sched_in"] = slip.get("new_sched_in") or new_data.get("pairing_sched_in")
    new_data["pending_date_slip"] = None
    data = _save_leg(row, new_data)
    return jsonify({"id": data["id"], "pairing_sched_out": data.get("pairing_sched_out"), "pairing_sched_in": data.get("pairing_sched_in")})


# ---------------------------------------------------------------------------
# Mid-trip recovery — the leg currently being viewed diverted/overran and the
# rest of its pairing is no longer trustworthy. Finds a legal way back to
# base from wherever the pilot actually ended up (pairing_edit.py, built on
# pairing_engine.Search.run_from), instead of the only previous option —
# hand-editing the raw PBS text and re-importing it.
# ---------------------------------------------------------------------------
def _duty_day_for_leg(seq, leg_row):
    """Which (duty_day, leg_index) in `seq` this flown Leg row came from —
    matched the same ICAO-normalized way _find()/carry_from already compare
    a PBS-pairing leg's raw station codes against a SimBrief-enriched leg's
    OFP codes, since Leg rows don't carry their duty_day/leg_index directly."""
    flight_number = (leg_row.data.get("flight_number") or "").strip()
    origin_icao = _airport_icao(leg_row.data.get("origin", ""))
    dest_icao = _airport_icao(leg_row.data.get("destination", ""))
    for day in seq.get("duty_days") or []:
        for i, leg in enumerate(day.get("legs") or []):
            if (
                leg.get("flight_number") == flight_number
                and _airport_icao(leg.get("origin", "")) == origin_icao
                and _airport_icao(leg.get("destination", "")) == dest_icao
            ):
                return day["duty_day"], i
    return None, None


@app.route("/fos/<int:leg_id>/recover", methods=["POST"])
def recover_leg(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    seq_number = row.data.get("seq")
    if not seq_number:
        return jsonify({"error": "this leg has no SEQ — nothing to recover into"}), 400
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found — it may have been deleted"}), 404

    body = request.get_json(silent=True) or {}
    actual_destination = (body.get("actual_destination") or "").strip().upper()
    actual_arrival_local = body.get("actual_arrival_local")
    if not actual_destination or not actual_arrival_local:
        return jsonify({"error": "actual_destination and actual_arrival_local are required"}), 400

    duty_day, leg_index = _duty_day_for_leg(seq, row)
    if duty_day is None:
        return jsonify({"error": "couldn't match this flight to a leg in its own sequence"}), 400

    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""
    if not dom:
        return jsonify({"error": "sequence has no origin to recover back to"}), 400

    legs, ap = pairing_engine.get_route_data()
    try:
        budget = min(float(body.get("budget", 8)), 10.0)
    except (TypeError, ValueError):
        budget = 8.0
    candidates, violations = pairing_edit.recover_from_disruption(
        seq, dom, ap, legs, duty_day, leg_index, actual_destination, actual_arrival_local, budget=budget,
    )
    if not candidates:
        return jsonify({"error": "; ".join(violations) or "no legal recovery found"}), 400
    return jsonify({
        "seq": seq_number, "duty_day": duty_day, "leg_index": leg_index,
        "actual_destination": actual_destination, "actual_arrival_local": actual_arrival_local,
        "candidates": candidates,
    })


@app.route("/fos/<int:leg_id>/recover/accept", methods=["POST"])
def recover_leg_accept(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    seq_number = row.data.get("seq")
    pbs_row = _pbs_row()
    seq = next((s for s in (pbs_row.sequences if pbs_row else []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    required = ("actual_destination", "actual_arrival_local", "chain", "day_number",
                "dlegs_today", "dblk_today", "duty_report_utc", "total_days")
    if any(body.get(k) is None for k in required):
        return jsonify({"error": f"missing one of: {', '.join(required)}"}), 400

    duty_day, leg_index = _duty_day_for_leg(seq, row)
    if duty_day is None:
        return jsonify({"error": "couldn't match this flight to a leg in its own sequence"}), 400

    legs, ap = pairing_engine.get_route_data()
    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""
    day = next((d for d in seq["duty_days"] if d["duty_day"] == duty_day), None)
    disrupted_leg = day["legs"][leg_index] if day else None
    actual_destination = (body.get("actual_destination") or "").strip().upper()
    chain = [int(i) for i in body["chain"]]

    # Defense in depth: re-verify the chosen chain before splicing it in —
    # same reasoning as /pairings/accept re-verifying a freshly generated
    # candidate — the client only sent back seed data from a prior stateless
    # /recover response, don't trust it blindly.
    try:
        _, actual_arrival_utc, _ = pairing_edit.anchor_arrival(
            ap, disrupted_leg, actual_destination, body["actual_arrival_local"],
        )
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": f"invalid disruption data: {e}"}), 400
    problems = pairing_engine.verify_from(
        legs, ap, chain, dom, actual_destination, actual_arrival_utc,
        int(body["day_number"]), int(body["dlegs_today"]), float(body["dblk_today"]),
        float(body["duty_report_utc"]), int(body["total_days"]),
    )
    if problems:
        return jsonify({"error": "candidate failed re-verification: " + "; ".join(problems)}), 400

    new_seq, errs = pairing_edit.apply_recovery(
        seq, dom, ap, legs, duty_day, leg_index,
        actual_destination, body["actual_arrival_local"],
        chain, int(body["day_number"]), int(body["dlegs_today"]),
        float(body["dblk_today"]), float(body["duty_report_utc"]), int(body["total_days"]),
    )
    if new_seq is None:
        return jsonify({"error": "; ".join(errs) or "could not apply recovery"}), 400

    pbs_row.sequences = [new_seq if s["seq"] == seq_number else s for s in pbs_row.sequences]
    db.session.commit()
    return jsonify(new_seq)


# ---------------------------------------------------------------------------
# "Timed out into rest" — the other common disruption shape: nothing
# diverted, a delay just pushed the rest of a duty day past legal limits, so
# the remainder gets pushed to the next day instead. Distinct from
# /recover above (which is for "ended up somewhere different"): this only
# needs to know when duty actually ended, not a different destination.
# Tries the cheapest fix first (replay the exact original plan, just
# later); falls back to a day-scoped search that still tries to reach the
# original day's own planned destination, preserving every day after it.
# ---------------------------------------------------------------------------
@app.route("/fos/<int:leg_id>/shift-day", methods=["POST"])
def shift_day(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    seq_number = row.data.get("seq")
    if not seq_number:
        return jsonify({"error": "this leg has no SEQ — nothing to shift"}), 400
    seq = next((s for s in _pbs_sequences() if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found — it may have been deleted"}), 404

    body = request.get_json(silent=True) or {}
    rest_start_local = body.get("rest_start_local")
    if not rest_start_local:
        return jsonify({"error": "rest_start_local is required"}), 400

    duty_day, leg_index = _duty_day_for_leg(seq, row)
    if duty_day is None:
        return jsonify({"error": "couldn't match this flight to a leg in its own sequence"}), 400

    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""
    if not dom:
        return jsonify({"error": "sequence has no origin to recover back to"}), 400

    legs, ap = pairing_engine.get_route_data()
    new_seq, failure = pairing_edit.retry_shifted_plan(seq, dom, ap, legs, duty_day, leg_index, rest_start_local)
    if new_seq is not None:
        return jsonify({"mode": "shifted", "seq": seq_number, "duty_day": duty_day,
                        "leg_index": leg_index, "rest_start_local": rest_start_local, "preview": new_seq})

    try:
        budget = min(float(body.get("budget", 8)), 10.0)
    except (TypeError, ValueError):
        budget = 8.0
    candidates, target_station, reached_target = pairing_edit.day_scoped_recovery(
        seq, dom, ap, legs, duty_day, leg_index, rest_start_local, budget=budget,
    )
    if not candidates:
        return jsonify({"error": "no legal way to continue from there, even settling for base"}), 400
    return jsonify({
        "mode": "day_patch", "seq": seq_number, "duty_day": duty_day, "leg_index": leg_index,
        "rest_start_local": rest_start_local, "target_station": target_station,
        "reached_target": reached_target, "candidates": candidates,
    })


@app.route("/fos/<int:leg_id>/shift-day/accept-shift", methods=["POST"])
def shift_day_accept_shift(leg_id):
    """Commits retry_shifted_plan's own (single, deterministic) result —
    re-run rather than trusting a client-cached copy, since it's cheap and
    the sequence may have changed since /shift-day was called."""
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    seq_number = row.data.get("seq")
    pbs_row = _pbs_row()
    seq = next((s for s in (pbs_row.sequences if pbs_row else []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    rest_start_local = body.get("rest_start_local")
    if not rest_start_local:
        return jsonify({"error": "rest_start_local is required"}), 400

    duty_day, leg_index = _duty_day_for_leg(seq, row)
    if duty_day is None:
        return jsonify({"error": "couldn't match this flight to a leg in its own sequence"}), 400
    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""

    legs, ap = pairing_engine.get_route_data()
    new_seq, failure = pairing_edit.retry_shifted_plan(seq, dom, ap, legs, duty_day, leg_index, rest_start_local)
    if new_seq is None:
        return jsonify({"error": "the shifted plan is no longer valid — search again"}), 400

    pbs_row.sequences = [new_seq if s["seq"] == seq_number else s for s in pbs_row.sequences]
    db.session.commit()
    return jsonify(new_seq)


@app.route("/fos/<int:leg_id>/shift-day/accept-patch", methods=["POST"])
def shift_day_accept_patch(leg_id):
    row = _get_leg_row(leg_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    seq_number = row.data.get("seq")
    pbs_row = _pbs_row()
    seq = next((s for s in (pbs_row.sequences if pbs_row else []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    required = ("rest_start_local", "chain", "day_number", "dlegs_today",
                "dblk_today", "duty_report_utc", "total_days", "target_station")
    if any(body.get(k) is None for k in required):
        return jsonify({"error": f"missing one of: {', '.join(required)}"}), 400
    reached_target = bool(body.get("reached_target"))

    duty_day, leg_index = _duty_day_for_leg(seq, row)
    if duty_day is None:
        return jsonify({"error": "couldn't match this flight to a leg in its own sequence"}), 400
    first_day = seq["duty_days"][0] if seq["duty_days"] else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day["legs"] else ""

    legs, ap = pairing_engine.get_route_data()
    chain = [int(i) for i in body["chain"]]

    # Defense in depth, same reasoning as /recover/accept — the client only
    # sent back seed data from a prior stateless /shift-day response, don't
    # trust it blindly. Re-derive the resume point the same way
    # day_scoped_recovery itself did and re-verify against it.
    try:
        day_idx = next(i for i, d in enumerate(seq["duty_days"]) if d["duty_day"] == duty_day)
        _kept, resume_station, earliest_report_utc = pairing_edit._prefix_rest_state(
            seq["duty_days"], day_idx, duty_day, leg_index, ap, body["rest_start_local"],
        )
    except (ValueError, KeyError, StopIteration) as e:
        return jsonify({"error": f"invalid disruption data: {e}"}), 400
    resume_utc = earliest_report_utc + pairing_engine.Rules.BRIEF
    problems = pairing_engine.verify_from(
        legs, ap, chain, body["target_station"], resume_station, resume_utc,
        int(body["day_number"]), int(body["dlegs_today"]), float(body["dblk_today"]),
        float(body["duty_report_utc"]), int(body["total_days"]),
    )
    if problems:
        return jsonify({"error": "candidate failed re-verification: " + "; ".join(problems)}), 400

    new_seq, errs = pairing_edit.apply_day_patch(
        seq, dom, ap, legs, duty_day, leg_index, body["rest_start_local"],
        chain, int(body["day_number"]), int(body["dlegs_today"]),
        float(body["dblk_today"]), float(body["duty_report_utc"]), int(body["total_days"]),
        reattach=reached_target,
    )
    if new_seq is None:
        return jsonify({"error": "; ".join(errs) or "could not apply patch"}), 400

    pbs_row.sequences = [new_seq if s["seq"] == seq_number else s for s in pbs_row.sequences]
    db.session.commit()
    return jsonify(new_seq)


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
    new_data = {**row.data, "signature": signature, "signed_at": signed_at}
    # Fit for Duty is a real attestation, not a bare checkbox — signing it
    # here sets the flag directly (not toggle_ffd's flip) so re-signing
    # can never accidentally turn an already-true declaration back off.
    if body.get("kind") == "ffd":
        new_data["fit_for_duty"] = True
    data = _save_leg(row, new_data)

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


def _gate_release_payload(payload, fit_for_duty):
    """A soft gate, not a hard block — the PDF still generates, caches,
    and can be VIEWED in-app either way (per the original "banner, not a
    block on generation" design). Only the fit_for_duty flag rides along;
    the frontend uses it to lock the actual Export/Download actions and
    show the floating warning banner, while the PDF bytes themselves stay
    in the payload so viewDoc() can still render the document."""
    return {**payload, "fit_for_duty": bool(fit_for_duty)}


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
        return jsonify({
            **_gate_release_payload(cached.payload, record.get("fit_for_duty")),
            "cached": True, "generated_at": cached.generated_at.isoformat(),
        })

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

    return jsonify({
        **_gate_release_payload(payload, record.get("fit_for_duty")),
        "cached": False, "generated_at": generated_at.isoformat(),
    })


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
    return jsonify({"status": "ok", "version": APP_VERSION, "archived_legs": Leg.query.count()})


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


@app.route("/settings/timezone", methods=["POST"])
def set_timezone():
    """Which clock face MOT (and any other local-time display) renders in
    — an IANA name, or blank to fall back to the bid pack's own local
    time with no conversion."""
    body = request.get_json(silent=True) or {}
    current_user.timezone = (body.get("timezone") or "").strip() or None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/settings/password", methods=["POST"])
def set_password():
    """Requires the current password (not just being logged in — a
    session cookie left signed-in on a shared/unlocked device shouldn't
    be enough on its own to take over the account) before setting a new
    one via the same set_password()/check_password() the login/register
    routes already use."""
    body = request.get_json(silent=True) or {}
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""
    if not current_user.check_password(current):
        return jsonify({"error": "Current password is incorrect"}), 400
    if len(new) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400
    current_user.set_password(new)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/settings/bid-shortcut")
def get_bid_shortcut():
    return jsonify(current_user.bid_shortcut or None)


@app.route("/settings/bid-shortcut", methods=["POST"])
def set_bid_shortcut():
    """Saves a default opr/base/fleet for the Pairing Library to jump
    straight to — e.g. "LGA 320/I". DELETE clears it back to no default."""
    body = request.get_json(silent=True) or {}
    opr = (body.get("opr") or "").strip().upper()
    base = (body.get("base") or "").strip().upper()
    fleet = (body.get("fleet") or "").strip().upper()
    if not opr or not base or not fleet:
        return jsonify({"error": "opr, base, and fleet are all required"}), 400
    label = (body.get("label") or "").strip() or f"{base} {fleet}"
    current_user.bid_shortcut = {"opr": opr, "base": base, "fleet": fleet, "label": label}
    db.session.commit()
    return jsonify(current_user.bid_shortcut)


@app.route("/settings/bid-shortcut", methods=["DELETE"])
def clear_bid_shortcut():
    current_user.bid_shortcut = None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/settings/saved-pairings")
def get_saved_pairings():
    return jsonify(current_user.saved_pairings or [])


@app.route("/settings/saved-pairings/toggle", methods=["POST"])
def toggle_saved_pairing():
    """Stars/unstars one pairing from the Library — same toggle-in-place
    shape as /docs/bookmark. Identity is (opr, base, fleet, seq); returns
    the updated list so the frontend can re-derive every star's state from
    one response, the same pattern toggleBookmark already uses."""
    body = request.get_json(silent=True) or {}
    opr = (body.get("opr") or "").strip().upper()
    base = (body.get("base") or "").strip().upper()
    fleet = (body.get("fleet") or "").strip().upper()
    seq = (body.get("seq") or "").strip()
    if not opr or not base or not fleet or not seq:
        return jsonify({"error": "opr, base, fleet, and seq are all required"}), 400
    saved = list(current_user.saved_pairings or [])
    key = {"opr": opr, "base": base, "fleet": fleet, "seq": seq}
    existing = next((p for p in saved if p["opr"] == opr and p["base"] == base and p["fleet"] == fleet and p["seq"] == seq), None)
    if existing:
        saved.remove(existing)
    else:
        saved.append(key)
    current_user.saved_pairings = saved
    db.session.commit()
    return jsonify(current_user.saved_pairings)


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


@app.route("/schedule")
def schedule_root():
    """The Schedule tab's own root — reachable from the bottom nav on every
    page (Home included), independent of any one leg. Reuses render_fos_html
    with an empty leg (id="") purely as a rendering vehicle: DEFAULT_LEG
    fills in every field, and the FOS_TEMPLATE JS checks `!LEG_ID` to know
    it's in this leg-independent mode — defaulting to the Schedule view,
    hiding the back chevron, and routing every other tab back to Home
    instead of trying to show some specific leg's Overview."""
    return Response(render_fos_html({"id": ""}), mimetype="text/html")


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
        current_leg_id=str(current_leg["id"]) if current_leg else "",
        current_leg_disabled="" if current_leg else " disabled",
        app_version=APP_VERSION,
    )


# A short, curated list rather than every IANA zone (airportsdata alone
# has thousands) — this is a per-pilot display preference, not an
# airport lookup, so a manageable picker matters more than completeness.
_TIMEZONE_CHOICES = [
    ("America/New_York", "Eastern"), ("America/Chicago", "Central"),
    ("America/Denver", "Mountain"), ("America/Phoenix", "Arizona"),
    ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"), ("UTC", "UTC"),
]


def _timezone_options_html(selected):
    opts = ['<option value=""' + (' selected' if not selected else '') + '>As Computed</option>']
    for tz_name, label in _TIMEZONE_CHOICES:
        sel = ' selected' if tz_name == selected else ''
        opts.append(f'<option value="{tz_name}"{sel}>{html.escape(label)} ({tz_name})</option>')
    return ''.join(opts)


def _mot_display(mot_raw, origin, pilot_tz):
    """MOT as HH:MM (L), shifted into the pilot's saved timezone when one's
    set. mot_raw is wall-clock local to `origin` (no real date exists in
    this data — see pbs_leg_to_fos_leg's own known-gap note — so this
    shifts by each zone's CURRENT utcoffset rather than doing full
    date-aware conversion; same reference-time approximation
    pairing_engine.Airports already uses for its own offset table)."""
    if len(mot_raw) != 4 or not mot_raw.isdigit():
        return "XX:XX (L)"
    if not pilot_tz:
        return f"{mot_raw[:2]}:{mot_raw[2:]} (L)"
    try:
        from datetime import datetime, timezone as dt_timezone
        from zoneinfo import ZoneInfo
        origin_tz_name = (_AIRPORT_TZ.get(_airport_icao(origin)) or {}).get("tz")
        if not origin_tz_name:
            return f"{mot_raw[:2]}:{mot_raw[2:]} (L)"
        now = datetime.now(dt_timezone.utc)
        origin_offset = ZoneInfo(origin_tz_name).utcoffset(now.replace(tzinfo=None))
        pilot_offset = ZoneInfo(pilot_tz).utcoffset(now.replace(tzinfo=None))
        shift_hours = (pilot_offset - origin_offset).total_seconds() / 3600
        shifted = (int(mot_raw[:2]) + int(mot_raw[2:]) / 60.0 + shift_hours) % 24
        h, m = int(shifted), round((shifted - int(shifted)) * 60)
        if m == 60:
            h, m = (h + 1) % 24, 0
        return f"{h:02d}:{m:02d} (L)"
    except Exception:
        return f"{mot_raw[:2]}:{mot_raw[2:]} (L)"


def _fdp_remaining_display(fdp_end_dec, origin):
    """"Xh Ym remaining" / "Xh Ym over" against this duty day's own FDP
    deadline, compared to the current time at `origin`. Same no-real-date
    caveat as _mot_display — wraps the raw difference into the nearest
    +/-12h window around now rather than doing full date-aware math, since
    this data never carries a real calendar date to anchor against."""
    if fdp_end_dec is None:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        origin_tz_name = (_AIRPORT_TZ.get(_airport_icao(origin)) or {}).get("tz")
        if not origin_tz_name:
            return None
        now_local = datetime.now(ZoneInfo(origin_tz_name))
        now_dec = now_local.hour + now_local.minute / 60.0 + now_local.second / 3600.0
        diff = ((fdp_end_dec - now_dec + 12) % 24) - 12
        h, m = int(abs(diff)), round((abs(diff) - int(abs(diff))) * 60)
        if m == 60:
            h, m = h + 1, 0
        return f"{h}h {m:02d}m {'remaining' if diff >= 0 else 'over'}"
    except Exception:
        return None


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
    # PU (purser) is on the roster/accordion but excluded from this count —
    # mobileCCI's crew-count tile tracks pilots + flight attendants, not the
    # lead FA separately.
    working_crew = [m for m in roster if m["seat"] != "PU"]
    ctx["crew_count"] = f"{len(working_crew)}/{len(working_crew)}"
    ctx["leg_id"] = str(leg.get("id", ""))
    ctx["origin_icao"] = _airport_icao(ctx.get("origin", ""))
    ctx["destination_icao"] = _airport_icao(ctx.get("destination", ""))
    # pairing_sched_out/in (frozen at PBS import) is the strikethrough
    # baseline; sched_out/in (overwritten by any later source) is the best
    # currently-known schedule; est_out/in (a live SimBrief estimate) wins
    # over both when present. See _station_time_html's docstring — using
    # pairing_sched as the display fallback here instead of sched was the
    # bug that kept showing pre-delay times after a redispatch.
    ctx["dep_time_html"] = _station_time_html(ctx.get("pairing_sched_out") or ctx.get("sched_out"), ctx.get("sched_out"), ctx.get("est_out"))
    ctx["arr_time_html"] = _station_time_html(ctx.get("pairing_sched_in") or ctx.get("sched_in"), ctx.get("sched_in"), ctx.get("est_in"))
    ctx["dep_date_display"] = _fmt_display_date(ctx.get("dep_date"))
    ctx["arr_date_display"] = _fmt_display_date(ctx.get("arr_date"))
    ctx["flight_time_display"] = _fmt_duration_hm(ctx.get("flight_time"))
    # IATA prefix on the flight number wherever it's displayed (especially
    # the Schedule pill strip — see initOverviewPills()); ICAO is the
    # fallback when SimBrief never reported an IATA code (fetch_ofp_leg_
    # fields' comment on why that happens), and a bare flight number is
    # the last resort for a leg with no known operator at all.
    _airline_prefix = ctx.get("airline_iata") or ctx.get("airline_icao") or ""
    ctx["flight_designator"] = f"{_airline_prefix} {ctx.get('flight_number', '')}".strip() if _airline_prefix else ctx.get("flight_number", "")
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
    ctx["selcal_html"] = html.escape(ctx.get("selcal") or "") or "—"
    ctx["oew_html"] = html.escape(ctx.get("oew") or "") or "—"
    ctx["max_zfw_html"] = html.escape(ctx.get("max_zfw") or "") or "—"
    ctx["max_tow_struct_html"] = html.escape(ctx.get("max_tow_struct") or "") or "—"
    ctx["max_ldw_html"] = html.escape(ctx.get("max_ldw") or "") or "—"
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
    ctx["pending_date_slip_json"] = json.dumps(ctx.get("pending_date_slip") or None)
    ctx["ffd_banner_style"] = "" if not ctx.get("fit_for_duty") else "display:none;"
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
    ctx["app_version"] = APP_VERSION
    ctx["mot_display"] = _mot_display(ctx.get("mot") or "", ctx.get("origin") or "", current_user.timezone)
    ctx["timezone_options"] = _timezone_options_html(current_user.timezone)
    str_ctx = {k: ("" if v is None else str(v)) for k, v in ctx.items() if k != "signature"}
    return Template(FOS_TEMPLATE).safe_substitute(**str_ctx)


LAUNCHER_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<script>(function(){var t=localStorage.getItem('fos_theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);})();</script>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#f5f5f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<title>MobileCCI</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --blue:#0071e3; --blue-dark:#0058a8; --red:#ff3b30; --green:#34c759; --inactive:#9aa1ab;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7; --inactive:#636366; }
  }
  :root[data-theme="dark"]{ --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7; --inactive:#636366; }
  :root[data-theme="light"]{ --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f; --inactive:#9aa1ab; }
  html,body{height:100%;overscroll-behavior:none;background:var(--bg);}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px 24px calc(88px + env(safe-area-inset-bottom));color:var(--value);padding-left:calc(24px + env(safe-area-inset-left));padding-right:calc(24px + env(safe-area-inset-right));}
  .tabbar{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);bottom:0;display:flex;justify-content:center;background:var(--card);border-top:1px solid var(--border);padding:5px 0 calc(5px + env(safe-area-inset-bottom));z-index:20;}
  .navtab{flex:1;max-width:110px;display:flex;flex-direction:column;align-items:center;gap:3px;background:transparent;border:none;color:var(--label);cursor:pointer;padding:4px 2px;position:relative;margin:0;}
  .navtab svg{width:22px;height:22px;}
  .navtab span{font-size:10px;font-weight:600;}
  .navtab.active{color:var(--blue-dark);}
  .navtab:disabled{opacity:.4;cursor:default;}
  /* Same fixed gear as FOS_TEMPLATE (see its own .settings-fab comment) —
     Home has no topbar of its own to hang a per-page icon off of. */
  .settings-fab{position:fixed;top:calc(env(safe-area-inset-top) + 10px);right:calc(env(safe-area-inset-right) + 16px);z-index:25;background:var(--card);border:1px solid var(--border);border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;color:var(--label);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);}
  .settings-fab svg{width:18px;height:18px;flex-shrink:0;}
  @media (min-width: 768px){ .settings-fab{top:calc(env(safe-area-inset-top) + 38px);} }
  h1{font-size:18px;color:var(--blue-dark);margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:var(--value);}
  textarea, select, input[type=text]{width:100%;max-width:640px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid var(--border);border-radius:5px;box-sizing:border-box;background:var(--card);color:var(--value);}
  textarea{height:160px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;}
  button{margin-top:10px;background:var(--blue);color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  button.secondary{background:var(--green);}
  .arow{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:8px;max-width:640px;}
  .arow-link{flex:1;min-width:0;text-decoration:none;color:var(--value);font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .arow-del{flex:0 0 auto;background:none;border:none;color:var(--inactive);cursor:pointer;padding:6px;margin:-6px;display:flex;}
  .arow-del svg{width:16px;height:16px;}
  .arow-del:hover{color:var(--red);}
  .clear-all-link{float:right;color:var(--blue);font-size:12.5px;font-weight:600;background:none;border:none;cursor:pointer;padding:0;}
  .arow span{color:var(--label);float:right;}
  .empty{color:var(--label);font-style:italic;}
  .msg{margin-top:8px;font-size:13px;color:var(--value);}
  .panel{max-width:640px;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:6px;margin-top:10px;}
  .tabs{display:flex;gap:8px;max-width:640px;border-bottom:1px solid var(--border);margin-bottom:16px;}
  .tab-btn{margin:0;background:none;color:var(--label);border:none;border-bottom:2px solid transparent;border-radius:0;padding:10px 4px;font-size:14px;font-weight:600;cursor:pointer;}
  .tab-btn.active{color:var(--blue-dark);border-bottom-color:var(--blue);}
  .tab-panel{display:none;}
  .tab-panel.active{display:block;}
  hr{max-width:640px;margin:14px 0;border:none;border-top:1px solid var(--border);}

  .sub-view{display:none;}
  .sub-view.active{display:block;}
  .subview-topbar{display:flex;align-items:center;gap:14px;margin-bottom:16px;max-width:640px;}
  .subview-topbar h1{margin:0;}
  .back-link{color:var(--blue);font-size:14px;font-weight:600;text-decoration:none;background:none;border:none;padding:0;margin:0;cursor:pointer;}
  .home-tiles{display:flex;flex-direction:column;gap:12px;max-width:640px;}
  .home-tile{display:flex;align-items:center;gap:14px;width:100%;text-align:left;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin:0;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .home-tile:disabled{opacity:.55;cursor:default;}
  .home-tile svg{width:26px;height:26px;color:var(--blue);flex:0 0 auto;}
  .home-tile .tile-title{font-size:15px;font-weight:700;color:var(--value);}
  .home-tile .tile-sub{font-size:12.5px;color:var(--label);margin-top:2px;}
</style></head><body>

<button class="settings-fab" title="Settings" onclick="window.location.href='/schedule?view=settings'">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
</button>
<div id="home-view" class="sub-view active">
  <h1>MobileCCI</h1>
  <div class="home-tiles">
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
    <button type="submit" style="width:100%;background:var(--label);">Sign Out ($username)</button>
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
    <button class="tab-btn" id="tab-generate-btn" onclick="showTab('generate')">Generate</button>
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
      <div style="font-size:13px;color:var(--label);margin-bottom:4px;">Skips PBS entirely — just enough to identify the flight. Everything else (aircraft, times, fuel...) gets set on SimBrief's own dispatch page on the next screen.</div>
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

  <div id="tab-generate" class="tab-panel">
    <div class="panel">
      <div style="font-size:13px;color:var(--label);margin-bottom:4px;">Searches the NAC route network for a legal multi-day trip starting and ending at the base you enter.</div>
      <label for="gen-base">Base</label>
      <input id="gen-base" type="text" placeholder="e.g. PHX">
      <label for="gen-days">Trip Length (days)</label>
      <input id="gen-days" type="text" value="4">
      <br><button onclick="submitGeneratePairing()">Generate</button>
      <div id="generate-msg" class="msg"></div>
      <div id="generate-candidates" style="margin-top:12px;"></div>
    </div>
  </div>
</div>

<div id="pick-leg-view" class="sub-view">
  <div class="subview-topbar">
    <button class="back-link" onclick="showHomeView('load-sequence')">Back</button>
    <h1>Pick a Leg</h1>
  </div>
  <div id="pick-leg-summary" style="font-size:13px;color:var(--label);margin-bottom:10px;max-width:640px;"></div>
  <div id="pick-leg-list" style="max-width:640px;"></div>
  <div id="pick-leg-msg" class="msg"></div>
</div>

<div id="import-simbrief-view" class="sub-view">
  <div class="subview-topbar">
    <button class="back-link" onclick="showHomeView('home')">Back</button>
    <h1>Import from SimBrief</h1>
  </div>
  <div class="panel">
    <div style="font-size:13px;color:var(--label);margin-bottom:4px;">Loads whatever OFP is currently on this SimBrief account right now — for dispatching the flight you're on today, not for browsing a schedule.</div>
    <label for="sb-user">SimBrief Username</label>
    <input id="sb-user" type="text" placeholder="Your SimBrief username">
    <br><button onclick="loadFromSimbrief()">Load Current Flight</button>
    <div id="sb-msg" class="msg"></div>
  </div>
</div>

<nav class="tabbar" aria-label="Primary">
  <button class="navtab active" id="tab-overview" onclick="showHomeView('home')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><path d="M9 10l2 2 4-4"/></svg>
    <span>Overview</span>
  </button>
  <button class="navtab" id="tab-schedule" onclick="window.location.href='/schedule'">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/></svg>
    <span>Schedule</span>
  </button>
  <button class="navtab"$current_leg_disabled id="tab-messages" onclick="homeNavTab('messages')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    <span>Messages</span>
  </button>
  <button class="navtab"$current_leg_disabled id="tab-docs" onclick="homeNavTab('doclocker')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M9.5 13h5M9.5 16h5"/></svg>
    <span>Docs</span>
  </button>
  <button class="navtab"$current_leg_disabled id="tab-more" onclick="homeNavTab('more')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
    <span>More</span>
  </button>
</nav>

<script>
const SERVER_SIMBRIEF_USER = "$default_simbrief_user";
const CURRENT_LEG_ID = "$current_leg_id";
// Home is itself the Overview-equivalent root when no leg is loaded, so
// its tabbar's Messages/Docs/More jump into the current leg's own page
// when one exists (mirrors FOS_TEMPLATE's navTab()) — those views have
// nothing to show without a real leg, so the buttons are server-rendered
// disabled in that case rather than silently no-op-ing here.
function homeNavTab(view){
  if(!CURRENT_LEG_ID) return;
  window.location.href = '/fos/' + CURRENT_LEG_ID + '?view=' + view;
}
function showHomeView(view){
  document.getElementById('home-view').classList.toggle('active', view==='home');
  document.getElementById('load-sequence-view').classList.toggle('active', view==='load-sequence');
  document.getElementById('import-simbrief-view').classList.toggle('active', view==='import-simbrief');
  document.getElementById('pick-leg-view').classList.toggle('active', view==='pick-leg');
  document.getElementById('tab-overview').classList.toggle('active', view==='home');
}
function showTab(tab){
  document.getElementById('tab-pbs').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-manual').classList.toggle('active', tab==='manual');
  document.getElementById('tab-generate').classList.toggle('active', tab==='generate');
  document.getElementById('tab-pbs-btn').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-manual-btn').classList.toggle('active', tab==='manual');
  document.getElementById('tab-generate-btn').classList.toggle('active', tab==='generate');
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
    el.style.color = 'var(--red)';
  };
  reader.readAsText(file);
}
function importPbs(text){
  const el = document.getElementById('import-msg');
  if(!text || !text.trim()){ el.textContent = 'That file was empty.'; el.style.color = 'var(--red)'; return; }
  fetch('/import-pbs', {method:'POST', headers:{'Content-Type':'text/plain'}, body: text})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      if(!ok){ el.textContent = data.error || 'Import failed'; el.style.color = 'var(--red)'; return; }
      el.textContent = `Imported ${data.sequences_parsed} sequence(s), ${data.legs_parsed} legs.`;
      el.style.color = 'var(--green)';
      loadSequences();
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = 'var(--red)'; });
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
    if(!seqR.ok){ el.textContent = seqData.error || 'Sequence not found'; el.style.color = 'var(--red)'; return; }
    el.textContent = '';
    renderLegPicker(seqData);
    showHomeView('pick-leg');
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
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
    heading.style.cssText = 'font-size:13px;font-weight:600;color:var(--blue-dark);margin:14px 0 6px;';
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
    if(!genR.ok){ el.textContent = genData.error || 'Generate failed'; el.style.color = 'var(--red)'; return; }
    window.location.href = genData.fos_url + '?view=release';
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}

async function submitManualEntry(){
  const el = document.getElementById('manual-msg');
  const origin = document.getElementById('manual-orig').value.trim().toUpperCase();
  const destination = document.getElementById('manual-dest').value.trim().toUpperCase();
  const flight_number = document.getElementById('manual-fltnum').value.trim();
  if(!origin || !destination || !flight_number){
    el.textContent = 'Origin, destination, and flight number are all required.';
    el.style.color = 'var(--red)';
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
    if(!r.ok){ el.textContent = data.error || 'Could not start this flight'; el.style.color = 'var(--red)'; return; }
    window.location.href = data.fos_url + '?view=release';
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}

async function submitGeneratePairing(){
  const el = document.getElementById('generate-msg');
  const list = document.getElementById('generate-candidates');
  list.innerHTML = '';
  const base = document.getElementById('gen-base').value.trim().toUpperCase();
  const days = parseInt(document.getElementById('gen-days').value, 10);
  if(!base){ el.textContent = 'Enter a base station.'; el.style.color = 'var(--red)'; return; }
  if(!days || days < 1){ el.textContent = 'Trip length must be a positive number of days.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Searching…';
  el.style.color = '';
  try {
    const r = await fetch('/pairings/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base, days}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Generate failed'; el.style.color = 'var(--red)'; return; }
    if(!data.candidates.length){
      el.textContent = 'No legal pairings found for that base/trip length.';
      el.style.color = 'var(--red)';
      return;
    }
    el.textContent = `Found ${data.candidates.length} candidate(s).`;
    el.style.color = 'var(--green)';
    list.innerHTML = data.candidates.map((c, i) => `<div class="arow" style="flex-direction:column;align-items:stretch;">
      <div style="display:flex;justify-content:space-between;font-size:13.5px;color:var(--value);">
        <span>${c.routing.join('-')}</span><span>${c.block.toFixed(2)}h block</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--label);margin-top:2px;">
        <span>${c.legs_per_day.join('-')} legs/day</span><span>${c.dacv.toFixed(2)}h/day</span>
      </div>
      <button onclick="acceptGeneratedPairing('${base}', ${i})" style="margin-top:8px;">Accept</button>
    </div>`).join('');
    window._genCandidates = data.candidates;
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}

async function acceptGeneratedPairing(base, i){
  const el = document.getElementById('generate-msg');
  const chain = (window._genCandidates || [])[i] && window._genCandidates[i].chain;
  if(!chain){ el.textContent = 'That candidate is no longer available — generate again.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Accepting…';
  el.style.color = '';
  try {
    const r = await fetch('/pairings/accept', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base, chain}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Accept failed'; el.style.color = 'var(--red)'; return; }
    document.getElementById('generate-candidates').innerHTML = '';
    el.textContent = '';
    showTab('pbs');
    loadSequences();
    openSequence(data.seq);
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}

function loadFromSimbrief(){
  const el = document.getElementById('sb-msg');
  const user = document.getElementById('sb-user').value.trim();
  if(!user){ el.textContent = 'Enter a SimBrief username first.'; el.style.color = 'var(--red)'; return; }
  localStorage.setItem('fos_simbrief_user', user);
  fetch('/settings/simbrief-user', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})});
  el.textContent = 'Loading current flight…';
  el.style.color = '';
  fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user})})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      if(!ok){ el.textContent = data.error || 'Load failed'; el.style.color = 'var(--red)'; return; }
      window.location.href = data.fos_url + '?view=confirm';
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = 'var(--red)'; });
}

// Current Flight / Request New Data are rendered server-side now (see
// render_launcher_html / current_user.current_leg_id) instead of a client
// fetch('/archive') + localStorage lookup — the server already knows which
// pilot is asking, so it can just say whether there's something to resume.

(function(){
  const saved = localStorage.getItem('fos_simbrief_user') || SERVER_SIMBRIEF_USER;
  if(saved) document.getElementById('sb-user').value = saved;
})();

// Deep link from Schedule's empty-state ("Load New Sequence" / "Generate
// a Pairing" now live there, not as Home tiles — see /schedule) straight
// into this page's own existing load-sequence sub-view/tab, unchanged.
(function(){
  const open = new URLSearchParams(window.location.search).get('open');
  if(open === 'load-sequence'){ showHomeView('load-sequence'); }
  else if(open === 'generate'){ showHomeView('load-sequence'); showTab('generate'); }
})();

loadSequences();
</script>
</body></html>"""


FOS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script>(function(){var t=localStorage.getItem('fos_theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);})();</script>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#f5f5f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<title>Flight $flight_designator \u2013 MobileCCI</title>
<style>
  :root{
    --navy:#1d1d1f; --blue:#0071e3; --blue-dark:#0058a8;
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --red:#ff3b30; --green:#34c759; --inactive:#9aa1ab; --radius:10px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      /* Blue/red/green stay exactly as-is by request — only the neutrals
         (surfaces, borders, text) switch for dark mode. */
      --navy:#2c2c2e;
      --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7;
      --inactive:#636366;
    }
    /* PDF pages are rendered bitmaps (real document ink), not styled
       content — inverting is a deliberate reading-comfort choice here,
       not a byproduct of the token swap above. hue-rotate after invert
       keeps a NOTAM's red/weather-map colors closer to their original
       hue instead of a flat color-swap. */
    .pdf-page{filter:invert(1) hue-rotate(180deg);}
  }
  :root[data-theme="dark"]{
    --navy:#2c2c2e; --bg:#000; --card:#1c1c1e; --border:#38383a; --label:#98989d; --value:#f5f5f7; --inactive:#636366;
  }
  :root[data-theme="light"]{
    --navy:#1d1d1f; --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f; --inactive:#9aa1ab;
  }
  :root[data-theme="dark"] .pdf-page{filter:invert(1) hue-rotate(180deg);}
  :root[data-theme="light"] .pdf-page{filter:none;}
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;height:100%;overscroll-behavior:none;background:var(--bg);}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--value);-webkit-font-smoothing:antialiased;}
  button{font-family:inherit;}
  :focus-visible{outline:2px solid var(--blue-dark);outline-offset:2px;}
  @media (prefers-reduced-motion: reduce){ *{transition:none !important;animation:none !important;} }
  .app-shell{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh;width:100%;padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right);}
  .main{flex:1;min-width:0;padding:14px 16px calc(72px + env(safe-area-inset-bottom));}
  .tabbar{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);bottom:0;display:flex;justify-content:center;background:var(--card);border-top:1px solid var(--border);padding:5px 0 calc(5px + env(safe-area-inset-bottom));z-index:20;}
  .navtab{flex:1;max-width:110px;display:flex;flex-direction:column;align-items:center;gap:3px;background:transparent;border:none;color:var(--label);cursor:pointer;padding:4px 2px;position:relative;}
  .navtab svg{width:22px;height:22px;}
  .navtab span{font-size:10px;font-weight:600;}
  .navtab.active{color:var(--blue-dark);}
  .navtab:disabled{opacity:.4;cursor:default;}
  .navtab .badge{position:absolute;top:0;left:50%;margin-left:6px;width:15px;height:15px;border-radius:50%;background:var(--red);color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;}
  .flight-card{display:flex;align-items:stretch;padding:22px 20px 18px;gap:12px;}
  .flight-card .station{flex:1;display:flex;flex-direction:column;gap:4px;min-width:0;}
  .flight-card .station.dest{align-items:flex-end;text-align:right;}
  .flight-card .station-date{font-size:12px;color:var(--label);text-transform:uppercase;letter-spacing:.02em;}
  .flight-card .station-code{font-size:27px;font-weight:800;}
  .flight-card .est-time{font-size:18px;font-weight:700;color:var(--green);}
  .flight-card .est-time.late{color:var(--red);}
  .flight-card .sched-time{display:block;font-size:13px;color:var(--label);}
  .flight-card .sched-time.superseded{text-decoration:line-through;}
  .flight-card .station-gate{font-size:17px;font-weight:700;color:var(--blue);margin-top:4px;}
  .flight-card-dur{flex:0 0 auto;align-self:center;display:flex;align-items:center;gap:4px;font-size:15px;font-weight:700;color:var(--label);white-space:nowrap;padding:0 8px;}
  .flight-card-dur .dur-chevron{width:11px;height:11px;color:var(--inactive);flex:0 0 auto;}
  .pill-strip{display:flex;gap:8px;overflow-x:auto;padding:8px 0 12px;scrollbar-width:none;}
  .pill-strip::-webkit-scrollbar{display:none;}
  .leg-pill{flex:0 0 auto;font-family:inherit;font-size:14px;font-weight:600;padding:8px 17px;border-radius:20px;border:none;background:transparent;color:var(--value);cursor:pointer;white-space:nowrap;}
  .leg-pill.selected{background:var(--blue);color:#fff;font-weight:700;}
  .split{display:flex;gap:0;align-items:flex-start;}
  .split-left{flex:0 0 auto;width:340px;display:flex;flex-direction:column;gap:20px;min-width:0;}
  .split-right{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px;margin-left:44px;}
  .panel-card{background:var(--card);border-radius:18px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.05);border:1px solid var(--border);}
  .panel-card-hdr{padding:16px 18px 13px;font-size:19px;font-weight:700;color:var(--value);background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;}
  .lead-icon{width:19px;height:19px;color:var(--label);flex:0 0 auto;}
  .mot-row{padding:14px 17px;display:flex;justify-content:space-between;align-items:center;}
  .mot-time{font-size:18px;font-weight:700;}
  .mot-scorecard{padding:12px 17px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;color:var(--inactive);}
  .mot-scorecard .code{font-weight:700;font-size:15px;color:var(--inactive);}
  .mot-scorecard .desc{font-size:13px;color:var(--inactive);}
  .mot-view{font-size:15px;font-weight:600;color:var(--inactive);}
  .stat-row{background:var(--card);border-radius:18px;border:1px solid var(--border);overflow:hidden;}
  .stat-hdr{display:flex;justify-content:space-between;align-items:center;padding:15px 16px;cursor:pointer;font-size:17px;font-weight:700;background:none;border:none;width:100%;text-align:left;color:var(--value);}
  .stat-hdr .val{color:var(--label);font-weight:500;display:flex;align-items:center;gap:6px;font-size:17px;}
  .stat-hdr svg{width:15px;height:15px;color:var(--inactive);transition:transform .15s ease;}
  .stat-row.open .stat-hdr svg{transform:rotate(180deg);}
  .stat-body{display:none;border-top:1px solid var(--border);}
  .stat-row.open .stat-body{display:block;}
  .stat-detail-row{position:relative;display:flex;justify-content:space-between;padding:11px 15px;font-size:14.5px;}
  .stat-detail-row:not(:last-child)::after{content:'';position:absolute;left:15px;right:15px;bottom:0;height:1px;background:var(--border);}
  .stat-detail-row:last-child{border-bottom:none;}
  .stat-detail-row .lbl{color:var(--label);}
  .stat-detail-row .val{font-weight:600;font-variant-numeric:tabular-nums;}
  .weight-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;}
  .weight-grid>div{display:flex;flex-direction:column;align-items:center;gap:2px;background:var(--bg);border-radius:8px;padding:7px 4px;}
  .weight-grid .wg-lbl{font-size:10.5px;color:var(--label);text-transform:uppercase;letter-spacing:.03em;}
  .weight-grid .wg-val{font-size:13.5px;font-weight:700;color:var(--value);font-variant-numeric:tabular-nums;}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;margin-bottom:10px;position:sticky;top:0;z-index:10;background:var(--bg);padding-top:calc(env(safe-area-inset-top) + 6px);margin-top:-6px;margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px;}
  /* Tablet-width browsers (iPadOS Safari's tabbed mode among them) draw
     their own chrome — tab-strip controls, a floating "stoplight" cluster
     — over the top-left/top-right of the page that safe-area-inset can't
     account for (it only reports hardware notch/home-indicator, not
     browser UI). Extra clearance here is a defensive guess, not measured
     against a real device — right height still needs confirming there. */
  @media (min-width: 768px){ .topbar{padding-top:calc(env(safe-area-inset-top) + 34px);} }
  .back-link{order:1;display:flex;align-items:center;color:var(--value);background:none;border:none;cursor:pointer;padding:6px 4px;text-decoration:none;}
  .topbar-actions{order:2;margin-left:auto;display:flex;align-items:center;gap:14px;padding-right:38px;}
  .topbar-title{order:3;flex:1 1 100%;text-align:center;margin-top:2px;}
  .topbar-title h1{font-size:19px;margin:0;font-weight:600;color:var(--blue-dark);}
  .topbar-title p{font-size:12px;margin:2px 0 0;color:var(--label);}
  .icon-btn{background:none;border:none;color:var(--label);cursor:pointer;padding:2px;display:flex;}
  .icon-btn svg{width:19px;height:19px;}
  .icon-btn.syncing svg{animation:spin .8s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  /* One settings gear, fixed above every view (not per-topbar) — the only
     way to guarantee it's on every single page without duplicating it
     into all 14 view sections. Sits clear of the topbar's own
     actions/title since it's positioned independently. */
  .settings-fab{position:fixed;top:calc(env(safe-area-inset-top) + 10px);right:calc(env(safe-area-inset-right) + 16px);z-index:25;background:var(--card);border:1px solid var(--border);border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;color:var(--label);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);}
  .settings-fab svg{width:18px;height:18px;flex-shrink:0;}
  @media (min-width: 768px){ .settings-fab{top:calc(env(safe-area-inset-top) + 38px);} }
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
  .info-row{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);font-size:14.5px;gap:10px;}
  .info-row .lbl{color:var(--label);}
  .info-row .val{color:var(--value);font-weight:500;text-align:right;word-break:break-word;}
  .search-block{background:var(--card);padding:12px 14px;border-bottom:1px solid var(--border);}
  .search-block label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;}
  .theme-opt{flex:1;font-family:inherit;font-size:14px;font-weight:600;padding:9px 0;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--value);cursor:pointer;}
  .theme-opt.active{background:var(--blue);border-color:var(--blue);color:#fff;}
  .search-block input,.search-block select{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:5px;font-size:13.5px;background:var(--card);color:var(--value);}
  .search-row{display:flex;gap:10px;background:var(--card);padding:12px 14px;border-bottom:1px solid var(--border);}
  .search-row .search-block{flex:1;border-bottom:none;padding:0;background:none;}
  .tabs{display:flex;gap:8px;padding:0 14px;border-bottom:1px solid var(--border);margin-bottom:10px;}
  .tab-btn{margin:0;background:none;color:var(--label);border:none;border-bottom:2px solid transparent;border-radius:0;padding:10px 4px;font-size:14px;font-weight:600;cursor:pointer;}
  .tab-btn.active{color:var(--blue-dark);border-bottom-color:var(--blue);}
  .tab-panel{display:none;}
  .tab-panel.active{display:block;}
  .panel{padding:14px;background:var(--card);border:1px solid var(--border);border-radius:6px;margin:10px 14px;box-sizing:border-box;}
  .panel label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:var(--value);}
  .panel input[type=text],.panel textarea,.panel select{width:100%;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid var(--border);border-radius:5px;box-sizing:border-box;background:var(--card);color:var(--value);}
  .panel button{margin-top:10px;background:var(--blue);color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  .msg{margin-top:8px;font-size:13px;color:var(--value);}
  .arow{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin:0 14px 8px;}
  .arow-link{flex:1;min-width:0;text-decoration:none;color:var(--value);font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .arow-del{flex:0 0 auto;background:none;border:none;color:var(--inactive);cursor:pointer;padding:6px;margin:-6px;display:flex;}
  .arow-del svg{width:16px;height:16px;}
  .arow-del:hover{color:var(--red);}
  .arow span{color:var(--label);}
  .arow button:not(.arow-del){margin-top:10px;background:var(--blue);color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;width:100%;}
  .section-bar{display:flex;align-items:center;justify-content:space-between;background:var(--blue);color:#fff;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer;border:none;width:100%;text-align:left;}
  .section-bar svg{width:16px;height:16px;transition:transform .15s ease;}
  .section-bar.collapsed svg.chevron{transform:rotate(180deg);}
  .no-prefs{padding:10px 14px;color:var(--label);font-size:13px;font-style:italic;background:var(--card);border-bottom:1px solid var(--border);}
  .doc-list{background:var(--card);}
  .doc-row{position:relative;display:flex;align-items:center;justify-content:space-between;padding:15px 17px;gap:12px;}
  .doc-row:not(:last-child)::after{content:'';position:absolute;left:17px;right:17px;bottom:0;height:1px;background:var(--border);}
  .doc-row .code{font-weight:700;font-size:16.5px;}
  .doc-row .desc{font-size:15px;color:var(--label);margin-top:2px;}
  .doc-row .actions{display:flex;align-items:center;gap:26px;flex:0 0 auto;}
  .doc-row .actions svg{width:19px;height:19px;color:var(--label);cursor:pointer;padding:7px;margin:-7px;box-sizing:content-box;}
  .doc-row .check{color:var(--inactive,#9aa1ab);cursor:pointer;}
  .ffd-banner{background:var(--red);color:#fff;font-size:13px;font-weight:600;line-height:1.4;padding:11px 17px;}
  .doc-row .check.signed{color:var(--blue-dark);}
  .doc-row .actions svg.bookmark-icon.bookmarked{color:var(--blue);fill:var(--blue);}
  .doc-row .actions svg.ext-link{color:var(--blue);}
  .lib-crumb{padding:0 14px 10px;font-size:12.5px;color:var(--label);display:flex;flex-wrap:wrap;gap:4px;align-items:center;}
  .lib-crumb a{color:var(--blue);text-decoration:none;}
  .lib-crumb .current{color:var(--value);font-weight:600;}
  .section-bar.lib-bar{cursor:default;justify-content:flex-start;gap:9px;}
  .lib-back{cursor:pointer;display:flex;align-items:center;flex:0 0 auto;padding:2px;margin:-2px;}
  .lib-back svg{width:10px;height:15px;display:block;}
  /* A small action living in the same blue bar as the pane's own title —
     white-on-translucent-white so it reads as part of that bar, not a
     plain-background link (like .clear-all-link) mismatched onto it. */
  .lib-bar-action{flex:0 0 auto;color:#fff;background:rgba(255,255,255,.18);border:none;border-radius:6px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer;}
  .lib-bar-action:active{background:rgba(255,255,255,.3);}
  .doc-row.lib-row{cursor:pointer;}
  .doc-row .code.seq-code{color:var(--blue);}
  .doc-row .desc.lib-routing{color:var(--value);}
  .doc-row .desc.lib-routing b{font-weight:700;}
  .lib-stats{text-align:right;font-size:12px;color:var(--label);flex:0 0 auto;line-height:1.45;white-space:nowrap;}
  .lib-stats .days{color:var(--value);font-weight:600;font-size:12.5px;}
  .lib-total{padding:0 14px 10px;font-size:12.5px;color:var(--label);}
  .fly-row{display:flex;gap:8px;padding:0 14px 12px;}
  .fly-btn{flex:1;margin:0;background:var(--blue);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  .save-btn{flex:0 0 auto;width:46px;margin:0;background:var(--card);border:1px solid var(--border);border-radius:5px;color:var(--inactive);cursor:pointer;display:flex;align-items:center;justify-content:center;}
  .save-btn svg{width:19px;height:19px;}
  .save-btn.saved{color:var(--blue);border-color:var(--blue);}
  .save-btn.saved svg{fill:var(--blue);}

  /* Duty-day card — reads like a web version of the bid pack's own leg
     table (DP/D-A/Flt·Eq/Dep/Arr/Blk·Gnd), not a list of app rows. One
     font size throughout (--dd-fs) — weight and color carry the
     hierarchy, not size. RPT lives stacked inside the first leg's own Dep
     cell, RLS inside the last leg's own Arr cell — both genuinely part of
     that leg's row, not a separate banner. HOTEL/rest sits outside the
     card entirely, between one day and the next, like an actual layover.
  */
  .dd-card{--dd-fs:14.5px;border-radius:12px;overflow:hidden;border:1px solid var(--border);background:var(--card);margin-bottom:2px;}
  .dd-hdr{padding:10px 14px 8px;font-size:17px;font-weight:700;color:var(--value);}
  /* Real iPhone widths clip this table (SF Pro's real metrics run wider
     than whatever this was last measured against) — .dd-card's own
     overflow:hidden was silently cropping the Blk/Gnd column off-screen
     with no way to reach it. This wrapper scrolls internally instead, and
     the narrow-viewport rule below shrinks things enough that scrolling
     usually isn't even needed. */
  .dd-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
  table.dd-table{width:100%;border-collapse:collapse;font-size:var(--dd-fs);font-variant-numeric:tabular-nums;}
  table.dd-table th{
    font-size:calc(var(--dd-fs) - 1px);font-weight:600;color:var(--label);text-align:left;line-height:1.3;
    text-transform:uppercase;letter-spacing:.02em;
    padding:4px 8px;border-bottom:1px solid var(--border);white-space:nowrap;
  }
  /* Uniform column padding — narrow columns (Dp/D-A/Flt-Eq/Blk-Gnd) don't
     get extra room just for being near an edge; only the very first/last
     column keeps a bit more to clear the card's own rounded corner. */
  table.dd-table th:first-child, table.dd-table td:first-child{padding-left:12px;}
  table.dd-table th:last-child, table.dd-table td:last-child{padding-right:12px;text-align:right;}
  table.dd-table td{padding:6px 8px;white-space:nowrap;vertical-align:top;position:relative;}
  @media (max-width: 400px){
    .dd-card{--dd-fs:13px;}
    table.dd-table th:first-child, table.dd-table td:first-child{padding-left:9px;}
    table.dd-table th:last-child, table.dd-table td:last-child{padding-right:9px;}
    table.dd-table th, table.dd-table td{padding-left:5px;padding-right:5px;}
  }
  table.dd-table tbody tr:first-child td{padding-top:8px;}
  table.dd-table tbody tr:last-child td{padding-bottom:8px;}
  table.dd-table .dp-cell{display:flex;align-items:center;gap:4px;color:var(--label);}
  table.dd-table .edit-icon{width:13px;height:13px;color:var(--inactive);cursor:pointer;flex:0 0 auto;}
  table.dd-table .flt{font-weight:700;color:var(--value);}
  table.dd-table .sta{font-weight:700;color:var(--value);}
  table.dd-table .sub{color:var(--label);margin-top:1px;}
  table.dd-table .blk{font-weight:600;color:var(--value);}
  /* RPT/RLS are genuine rows of their own, spanning the full table width —
     not squeezed into a leg's own departure/arrival cell. */
  table.dd-table tr.marker-row td:last-child{
    padding-top:7px;padding-bottom:7px;white-space:normal;
    color:var(--label);font-weight:600;text-align:left;
    background:var(--bg);
  }
  table.dd-table .marker-row b{color:var(--value);font-weight:700;}
  table.dd-table .marker-row + tr td{padding-top:8px;}
  .dd-summary{display:flex;justify-content:flex-end;gap:6px;padding:7px 14px 9px;font-size:var(--dd-fs);color:var(--label);border-top:1px solid var(--border);}
  .dd-summary b{color:var(--value);font-weight:700;}
  /* Hotel/rest is the one thing between two duty days worth noticing at a
     glance — bigger and bolder than the muted card chrome around it. */
  .layover{display:flex;justify-content:space-between;align-items:baseline;padding:9px 14px;font-size:calc(var(--dd-fs, 14.5px) + 1.5px);font-weight:600;color:var(--value);}
  .layover b{font-weight:700;}
  .layover .rest{font-variant-numeric:tabular-nums;color:var(--blue);font-weight:700;}
  .layover .rest b{color:var(--blue);}
  #sign-pad{touch-action:none;background:#fff;border:1px solid var(--border);border-radius:6px;width:100%;height:220px;}
  .placeholder-note{padding:12px 14px;color:var(--label);font-style:italic;font-size:13px;background:var(--card);}
  .view{display:none;}
  .view.active{display:block;}
  #toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(12px);background:#1a1f29;color:#fff;padding:9px 16px;border-radius:20px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .18s ease, transform .18s ease;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:10;}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
  #date-slip-modal{display:none;position:fixed;inset:0;z-index:50;background:var(--bg);align-items:center;justify-content:center;padding:24px;}
  #date-slip-modal.show{display:flex;}
  .date-slip-card{max-width:400px;width:100%;background:var(--card);border-radius:18px;border:1px solid var(--border);padding:28px 24px;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.25);}
  .date-slip-card .ds-icon{width:44px;height:44px;color:var(--red);margin-bottom:14px;}
  .date-slip-card h2{font-size:19px;margin:0 0 10px;color:var(--value);}
  .date-slip-card p{font-size:14.5px;color:var(--label);margin:0 0 22px;line-height:1.5;}
  .date-slip-card .ds-actions{display:flex;gap:10px;}
  .date-slip-card button{flex:1;font-family:inherit;font-size:15px;font-weight:700;padding:13px 0;border-radius:10px;border:none;cursor:pointer;}
  .date-slip-card .ds-reject{background:var(--bg);color:var(--value);border:1px solid var(--border);}
  .date-slip-card .ds-confirm{background:var(--blue);color:#fff;}
  @media (max-width:640px){
    .content-grid{grid-template-columns:1fr;}
    .col-divider{border-right:none;border-bottom:6px solid var(--bg);}
    .flight-summary{gap:12px;font-size:12px;}
    .flight-card .station-code{font-size:24px;}
    .split{flex-direction:column;}
    .split-left{width:100%;}
    .split-right{margin-left:0;margin-right:0;margin-top:2px;width:100%;}
  }
</style>
</head>
<body>
<div id="date-slip-modal">
  <div class="date-slip-card">
    <svg class="ds-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v5"/><path d="M12 16h.01"/></svg>
    <h2 id="ds-title">Flight delayed</h2>
    <p id="ds-body"></p>
    <div class="ds-actions">
      <button type="button" class="ds-reject" onclick="_modalAction('reject')">Reject</button>
      <button type="button" class="ds-confirm" onclick="_modalAction('confirm')">Confirm</button>
    </div>
  </div>
</div>
<button class="settings-fab" title="Settings" onclick="showView('settings')">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
</button>
<div class="app-shell">
  <main class="main">
    <section id="overview-view" class="view active">
      <div class="topbar">
        <a class="back-link" href="/" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></a>
        <div class="topbar-actions">
          <button class="icon-btn" id="sync-btn" title="Sync from SimBrief" onclick="syncFromSimbrief(false)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
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
                <div class="station-date">$dep_date_display</div>
                <div class="station-code">$origin</div>
                <div>$dep_time_html</div>
                <div class="station-gate" id="ov-dep-gate">$dep_gate</div>
              </div>
              <div class="flight-card-dur"><svg class="dur-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg><span>$flight_time_display</span><svg class="dur-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></div>
              <div class="station dest">
                <div class="station-date">$arr_date_display</div>
                <div class="station-code">$destination</div>
                <div>$arr_time_html</div>
                <div class="station-gate" id="ov-arr-gate">$arr_gate</div>
              </div>
            </div>
          </div>
          <div class="panel-card" id="ov-docs-card">
            <div class="panel-card-hdr">Preflight Docs</div>
            <div class="doc-row" style="cursor:pointer;" onclick="showView('saveddocs')">
              <div style="display:flex;align-items:center;gap:10px;">
                <svg class="lead-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M12 11v6"/><path d="M9 14l3 3 3-3"/></svg>
                <div class="code">Saved Docs</div>
              </div>
              <div class="actions"><span class="val" id="ov-saved-count" style="color:var(--label);font-size:13px;">$saved_docs_count</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
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
              <div class="actions"><span style="color:var(--blue);font-weight:800;font-size:20px;letter-spacing:1px;">&bull;&bull;&bull;</span></div>
            </div>
          </div>
          <div class="panel-card">
            <div class="panel-card-hdr">Mandatory Off Time</div>
            <div class="mot-row" style="cursor:pointer;" onclick="showMotLog()"><span class="mot-time">$mot_display</span></div>
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
              <div class="stat-detail-row"><span class="lbl">SELCAL</span><span class="val">$selcal_html</span></div>
              <div class="stat-detail-row" style="display:block;">
                <div class="lbl" style="margin-bottom:6px;">Weights (Structural)</div>
                <div class="weight-grid">
                  <div><span class="wg-lbl">EOW</span><span class="wg-val">$oew_html</span></div>
                  <div><span class="wg-lbl">MZFW</span><span class="wg-val">$max_zfw_html</span></div>
                  <div><span class="wg-lbl">MTOW</span><span class="wg-val">$max_tow_struct_html</span></div>
                  <div><span class="wg-lbl">MLW</span><span class="wg-val">$max_ldw_html</span></div>
                </div>
              </div>
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
    <section id="allcommands-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>All Commands</h1></div>
      </div>
      <div class="panel-card">
        <div class="ffd-banner" style="$ffd_banner_style">Fit for Duty not signed \u2014 sign below to unlock release and document downloads.</div>
        <div class="doc-row">
          <div><div class="code">FFD</div><div class="desc">Fit for Duty Declaration</div></div>
          <div class="actions">
            <svg id="ffd-doc-check" class="check $ffd_check_class" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="openSignPad('ffd')"><path d="M20 6L9 17l-5-5"/></svg>
            <svg class="bookmark-icon" data-doc="FFD" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FFD', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">EFLIGHT PLAN</div><div class="desc">eFlight Plan</div></div>
          <div class="actions">
            <svg id="sign-check" class="check $signed_class" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="openSignPad('eflightplan')"><path d="M20 6L9 17l-5-5"/></svg>
            <svg class="bookmark-icon" data-doc="EFLIGHT PLAN" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('EFLIGHT PLAN', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
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
        <div class="doc-row" style="border-bottom:none;">
          <div><div class="code">G*L/SS</div><div class="desc">Customers Requiring Special Services</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Not available \u2014 no data source for this document yet')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
      </div>
    </section>
    <section id="weather-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('allcommands')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>Winds &amp; Weather</h1>
          <p>WX*$origin / WX*$destination</p>
        </div>
      </div>
      <div id="weather-body"><p class="placeholder-note">Loading\u2026</p></div>
    </section>

    <section id="saveddocs-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Saved Docs</h1></div>
      </div>
      <div id="saveddocs-body"><p class="placeholder-note">No saved documents yet.</p></div>
    </section>

    <section id="doclocker-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Docs</h1></div>
      </div>
      <p class="placeholder-note">No documents.</p>
    </section>

    <section id="messages-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Messages</h1></div>
      </div>
      <p class="placeholder-note">No messages.</p>
    </section>

    <section id="pdf-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="closePdfView()" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-actions">
          <a id="pdf-export-link" style="font-size:14px;color:var(--blue-dark);text-decoration:none;font-weight:600;">Export</a>
        </div>
        <div class="topbar-title">
          <h1 id="pdf-view-title"></h1>
        </div>
      </div>
      <div id="pdf-ffd-banner" class="ffd-banner" style="display:none;">Fit for Duty not signed — you can view this document, but Export is locked until you sign it.</div>
      <div id="pdf-pages" style="background:#525659;margin:0 -16px;padding:12px 12px 32px;display:flex;flex-direction:column;align-items:center;gap:12px;"></div>
    </section>
    <section id="motlog-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>MOT Log</h1>
          <p>Signed times and FDP remaining, per leg</p>
        </div>
      </div>
      <div id="motlog-body"><p class="placeholder-note">Loading…</p></div>
    </section>

    <section id="sign-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('allcommands')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1 id="sign-title">Sign eFlight Plan</h1>
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
        <div class="topbar-title" style="flex:1 1 100%;">
          <h1>Schedule</h1>
        </div>
      </div>
      <div class="tabs">
        <button class="tab-btn active" id="tab-mytrip-btn" onclick="showScheduleTab('mytrip')">My Trip</button>
        <button class="tab-btn" id="tab-library-btn" onclick="showScheduleTab('library')">Pairing Library</button>
        <button class="tab-btn" id="tab-layers-btn" onclick="showScheduleTab('layers')">Bid Layers</button>
      </div>
      <div id="tab-mytrip" class="tab-panel active">
        <div id="pairing-body"><p class="placeholder-note">Loading…</p></div>
      </div>
      <div id="tab-library" class="tab-panel">
        <div id="library-crumb" style="padding:10px 14px;font-size:12.5px;color:var(--label);"></div>
        <div id="library-body"><p class="placeholder-note">Loading…</p></div>
      </div>
      <div id="tab-layers" class="tab-panel">
        <div id="layers-body"><p class="placeholder-note">Loading…</p></div>
      </div>
    </section>
    <section id="recovery-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>Report a Disruption</h1>
          <p>Find a legal way back on track</p>
        </div>
      </div>
      <div class="tabs">
        <button class="tab-btn active" id="tab-shift-btn" onclick="showRecoveryTab('shift')">Delayed / Timed Out</button>
        <button class="tab-btn" id="tab-divert-btn" onclick="showRecoveryTab('divert')">Diverted</button>
      </div>
      <div id="tab-shift" class="tab-panel active">
        <div class="panel">
          <div style="font-size:13px;color:var(--label);margin-bottom:4px;">Same destination, just later — a delay pushed you into rest instead of continuing as planned. Enter when duty actually ended; this first tries the exact original plan shifted to the next legal day, then falls back to an alternate routing if that doesn't fit.</div>
          <label for="shift-rest-start">Rest Started (local time, HHMM)</label>
          <input id="shift-rest-start" type="text" placeholder="e.g. 0100">
          <br><button onclick="submitShiftDay()">Find a Way Forward</button>
          <div id="shift-msg" class="msg"></div>
        </div>
        <div id="shift-candidates" style="margin-top:4px;"></div>
      </div>
      <div id="tab-divert" class="tab-panel">
        <div class="panel">
          <div style="font-size:13px;color:var(--label);margin-bottom:4px;">Enter where this flight actually ended up. Everything after it in the pairing will be replaced with a legal way back to base.</div>
          <label for="rec-dest">Actual Destination</label>
          <input id="rec-dest" type="text" placeholder="Station code">
          <label for="rec-arrival">Actual Arrival (local time, HHMM)</label>
          <input id="rec-arrival" type="text" placeholder="e.g. 0300">
          <br><button onclick="submitRecovery()">Find Recovery Options</button>
          <div id="recovery-msg" class="msg"></div>
        </div>
        <div id="recovery-candidates" style="margin-top:4px;"></div>
      </div>
    </section>
    <section id="release-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>Flight $flight_designator</h1>
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
        <button class="back-link" onclick="backFromSettings()" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
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
        <label>Auto-Sync with SimBrief</label>
        <div style="display:flex;gap:8px;">
          <button type="button" class="theme-opt" data-autosync-opt="on" onclick="setAutoSyncPref(true)">On</button>
          <button type="button" class="theme-opt" data-autosync-opt="off" onclick="setAutoSyncPref(false)">Off</button>
        </div>
        <div style="font-size:12px;color:var(--label);margin-top:8px;">Periodically checks your SimBrief account for a newer flight plan and pulls in updated times, gates, and crew for this flight — without opening SimBrief's dispatch page.</div>
      </div>
      <div class="search-block">
        <label for="aero-key">FlightAware AeroAPI Key</label>
        <input id="aero-key" type="password" placeholder="Your AeroAPI key" value="$aeroapi_key" onchange="saveAeroApiKey(this.value)">
      </div>
      <div class="search-block">
        <label for="tz-select">Timezone (for MOT)</label>
        <select id="tz-select" onchange="saveTimezone(this.value)">$timezone_options</select>
        <div style="font-size:12px;color:var(--label);margin-top:8px;">Controls what clock face Mandatory Off Time is shown in. Leave on "As Computed" to see it in the bid pack's own local time.</div>
      </div>
      <div class="search-block">
        <label>Appearance</label>
        <div style="display:flex;gap:8px;">
          <button type="button" class="theme-opt" data-theme-opt="light" onclick="setThemePref('light')">Light</button>
          <button type="button" class="theme-opt" data-theme-opt="auto" onclick="setThemePref('auto')">Auto</button>
          <button type="button" class="theme-opt" data-theme-opt="dark" onclick="setThemePref('dark')">Dark</button>
        </div>
      </div>
      <div class="search-block">
        <label for="pw-current">Change Password</label>
        <input id="pw-current" type="password" placeholder="Current password" autocomplete="current-password">
        <input id="pw-new" type="password" placeholder="New password (min. 8 characters)" autocomplete="new-password" style="margin-top:8px;">
        <button type="button" onclick="changePassword()" style="margin-top:10px;width:100%;background:var(--blue);color:#fff;border:none;padding:10px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;">Update Password</button>
        <div id="password-msg" style="margin-top:8px;font-size:12.5px;color:var(--label);"></div>
      </div>
      <div id="settings-msg" class="placeholder-note"></div>
      <div style="text-align:center;padding:18px 0 4px;font-size:12px;color:var(--label);">Version $app_version</div>
    </section>
    <section id="confirm-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>Flight $flight_designator</h1>
          <p>Confirm &amp; Generate Release</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="flight-summary">
        <div class="fnum">$flight_designator</div>
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
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>More</h1></div>
      </div>
      <p class="placeholder-note">Nothing here yet — SimBrief and ForeFlight moved to Overview's External Apps card; Settings is now the gear icon at top right.</p>
    </section>
  </main>
  <nav class="tabbar" aria-label="Primary">
    <button class="navtab active" id="tab-overview" onclick="navTab('overview')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><path d="M9 10l2 2 4-4"/></svg>
      <span>Overview</span>
    </button>
    <button class="navtab" id="tab-schedule" onclick="navTab('pairing')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/></svg>
      <span>Schedule</span>
    </button>
    <button class="navtab" id="tab-messages" onclick="navTab('messages')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      <span>Messages</span>
    </button>
    <button class="navtab" id="tab-docs" onclick="navTab('doclocker')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/><path d="M9.5 13h5M9.5 16h5"/></svg>
      <span>Docs</span>
    </button>
    <button class="navtab" id="tab-more" onclick="navTab('more')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
      <span>More</span>
    </button>
  </nav>
</div>
<div id="toast"></div>
<script>
const LEG_ID = "$leg_id";
const LEG_FLIGHT_NUMBER = "$flight_number";
const LEG_FLIGHT_DESIGNATOR = "$flight_designator";
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
const LEG_PENDING_DATE_SLIP = $pending_date_slip_json;
// Settings is now reachable from every view (the global gear, not just
// Overview's own topbar) — its own back chevron needs to return to
// whichever view was actually active, not a hardcoded 'overview'. Simple
// one-slot memory rather than a real history stack: fine since Settings
// is always a dead-end detour, never itself a jumping-off point to a
// third view.
let _preSettingsView = 'overview';
function showView(view){
  if(view === 'settings'){
    const activeSection = document.querySelector('.view.active');
    if(activeSection && activeSection.id !== 'settings-view'){
      _preSettingsView = activeSection.id.replace('-view', '');
    }
  }
  document.getElementById('overview-view').classList.toggle('active', view==='overview');
  document.getElementById('allcommands-view').classList.toggle('active', view==='allcommands');
  document.getElementById('release-view').classList.toggle('active', view==='release');
  document.getElementById('confirm-view').classList.toggle('active', view==='confirm');
  document.getElementById('pdf-view').classList.toggle('active', view==='pdf');
  document.getElementById('motlog-view').classList.toggle('active', view==='motlog');
  document.getElementById('sign-view').classList.toggle('active', view==='sign');
  document.getElementById('pairing-view').classList.toggle('active', view==='pairing');
  document.getElementById('recovery-view').classList.toggle('active', view==='recovery');
  document.getElementById('weather-view').classList.toggle('active', view==='weather');
  document.getElementById('doclocker-view').classList.toggle('active', view==='doclocker');
  document.getElementById('saveddocs-view').classList.toggle('active', view==='saveddocs');
  document.getElementById('messages-view').classList.toggle('active', view==='messages');
  document.getElementById('settings-view').classList.toggle('active', view==='settings');
  document.getElementById('more-view').classList.toggle('active', view==='more');
  document.getElementById('tab-overview').classList.toggle('active', view==='overview');
  document.getElementById('tab-schedule').classList.toggle('active', view==='pairing');
  document.getElementById('tab-docs').classList.toggle('active', view==='doclocker');
  document.getElementById('tab-messages').classList.toggle('active', view==='messages');
  // Release/Confirm are still only reached through the More tab; Settings
  // moved to the topbar gear icon (mobileCCI's five-tab bar has no
  // dedicated Release icon, but does put settings at top right, not
  // under More).
  document.getElementById('tab-more').classList.toggle('active', view==='release' || view==='confirm' || view==='more');
  window.scrollTo(0,0);
  if(view === 'release') initReleaseView();
  if(view === 'confirm') initConfirmView();
  if(view === 'sign') initSignPad();
  if(view === 'settings'){ updateThemeButtons(); updateAutoSyncButtons(); }
  if(view === 'saveddocs') initSavedDocs();
  if(view === 'pairing') initPairingView();
  if(view === 'overview') initOverviewPills();
  if(view === 'weather' && !_weatherLoaded) loadWeather();
}
// Schedule is now a leg-independent root (see /schedule) — its tabbar
// button always navigates there via a real page load, never an in-page
// showView() toggle, so its own back chevron never has a specific leg to
// dump you back into. The other tabs stay in-page IF this page already
// has a real leg loaded; from the leg-independent /schedule page itself
// (LEG_ID empty) they fall back to Home, which is schedule-independent
// and leg-aware (Current Flight tile) — there's nothing sensible for
// Overview/Messages/Docs to show without a leg.
function navTab(view){
  if(view === 'pairing'){ window.location.href = '/schedule'; return; }
  if(!LEG_ID){ window.location.href = '/'; return; }
  showView(view);
}
// Settings' own back chevron — go to whichever view was actually active
// before Settings (see _preSettingsView, set in showView()), not a fixed
// 'overview'. The one case _preSettingsView can't capture is arriving
// straight into Settings on page load (Home's gear links to
// /schedule?view=settings) — nothing was "active" yet to remember, so
// that still defaults to 'overview', which only makes sense with a real
// leg; on the leg-independent page it means Home instead.
function backFromSettings(){
  if(!LEG_ID && _preSettingsView === 'overview'){ window.location.href = '/'; return; }
  showView(_preSettingsView);
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
function saveTimezone(value){
  fetch('/settings/timezone', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({timezone: value})})
    .then(()=>{ document.getElementById('settings-msg').textContent = 'Saved.'; })
    .catch(()=>{ document.getElementById('settings-msg').textContent = 'Could not save — try again.'; });
}
async function changePassword(){
  const el = document.getElementById('password-msg');
  const currentEl = document.getElementById('pw-current');
  const newEl = document.getElementById('pw-new');
  const current = currentEl.value;
  const next = newEl.value;
  if(!current || !next){ el.textContent = 'Enter both your current and new password.'; el.style.color = 'var(--red)'; return; }
  if(next.length < 8){ el.textContent = 'New password must be at least 8 characters.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Updating…'; el.style.color = '';
  try {
    const r = await fetch('/settings/password', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({current_password: current, new_password: next})});
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Could not update password'; el.style.color = 'var(--red)'; return; }
    currentEl.value = ''; newEl.value = '';
    el.textContent = 'Password updated.'; el.style.color = 'var(--blue-dark)';
  } catch(e) { el.textContent = 'Request failed: ' + e; el.style.color = 'var(--red)'; }
}
function setThemePref(pref){
  if(pref === 'auto'){
    localStorage.removeItem('fos_theme');
    document.documentElement.removeAttribute('data-theme');
  } else {
    localStorage.setItem('fos_theme', pref);
    document.documentElement.setAttribute('data-theme', pref);
  }
  updateThemeButtons();
}
function updateThemeButtons(){
  const current = localStorage.getItem('fos_theme') || 'auto';
  document.querySelectorAll('[data-theme-opt]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.themeOpt === current);
  });
}

// Auto-Sync: periodically checks the pilot's SimBrief account for a newer
// OFP than the one this leg was last built from, and silently pulls it in
// via the same /generate path "Import from SimBrief" already uses — no
// popup, no visit to SimBrief's own dispatch page (that's a separate,
// explicit action under Release). Defaults on; toggle lives in Settings.
let _autoSyncTimer = null;
let _lastKnownOfpTs = '';
function isAutoSyncOn(){
  return localStorage.getItem('fos_autosync') !== 'off';
}
function setAutoSyncPref(on){
  localStorage.setItem('fos_autosync', on ? 'on' : 'off');
  updateAutoSyncButtons();
  if(on) startAutoSync(); else stopAutoSync();
}
function updateAutoSyncButtons(){
  const current = isAutoSyncOn() ? 'on' : 'off';
  document.querySelectorAll('[data-autosync-opt]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.autosyncOpt === current);
  });
}
function _simbriefUser(){
  const el = document.getElementById('release-user');
  return (localStorage.getItem('fos_simbrief_user') || (el && el.value) || '').trim();
}
async function syncFromSimbrief(manual){
  const user = _simbriefUser();
  if(!user){ if(manual) showToast('Add a SimBrief username in Settings first'); return; }
  const btn = document.getElementById('sync-btn');
  if(btn) btn.classList.add('syncing');
  try {
    const r = await fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({simbrief_user: user, carry_gates_from: LEG_ID})});
    const data = await r.json();
    if(!r.ok){ if(manual) showToast(data.error || 'Sync failed'); return; }
    if(String(data.id) === String(LEG_ID)){
      showToast('Synced updated SimBrief data');
      setTimeout(() => window.location.reload(), 700);
    } else if(manual){
      // _find() couldn't match this to the same flight — a different
      // pairing/date is on the account right now, not an update to this one.
      showToast('Current SimBrief plan is for a different flight — check Current Flight on Home');
    }
  } catch(e) {
    if(manual) showToast('Sync failed: ' + e);
  } finally {
    if(btn) btn.classList.remove('syncing');
  }
}
async function checkForSimbriefUpdate(){
  if(!isAutoSyncOn()) return;
  const user = _simbriefUser();
  if(!user) return;
  try {
    const r = await fetch('/simbrief-api/generated-at?user=' + encodeURIComponent(user));
    const ts = (await r.json()).time_generated || '';
    if(!ts) return;
    if(_lastKnownOfpTs && ts !== _lastKnownOfpTs){
      await syncFromSimbrief(false);
      return;
    }
    _lastKnownOfpTs = ts;
  } catch(e) { /* best-effort — try again next interval */ }
}
function startAutoSync(){
  if(_autoSyncTimer || !isAutoSyncOn()) return;
  checkForSimbriefUpdate();
  _autoSyncTimer = setInterval(checkForSimbriefUpdate, 3 * 60 * 1000);
}
function stopAutoSync(){
  if(_autoSyncTimer){ clearInterval(_autoSyncTimer); _autoSyncTimer = null; }
}

// Date-slip lockout: _store_leg (server.py) flags this leg when a sync
// actually moves dep_date off what it was — a real day slip like a
// diversion pushing a leg into the next morning, not just a same-day
// delay. Blocks the page until the pilot explicitly confirms (accepts the
// new day as the pairing's schedule going forward) or rejects (dismisses
// without changing the pairing baseline — see resolve_date_slip's own
// docstring for exactly what each does server-side).
function _fmtDateMDY(mmddyy){
  const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  const parts = (mmddyy || '').split('/');
  if(parts.length !== 3) return mmddyy || '';
  const mi = parseInt(parts[0], 10) - 1;
  return (months[mi] || parts[0]) + ' ' + parts[1];
}
// Generalized confirm/reject modal — reuses #date-slip-modal's DOM/CSS for
// any "stage a risky change, let the user confirm or reject" flow, not just
// the date-slip lockout it was originally built for (see also the leg-edit
// propose/resolve flow in initPairingView()).
let _modalOnConfirm = null, _modalOnReject = null;
function showConfirmModal(title, body, onConfirm, onReject){
  document.getElementById('ds-title').textContent = title;
  document.getElementById('ds-body').textContent = body;
  _modalOnConfirm = onConfirm;
  _modalOnReject = onReject;
  document.getElementById('date-slip-modal').classList.add('show');
}
function _modalAction(action){
  document.getElementById('date-slip-modal').classList.remove('show');
  const handler = action === 'confirm' ? _modalOnConfirm : _modalOnReject;
  _modalOnConfirm = null; _modalOnReject = null;
  if(handler) handler();
}
function showDateSlipModalIfPending(){
  if(!LEG_PENDING_DATE_SLIP) return;
  const slip = LEG_PENDING_DATE_SLIP;
  showConfirmModal(
    'FLT ' + (LEG_FLIGHT_DESIGNATOR || LEG_FLIGHT_NUMBER) + ' delayed',
    'Now departing ' + (slip.new_sched_out || '?') + ' on ' + _fmtDateMDY(slip.new_dep_date) +
      ". Confirm to accept this as the pairing's new scheduled time, or reject to dismiss.",
    () => resolveDateSlip('confirm'),
    () => resolveDateSlip('reject'),
  );
}
async function resolveDateSlip(action){
  try {
    await fetch('/fos/' + LEG_ID + '/resolve-date-slip', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action})});
  } catch(e) { /* best-effort — reload either way rather than leaving a stuck lockout */ }
  window.location.reload();
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
  document.getElementById('sbgen-date').value = todayZuluISO();  // placeholder — replaced below once the origin's local timezone is known
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
            // Match on ICAO-normalized codes, not the raw origin/destination
            // strings: a PBS-pairing leg's own l.origin is the pairing's
            // 3-letter station code, but LEG_ORIGIN can already be a
            // SimBrief-overwritten ICAO code (4-letter) once this leg's
            // been regenerated once before — a raw string match silently
            // fails forever after that, same bug as initOverviewPills().
            if(l.flight_number === LEG_FLIGHT_NUMBER && (l.origin_icao || l.origin) === LEG_ORIGIN_ICAO && (l.destination_icao || l.destination) === LEG_DESTINATION_ICAO){
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
  // Now that the origin's real timezone is known, replace the zulu
  // placeholder date with "today" in that zone.
  if(_origTzName) document.getElementById('sbgen-date').value = localDateISO(_origTzName);
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
    const depGateEl = document.getElementById('ov-dep-gate'); if(depGateEl) depGateEl.textContent = data.dep_gate || '';
    const arrGateEl = document.getElementById('ov-arr-gate'); if(arrGateEl) arrGateEl.textContent = data.arr_gate || '';
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
      // Generation itself is never blocked (soft gate) — the PDF renders
      // fine via viewDoc() either way. Only the Download links here stay
      // locked until FFD is signed, same as pdf-view's Export link.
      if(!data.fit_for_duty){
        status.textContent = 'Release generated — sign Fit for Duty (All Commands > FFD) to unlock downloads.';
        status.style.color = 'var(--red)';
        document.getElementById('release-rls-link').style.display = 'none';
        document.getElementById('release-wb-link').style.display = 'none';
        return;
      }
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
  // Render the bitmap at the screen's real device pixel ratio, not just
  // the CSS width — a canvas sized 1:1 to CSS pixels looks soft once the
  // browser upscales it on a Retina/high-DPI iPad. Same DPR-scaling
  // pattern the signature pad canvas already uses; canvas.style.width
  // stays at targetWidth so the displayed size is unchanged, only the
  // underlying bitmap gets sharper.
  const dpr = window.devicePixelRatio || 1;
  for(let pageNum = 1; pageNum <= pdf.numPages; pageNum++){
    if(token !== _pdfRenderToken) return;
    const page = await pdf.getPage(pageNum);
    const scale = (targetWidth / page.getViewport({scale:1}).width) * dpr;
    const viewport = page.getViewport({scale});
    const canvas = document.createElement('canvas');
    canvas.className = 'pdf-page';
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
  const banner = document.getElementById('pdf-ffd-banner');
  // Soft gate — viewing always works; Export locks until FFD is signed,
  // with a floating banner above the rendered pages as the reminder.
  if(data.fit_for_duty){
    exportLink.href = _pdfObjectUrl;
    exportLink.download = kind === 'rls' ? data.filename : (data.filename || 'release.pdf').replace('-RLS.pdf', '-' + kind.toUpperCase() + '.pdf');
    exportLink.style.opacity = '';
    exportLink.onclick = null;
    banner.style.display = 'none';
  } else {
    exportLink.removeAttribute('href');
    exportLink.style.opacity = '0.4';
    exportLink.onclick = (e) => { e.preventDefault(); showToast('Sign Fit for Duty first to export'); };
    banner.style.display = '';
  }
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
  showView('allcommands');
}
async function showMotLog(){
  showView('motlog');
  const body = document.getElementById('motlog-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  if(!LEG_SEQ){ body.innerHTML = '<p class="placeholder-note">This flight isn’t tied to a sequence.</p>'; return; }
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(LEG_SEQ) + '/mot-log');
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
    if(!data.legs.length){ body.innerHTML = '<p class="placeholder-note">No legs in this sequence.</p>'; return; }
    body.innerHTML = '';
    let lastDay = null;
    data.legs.forEach(leg => {
      if(leg.duty_day !== lastDay){
        lastDay = leg.duty_day;
        const hdr = document.createElement('div');
        hdr.className = 'panel-card-hdr';
        hdr.style.cssText = 'padding:14px 17px 6px;';
        hdr.textContent = 'Day ' + leg.duty_day;
        body.appendChild(hdr);
      }
      const row = document.createElement('div');
      row.className = 'doc-row';
      row.style.cssText = 'display:block;';
      const signedText = leg.generated
        ? (leg.fit_for_duty ? ('FFD signed ' + new Date(leg.signed_at).toLocaleString()) : 'Generated — FFD not signed')
        : 'Not generated yet';
      row.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:baseline;">' +
          '<span style="font-weight:700;font-size:15px;">' + (leg.flight_number || '—') + ' ' + leg.origin + '→' + leg.destination + '</span>' +
          '<span class="mot-time" style="font-size:15px;">' + leg.mot_display + '</span>' +
        '</div>' +
        '<div style="font-size:12.5px;color:' + (leg.fit_for_duty ? 'var(--label)' : 'var(--red)') + ';margin-top:3px;">' + signedText + '</div>' +
        (leg.fdp_remaining ? '<div style="font-size:12.5px;color:var(--label);margin-top:2px;">FDP ' + leg.fdp_remaining + '</div>' : '');
      body.appendChild(row);
    });
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}

// Fit for Duty and the eFlight Plan share the same canvas/signature slot
// on the leg (there's only ever one "current" signature, not one per
// document) — _signKind just tracks which check icon and which follow-up
// effect (fit_for_duty=True server-side, see sign_leg) this particular
// signing session is for.
let _signKind = 'eflightplan';
const _SIGN_KIND_COPY = {
  ffd: {title: 'Sign Fit for Duty Declaration', line: 'Attests you are fit for duty on this flight'},
  eflightplan: {title: 'Sign eFlight Plan', line: 'Acknowledges receipt of the current release'},
};
function openSignPad(kind){
  _signKind = kind || 'eflightplan';
  const copy = _SIGN_KIND_COPY[_signKind] || _SIGN_KIND_COPY.eflightplan;
  document.getElementById('sign-title').textContent = copy.title;
  document.getElementById('sign-status-line').textContent = copy.line;
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
    const r = await fetch('/fos/' + LEG_ID + '/sign', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({signature: dataUrl, kind: _signKind})});
    const data = await r.json();
    btn.disabled = false;
    if(!r.ok){ el.textContent = data.error || 'Sign failed'; el.style.color = '#c0392b'; return; }
    const checkId = _signKind === 'ffd' ? 'ffd-doc-check' : 'sign-check';
    const check = document.getElementById(checkId);
    if(check) check.classList.add('signed');
    showToast('Signed');
    showView('allcommands');
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
const TRASH_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>';
let _libraryLoaded = false;
function showScheduleTab(tab){
  document.getElementById('tab-mytrip').classList.toggle('active', tab==='mytrip');
  document.getElementById('tab-library').classList.toggle('active', tab==='library');
  document.getElementById('tab-layers').classList.toggle('active', tab==='layers');
  document.getElementById('tab-mytrip-btn').classList.toggle('active', tab==='mytrip');
  document.getElementById('tab-library-btn').classList.toggle('active', tab==='library');
  document.getElementById('tab-layers-btn').classList.toggle('active', tab==='layers');
  if(tab === 'library' && !_libraryLoaded){
    _libraryLoaded = true;
    initLibraryView();
  }
  if(tab === 'layers' && !_layersLoaded){
    _layersLoaded = true;
    layerShowList();
  }
}
// My Trip — the pilot's own active sequence pool (PbsImport.sequences),
// not scoped to whichever leg (if any) this page happened to be reached
// through. Zero sequences shows the Load/Generate empty-state (relocated
// here from Home); exactly one goes straight to its full duty-day view
// (the common case); two or more show a pickable list first, matching the
// Pairing Library's own Sequences list look.
async function initPairingView(){
  const body = document.getElementById('pairing-body');
  // Reached from a real leg (e.g. right after accepting a shift-day patch
  // or a recovery for THIS leg's own sequence) — jump straight to that
  // sequence's updated view rather than the whole-pool list below, same
  // as before Schedule became leg-independent. Only /schedule itself
  // (LEG_SEQ empty) falls through to the pool-wide list/empty-state.
  if(LEG_SEQ){ await myTripShowDetail(LEG_SEQ, false); return; }
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/sequences');
    const seqs = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (seqs.error || 'Failed to load') + '</p>'; return; }
    if(!seqs.length){ renderMyTripEmptyState(body); return; }
    if(seqs.length === 1){ await myTripShowDetail(seqs[0].seq, false); return; }
    myTripShowList(seqs);
  } catch(e) {
    body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>';
  }
}
function renderMyTripEmptyState(body){
  body.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding:14px;';
  const msg = document.createElement('p');
  msg.className = 'placeholder-note';
  msg.style.cssText = 'margin:0 0 12px;padding:0;';
  msg.textContent = "You don't have a pairing loaded yet.";
  const loadBtn = document.createElement('button');
  loadBtn.textContent = 'Load New Sequence';
  loadBtn.style.cssText = 'width:100%;margin:0 0 10px;background:var(--blue);color:#fff;border:none;padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;';
  loadBtn.onclick = () => { window.location.href = '/?open=load-sequence'; };
  const genBtn = document.createElement('button');
  genBtn.textContent = 'Generate a Pairing';
  genBtn.style.cssText = 'width:100%;margin:0;background:var(--card);color:var(--blue);border:1px solid var(--blue);padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;';
  genBtn.onclick = () => { window.location.href = '/?open=generate'; };
  wrap.appendChild(msg); wrap.appendChild(loadBtn); wrap.appendChild(genBtn);
  body.appendChild(wrap);
}
function myTripShowList(seqs){
  const body = document.getElementById('pairing-body');
  body.innerHTML = '';
  // Lives inside the pane's own blue title bar (via libraryBar's actionEl
  // slot), not floating above it as an orphan link — .clear-all-link is a
  // blue-text-on-plain-background style meant to sit next to a plain <h1>
  // (see Home's "Clear All"), which would be invisible against this bar's
  // own blue background.
  const tidyBtn = document.createElement('button');
  tidyBtn.className = 'lib-bar-action';
  tidyBtn.textContent = 'Tidy Up';
  tidyBtn.title = 'Remove every trip you promoted but never flew or saved';
  tidyBtn.onclick = (e) => { e.stopPropagation(); tidyMyTrips(); };
  const {pane, list} = libraryPane('My Trips', null, tidyBtn);
  seqs.forEach(s => list.appendChild(sequenceListRow(s, () => myTripShowDetail(s.seq, true))));
  body.appendChild(pane);
}
async function tidyMyTrips(){
  if(!confirm("Remove every trip in My Trips that you haven't flown a leg of or saved? Saved and in-progress trips are kept.")) return;
  try {
    const r = await fetch('/pbs/sequences/tidy', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Tidy failed'); return; }
    showToast(data.removed ? `Removed ${data.removed} trip${data.removed === 1 ? '' : 's'}` : 'Nothing to remove');
    initPairingView();
  } catch(e) { showToast('Request failed: ' + e); }
}
async function myTripShowDetail(seqNumber, showBack){
  const body = document.getElementById('pairing-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seqNumber));
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
    renderPairing(data);
    if(showBack) body.insertBefore(libraryBackLink('Back to My Trips', initPairingView), body.firstChild);
  } catch(e) {
    body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>';
  }
}
// Duty-day card — shared by My Trip (renderPairing, interactive: tap a
// leg to fly it, pencil icon to edit it) and the Pairing Library's
// sequence detail (read-only preview of a not-yet-promoted pairing).
// Renders as a real <table> mirroring the bid pack's own column grid —
// DP / D-A / Flt·Eq / Dep / Arr / Blk·Gnd — not a list of app rows. RPT
// and RLS are each a genuine full-width row of their own (spanning every
// column), sitting right before the first leg and right after the last
// leg — not stacked inside a leg's own cell, which read as crowding the
// leg's own departure/arrival next to it. HOTEL/rest renders separately,
// between two day cards, never inside one.
//
// NOTE: string concatenation throughout, not template literals — see the
// Pairing Library comment above initLibraryView for why (Python's
// string.Template silently eats a bare ${identifier} that collides with a
// real template context key).
function dutyDayCardHtml(day, withEditIcons){
  const legs = day.legs || [];
  const colCount = 6;
  const rptRow = day.report
    ? '<tr class="marker-row"><td colspan="' + colCount + '">RPT <b>' + day.report + '/' + (day.report_hbt || day.report) + '</b></td></tr>'
    : '';
  let rows = rptRow;
  legs.forEach((leg, i) => {
    const gndLine = leg.ground ? ('<div class="sub">' + leg.ground + '</div>') : '';
    // No operator prefix here — it's the same operator for every leg in
    // the trip, so it's shown once in the summary line above instead of
    // repeated on every single row.
    const fltText = leg.flight_number || '—';
    const editIcon = withEditIcons
      ? '<svg class="edit-icon" data-leg-index="' + i + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><title>Edit this leg</title><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>'
      : '';
    rows += '<tr data-leg-index="' + i + '">' +
      '<td><div class="dp-cell">' + (i + 1) + editIcon + '</div></td>' +
      '<td>' + (leg.da || '') + '</td>' +
      '<td><div class="flt">' + fltText + '</div><div class="sub">' + (leg.equipment || '') + '</div></td>' +
      '<td><div class="sta">' + leg.origin + '</div><div class="sub">' + leg.dep_local + '/' + leg.dep_z + '</div></td>' +
      '<td><div class="sta">' + leg.destination + '</div><div class="sub">' + leg.arr_local + '/' + leg.arr_z + '</div></td>' +
      '<td><div class="blk">' + (leg.block || '') + '</div>' + gndLine + '</td>' +
      '</tr>';
  });
  if(day.release){
    rows += '<tr class="marker-row"><td colspan="' + colCount + '">RLS <b>' + day.release + '/' + (day.release_hbt || day.release) + '</b></td></tr>';
  }
  // No per-day TAFB — it's only a whole-pairing figure (see pbs_parser's
  // RLS_RE comment); the real cumulative one shows on the TOTAL card below.
  const summaryBits = [
    day.block ? ('Blk <b>' + day.block + '</b>') : '',
    day.duty ? ('Duty <b>' + day.duty + '</b>') : '',
  ].filter(Boolean).join(' &middot; ');
  return '<div class="dd-card">' +
    '<div class="dd-hdr">Day ' + day.duty_day + '</div>' +
    '<div class="dd-table-wrap"><table class="dd-table"><thead><tr>' +
      '<th>Dp</th><th>D/A</th><th>Flt/Eq</th>' +
      '<th>STA<br>DLCL/DHBT</th><th>STA<br>ALCL/AHBT</th><th>Blk/Gnd</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
    (summaryBits ? ('<div class="dd-summary">' + summaryBits + '</div>') : '') +
    '</div>';
}
function layoverHtml(day){
  if(!day.hotel) return '';
  const spaceIdx = day.hotel.indexOf(' ');
  const sta = spaceIdx === -1 ? day.hotel : day.hotel.slice(0, spaceIdx);
  const name = spaceIdx === -1 ? '' : day.hotel.slice(spaceIdx + 1);
  return '<div class="layover"><span><b>' + sta + '</b> HOTEL ' + name + '</span>' +
    (day.hotel_rest ? ('<span class="rest">REST: <b>' + day.hotel_rest + '</b></span>') : '') + '</div>';
}
function totalCardHtml(seqData){
  const bits = [
    seqData.block ? ('Block <b>' + seqData.block + '</b>') : '',
    seqData.tpay ? ('TPAY <b>' + seqData.tpay + '</b>') : '',
    seqData.tafb ? ('TAFB <b>' + seqData.tafb + '</b>') : '',
  ].filter(Boolean).join(' &middot; ');
  if(!bits) return '';
  return '<div class="dd-card"><div class="dd-hdr">Total</div><div class="dd-summary" style="justify-content:center;border-top:none;">' + bits + '</div></div>';
}
// opts: {interactive, onRowClick(day,legIndex,leg), onEditClick(row,day,legIndex,leg)}
function renderDutyDayCards(container, seqData, opts){
  opts = opts || {};
  const dutyDays = seqData.duty_days || [];
  dutyDays.forEach(day => {
    const wrap = document.createElement('div');
    wrap.innerHTML = dutyDayCardHtml(day, !!opts.interactive);
    const cardEl = wrap.firstElementChild;
    if(opts.interactive){
      cardEl.querySelectorAll('tbody tr').forEach((row, i) => {
        const leg = (day.legs || [])[i];
        row.style.cursor = 'pointer';
        row.onclick = () => opts.onRowClick(day, i, leg);
        const editIcon = row.querySelector('.edit-icon');
        if(editIcon) editIcon.onclick = (e) => { e.stopPropagation(); opts.onEditClick(row, day, i, leg); };
      });
    }
    container.appendChild(cardEl);
    const layoverWrap = document.createElement('div');
    layoverWrap.innerHTML = layoverHtml(day);
    if(layoverWrap.firstElementChild) container.appendChild(layoverWrap.firstElementChild);
  });
  if(!dutyDays.length){
    container.innerHTML = '<p class="placeholder-note">No duty days on this sequence.</p>';
    return;
  }
  const totalWrap = document.createElement('div');
  totalWrap.innerHTML = totalCardHtml(seqData);
  if(totalWrap.firstElementChild) container.appendChild(totalWrap.firstElementChild);
}
function renderPairing(seqData){
  const body = document.getElementById('pairing-body');
  body.innerHTML = '';
  const position = LEG_POSITION || (seqData.positions && seqData.positions[0]) || '';
  const flightPrefix = seqData.operator_iata || seqData.operator || '';

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
  if(seqData.active) summaryBits.unshift('● ACTIVE');
  if(firstLeg && lastLeg) summaryBits.unshift(firstLeg.origin + ' → ' + lastLeg.destination);
  if(flightPrefix) summaryBits.unshift(flightPrefix);
  summary.textContent = summaryBits.filter(Boolean).join('  ·  ');
  body.appendChild(summary);

  // Pick Up (trip check-in) and generating legs to SimBrief are
  // independent actions now, usable in either order — Pick Up is a plain
  // one-tap check-in (no signature; that requirement was cut), and
  // Generate & Cache is always available whether or not the trip has
  // been picked up yet.
  const pickupWrap = document.createElement('div');
  pickupWrap.style.cssText = 'padding:11px 14px;background:var(--card);border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:8px;';
  const pickupBtn = document.createElement('button');
  if(seqData.active){
    pickupBtn.textContent = 'Close Trip';
    pickupBtn.style.cssText = 'margin:0;width:100%;background:var(--label);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
    pickupBtn.onclick = () => closeTrip(seqData.seq);
  } else {
    pickupBtn.textContent = 'Pick Up';
    pickupBtn.style.cssText = 'margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
    pickupBtn.onclick = () => pickUpTrip(seqData.seq);
  }
  pickupWrap.appendChild(pickupBtn);
  if(seqData.active && firstLeg){
    // The one clear path from "picked up" to the actual flight page —
    // previously there was no obvious way to get from a checked-in trip
    // in Schedule to that trip's real /fos/<id> Overview.
    const goBtn = document.createElement('button');
    goBtn.textContent = 'Go to Flight';
    goBtn.style.cssText = 'margin:0;width:100%;background:var(--green,#34c759);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
    goBtn.onclick = () => generatePairingLeg(seqData.seq, dutyDays[0].duty_day, 0, position);
    pickupWrap.appendChild(goBtn);
  }
  body.appendChild(pickupWrap);

  const cacheWrap = document.createElement('div');
  cacheWrap.style.cssText = 'padding:11px 14px;background:var(--card);border-bottom:1px solid var(--border);';
  const cacheBtn = document.createElement('button');
  cacheBtn.textContent = 'Generate & Cache All Legs in Sequence';
  cacheBtn.style.cssText = 'margin:0;width:100%;background:var(--card);color:var(--blue);border:1px solid var(--blue);padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
  const cacheMsg = document.createElement('div');
  cacheMsg.style.cssText = 'margin-top:8px;font-size:12.5px;color:var(--label);';
  cacheBtn.onclick = () => cacheAllPairingLegs(seqData, position, cacheMsg);
  cacheWrap.appendChild(cacheBtn);
  cacheWrap.appendChild(cacheMsg);
  body.appendChild(cacheWrap);

  const cardsWrap = document.createElement('div');
  cardsWrap.style.cssText = 'padding:0 14px 14px;';
  renderDutyDayCards(cardsWrap, seqData, {
    interactive: true,
    onRowClick: (day, i, leg) => generatePairingLeg(seqData.seq, day.duty_day, i, position),
    onEditClick: (row, day, i, leg) => toggleLegEditForm(row, seqData.seq, day.duty_day, i, leg),
  });
  body.appendChild(cardsWrap);

  const dropWrap = document.createElement('div');
  dropWrap.style.cssText = 'padding:0 14px 18px;';
  const dropBtn = document.createElement('button');
  dropBtn.textContent = 'Drop Trip';
  dropBtn.style.cssText = 'margin:0;width:100%;background:var(--red);color:#fff;border:none;padding:12px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;';
  dropBtn.onclick = () => dropTrip(seqData.seq);
  dropWrap.appendChild(dropBtn);
  body.appendChild(dropWrap);
}
async function dropTrip(seq){
  if(!confirm('Drop SEQ ' + seq + ' from My Trips? Any flights you already generated from it stay in your archive — this only removes it from the active list.')) return;
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq), {method: 'DELETE'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not drop this trip'); return; }
    showToast('Trip dropped');
    initPairingView();
  } catch(e) { showToast('Request failed: ' + e); }
}
async function pickUpTrip(seq){
  // A plain one-tap check-in — no signature. Independent of Generate &
  // Cache, which stays its own separate button: either can be done first.
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/pick-up', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not pick up this trip'); return; }
    showToast('Trip picked up');
    myTripShowDetail(seq, true);
  } catch(e) { showToast('Request failed: ' + e); }
}
async function closeTrip(seq){
  if(!confirm('Close SEQ ' + seq + '? You can pick it back up later, or pick up a different trip once it’s closed.')) return;
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/close', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not close this trip'); return; }
    showToast('Trip closed');
    myTripShowDetail(seq, true);
  } catch(e) { showToast('Request failed: ' + e); }
}

// Pairing Library — bulk-imported bid-pack files, browsed
// opr -> base -> fleet -> sequence. Picking one promotes it into this
// pilot's own active sequence pool (same as accepting a generated
// pairing), then hands off to the existing Pick-a-Leg flow to start
// flying it.
//
// NOTE: dynamic strings below use concatenation, not JS template
// literals — Python's string.Template (rendering this whole page) treats
// bare ${word} as ITS OWN substitution syntax and will silently eat one
// that happens to match a real template context key (e.g. ${base} was
// getting replaced with this leg's own base — cost real debugging time to
// track down). String concatenation sidesteps it entirely.
let _libraryPath = {opr: null, base: null, fleet: null};
let _libraryShortcut = null;
let _savedPairings = [];

async function initLibraryView(){
  const [scRes, savedRes] = await Promise.all([
    fetch('/settings/bid-shortcut'),
    fetch('/settings/saved-pairings'),
  ]);
  _libraryShortcut = scRes.ok ? await scRes.json() : null;
  _savedPairings = savedRes.ok ? await savedRes.json() : [];
  renderLibraryShortcutBar();
  await libraryShowOprs();
}
function isPairingSaved(opr, base, fleet, seq){
  return _savedPairings.some(p => p.opr === opr && p.base === base && p.fleet === fleet && p.seq === seq);
}
function renderLibraryShortcutBar(){
  const crumb = document.getElementById('library-crumb');
  crumb.innerHTML = '';
  if(_libraryShortcut){
    const row = document.createElement('div');
    row.className = 'arow';
    row.innerHTML = '<a class="arow-link" href="#">⭐ ' + _libraryShortcut.label + '</a>' +
      '<button class="arow-del" title="Clear saved bid">' + TRASH_ICON_SVG + '</button>';
    row.querySelector('.arow-link').onclick = (e) => { e.preventDefault(); libraryJumpToShortcut(); };
    row.querySelector('.arow-del').onclick = (e) => { e.stopPropagation(); clearBidShortcut(); };
    crumb.appendChild(row);
  }
  if(_savedPairings.length){
    const label = document.createElement('div');
    label.textContent = 'SAVED PAIRINGS';
    label.style.cssText = 'font-size:11px;font-weight:700;color:var(--label);letter-spacing:.04em;padding:2px 14px 6px;';
    crumb.appendChild(label);
    _savedPairings.forEach(p => {
      const row = document.createElement('a');
      row.className = 'arow'; row.href = '#';
      row.innerHTML = '<span style="color:var(--blue);font-weight:600;">SEQ ' + p.seq + '</span>' +
        '<span>' + p.opr + ' ' + p.base + '/' + p.fleet + '</span>';
      row.onclick = (e) => {
        e.preventDefault();
        _libraryPath = {opr: p.opr, base: p.base, fleet: p.fleet};
        libraryShowSequenceDetail(p.seq);
      };
      crumb.appendChild(row);
    });
  }
}
async function libraryJumpToShortcut(){
  if(!_libraryShortcut) return;
  _libraryPath = {opr: _libraryShortcut.opr, base: _libraryShortcut.base, fleet: _libraryShortcut.fleet};
  await libraryShowSequences();
}
async function clearBidShortcut(){
  await fetch('/settings/bid-shortcut', {method: 'DELETE'});
  _libraryShortcut = null;
  renderLibraryShortcutBar();
}
async function saveBidShortcut(opr, base, fleet){
  const r = await fetch('/settings/bid-shortcut', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({opr, base, fleet}),
  });
  const data = await r.json();
  if(r.ok){ _libraryShortcut = data; renderLibraryShortcutBar(); showToast('Saved as your bid'); }
}
async function toggleSavedPairing(opr, base, fleet, seq, btnEl){
  try {
    const r = await fetch('/settings/saved-pairings/toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({opr, base, fleet, seq}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not update saved pairings'); return; }
    _savedPairings = data;
    const nowSaved = isPairingSaved(opr, base, fleet, seq);
    if(btnEl) btnEl.classList.toggle('saved', nowSaved);
    renderLibraryShortcutBar();
    showToast(nowSaved ? 'Pairing saved' : 'Removed from saved');
  } catch(e) { showToast('Request failed: ' + e); }
}
// Breadcrumb for jumping several levels at once. extraLabel (e.g. "SEQ
// 149764") is appended as the non-clickable current level, pushing the
// deepest real path segment (fleet) into a clickable link instead.
function libraryCrumb(extraLabel){
  const p = _libraryPath;
  const parts = [{label: 'Operators', fn: libraryShowOprs}];
  if(p.opr) parts.push({label: p.opr, fn: libraryShowBases});
  if(p.base) parts.push({label: p.base, fn: libraryShowFleets});
  if(p.fleet) parts.push({label: p.fleet, fn: libraryShowSequences});
  const el = document.createElement('div');
  el.className = 'lib-crumb';
  const lastIsCurrent = !extraLabel;
  parts.forEach((part, i) => {
    if(i > 0){ const sep = document.createElement('span'); sep.textContent = '/'; el.appendChild(sep); }
    if(lastIsCurrent && i === parts.length - 1){
      const cur = document.createElement('span'); cur.className = 'current'; cur.textContent = part.label;
      el.appendChild(cur);
    } else {
      const a = document.createElement('a'); a.href = '#'; a.textContent = part.label;
      a.onclick = (e) => { e.preventDefault(); part.fn(); };
      el.appendChild(a);
    }
  });
  if(extraLabel){
    const sep = document.createElement('span'); sep.textContent = '/'; el.appendChild(sep);
    const cur = document.createElement('span'); cur.className = 'current'; cur.textContent = extraLabel;
    el.appendChild(cur);
  }
  return el;
}
// A single, obvious "go up one level" link — the crumb above still lets
// you jump multiple levels, this is the fast one-tap path back.
function libraryBackLink(label, fn){
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding:0 14px 8px;';
  const a = document.createElement('a');
  a.href = '#';
  a.style.cssText = 'color:var(--blue);font-size:13px;text-decoration:none;display:inline-flex;align-items:center;gap:4px;';
  a.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" style="width:9px;height:13px;"><path d="M15 18l-6-6 6-6"/></svg> ' + label;
  a.onclick = (e) => { e.preventDefault(); fn(); };
  wrap.appendChild(a);
  return wrap;
}
function libraryBar(title, backFn, actionEl){
  const bar = document.createElement('div');
  bar.className = 'section-bar lib-bar';
  if(backFn){
    const back = document.createElement('span');
    back.className = 'lib-back';
    back.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>';
    back.onclick = (e) => { e.stopPropagation(); backFn(); };
    bar.appendChild(back);
  }
  const label = document.createElement('span');
  label.textContent = title;
  bar.appendChild(label);
  // Pushed to the far right regardless of the bar's own flex-start layout —
  // margin-left:auto does this in any flex row, no separate right-aligned
  // wrapper needed.
  if(actionEl){ actionEl.style.marginLeft = 'auto'; bar.appendChild(actionEl); }
  return bar;
}
function libraryPane(title, backFn, actionEl){
  const pane = document.createElement('div');
  pane.style.cssText = 'margin:0 14px 14px;border-radius:16px;overflow:hidden;border:1px solid var(--border);';
  pane.appendChild(libraryBar(title, backFn, actionEl));
  const list = document.createElement('div');
  list.className = 'doc-list';
  pane.appendChild(list);
  return {pane, list};
}
function libraryDisclosureIcon(){
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.innerHTML = '<path d="M9 18l6-6-6-6"/>';
  return svg;
}
// One "opr" or "base" drill-down row — a plain count + disclosure chevron,
// plus an optional extra action icon (the fleet level's save-as-bid star).
function libraryGroupRow(name, count, onClick, extraActionEl){
  const row = document.createElement('div');
  row.className = 'doc-row lib-row';
  const left = document.createElement('div');
  const code = document.createElement('div'); code.className = 'code'; code.textContent = name;
  const desc = document.createElement('div'); desc.className = 'desc';
  desc.textContent = count + ' pairing' + (count === 1 ? '' : 's');
  left.appendChild(code); left.appendChild(desc);
  const actions = document.createElement('div'); actions.className = 'actions';
  if(extraActionEl) actions.appendChild(extraActionEl);
  actions.appendChild(libraryDisclosureIcon());
  row.appendChild(left); row.appendChild(actions);
  row.onclick = onClick;
  return row;
}
// Renders a sequence's station chain with layover stations bolded (where
// this pairing actually overnights) so it reads at a glance, distinct
// from stations the pairing only touches down/up at same-day.
function libraryRoutingHtml(routing, layoverIndices){
  const layovers = {};
  (layoverIndices || []).forEach(i => { layovers[i] = true; });
  return (routing || []).map((sta, i) => layovers[i] ? ('<b>' + sta + '</b>') : sta).join('-');
}
async function libraryShowOprs(){
  _libraryPath = {opr: null, base: null, fleet: null};
  const body = document.getElementById('library-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs');
    const packs = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (packs.error || 'Failed to load') + '</p>'; return; }
    const oprs = {};
    packs.forEach(p => { oprs[p.opr] = (oprs[p.opr] || 0) + p.seq_count; });
    body.innerHTML = '';
    body.appendChild(libraryCrumb());
    const names = Object.keys(oprs).sort();
    if(!names.length){
      body.innerHTML += '<p class="placeholder-note">No pairing packs imported yet.</p>';
      return;
    }
    const {pane, list} = libraryPane('Operators', null);
    names.forEach(oprName => {
      list.appendChild(libraryGroupRow(oprName, oprs[oprName], () => { _libraryPath.opr = oprName; libraryShowBases(); }));
    });
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
async function libraryShowBases(){
  const body = document.getElementById('library-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs');
    const packs = (await r.json()).filter(p => p.opr === _libraryPath.opr);
    const bases = {};
    packs.forEach(p => { bases[p.base] = (bases[p.base] || 0) + p.seq_count; });
    body.innerHTML = '';
    body.appendChild(libraryCrumb());
    const {pane, list} = libraryPane('Bases', libraryShowOprs);
    Object.keys(bases).sort().forEach(baseName => {
      list.appendChild(libraryGroupRow(baseName, bases[baseName], () => { _libraryPath.base = baseName; libraryShowFleets(); }));
    });
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
async function libraryShowFleets(){
  const body = document.getElementById('library-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs');
    const packs = (await r.json()).filter(p => p.opr === _libraryPath.opr && p.base === _libraryPath.base);
    body.innerHTML = '';
    body.appendChild(libraryCrumb());
    const {pane, list} = libraryPane('Fleets', libraryShowBases);
    packs.sort((a,b) => a.fleet.localeCompare(b.fleet)).forEach(p => {
      const star = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      star.setAttribute('viewBox', '0 0 24 24');
      star.setAttribute('fill', 'none');
      star.setAttribute('stroke', 'currentColor');
      star.setAttribute('stroke-width', '2');
      star.setAttribute('stroke-linecap', 'round');
      star.setAttribute('stroke-linejoin', 'round');
      const titleEl = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      titleEl.textContent = 'Save as my bid';
      star.appendChild(titleEl);
      star.innerHTML += '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>';
      star.style.color = 'var(--blue)';
      star.onclick = (e) => { e.stopPropagation(); saveBidShortcut(_libraryPath.opr, _libraryPath.base, p.fleet); };
      list.appendChild(libraryGroupRow(p.fleet, p.seq_count, () => { _libraryPath.fleet = p.fleet; libraryShowSequences(); }, star));
    });
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
// Shared by the Pairing Library's Sequences list and My Trip's own list —
// SEQ in blue, layover stations bold, a plain touched airport in neither,
// TAFB/TPAY (when known) above the day-count badge.
function sequenceListRow(s, onClick){
  const row = document.createElement('div');
  row.className = 'doc-row lib-row';
  const left = document.createElement('div');
  const code = document.createElement('div'); code.className = 'code seq-code';
  code.textContent = (s.active ? '● ' : '') + 'SEQ ' + s.seq + (s.active ? ' (ACTIVE — picked up)' : '');
  const desc = document.createElement('div'); desc.className = 'desc lib-routing';
  desc.innerHTML = libraryRoutingHtml(s.routing, s.layover_indices);
  left.appendChild(code); left.appendChild(desc);
  const stats = document.createElement('div'); stats.className = 'lib-stats';
  const payBits = [s.tafb ? ('TAFB ' + s.tafb) : '', s.tpay ? ('TPAY ' + s.tpay) : ''].filter(Boolean);
  if(payBits.length){
    const payLine = document.createElement('div');
    payLine.textContent = payBits.join(' · ');
    stats.appendChild(payLine);
  }
  const daysLine = document.createElement('div'); daysLine.className = 'days';
  daysLine.textContent = s.days + ' day' + (s.days === 1 ? '' : 's');
  stats.appendChild(daysLine);
  row.appendChild(left); row.appendChild(stats);
  row.onclick = onClick;
  return row;
}
async function libraryShowSequences(){
  const {opr, base, fleet} = _libraryPath;
  const body = document.getElementById('library-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs/' + opr + '/' + base + '/' + fleet + '/sequences');
    const seqs = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (seqs.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryCrumb());
    if(!seqs.length){ body.innerHTML += '<p class="placeholder-note">No sequences in this pack.</p>'; return; }
    const {pane, list} = libraryPane('Sequences', libraryShowFleets);
    seqs.forEach(s => list.appendChild(sequenceListRow(s, () => libraryShowSequenceDetail(s.seq))));
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
async function libraryShowSequenceDetail(seqNumber){
  const {opr, base, fleet} = _libraryPath;
  const body = document.getElementById('library-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs/' + opr + '/' + base + '/' + fleet + '/sequences/' + encodeURIComponent(seqNumber));
    const seqData = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (seqData.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryCrumb('SEQ ' + seqNumber));
    body.appendChild(libraryBackLink('Back to Sequences', libraryShowSequences));

    // Block/TPAY/TAFB moved to the Total card at the bottom (below every
    // duty day, via renderDutyDayCards' own totalCardHtml) — this line is
    // just operator + crew position now.
    const totalBits = [
      opr,
      (seqData.positions || []).join('/'),
    ].filter(Boolean);
    if(totalBits.length){
      const totalEl = document.createElement('div');
      totalEl.className = 'lib-total';
      totalEl.textContent = totalBits.join(' · ');
      body.appendChild(totalEl);
    }

    const flyRow = document.createElement('div');
    flyRow.className = 'fly-row';
    const flyBtn = document.createElement('button');
    flyBtn.className = 'fly-btn';
    flyBtn.textContent = 'Fly This Pairing';
    flyBtn.onclick = () => promoteAndFly(opr, base, fleet, seqNumber);
    const saveBtn = document.createElement('button');
    saveBtn.className = 'save-btn';
    saveBtn.title = 'Save this pairing';
    saveBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>';
    if(isPairingSaved(opr, base, fleet, seqNumber)) saveBtn.classList.add('saved');
    saveBtn.onclick = () => toggleSavedPairing(opr, base, fleet, seqNumber, saveBtn);
    flyRow.appendChild(flyBtn);
    flyRow.appendChild(saveBtn);
    body.appendChild(flyRow);

    const cardsWrap = document.createElement('div');
    cardsWrap.style.cssText = 'padding:0 14px 14px;';
    renderDutyDayCards(cardsWrap, seqData, {interactive: false});
    body.appendChild(cardsWrap);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
async function promoteAndFly(opr, base, fleet, seqNumber){
  // FOS_TEMPLATE has no leg-picker UI of its own (that lives on Home) — so
  // rather than promote-then-redirect-to-Home-then-pick-a-leg, promote and
  // immediately generate day 1 leg 0, landing straight on its FOS page,
  // the same way tapping a leg in renderPairing's own duty-day cards does.
  try {
    const r = await fetch('/pbs/packs/' + opr + '/' + base + '/' + fleet + '/sequences/' + encodeURIComponent(seqNumber) + '/promote', {method: 'POST'});
    const seqData = await r.json();
    if(!r.ok){ showToast(seqData.error || 'Could not promote this pairing'); return; }
    const position = (seqData.positions && seqData.positions[0]) || '';
    const genR = await fetch('/pbs/sequences/' + encodeURIComponent(seqNumber) + '/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: 1, leg_index: 0, position}),
    });
    const genData = await genR.json();
    if(!genR.ok){ showToast(genData.error || 'Promoted, but could not start flying it'); return; }
    window.location.href = genData.fos_url + '?view=release';
  } catch(e) { showToast('Request failed: ' + e); }
}

// Bid Layers — saved filter criteria over one Pairing Library pack
// (PairingPack), for sorting through a pack's own hundreds of sequences
// instead of scrolling all of them. Every route lives under /pbs/layers;
// see _layer_matches() in server.py for what "properties" supports.
// Reuses libraryPane/libraryGroupRow/sequenceListRow/renderDutyDayCards —
// only the list/form/matches rendering here is new.
let _layersLoaded = false;
async function layerShowList(){
  const body = document.getElementById('layers-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/layers');
    const layers = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (layers.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    const newBtn = document.createElement('button');
    newBtn.className = 'lib-bar-action';
    newBtn.textContent = '+ New';
    newBtn.onclick = (e) => { e.stopPropagation(); layerShowForm(null); };
    const {pane, list} = libraryPane('Bid Layers', null, newBtn);
    if(!layers.length){
      list.innerHTML = '<p class="placeholder-note" style="padding:14px;">No layers yet — save a filter to start sorting a pack’s pairings.</p>';
    }
    layers.forEach(layer => list.appendChild(layerRow(layer)));
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
function layerRow(layer){
  const row = document.createElement('div');
  row.className = 'doc-row lib-row';
  const left = document.createElement('div');
  const code = document.createElement('div'); code.className = 'code'; code.textContent = layer.name;
  const desc = document.createElement('div'); desc.className = 'desc';
  desc.textContent = layer.opr + ' ' + layer.base + '/' + layer.fleet;
  left.appendChild(code); left.appendChild(desc);
  const stats = document.createElement('div'); stats.className = 'lib-stats';
  const countLine = document.createElement('div');
  countLine.textContent = layer.count + ' pairing' + (layer.count === 1 ? '' : 's');
  stats.appendChild(countLine);
  row.appendChild(left); row.appendChild(stats);
  row.onclick = () => layerShowPairings(layer);
  return row;
}
// Round-number dropdowns instead of free-typed numbers — less error-prone
// (no fat-fingered "45" meant to be "4.5"), and on iOS a plain <select>
// already renders as the native scroll-wheel picker, so this is both
// asks in one change.
function _rangeArray(start, end, step){
  const out = [];
  for(let v = start; v <= end; v += step) out.push(v);
  return out;
}
function _rangeOptionsHtml(values, current){
  const cur = (current === null || current === undefined) ? '' : String(current);
  let html = '<option value=""' + (cur === '' ? ' selected' : '') + '>Any</option>';
  values.forEach(v => {
    html += '<option value="' + v + '"' + (String(v) === cur ? ' selected' : '') + '>' + v + '</option>';
  });
  return html;
}
async function layerShowForm(existing){
  const body = document.getElementById('layers-body');
  body.innerHTML = '';
  body.appendChild(libraryBackLink('Back to Bid Layers', layerShowList));
  const panel = document.createElement('div');
  panel.className = 'panel';
  const p = (existing && existing.properties) || {};
  panel.innerHTML =
    '<label>Layer Name (only needed to Save)</label>' +
    '<input class="lf-name" type="text" placeholder="Adjust filters freely — name it when you’re ready to save" value="' + (existing ? existing.name.replace(/"/g, '&quot;') : '') + '">' +
    (existing
      ? ''
      : '<label>Operator</label><select class="lf-opr"></select>' +
        '<label>Base</label><select class="lf-base"></select>' +
        '<label>Fleet</label><select class="lf-fleet"></select>') +
    '<label>Days (min / max)</label>' +
    '<div style="display:flex;gap:8px;">' +
      '<select class="lf-min-days">' + _rangeOptionsHtml(_rangeArray(1, 10, 1), p.min_days) + '</select>' +
      '<select class="lf-max-days">' + _rangeOptionsHtml(_rangeArray(1, 10, 1), p.max_days) + '</select>' +
    '</div>' +
    '<label>Min Block Hours</label>' +
    '<select class="lf-min-block">' + _rangeOptionsHtml(_rangeArray(0, 60, 5), p.min_block) + '</select>' +
    '<label>Min TAFB Hours</label>' +
    '<select class="lf-min-tafb">' + _rangeOptionsHtml(_rangeArray(0, 150, 10), p.min_tafb) + '</select>' +
    '<label>Max Legs Per Day</label>' +
    '<select class="lf-max-legs">' + _rangeOptionsHtml(_rangeArray(1, 6, 1), p.max_legs_per_day) + '</select>' +
    '<label>Red-Eyes</label>' +
    '<select class="lf-redeye">' +
      '<option value="any"' + ((_redEyeMode(p) === 'any') ? ' selected' : '') + '>Any</option>' +
      '<option value="exclude"' + ((_redEyeMode(p) === 'exclude') ? ' selected' : '') + '>Exclude Red-Eyes</option>' +
      '<option value="only"' + ((_redEyeMode(p) === 'only') ? ' selected' : '') + '>Only Red-Eyes</option>' +
    '</select>' +
    '<label>Layover Include (must overnight here — station codes, comma separated)</label>' +
    '<input class="lf-layover-include" type="text" placeholder="e.g. MIA, LAX" value="' + ((p.layover_include || []).join(', ')) + '">' +
    '<label>Include (touches this stop at all, same-day or overnight)</label>' +
    '<input class="lf-include" type="text" placeholder="e.g. MIA, LAX" value="' + ((p.include_stations || []).join(', ')) + '">' +
    '<label>Avoid (never touches this stop, same-day or overnight)</label>' +
    '<input class="lf-avoid" type="text" placeholder="e.g. ORD" value="' + ((p.avoid_stations || []).join(', ')) + '">' +
    '<div class="lf-live-count" style="margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--bg);font-size:13px;font-weight:600;color:var(--label);"></div>' +
    '<div style="display:flex;gap:8px;margin-top:14px;">' +
      (existing ? '<button type="button" class="lf-delete" style="margin:0;flex:1;background:var(--red);">Delete</button>' : '') +
      '<button type="button" class="lf-save" style="margin:0;flex:2;">Save</button>' +
    '</div>' +
    '<div class="lf-msg" style="margin-top:8px;font-size:12.5px;color:var(--label);"></div>';
  body.appendChild(panel);

  let getScope;
  if(existing){
    getScope = () => ({opr: existing.opr, base: existing.base, fleet: existing.fleet});
  } else {
    const oprSel = panel.querySelector('.lf-opr');
    const baseSel = panel.querySelector('.lf-base');
    const fleetSel = panel.querySelector('.lf-fleet');
    const r = await fetch('/pbs/packs');
    const packs = await r.json();
    // (ALL) is always the first option at every level — picking it drops
    // that dimension from the scope entirely (server-side: ALL_SCOPE),
    // so a layer can span every base for one operator, or genuinely
    // every pack there is, instead of being pinned to one exact pack.
    const fillSelect = (sel, values) => {
      sel.innerHTML = '<option value="ALL">(ALL)</option>' + values.map(v => '<option value="' + v + '">' + v + '</option>').join('');
    };
    const oprs = [...new Set(packs.map(pk => pk.opr))].sort();
    fillSelect(oprSel, oprs);
    const refreshBases = () => {
      const scoped = oprSel.value === 'ALL' ? packs : packs.filter(pk => pk.opr === oprSel.value);
      const bases = [...new Set(scoped.map(pk => pk.base))].sort();
      fillSelect(baseSel, bases);
      refreshFleets();
    };
    const refreshFleets = () => {
      let scoped = oprSel.value === 'ALL' ? packs : packs.filter(pk => pk.opr === oprSel.value);
      scoped = baseSel.value === 'ALL' ? scoped : scoped.filter(pk => pk.base === baseSel.value);
      const fleets = [...new Set(scoped.map(pk => pk.fleet))].sort();
      fillSelect(fleetSel, fleets);
      _queueLayerPreview(panel, getScope);
    };
    oprSel.onchange = refreshBases;
    baseSel.onchange = refreshFleets;
    getScope = () => ({opr: oprSel.value, base: baseSel.value, fleet: fleetSel.value});
    refreshBases();
  }

  // Live count while editing — every property input re-runs a debounced
  // /pbs/layers/preview fetch so the match count updates as you type,
  // before anything is saved. Name doesn't affect matches, so it's the
  // one field left out.
  panel.querySelectorAll('input, select').forEach(el => {
    if(el.classList.contains('lf-name')) return;
    el.addEventListener('input', () => _queueLayerPreview(panel, getScope));
    el.addEventListener('change', () => _queueLayerPreview(panel, getScope));
  });
  _queueLayerPreview(panel, getScope);

  const msgEl = panel.querySelector('.lf-msg');
  panel.querySelector('.lf-save').onclick = () => layerSaveForm(panel, existing, msgEl);
  if(existing){
    panel.querySelector('.lf-delete').onclick = () => layerDeleteLayer(existing.id);
  }
}
function _numOrNull(v){ return v === '' || v === null || v === undefined ? null : Number(v); }
function _stationList(v){ return (v || '').split(',').map(s => s.trim().toUpperCase()).filter(Boolean); }
// Old saved layers only ever have the boolean exclude_red_eye — reads
// the new tri-state key first, falls back to that boolean otherwise.
function _redEyeMode(p){
  if(p.red_eye) return p.red_eye;
  return p.exclude_red_eye ? 'exclude' : 'any';
}
function _gatherLayerProperties(panel){
  return {
    min_days: _numOrNull(panel.querySelector('.lf-min-days').value),
    max_days: _numOrNull(panel.querySelector('.lf-max-days').value),
    min_block: _numOrNull(panel.querySelector('.lf-min-block').value),
    min_tafb: _numOrNull(panel.querySelector('.lf-min-tafb').value),
    max_legs_per_day: _numOrNull(panel.querySelector('.lf-max-legs').value),
    red_eye: panel.querySelector('.lf-redeye').value,
    layover_include: _stationList(panel.querySelector('.lf-layover-include').value),
    include_stations: _stationList(panel.querySelector('.lf-include').value),
    avoid_stations: _stationList(panel.querySelector('.lf-avoid').value),
  };
}
let _layerPreviewTimer = null;
let _layerPreviewSeq = 0;
function _queueLayerPreview(panel, getScope){
  const countEl = panel.querySelector('.lf-live-count');
  clearTimeout(_layerPreviewTimer);
  countEl.textContent = 'Checking…';
  _layerPreviewTimer = setTimeout(async () => {
    const mySeq = ++_layerPreviewSeq;
    const {opr, base, fleet} = getScope();
    if(!opr || !base || !fleet){ countEl.textContent = ''; return; }
    const props = _gatherLayerProperties(panel);
    const params = new URLSearchParams({
      opr, base, fleet,
      min_days: props.min_days ?? '', max_days: props.max_days ?? '',
      min_block: props.min_block ?? '', min_tafb: props.min_tafb ?? '',
      max_legs_per_day: props.max_legs_per_day ?? '',
      red_eye: props.red_eye,
      layover_include: props.layover_include.join(','),
      include_stations: props.include_stations.join(','),
      avoid_stations: props.avoid_stations.join(','),
    });
    try {
      const r = await fetch('/pbs/layers/preview?' + params.toString());
      const data = await r.json();
      if(mySeq !== _layerPreviewSeq) return; // a newer edit already superseded this request
      countEl.textContent = r.ok ? (data.count + ' pairing' + (data.count === 1 ? '' : 's') + ' match right now') : (data.error || 'Could not check');
    } catch(e) {
      if(mySeq === _layerPreviewSeq) countEl.textContent = '';
    }
  }, 400);
}
async function layerSaveForm(panel, existing, msgEl){
  const name = panel.querySelector('.lf-name').value.trim();
  if(!name){ msgEl.textContent = 'Name is required.'; msgEl.style.color = 'var(--red)'; return; }
  const properties = _gatherLayerProperties(panel);
  try {
    let r;
    if(existing){
      r = await fetch('/pbs/layers/' + existing.id, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, properties}),
      });
    } else {
      const opr = panel.querySelector('.lf-opr').value;
      const base = panel.querySelector('.lf-base').value;
      const fleet = panel.querySelector('.lf-fleet').value;
      if(!opr || !base || !fleet){ msgEl.textContent = 'Pick an operator/base/fleet.'; msgEl.style.color = 'var(--red)'; return; }
      r = await fetch('/pbs/layers', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, opr, base, fleet, properties}),
      });
    }
    const data = await r.json();
    if(!r.ok){ msgEl.textContent = data.error || 'Save failed'; msgEl.style.color = 'var(--red)'; return; }
    showToast('Layer saved');
    layerShowList();
  } catch(e) { msgEl.textContent = 'Request failed: ' + e; msgEl.style.color = 'var(--red)'; }
}
async function layerDeleteLayer(id){
  if(!confirm('Delete this layer?')) return;
  try {
    const r = await fetch('/pbs/layers/' + id, {method: 'DELETE'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Delete failed'); return; }
    showToast('Layer deleted');
    layerShowList();
  } catch(e) { showToast('Request failed: ' + e); }
}
const _LAYER_SORT_OPTIONS = [
  ['', 'Default'], ['-block', 'Block (high→low)'], ['block', 'Block (low→high)'],
  ['-tafb', 'TAFB (high→low)'], ['tafb', 'TAFB (low→high)'],
  ['-days', 'Days (high→low)'], ['days', 'Days (low→high)'],
  ['seq', 'SEQ'],
];
async function layerShowPairings(layer, sort){
  sort = sort || '';
  const body = document.getElementById('layers-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/layers/' + layer.id + '/pairings' + (sort ? ('?sort=' + encodeURIComponent(sort)) : ''));
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryBackLink('Back to Bid Layers', layerShowList));
    const editBtn = document.createElement('button');
    editBtn.className = 'lib-bar-action';
    editBtn.textContent = 'Edit';
    editBtn.onclick = (e) => { e.stopPropagation(); layerShowForm(layer); };
    const {pane, list} = libraryPane(layer.name + ' (' + data.pairings.length + ')', layerShowList, editBtn);
    if(data.pairings.length){
      const sortWrap = document.createElement('div');
      sortWrap.style.cssText = 'padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;';
      const sortLabel = document.createElement('span');
      sortLabel.textContent = 'Sort'; sortLabel.style.cssText = 'font-size:12.5px;color:var(--label);';
      const sortSel = document.createElement('select');
      sortSel.style.cssText = 'flex:1;';
      sortSel.innerHTML = _LAYER_SORT_OPTIONS.map(([v, label]) => '<option value="' + v + '"' + (v === sort ? ' selected' : '') + '>' + label + '</option>').join('');
      sortSel.onchange = () => layerShowPairings(layer, sortSel.value);
      sortWrap.appendChild(sortLabel); sortWrap.appendChild(sortSel);
      list.parentNode.insertBefore(sortWrap, list);
    } else {
      list.innerHTML = '<p class="placeholder-note" style="padding:14px;">Nothing in this pack matches this layer’s filters.</p>';
    }
    data.pairings.forEach(s => list.appendChild(sequenceListRow(s, () => layerShowSequenceDetail(layer, s))));
    body.appendChild(pane);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
// Structurally parallel to libraryShowSequenceDetail — same detail route,
// same Fly/Save buttons, same duty-day cards — just targeting layers-body
// and backing out to this layer's own matches list instead of the
// Library's own Sequences list. Takes the matched pairing's OWN opr/base/
// fleet (not the layer's) — a layer scoped to (ALL) spans several packs,
// so each match carries its real pack identity, tagged server-side.
async function layerShowSequenceDetail(layer, match){
  const {opr, base, fleet} = match;
  const seqNumber = match.seq;
  const body = document.getElementById('layers-body');
  body.innerHTML = '<p class="placeholder-note">Loading…</p>';
  try {
    const r = await fetch('/pbs/packs/' + opr + '/' + base + '/' + fleet + '/sequences/' + encodeURIComponent(seqNumber));
    const seqData = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (seqData.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryBackLink('Back to ' + layer.name, () => layerShowPairings(layer)));

    const totalEl = document.createElement('div');
    totalEl.className = 'lib-total';
    totalEl.textContent = [opr, (seqData.positions || []).join('/')].filter(Boolean).join(' · ');
    body.appendChild(totalEl);

    const flyRow = document.createElement('div');
    flyRow.className = 'fly-row';
    const flyBtn = document.createElement('button');
    flyBtn.className = 'fly-btn';
    flyBtn.textContent = 'Fly This Pairing';
    flyBtn.onclick = () => promoteAndFly(opr, base, fleet, seqNumber);
    const saveBtn = document.createElement('button');
    saveBtn.className = 'save-btn';
    saveBtn.title = 'Save this pairing';
    saveBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>';
    if(isPairingSaved(opr, base, fleet, seqNumber)) saveBtn.classList.add('saved');
    saveBtn.onclick = () => toggleSavedPairing(opr, base, fleet, seqNumber, saveBtn);
    flyRow.appendChild(flyBtn);
    flyRow.appendChild(saveBtn);
    body.appendChild(flyRow);

    const cardsWrap = document.createElement('div');
    cardsWrap.style.cssText = 'padding:0 14px 14px;';
    renderDutyDayCards(cardsWrap, seqData, {interactive: false});
    body.appendChild(cardsWrap);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
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

// Editing a leg within a pairing — swap its destination (and optionally
// flight number), recomputed/re-validated server-side. Legs are contiguous,
// so editing one truncates the pairing at that point (see pairing_edit.py);
// an illegal result routes through the same confirm/reject modal the
// date-slip lockout uses, via showConfirmModal().
let _legEditFormOpen = null;
function toggleLegEditForm(row, seq, dutyDay, legIndex, leg){
  if(_legEditFormOpen){ _legEditFormOpen.remove(); _legEditFormOpen = null; }
  const form = document.createElement('div');
  form.className = 'panel';
  form.style.cssText = 'margin:0;border-radius:0;border-left:none;border-right:none;';
  // This form lives inside a duty-day pane whose own onclick navigates away
  // (tap-anywhere-to-generate) — without this, clicking Save/Cancel or even
  // focusing an input bubbles up and fires that navigation at the same time.
  form.onclick = (e) => e.stopPropagation();
  form.innerHTML = `
    <label>New Destination</label>
    <input type="text" class="le-dest" placeholder="${leg.destination || ''}" value="${leg.destination || ''}">
    <label>Flight Number (optional — leave blank to match by route)</label>
    <input type="text" class="le-flt" placeholder="${leg.flight_number || ''}">
    <div style="display:flex;gap:8px;margin-top:10px;">
      <button type="button" class="le-cancel" style="margin:0;flex:1;background:var(--label);">Cancel</button>
      <button type="button" class="le-save" style="margin:0;flex:1;">Save</button>
    </div>
    <div class="le-msg msg"></div>
  `;
  form.querySelector('.le-cancel').onclick = () => { form.remove(); _legEditFormOpen = null; };
  form.querySelector('.le-save').onclick = () => {
    const dest = form.querySelector('.le-dest').value.trim().toUpperCase();
    const flt = form.querySelector('.le-flt').value.trim();
    if(!dest){ form.querySelector('.le-msg').textContent = 'Destination is required.'; return; }
    proposeLegEdit(seq, dutyDay, legIndex, dest, flt, form.querySelector('.le-msg'));
  };
  if(row.tagName === 'TR'){
    // row is a duty-day-card leg row (dutyDayCardHtml) — a bare <div>
    // can't sit between <tr>s, so wrap the form in its own full-width row.
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = row.children.length;
    td.style.padding = '0';
    td.appendChild(form);
    tr.appendChild(td);
    row.insertAdjacentElement('afterend', tr);
    _legEditFormOpen = tr;
  } else {
    row.insertAdjacentElement('afterend', form);
    _legEditFormOpen = form;
  }
}
async function proposeLegEdit(seq, dutyDay, legIndex, newDestination, flightNumber, msgEl){
  msgEl.textContent = 'Checking…';
  msgEl.style.color = '';
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/legs/' + dutyDay + '/' + legIndex + '/propose-edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({new_destination: newDestination, flight_number: flightNumber || undefined}),
    });
    const data = await r.json();
    if(!r.ok){ msgEl.textContent = data.error || 'Edit failed'; msgEl.style.color = 'var(--red)'; return; }
    if(data.legal){
      await resolveLegEdit(seq, 'confirm');
    } else {
      showConfirmModal(
        'This edit breaks a legality rule',
        data.violations.join('; ') + ' — apply the edit anyway, or reject it?',
        () => resolveLegEdit(seq, 'confirm'),
        () => resolveLegEdit(seq, 'reject'),
      );
    }
  } catch(e) { msgEl.textContent = 'Request failed: ' + e; msgEl.style.color = 'var(--red)'; }
}
async function resolveLegEdit(seq, action){
  try {
    await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/legs/resolve-edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
  } catch(e) { /* best-effort */ }
  if(_legEditFormOpen){ _legEditFormOpen.remove(); _legEditFormOpen = null; }
  showToast(action === 'confirm' ? 'Leg updated' : 'Edit discarded');
  initPairingView();
}

function showRecoveryTab(tab){
  document.getElementById('tab-shift').classList.toggle('active', tab==='shift');
  document.getElementById('tab-divert').classList.toggle('active', tab==='divert');
  document.getElementById('tab-shift-btn').classList.toggle('active', tab==='shift');
  document.getElementById('tab-divert-btn').classList.toggle('active', tab==='divert');
}

// "Timed out into rest" — same destination, just later. Tries the exact
// original plan shifted first (mode:"shifted", a single deterministic
// preview to confirm); falls back to a day-scoped alternate-routing search
// (mode:"day_patch", pick one of several legal options) if the shifted
// plan doesn't fit.
let _shiftRestStart = null;
async function submitShiftDay(){
  const el = document.getElementById('shift-msg');
  const list = document.getElementById('shift-candidates');
  list.innerHTML = '';
  const restStart = document.getElementById('shift-rest-start').value.trim();
  if(!restStart){ el.textContent = 'Rest start time is required.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Checking the original plan, shifted…';
  el.style.color = '';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/shift-day', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rest_start_local: restStart}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'No way forward found'; el.style.color = 'var(--red)'; return; }
    _shiftRestStart = restStart;
    if(data.mode === 'shifted'){
      el.textContent = 'The original plan still works, just a day later.';
      el.style.color = 'var(--green)';
      const days = data.preview.duty_days.map(d => `Day ${d.duty_day}: ` +
        d.legs.map(l => `${l.flight_number} ${l.origin}→${l.destination} ${l.dep_local}/${l.arr_local}`).join(', ')
      ).join('<br>');
      list.innerHTML = `<div class="arow" style="flex-direction:column;align-items:stretch;">
        <div style="font-size:12.5px;color:var(--value);line-height:1.6;">${days}</div>
        <button onclick="acceptShiftedPlan()" style="margin-top:8px;">Accept Shifted Plan</button>
      </div>`;
    } else if(data.mode === 'day_patch'){
      window._shiftPatch = {target_station: data.target_station, reached_target: data.reached_target};
      el.textContent = data.reached_target
        ? `Original plan didn't fit — found ${data.candidates.length} way(s) to still reach ${data.target_station} on time.`
        : `Couldn't reach ${data.target_station} — found ${data.candidates.length} legal way(s) back to base instead.`;
      el.style.color = data.reached_target ? 'var(--green)' : '#c98a1f';
      list.innerHTML = data.candidates.map((c, i) => `<div class="arow" style="flex-direction:column;align-items:stretch;">
        <div style="display:flex;justify-content:space-between;font-size:13.5px;color:var(--value);">
          <span>${c.routing.join('-')}</span><span>${c.block.toFixed(2)}h block</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--label);margin-top:2px;">
          <span>${c.legs_per_day.join('-')} legs/day</span><span>${c.total_days} total duty period(s)</span>
        </div>
        <button onclick="acceptDayPatch(${i})" style="margin-top:8px;">Accept</button>
      </div>`).join('');
      window._shiftCandidates = data.candidates;
    }
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}
async function acceptShiftedPlan(){
  const el = document.getElementById('shift-msg');
  el.textContent = 'Applying…';
  el.style.color = '';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/shift-day/accept-shift', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rest_start_local: _shiftRestStart}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Failed to apply'; el.style.color = 'var(--red)'; return; }
    document.getElementById('shift-candidates').innerHTML = '';
    showToast('Pairing shifted to the next day');
    showView('pairing');
  } catch(e) { el.textContent = 'Request failed: ' + e; el.style.color = 'var(--red)'; }
}
async function acceptDayPatch(i){
  const el = document.getElementById('shift-msg');
  const c = (window._shiftCandidates || [])[i];
  const ctx = window._shiftPatch;
  if(!c || !ctx || !_shiftRestStart){ el.textContent = 'That option is no longer available — search again.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Applying…';
  el.style.color = '';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/shift-day/accept-patch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        rest_start_local: _shiftRestStart, target_station: ctx.target_station, reached_target: ctx.reached_target,
        chain: c.chain, day_number: c.day_number, dlegs_today: c.dlegs_today,
        dblk_today: c.dblk_today, duty_report_utc: c.duty_report_utc, total_days: c.total_days,
      }),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Failed to apply'; el.style.color = 'var(--red)'; return; }
    document.getElementById('shift-candidates').innerHTML = '';
    showToast('Pairing patched');
    showView('pairing');
  } catch(e) { el.textContent = 'Request failed: ' + e; el.style.color = 'var(--red)'; }
}

// Mid-trip recovery — the currently-viewed leg diverted/overran. Every
// candidate /fos/<id>/recover returns is already legal by construction
// (pairing_edit.recover_from_disruption only returns verify_from()-clean
// continuations), so — unlike leg editing — there's no illegal-but-
// confirmable state and no confirm/reject modal needed here, just pick one.
let _recoveryContext = null;
async function submitRecovery(){
  const el = document.getElementById('recovery-msg');
  const list = document.getElementById('recovery-candidates');
  list.innerHTML = '';
  const dest = document.getElementById('rec-dest').value.trim().toUpperCase();
  const arrival = document.getElementById('rec-arrival').value.trim();
  if(!dest || !arrival){ el.textContent = 'Actual destination and arrival time are both required.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Searching for a legal way back…';
  el.style.color = '';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/recover', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({actual_destination: dest, actual_arrival_local: arrival}),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'No recovery found'; el.style.color = 'var(--red)'; return; }
    _recoveryContext = {actual_destination: dest, actual_arrival_local: arrival};
    el.textContent = `Found ${data.candidates.length} legal option(s).`;
    el.style.color = 'var(--green)';
    list.innerHTML = data.candidates.map((c, i) => `<div class="arow" style="flex-direction:column;align-items:stretch;">
      <div style="display:flex;justify-content:space-between;font-size:13.5px;color:var(--value);">
        <span>${c.routing.join('-')}</span><span>${c.block.toFixed(2)}h block</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--label);margin-top:2px;">
        <span>${c.legs_per_day.join('-')} legs/day</span><span>${c.total_days} total duty period(s)</span>
      </div>
      <button onclick="acceptRecovery(${i})" style="margin-top:8px;">Accept &amp; Recover</button>
    </div>`).join('');
    window._recoveryCandidates = data.candidates;
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}
async function acceptRecovery(i){
  const el = document.getElementById('recovery-msg');
  const c = (window._recoveryCandidates || [])[i];
  if(!c || !_recoveryContext){ el.textContent = 'That option is no longer available — search again.'; el.style.color = 'var(--red)'; return; }
  el.textContent = 'Applying recovery…';
  el.style.color = '';
  try {
    const r = await fetch('/fos/' + LEG_ID + '/recover/accept', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        actual_destination: _recoveryContext.actual_destination,
        actual_arrival_local: _recoveryContext.actual_arrival_local,
        chain: c.chain, day_number: c.day_number, dlegs_today: c.dlegs_today,
        dblk_today: c.dblk_today, duty_report_utc: c.duty_report_utc, total_days: c.total_days,
      }),
    });
    const data = await r.json();
    if(!r.ok){ el.textContent = data.error || 'Recovery failed'; el.style.color = 'var(--red)'; return; }
    document.getElementById('recovery-candidates').innerHTML = '';
    showToast('Recovered — pairing updated');
    showView('pairing');
  } catch(e) {
    el.textContent = 'Request failed: ' + e;
    el.style.color = 'var(--red)';
  }
}

const _DDMMMYY_MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
function todayZuluISO(){
  return new Date().toISOString().slice(0, 10);
}
// "Today" in a specific IANA zone, not bare UTC — a late-evening domestic
// report time (e.g. 22:40 at PHX, UTC-7) is already the next zulu
// calendar day, so defaulting the generate form's date to todayZuluISO()
// silently sent SimBrief a date a day ahead of the pairing's real local
// day. See prefillSimbriefGen() for where this replaces that default.
function localDateISO(tzName){
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tzName, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date()).reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
  return parts.year && parts.month && parts.day ? `${parts.year}-${parts.month}-${parts.day}` : todayZuluISO();
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
    // LEG_BOOKMARKED_DOCS is a page-load snapshot initSavedDocs() reads
    // from — without syncing it here, Saved Docs kept showing "No saved
    // documents yet" after bookmarking, since the array itself never
    // changed even though the icon/count on this page did. const only
    // blocks reassignment, not mutation, so update it in place.
    LEG_BOOKMARKED_DOCS.splice(0, LEG_BOOKMARKED_DOCS.length, ...bookmarked);
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
function initSavedDocs(){
  const body = document.getElementById('saveddocs-body');
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
  showView('allcommands');
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
    // Whole pairing shares one carrier — prefer its IATA code, falling
    // back to ICAO when the bid pack's operator code isn't a real IATA
    // 2-letter (rare, but _IATA_TO_ICAO passes an unmapped code through
    // unchanged server-side).
    const flightPrefix = seqData.operator_iata || seqData.operator || '';
    for(const day of (seqData.duty_days || [])){
      const legs = day.legs || [];
      // ICAO-normalized match — see prefillSimbriefGen()'s identical fix
      // for why a raw origin/destination string compare silently orphans
      // a leg from its own pairing's pill strip once it's carried a
      // SimBrief-overwritten ICAO code.
      const idx = legs.findIndex(l => l.flight_number === LEG_FLIGHT_NUMBER && (l.origin_icao || l.origin) === LEG_ORIGIN_ICAO && (l.destination_icao || l.destination) === LEG_DESTINATION_ICAO);
      if(idx === -1) continue;
      legs.forEach((l, i) => {
        const pill = document.createElement('button');
        pill.className = 'leg-pill' + (i === idx ? ' selected' : '');
        pill.textContent = (l.flight_number ? (flightPrefix ? flightPrefix + ' ' : '') + l.flight_number : '—');
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
  if(!LEG_ID){
    // /schedule — the leg-independent root. Nothing here is scoped to a
    // real leg, so there's no Overview to default to and no per-leg sync
    // to start. Still honor an explicit ?view= (e.g. the global settings
    // gear links here as /schedule?view=settings) — only default to
    // Schedule itself when nothing was asked for.
    showView(view || 'pairing');
    return;
  }
  if(view === 'pairing' || view === 'release' || view === 'confirm' || view === 'settings') showView(view);
  else initOverviewPills();
  showDateSlipModalIfPending();
  startAutoSync();
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
