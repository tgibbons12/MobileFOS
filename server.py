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
import copy
import csv
import html
import io
import json
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

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
from models import (
    db, User, Leg, PbsImport, PairingPack, SignatureLog, ReleaseCache, TripCheckIn,
    Document, DocumentAck, Message,
)
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


_bundled_iata_to_icao = None


def _bundled_iata_to_icao_airports():
    """IATA->ICAO from the airportsdata package already bundled for
    _AIRPORT_TZ. A fallback under the OurAirports CSV above, not a
    replacement: OurAirports is the richer dataset and stays first.

    This exists because the CSV is a runtime download with no copy in the
    repo — one failed fetch on a container with no cache yet leaves the map
    empty, and _airport_icao() then passes every 3-letter station straight
    through. That silently turns every PBS-vs-SimBrief comparison keyed on
    it ("PHX" vs "KPHX") into a non-match, which is one of the ways a
    re-imported leg came back detached from its pairing."""
    global _bundled_iata_to_icao
    if _bundled_iata_to_icao is None:
        _bundled_iata_to_icao = {
            (rec.get("iata") or "").strip().upper(): icao
            for icao, rec in _AIRPORT_TZ.items()
            if (rec.get("iata") or "").strip()
        }
    return _bundled_iata_to_icao


def _airport_icao(code):
    """Best-effort IATA->ICAO for a station code; passes through anything
    already 4 letters or not found in either map (some PBS stations are
    already ICAO, e.g. Canadian CYxx fields)."""
    code = (code or "").strip().upper()
    if not code or len(code) == 4:
        return code
    found = _load_iata_to_icao_airports().get(code)
    return found or _bundled_iata_to_icao_airports().get(code, code)


# PBS's equipment codes are keyed by exact sub-fleet code, not a shared
# prefix — a prefix scheme can't work here since it collides on real codes
# (21A/21C/21D/21S are A321ceo but 21Q is A321neo; 73G is a 737-700 but
# 73S/73W are 737-800; 39E is a 737-900 but 39N is an A330; E70 is an E170
# but E7L/E7S/E7W are E175s; E90 is an E190 but E95 is an E195; 738K/738R
# are 737-800s but 738M is a MAX 8; H319 is an A319 but H205 is an A320,
# so even the digits in a code aren't reliable). Table below is straight
# off the operator's own OPERATOR/FLEET sub-fleet reference (equipment
# code -> aircraft), not guessed. Extend as more codes get confirmed;
# anything unmapped (including already-ICAO codes like "B738" or "A320"
# from a SimBrief-loaded leg) passes through unchanged — which is why a
# genuinely unknown code is left OUT of this table rather than given a
# best guess: an unmapped code fails visibly, a wrong one silently feeds
# SimBrief the wrong airframe and returns plausible but wrong performance.
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
    # --- 4-character sub-fleet codes (Smart Routes "AC CODE" column) ---
    # Same reference, newer scheme — most of the network moved to 4-char
    # codes while the 3-char ones above stayed in use, so both forms have
    # to resolve.
    "319S": "A319", "319W": "A319", "H319": "A319",
    # F320 (CFM sharklet), 320S (IAE sharklet) and H205 are all A320ceo —
    # engine/winglet sub-variants, which the ICAO type code doesn't
    # distinguish. Worth knowing if SimBrief airframe selection ever needs
    # to tell a CFM56 A320 from a V2500 one: that detail is lost here.
    "320S": "A320", "F320": "A320", "H205": "A320",
    "320N": "A20N",
    "321F": "A321", "321K": "A321", "321R": "A321", "321T": "A321",
    "F321": "A321",
    "321E": "A21N", "321L": "A21N", "321N": "A21N", "321X": "A21N",
    "330N": "A339",
    "738K": "B738", "738R": "B738",
    "738M": "B38M",
    "E4X": "E45X",
    "EMJ": "E145", "ERD": "E145",
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
    if "is_admin" not in existing:
        # No DB-level default on purpose — "BOOLEAN DEFAULT 0" is rejected
        # by Postgres and "DEFAULT FALSE" by older SQLite, so a bare
        # nullable column is the one form that migrates cleanly on both.
        # Existing rows land as NULL, which bool() reads as not-an-admin.
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN"))
        LOG.info("Migrated: added users.is_admin")
    if "last_seen" not in existing:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN last_seen TIMESTAMP"))
        LOG.info("Migrated: added users.last_seen")
    # Message.body started life as String(200) and was widened to Text when
    # a reassignment began carrying a whole repaired pairing. The model
    # change alone is invisible to an existing database: SQLite ignores the
    # length entirely (so local development never noticed), while Postgres
    # enforces it and rejects the insert — "value too long for type
    # character varying(200)" — which surfaced as a 500 on recover-accept.
    # SQLite is skipped rather than migrated: it does not enforce a VARCHAR
    # length in the first place (nothing to fix), and it has no ALTER COLUMN
    # TYPE, so attempting it would only fail.
    if db.engine.dialect.name != "sqlite":
        for col in inspector.get_columns("messages"):
            if col["name"] != "body":
                continue
            if getattr(col["type"], "length", None) is not None:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(sa_text("ALTER TABLE messages ALTER COLUMN body TYPE TEXT"))
                    LOG.info("Migrated: widened messages.body to TEXT")
                except Exception as e:
                    # Never let a migration stop the app from booting — the
                    # symptom without it is one failing message insert, not
                    # a dead deployment.
                    LOG.error(f"Could not widen messages.body to TEXT: {e}")
            break

    existing_pbs = {c["name"] for c in inspector.get_columns("pbs_imports")}
    if "pending_edits" not in existing_pbs:
        with db.engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE pbs_imports ADD COLUMN pending_edits JSON"))
        LOG.info("Migrated: added pbs_imports.pending_edits")


def _sync_admin_usernames():
    """Promote the accounts named in ADMIN_USERNAMES ("a" or "a,b,c") on
    boot. This exists because the first admin can't be created from inside
    the app — nothing can grant the right before one holder exists — and
    the alternative bootstrap (running make_admin.py against production)
    means getting the production DATABASE_URL onto a laptop, which is both
    fiddly and easy to get silently wrong: run it without that variable
    and it cheerfully grants admin on the local SQLite file instead, with
    no visible difference. Setting a variable in the Railway dashboard has
    no such failure mode.

    Only ever GRANTS. Removing a name here does NOT revoke it — a
    dropped/renamed variable shouldn't quietly strip publishing rights,
    and revoking is a deliberate act (make_admin.py --revoke). A name
    that doesn't match an account is logged rather than treated as an
    error, since the variable may well be set before the account exists."""
    names = [n.strip() for n in (os.environ.get("ADMIN_USERNAMES") or "").split(",") if n.strip()]
    if not names:
        return
    changed = False
    for name in names:
        user = User.query.filter_by(username=name).first()
        if user is None:
            LOG.warning(f"ADMIN_USERNAMES lists {name!r}, but no such account exists yet")
        elif not user.is_admin:
            user.is_admin = True
            changed = True
            LOG.info(f"Granted admin to {name!r} via ADMIN_USERNAMES")
    if changed:
        db.session.commit()


with app.app_context():
    db.create_all()
    _ensure_columns()
    _sync_admin_usernames()


# ---------------------------------------------------------------------------
# Auth — this is a personal/crew tool, not a public site, so the whole app
# sits behind a login rather than decorating each of the ~18 routes below.
# ---------------------------------------------------------------------------
_PUBLIC_ENDPOINTS = {"login", "register", "health", "static"}


# How stale users.last_seen is allowed to get before a request rewrites it.
# The admin roster shows "last active" to the nearest few minutes at best, so
# writing on every single request would be a per-page-load DB write to buy
# precision nothing displays.
_LAST_SEEN_THROTTLE = timedelta(minutes=5)


def _touch_last_seen():
    last = current_user.last_seen
    now = datetime.now(timezone.utc)
    if last is not None:
        # SQLite hands back naive datetimes where Postgres gives aware ones;
        # compare on common ground rather than raising on the subtraction.
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last < _LAST_SEEN_THROTTLE:
            return
    current_user.last_seen = now
    db.session.commit()


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if current_user.is_authenticated:
        try:
            _touch_last_seen()
        except Exception as e:
            # Activity tracking is never worth failing a real request over.
            db.session.rollback()
            LOG.warning(f"last_seen update failed: {e}")
        return None
    # A redirect would hand back an HTML login page to a fetch() call that
    # expects JSON — only the full-page GETs (opening a URL directly) should
    # redirect; POST/etc (all the in-app API calls) get a plain 401 instead.
    if request.method == "GET":
        return redirect(url_for("login", next=request.path))
    return jsonify({"error": "login required"}), 401


AUTH_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<script>(function(){var t=localStorage.getItem('fos_theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);})();</script>
<!-- No viewport-fit=cover. That is what makes the layout viewport span
     the WHOLE screen, status bar included, and it is the actual reason
     content sat under the frosted bar — the status-bar style only
     decides how iOS paints over whatever is up there. Without it the
     web view is laid out inside the safe area, so nothing is beneath
     the bar to blur. It also means env(safe-area-inset-*) reports 0,
     which every use here already handles by adding it to something. -->
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<!-- NOT black-translucent. That asks iOS to draw its status bar OVER the
     page, which is what put a frosted band across the top of every
     screen. "default" makes iOS reserve the space instead, so the app
     starts below the bar and nothing is drawn on top of it. Paired with
     --status-bar-min below, which drops to 0 because there is no longer
     an overlay to sit clear of. -->
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<script>
// How much of the top of the window iOS is covering, which is NOT the same
// as env(safe-area-inset-top).
//
// An iPad has no notch, so that inset is 0 — including in landscape, and
// including when the app is installed to the home screen. But
// apple-mobile-web-app-status-bar-style=black-translucent means iOS still
// draws its translucent status bar OVER the page there. Trusting env()
// alone is why the acknowledgement banner kept ending up underneath it: the
// padding computed to 11px against a ~24pt overlay.
//
// So: take the inset when there is one (a notched phone reports 44-59), and
// otherwise reserve a status bar's worth, but only in standalone — in a
// normal browser tab nothing is overlaid and reserving space would just
// waste it. Every top offset in the stylesheet reads this one value.
(function(){
  var standalone = window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  var root = document.documentElement;
  // Reserved again, and this time not on a theory about which meta tag
  // controls the overlay. Observed behaviour on iOS 26 is that a
  // home-screen web app's content sits under the status bar regardless of
  // apple-mobile-web-app-status-bar-style or viewport-fit, so the only
  // reliable fix is for the page to keep its own content out of that band.
  // .topbar's padding absorbs it, which leaves the strip painted in
  // .topbar's own flat background — the solid colour-matched bar other PWAs
  // show, rather than the system blurring our content through it.
  root.style.setProperty('--status-bar-min', standalone ? '24px' : '0px');
  root.style.setProperty('--status-bar',
    'max(env(safe-area-inset-top), var(--status-bar-min, 0px))');
})();
</script>
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<!-- ONE tag, no media attribute, and it is never removed or replaced —
     only its content is rewritten. iOS samples theme-color when a
     home-screen app launches; a tag that script deletes and recreates
     can leave it with no colour at all, at which point it falls back to
     its own material and the status bar reads as frosted rather than a
     flat colour matching the app. -->
<meta name="theme-color" id="theme-color-tag" content="#f5f5f7">
<script>
// theme-color has to follow the theme the app is ACTUALLY showing. The two
// media-keyed tags above only track the OS, and this app has its own
// Light/Auto/Dark override — so with the OS light and the app set to dark,
// iOS painted the status strip #f5f5f7 over a black page, and vice versa.
// Resolving it here to a single tag removes that whole class of mismatch.
function _syncThemeColor(){
  var t = localStorage.getItem('fos_theme');
  var dark = (t === 'dark') || (t !== 'light' &&
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var m = document.getElementById('theme-color-tag');
  if (m) m.setAttribute('content', dark ? '#000000' : '#f5f5f7');
}
_syncThemeColor();
if (window.matchMedia) {
  try { window.matchMedia('(prefers-color-scheme: dark)')
          .addEventListener('change', _syncThemeColor); } catch (e) {}
}
</script>

<title>$title – MobileCCI</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --blue:#0071e3; --blue-dark:#0058a8; --red:#ff3b30; --inactive:#9aa1ab;
    /* iOS was falling all the way through to the generic monospace,
       which is Courier: ui-monospace is not honoured everywhere and a
       bare "Menlo" does not match on iOS. SFMono-Regular is the name
       Safari actually resolves, so it goes first among the real
       families and Courier stays what it should be — the last
       resort. */
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
            "Liberation Mono", "Courier New", monospace;
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
    # The AeroAPI lookup that produced dep_gate/arr_gate, kept whole so
    # re-opening or regenerating a leg can repaint the panel from what is
    # already on file instead of spending another (paid) API call.
    "aero_suggestion": None,
    "fleet_type": "", "equipment_type": "", "tail_number": "", "tail_routing": "",
    "aircraft_name": "", "fin": "", "engines": "", "selcal": "", "seat_capacity": "",
    "oew": "", "max_zfw": "", "max_tow_struct": "", "max_ldw": "",
    "bookmarked_docs": [],
    "status": "", "customer_load": "", "position": "", "crew": [],
    "flight_time": "", "odl_time": "", "duty_time": "", "ground_time": "", "mot": "",
    "tz_diff": "", "hotel_details": "", "limo_details": "",
    "signed_in": False, "fit_for_duty": False,
    "signature": "", "signed_at": "",
    # Per-attestation signing details — FFD and eFlight Plan used to share
    # this same signature/signed_at pair, so signing either one silently
    # marked BOTH as "signed" and overwrote whichever had signed first.
    # Split so each has its own record of who signed it and when.
    "ffd_signature": "", "ffd_signed_at": "", "ffd_signed_by": "",
    "eflightplan_signature": "", "eflightplan_signed_at": "", "eflightplan_signed_by": "",
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


def _seq_pack_meta(seq):
    """Meta for one sequence — the pool-wide PbsImport.meta, overridden by
    this sequence's own pack stamp when it has one. Same precedence
    generate_from_pbs and get_pbs_sequence already apply (a single pool-wide
    meta goes stale once sequences from more than one pack share the pool)."""
    meta = dict(_pbs_meta())
    if seq.get("_pack_opr"):
        meta["operator"] = seq["_pack_opr"]
        meta["base"] = seq.get("_pack_base") or meta.get("base")
        meta["fleet"] = seq.get("_pack_fleet") or meta.get("fleet")
    return meta


def _pairing_baseline_for_leg(leg):
    """The pairing fields for `leg` — seq first among them — looked up in
    this pilot's own PBS sequences, or None when nothing there matches it
    unambiguously.

    A SimBrief OFP has no `seq` field at all (see build_leg_from_sources):
    it carries day-of-ops detail for one flight, nothing about the trip that
    flight belongs to. So a re-import — a leg generated from a sequence in
    the app, edited on SimBrief, then loaded back in — rebuilds the leg from
    the OFP alone and comes back with no pairing linkage. That is invisible
    while _store_leg's merge happens to land on the same row (the existing
    seq survives), and shows up as a standalone leg the moment it doesn't:
    a date the pilot moved past _dates_match's one-day tolerance is enough.

    This re-derives the linkage from the same place it came from the first
    time — the pilot's own sequences, run through pbs_leg_to_fos_leg — so a
    re-imported leg is attached by the same rule as a freshly generated one
    rather than by a second, drifting mechanism.

    Matching is flight number + ICAO-normalized city pair, deliberately
    ignoring dates (the edit that detached the leg is usually a date change,
    and a bid pack has no calendar dates anyway). Two different sequences
    flying the same flight is genuinely ambiguous — real bid packs repeat a
    city pair across dozens of SEQs — so that resolves to the pilot's active
    sequence if one of them is it, and otherwise to nothing at all. Guessing
    would re-attach the leg to the wrong trip, which is worse than leaving
    it standalone."""
    flight_number = (leg.get("flight_number") or "").strip()
    origin, destination = _airport_icao(leg.get("origin", "")), _airport_icao(leg.get("destination", ""))
    if not flight_number or not origin or not destination:
        return None

    matches = []  # (sequence, duty day, pbs leg)
    for seq in _pbs_sequences():
        for day in seq.get("duty_days") or []:
            for pbs_leg in day.get("legs") or []:
                if (
                    (pbs_leg.get("flight_number") or "").strip() == flight_number
                    and _airport_icao(pbs_leg.get("origin", "")) == origin
                    and _airport_icao(pbs_leg.get("destination", "")) == destination
                ):
                    matches.append((seq, day, pbs_leg))
    if not matches:
        return None
    if len({s["seq"] for s, _, _ in matches}) > 1:
        matches = [m for m in matches if m[0]["seq"] == current_user.active_seq]
        if not matches:
            return None
    seq, day, pbs_leg = matches[0]  # a trip that flies the same leg twice is
    # still that one trip — the seq is right either way, and the first match
    # is as good a source as any for the day-level hotel/duty fields.

    # Position isn't in the OFP or the bid pack: it's the pilot's own. Reuse
    # whatever they were already flying this sequence as, falling back to the
    # sequence's first published position.
    flown_as = ""
    for row in Leg.query.filter_by(user_id=current_user.id).all():
        if row.data.get("seq") == seq["seq"] and row.data.get("position"):
            flown_as = row.data["position"]
            break
    position = flown_as or (seq["positions"][0] if seq.get("positions") else "")
    return pbs_parser.pbs_leg_to_fos_leg(_seq_pack_meta(seq), seq, day, pbs_leg, position)


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
    from_simbrief = bool(payload.get("simbrief_user"))
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
                "aero_suggestion",
                "bookmarked_docs", "signed_in", "fit_for_duty",
                "pairing_sched_out", "pairing_sched_in",
            ):
                if src_row.data.get(key) and not leg.get(key):
                    leg[key] = src_row.data[key]
    # Still no pairing linkage after all that: either this came in through
    # "Import from SimBrief" on Home (which sends no carry_gates_from at all),
    # or carry_from's same-flight guard above rightly declined. Look the leg
    # up in the pilot's own sequences instead — a re-imported leg belongs to
    # its trip just as much as a freshly generated one does, and the OFP it
    # was rebuilt from has no field that could say so.
    if from_simbrief and not leg.get("seq"):
        pairing = _pairing_baseline_for_leg(leg)
        if pairing:
            # Only the fields a pairing owns and a SimBrief OFP has no
            # equivalent for — the same set carry_gates_from carries, minus
            # the ones that aren't the pairing's to give (gates, bookmarks,
            # signatures belong to the leg, not the trip). Everything both
            # sources describe (times, tail, load, fleet) is left to SimBrief:
            # it's the fresher one, and having it is why the pilot re-imported.
            for key in (
                "seq", "position", "base", "airline_iata", "equipment_type",
                "duty_time", "ground_time", "mot", "hotel_details", "limo_details",
                "pairing_sched_out", "pairing_sched_in",
            ):
                if pairing.get(key) and not leg.get(key):
                    leg[key] = pairing[key]
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


def _hhmm_int(v):
    """Plain int from a 4-digit HHMM string ("1145" -> 1145), or None."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _summarize_sequence(s):
    """The compact {seq, days, routing, ...} shape both /pbs/sequences and
    the pack-browsing routes list sequences with."""
    duty_days = s["duty_days"]
    first_day = duty_days[0]
    last_day = duty_days[-1]
    first_leg = first_day["legs"][0] if first_day["legs"] else None
    last_leg = last_day["legs"][-1] if last_day["legs"] else None
    try:
        _block_f = float(s.get("block") or 0)
    except (TypeError, ValueError):
        _block_f = 0.0
    # Real calendar-day span, NOT len(duty_days) — a 24+ hour layover can
    # swallow a whole "dead" day with no duty at all, which never gets its
    # own duty_days entry to count. See pbs_parser.sequence_calendar_days.
    num_days = pbs_parser.sequence_calendar_days(s)
    return {
        "seq": s["seq"], "days": num_days,
        "positions": s["positions"], "ops_per_period": s["ops_per_period"],
        "report": first_day["report"],
        # Last day's release, alongside first day's report above — lets the
        # pairing list show "on duty from X to Y" without opening the detail
        # view for every candidate.
        "release": last_day["release"],
        "origin": first_leg["origin"] if first_leg else None,
        "final_destination": last_leg["destination"] if last_leg else None,
        "routing": _sequence_routing(s),
        "layover_indices": _layover_indices(s),
        # Pairing totals from the bid-pack's own TTL row — cumulative for
        # the whole trip, distinct from any one day's own block/tpay/tafb.
        "block": s.get("block"), "tpay": s.get("tpay"), "tafb": s.get("tafb"),
        # Block hours per day worked — the same "value density" metric
        # pairing_edit.py's recovery-candidate ranking already uses.
        "dacv": round(_block_f / num_days, 3) if num_days else 0.0,
    }


def _decorate_sequence(seq, operator_iata):
    """Full sequence detail with ICAO-decorated legs — the shape
    /pbs/sequences/<seq> and the pack sequence-detail route both return."""
    out = dict(seq)
    out["operator"] = _IATA_TO_ICAO.get(operator_iata, operator_iata)
    out["operator_iata"] = operator_iata
    # Real calendar-day span — see _summarize_sequence's own "days" field
    # and pbs_parser.sequence_calendar_days for why this isn't just
    # len(duty_days) (a 24+ hour layover can hide a whole dead day).
    out["days"] = pbs_parser.sequence_calendar_days(seq)
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


def _num_or_none(v):
    """float(v), or None for a blank/missing/unparseable value — same
    "tolerate anything, never throw" contract as _hhmm_int. properties
    dicts round-trip through JSON and arbitrary API callers, so a
    threshold showing up as a string (or junk) shouldn't 500 every layer
    in the list — just treat it as "no filter" instead of failing closed."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _layer_matches(seq, summary, properties):
    """True if `seq` (raw parsed sequence) satisfies a bid layer's saved
    filter criteria. `summary` is this seq's own _summarize_sequence()
    output, reused so routing/layover_indices aren't recomputed twice."""
    p = properties or {}

    def num(key):
        return _num_or_none(p.get(key))

    days = summary["days"]
    min_days, max_days = num("min_days"), num("max_days")
    if min_days is not None and days < min_days:
        return False
    if max_days is not None and days > max_days:
        return False
    block = float(summary.get("block") or 0)
    min_block, max_block = num("min_block"), num("max_block")
    if min_block is not None and block < min_block:
        return False
    if max_block is not None and block > max_block:
        return False
    tafb = float(summary.get("tafb") or 0)
    min_tafb, max_tafb = num("min_tafb"), num("max_tafb")
    if min_tafb is not None and tafb < min_tafb:
        return False
    if max_tafb is not None and tafb > max_tafb:
        return False
    tpay = float(summary.get("tpay") or 0)
    min_tpay, max_tpay = num("min_tpay"), num("max_tpay")
    if min_tpay is not None and tpay < min_tpay:
        return False
    if max_tpay is not None and tpay > max_tpay:
        return False
    max_legs = num("max_legs_per_day")
    if max_legs is not None and _seq_max_legs_per_day(seq) > max_legs:
        return False
    # Report (first day RPT) / Release (last day RLS) cutoffs — e.g. "5 day
    # trip, released by 1145". Both stored/compared as plain HHMM ints, no
    # real calendar date exists in this data so a straight numeric compare
    # is exactly as meaningful as the rest of this app's HHMM handling.
    report_val = _hhmm_int(summary.get("report"))
    min_report, max_report = num("min_report"), num("max_report")
    if min_report is not None and (report_val is None or report_val < min_report):
        return False
    if max_report is not None and (report_val is None or report_val > max_report):
        return False
    release_val = _hhmm_int(summary.get("release"))
    min_release, max_release = num("min_release"), num("max_release")
    if min_release is not None and (release_val is None or release_val < min_release):
        return False
    if max_release is not None and (release_val is None or release_val > max_release):
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


# One bid layer = an ORDERED STACK of individual criteria, matching how a
# real PBS bid is built: each numbered layer states exactly one thing
# ("Layer 1: 1 day", "Layer 2: min block 7 hours", "Layer 3: include STT",
# "Layer 4: release before 2240"), rather than one flat form holding every
# filter at once. A sequence must satisfy every layer to match; the layer
# NUMBERING is what makes the stack readable and gives _layer_funnel()
# somewhere meaningful to report "your bid got too narrow at Layer 3".
#
# Numeric fields carry an op (min/max/exact); the rest are their own field
# types whose value shape is specific to them (a mode string, or a station
# list). Legacy `properties`-dict layers keep working untouched — see
# _layer_matches_any below for how the two coexist.
_CRITERION_VALUE_FOR = {
    "days": lambda seq, summary: summary["days"],
    "block": lambda seq, summary: _num_or_none(summary.get("block")) or 0.0,
    "tafb": lambda seq, summary: _num_or_none(summary.get("tafb")) or 0.0,
    "tpay": lambda seq, summary: _num_or_none(summary.get("tpay")) or 0.0,
    "report": lambda seq, summary: _hhmm_int(summary.get("report")),
    "release": lambda seq, summary: _hhmm_int(summary.get("release")),
    "legs_per_day": lambda seq, summary: _seq_max_legs_per_day(seq),
}
_STATION_CRITERIA = {"layover_include", "include_stations", "avoid_stations"}
# Equipment is per LEG, not per pairing — a trip can change aircraft between
# days — so both the filter and the search look at every leg's code.
_AC_CRITERIA = {"include_ac", "avoid_ac"}


def _sequence_ac_codes(seq):
    return {
        (leg.get("equipment") or "").strip().upper()
        for day in seq.get("duty_days") or []
        for leg in day.get("legs") or []
        if (leg.get("equipment") or "").strip()
    }


def _sequence_flight_numbers(seq):
    return {
        (leg.get("flight_number") or "").strip().upper()
        for day in seq.get("duty_days") or []
        for leg in day.get("legs") or []
        if (leg.get("flight_number") or "").strip()
    }


def _sequence_matches_query(seq, summary, q):
    """Free-text search over one pairing. One box, because a pilot looking
    for "LAX" or "320S" or "1704" should not first have to say which KIND of
    thing it is. Terms are ANDed, so "LAX 320S" means both.

    Understands, per term:
      SEQ number            exact or prefix on the sequence number
      station               any stop in the routing
      A-B                   a leg flown from A to B, in that order, plus the
                            pairing's own origin-destination
      AC code               any leg's equipment
      flight number         any leg's flight number
    """
    q = (q or "").strip().upper()
    if not q:
        return True
    routing = [str(x).upper() for x in summary.get("routing") or []]
    route_set = set(routing)
    pairs = {f"{a}-{b}" for a, b in zip(routing, routing[1:])}
    if routing:
        pairs.add(f"{routing[0]}-{routing[-1]}")
    acs = _sequence_ac_codes(seq)
    flts = _sequence_flight_numbers(seq)
    seq_no = str(summary.get("seq") or "").upper()

    for term in q.replace(",", " ").split():
        if "-" in term and term in pairs:
            continue
        if term in route_set or term in acs or term in flts:
            continue
        if seq_no == term or seq_no.startswith(term):
            continue
        # Partial equipment ("320" finding 320S/320N) and partial flight
        # numbers, which is how people actually half-remember them.
        if any(a.startswith(term) for a in acs) or any(f.startswith(term) for f in flts):
            continue
        return False
    return True



def _criterion_matches(seq, summary, criterion):
    """One layer's single criterion against one sequence. Unknown fields and
    blank/unparseable values pass rather than fail — an incomplete layer the
    pilot is still building shouldn't silently zero out their whole list."""
    c = criterion or {}
    field = c.get("field")
    value = c.get("value")

    if field == "red_eye":
        mode = value or "any"
        if mode == "any":
            return True
        has_red_eye = _seq_has_red_eye(seq)
        return has_red_eye if mode == "only" else not has_red_eye

    if field in _STATION_CRITERIA:
        wanted = {s.strip().upper() for s in (value or []) if s and s.strip()}
        if not wanted:
            return True
        if field == "layover_include":
            # Strict overnight-only sense, vs include/avoid's route-wide
            # "touches this stop at all" — same two axes the flat form had.
            layovers = {summary["routing"][i] for i in summary["layover_indices"]}
            return bool(layovers & wanted)
        route = set(summary["routing"])
        return bool(route & wanted) if field == "include_stations" else not (route & wanted)

    if field in _AC_CRITERIA:
        wanted = {a.strip().upper() for a in (value or []) if a and a.strip()}
        if not wanted:
            return True
        flown = _sequence_ac_codes(seq)
        # Prefix, so "320" covers 320S/320N/320D without listing each — the
        # 4-character sub-fleet codes are what the packs carry now.
        hit = any(any(a.startswith(w) for a in flown) for w in wanted)
        return hit if field == "include_ac" else not hit

    getter = _CRITERION_VALUE_FOR.get(field)
    if getter is None:
        return True
    threshold = _num_or_none(value)
    if threshold is None:
        return True
    actual = getter(seq, summary)
    if actual is None:
        return False
    op = c.get("op") or "min"
    if op == "max":
        return actual <= threshold
    if op == "exact":
        return actual == threshold
    return actual >= threshold


def _criteria_matches(seq, summary, criteria):
    return all(_criterion_matches(seq, summary, c) for c in (criteria or []))


def _layer_matches_any(seq, summary, layer_or_props):
    """Dispatch for either layer shape. A layer with a `criteria` list uses
    the ordered stack; anything else falls back to the legacy flat
    `properties` dict, so layers saved before this change keep matching
    exactly as they did with no migration step."""
    if isinstance(layer_or_props, dict) and layer_or_props.get("criteria") is not None:
        return _criteria_matches(seq, summary, layer_or_props.get("criteria"))
    props = (layer_or_props or {}).get("properties", layer_or_props) if isinstance(layer_or_props, dict) else layer_or_props
    return _layer_matches(seq, summary, props)


def _layer_funnel(sequences_by_pack, criteria):
    """Per-layer match counts down the stack: how many survive Layer 1, then
    Layers 1-2, then 1-3... The whole point of numbering the layers — it
    shows exactly which layer took the bid from "plenty" to "nothing", which
    a single final count never can."""
    counts = []
    for depth in range(1, len(criteria or []) + 1):
        prefix = criteria[:depth]
        counts.append(sum(
            1 for seq, summary in sequences_by_pack
            if _criteria_matches(seq, summary, prefix)
        ))
    return counts


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


def _scope_sequences(opr, base, fleet):
    """Every (sequence, summary) pair in scope, or None when the scope
    resolves to no packs at all. Summaries are built once here so a funnel
    pass (which re-tests the same sequences at every depth) never re-runs
    _summarize_sequence per layer."""
    packs = _packs_for_scope(opr, base, fleet)
    if not packs:
        return None
    return [(s, _summarize_sequence(s)) for pack in packs for s in (pack.sequences or [])]


def _count_layer_matches(opr, base, fleet, layer_or_props):
    """Shared by the layer list (persisted layers) and the live preview (a
    not-yet-saved layer) — one pass over every pack the scope resolves to
    (one pack in the common case, more when any of opr/base/fleet is
    ALL_SCOPE)."""
    scoped = _scope_sequences(opr, base, fleet)
    if scoped is None:
        return None
    return sum(1 for seq, summary in scoped if _layer_matches_any(seq, summary, layer_or_props))


@app.route("/pbs/layers/preview", methods=["POST"])
def preview_bid_layer():
    """Live count for the form, before a layer is saved. POST (not GET with
    query params like the old flat-properties version) because a layer is
    now an ordered list of criterion objects — a shape that doesn't survive
    a query string cleanly. Returns the per-layer funnel alongside the final
    count so the form can show exactly where the stack narrows to nothing."""
    body = request.get_json(silent=True) or {}
    opr = (body.get("opr") or "").strip().upper()
    base = (body.get("base") or "").strip().upper()
    fleet = (body.get("fleet") or "").strip().upper()
    if not opr or not base or not fleet:
        return jsonify({"error": "opr, base, and fleet are all required"}), 400
    criteria = body.get("criteria") or []
    scoped = _scope_sequences(opr, base, fleet)
    if scoped is None:
        return jsonify({"error": "no pack found for that operator/base/fleet"}), 404
    count = sum(1 for seq, summary in scoped if _criteria_matches(seq, summary, criteria))
    return jsonify({
        "count": count,
        "funnel": _layer_funnel(scoped, criteria),
        "total_in_scope": len(scoped),
    })


@app.route("/pbs/layers")
def list_bid_layers():
    layers = current_user.bid_layers or []
    out = []
    for layer in layers:
        count = _count_layer_matches(layer["opr"], layer["base"], layer["fleet"], layer)
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
        "criteria": body.get("criteria") or [],
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
    if "criteria" in body:
        # Saving a criteria stack retires this layer's legacy flat
        # properties dict — leaving both would make which one actually
        # filters depend on _layer_matches_any's precedence rather than on
        # anything the pilot can see in the form.
        updated["criteria"] = body.get("criteria") or []
        updated.pop("properties", None)
    # Saved layers used to lock opr/base/fleet at creation — a real bid
    # sometimes needs a rescoped layer (moved to a different fleet, or
    # widened to ALL) without losing its saved filters/name/position.
    if "opr" in body or "base" in body or "fleet" in body:
        new_opr = (body.get("opr") or updated["opr"]).strip().upper()
        new_base = (body.get("base") or updated["base"]).strip().upper()
        new_fleet = (body.get("fleet") or updated["fleet"]).strip().upper()
        if not _packs_for_scope(new_opr, new_base, new_fleet):
            return jsonify({"error": "no pack found for that operator/base/fleet"}), 404
        updated["opr"], updated["base"], updated["fleet"] = new_opr, new_base, new_fleet
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


_SORT_KEYS = {"seq", "days", "block", "tafb", "tpay", "report", "release", "dacv"}


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
    # Search narrows the RESULTS, not the funnel: total_in_scope and the
    # per-layer counts below still describe what the stack itself does, so
    # typing in the box cannot make a layer look like it filters differently
    # than it does.
    _q = request.args.get("q") or ""
    matches, scoped = [], []
    for pack in packs:
        for s in (pack.sequences or []):
            summary = _summarize_sequence(s)
            scoped.append((s, summary))
            if _layer_matches_any(s, summary, layer) and _sequence_matches_query(s, summary, _q):
                # Tagged per-match rather than trusting the layer's own
                # opr/base/fleet — those can be ALL_SCOPE, spanning
                # several packs, so each result needs its own real pack
                # identity for detail/promote navigation.
                summary["opr"], summary["base"], summary["fleet"] = pack.opr, pack.base, pack.fleet
                matches.append(summary)
    matches = _sort_summaries(matches, request.args.get("sort"))
    return jsonify({
        "layer": layer,
        "q": _q,
        "pairings": matches,
        # Empty for a legacy properties-only layer, which has no stack to
        # walk — the frontend only renders a funnel when there is one.
        "funnel": _layer_funnel(scoped, layer.get("criteria") or []),
        "total_in_scope": len(scoped),
    })


# ---------------------------------------------------------------------------
# Company documents — instance-wide PDFs (ops manuals, bulletins, revisions)
# published by an admin and acknowledged by every pilot.
#
# Same "upload once, everyone sees it" model as PairingPack, with one hard
# difference: publishing forces an acknowledgement on every other pilot, so
# unlike pack import (honor system, user_id audit-only) this is gated on a
# real User.is_admin. Uploads come through upload_docs.py, mirroring
# bulk_import_packs.py.
# ---------------------------------------------------------------------------
# Base64 inflates by ~4/3, and the whole payload is JSON-parsed in memory on
# a small Railway dyno — cap the decoded PDF rather than letting an
# accidental 200MB upload take the process down.
MAX_DOC_BYTES = 25 * 1024 * 1024


def _admin_required(action="publish documents"):
    """None when the caller is an admin, else a ready-to-return 403 naming
    what they were trying to do — a stats request refused with "only an
    admin can publish documents" reads like a bug in the caller."""
    if bool(getattr(current_user, "is_admin", False)):
        return None
    return jsonify({"error": f"only an admin can {action}"}), 403


def _doc_slug(filename):
    """Stable cross-revision identity from the filename — lowercased, no
    extension, non-alphanumerics collapsed to '-'. Re-uploading
    "FOM-Rev-12.pdf" over "FOM Rev 12.PDF" is the same document."""
    stem = re.sub(r"\.pdf$", "", (filename or "").strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "document"


def _doc_summary(doc, acked_ids):
    return {
        "id": doc.id, "slug": doc.slug, "title": doc.title,
        "filename": doc.filename, "category": doc.category or "",
        "revision": doc.revision, "size_bytes": doc.size_bytes or 0,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else "",
        "acknowledged": doc.id in acked_ids,
    }


def _acked_doc_ids():
    return {
        a.document_id for a in
        DocumentAck.query.filter_by(user_id=current_user.id).all()
    }


def _unacked_doc_count():
    """Drives the blocking banner. Counts documents this pilot has no ack
    row for — a revision bump deletes that document's acks, so a revised
    doc comes back into this count automatically."""
    total = Document.query.count()
    if not total:
        return 0
    return total - DocumentAck.query.filter_by(user_id=current_user.id).count()


@app.route("/docs/list")
def list_documents():
    acked = _acked_doc_ids()
    docs = Document.query.order_by(Document.uploaded_at.desc()).all()
    return jsonify({
        "documents": [_doc_summary(d, acked) for d in docs],
        "unacknowledged": _unacked_doc_count(),
        "is_admin": bool(getattr(current_user, "is_admin", False)),
    })


@app.route("/docs/<int:doc_id>/pdf")
def document_pdf(doc_id):
    doc = db.session.get(Document, doc_id)
    if not doc:
        return jsonify({"error": "not found"}), 404
    acked = _acked_doc_ids()
    return jsonify({**_doc_summary(doc, acked), "pdf_b64": doc.pdf_b64})


@app.route("/docs/<int:doc_id>/ack", methods=["POST"])
def acknowledge_document(doc_id):
    doc = db.session.get(Document, doc_id)
    if not doc:
        return jsonify({"error": "not found"}), 404
    existing = DocumentAck.query.filter_by(document_id=doc_id, user_id=current_user.id).first()
    acknowledged_at = datetime.now(timezone.utc).isoformat()
    if existing:
        # Idempotent — re-acknowledging just refreshes the recorded
        # revision/timestamp rather than erroring or duplicating the row.
        existing.revision, existing.acknowledged_at = doc.revision, acknowledged_at
    else:
        db.session.add(DocumentAck(
            document_id=doc_id, user_id=current_user.id,
            revision=doc.revision, acknowledged_at=acknowledged_at,
        ))
    db.session.commit()
    LOG.info(f"DOC ACK doc={doc_id} rev={doc.revision} user={current_user.username} at={acknowledged_at}")
    return jsonify({"ok": True, "acknowledged_at": acknowledged_at, "unacknowledged": _unacked_doc_count()})


@app.route("/docs/import", methods=["POST"])
def import_document():
    """Body: {title, filename, pdf_b64, category}. Re-uploading the same
    slug replaces that document in place, bumps its revision, and clears
    its acknowledgements so every pilot has to acknowledge the new
    revision — the whole point of versioning these."""
    denied = _admin_required()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    filename = (body.get("filename") or "").strip()
    pdf_b64 = (body.get("pdf_b64") or "").strip()
    title = (body.get("title") or "").strip() or re.sub(r"\.pdf$", "", filename, flags=re.I)
    if not filename or not pdf_b64:
        return jsonify({"error": "filename and pdf_b64 are both required"}), 400
    try:
        raw = base64.b64decode(pdf_b64, validate=True)
    except Exception:
        return jsonify({"error": "pdf_b64 is not valid base64"}), 400
    if not raw.startswith(b"%PDF"):
        return jsonify({"error": "that file isn't a PDF"}), 400
    if len(raw) > MAX_DOC_BYTES:
        return jsonify({"error": f"PDF is {len(raw) // (1024*1024)}MB — the limit is {MAX_DOC_BYTES // (1024*1024)}MB"}), 413

    slug = _doc_slug(filename)
    doc = Document.query.filter_by(slug=slug).first()
    if doc:
        doc.title, doc.filename = title, filename
        doc.category = (body.get("category") or "").strip() or None
        doc.pdf_b64, doc.size_bytes = pdf_b64, len(raw)
        doc.revision += 1
        doc.uploaded_by, doc.uploaded_at = current_user.id, datetime.now(timezone.utc)
        DocumentAck.query.filter_by(document_id=doc.id).delete()
    else:
        doc = Document(
            slug=slug, title=title, filename=filename,
            category=(body.get("category") or "").strip() or None,
            pdf_b64=pdf_b64, size_bytes=len(raw), revision=1,
            uploaded_by=current_user.id,
        )
        db.session.add(doc)
    db.session.commit()
    LOG.info(f"DOC PUBLISH slug={slug} rev={doc.revision} by={current_user.username} bytes={len(raw)}")
    return jsonify({
        "id": doc.id, "slug": doc.slug, "title": doc.title,
        "revision": doc.revision, "size_bytes": doc.size_bytes,
        "acks_cleared": doc.revision > 1,
    })


@app.route("/docs/sync", methods=["POST"])
def sync_documents():
    """Body: {"slugs": [...], "dry_run": bool}. Makes the published set
    match the uploader's folder by deleting every document whose slug is
    NOT in the list.

    Import on its own is add-or-update, so a file deleted from data/DOCS
    stayed published forever with nothing in the app to say it was stale —
    the reported "it caches old deleted ones". This is the other half.

    Two deliberate guards, because this deletes PDFs that exist nowhere
    else once removed (documents live in the database, not in the deployed
    code):
      * An empty slug list is refused outright. That is what an upload run
        against the wrong directory looks like, and honouring it would wipe
        every published document.
      * dry_run returns exactly what WOULD go without touching anything,
        so the uploader can show the list and ask before doing it.
    """
    denied = _admin_required("sync documents")
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    slugs = body.get("slugs")
    if not isinstance(slugs, list) or not slugs:
        return jsonify({"error": "slugs must be a non-empty list — refusing to "
                                 "delete every document"}), 400
    keep = {_doc_slug(str(x)) for x in slugs}
    doomed = [d for d in Document.query.order_by(Document.title).all() if d.slug not in keep]
    removed = [{"id": d.id, "slug": d.slug, "title": d.title, "filename": d.filename}
               for d in doomed]
    if body.get("dry_run"):
        return jsonify({"would_remove": removed, "kept": len(keep)})
    for d in doomed:
        DocumentAck.query.filter_by(document_id=d.id).delete()
        db.session.delete(d)
    db.session.commit()
    return jsonify({"removed": removed, "kept": len(keep)})


@app.route("/docs/<int:doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    denied = _admin_required("delete documents")
    if denied:
        return denied
    doc = db.session.get(Document, doc_id)
    if not doc:
        return jsonify({"error": "not found"}), 404
    DocumentAck.query.filter_by(document_id=doc_id).delete()
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin stats — roster, activity, and document-acknowledgement compliance.
# Read-only; the only thing here that can change state is nothing at all.
# ---------------------------------------------------------------------------
def _iso(dt):
    return dt.isoformat() if dt else ""


@app.route("/admin/stats")
def admin_stats():
    denied = _admin_required("view crew stats")
    if denied:
        return denied

    users = User.query.order_by(User.username).all()
    docs = Document.query.order_by(Document.uploaded_at.desc()).all()
    user_count = len(users)

    # One grouped query per table instead of per-user counts in a loop —
    # this page is small today but it's the kind of thing that quietly
    # becomes N+1 once there's a real crew list.
    def _counts(model, col):
        return dict(
            db.session.query(col, db.func.count()).group_by(col).all()
        )

    legs_by_user = _counts(Leg, Leg.user_id)
    sigs_by_user = _counts(SignatureLog, SignatureLog.user_id)
    trips_by_user = _counts(TripCheckIn, TripCheckIn.user_id)
    acks_by_user = _counts(DocumentAck, DocumentAck.user_id)

    roster = [{
        "id": u.id,
        "username": u.username,
        "is_admin": bool(u.is_admin),
        "created_at": _iso(u.created_at),
        "last_seen": _iso(u.last_seen),
        "timezone": u.timezone or "",
        "active_seq": u.active_seq or "",
        "legs": legs_by_user.get(u.id, 0),
        "signings": sigs_by_user.get(u.id, 0),
        "trip_checkins": trips_by_user.get(u.id, 0),
        "docs_acked": acks_by_user.get(u.id, 0),
        "docs_outstanding": len(docs) - acks_by_user.get(u.id, 0),
    } for u in users]

    # Compliance is per document: who has signed off on the CURRENT
    # revision. A re-upload deletes that document's acks, so anyone
    # missing here genuinely hasn't acknowledged what's published now.
    acks_by_doc = {}
    for a in DocumentAck.query.all():
        acks_by_doc.setdefault(a.document_id, {})[a.user_id] = a
    by_id = {u.id: u for u in users}
    documents = []
    for d in docs:
        acked = acks_by_doc.get(d.id, {})
        documents.append({
            "id": d.id, "title": d.title, "revision": d.revision,
            "category": d.category or "",
            "uploaded_at": _iso(d.uploaded_at),
            "acked_count": len(acked), "user_count": user_count,
            "acknowledged": sorted(
                ({"username": by_id[uid].username, "at": a.acknowledged_at}
                 for uid, a in acked.items() if uid in by_id),
                key=lambda r: r["username"],
            ),
            "outstanding": sorted(u.username for u in users if u.id not in acked),
        })

    return jsonify({"users": roster, "documents": documents, "user_count": user_count})


@app.route("/admin/users/<int:user_id>")
def admin_user_detail(user_id):
    denied = _admin_required("view crew stats")
    if denied:
        return denied
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not found"}), 404

    legs = (Leg.query.filter_by(user_id=user_id)
            .order_by(Leg.created_at.desc()).limit(25).all())
    sigs = (SignatureLog.query.filter_by(user_id=user_id)
            .order_by(SignatureLog.id.desc()).limit(25).all())
    trips = (TripCheckIn.query.filter_by(user_id=user_id)
             .order_by(TripCheckIn.id.desc()).limit(25).all())
    acked = {a.document_id: a for a in DocumentAck.query.filter_by(user_id=user_id).all()}

    return jsonify({
        "user": {
            "id": user.id, "username": user.username,
            "is_admin": bool(user.is_admin),
            "created_at": _iso(user.created_at), "last_seen": _iso(user.last_seen),
            "timezone": user.timezone or "", "active_seq": user.active_seq or "",
            "simbrief_user": user.default_simbrief_user or "",
        },
        "legs": [{
            "id": l.id, "flight_number": l.data.get("flight_number", ""),
            "origin": l.data.get("origin", ""), "destination": l.data.get("destination", ""),
            "seq": l.data.get("seq", ""), "created_at": _iso(l.created_at),
            "fit_for_duty": bool(l.data.get("fit_for_duty")),
        } for l in legs],
        "signings": [{
            "flight_number": s.flight_number or "", "dep_date": s.dep_date or "",
            "signed_at": s.signed_at or "",
        } for s in sigs],
        "trip_checkins": [{"seq": t.seq, "signed_at": t.signed_at or ""} for t in trips],
        "documents": [{
            "title": d.title, "revision": d.revision,
            "acknowledged": d.id in acked,
            "at": acked[d.id].acknowledged_at if d.id in acked else "",
        } for d in Document.query.order_by(Document.uploaded_at.desc()).all()],
    })


@app.route("/docs")
def docs_root():
    """The Docs tab's own leg-independent root, same pattern as
    /schedule — company documents aren't scoped to any one flight, and the
    acknowledgement banner has to be reachable with no leg loaded."""
    return Response(render_fos_html({"id": ""}, default_view="doclocker"), mimetype="text/html")


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
                "duty_day": day["duty_day"], "da": leg.get("da"), "flight_number": leg.get("flight_number"),
                "origin": leg.get("origin"), "destination": leg.get("destination"),
                "mot_display": _mot_display(mot or "", leg.get("origin") or "", current_user.timezone),
                "fdp_remaining": fdp_remaining,
                "generated": row is not None,
                "signed_at": (row.data.get("ffd_signed_at") if row else "") or "",
                "signed_by": (row.data.get("ffd_signed_by") if row else "") or "",
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
    q = request.args.get("q") or ""
    summaries = []
    for seq in (row.sequences or []):
        summary = _summarize_sequence(seq)
        if _sequence_matches_query(seq, summary, q):
            summaries.append(summary)
    return jsonify(_sort_summaries(summaries, request.args.get("sort")))


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
    # Stored so the panel can be repainted later without another lookup —
    # see DEFAULT_LEG["aero_suggestion"]. Guarded to a dict so a malformed
    # body can't put a string or a list where the page expects an object.
    if isinstance(body.get("suggestion"), dict):
        changes["aero_suggestion"] = body["suggestion"]
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
@app.route("/pbs/sequences/<seq_number>/recover-options", methods=["POST"])
def recover_options(seq_number):
    """Recovery options for one leg of one duty day, worked in priority
    order and labelled with which priority answered.

    Body: {duty_day, leg_index, kind, at, station?}. `kind` is the
    disruption — late_departure / late_arrival / diverted / cancelled — and
    `at` is the one time that kind implies (new departure, actual arrival,
    or when you are available again). `station` only matters for a
    diversion, where you are somewhere the pairing never planned for.

    Priorities, first one that yields anything wins:

      day_intact  reach this day's own planned destination, so every later
                  day reattaches exactly as printed
      same_length rebuild through to domicile without adding a day
      get_home    domicile, extra days allowed

    Whether the duty clock has started is NOT asked — it is read from FFD.
    Unsigned means the day has not begun and the crew can be fully
    reassigned off a fresh report; signed means duty is running from
    ffd_signed_at and only what fits the remaining period is offered.
    """
    seq = next((x for x in _pbs_sequences() if x["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    try:
        duty_day = int(body.get("duty_day"))
        leg_index = int(body.get("leg_index"))
    except (TypeError, ValueError):
        return jsonify({"error": "duty_day and leg_index are required"}), 400
    kind = (body.get("kind") or "").strip() or "late_departure"
    if kind not in _DISRUPTION_LABELS:
        return jsonify({"error": f"unknown disruption type {kind!r}"}), 400
    at_hhmm = re.sub(r"[^0-9]", "", str(body.get("at") or ""))[:4]
    if len(at_hhmm) != 4:
        return jsonify({"error": "a time in HHMM is required"}), 400

    days = seq.get("duty_days") or []
    day = next((d for d in days if d.get("duty_day") == duty_day), None)
    if not day:
        return jsonify({"error": f"sequence has no duty day {duty_day}"}), 404
    day_legs = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(day_legs):
        return jsonify({"error": "no such leg on that day"}), 404
    leg = day_legs[leg_index]

    first_day = days[0] if days else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day.get("legs") else ""
    if not dom:
        return jsonify({"error": "sequence has no origin to recover back to"}), 400

    # FFD state decides the constraint. Read off whichever Leg row belongs to
    # this sequence and this flight — a day the pilot never generated a leg
    # for simply has no signature, which is itself the answer.
    signed, report_hhmm = False, (day.get("report") or "")
    for row in Leg.query.filter_by(user_id=current_user.id).all():
        if (row.data or {}).get("seq") != seq_number:
            continue
        if (row.data.get("flight_number") or "").strip() == (leg.get("flight_number") or "").strip():
            signed = bool(row.data.get("fit_for_duty"))
            break
    originated = leg_index > 0 or duty_day > (days[0].get("duty_day") if days else 1)

    legs_net, ap = pairing_engine.get_route_data()

    # Where the crew is and from when, which is all the engine needs. A late
    # departure or a cancellation leaves them at the leg's ORIGIN; a late
    # arrival puts them at the planned destination; a diversion somewhere
    # else entirely.
    #
    # Two shapes, not four. A cancellation and a departure going late both
    # leave the crew standing at the ORIGIN with the planned leg not flown
    # as planned — what they actually fly next has to be searched for, so
    # the leg is dropped rather than rewritten. A late arrival or a
    # diversion did fly: the leg happened, it just ended somewhere or
    # sometime other than planned.
    drop_disrupted = kind in ("late_departure", "cancelled")
    if drop_disrupted:
        at_station = leg.get("origin")
    elif kind == "late_arrival":
        at_station = leg.get("destination")
    else:
        at_station = (body.get("station") or "").strip().upper() or leg.get("destination")

    tiers = []
    try:
        # 1. Day intact — everything after this day survives untouched.
        # FFD unsigned: the day has not begun, so `at` IS the report time.
        # Signed: duty is already running, so `at` is when it ended and the
        # engine adds minimum rest as it always has.
        cands, target, reached = pairing_edit.day_scoped_recovery(
            seq, dom, ap, legs_net, duty_day, leg_index,
            at_hhmm if signed else None,
            report_local=None if signed else at_hhmm,
        )
        # Only offer what can actually be accepted. day_scoped_recovery
        # searches the disrupted day in isolation; apply_day_patch then
        # re-checks that the patch's later release still leaves legal rest
        # before the first reattached day, and can legitimately refuse.
        # Filtering here rather than at accept time means the pilot never
        # taps an option that turns out to be illegal.
        applicable = []
        for c in cands[:_REC_TRY_MAX]:
            if len(applicable) >= _REC_SHOW_MAX:
                break
            try:
                patched, _errs = pairing_edit.apply_day_patch(
                    seq, dom, ap, legs_net, duty_day, leg_index,
                    at_hhmm if signed else None,
                    c["chain"], c["day_number"], c["dlegs_today"],
                    c["dblk_today"], c["duty_report_utc"], c["total_days"],
                    reattach=reached,
                    report_local=None if signed else at_hhmm,
                )
            except Exception as e:
                LOG.warning(f"day patch trial failed for {seq_number}: {e}")
                continue
            if patched is not None:
                # The trial apply already built the repaired trip, so the
                # legs it would give are free to carry along.
                c = dict(c, days=_candidate_days(patched, duty_day))
                applicable.append(c)
        if applicable:
            tiers.append({"tier": "day_intact", "target": target,
                          "reached_target": reached, "candidates": applicable})
    except Exception as e:
        LOG.warning(f"day_scoped_recovery failed for {seq_number} d{duty_day}: {e}")

    if not tiers:
        for tier_name, extra in (("same_length", 0), ("get_home", 2)):
            try:
                cands, violations = pairing_edit.recover_from_disruption(
                    seq, dom, ap, legs_net, duty_day, leg_index,
                    at_station, at_hhmm, max_extra_days=extra,
                    drop_disrupted=drop_disrupted,
                )
            except Exception as e:
                LOG.warning(f"recover_from_disruption({extra}) failed: {e}")
                cands, violations = [], [str(e)]
            applicable = []
            for c in cands[:_REC_TRY_MAX]:
                if len(applicable) >= _REC_SHOW_MAX:
                    break
                try:
                    rebuilt, _errs = pairing_edit.apply_recovery(
                        seq, dom, ap, legs_net, duty_day, leg_index,
                        at_station, at_hhmm,
                        c["chain"], c["day_number"], c["dlegs_today"],
                        c["dblk_today"], c["duty_report_utc"], c["total_days"],
                        drop_disrupted=drop_disrupted,
                    )
                except Exception as e:
                    LOG.warning(f"recovery trial failed for {seq_number}: {e}")
                    continue
                if rebuilt is not None:
                    applicable.append(dict(c, days=_candidate_days(rebuilt, duty_day)))
            if applicable:
                tiers.append({"tier": tier_name, "target": dom,
                              "reached_target": True, "candidates": applicable})
                break

    return jsonify({
        "seq": seq_number, "duty_day": duty_day, "leg_index": leg_index,
        "kind": kind, "at": at_hhmm, "station": at_station,
        "ffd_signed": signed, "originated": originated,
        "report": report_hhmm,
        "repair_window_end": _repair_window_end(at_hhmm, report_hhmm, originated),
        "leg": {"flight_number": leg.get("flight_number"), "origin": leg.get("origin"),
                "destination": leg.get("destination"), "dep_local": leg.get("dep_local")},
        "options": tiers,
    })


def _disruption_start(ap, days, day, leg, leg_index, kind, at_hhmm, station_hint):
    """Where the disruption leaves the crew, and in what duty state.

    Returns (start, prefix_steps, error). `prefix_steps` is whatever they
    genuinely flew before the repair begins — the legs earlier in the day,
    plus the disrupted leg itself when it actually operated (a diversion or
    an overrun) and not when it never left the gate.
    """
    day_legs = day.get("legs") or []
    kept = day_legs[:leg_index]
    drop = kind in ("late_departure", "cancelled")
    try:
        if drop:
            station, avail = pairing_edit.anchor_available(ap, leg, kept, at_hhmm)
            legs_flown = len(kept)
            block_flown = sum(pairing_edit._bid_or_hhmm_span_to_dec(l) for l in kept)
            disrupted_step = None
        else:
            station = (station_hint or leg.get("destination")) if kind == "diverted" \
                else leg.get("destination")
            dep_utc, avail, blk = pairing_edit.anchor_arrival(ap, leg, station, at_hhmm)
            legs_flown = len(kept) + 1
            block_flown = sum(pairing_edit._bid_or_hhmm_span_to_dec(l) for l in kept) + blk
            disrupted_step = dict(
                day=day["duty_day"],
                leg=dict(f=leg.get("flight_number", ""), o=leg["origin"], d=station,
                          blk=blk, fleet=leg.get("equipment", "")),
                dep=dep_utc, arr=avail,
            )
    except (ValueError, KeyError) as e:
        return None, None, f"invalid disruption data: {e}"

    if kept:
        first = kept[0]
        report_utc = (pairing_edit._hhmm_to_dec(first["dep_local"])
                      - ap.off(first["origin"]) - pairing_engine.Rules.BRIEF)
    else:
        report_utc = avail - pairing_engine.Rules.BRIEF

    prefix_steps = [dict(
        day=day["duty_day"],
        leg=dict(f=l["flight_number"], o=l["origin"], d=l["destination"],
                  blk=pairing_edit._bid_or_hhmm_span_to_dec(l), fleet=l.get("equipment", "")),
        dep=pairing_edit._hhmm_to_dec(l["dep_local"]) - ap.off(l["origin"]),
        arr=pairing_edit._hhmm_to_dec(l["arr_local"]) - ap.off(l["destination"]),
    ) for l in kept]
    if disrupted_step:
        prefix_steps.append(disrupted_step)

    return {
        "station": station, "avail": avail, "legs_flown": legs_flown,
        "block_flown": block_flown, "report_utc": report_utc,
        "day_number": day["duty_day"], "hbt": ap.off(station),
    }, prefix_steps, None


@app.route("/pbs/sequences/<seq_number>/recover-step", methods=["POST"])
def recover_step(seq_number):
    """One step of building the repair by hand.

    The pilot picks every leg; this only replays what they have picked so
    far and says what the rules allow next. Nothing is ranked, nothing is
    chosen, and no chain is searched for — the compute is theirs.

    `picks` is the network leg indices chosen so far, in order. Replaying
    them each time keeps this stateless, the same way every other route
    here works, and means a Back is just a shorter list.
    """
    seq = next((x for x in _pbs_sequences() if x["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    try:
        duty_day = int(body.get("duty_day"))
        leg_index = int(body.get("leg_index"))
        # Each pick says which list it came from. Inferring it here instead
        # would let the replay disagree with what was actually offered —
        # a leg can be legal only after a rest, and guessing "same day"
        # because its clock time is earlier would rebuild a different trip
        # from the one the pilot chose.
        picks = [{"index": int(p["index"]), "after_rest": bool(p.get("after_rest"))}
                 for p in (body.get("picks") or [])]
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "duty_day, leg_index and picks are required"}), 400
    kind = (body.get("kind") or "").strip() or "late_departure"
    if kind not in _DISRUPTION_LABELS:
        return jsonify({"error": f"unknown disruption type {kind!r}"}), 400
    at_hhmm = re.sub(r"[^0-9]", "", str(body.get("at") or ""))[:4]
    if len(at_hhmm) != 4:
        return jsonify({"error": "a time in HHMM is required"}), 400

    days = seq.get("duty_days") or []
    day = next((d for d in days if d.get("duty_day") == duty_day), None)
    if not day:
        return jsonify({"error": f"sequence has no duty day {duty_day}"}), 404
    day_legs = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(day_legs):
        return jsonify({"error": "no such leg on that day"}), 404
    leg = day_legs[leg_index]
    dom = days[0]["legs"][0]["origin"] if days and days[0].get("legs") else ""

    legs_net, ap = pairing_engine.get_route_data()
    drop = kind in ("late_departure", "cancelled")

    startstate, _prefix, err = _disruption_start(
        ap, days, day, leg, leg_index, kind, at_hhmm,
        (body.get("station") or "").strip().upper())
    if err:
        return jsonify({"error": err}), 400

    steps, _rests, endstate, err = pairing_edit.replay_picks(ap, legs_net, startstate, picks)
    if err:
        return jsonify({"error": err}), 400
    station = endstate["station"]
    avail = endstate["avail"]
    legs_flown = endstate["legs_flown"]
    block_flown = endstate["block_flown"]
    report_utc = endstate["report_utc"]
    day_number = endstate["day_number"]
    # Label the trail with the day numbers the trip will actually end up
    # with. replay_picks counts duty periods from the disrupted day, but a
    # day left with no flying at all simply ceases to exist once the repair
    # is rendered — so a preview that counted it would be one ahead of the
    # trip the pilot gets.
    _seen = sorted({st["day"] for st in steps})
    _label = {d: duty_day + i for i, d in enumerate(_seen)}
    trail = [{
        "index": p["index"], "flight_number": st["leg"]["f"],
        "origin": st["leg"]["o"], "destination": st["leg"]["d"],
        "dep_local": pairing_edit._dec_to_hhmm(st["dep"] + ap.off(st["leg"]["o"])),
        "arr_local": pairing_edit._dec_to_hhmm(st["arr"] + ap.off(st["leg"]["d"])),
        "day": _label[st["day"]], "after_rest": bool(p.get("after_rest")),
    } for p, st in zip(picks, steps)]

    # The leg the pairing said to fly next, if the original trip still has
    # one to offer from where they now stand.
    planned = None
    if not picks:
        rest_today = day_legs[leg_index + 1:] if not drop else day_legs[leg_index:]
        planned = next((l for l in rest_today if l.get("origin") == station), None)
    else:
        for d in days:
            if d.get("duty_day", 0) < duty_day:
                continue
            planned = next((l for l in (d.get("legs") or [])
                            if l.get("origin") == station), None)
            if planned:
                break

    # Delaying the rest of the day in one tap. Offered only when there is
    # more than one leg left to place — a single leg is already one tap on
    # the planned-leg card, and a second button for the same action is
    # just noise.
    rest_of_day = [l for l in (day_legs[leg_index:] if drop else day_legs[leg_index + 1:])]
    cascade = None
    if len(rest_of_day) > 1 and not picks:
        c_picks, placed, stopped = pairing_edit.cascade_delay(
            ap, legs_net, startstate, rest_of_day, picks)
        if len(placed) > 1:
            cascade = {
                "picks": c_picks,
                "legs": [{"flight_number": o["flight_number"], "origin": o["origin"],
                          "destination": o["destination"], "dep_local": o["dep_local"],
                          "arr_local": o["arr_local"], "after_rest": o["after_rest"],
                          "instead_of": o.get("instead_of")}
                         for o in placed],
                "covers": len(placed), "of": len(rest_of_day),
                "stopped_because": stopped,
            }

    state = {"station": station, "avail": avail, "dlegs_today": legs_flown,
             "dblk_today": block_flown, "duty_report_utc": report_utc,
             "day_number": day_number, "hbt": endstate["hbt"]}
    options = pairing_edit.step_options(ap, legs_net, state, planned=planned)

    return jsonify({
        "seq": seq_number, "duty_day": duty_day, "leg_index": leg_index,
        "kind": kind, "at": at_hhmm, "domicile": dom,
        "station": station, "at_home": station == dom and bool(picks),
        "avail_local": pairing_edit._dec_to_hhmm(avail + ap.off(station)),
        "day_number": _label.get(day_number, duty_day), "legs_today": legs_flown,
        "block_today": round(block_flown, 2),
        "trail": trail, "options": options, "cascade": cascade,
        "repair_window_end": _repair_window_end(
            at_hhmm, day.get("report") or "",
            leg_index > 0 or duty_day > (days[0].get("duty_day") if days else 1)),
    })


@app.route("/pbs/sequences/<seq_number>/recover-accept", methods=["POST"])
def recover_accept(seq_number):
    """Takes one option from /recover-options and makes it the trip.

    The client sends back the disruption it reported plus the candidate it
    picked. Which engine applies it depends on the tier: `day_intact` came
    from day_scoped_recovery, so it splices onto just the disrupted day and
    every later day reattaches unchanged; the other two came from
    recover_from_disruption, which rebuilds from the disruption point all
    the way to domicile and discards what the original trip had planned
    after it.

    Two messages get written, in the order they'd really arrive: what
    happened, then the reassignment itself.
    """
    pbs_row = _pbs_row()
    seq = next((s for s in (pbs_row.sequences if pbs_row else []) if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "sequence not found"}), 404

    body = request.get_json(silent=True) or {}
    cand = body.get("candidate") or {}
    tier = (body.get("tier") or "").strip()
    if tier not in ("manual", "day_intact", "same_length", "get_home"):
        return jsonify({"error": f"unknown tier {tier!r}"}), 400
    picks, chain = [], []
    day_number = dlegs_today = duty_report_utc = total_days = 0
    dblk_today = 0.0
    try:
        duty_day = int(body.get("duty_day"))
        leg_index = int(body.get("leg_index"))
        if tier == "manual":
            picks = [{"index": int(p["index"]), "after_rest": bool(p.get("after_rest"))}
                     for p in (body.get("picks") or [])]
            if not picks:
                return jsonify({"error": "nothing picked yet"}), 400
        else:
            chain = [int(i) for i in cand["chain"]]
            day_number = int(cand["day_number"])
            dlegs_today = int(cand["dlegs_today"])
            dblk_today = float(cand["dblk_today"])
            duty_report_utc = float(cand["duty_report_utc"])
            total_days = int(cand["total_days"])
    except (TypeError, ValueError, KeyError) as e:
        return jsonify({"error": f"incomplete candidate: {e}"}), 400
    kind = (body.get("kind") or "").strip() or "late_departure"
    if kind not in _DISRUPTION_LABELS:
        return jsonify({"error": f"unknown disruption type {kind!r}"}), 400
    at_hhmm = re.sub(r"[^0-9]", "", str(body.get("at") or ""))[:4]
    if len(at_hhmm) != 4:
        return jsonify({"error": "a time in HHMM is required"}), 400

    days = seq.get("duty_days") or []
    day = next((d for d in days if d.get("duty_day") == duty_day), None)
    if not day:
        return jsonify({"error": f"sequence has no duty day {duty_day}"}), 404
    day_legs = day.get("legs") or []
    if leg_index < 0 or leg_index >= len(day_legs):
        return jsonify({"error": "no such leg on that day"}), 404
    leg = day_legs[leg_index]

    first_day = days[0] if days else None
    dom = first_day["legs"][0]["origin"] if first_day and first_day.get("legs") else ""
    if not dom:
        return jsonify({"error": "sequence has no origin to recover back to"}), 400

    # Same FFD read as /recover-options — the options were generated under
    # this constraint, so applying one has to honor the same reading of it.
    signed = False
    for row in Leg.query.filter_by(user_id=current_user.id).all():
        if (row.data or {}).get("seq") != seq_number:
            continue
        if (row.data.get("flight_number") or "").strip() == (leg.get("flight_number") or "").strip():
            signed = bool(row.data.get("fit_for_duty"))
            break
    # Same two-shape reading as /recover-options — the option was searched
    # under it, so applying it has to honor it.
    drop_disrupted = kind in ("late_departure", "cancelled")
    if drop_disrupted:
        at_station = leg.get("origin")
    elif kind == "late_arrival":
        at_station = leg.get("destination")
    else:
        at_station = (body.get("station") or "").strip().upper() or leg.get("destination")

    legs_net, ap = pairing_engine.get_route_data()
    if tier == "manual":
        startstate, prefix_steps, err = _disruption_start(
            ap, days, day, leg, leg_index, kind, at_hhmm,
            (body.get("station") or "").strip().upper())
        if err:
            return jsonify({"error": err}), 400
        steps, rests, _end, err = pairing_edit.replay_picks(
            ap, legs_net, startstate, picks)
        if err:
            return jsonify({"error": err}), 400
        new_seq, errs = pairing_edit.apply_steps(
            seq, dom, ap, duty_day, leg_index, prefix_steps, steps, rests)
    elif tier == "day_intact":
        new_seq, errs = pairing_edit.apply_day_patch(
            seq, dom, ap, legs_net, duty_day, leg_index,
            at_hhmm if signed else None,
            chain, day_number, dlegs_today, dblk_today, duty_report_utc, total_days,
            reattach=bool(body.get("reached_target", True)),
            report_local=None if signed else at_hhmm,
        )
    else:
        new_seq, errs = pairing_edit.apply_recovery(
            seq, dom, ap, legs_net, duty_day, leg_index,
            at_station, at_hhmm,
            chain, day_number, dlegs_today, dblk_today, duty_report_utc, total_days,
            drop_disrupted=drop_disrupted,
        )
    if new_seq is None:
        return jsonify({"error": "; ".join(errs) or "could not apply this option"}), 400

    # The repaired trip is a NEW sequence, marked with a trailing asterisk,
    # and the pairing as published stays exactly as it was. Overwriting it
    # in place left no way back once accepted — and a trip that has been
    # rebuilt around a disruption is not the trip that was bid, so it
    # should not go on wearing its number unqualified. Recovering an
    # already-repaired trip replaces that repair rather than starring the
    # star.
    base_seq = seq_number[:-1] if seq_number.endswith("*") else seq_number
    starred = base_seq + "*"
    new_seq = dict(new_seq, seq=starred)
    pbs_row.sequences = ([x for x in pbs_row.sequences if x["seq"] != starred]
                         + [new_seq])
    if (current_user.active_seq or "") in (seq_number, base_seq):
        current_user.active_seq = starred
    db.session.commit()

    # Messages, in the order they would really land. The dedupe key carries
    # the disruption itself so reporting a *different* disruption on the
    # same leg still speaks up, while a double-tap on Accept does not.
    record = {"flight_number": leg.get("flight_number"), "dep_date": day.get("date") or ""}
    key = f"recover:{seq_number}:{duty_day}:{leg_index}:{kind}:{at_hhmm}"
    originated = leg_index > 0 or duty_day > (days[0].get("duty_day") if days else 1)
    position = (seq["positions"][0] if seq.get("positions") else "")
    _add_message(current_user.id, "disruption",
                 _disruption_message(record, seq_number, duty_day, leg, kind, at_hhmm,
                                     day.get("report") or "", originated),
                 key + ":reported", record=record)
    _add_message(current_user.id, "reassignment",
                 _reassignment_message(record, starred, kind,
                                       new_seq.get("duty_days") or [], position),
                 key + ":assigned", record=record)

    return jsonify({"sequence": new_seq, "seq": starred,
                    "unacknowledged": _unacked_message_count()})


@app.route("/pbs/sequences/<seq_number>/revert", methods=["POST"])
def revert_sequence(seq_number):
    """Undo a recovery: drop the starred repair and leave the pairing as
    published.

    Also repairs the older damage. Before repairs were starred, accepting
    one overwrote the sequence in place, so the original is simply gone
    from the trip list — but a promoted sequence keeps a pristine copy in
    the pack library it came from, and that copy is what gets restored.
    """
    pbs_row = _pbs_row()
    if not pbs_row:
        return jsonify({"error": "no imported pairings"}), 404
    base_seq = seq_number[:-1] if seq_number.endswith("*") else seq_number
    starred = base_seq + "*"

    kept = [x for x in (pbs_row.sequences or []) if x["seq"] != starred]
    dropped = len(pbs_row.sequences or []) - len(kept)

    # A repair overwritten in place still carries the original's number, so
    # its presence in the list proves nothing about whether it is the
    # pairing as published. Reverting one therefore REPLACES it with the
    # library's copy rather than checking whether something by that name is
    # already there.
    original = None
    for pack in PairingPack.query.filter_by(user_id=current_user.id).all():
        for cand in (pack.sequences or []):
            if cand.get("seq") == base_seq:
                original = cand
                break
        if original:
            break

    restored = False
    have_base = any(x["seq"] == base_seq for x in kept)
    if original is not None and (not dropped or not have_base):
        kept = [x for x in kept if x["seq"] != base_seq]
        kept.append(copy.deepcopy(original))
        restored = True
    elif not have_base and not dropped:
        return jsonify({"error": f"nothing to restore SEQ {base_seq} from — "
                                 "it is not in any imported pack"}), 404

    if not dropped and not restored:
        return jsonify({"error": f"SEQ {base_seq} has no recovery to revert, and no "
                                 "library copy to restore it from"}), 400

    pbs_row.sequences = kept
    if (current_user.active_seq or "") in (starred, base_seq):
        current_user.active_seq = base_seq
    db.session.commit()
    return jsonify({"seq": base_seq, "dropped_repair": bool(dropped),
                    "restored_from_pack": restored})


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

    # FFD and eFlight Plan each get their own signature/signed_at/signed_by
    # trio — they used to share one signature/signed_at pair, so signing
    # either one silently marked BOTH "signed" and clobbered whichever had
    # signed first with the other's time.
    kind = "ffd" if body.get("kind") == "ffd" else "eflightplan"
    signed_at = datetime.now(timezone.utc).isoformat()
    signed_by = current_user.username
    new_data = {
        **row.data,
        f"{kind}_signature": signature,
        f"{kind}_signed_at": signed_at,
        f"{kind}_signed_by": signed_by,
    }
    # Fit for Duty is a real attestation, not a bare checkbox — signing it
    # here sets the flag directly (not toggle_ffd's flip) so re-signing
    # can never accidentally turn an already-true declaration back off.
    if kind == "ffd":
        new_data["fit_for_duty"] = True
    data = _save_leg(row, new_data)

    db.session.add(SignatureLog(
        user_id=current_user.id, leg_id=leg_id,
        flight_number=data.get("flight_number"), dep_date=data.get("dep_date"),
        signed_at=signed_at,
    ))
    db.session.commit()
    LOG.info(f"SIGNATURE leg={leg_id} kind={kind} by={signed_by} at={signed_at}")

    return jsonify({"signed_at": signed_at, "signed_by": signed_by, "kind": kind})


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


def _ofp_is_this_leg(simbrief_user, record):
    """Is the OFP sitting on this pilot's SimBrief account right now
    genuinely THIS leg? Same flight number + city pair check
    generate_from_pbs already uses — an account only ever holds one OFP,
    and it belongs to whichever flight was dispatched last, not to
    whichever leg the app happens to be showing.

    Returns (True, "") when it matches, (False, <reason>) otherwise.
    A fetch that fails is a "no", not a "yes": generate_release_pdfs()
    pulls the same OFP from the same API a moment later, so waving it
    through on a network hiccup wouldn't rescue the generation anyway —
    it would only reopen the leak this check exists to close."""
    try:
        ofp_fields = _cached_ofp_fields(simbrief_user)
    except Exception as e:
        LOG.warning(f"SimBrief OFP identity check failed for {simbrief_user}: {e}")
        return False, f"couldn't reach SimBrief to confirm which flight is on the account: {e}"
    if not ofp_fields:
        return False, "no OFP on that SimBrief account yet"
    if (
        ofp_fields.get("flight_number") == record.get("flight_number")
        and _airport_icao(ofp_fields.get("origin", "")) == _airport_icao(record.get("origin", ""))
        and _airport_icao(ofp_fields.get("destination", "")) == _airport_icao(record.get("destination", ""))
    ):
        return True, ""
    on_account = "{} {}-{}".format(
        ofp_fields.get("flight_number") or "?",
        ofp_fields.get("origin") or "?", ofp_fields.get("destination") or "?",
    )
    return False, (
        f"SimBrief currently holds {on_account}, not this flight — send this leg "
        "to SimBrief first, then generate its release"
    )


# ---------------------------------------------------------------------------
# Flight-deck messages
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rescheduling messages
#
# Modelled on how a real ECS reschedule notification is structured (APFA
# Contract Implementation Update #58, 25 APR 2026): a disruption produces
# TWO messages, not one. The first acknowledges the disruption and states
# how long the crew must stay contactable; the second carries the repaired
# sequence once it exists.
#
# Times are Sequence Base Time — the timezone of the base the sequence
# originated from — which is what the real notification uses and what a
# crew member reads their own bid pack in.
# ---------------------------------------------------------------------------

# CBA 10.J.3.d — disrupted after report, before the sequence originates.
# Dropping a cancelled or delayed leg frees the duty day enough that the
# search can return dozens of legal paths — more than anyone scrolls, and
# each one costs a trial apply to price. Both tiers are already sorted
# best-first, so the tail is what gets cut.
_REC_SHOW_MAX = 8
_REC_TRY_MAX = 24

_REPAIR_AFTER_REPORT_H = 4.0
_REPAIR_AFTER_DISRUPTION_H = 3.0


def _hhmm_add(hhmm, hours):
    """"1445" + 4.0 -> "1845", wrapping past midnight. Returns "" on junk
    rather than raising: a message is worth sending even if one of its
    times cannot be computed."""
    try:
        raw = str(hhmm).strip()
        total = (int(raw[:2]) * 60 + int(raw[2:4])) + int(round(hours * 60))
    except (ValueError, IndexError, TypeError):
        return ""
    total %= 24 * 60
    return f"{total // 60:02d}{total % 60:02d}"


def _hhmm_to_min(hhmm):
    try:
        raw = str(hhmm).strip()
        return int(raw[:2]) * 60 + int(raw[2:4])
    except (ValueError, IndexError, TypeError):
        return None


def _repair_window_end(disruption_hhmm, report_hhmm, originated):
    """When Crew Scheduling's window to repair the sequence closes.

    Before origination it is the LATER of report+4h and disruption+3h —
    "whichever is later" in the CBA, so a disruption early in a duty day
    does not shorten the window. After origination it is disruption+3h.

    Both candidates are measured as minutes AFTER REPORT, not as clock
    faces and not relative to the disruption. Report is the one fixed point
    the duty day hangs off, and everything after it runs forward from
    there. Comparing any other way mis-ranks the pair as soon as one of
    them crosses midnight, or whenever report+4h lands before the
    disruption itself — a 0500 report disrupted at 1445 has a window to
    1745, not to 0900.
    """
    d = _hhmm_to_min(disruption_hhmm)
    if d is None:
        return ""
    if originated or not report_hhmm:
        return f"{((d + 180) % 1440) // 60:02d}{((d + 180) % 1440) % 60:02d}"
    r = _hhmm_to_min(report_hhmm)
    if r is None:
        return f"{((d + 180) % 1440) // 60:02d}{((d + 180) % 1440) % 60:02d}"
    # Minutes from report to the disruption, wrapped forward: a duty day
    # can start before midnight and be disrupted after it.
    d_off = (d - r) % 1440
    end_off = max(int(_REPAIR_AFTER_REPORT_H * 60), d_off + int(_REPAIR_AFTER_DISRUPTION_H * 60))
    end = (r + end_off) % 1440
    return f"{end // 60:02d}{end % 60:02d}"


_DISRUPTION_LABELS = {
    "late_departure": "LATE DEPARTURE",
    "late_arrival": "LATE ARRIVAL",
    "diverted": "DIVERTED",
    "cancelled": "CANCELLED",
}


def _candidate_days(new_seq, from_day):
    """The legs an accepted option would actually give you, from the
    disrupted day onward — days before it are untouched, so listing them
    back would only bury the part that changed."""
    out = []
    for day in (new_seq.get("duty_days") or []):
        if day.get("duty_day", 0) < from_day:
            continue
        out.append({
            "duty_day": day.get("duty_day"),
            "report": day.get("report") or "",
            "release": day.get("release") or "",
            "legs": [{
                "flight_number": l.get("flight_number") or "",
                "origin": l.get("origin") or "",
                "destination": l.get("destination") or "",
                "dep_local": l.get("dep_local") or "",
                "arr_local": l.get("arr_local") or "",
                "equipment": l.get("equipment") or "",
                "deadhead": bool(l.get("deadhead")),
            } for l in (day.get("legs") or [])],
        })
    return out


def _disruption_message(record, seq_number, duty_day, leg, kind, at_hhmm,
                        report_hhmm, originated):
    """The first message: what happened, and how long to stay contactable."""
    window = _repair_window_end(at_hhmm, report_hhmm, originated)
    leg_txt = f"{(leg or {}).get('origin','?')}-{(leg or {}).get('destination','?')}"
    lines = [
        f"{_msg_prefix(record)} [DISRUPTION] SEQ {seq_number}",
        f"TYPE   {_DISRUPTION_LABELS.get(kind, str(kind).upper())}",
        f"LEG    {leg_txt}  DAY {duty_day}",
        f"START  {at_hhmm}L at {(leg or {}).get('origin', '?')}",
    ]
    if window:
        lines.append(f"REMAIN CONTACTABLE UNTIL {window}L")
    lines.append("STATUS RELEASED PENDING REPAIR")
    return "\n".join(lines)


def _reassignment_message(record, seq_number, kind, days, position):
    """The second message: the repaired sequence itself.

    Field order follows the real notification — flight number, date,
    departure, city pair, position, and 'dhd' on a deadhead — with duty
    days separated by a blank line, which is how a crew member scans it.
    """
    # No name. The real notification does not carry one -- it is delivered
    # to one crew member's own device, so addressing them by name says
    # nothing the header does not, and it would need a crew-name field the
    # account does not have.
    #
    # Position IS in the spec. Our packs carry one position for the whole
    # sequence rather than per leg, so it is stated once here instead of
    # repeated on every line.
    lines = [
        f"{_msg_prefix(record)} [REASSIGNED] SEQ {seq_number}",
        "YOU HAVE BEEN REASSIGNED",
        f"TYPE   {_DISRUPTION_LABELS.get(kind, str(kind).upper())}",
    ]
    pos = (position or "").strip().upper()
    if pos:
        lines.append(f"POS    {pos}")
    lines.append("NEW PAIRING IS AS FOLLOWS:")
    for day in days or []:
        lines.append("")
        lines.append(f"DAY {day.get('duty_day', '?')}")
        for leg in day.get("legs") or []:
            dhd = "  dhd" if leg.get("deadhead") else ""
            lines.append(
                "  {:<6} {:<5} {}-{}{}".format(
                    leg.get("flight_number") or "----",
                    leg.get("dep_local") or "----",
                    leg.get("origin") or "???",
                    leg.get("destination") or "???",
                    dhd,
                )
            )
    lines.append("")
    lines.append("STATUS REPAIRED")
    return "\n".join(lines)


def _ofp_time_generated(user_id):
    """SimBrief's own timestamp for the OFP currently on an account, or "".
    Never raises: this is recorded alongside a release that has already been
    built, and failing to label it must not throw that work away."""
    try:
        return str(simbrief_ofp.fetch_ofp_generated_at(user_id) or "")
    except Exception as e:
        LOG.warning(f"Could not read SimBrief generation time for {user_id}: {e}")
        return ""


def _msg_prefix(record):
    """"FLT 1234 31AUG" — the flight this message is about, in the form a
    crew reads it off a release."""
    flt = (record.get("flight_number") or "").strip() or "----"
    raw = (record.get("dep_date") or record.get("date") or "").strip()
    day = ""
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%d%b%y", "%d%b"):
        try:
            day = datetime.strptime(raw, fmt).strftime("%d%b").upper()
            break
        except (ValueError, TypeError):
            continue
    if not day:
        day = raw.upper()
    return f"FLT {flt} {day}".strip()


def _add_message(user_id, kind, body, dedupe, record=None, leg_id=None):
    """Writes one message unless one already carries the same dedupe key.

    This is what makes it safe to call from the 3-minute sync tick. The
    check deliberately counts ACKNOWLEDGED messages too: an earlier version
    ignored them, which meant acknowledging a drift got it re-posted on the
    very next tick and every three minutes after that. A condition the
    pilot has already seen and dealt with must stay quiet.

    Saying something new therefore requires a new key, which is why the
    drift key includes the flight SimBrief actually holds — drifting to a
    *different* flight is a different key and does speak up."""
    existing = Message.query.filter_by(user_id=user_id, dedupe=dedupe).first()
    if existing:
        return None
    record = record or {}
    msg = Message(
        user_id=user_id, leg_id=leg_id, kind=kind,
        flight_number=(record.get("flight_number") or "")[:16],
        dep_date=(record.get("dep_date") or "")[:16],
        body=body, dedupe=dedupe[:200],
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def _unacked_message_count():
    try:
        return Message.query.filter_by(user_id=current_user.id, acknowledged_at=None).count()
    except Exception:
        return 0


@app.route("/messages")
def list_messages():
    rows = (Message.query.filter_by(user_id=current_user.id)
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(100).all())
    return jsonify({
        "unacknowledged": _unacked_message_count(),
        "messages": [{
            "id": m.id, "kind": m.kind, "body": m.body,
            "leg_id": m.leg_id, "flight_number": m.flight_number,
            "created_at": _iso(m.created_at), "acknowledged_at": m.acknowledged_at,
        } for m in rows],
    })


@app.route("/messages/<int:msg_id>/ack", methods=["POST"])
def ack_message(msg_id):
    m = Message.query.filter_by(id=msg_id, user_id=current_user.id).first()
    if not m:
        return jsonify({"error": "not found"}), 404
    if not m.acknowledged_at:
        m.acknowledged_at = datetime.now(timezone.utc).isoformat()
        db.session.commit()
    return jsonify({"ok": True, "unacknowledged": _unacked_message_count()})


@app.route("/messages/check", methods=["POST"])
def check_messages():
    """Called from the auto-sync tick. Asks one question: does the release
    we hold for this leg still describe what SimBrief has on the account?

    The FIRST message for a leg is posted when its release is generated
    ("[NEW] RLS AVAILABLE" — see generate_release). Everything after that
    is drift: the pilot re-dispatched on SimBrief and what is in the app is
    now stale. Nothing is posted for a leg with no release, because there
    is nothing yet to be out of date.
    """
    body = request.get_json(silent=True) or {}
    try:
        leg_id = int(body.get("leg_id") or 0)
    except (TypeError, ValueError):
        leg_id = 0
    if not leg_id:
        return jsonify({"unacknowledged": _unacked_message_count()})
    record = _get_leg(leg_id)
    if not record:
        return jsonify({"unacknowledged": _unacked_message_count()})
    if not ReleaseCache.query.filter_by(leg_id=leg_id).first():
        return jsonify({"unacknowledged": _unacked_message_count()})

    simbrief_user = (body.get("simbrief_user") or "").strip() or current_user.default_simbrief_user
    if not simbrief_user:
        return jsonify({"unacknowledged": _unacked_message_count()})

    matches, _why = _ofp_is_this_leg(simbrief_user, record)
    if not matches:
        fields = {}
        try:
            fields = _cached_ofp_fields(simbrief_user) or {}
        except Exception:
            fields = {}
        on_account = "{} {}-{}".format(
            (fields.get("flight_number") or "?"),
            (fields.get("origin") or "?"), (fields.get("destination") or "?"))
        _add_message(
            current_user.id, "rls_drift",
            f"{_msg_prefix(record)} [UPD] RLS DIFFERS FROM SIMBRIEF - NOW {on_account}",
            # Keyed on what SimBrief holds, so drifting to a different
            # flight later is a NEW message rather than a silenced repeat.
            f"rls-drift:{leg_id}:{on_account}", record=record, leg_id=leg_id)
    return jsonify({"unacknowledged": _unacked_message_count()})


@app.route("/fos/<int:leg_id>/release", methods=["POST"])
def generate_release(leg_id):
    """Generates (or, by far the common case, just returns) this leg's
    release PDFs. generate_release_pdfs() is a real SimBrief OFP fetch +
    PDF render that takes up to a minute — both the Confirm view's
    "Generate Release" button and the Documents PDF viewer hit this same
    route/cache now, instead of each independently re-running it on every
    click or page load. Pass {"force": true} to regenerate anyway (e.g.
    gates changed since the cached copy, or the weather's gone stale).

    Two things scope this to the leg in the URL, and both matter — the
    reported "a leg with nothing generated shows another leg's OFP and
    documents" bug was this route having neither:

      cached_only — read-only. Serves this leg's cached release or 404s.
        The Documents view (viewDoc -> ensureRelease) uses it, because
        merely LOOKING at a leg's document list must never mint a release
        for it. There is deliberately no "most recent release" fallback:
        a leg with nothing generated has nothing to show, full stop.
      identity check — generate_release_pdfs() renders whatever OFP is on
        the pilot's SimBrief ACCOUNT; it takes a username, not a leg, and
        cannot tell one leg from another. Generating without first
        confirming the account actually holds THIS flight is what wrote
        another leg's release into this leg's cache."""
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
    if body.get("cached_only"):
        return jsonify({"error": "no release has been generated for this flight yet"}), 404

    if not release_engine.is_available():
        return jsonify({"error": release_engine.import_error()}), 503

    user_id = body.get("user_id") or os.environ.get("SIMBRIEF_USER")
    if not user_id:
        return jsonify({"error": "no SimBrief user id — pass \"user_id\" or set SIMBRIEF_USER"}), 400

    matches, why_not = _ofp_is_this_leg(user_id, record)
    if not matches:
        return jsonify({"error": why_not}), 409

    # The digit after the point in the page header's "RELEASE 6.7": how many
    # times this leg has been generated, wrapping 9 -> 0. Kept in the cached
    # payload rather than its own column — it is one small int that belongs
    # to exactly the row already being written, so it needs no migration.
    # First generation is 0, which is what the header printed before this
    # existed, so an unregenerated release reads the same as it always did.
    generation = ((cached.payload.get("generation", -1) + 1) % 10) if cached else 0
    try:
        rls_bytes, wb_bytes, filename = release_engine.generate_release_pdfs(
            user_id, gate=record.get("dep_gate", ""), arr_gate=record.get("arr_gate", ""),
            generation=generation)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    payload = {
        "filename": filename,
        "generation": generation,
        # SimBrief's own generation timestamp for the OFP this was built
        # from. Our generated_at is a wall clock and says nothing about
        # whether the PLAN changed; this is what Refresh OFP compares.
        "ofp_time_generated": _ofp_time_generated(user_id),
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

    _add_message(
        current_user.id, "rls_new",
        f"{_msg_prefix(record)} [NEW] RLS AVAILABLE",
        f"rls-new:{leg_id}", record=record, leg_id=leg_id)
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


def _trip_signin_banner():
    """Home's "you haven't signed in to your trip yet" call-to-action.

    Pick Up (POST /pbs/sequences/<seq>/pick-up) IS the trip check-in, but it
    only ever existed as one button inside a sequence's detail view — three
    taps deep, under a name that doesn't say "sign in". Pilots couldn't find
    where to sign in to their trip. This puts it where it can't be missed on
    the one screen everyone starts from, and only while there's actually
    something to sign in to: a sequence in the pool and no active trip.
    Rendered server-side (like Current Flight above) rather than fetched —
    the server already knows both halves of that.

    Deliberately a link to Schedule, not a one-tap check-in from Home: Pick
    Up is a real logged event (TripCheckIn), and the pilot should see which
    trip they're signing in to before it happens."""
    if current_user.active_seq:
        return ""
    seqs = _pbs_sequences()
    if not seqs:
        return ""
    if len(seqs) == 1:
        s = _summarize_sequence(seqs[0])
        routing = f'{s.get("origin") or ""}→{s.get("final_destination") or ""}'.strip("→")
        detail = f'SEQ {s["seq"]}' + (f' · {routing}' if routing else "") + f' · {s["days"]} day{"" if s["days"] == 1 else "s"}'
    else:
        detail = f"{len(seqs)} trips in My Trips · pick the one you're flying"
    return (
        '<div class="trip-signin-banner" onclick="window.location.href=\'/schedule\'">'
        '<div class="tsb-title">Sign in to your trip</div>'
        f'<div class="tsb-sub">{html.escape(detail)}</div>'
        '</div>'
    )


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
        unacked_docs=str(_unacked_doc_count()),
        trip_signin_banner=_trip_signin_banner(),
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


def _hhmmz(iso_str):
    """'HH:MMZ' from a stored *_signed_at ISO8601 UTC timestamp."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    return dt.strftime("%H:%M") + "Z"


def _sign_row_html(ctx, kind, elem_id, unsigned_desc, signed):
    """Renders one FFD/EFLIGHT PLAN doc-row's desc + primary action —
    "Signed by <user> at HH:MMZ" plus a Re-sign link once signed, instead
    of just leaving a checkmark as the only trace of who actually signed
    and when. Falls back to a plain "Signed" (no by-line) for legs signed
    before this per-kind signed_by/signed_at existed."""
    signed_at = ctx.get(f"{kind}_signed_at")
    signed_by = ctx.get(f"{kind}_signed_by")
    check_svg = (
        f'<svg id="{elem_id}" class="check{" signed" if signed else ""}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" '
        f'onclick="openSignPad(\'{kind}\')"><path d="M20 6L9 17l-5-5"/></svg>'
    )
    if not signed:
        return html.escape(unsigned_desc), check_svg
    if signed_by and signed_at:
        desc_html = f'Signed by <b>{html.escape(signed_by)}</b> at <b>{_hhmmz(signed_at)}</b>'
    elif signed_at:
        desc_html = f'Signed at <b>{_hhmmz(signed_at)}</b>'
    else:
        desc_html = 'Signed'
    action_html = check_svg + '<a href="#" class="resign-link" onclick="openSignPad(\'' + kind + '\');return false;">Re-sign</a>'
    return desc_html, action_html


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


def render_fos_html(leg, default_view=""):
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
        if ctx.get("eflightplan_signed_at"):
            ctx["status"] = "Released & Signed"
        elif ctx.get("signed_in") and ctx.get("fit_for_duty"):
            ctx["status"] = "Ready for Departure"
        elif ctx.get("signed_in") or ctx.get("fit_for_duty"):
            ctx["status"] = "Checking In"
        else:
            ctx["status"] = "Scheduled"
    ctx["signed_in_class"] = "" if ctx.get("signed_in") else "inactive"
    ctx["ffd_class"] = "" if ctx.get("fit_for_duty") else "inactive"
    # FFD and eFlight Plan each get their own doc-row desc/action markup —
    # a plain checkmark was the only trace that either had been signed at
    # all, with no record of who or when visible anywhere on the row. Once
    # signed, the row shows "Signed by <user> at HH:MMZ" plus an explicit
    # Re-sign action instead of just relying on the check going green.
    ctx["ffd_desc_html"], ctx["ffd_action_html"] = _sign_row_html(
        ctx, "ffd", "ffd-doc-check", "Fit for Duty Declaration", bool(ctx.get("fit_for_duty")))
    ctx["eflightplan_desc_html"], ctx["eflightplan_action_html"] = _sign_row_html(
        ctx, "eflightplan", "sign-check", "eFlight Plan", bool(ctx.get("eflightplan_signed_at")))
    # Raw user-typed account settings, not OFP/PBS data — escaped before
    # landing in an HTML attribute since, unlike the rest of ctx, a pilot
    # can type anything here.
    ctx["default_simbrief_user"] = html.escape(current_user.default_simbrief_user or "")
    ctx["aeroapi_key"] = html.escape(current_user.aeroapi_key or "")
    ctx["app_version"] = APP_VERSION
    # Embedded as JSON so the AeroAPI panel can paint itself on first load
    # from what's already on the leg. json.dumps also makes this safe to
    # drop straight into a <script>: the value is inserted by
    # safe_substitute AFTER the template is scanned, so a $ inside it is
    # never treated as a placeholder.
    # The "</" split is not cosmetic: a literal </script> anywhere inside
    # this JSON ends the <script> block early and silently kills every
    # function after it. "<\\/" is a legal JSON escape for "/", so the
    # parsed value is unchanged. ensure_ascii (the default) already takes
    # care of U+2028/U+2029, the other two characters that can break out.
    ctx["aero_suggestion_json"] = json.dumps(
        ctx.get("aero_suggestion") or None).replace("</", "<\\/")
    ctx["mot_display"] = _mot_display(ctx.get("mot") or "", ctx.get("origin") or "", current_user.timezone)
    ctx["timezone_options"] = _timezone_options_html(current_user.timezone)
    ctx["default_view"] = default_view
    ctx["is_admin"] = "1" if bool(getattr(current_user, "is_admin", False)) else ""
    # Server-rendered rather than fetched, so the banner is on screen with
    # the first paint instead of appearing a moment later on every page.
    ctx["unacked_docs"] = str(_unacked_doc_count())
    _SIG_IMAGE_KEYS = {"signature", "ffd_signature", "eflightplan_signature"}
    str_ctx = {k: ("" if v is None else str(v)) for k, v in ctx.items() if k not in _SIG_IMAGE_KEYS}
    return Template(FOS_TEMPLATE).safe_substitute(**str_ctx)


LAUNCHER_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<script>(function(){var t=localStorage.getItem('fos_theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);})();</script>
<!-- No viewport-fit=cover. That is what makes the layout viewport span
     the WHOLE screen, status bar included, and it is the actual reason
     content sat under the frosted bar — the status-bar style only
     decides how iOS paints over whatever is up there. Without it the
     web view is laid out inside the safe area, so nothing is beneath
     the bar to blur. It also means env(safe-area-inset-*) reports 0,
     which every use here already handles by adding it to something. -->
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- ONE tag, no media attribute, and it is never removed or replaced —
     only its content is rewritten. iOS samples theme-color when a
     home-screen app launches; a tag that script deletes and recreates
     can leave it with no colour at all, at which point it falls back to
     its own material and the status bar reads as frosted rather than a
     flat colour matching the app. -->
<meta name="theme-color" id="theme-color-tag" content="#f5f5f7">
<script>
// theme-color has to follow the theme the app is ACTUALLY showing. The two
// media-keyed tags above only track the OS, and this app has its own
// Light/Auto/Dark override — so with the OS light and the app set to dark,
// iOS painted the status strip #f5f5f7 over a black page, and vice versa.
// Resolving it here to a single tag removes that whole class of mismatch.
function _syncThemeColor(){
  var t = localStorage.getItem('fos_theme');
  var dark = (t === 'dark') || (t !== 'light' &&
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var m = document.getElementById('theme-color-tag');
  if (m) m.setAttribute('content', dark ? '#000000' : '#f5f5f7');
}
_syncThemeColor();
if (window.matchMedia) {
  try { window.matchMedia('(prefers-color-scheme: dark)')
          .addEventListener('change', _syncThemeColor); } catch (e) {}
}
</script>

<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<!-- NOT black-translucent. That asks iOS to draw its status bar OVER the
     page, which is what put a frosted band across the top of every
     screen. "default" makes iOS reserve the space instead, so the app
     starts below the bar and nothing is drawn on top of it. Paired with
     --status-bar-min below, which drops to 0 because there is no longer
     an overlay to sit clear of. -->
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<script>
// How much of the top of the window iOS is covering, which is NOT the same
// as env(safe-area-inset-top).
//
// An iPad has no notch, so that inset is 0 — including in landscape, and
// including when the app is installed to the home screen. But
// apple-mobile-web-app-status-bar-style=black-translucent means iOS still
// draws its translucent status bar OVER the page there. Trusting env()
// alone is why the acknowledgement banner kept ending up underneath it: the
// padding computed to 11px against a ~24pt overlay.
//
// So: take the inset when there is one (a notched phone reports 44-59), and
// otherwise reserve a status bar's worth, but only in standalone — in a
// normal browser tab nothing is overlaid and reserving space would just
// waste it. Every top offset in the stylesheet reads this one value.
(function(){
  var standalone = window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  var root = document.documentElement;
  // Reserved again, and this time not on a theory about which meta tag
  // controls the overlay. Observed behaviour on iOS 26 is that a
  // home-screen web app's content sits under the status bar regardless of
  // apple-mobile-web-app-status-bar-style or viewport-fit, so the only
  // reliable fix is for the page to keep its own content out of that band.
  // .topbar's padding absorbs it, which leaves the strip painted in
  // .topbar's own flat background — the solid colour-matched bar other PWAs
  // show, rather than the system blurring our content through it.
  root.style.setProperty('--status-bar-min', standalone ? '24px' : '0px');
  root.style.setProperty('--status-bar',
    'max(env(safe-area-inset-top), var(--status-bar-min, 0px))');
})();
</script>
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/icon-192.png">
<title>MobileCCI</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#fff; --border:#d2d2d7; --label:#6e6e73; --value:#1d1d1f;
    --blue:#0071e3; --blue-dark:#0058a8; --red:#ff3b30; --green:#34c759; --inactive:#9aa1ab;
    /* iOS was falling all the way through to the generic monospace,
       which is Courier: ui-monospace is not honoured everywhere and a
       bare "Menlo" does not match on iOS. SFMono-Regular is the name
       Safari actually resolves, so it goes first among the real
       families and Courier stays what it should be — the last
       resort. */
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
            "Liberation Mono", "Courier New", monospace;
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
  /* Same treatment as the FOS template's copy: pinned, and it owns the
     safe-area inset because it is the topmost element. */
  /* Blocking, not advisory: opaque rather than the loading overlay's 50%,
     because there is nothing behind it worth reading until the documents
     are acknowledged. Same top/bottom geometry though, so the header and
     the tab bar stay visible and tappable. z-index 17 puts it over the
     loading overlay (15) — a load finishing must not reveal the app. */
  #doc-lock{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);
    z-index:17;display:flex;align-items:center;justify-content:center;padding:24px;}
  #doc-lock[hidden]{display:none;}
  .dl-scrim{position:absolute;inset:0;background:var(--bg);}
  .dl-card{position:relative;max-width:420px;text-align:center;display:flex;
    flex-direction:column;align-items:center;gap:14px;}
  .dl-mark{width:76px;height:76px;background:var(--nac-red);display:flex;align-items:center;
    justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.18);flex-shrink:0;}
  .dl-mark img{width:56px;height:56px;object-fit:contain;}
  .dl-card h2{margin:0;font-size:19px;font-weight:700;color:var(--value);}
  .dl-card p{margin:0;font-size:14px;line-height:1.45;color:var(--label);}
  .dl-card button{margin:4px 0 0;padding:12px 22px;font-size:15px;font-weight:600;
    background:var(--blue);color:#fff;border:none;border-radius:7px;cursor:pointer;}
  .settings-fab{position:fixed;top:calc(var(--ack-h,0px) + var(--safe-top, var(--status-bar, 0px)) + var(--topbar-lead, 14px));right:calc(env(safe-area-inset-right) + 16px);z-index:25;background:var(--card);border:1px solid var(--border);border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;color:var(--blue-dark);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);}
  .settings-fab svg{width:18px;height:18px;flex-shrink:0;}
  h1{font-size:18px;color:var(--blue-dark);margin:0 0 16px;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:var(--value);}
  textarea, select, input[type=text]{width:100%;max-width:640px;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid var(--border);border-radius:5px;box-sizing:border-box;background:var(--card);color:var(--value);}
  textarea{height:160px;font-family:var(--mono);font-size:12.5px;}
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
  /* Trip check-in call-to-action — blue, not the doc-ack banner's red: this
     is "here's the thing you're looking for", not a compliance warning. */
  .trip-signin-banner{max-width:640px;background:var(--blue);color:#fff;border-radius:10px;padding:14px 16px;margin:0 0 14px;cursor:pointer;}
  .trip-signin-banner .tsb-title{font-size:15px;font-weight:700;}
  .trip-signin-banner .tsb-sub{font-size:12.5px;opacity:.9;margin-top:3px;}
</style></head><body>
<!-- Styled inline rather than via .ffd-banner: that class lives only in
     FOS_TEMPLATE's stylesheet, and this template has its own. -->
<div id="doc-lock" hidden role="alertdialog" aria-labelledby="doc-lock-title-home">
  <div class="dl-scrim"></div>
  <div class="dl-card">
    <div class="dl-mark"><img src="/static/nac-bear.png" alt="" onerror="this.remove()"></div>
    <h2 id="doc-lock-title-home">Acknowledgement Required</h2>
    <p id="doc-lock-body"></p>
    <button type="button" onclick="window.location.href='/docs'">Review Documents</button>
  </div>
</div>

<button class="settings-fab" title="Settings" onclick="window.location.href='/schedule?view=settings'">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1 1.55V21a2 2 0 11-4 0v-.09A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.6 15a1.7 1.7 0 00-1.55-1H3a2 2 0 110-4h.09A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.6a1.7 1.7 0 001-1.55V3a2 2 0 114 0v.09a1.7 1.7 0 001 1.55 1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.55 1H21a2 2 0 110 4h-.09a1.7 1.7 0 00-1.55 1z"/></svg>
</button>
<div id="home-view" class="sub-view active">
  <h1>MobileCCI</h1>
  $trip_signin_banner
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
  <button class="navtab" id="tab-docs" onclick="window.location.href='/docs'">
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
// Company documents need acknowledging whether or not a leg is loaded, so
// Home carries the same lock (FOS_TEMPLATE renders its own copy). There is
// no exempt view here — Docs is a page of its own, and the button goes to
// it, so the lock always has an exit.
function _positionDocLock(){
  const el = document.getElementById('doc-lock');
  if(!el || el.hidden) return;
  const topbar = document.querySelector('.view.active .topbar') || document.querySelector('.topbar');
  const tabbar = document.querySelector('.tabbar');
  el.style.top = Math.max(0, topbar ? topbar.getBoundingClientRect().bottom : 0) + 'px';
  el.style.bottom = (tabbar ? tabbar.getBoundingClientRect().height : 0) + 'px';
}
(function(){
  const n = parseInt("$unacked_docs", 10) || 0;
  if(!n) return;
  const el = document.getElementById('doc-lock');
  if(!el) return;
  document.getElementById('doc-lock-body').textContent =
    n + ' company document' + (n === 1 ? '' : 's') + ' need' + (n === 1 ? 's' : '') +
    ' your acknowledgement before you can use the app.';
  el.hidden = false;
  _positionDocLock();
  window.addEventListener('resize', _positionDocLock);
  window.addEventListener('scroll', _positionDocLock, {passive: true});
})();
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

// Inline SVG rather than literal star/cross/triangle characters. A glyph
// renders in the system font — it ignores currentColor, so it cannot
// follow the theme, and it sits on a different baseline from the text
// beside it.
const ACTIVE_DOT_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" style="width:9px;height:9px;vertical-align:1px;color:var(--green);"><circle cx="12" cy="12" r="7"/></svg>';
function _escHtml(v){ return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
const STAR_ICON_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px;vertical-align:-2px;"><path d="M12 2.6l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5-5.8-3.1-5.8 3.1 1.1-6.5L2.6 9.4l6.5-.9z"/></svg>';
const X_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" style="width:11px;height:11px;"><path d="M6 6l12 12M18 6L6 18"/></svg>';
const CHEVRON_UP_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:11px;height:11px;"><path d="M6 15l6-6 6 6"/></svg>';
const CHEVRON_DOWN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:11px;height:11px;"><path d="M6 9l6 6 6-6"/></svg>';
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
    heading.textContent = 'Day ' + _dayCalendarNumber(day) + ' — RPT ' + (day.report || '');
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
<!-- No viewport-fit=cover. That is what makes the layout viewport span
     the WHOLE screen, status bar included, and it is the actual reason
     content sat under the frosted bar — the status-bar style only
     decides how iOS paints over whatever is up there. Without it the
     web view is laid out inside the safe area, so nothing is beneath
     the bar to blur. It also means env(safe-area-inset-*) reports 0,
     which every use here already handles by adding it to something. -->
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<!-- ONE tag, no media attribute, and it is never removed or replaced —
     only its content is rewritten. iOS samples theme-color when a
     home-screen app launches; a tag that script deletes and recreates
     can leave it with no colour at all, at which point it falls back to
     its own material and the status bar reads as frosted rather than a
     flat colour matching the app. -->
<meta name="theme-color" id="theme-color-tag" content="#f5f5f7">
<script>
// theme-color has to follow the theme the app is ACTUALLY showing. The two
// media-keyed tags above only track the OS, and this app has its own
// Light/Auto/Dark override — so with the OS light and the app set to dark,
// iOS painted the status strip #f5f5f7 over a black page, and vice versa.
// Resolving it here to a single tag removes that whole class of mismatch.
function _syncThemeColor(){
  var t = localStorage.getItem('fos_theme');
  var dark = (t === 'dark') || (t !== 'light' &&
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var m = document.getElementById('theme-color-tag');
  if (m) m.setAttribute('content', dark ? '#000000' : '#f5f5f7');
}
_syncThemeColor();
if (window.matchMedia) {
  try { window.matchMedia('(prefers-color-scheme: dark)')
          .addEventListener('change', _syncThemeColor); } catch (e) {}
}
</script>

<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<!-- NOT black-translucent. That asks iOS to draw its status bar OVER the
     page, which is what put a frosted band across the top of every
     screen. "default" makes iOS reserve the space instead, so the app
     starts below the bar and nothing is drawn on top of it. Paired with
     --status-bar-min below, which drops to 0 because there is no longer
     an overlay to sit clear of. -->
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="MobileCCI">
<script>
// How much of the top of the window iOS is covering, which is NOT the same
// as env(safe-area-inset-top).
//
// An iPad has no notch, so that inset is 0 — including in landscape, and
// including when the app is installed to the home screen. But
// apple-mobile-web-app-status-bar-style=black-translucent means iOS still
// draws its translucent status bar OVER the page there. Trusting env()
// alone is why the acknowledgement banner kept ending up underneath it: the
// padding computed to 11px against a ~24pt overlay.
//
// So: take the inset when there is one (a notched phone reports 44-59), and
// otherwise reserve a status bar's worth, but only in standalone — in a
// normal browser tab nothing is overlaid and reserving space would just
// waste it. Every top offset in the stylesheet reads this one value.
(function(){
  var standalone = window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  var root = document.documentElement;
  // Reserved again, and this time not on a theory about which meta tag
  // controls the overlay. Observed behaviour on iOS 26 is that a
  // home-screen web app's content sits under the status bar regardless of
  // apple-mobile-web-app-status-bar-style or viewport-fit, so the only
  // reliable fix is for the page to keep its own content out of that band.
  // .topbar's padding absorbs it, which leaves the strip painted in
  // .topbar's own flat background — the solid colour-matched bar other PWAs
  // show, rather than the system blurring our content through it.
  root.style.setProperty('--status-bar-min', standalone ? '24px' : '0px');
  root.style.setProperty('--status-bar',
    'max(env(safe-area-inset-top), var(--status-bar-min, 0px))');
})();
</script>
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
    /* iOS was falling all the way through to the generic monospace,
       which is Courier: ui-monospace is not honoured everywhere and a
       bare "Menlo" does not match on iOS. SFMono-Regular is the name
       Safari actually resolves, so it goes first among the real
       families and Courier stays what it should be — the last
       resort. */
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
            "Liberation Mono", "Courier New", monospace;
    /* NAC's brand maroon, sampled from static/nac-bear.png. Distinct from
       --red, which is the UI ALERT red and has nothing to do with the
       livery -- keep the two apart. */
    --nac-red:#640019;
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
  .topbar{display:flex;flex-wrap:wrap;align-items:center;margin-bottom:10px;position:sticky;top:var(--ack-h,0px);z-index:10;background:var(--bg);padding-top:calc(var(--safe-top, var(--status-bar, 0px)) + var(--topbar-lead, 14px));margin-top:-14px;margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px;}
  /* Tablet-width browsers (iPadOS Safari's tabbed mode among them) draw
     their own chrome — tab-strip controls, a floating "stoplight" cluster
     — over the top-left/top-right of the page that safe-area-inset can't
     account for (it only reports hardware notch/home-indicator, not
     browser UI). Extra clearance here is a defensive guess, not measured
     against a real device — right height still needs confirming there. */
  /* --status-bar now guarantees clearance of the overlaid iOS status bar,
     so the 42px this used to carry on tablets was doing that job twice
     and made the header noticeably tall. The lead is just breathing
     room now, and 18px is enough of it. */
  @media (min-width: 768px){ :root{ --topbar-lead:18px; } }
  .back-link{order:1;display:flex;align-items:center;color:var(--value);background:none;border:none;cursor:pointer;padding:6px 4px;text-decoration:none;}
  .topbar{position:sticky;}
  .topbar-actions{order:3;display:flex;align-items:center;gap:14px;padding-right:38px;}
  #pdf-view .topbar{position:sticky;}
  #pdf-view .topbar-actions{gap:2px;}
  /* The title used to be flex:1 1 100%, which forced it onto a SECOND row
     under the back arrow and the actions and made the header about 30px
     taller than it needed to be. It now shares the row, taking the space
     between them; min-width:0 lets a long title ellipsise rather than
     pushing the actions off the edge, and .topbar still wraps if a really
     narrow screen leaves it no room. */
  .topbar-title{order:2;flex:1 1 auto;min-width:0;text-align:center;}
  .topbar-title h1{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .topbar-title h1{font-size:19px;margin:0;font-weight:600;color:var(--blue-dark);}
  .topbar-title p{font-size:12px;margin:2px 0 0;color:var(--label);}
  /* Same blue as the PDF viewer's Download/Print/Share, so every icon
     action in a header reads as the same kind of control. */
  .icon-btn{background:none;border:none;color:var(--blue-dark);cursor:pointer;padding:2px;display:flex;}
  .icon-btn svg{width:19px;height:19px;}
  .icon-btn.syncing svg{animation:spin .8s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  /* One settings gear, fixed above every view (not per-topbar) — the only
     way to guarantee it's on every single page without duplicating it
     into all 14 view sections. Sits clear of the topbar's own
     actions/title since it's positioned independently. */
  .settings-fab{position:fixed;top:calc(var(--ack-h,0px) + var(--safe-top, var(--status-bar, 0px)) + var(--topbar-lead, 14px));right:calc(env(safe-area-inset-right) + 16px);z-index:25;background:var(--card);border:1px solid var(--border);border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;color:var(--blue-dark);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);}
  .settings-fab svg{width:18px;height:18px;flex-shrink:0;}
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
  /* The doc-ack banner sits ABOVE .topbar in the document and used to be
     static, so it scrolled away and came back — which reads as the header
     growing and shrinking as you scroll. Pinning it keeps the header one
     constant height, and keeps an unacknowledged-documents notice on screen
     rather than only when you happen to be at the top.
     It also has to outrank the loading overlay (15), or a load buries the
     notice behind the translucent scrim. */
  /* Topmost element on the page, so it owns the safe-area inset: pinned
     at top:0, its text would otherwise sit underneath the iOS status
     bar. When it is showing, script zeroes --safe-top so .topbar stops
     adding the same inset a second time, and trims --topbar-lead --
     the bar is no longer the thing holding the header off the screen
     edge. Both of those were double-counted on a real iPad and are
     invisible in a desktop emulator, where the inset is 0. */
  /* Blocking, not advisory: opaque rather than the loading overlay's 50%,
     because there is nothing behind it worth reading until the documents
     are acknowledged. Same top/bottom geometry though, so the header and
     the tab bar stay visible and tappable. z-index 17 puts it over the
     loading overlay (15) — a load finishing must not reveal the app. */
  #doc-lock{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);
    z-index:17;display:flex;align-items:center;justify-content:center;padding:24px;}
  #doc-lock[hidden]{display:none;}
  .dl-scrim{position:absolute;inset:0;background:var(--bg);}
  .dl-card{position:relative;max-width:420px;text-align:center;display:flex;
    flex-direction:column;align-items:center;gap:14px;}
  .dl-mark{width:76px;height:76px;background:var(--nac-red);display:flex;align-items:center;
    justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.18);flex-shrink:0;}
  .dl-mark img{width:56px;height:56px;object-fit:contain;}
  .dl-card h2{margin:0;font-size:19px;font-weight:700;color:var(--value);}
  .dl-card p{margin:0;font-size:14px;line-height:1.45;color:var(--label);}
  .dl-card button{margin:4px 0 0;padding:12px 22px;font-size:15px;font-weight:600;
    background:var(--blue);color:#fff;border:none;border-radius:7px;cursor:pointer;}
  .doc-row .check.signed{color:var(--blue-dark);}
  .doc-row .resign-link{font-size:12.5px;font-weight:600;color:var(--blue);cursor:pointer;text-decoration:none;white-space:nowrap;}
  .doc-row .primary-action{display:inline-flex;align-items:center;gap:10px;}
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
  /* .panel button (0,1,1) outranks a bare .layer-move-btn (0,1,0), so the
     in-panel copy needs the extra qualifier or these render as full-size
     blue primary buttons inside the layer stack. */
  .layer-move-btn,.panel .layer-move-btn{margin:0;padding:4px 6px;line-height:0;display:inline-flex;align-items:center;justify-content:center;background:var(--bg);color:var(--blue-dark);border:1px solid var(--border);border-radius:4px;cursor:pointer;}
  .layer-move-btn:disabled,.panel .layer-move-btn:disabled{opacity:.3;cursor:default;}
  /* One criterion layer inside a bid — numbered header with its running
     match count, then that layer's own field/op/value controls. */
  .lf-layer-row{border:1px solid var(--border);border-radius:8px;padding:9px 10px;margin-top:8px;background:var(--bg);}
  .lf-layer-hdr{display:flex;align-items:center;gap:8px;margin-bottom:7px;}
  .lf-layer-num{font-size:12px;font-weight:700;color:var(--blue);letter-spacing:.02em;}
  .lf-layer-count{font-size:11.5px;color:var(--label);font-variant-numeric:tabular-nums;}
  .lf-layer-tools{margin-left:auto;display:flex;gap:3px;}
  .lf-layer-controls{display:flex;gap:6px;flex-wrap:wrap;}
  /* width:auto overrides .panel select's own width:100%, which would
     otherwise force each control onto its own line inside this flex row. */
  .lf-layer-controls select,.lf-layer-controls input[type=text]{flex:1 1 90px;width:auto;min-width:0;margin:0;}
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
  /* One loading overlay for the whole app — replaces the copies of
     <p class="placeholder-note">Loading…</p> that used to be scattered
     through the JS. It covers ONLY the content area: top/bottom are set in
     _positionLoadOverlay() from the active view's own .topbar and the
     .tabbar, and z-index 15 sits between .topbar (10) and .tabbar (20) so
     the header and the nav stay fully visible, undimmed, and still tappable
     while something is in flight. */
  #load-overlay{position:fixed;left:env(safe-area-inset-left);right:env(safe-area-inset-right);z-index:15;display:flex;align-items:center;justify-content:center;}
  /* Needed explicitly: display:flex above would otherwise beat [hidden]. */
  #load-overlay[hidden]{display:none;}
  /* The dimming is its own layer so the 50% applies to the content showing
     through and never to the mark and spinner sitting on top of it. */
  .lo-scrim{position:absolute;inset:0;background:var(--bg);opacity:.5;}
  .lo-center{position:relative;display:flex;flex-direction:column;align-items:center;gap:14px;}
  /* NAC red square behind the bear. Its own element with a fixed size, so
     the square and the spinner below it sit in exactly the same place
     whether or not the artwork loads — see #lo-bear, which is removed
     outright when it doesn't rather than rendering a broken image.
     The colour must stay --nac-red: nac-bear.png carries that same maroon as
     its own ground, so frame and artwork read as a single mark. Using --red
     here instead frames the maroon in bright alert red. */
  .lo-mark{width:88px;height:88px;background:var(--nac-red);display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.18);}
  #lo-bear{width:64px;height:64px;object-fit:contain;}
  .lo-spinner{width:22px;height:22px;border:2.5px solid var(--border);border-top-color:var(--nac-red);border-radius:50%;animation:lo-spin .8s linear infinite;}
  @keyframes lo-spin{to{transform:rotate(360deg);}}
  /* The global reduce rule near the top already kills this; stated again so
     the spinner's own behavior is readable where the spinner is defined. */
  @media (prefers-reduced-motion: reduce){ .lo-spinner{animation:none;} }
  /* Tab strip for the shared PDF viewer. Hidden at one tab: on a phone the
     title bar already names a lone document, and a strip holding a single
     chip is pure vertical cost. Scrolls sideways rather than wrapping, so
     the pages below never shift down as tabs are opened. */
  /* Browser-style tabs rather than pills: seated ON the divider with the
     active one carrying the page colour up out of it, so the tab reads as
     the top edge of the document rather than as a filter chip floating
     above it. */
  /* Sticky directly beneath .topbar, which is itself sticky at
     var(--ack-h). Tabs that scroll away with the document are only
     usable at the top of page one, which for a 60-page release means
     scrolling back up just to switch documents.
     z-index 9 keeps it under .topbar (10) so it slides beneath the
     header rather than over it. */
  .pdf-tabs{display:none;gap:1px;overflow-x:auto;padding:6px 10px 0;background:var(--card);
    position:sticky;top:calc(var(--ack-h,0px) + var(--topbar-h,0px));z-index:9;
    border-bottom:1px solid var(--border);-webkit-overflow-scrolling:touch;scrollbar-width:none;}
  .pdf-tabs::-webkit-scrollbar{display:none;}
  .pdf-tab{position:relative;flex:0 0 auto;display:flex;align-items:center;gap:8px;margin:0;
    padding:8px 12px 9px;background:transparent;color:var(--label);border:none;
    border-radius:9px 9px 0 0;font-size:12.5px;font-weight:600;max-width:200px;cursor:pointer;}
  /* Hairline separators BETWEEN inactive tabs only, the way a browser does
     it — and never beside the active one, which owns both its edges. */
  .pdf-tab:not(.active) + .pdf-tab:not(.active)::before{content:'';position:absolute;left:-1px;
    top:8px;bottom:8px;width:1px;background:var(--border);}
  .pdf-tab.active{background:var(--bg);color:var(--value);}
  /* Squares off the join to the page below, so the tab and the document
     read as one surface. */
  .pdf-tab.active::after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:2px;
    background:var(--bg);}
  .pdf-tab > span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  /* A real hit target on a touch screen without making the tab taller. */
  .pdf-tab-x{flex:0 0 auto;display:flex;align-items:center;justify-content:center;
    width:17px;height:17px;border-radius:50%;font-size:14px;line-height:1;opacity:.55;}
  .pdf-tab.active .pdf-tab-x{opacity:.8;}
  .pdf-tab-x:hover{opacity:1;background:var(--border);}
  .pdf-act{display:flex;align-items:center;justify-content:center;width:30px;height:30px;
    padding:0;margin:0;background:none;border:none;border-radius:7px;color:var(--blue-dark);
    cursor:pointer;text-decoration:none;}
  .pdf-act svg{width:19px;height:19px;}
  .pdf-act:hover{background:var(--border);}
  .pdf-act[hidden]{display:none;}
  #pdf-more-menu{position:absolute;top:100%;right:16px;z-index:30;background:var(--card);
    border:1px solid var(--border);border-radius:9px;box-shadow:0 6px 20px rgba(0,0,0,.28);
    padding:5px;display:flex;flex-direction:column;min-width:180px;}
  #pdf-more-menu[hidden]{display:none;}
  #pdf-more-menu button{margin:0;padding:10px 12px;background:none;border:none;text-align:left;
    font-size:13.5px;font-weight:600;color:var(--value);border-radius:6px;cursor:pointer;}
  #pdf-more-menu button:hover{background:var(--bg);}
  /* Messages read like ACARS lines: monospace, one flight per card, with
     the acknowledgement kept as a record rather than dismissing the card. */
  .pf-hdr{display:flex;align-items:center;justify-content:space-between;gap:10px;}
  .pf-more{margin:0;padding:2px 6px;background:none;border:none;color:var(--blue);
    font-weight:800;font-size:18px;letter-spacing:1px;line-height:1;cursor:pointer;}
  .pairing-search{padding:10px 14px 4px;}
  .pairing-search input{width:100%;box-sizing:border-box;padding:9px 12px;font-size:14px;
    border:1px solid var(--border);border-radius:7px;background:var(--bg);color:var(--value);
    -webkit-appearance:none;}
  /* Mail-style list: unread dot, subject, two-line preview, time on the
     right. A reassignment message carries a whole rebuilt pairing, far too
     much to sit in a list — the list says what arrived, the reading pane
     holds the body. */
  .msg-row{display:flex;gap:10px;padding:11px 14px 12px;border-bottom:1px solid var(--border);
    background:var(--card);cursor:pointer;align-items:flex-start;}
  .msg-row:active{background:var(--bg);}
  .msg-dot{flex:0 0 9px;width:9px;height:9px;border-radius:50%;background:var(--blue);margin-top:6px;}
  .msg-dot.read{background:transparent;}
  .msg-main{flex:1;min-width:0;}
  .msg-top{display:flex;align-items:baseline;gap:8px;}
  .msg-subject{flex:1;min-width:0;font-size:14.5px;font-weight:700;color:var(--value);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .msg-when{flex:0 0 auto;font-size:12px;color:var(--label);}
  /* Two lines then ellipsis, the Mail convention — it stops a 40-line
     pairing taking over the list. */
  .msg-preview{margin-top:3px;font-size:12.5px;line-height:1.35;color:var(--label);
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .msg-empty{padding:40px 20px;text-align:center;color:var(--label);font-size:14px;}
  /* Reading pane. The message is text and is shown as written — never
     reformatted into HTML — but text does not mean teletype: it reads in
     the same face as the rest of the app. pre-wrap keeps the line breaks
     and indentation the format itself carries, and tabular figures keep
     the flight numbers and times lining up without monospacing the prose
     along with them. */
  .msg-detail{padding:16px;}
  .msg-detail-hdr{font-size:12px;color:var(--label);margin-bottom:12px;}
  .msg-body{font-size:14.5px;line-height:1.55;color:var(--value);
    white-space:pre-wrap;word-break:break-word;
    font-variant-numeric:tabular-nums;}
  .msg-ackbar{margin-top:18px;}
  .msg-ackbar button{margin:0;padding:11px 18px;font-size:14px;font-weight:600;
    background:var(--blue);color:#fff;border:none;border-radius:7px;cursor:pointer;}
  .msg-acked{margin-top:18px;font-size:12.5px;color:var(--label);}
  .msg-badge{position:absolute;top:3px;right:calc(50% - 22px);min-width:16px;height:16px;
    padding:0 4px;border-radius:8px;background:var(--red);color:#fff;font-size:10px;
    font-weight:700;line-height:16px;text-align:center;}
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
  <!-- Sits between the header and the tab bar exactly like the loading
       overlay, so both stay visible and usable — the pilot has to be able
       to REACH Docs to clear it. Suppressed on the Docs list and the PDF
       viewer for the same reason. -->
  <div id="doc-lock" hidden role="alertdialog" aria-labelledby="doc-lock-title">
    <div class="dl-scrim"></div>
    <div class="dl-card">
      <div class="dl-mark"><img src="/static/nac-bear.png" alt="" onerror="this.remove()"></div>
      <h2 id="doc-lock-title">Acknowledgement Required</h2>
      <p id="doc-lock-body"></p>
      <button type="button" onclick="showView('doclocker')">Review Documents</button>
    </div>
  </div>
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
            <div class="panel-card-hdr pf-hdr">
              <span>Preflight Docs</span>
              <button type="button" class="pf-more" onclick="togglePreflightMore()" aria-expanded="false" aria-controls="preflight-more" title="More">&bull;&bull;&bull;</button>
            </div>
            <!-- Behind the ... deliberately: this is an occasional action, not
                 something to step past on every look at the card. The status
                 line says which OFP the release was built from, so whether it
                 is stale is answerable before pressing anything. -->
            <div id="preflight-more" style="display:none;">
              <div class="doc-row" style="cursor:pointer;" onclick="refreshOfp()">
                <div style="display:flex;align-items:center;gap:10px;">
                  <svg class="lead-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                  <div><div class="code">Refresh OFP</div><div class="desc" id="ov-release-state">&mdash;</div></div>
                </div>
                <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>
              </div>
            </div>
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
          <div><div class="code">FFD</div><div class="desc" id="ffd-desc">$ffd_desc_html</div></div>
          <div class="actions">
            <span id="ffd-primary-action" class="primary-action">$ffd_action_html</span>
            <svg class="bookmark-icon" data-doc="FFD" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="toggleBookmark('FFD', this)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">EFLIGHT PLAN</div><div class="desc" id="eflightplan-desc">$eflightplan_desc_html</div></div>
          <div class="actions">
            <span id="eflightplan-primary-action" class="primary-action">$eflightplan_action_html</span>
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
      <div id="weather-body"></div>
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
      <div id="doclocker-body"></div>
    </section>

    <section id="messages-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Messages</h1></div>
      </div>
      <div id="messages-body"></div>
    </section>

    <section id="msgdetail-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('messages')" aria-label="Back to Messages"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Message</h1></div>
      </div>
      <div id="msgdetail-body"></div>
    </section>

    <section id="pdf-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="closePdfView()" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-actions" id="pdf-actions">
          <!-- The anchor still carries the download: it is the only one of
               these that has to BE a link, since a data:/blob: href is what
               actually saves the file. -->
          <a id="pdf-export-link" class="pdf-act" title="Download" aria-label="Download"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></a>
          <button type="button" id="pdf-print-btn" class="pdf-act" title="Print" aria-label="Print" onclick="printCurrentPdf()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2"/><path d="M6 14h12v8H6z"/></svg></button>
          <button type="button" id="pdf-share-btn" class="pdf-act" title="Share" aria-label="Share" onclick="shareCurrentPdf()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V3"/><path d="M8 7l4-4 4 4"/><path d="M4 14v5a2 2 0 002 2h12a2 2 0 002-2v-5"/></svg></button>
          <button type="button" id="pdf-more-btn" class="pdf-act" title="More" aria-label="More" aria-expanded="false" onclick="togglePdfMore()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg></button>
        </div>
        <div id="pdf-more-menu" hidden>
          <button type="button" onclick="closeAllPdfTabs()">Close All Tabs</button>
          <button type="button" onclick="reloadCurrentPdf()">Reload Document</button>
        </div>
        <div class="topbar-title">
          <h1 id="pdf-view-title"></h1>
        </div>
      </div>
      <div id="pdf-tabs" class="pdf-tabs"></div>
      <div id="pdf-ffd-banner" class="ffd-banner" style="display:none;">Fit for Duty not signed — you can view this document, but Export is locked until you sign it.</div>
      <div id="pdf-ack-bar" style="display:none;padding:11px 14px;background:var(--card);border-bottom:1px solid var(--border);align-items:center;gap:10px;">
        <span id="pdf-ack-text" style="font-size:12.5px;color:var(--label);flex:1;"></span>
        <button type="button" id="pdf-ack-btn" style="margin:0;padding:8px 14px;font-size:13px;">Acknowledge</button>
      </div>
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
      <div id="motlog-body"></div>
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
        <div id="pairing-body"></div>
      </div>
      <div id="tab-library" class="tab-panel">
        <div id="library-crumb" style="padding:10px 14px;font-size:12.5px;color:var(--label);"></div>
        <div id="library-body"></div>
      </div>
      <div id="tab-layers" class="tab-panel">
        <div id="layers-body"></div>
      </div>
    </section>
    <section id="recovery-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="closeRecovery()" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title">
          <h1>Report a Disruption</h1>
        </div>
      </div>
      <div id="recovery-body"></div>
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
            <div id="aero-applied" style="margin:8px 0 0;font-size:13px;font-weight:600;color:var(--blue-dark);"></div>
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
      <div id="admin-entry" class="search-block" style="display:none;">
        <label>Admin</label>
        <button type="button" onclick="showView('admin')" style="margin-top:4px;width:100%;background:var(--bg);color:var(--blue);border:1px solid var(--border);padding:10px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;">Crew &amp; Document Stats</button>
      </div>
      <div id="settings-msg" class="placeholder-note"></div>
      <div style="text-align:center;padding:18px 0 4px;font-size:12px;color:var(--label);">Version $app_version</div>
    </section>
    <section id="admin-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('settings')" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:20px;"><path d="M15 18l-6-6 6-6"/></svg></button>
        <div class="topbar-title"><h1>Crew &amp; Documents</h1></div>
      </div>
      <div id="admin-body"></div>
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
        <!-- Regenerating used to be a text link buried mid-sentence in the
             status line, and only rendered on the branch where FFD was
             already signed — so the one time you most want a fresh release,
             with the paperwork not yet signed, it was not on screen at all.
             It is its own control now, shown whenever a release exists. -->
        <button id="release-regen-btn" style="display:none;margin-top:10px;width:100%;background:var(--card);color:var(--blue-dark);border:1px solid var(--blue-dark);padding:11px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;" onclick="generateRelease(true)">Regenerate Release &amp; TPS</button>
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
<!-- onerror still removes the <img> outright rather than painting a broken
     image, and .lo-mark's size is its own, so a missing asset costs the bear
     and nothing else — no layout shift. It starts hidden and is revealed on
     load, so the broken state is never painted even for a frame. -->
<div id="load-overlay" hidden role="status" aria-live="polite" aria-label="Loading">
  <div class="lo-scrim"></div>
  <div class="lo-center">
    <div class="lo-mark"><img id="lo-bear" src="/static/nac-bear.png" alt="" hidden onload="this.hidden=false" onerror="this.remove()"></div>
    <div class="lo-spinner"></div>
  </div>
</div>
<div id="toast"></div>
<script>
const LEG_ID = "$leg_id";
// The AeroAPI lookup already stored on this leg, or null. Present so
// opening a leg -- or coming back to one after a regenerate -- repaints
// the panel for free instead of calling a paid API again.
const AERO_SUGGESTION = $aero_suggestion_json;
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
  document.getElementById('msgdetail-view').classList.toggle('active', view==='msgdetail');
  document.getElementById('sign-view').classList.toggle('active', view==='sign');
  document.getElementById('pairing-view').classList.toggle('active', view==='pairing');
  document.getElementById('recovery-view').classList.toggle('active', view==='recovery');
  document.getElementById('weather-view').classList.toggle('active', view==='weather');
  document.getElementById('doclocker-view').classList.toggle('active', view==='doclocker');
  document.getElementById('saveddocs-view').classList.toggle('active', view==='saveddocs');
  document.getElementById('messages-view').classList.toggle('active', view==='messages');
  document.getElementById('settings-view').classList.toggle('active', view==='settings');
  document.getElementById('admin-view').classList.toggle('active', view==='admin');
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
  if(view === 'settings'){
    updateThemeButtons(); updateAutoSyncButtons();
    // Entry point only exists for admins — hidden by default so a
    // non-admin never sees a control that would 403 on them.
    const ae = document.getElementById('admin-entry');
    if(ae) ae.style.display = IS_ADMIN ? '' : 'none';
  }
  if(view === 'admin') initAdminView();
  if(view === 'saveddocs') initSavedDocs();
  if(view === 'messages') initMessages();
  // Exempt on Docs and the PDF viewer, so every view change has to
  // re-evaluate — leaving those puts the lock back up.
  paintDocAckBanner();
  if(view === 'doclocker') initDocLocker();
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
  // Company docs aren't scoped to a leg — Docs has its own root, so this
  // tab works with no leg loaded instead of bouncing to Home like the
  // genuinely leg-scoped tabs do.
  if(view === 'doclocker'){ if(!LEG_ID){ window.location.href = '/docs'; return; } showView(view); return; }
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
  if (typeof _syncThemeColor === 'function') _syncThemeColor();
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
  // Same tick asks whether the release we hold still matches what SimBrief
  // has. Kept separate from the block above deliberately: that one only
  // fires when the OFP TIMESTAMP moves, and a pilot who re-dispatches a
  // different flight onto the account is exactly the case where our
  // release goes stale without this leg's own timestamp telling us.
  try {
    const mr = await fetch('/messages/check', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({leg_id: LEG_ID ? Number(LEG_ID) : 0, simbrief_user: user}),
    });
    if(mr.ok) paintMessageBadge((await mr.json()).unacknowledged || 0);
  } catch(e) { /* best-effort */ }
}

// The nav tab carries the unacknowledged count, so a message posted by the
// background tick is visible without opening Messages to look.
function paintMessageBadge(n){
  _unackedMessages = n;
  ['tab-messages'].forEach(id => {
    const tab = document.getElementById(id);
    if(!tab) return;
    let dot = tab.querySelector('.msg-badge');
    if(!n){ if(dot) dot.remove(); return; }
    if(!dot){
      dot = document.createElement('span');
      dot.className = 'msg-badge';
      tab.appendChild(dot);
    }
    dot.textContent = n > 9 ? '9+' : String(n);
  });
}
let _unackedMessages = 0;

// A message's first line is its subject and the rest is its body — the
// format writes them that way, so the list needs no separate field.
function _msgSubject(body){ return String(body || '').split('\\n')[0]; }
function _msgPreview(body){
  return String(body || '').split('\\n').slice(1).join(' ').replace(/\\s+/g, ' ').trim();
}

// Today shows a time, anything older shows a date — the Mail convention,
// and on a flight deck "1445" is more use than "9/1/2026" for something
// that arrived an hour ago.
function _msgWhen(iso){
  if(!iso) return '';
  const d = new Date(iso);
  if(isNaN(d)) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if(sameDay) return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const days = (now - d) / 86400000;
  if(days < 7) return d.toLocaleDateString([], {weekday: 'short'});
  return d.toLocaleDateString([], {month: 'numeric', day: 'numeric'});
}

let _messagesCache = [];

async function initMessages(){
  const body = document.getElementById('messages-body');
  showLoading(body);
  let data;
  try {
    const r = await fetch('/messages');
    data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; return; }
  _messagesCache = data.messages || [];
  paintMessageBadge(data.unacknowledged || 0);
  body.innerHTML = '';
  if(!_messagesCache.length){
    const none = document.createElement('p');
    none.className = 'msg-empty';
    none.textContent = 'No Messages';
    body.appendChild(none);
    return;
  }
  _messagesCache.forEach(m => {
    const row = document.createElement('div');
    row.className = 'msg-row';
    row.onclick = () => openMessage(m.id);

    const dot = document.createElement('div');
    // Acknowledged is this app's "read": the blue dot marks what still
    // wants attention rather than merely what has not been opened.
    dot.className = 'msg-dot' + (m.acknowledged_at ? ' read' : '');
    row.appendChild(dot);

    const main = document.createElement('div');
    main.className = 'msg-main';
    const top = document.createElement('div');
    top.className = 'msg-top';
    const subj = document.createElement('div');
    subj.className = 'msg-subject';
    subj.textContent = _msgSubject(m.body);
    const when = document.createElement('div');
    when.className = 'msg-when';
    when.textContent = _msgWhen(m.created_at);
    top.appendChild(subj); top.appendChild(when);
    main.appendChild(top);

    const prev = _msgPreview(m.body);
    if(prev){
      const p = document.createElement('div');
      p.className = 'msg-preview';
      p.textContent = prev;
      main.appendChild(p);
    }
    row.appendChild(main);
    body.appendChild(row);
  });
}

function openMessage(id){
  const m = _messagesCache.find(x => String(x.id) === String(id));
  if(!m) return;
  const body = document.getElementById('msgdetail-body');
  body.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'msg-detail';

  const hdr = document.createElement('div');
  hdr.className = 'msg-detail-hdr';
  hdr.textContent = m.created_at ? new Date(m.created_at).toLocaleString() : '';
  wrap.appendChild(hdr);

  const pre = document.createElement('div');
  pre.className = 'msg-body';
  pre.textContent = m.body;          // textContent: the body is teletype text, not markup
  wrap.appendChild(pre);

  if(m.acknowledged_at){
    const done = document.createElement('div');
    done.className = 'msg-acked';
    done.textContent = 'Acknowledged ' + new Date(m.acknowledged_at).toLocaleString();
    wrap.appendChild(done);
  } else {
    const bar = document.createElement('div');
    bar.className = 'msg-ackbar';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Acknowledge';
    btn.onclick = () => acknowledgeMessage(m.id, btn);
    bar.appendChild(btn);
    wrap.appendChild(bar);
  }
  body.appendChild(wrap);
  showView('msgdetail');
}

async function acknowledgeMessage(id, btn){
  btn.disabled = true;
  try {
    const r = await fetch('/messages/' + id + '/ack', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ btn.disabled = false; showToast(data.error || 'Could not acknowledge'); return; }
    paintMessageBadge(data.unacknowledged || 0);
    await initMessages();
    // Back to the list, the way Mail leaves you after acting on a message.
    showView('messages');
  } catch(e) { btn.disabled = false; showToast('Request failed: ' + e); }
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

// Paints the results panel from a suggestion object. Shared by a live
// lookup and by the copy already stored on the leg, so those two paths
// cannot drift apart in what they show.
function paintAeroSuggestion(data, applied){
  if(!data) return;
  _aeroSuggestion = data;
  const orig = data.orig || "", dest = data.dest || "";
  document.getElementById('aero-route-val').textContent =
    data.route_found ? (data.route || "(no filed route on record)")
                     : ("AA does not fly " + orig + "\u2192" + dest + " nonstop \u2014 no route suggestion");
  document.getElementById('aero-gate-orig').textContent = data.gate_origin || "\u2014";
  document.getElementById('aero-gate-dest').textContent = data.gate_destination || "\u2014";
  document.getElementById('aero-basis').textContent =
    "Gates based on " + data.sample_size_origin + " AA departure(s) at " + orig +
    " and " + data.sample_size_destination + " AA arrival(s) at " + dest + " in the last 10 days.";
  document.getElementById('aero-applied').textContent = applied || "";
  document.getElementById('aero-results').style.display = 'block';
}

// Writes the gates onto the leg AND stores the suggestion that produced
// them. Called automatically by a lookup rather than from a button: the
// pilot asked for gates by running the lookup, so a second click was a
// step with no decision in it.
async function applyAeroGates(){
  if(!_aeroSuggestion) return false;
  const dep = _aeroSuggestion.gate_origin || "", arr = _aeroSuggestion.gate_destination || "";
  if(!dep && !arr){
    document.getElementById('aero-applied').textContent = "No gate data to apply.";
    return false;
  }
  try {
    const r = await fetch('/fos/' + LEG_ID + '/gates', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dep_gate: dep, arr_gate: arr, suggestion: _aeroSuggestion}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not apply gates'); return false; }
    const depGateEl = document.getElementById('ov-dep-gate'); if(depGateEl) depGateEl.textContent = data.dep_gate || "";
    const arrGateEl = document.getElementById('ov-arr-gate'); if(arrGateEl) arrGateEl.textContent = data.arr_gate || "";
    return true;
  } catch(e) { showToast('Request failed: ' + e); return false; }
}

async function fetchAeroSuggestions(){
  const msg = document.getElementById('aero-msg');
  const btn = document.getElementById('aero-btn');
  const key = document.getElementById('aero-key').value.trim();
  const orig = document.getElementById('sbgen-orig').value.trim().toUpperCase();
  const dest = document.getElementById('sbgen-dest').value.trim().toUpperCase();
  document.getElementById('aero-results').style.display = 'none';
  if(!key){ msg.textContent = 'Enter your AeroAPI key first.'; msg.style.color = '#c0392b'; return; }
  if(!orig || !dest){ msg.textContent = "Origin and Destination are required \u2014 set those in Generate Flight Plan below first."; msg.style.color = '#c0392b'; return; }

  btn.disabled = true;
  msg.textContent = 'Looking up AA at ' + orig + ' / ' + dest + "\u2026";
  msg.style.color = '';
  try {
    const r = await fetch('/aeroapi/suggest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_key: key, orig, dest}),
    });
    const data = await r.json();
    btn.disabled = false;
    if(!r.ok){ msg.textContent = data.error || 'Lookup failed'; msg.style.color = '#c0392b'; return; }
    // Carried on the object so the stored copy can repaint the panel later
    // without the Generate form still holding these two values.
    data.orig = orig; data.dest = dest;
    paintAeroSuggestion(data, "");
    if(await applyAeroGates()) document.getElementById('aero-applied').textContent = "Gates applied to this flight.";
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
// readOnly re-reads what is already on file and repaints, without ever
// generating — used by the post-FFD refresh, which only needs to pick up
// the now-true fit_for_duty flag and must not manufacture a release for a
// leg that never had one.
function togglePreflightMore(){
  const el = document.getElementById('preflight-more');
  const btn = document.querySelector('.pf-more');
  if(!el) return;
  const open = el.style.display === 'none';
  el.style.display = open ? 'block' : 'none';
  if(btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// Checks whether SimBrief has a NEWER plan than the one this release was
// built from, and only regenerates if so. Comparing SimBrief's own
// time_generated rather than our generated_at: ours is a wall clock and
// says nothing about whether the PLAN changed, so it would rebuild on
// every press.
let _ofpRefreshBusy = false;
async function refreshOfp(){
  if(_ofpRefreshBusy) return;
  const user = _simbriefUser();
  if(!user){ showToast('Add a SimBrief username in Settings first'); return; }
  _ofpRefreshBusy = true;
  const el = document.getElementById('ov-release-state');
  const was = el ? el.textContent : '';
  if(el) el.textContent = 'Checking SimBrief\u2026';
  try {
    const [liveR, mineR] = await Promise.all([
      fetch('/simbrief-api/generated-at?user=' + encodeURIComponent(user)),
      fetch('/fos/' + LEG_ID + '/release', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cached_only: true}),
      }),
    ]);
    const live = liveR.ok ? String((await liveR.json()).time_generated || '') : '';
    // 404 here is "nothing generated for this leg yet", the normal state of
    // a fresh flight — there is no comparison to make, so just build it.
    const mine = mineR.ok ? String((await mineR.json()).ofp_time_generated || '') : null;

    if(live && mine !== null && mine && live === mine){
      if(el) el.textContent = was;
      showToast('OFP is already current');
      return;
    }
    // Either nothing is on file yet, SimBrief has moved on, or one side has
    // no timestamp to compare — rebuild rather than guess, since a stale
    // release is the worse error.
    //
    // Done in place, deliberately. This used to showView('release') first,
    // which dumps you on Confirm & Generate Release — the send-to-SimBrief
    // page — when all you asked for was a refresh. Refreshing from Overview
    // should leave you on Overview.
    if(el) el.textContent = mine === null ? 'Generating\u2026' : 'Refreshing\u2026';
    const gen = await fetch('/fos/' + LEG_ID + '/release', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: user, force: mine !== null}),
    });
    const data = await gen.json().catch(() => ({}));
    if(!gen.ok){
      if(el) el.textContent = was;
      showToast(data.error || 'Could not refresh the OFP');
      return;
    }
    // Anything already holding the old release has to let go of it.
    _setReleaseCache(null);
    _pdfTabs = _pdfTabs.filter(t => t.kind !== 'release');
    _activePdfTab = _pdfTabs.length ? _pdfTabs[0].key : null;
    await paintReleaseState();
    showToast(mine === null ? 'Release generated' : 'OFP refreshed');
  } catch(e) {
    if(el) el.textContent = was;
    showToast('Could not check SimBrief: ' + e);
  } finally {
    _ofpRefreshBusy = false;
  }
}

// Says which OFP the release on file was built from, so whether it is stale
// is visible before pressing anything. Uses the read-only path — looking
// must never generate.
async function paintReleaseState(){
  const el = document.getElementById('ov-release-state');
  if(!el || !LEG_ID) return;
  try {
    const r = await fetch('/fos/' + LEG_ID + '/release', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cached_only: true}),
    });
    if(r.status === 404){ el.textContent = 'Not generated yet'; return; }
    if(!r.ok){ el.textContent = ''; return; }
    const data = await r.json();
    const when = data.generated_at ? new Date(data.generated_at) : null;
    const rev = (data.generation === undefined || data.generation === null)
      ? '' : (' \u00b7 rev .' + data.generation);
    el.textContent = when ? ('Generated ' + when.toLocaleString() + rev) : ('On file' + rev);
  } catch(e) { el.textContent = ''; }
}

function generateRelease(force, readOnly){
  const btn = document.getElementById('release-gen-btn');
  const status = document.getElementById('release-status');
  const userId = document.getElementById('release-user').value.trim();
  if(!userId){ status.textContent = 'No SimBrief username on file — set one in Settings first.'; status.style.color = '#c0392b'; return; }
  btn.disabled = true;
  const regenBtn = document.getElementById('release-regen-btn');
  if(regenBtn) regenBtn.disabled = true;
  status.style.color = '';
  status.textContent = force ? 'Regenerating — this can take up to a minute…' : 'Generating release — this can take up to a minute…';
  document.getElementById('release-downloads').style.display = 'none';
  // Both branches POST. readOnly used to GET, from a version of the route
  // that had a GET; the merged one is POST-only with a cached_only flag, so
  // that GET fell through to a 404 HTML page and the JSON parse blew up with
  // "Unexpected token '<'". Its only caller is the refresh after signing
  // FFD, which is why this went unnoticed.
  fetch('/fos/' + LEG_ID + '/release', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(readOnly ? {cached_only: true}
                                  : {user_id: userId, force: !!force}),
  })
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      btn.disabled = false;
      if(regenBtn) regenBtn.disabled = false;
      // cached_only 404s when nothing is on file, which is the normal state
      // of a fresh flight rather than a failure worth a red line.
      if(readOnly && !ok){ status.textContent = ''; return; }
      if(!ok){ status.textContent = 'Failed: ' + (data.error || 'unknown error'); status.style.color = '#c0392b'; return; }
      _setReleaseCache(data); // ensureRelease()/viewDoc() reuse this — one generation serves both paths
      // Generation itself is never blocked (soft gate) — the PDF renders
      // fine via viewDoc() either way. Only the Download links here stay
      // locked until FFD is signed, same as pdf-view's Export link.
      // Regenerating is possible the moment a release exists, signed or
      // not — it rebuilds the OFP and the TPS together.
      document.getElementById('release-regen-btn').style.display = 'block';
      if(!data.fit_for_duty){
        status.textContent = 'Release generated — sign Fit for Duty (All Commands > FFD) to unlock downloads.';
        status.style.color = 'var(--red)';
        document.getElementById('release-rls-link').style.display = 'none';
        document.getElementById('release-wb-link').style.display = 'none';
        return;
      }
      // The generation counter is the digit after the point in the PDF's
      // own "RELEASE 6.7" header, so showing it here lets a pilot match the
      // printout in front of them to what the app currently holds.
      const _gen = (data.generation === undefined || data.generation === null) ? '' : (' \u00b7 rev .' + data.generation);
      status.textContent = (data.cached
        ? 'Release on file, generated ' + new Date(data.generated_at).toLocaleString()
        : 'Release generated') + _gen;
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
      paintReleaseState();
    })
    .catch(e => { btn.disabled = false; if(regenBtn) regenBtn.disabled = false;
                  status.textContent = 'Request failed: ' + e; status.style.color = '#c0392b'; });
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
// ---------------------------------------------------------------------------
// Loading overlay — the one loading state in this app, in place of the copies
// of <p class="placeholder-note">Loading…</p> that each load site used to
// write into its own container. Sits between the header and the bottom nav
// (see #load-overlay's CSS) and leaves both fully visible.
//
// showLoading(el) takes the element whose content is being loaded and takes
// itself down again as soon as that element's content changes — so each site
// is one call with no hideLoading() to forget on an error path. Every one of
// them writes into that same element either way, content or error message.
// ---------------------------------------------------------------------------
let _loadObserver = null;
let _loadTimer = null;
function _positionLoadOverlay(){
  const overlay = document.getElementById('load-overlay');
  if(!overlay) return;
  // Measured, not assumed: .topbar is sticky and its height moves with its
  // own title and the device's safe-area inset, and the doc-ack banner can
  // sit above it.
  const topbar = document.querySelector('.view.active .topbar');
  const tabbar = document.querySelector('.tabbar');
  overlay.style.top = Math.max(0, topbar ? topbar.getBoundingClientRect().bottom : 0) + 'px';
  overlay.style.bottom = (tabbar ? tabbar.getBoundingClientRect().height : 0) + 'px';
}
function showLoading(el){
  const overlay = document.getElementById('load-overlay');
  if(!overlay) return;
  hideLoading();
  _positionLoadOverlay();
  overlay.hidden = false;
  if(el && window.MutationObserver){
    _loadObserver = new MutationObserver(hideLoading);
    _loadObserver.observe(el, {childList: true, subtree: true, characterData: true});
  }
  // Backstop — nothing should stay covered forever if a load somehow neither
  // renders nor reports.
  _loadTimer = setTimeout(hideLoading, 45000);
}
function hideLoading(){
  const overlay = document.getElementById('load-overlay');
  if(overlay) overlay.hidden = true;
  if(_loadObserver){ _loadObserver.disconnect(); _loadObserver = null; }
  if(_loadTimer){ clearTimeout(_loadTimer); _loadTimer = null; }
}
window.addEventListener('resize', () => {
  const overlay = document.getElementById('load-overlay');
  if(overlay && !overlay.hidden) _positionLoadOverlay();
});
// The overlay is positioned from the header's measured bottom edge, and a
// sticky header's bottom edge moves while the browser chrome collapses on
// scroll. Without this it keeps a stale top and creeps over the header.
window.addEventListener('scroll', () => {
  const overlay = document.getElementById('load-overlay');
  if(overlay && !overlay.hidden) _positionLoadOverlay();
  const lock = document.getElementById('doc-lock');
  if(lock && !lock.hidden) _positionDocLock();
}, {passive: true});

let toastTimer;
function showToast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 1600);
}

// This leg's release, held for the life of the page. Keyed by leg id, not a
// bare object: LEG_ID is server-rendered per page load so today a leg switch
// is a full navigation and this can't outlive it — but this cache has already
// caused one stale-data bug (a saved FFD signature reading as unsaved, see
// submitSignature), and "it can't hold another leg's release" should be a
// property of the cache itself rather than of how the page happens to be
// reached.
let _releaseCache = null;
let _releaseCacheLeg = null;
function _setReleaseCache(data){
  _releaseCache = data;
  _releaseCacheLeg = data ? String(LEG_ID) : null;
}
function _getReleaseCache(){
  if(_releaseCache && _releaseCacheLeg === String(LEG_ID)) return _releaseCache;
  _releaseCache = null; _releaseCacheLeg = null;
  return null;
}
// READ-ONLY on purpose (cached_only). Opening a document must only ever show
// what was actually generated FOR THIS LEG — generating on demand here is what
// leaked another leg's OFP onto a leg nothing had been generated for, because
// the generator renders whatever OFP is on the SimBrief account, not this leg.
// Generating stays where the pilot asks for it explicitly: Release > Generate.
// quiet suppresses the toasts — restorePdfTabs() runs on every page load and
// must say nothing on a leg that simply has no release yet.
async function ensureRelease(quiet){
  const held = _getReleaseCache();
  if(held) return held;
  const say = (m) => { if(!quiet) showToast(m); };
  if(!LEG_ID){ say('Open a flight first'); return null; }
  let r, data;
  try {
    r = await fetch('/fos/' + LEG_ID + '/release', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cached_only: true})});
    data = await r.json();
  } catch(e) { say('Request failed: ' + e); return null; }
  if(r.status === 404){ say('No release generated for this flight yet — generate one under Release'); return null; }
  if(!r.ok){ say('Failed: ' + (data.error || 'unknown error')); return null; }
  _setReleaseCache(data);
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
// Every page gets a correctly-sized placeholder up front, but only the
// pages near the viewport are ever rasterised, and they are released again
// once they scroll well clear. Rendering the whole document eagerly is what
// crashed the tab on the bigger company manuals: a page at device pixel
// ratio is roughly 2.7 MB of canvas, so a 200-page FOM asked the browser
// for gigabytes in one go.
//
// The placeholders carry each page's real aspect ratio, so the scrollbar is
// honest from the first paint and nothing jumps around as pages fill in.
let _pdfObserver = null;

function _teardownPdfObserver(){
  if(_pdfObserver){ _pdfObserver.disconnect(); _pdfObserver = null; }
}

async function renderPdfInline(bytes){
  const token = ++_pdfRenderToken;
  const container = document.getElementById('pdf-pages');
  _teardownPdfObserver();
  container.innerHTML = '<p style="color:#fff;">Rendering…</p>';
  const pdf = await pdfjsLib.getDocument({data: bytes}).promise;
  if(token !== _pdfRenderToken) return; // a newer open superseded this one
  container.innerHTML = '';
  const targetWidth = Math.max(container.clientWidth - 24, 280);
  // Render the bitmap at the screen's real device pixel ratio, not just
  // the CSS width — a canvas sized 1:1 to CSS pixels looks soft once the
  // browser upscales it on a Retina/high-DPI iPad. Same DPR-scaling
  // pattern the signature pad canvas already uses.
  const dpr = window.devicePixelRatio || 1;

  // Every slot is sized from page 1 rather than from its own page. getPage
  // is per-page work even though it rasterises nothing, and asking for all
  // of them up front is what made a 200-page manual sit blank for the best
  // part of a minute before the first page appeared. Documents are
  // overwhelmingly uniform, and the rare page that is not corrects its own
  // slot when it renders — a late nudge on one page beats a stall on all
  // of them. Opening is now O(1) in page count.
  const first = await pdf.getPage(1);
  if(token !== _pdfRenderToken) return;
  const nominal = first.getViewport({scale: 1});
  const slots = [];
  for(let n = 1; n <= pdf.numPages; n++){
    const slot = document.createElement('div');
    slot.className = 'pdf-slot';
    slot.style.width = '100%';
    slot.style.maxWidth = targetWidth + 'px';
    // Reserve the height this page is expected to occupy, so the scrollbar
    // is honest from the first paint.
    slot.style.aspectRatio = nominal.width + ' / ' + nominal.height;
    slot.style.background = '#fff';
    slot.style.boxShadow = '0 1px 4px rgba(0,0,0,.35)';
    slot.dataset.page = String(n);
    container.appendChild(slot);
    slots.push({slot, page: (n === 1 ? first : null), rendered: false, rendering: false});
  }
  if(!slots.length) return;

  async function fill(entry){
    if(entry.rendered || entry.rendering || token !== _pdfRenderToken) return;
    entry.rendering = true;
    try {
      // Fetched here, not up front — this is the per-page cost that used to
      // be paid for the whole document before anything was shown.
      if(!entry.page) entry.page = await pdf.getPage(Number(entry.slot.dataset.page));
      if(token !== _pdfRenderToken) return;
      const base = entry.page.getViewport({scale: 1});
      // A page that is not the nominal size fixes its own slot.
      const want = base.width + ' / ' + base.height;
      if(entry.slot.style.aspectRatio.replace(/\s/g, '') !== want.replace(/\s/g, '')){
        entry.slot.style.aspectRatio = want;
      }
      const scale = (targetWidth / base.width) * dpr;
      const viewport = entry.page.getViewport({scale});
      const canvas = document.createElement('canvas');
      canvas.className = 'pdf-page';
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.style.width = '100%';
      canvas.style.display = 'block';
      await entry.page.render({canvasContext: canvas.getContext('2d'), viewport}).promise;
      if(token !== _pdfRenderToken) return;
      entry.slot.innerHTML = '';
      entry.slot.appendChild(canvas);
      entry.rendered = true;
    } catch(e) {
      // A page that will not render should not take the document with it.
      entry.slot.innerHTML = '<p style="color:#900;padding:12px;font-size:12px;">page ' +
        entry.slot.dataset.page + ' failed to render</p>';
      entry.rendered = true;
    } finally {
      entry.rendering = false;
    }
  }

  function release(entry){
    if(!entry.rendered) return;
    // Zero the canvas before dropping it: some browsers hold the backing
    // store until the element is collected otherwise.
    const c = entry.slot.querySelector('canvas');
    if(c){ c.width = 0; c.height = 0; }
    entry.slot.innerHTML = '';
    entry.rendered = false;
  }

  // Without IntersectionObserver there is no way to know what is on screen,
  // and a viewer stuck on page 1 forever is worse than a heavy one. Fall
  // back to the old eager render — correct, just costly, and only on
  // browsers old enough to lack an API iOS has had since 12.2.
  if(typeof IntersectionObserver === 'undefined'){
    for(const e of slots){
      if(token !== _pdfRenderToken) return;
      await fill(e);
    }
    return;
  }

  // rootMargin renders a screen ahead and behind, so scrolling at a normal
  // speed never catches a blank page; anything further out is released.
  _pdfObserver = new IntersectionObserver((entries) => {
    if(token !== _pdfRenderToken) return;
    entries.forEach(e => {
      const entry = slots[Number(e.target.dataset.page) - 1];
      if(!entry) return;
      if(e.isIntersecting) fill(entry); else release(entry);
    });
  }, {root: null, rootMargin: '150% 0px', threshold: 0.01});
  slots.forEach(e => _pdfObserver.observe(e.slot));

  // The observer only fires on the next frame; paint page 1 immediately so
  // the viewer is never briefly blank.
  await fill(slots[0]);
}
// ---------------------------------------------------------------------
// Tabbed PDF viewer. One #pdf-view is shared by release paperwork and by
// company documents, so the strip covers both kinds at once and a release,
// its W&B and a manual can all be held open together.
//
// A tab keeps the PDF BYTES and re-renders when selected; only the ACTIVE
// tab has canvases in the DOM. That split is deliberate. Bytes are a few
// hundred KB, but a rendered page at device pixel ratio is roughly 2.7 MB,
// so an OFP is ~50 MB of canvas — keeping several tabs' pages alive is how
// you get iOS Safari to evict the whole app mid-flight.
let _pdfTabs = [];
let _activePdfTab = null;

// Which documents are open, and which one was showing, kept across reloads.
// Only the KEYS are stored — never the PDF bytes. Those are megabytes, they
// go stale the moment the release is regenerated, and localStorage is the
// wrong place for a document. The bytes are re-fetched from the release the
// tab is reopened against, so a restored tab always shows current paperwork
// rather than a snapshot from before the last refresh.
const _PDF_TABS_KEY = 'fos_pdf_tabs_' + (typeof LEG_ID !== 'undefined' ? LEG_ID : '');

function _savePdfTabs(){
  try {
    localStorage.setItem(_PDF_TABS_KEY, JSON.stringify({
      keys: _pdfTabs.filter(t => t.kind === 'release').map(t => t.key),
      active: _activePdfTab,
    }));
  } catch(e) { /* private mode, quota — losing tab state is not worth an error */ }
}

function _readSavedPdfTabs(){
  try {
    const raw = localStorage.getItem(_PDF_TABS_KEY);
    if(!raw) return null;
    const v = JSON.parse(raw);
    return (v && Array.isArray(v.keys)) ? v : null;
  } catch(e) { return null; }
}

// Reopens whatever was open last time, and failing that seeds from Saved
// Docs — the documents the pilot bookmarked are exactly the ones they want
// waiting for them. Silent: it must never steal the view or toast on a
// leg whose release has since been regenerated away.
async function restorePdfTabs(){
  if(typeof LEG_ID === 'undefined' || !LEG_ID) return;
  if(_pdfTabs.length) return;
  const saved = _readSavedPdfTabs();
  let keys = saved ? saved.keys : [];
  if(!keys.length){
    keys = (LEG_BOOKMARKED_DOCS || [])
      .map(code => DOC_CODE_TO_KIND[code])
      .filter(Boolean)
      .map(m => 'rel:' + m[0]);
  }
  if(!keys.length) return;
  const data = await ensureRelease(true);
  if(!data) return;                        // nothing on file — nothing to reopen
  const byKind = {rls:'rls_pdf_b64', fi:'fi_pdf_b64', fil:'fil_pdf_b64', wb:'wb_pdf_b64',
                  weather:'weather_pdf_b64', notams:'notams_pdf_b64',
                  field_report:'field_report_pdf_b64'};
  const labelFor = {};
  Object.keys(DOC_CODE_TO_KIND).forEach(code => {
    const m = DOC_CODE_TO_KIND[code];
    labelFor['rel:' + m[0]] = m[1];
  });
  keys.forEach(key => {
    const kind = key.replace(/^rel:/, '');
    const b64 = data[byKind[kind]];
    if(!b64) return;                       // not in this release any more
    _pdfTabs.push({
      key: key, label: labelFor[key] || kind.toUpperCase(), kind: 'release',
      bytes: b64ToBytes(b64),
      meta: {
        fitForDuty: !!data.fit_for_duty,
        exportName: kind === 'rls' ? data.filename
                  : (data.filename || 'release.pdf').replace('-RLS.pdf', '-' + kind.toUpperCase() + '.pdf'),
      },
    });
  });
  if(!_pdfTabs.length) return;
  const want = saved && saved.active && _pdfTabIndex(saved.active) >= 0
    ? saved.active : _pdfTabs[0].key;
  _activePdfTab = want;
  // Persist the seeded set too, so closing one of the Saved-Docs tabs
  // sticks instead of being re-seeded on the next load.
  _savePdfTabs();
  renderPdfTabs();
}

function _pdfTabIndex(key){ return _pdfTabs.findIndex(t => t.key === key); }

// The tab strip pins under the header, so it needs the header's real height
// — which moves with the safe-area inset and with the title wrapping.
function _syncTopbarHeight(){
  const tb = document.querySelector('#pdf-view .topbar');
  if(!tb) return;
  document.documentElement.style.setProperty(
    '--topbar-h', Math.round(tb.getBoundingClientRect().height) + 'px');
}

function renderPdfTabs(){
  _syncTopbarHeight();
  const strip = document.getElementById('pdf-tabs');
  strip.innerHTML = '';
  // Shown from the FIRST tab. It used to wait for a second one, on the
  // reasoning that the title bar already names a lone document and a strip
  // holding one chip is wasted height — but that made opening a single
  // saved doc look like the tabs were missing entirely, with no hint that
  // opening another would keep this one. One chip is the affordance.
  //
  // Still hidden while an untabbed company document is showing:
  // _activePdfTab is null then, so no chip matches what is on screen.
  strip.style.display = (_pdfTabs.length >= 1 && _activePdfTab) ? 'flex' : 'none';
  _pdfTabs.forEach(t => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'pdf-tab' + (t.key === _activePdfTab ? ' active' : '');
    el.onclick = () => activatePdfTab(t.key);
    const name = document.createElement('span');
    name.textContent = t.label;
    el.appendChild(name);
    const x = document.createElement('span');
    x.className = 'pdf-tab-x';
    x.textContent = "\u00d7";
    x.setAttribute('aria-label', 'Close ' + t.label);
    x.onclick = (e) => { e.stopPropagation(); closePdfTab(t.key); };
    el.appendChild(x);
    strip.appendChild(el);
  });
  const active = strip.querySelector('.pdf-tab.active');
  if(active && active.scrollIntoView) active.scrollIntoView({block:'nearest', inline:'nearest'});
}

// Opens a document, or focuses it if it is already open. Re-opening also
// refreshes the stored copy, so a regenerated release replaces its tab
// instead of leaving a stale one beside a new one.
async function openPdfTab(tab){
  const i = _pdfTabIndex(tab.key);
  if(i >= 0) _pdfTabs[i] = tab; else _pdfTabs.push(tab);
  // Coming back from an untabbed company document.
  _currentCompanyDoc = null;
  showView('pdf');
  _savePdfTabs();
  await activatePdfTab(tab.key);
}

// Download, Print and Share are release-document controls. A company manual
// is read in the app and never leaves it, so they are hidden rather than
// disabled there — a greyed control invites a question about why it does
// not work.
function _setPdfActions(on){
  ['pdf-export-link', 'pdf-print-btn', 'pdf-share-btn', 'pdf-more-btn'].forEach(id => {
    const el = document.getElementById(id);
    if(el) el.hidden = !on;
  });
  if(!on){
    closePdfMore();
    const a = document.getElementById('pdf-export-link');
    if(a){ a.removeAttribute('href'); a.removeAttribute('download'); }
  }
}

function togglePdfMore(){
  const m = document.getElementById('pdf-more-menu');
  const b = document.getElementById('pdf-more-btn');
  if(!m) return;
  const open = m.hidden;
  m.hidden = !open;
  if(b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
}
function closePdfMore(){
  const m = document.getElementById('pdf-more-menu');
  if(m && !m.hidden){ m.hidden = true;
    const b = document.getElementById('pdf-more-btn');
    if(b) b.setAttribute('aria-expanded', 'false'); }
}
document.addEventListener('click', (e) => {
  if(!e.target.closest('#pdf-more-menu') && !e.target.closest('#pdf-more-btn')) closePdfMore();
});

function _activePdfBytes(){
  const t = _pdfTabs[_pdfTabIndex(_activePdfTab)];
  if(t) return {bytes: t.bytes, name: (t.meta && t.meta.exportName) || (t.label + '.pdf')};
  // A company document is untabbed and read-in-app only — no bytes handed
  // out, which is what keeps Download/Print/Share off it.
  return null;
}

function printCurrentPdf(){
  closePdfMore();
  const cur = _activePdfBytes();
  if(!cur){ showToast('Nothing to print'); return; }
  // A hidden iframe rather than window.open: iOS Safari blocks the popup
  // when it is not the direct result of the tap, and an iframe prints the
  // PDF itself rather than a screenshot of the canvas.
  const url = URL.createObjectURL(new Blob([cur.bytes.slice()], {type: 'application/pdf'}));
  const frame = document.createElement('iframe');
  frame.style.cssText = 'position:fixed;right:0;bottom:0;width:1px;height:1px;border:0;opacity:0;';
  frame.src = url;
  frame.onload = () => {
    try { frame.contentWindow.focus(); frame.contentWindow.print(); }
    catch(e) { showToast('Printing is not available here'); }
    // Left in the DOM until the print dialog is done with it; revoking or
    // removing immediately cancels the job on some browsers.
    setTimeout(() => { URL.revokeObjectURL(url); frame.remove(); }, 60000);
  };
  document.body.appendChild(frame);
}

async function shareCurrentPdf(){
  closePdfMore();
  const cur = _activePdfBytes();
  if(!cur){ showToast('Nothing to share'); return; }
  const file = new File([cur.bytes.slice()], cur.name, {type: 'application/pdf'});
  // canShare with the actual file, not just a feature check: iOS reports
  // navigator.share on desktop Safari too, where sharing a FILE fails.
  if(navigator.canShare && navigator.canShare({files: [file]})){
    try { await navigator.share({files: [file], title: cur.name}); }
    catch(e) { if(e && e.name !== 'AbortError') showToast('Share failed: ' + e.message); }
    return;
  }
  showToast('Sharing is not available on this device — use Download');
}

function reloadCurrentPdf(){
  closePdfMore();
  if(_activePdfTab) activatePdfTab(_activePdfTab);
  else showToast('Nothing open');
}

function closeAllPdfTabs(){
  closePdfMore();
  _pdfTabs = [];
  _activePdfTab = null;
  _savePdfTabs();
  renderPdfTabs();
  closePdfView();
}

async function activatePdfTab(key){
  const tab = _pdfTabs[_pdfTabIndex(key)];
  if(!tab) return;
  _activePdfTab = key;
  _savePdfTabs();
  renderPdfTabs();
  document.getElementById('pdf-view-title').textContent = tab.label;
  const exportLink = document.getElementById('pdf-export-link');
  const banner = document.getElementById('pdf-ffd-banner');
  // One blob URL at a time, for whichever tab is showing.
  if(_pdfObjectUrl){ URL.revokeObjectURL(_pdfObjectUrl); _pdfObjectUrl = null; }
  if(tab.kind === 'company'){
    // The same object the tab holds, so acknowledging updates both at once.
    _currentCompanyDoc = tab.meta;
    banner.style.display = 'none';
    // Company documents are read-in-app only: no Download/Print/Share, and
    // no blob URL minted for them either.
    _setPdfActions(false);
    paintCompanyDocAckBar();
  } else {
    _currentCompanyDoc = null;
    document.getElementById('pdf-ack-bar').style.display = 'none';
    // Export uses a blob URL (fine for downloads); the inline VIEW uses
    // PDF.js on canvas, since iOS Safari routinely refuses to render a PDF
    // inside an iframe at all and kicks out to the system viewer instead.
    _pdfObjectUrl = URL.createObjectURL(new Blob([tab.bytes], {type:'application/pdf'}));
    _setPdfActions(true);
    // Soft gate — viewing always works; Download locks until FFD is signed.
    // Print and Share stay available: both are ways of READING the document,
    // which the gate has never blocked.
    if(tab.meta.fitForDuty){
      exportLink.href = _pdfObjectUrl;
      exportLink.download = tab.meta.exportName;
      exportLink.style.opacity = '';
      exportLink.onclick = null;
      banner.style.display = 'none';
    } else {
      exportLink.removeAttribute('href');
      exportLink.style.opacity = '0.4';
      exportLink.onclick = (e) => { e.preventDefault(); showToast('Sign Fit for Duty first to export'); };
      banner.style.display = '';
    }
  }
  try {
    // .slice() is load-bearing: PDF.js takes ownership of the buffer it is
    // handed and detaches it, which would leave this tab holding an empty
    // array the second time it is selected.
    await renderPdfInline(tab.bytes.slice());
  } catch(e) {
    document.getElementById('pdf-pages').innerHTML = '<p style="color:#fff;padding:20px;">Failed to render this PDF: ' + e + '</p>';
  }
}

function closePdfTab(key){
  const i = _pdfTabIndex(key);
  if(i < 0) return;
  _pdfTabs.splice(i, 1);
  _savePdfTabs();
  if(key !== _activePdfTab){ renderPdfTabs(); return; }
  _pdfRenderToken++; // whatever is rendering belongs to a tab that is gone
  if(!_pdfTabs.length){
    _activePdfTab = null;
    renderPdfTabs();
    closePdfView();
    return;
  }
  activatePdfTab(_pdfTabs[Math.min(i, _pdfTabs.length - 1)].key);
}

async function viewDoc(kind, label){
  const data = await ensureRelease();
  if(!data) return;
  const field = {rls:'rls_pdf_b64', fi:'fi_pdf_b64', fil:'fil_pdf_b64', wb:'wb_pdf_b64', weather:'weather_pdf_b64', notams:'notams_pdf_b64', field_report:'field_report_pdf_b64'}[kind];
  const b64 = data[field];
  if(!b64){ showToast(label + ' not available in this release'); return; }
  await openPdfTab({
    key: 'rel:' + kind,
    label: label,
    kind: 'release',
    bytes: b64ToBytes(b64),
    meta: {
      fitForDuty: !!data.fit_for_duty,
      exportName: kind === 'rls' ? data.filename
                : (data.filename || 'release.pdf').replace('-RLS.pdf', '-' + kind.toUpperCase() + '.pdf'),
    },
  });
}

// Leaves the viewer without discarding the tabs — reopening any document
// brings the whole set back, which is the point of having them. Only
// closing the last tab tears the viewer down.
function closePdfView(){
  _pdfRenderToken++; // cancel any render still in flight
  _teardownPdfObserver();
  document.getElementById('pdf-pages').innerHTML = '';
  if(_pdfObjectUrl){ URL.revokeObjectURL(_pdfObjectUrl); _pdfObjectUrl = null; }
  // pdf-view is shared: a release PDF backs out to All Commands, but a
  // company doc came from the Docs tab — and All Commands is leg-scoped,
  // so on /docs (no leg) backing out there lands on an empty view.
  const active = _pdfTabs[_pdfTabIndex(_activePdfTab)];
  const wasCompanyDoc = active ? active.kind === 'company' : !!_currentCompanyDoc;
  _currentCompanyDoc = null;
  showView(wasCompanyDoc ? 'doclocker' : 'allcommands');
}
async function showMotLog(){
  showView('motlog');
  const body = document.getElementById('motlog-body');
  showLoading(body);
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
        hdr.textContent = 'Day ' + _dayCalendarNumber({legs: [leg], duty_day: leg.duty_day});
        body.appendChild(hdr);
      }
      const row = document.createElement('div');
      row.className = 'doc-row';
      row.style.cssText = 'display:block;';
      const signedText = leg.generated
        ? (leg.fit_for_duty
            ? ('FFD signed' + (leg.signed_by ? (' by ' + leg.signed_by) : '') + (leg.signed_at ? (' at ' + new Date(leg.signed_at).toLocaleString()) : ''))
            : 'Generated — FFD not signed')
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
// Same "Signed by <user> at HH:MMZ" + Re-sign markup _sign_row_html()
// renders server-side — kept in sync so a fresh sign updates the row
// in place (no reload needed) with exactly what a reload would show.
const _SIGN_ROW_IDS = {ffd: 'ffd-doc-check', eflightplan: 'sign-check'};
function _applySignedRow(kind, signedBy, signedAtIso){
  const desc = document.getElementById(kind + '-desc');
  const action = document.getElementById(kind + '-primary-action');
  if(!desc || !action) return;
  const d = new Date(signedAtIso);
  const hhmm = String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
  desc.innerHTML = signedBy
    ? ('Signed by <b>' + signedBy.replace(/</g, '&lt;') + '</b> at <b>' + hhmm + '</b>Z')
    : ('Signed at <b>' + hhmm + '</b>Z');
  const elemId = _SIGN_ROW_IDS[kind] || (kind + '-doc-check');
  // Template literal deliberately, not quote-concatenation — this file's
  // standing rule (a bare \' inside a Python triple-quoted string silently
  // collapses to a literal ', breaking a JS string boundary) makes nested
  // single/double quoting in these onclick attributes an easy way to
  // corrupt this exact script block.
  action.innerHTML =
    `<svg id="${elemId}" class="check signed" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="openSignPad('${kind}')"><path d="M20 6L9 17l-5-5"/></svg>` +
    `<a href="#" class="resign-link" onclick="openSignPad('${kind}');return false;">Re-sign</a>`;
}
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
    _applySignedRow(_signKind, data.signed_by, data.signed_at);
    if(_signKind === 'ffd'){
      // The signature itself always persists server-side (sign_leg sets
      // fit_for_duty=True unconditionally) — the "signature doesn't save,
      // downloads stay locked" reports were this stale client cache: once
      // a release had been generated/viewed BEFORE signing, _releaseCache
      // (and the Confirm view's already-rendered download links) kept
      // serving that pre-sign fit_for_duty=false forever, since nothing
      // ever invalidated them after a later sign. Dropping the cache and
      // re-pulling the release (server-side still instant — it's already
      // cached, this just re-reads the now-true flag) unlocks downloads
      // immediately instead of requiring a page reload to notice.
      //
      // Only re-pull when this leg actually HAS a release — generateRelease()
      // is the explicit "generate" path and now refuses (409) when SimBrief
      // isn't holding this flight, so firing it after every FFD signature on a
      // leg nothing was ever generated for would just raise a confusing error.
      const hadRelease = !!_getReleaseCache();
      _setReleaseCache(null);
      // Any release tab already open still holds the pre-sign flag;
      // without this its Export stays greyed until the tab is reopened.
      _pdfTabs.forEach(t => { if(t.kind === 'release') t.meta.fitForDuty = true; });
      if(hadRelease && document.getElementById('release-user')) generateRelease();
    }
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
// Inline SVG rather than literal star/cross/triangle characters. A glyph
// renders in the system font — it ignores currentColor, so it cannot
// follow the theme, and it sits on a different baseline from the text
// beside it.
const ACTIVE_DOT_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" style="width:9px;height:9px;vertical-align:1px;color:var(--green);"><circle cx="12" cy="12" r="7"/></svg>';
function _escHtml(v){ return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
const STAR_ICON_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px;vertical-align:-2px;"><path d="M12 2.6l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5-5.8-3.1-5.8 3.1 1.1-6.5L2.6 9.4l6.5-.9z"/></svg>';
const X_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" style="width:11px;height:11px;"><path d="M6 6l12 12M18 6L6 18"/></svg>';
const CHEVRON_UP_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:11px;height:11px;"><path d="M6 15l6-6 6 6"/></svg>';
const CHEVRON_DOWN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:11px;height:11px;"><path d="M6 9l6 6 6-6"/></svg>';
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
  showLoading(body);
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
  // Same call-to-action Home carries, repeated here because this is where
  // the pilot lands after tapping it — otherwise the trail goes cold at a
  // list of trips with no indication that one of them needs signing in to.
  if(!seqs.some(s => s.active)){
    const hint = document.createElement('div');
    hint.style.cssText = 'margin:0 0 10px;padding:11px 13px;background:var(--blue);color:#fff;border-radius:8px;font-size:12.5px;line-height:1.4;';
    hint.textContent = "You're not signed in to a trip yet — open the one you're flying and tap Sign In to Trip.";
    body.appendChild(hint);
  }
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
  showLoading(body);
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
// Real calendar-day number for a duty-day header — day.duty_day is the
// sequential duty-PERIOD index (1, 2, 3, ...) and stays that way on
// purpose (it's the identifier every /generate, /sign, and edit route
// keys off), but a 24+ hour layover can swallow a whole calendar day with
// no duty at all, which never gets its own duty_day number — so labeling
// the card strictly by duty_day silently skips a day (e.g. "Day 2", "Day
// 3" when the trip is really on calendar day 2, then day 4). Each leg's
// own "da" field ("duty_day/calendar_day", e.g. "3/4") already carries
// the real calendar day the airline assigned it — same source used
// server-side by pbs_parser.sequence_calendar_days for the trip's overall
// day count.
function _dayCalendarNumber(day){
  const leg = (day.legs || [])[0];
  const da = leg && leg.da;
  if(da && da.indexOf('/') !== -1){
    const n = parseInt(da.split('/')[1], 10);
    if(!isNaN(n)) return n;
  }
  return day.duty_day;
}
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
    '<div class="dd-hdr">Day ' + _dayCalendarNumber(day) + '</div>' +
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
        if(opts.onLongPress){
          // Long-press the leg that went wrong. A plain tap already means
          // "generate this leg", so recovery needs its own gesture rather
          // than a second tap target squeezed into the row.
          //
          // Tracked by hand instead of using contextmenu: iOS fires that
          // inconsistently in a standalone web app, and a moved finger has
          // to cancel or every scroll that starts on a row opens recovery.
          let timer = null, moved = false;
          const start = () => {
            moved = false;
            timer = setTimeout(() => {
              if(moved) return;
              // Cancel the click that would otherwise follow the release.
              row.dataset.longpressed = '1';
              if(navigator.vibrate) navigator.vibrate(12);
              opts.onLongPress(day, i, leg);
            }, 500);
          };
          const cancel = () => { moved = true; clearTimeout(timer); };
          row.addEventListener('touchstart', start, {passive: true});
          row.addEventListener('touchmove', cancel, {passive: true});
          row.addEventListener('touchend', cancel);
          row.addEventListener('mousedown', start);
          row.addEventListener('mousemove', cancel);
          row.addEventListener('mouseup', cancel);
          row.addEventListener('mouseleave', cancel);
          row.addEventListener('click', (e) => {
            if(row.dataset.longpressed){ delete row.dataset.longpressed; e.stopPropagation(); }
          }, true);
        }
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
  const tripDays = seqData.days || dutyDays.length;
  const summaryBits = [
    tripDays + ' day' + (tripDays === 1 ? '' : 's'),
    totalLegs + ' leg' + (totalLegs === 1 ? '' : 's'),
    (seqData.positions || []).join('/'),
  ];
  if(seqData.active) summaryBits.unshift(ACTIVE_DOT_SVG + ' ACTIVE');
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
  // "Pick Up" alone is what pilots couldn't find when looking for where to
  // sign in to a trip — it's the company word for it, but nothing on screen
  // said "sign in". Both words now, and a line under the button saying what
  // tapping it actually does.
  const pickupNote = document.createElement('div');
  pickupNote.style.cssText = 'font-size:12px;color:var(--label);line-height:1.35;';
  if(seqData.active){
    pickupBtn.textContent = 'Close Trip';
    pickupBtn.style.cssText = 'margin:0;width:100%;background:var(--label);color:#fff;border:none;padding:10px;border-radius:5px;font-size:13.5px;font-weight:600;cursor:pointer;';
    pickupBtn.onclick = () => closeTrip(seqData.seq);
    pickupNote.textContent = 'Signed in to SEQ ' + seqData.seq + '. Closing it lets you sign in to a different trip.';
  } else {
    pickupBtn.textContent = 'Sign In to Trip (Pick Up)';
    pickupBtn.style.cssText = 'margin:0;width:100%;background:var(--blue);color:#fff;border:none;padding:12px;border-radius:5px;font-size:14.5px;font-weight:700;cursor:pointer;';
    pickupBtn.onclick = () => pickUpTrip(seqData.seq);
    pickupNote.textContent = 'Trip check-in — signs you in to SEQ ' + seqData.seq + ' and makes it your active trip.';
  }
  pickupWrap.appendChild(pickupBtn);
  pickupWrap.appendChild(pickupNote);
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
    onLongPress: (day, i, leg) => openRecovery(seqData, day, i, leg),
  });
  body.appendChild(cardsWrap);

  const recWrap = document.createElement('div');
  recWrap.style.cssText = 'padding:0 14px 14px;';
  const recBtn = document.createElement('button');
  recBtn.type = 'button';
  recBtn.className = 'docs-btn';
  recBtn.style.cssText = 'border-radius:7px;background:var(--card);color:var(--blue-dark);border:1px solid var(--blue-dark);';
  recBtn.textContent = 'Report a Disruption';
  // The long-press is the fast path; this is the discoverable one. It asks
  // which leg, because nothing about the button says which one you meant.
  recBtn.onclick = () => openRecovery(seqData, null, null, null);
  recWrap.appendChild(recBtn);
  body.appendChild(recWrap);

  // A repaired trip wears a trailing asterisk, and the pairing as
  // published is still in the list beside it — so reverting is offered on
  // the repair rather than buried as an undo.
  if(String(seqData.seq || '').endsWith('*')){
    const revWrap = document.createElement('div');
    revWrap.style.cssText = 'padding:0 14px 14px;';
    const revBtn = document.createElement('button');
    revBtn.type = 'button';
    revBtn.className = 'docs-btn';
    revBtn.style.cssText = 'border-radius:7px;background:var(--card);color:var(--label);border:1px solid var(--border);';
    revBtn.textContent = 'Revert to the Published Pairing';
    revBtn.onclick = () => revertTrip(seqData.seq);
    revWrap.appendChild(revBtn);
    body.appendChild(revWrap);
  }

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
async function revertTrip(seq){
  const base = seq.endsWith('*') ? seq.slice(0, -1) : seq;
  if(!confirm('Revert to SEQ ' + base + ' as published? The repaired version is discarded.')) return;
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/revert', {method: 'POST'});
    const data = await readJson(r);
    if(!r.ok){ showToast(data.error || 'Could not revert'); return; }
    showToast(data.restored_from_pack ? ('SEQ ' + base + ' restored from the pack')
                                      : ('Reverted to SEQ ' + base));
    myTripShowDetail(base, true);
  } catch(e) { showToast(e.message); }
}

async function pickUpTrip(seq){
  // A plain one-tap check-in — no signature. Independent of Generate &
  // Cache, which stays its own separate button: either can be done first.
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/pick-up', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not sign in to this trip'); return; }
    showToast('Signed in to SEQ ' + seq);
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
    row.innerHTML = '<a class="arow-link" href="#">' + STAR_ICON_SVG + ' ' + _libraryShortcut.label + '</a>' +
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
  showLoading(body);
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
  showLoading(body);
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
  showLoading(body);
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
  // innerHTML, not textContent, so the dot can be an icon. s.seq comes
  // from a parsed bid pack, so it is escaped rather than trusted.
  code.innerHTML = (s.active ? ACTIVE_DOT_SVG + ' ' : '') + 'SEQ ' + _escHtml(s.seq)
    + (s.active ? ' (ACTIVE \u2014 picked up)' : '');
  const desc = document.createElement('div'); desc.className = 'desc lib-routing';
  desc.innerHTML = libraryRoutingHtml(s.routing, s.layover_indices);
  left.appendChild(code); left.appendChild(desc);
  const stats = document.createElement('div'); stats.className = 'lib-stats';
  // Raw HHMM, no colon inserted — matches the RPT/RLS convention already
  // used on the duty-day cards themselves (e.g. "RPT 1705").
  const timeBits = [s.report ? ('RPT ' + s.report) : '', s.release ? ('RLS ' + s.release) : ''].filter(Boolean);
  if(timeBits.length){
    const timeLine = document.createElement('div');
    timeLine.textContent = timeBits.join(' · ');
    stats.appendChild(timeLine);
  }
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
let _librarySearch = '';
async function libraryShowSequences(sort, q){
  sort = sort || '';
  if(q !== undefined) _librarySearch = q;
  const {opr, base, fleet} = _libraryPath;
  const body = document.getElementById('library-body');
  showLoading(body);
  try {
    const params = [];
    if(sort) params.push('sort=' + encodeURIComponent(sort));
    if(_librarySearch) params.push('q=' + encodeURIComponent(_librarySearch));
    const r = await fetch('/pbs/packs/' + opr + '/' + base + '/' + fleet + '/sequences'
                          + (params.length ? ('?' + params.join('&')) : ''));
    const seqs = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (seqs.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryCrumb());
    const {pane, list} = libraryPane('Sequences', libraryShowFleets);
    // Search and sort stay on screen even with no results — otherwise a
    // search that matches nothing removes the box you would clear it in.
    list.parentNode.insertBefore(_pairingSortControl(sort, (v) => libraryShowSequences(v)), list);
    list.parentNode.insertBefore(
      _pairingSearchControl(_librarySearch, (v) => libraryShowSequences(sort, v)), list);
    if(!seqs.length){
      const none = document.createElement('p');
      none.className = 'placeholder-note';
      none.textContent = _librarySearch
        ? ('Nothing matches \u201c' + _librarySearch + '\u201d in this pack.')
        : 'No sequences in this pack.';
      list.appendChild(none);
    }
    seqs.forEach(s => list.appendChild(sequenceListRow(s, () => libraryShowSequenceDetail(s.seq))));
    body.appendChild(pane);
    const inp = pane.querySelector('.pairing-search input');
    // Typing re-fetches and rebuilds the pane, so put the caret back.
    if(inp && _librarySearch){ inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
async function libraryShowSequenceDetail(seqNumber){
  const {opr, base, fleet} = _libraryPath;
  const body = document.getElementById('library-body');
  showLoading(body);
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

// Bid Layers — saved bids over the Pairing Library, for sorting through
// hundreds of sequences instead of scrolling all of them. Each saved bid
// scopes to opr/base/fleet and holds an ordered stack of single-criterion
// LAYERS (see _CRITERION_FIELDS below and _criterion_matches() in
// server.py). Every route lives under /pbs/layers. Reuses libraryPane/
// libraryGroupRow/sequenceListRow/renderDutyDayCards — only the list/form/
// matches rendering here is new.
let _layersLoaded = false;
async function layerShowList(){
  const body = document.getElementById('layers-body');
  showLoading(body);
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
// One saved bid in the list. No ordering controls and no "Layer N" label
// here on purpose: the numbered, reorderable layers live INSIDE a bid
// (its criterion stack), and priority only means anything there. Saved
// bids are just independent saved searches — ordering them would imply a
// precedence between whole bids that nothing in this app acts on.
function layerRow(layer){
  const row = document.createElement('div');
  row.className = 'doc-row lib-row';
  const left = document.createElement('div');
  const code = document.createElement('div'); code.className = 'code'; code.textContent = layer.name;
  const desc = document.createElement('div'); desc.className = 'desc';
  const layerCount = (layer.criteria || _criteriaFromLegacyProperties(layer.properties)).length;
  desc.textContent = layer.opr + ' ' + layer.base + '/' + layer.fleet
    + ' · ' + layerCount + ' layer' + (layerCount === 1 ? '' : 's');
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
// 5-minute HHMM steps (0000..2355) — same "round-number dropdown, not a
// free-typed field" reasoning as _rangeOptionsHtml, just clock-valued.
// 5 minutes rather than 15: a real cutoff a pilot actually wants to bid
// (e.g. "released before 2240") is routinely off a quarter-hour grid, and
// a coarser step silently makes their own stated cutoff unselectable.
// 288 options is fine for the native scroll-wheel picker this renders as
// on iOS.
function _timeOptionsHtml(current){
  const cur = (current === null || current === undefined || current === '') ? '' : String(current).padStart(4, '0');
  let html = '<option value=""' + (cur === '' ? ' selected' : '') + '>Any</option>';
  let seen = cur === '';
  for(let h = 0; h < 24; h++){
    for(let m = 0; m < 60; m += 5){
      const v = String(h).padStart(2, '0') + String(m).padStart(2, '0');
      const label = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
      if(v === cur) seen = true;
      html += '<option value="' + v + '"' + (v === cur ? ' selected' : '') + '>' + label + '</option>';
    }
  }
  // A saved value off the 5-minute grid (from an older save, or any API
  // caller) gets its own option rather than silently rendering as "Any" —
  // which would look like the layer had no cutoff at all.
  if(!seen){
    html += '<option value="' + cur + '" selected>' + _hhmmLabel(cur) + '</option>';
  }
  return html;
}
// A bid layer is an ORDERED STACK of single-criterion layers, the way a
// real PBS bid reads — "Layer 1: 1 day / Layer 2: min block 7 hours /
// Layer 3: include STT / Layer 4: release before 2240". Each row states
// exactly one thing; the running count beside each row shows how many
// pairings survive down to that layer, which is the whole reason the
// layers are numbered rather than being one flat form of every filter.
const _CRITERION_FIELDS = [
  {v: 'days',             label: 'Days',            kind: 'num',  range: [1, 10, 1]},
  {v: 'block',            label: 'Block Hours',     kind: 'num',  range: [0, 60, 1]},
  {v: 'tafb',             label: 'TAFB Hours',      kind: 'num',  range: [0, 150, 5]},
  {v: 'tpay',             label: 'TPAY Hours',      kind: 'num',  range: [0, 60, 1]},
  {v: 'legs_per_day',     label: 'Legs Per Day',    kind: 'num',  range: [1, 6, 1]},
  {v: 'report',           label: 'Report Time',     kind: 'time'},
  {v: 'release',          label: 'Release Time',    kind: 'time'},
  {v: 'red_eye',          label: 'Red-Eyes',        kind: 'mode'},
  {v: 'layover_include',  label: 'Layover At',      kind: 'stations', ph: 'e.g. MIA, LAX'},
  {v: 'include_stations', label: 'Include Station', kind: 'stations', ph: 'e.g. STT'},
  {v: 'avoid_stations',   label: 'Avoid Station',   kind: 'stations', ph: 'e.g. ORD'},
  // Matched by prefix server-side, so 320 covers 320S/320N/320D rather than
  // making the pilot list every sub-fleet code.
  {v: 'include_ac',       label: 'Include AC Code', kind: 'stations', ph: 'e.g. 320S, 738'},
  {v: 'avoid_ac',         label: 'Ignore AC Code',  kind: 'stations', ph: 'e.g. E45X'},
];
const _CRITERION_OPS = {
  num:  [['min', 'at least'], ['max', 'at most'], ['exact', 'exactly']],
  time: [['max', 'before'], ['min', 'after'], ['exact', 'exactly']],
};
// One box for station, AC code, route (LAX-JFK), sequence number and flight
// number, because someone hunting a pairing knows the string, not which
// field it belongs to. Debounced — every keystroke otherwise re-filters a
// pack of several thousand sequences server-side.
function _pairingSearchControl(value, onChange){
  const wrap = document.createElement('div');
  wrap.className = 'pairing-search';
  const input = document.createElement('input');
  input.type = 'search';
  input.value = value || '';
  input.placeholder = 'Search station, AC code, LAX-JFK, SEQ, flight\u2026';
  input.setAttribute('aria-label', 'Search pairings');
  let t = null;
  input.oninput = () => {
    clearTimeout(t);
    t = setTimeout(() => onChange(input.value.trim()), 300);
  };
  // Enter applies immediately rather than waiting out the debounce.
  input.onkeydown = (e) => { if(e.key === 'Enter'){ clearTimeout(t); onChange(input.value.trim()); } };
  wrap.appendChild(input);
  return wrap;
}

function _criterionField(v){ return _CRITERION_FIELDS.find(f => f.v === v) || _CRITERION_FIELDS[0]; }
function _hhmmLabel(v){
  const s = String(v).padStart(4, '0');
  return s.slice(0, 2) + ':' + s.slice(2);
}
// One layer as a single readable line — "Block Hours at least 7",
// "Release Time before 22:40", "Include Station STT". Used by the funnel
// readout so each row names the criterion that trimmed the count.
function _criterionLabel(c){
  const f = _criterionField(c && c.field);
  if(f.kind === 'mode'){
    return f.label + ': ' + ({any: 'Any', exclude: 'Exclude', only: 'Only'}[(c || {}).value] || 'Any');
  }
  if(f.kind === 'stations'){
    return f.label + ' ' + (((c || {}).value || []).join('/') || '—');
  }
  const op = (_CRITERION_OPS[f.kind] || []).find(o => o[0] === ((c || {}).op || 'min'));
  const raw = (c || {}).value;
  const shown = (raw === '' || raw === null || raw === undefined)
    ? '—' : (f.kind === 'time' ? _hhmmLabel(raw) : raw);
  return f.label + ' ' + (op ? op[1] : '') + ' ' + shown;
}
// A layer saved under the old flat-properties shape opens as the
// equivalent stack, so editing one isn't a start-over. Mirrors
// _layer_matches's own field list server-side, in a stable order.
function _criteriaFromLegacyProperties(p){
  if(!p) return [];
  const out = [];
  const num = (field, op, v) => { if(v !== null && v !== undefined && v !== '') out.push({field, op, value: v}); };
  num('days', 'min', p.min_days);          num('days', 'max', p.max_days);
  num('block', 'min', p.min_block);        num('block', 'max', p.max_block);
  num('tafb', 'min', p.min_tafb);          num('tafb', 'max', p.max_tafb);
  num('tpay', 'min', p.min_tpay);          num('tpay', 'max', p.max_tpay);
  num('report', 'min', p.min_report);      num('report', 'max', p.max_report);
  num('release', 'min', p.min_release);    num('release', 'max', p.max_release);
  num('legs_per_day', 'max', p.max_legs_per_day);
  const mode = p.red_eye || (p.exclude_red_eye ? 'exclude' : null);
  if(mode && mode !== 'any') out.push({field: 'red_eye', value: mode});
  if((p.layover_include || []).length) out.push({field: 'layover_include', value: p.layover_include});
  if((p.include_stations || []).length) out.push({field: 'include_stations', value: p.include_stations});
  if((p.avoid_stations || []).length) out.push({field: 'avoid_stations', value: p.avoid_stations});
  return out;
}
async function layerShowForm(existing){
  const body = document.getElementById('layers-body');
  body.innerHTML = '';
  body.appendChild(libraryBackLink('Back to Bid Layers', layerShowList));
  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.innerHTML =
    '<label>Bid Name (only needed to Save)</label>' +
    '<input class="lf-name" type="text" placeholder="Build your stack freely — name it when you’re ready to save" value="' + (existing ? existing.name.replace(/"/g, '&quot;') : '') + '">' +
    // Operator/Base/Fleet are always editable, even on a saved bid — a real
    // bid sometimes needs rescoping to a different fleet/base (or widening
    // to ALL) without recreating it and losing its layers.
    '<label>Operator</label><select class="lf-opr"></select>' +
    '<label>Base</label><select class="lf-base"></select>' +
    '<label>Fleet</label><select class="lf-fleet"></select>' +
    '<label style="margin-top:16px;">Layers (each one criterion, in priority order)</label>' +
    '<div class="lf-stack"></div>' +
    '<button type="button" class="lf-add" style="margin-top:8px;width:100%;background:transparent;color:var(--blue);border:1px dashed var(--border);">+ Add Layer</button>' +
    '<div class="lf-live-count" style="margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--bg);font-size:13px;font-weight:600;color:var(--label);"></div>' +
    '<div style="display:flex;gap:8px;margin-top:14px;">' +
      (existing ? '<button type="button" class="lf-delete" style="margin:0;flex:1;background:var(--red);">Delete</button>' : '') +
      '<button type="button" class="lf-save" style="margin:0;flex:2;">Save</button>' +
    '</div>' +
    '<div class="lf-msg" style="margin-top:8px;font-size:12.5px;color:var(--label);"></div>';
  body.appendChild(panel);

  const criteria = (existing && existing.criteria)
    ? existing.criteria.map(c => ({...c}))
    : _criteriaFromLegacyProperties(existing && existing.properties);

  const oprSel = panel.querySelector('.lf-opr');
  const baseSel = panel.querySelector('.lf-base');
  const fleetSel = panel.querySelector('.lf-fleet');
  const packsResp = await fetch('/pbs/packs');
  const packs = await packsResp.json();
  // (ALL) is always the first option at every level — picking it drops
  // that dimension from the scope entirely (server-side: ALL_SCOPE), so a
  // bid can span every base for one operator, or genuinely every pack
  // there is, instead of being pinned to one exact pack.
  const fillSelect = (sel, values, selectedVal) => {
    sel.innerHTML = '<option value="ALL">(ALL)</option>' + values.map(v => '<option value="' + v + '">' + v + '</option>').join('');
    if(selectedVal && [...sel.options].some(o => o.value === selectedVal)) sel.value = selectedVal;
  };
  const oprs = [...new Set(packs.map(pk => pk.opr))].sort();
  fillSelect(oprSel, oprs, existing && existing.opr);
  const refreshBases = (preselect) => {
    const scoped = oprSel.value === 'ALL' ? packs : packs.filter(pk => pk.opr === oprSel.value);
    fillSelect(baseSel, [...new Set(scoped.map(pk => pk.base))].sort(), preselect);
    refreshFleets(preselect ? (existing && existing.fleet) : null);
  };
  const refreshFleets = (preselect) => {
    let scoped = oprSel.value === 'ALL' ? packs : packs.filter(pk => pk.opr === oprSel.value);
    scoped = baseSel.value === 'ALL' ? scoped : scoped.filter(pk => pk.base === baseSel.value);
    fillSelect(fleetSel, [...new Set(scoped.map(pk => pk.fleet))].sort(), preselect);
    queuePreview();
  };
  oprSel.onchange = () => refreshBases(null);
  baseSel.onchange = () => refreshFleets(null);
  const getScope = () => ({opr: oprSel.value, base: baseSel.value, fleet: fleetSel.value});

  const stackEl = panel.querySelector('.lf-stack');
  const countEl = panel.querySelector('.lf-live-count');
  let funnel = [];

  function criterionRow(c, i){
    const row = document.createElement('div');
    row.className = 'lf-layer-row';
    const hdr = document.createElement('div');
    hdr.className = 'lf-layer-hdr';
    const num = document.createElement('span');
    num.className = 'lf-layer-num';
    num.textContent = 'Layer ' + (i + 1);
    const cnt = document.createElement('span');
    cnt.className = 'lf-layer-count';
    const tools = document.createElement('span');
    tools.className = 'lf-layer-tools';
    const toolBtn = (svg, label, disabled, fn) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'layer-move-btn'; b.innerHTML = svg;
      b.setAttribute('aria-label', label); b.title = label;
      b.disabled = disabled; b.onclick = fn;
      return b;
    };
    tools.appendChild(toolBtn(CHEVRON_UP_SVG, 'Move up', i === 0, () => {
      [criteria[i - 1], criteria[i]] = [criteria[i], criteria[i - 1]];
      renderStack(); queuePreview();
    }));
    tools.appendChild(toolBtn(CHEVRON_DOWN_SVG, 'Move down', i === criteria.length - 1, () => {
      [criteria[i + 1], criteria[i]] = [criteria[i], criteria[i + 1]];
      renderStack(); queuePreview();
    }));
    tools.appendChild(toolBtn(X_ICON_SVG, 'Remove layer', false, () => {
      criteria.splice(i, 1);
      renderStack(); queuePreview();
    }));
    hdr.appendChild(num); hdr.appendChild(cnt); hdr.appendChild(tools);
    row.appendChild(hdr);

    const controls = document.createElement('div');
    controls.className = 'lf-layer-controls';
    const fieldSel = document.createElement('select');
    fieldSel.innerHTML = _CRITERION_FIELDS.map(f => '<option value="' + f.v + '">' + f.label + '</option>').join('');
    fieldSel.value = c.field || 'days';
    fieldSel.onchange = () => {
      const f = _criterionField(fieldSel.value);
      // Switching field resets this layer's value — an op/value from the
      // previous field (a station list under "Block Hours", say) would
      // never match anything and reads as a broken layer.
      criteria[i] = {
        field: f.v,
        op: f.kind === 'time' ? 'max' : 'min',
        value: f.kind === 'stations' ? [] : (f.kind === 'mode' ? 'any' : ''),
      };
      renderStack(); queuePreview();
    };
    controls.appendChild(fieldSel);

    const f = _criterionField(c.field);
    if(f.kind === 'num' || f.kind === 'time'){
      const opSel = document.createElement('select');
      opSel.innerHTML = _CRITERION_OPS[f.kind].map(o => '<option value="' + o[0] + '">' + o[1] + '</option>').join('');
      opSel.value = c.op || (f.kind === 'time' ? 'max' : 'min');
      opSel.onchange = () => { criteria[i].op = opSel.value; queuePreview(); };
      controls.appendChild(opSel);
      const valSel = document.createElement('select');
      valSel.innerHTML = f.kind === 'time'
        ? _timeOptionsHtml(c.value)
        : _rangeOptionsHtml(_rangeArray(f.range[0], f.range[1], f.range[2]), c.value);
      valSel.onchange = () => { criteria[i].value = valSel.value; queuePreview(); };
      controls.appendChild(valSel);
    } else if(f.kind === 'mode'){
      const modeSel = document.createElement('select');
      modeSel.innerHTML = '<option value="any">Any</option><option value="exclude">Exclude Red-Eyes</option><option value="only">Only Red-Eyes</option>';
      modeSel.value = c.value || 'any';
      modeSel.onchange = () => { criteria[i].value = modeSel.value; queuePreview(); };
      controls.appendChild(modeSel);
    } else {
      const txt = document.createElement('input');
      txt.type = 'text';
      txt.placeholder = f.ph || 'e.g. STT';
      txt.value = (c.value || []).join(', ');
      txt.oninput = () => { criteria[i].value = _stationList(txt.value); queuePreview(); };
      controls.appendChild(txt);
    }
    row.appendChild(controls);
    return row;
  }

  function renderStack(){
    stackEl.innerHTML = '';
    if(!criteria.length){
      const empty = document.createElement('p');
      empty.className = 'placeholder-note';
      empty.style.cssText = 'padding:10px 2px;margin:0;';
      empty.textContent = 'No layers yet — add one to start narrowing this pack.';
      stackEl.appendChild(empty);
      return;
    }
    criteria.forEach((c, i) => stackEl.appendChild(criterionRow(c, i)));
    paintFunnel();
  }
  // Counts are painted onto the existing rows rather than re-rendering the
  // stack — a re-render mid-preview would destroy and rebuild the station
  // text input the pilot is actively typing into, losing focus every time
  // the debounce fires.
  function paintFunnel(){
    stackEl.querySelectorAll('.lf-layer-count').forEach((el, i) => {
      el.textContent = funnel.length > i ? (funnel[i] + ' match') : '';
      el.style.color = (funnel.length > i && funnel[i] === 0) ? 'var(--red)' : '';
    });
  }

  let previewTimer = null, previewSeq = 0;
  function queuePreview(){
    clearTimeout(previewTimer);
    countEl.textContent = 'Checking…';
    previewTimer = setTimeout(async () => {
      const mySeq = ++previewSeq;
      const scope = getScope();
      if(!scope.opr || !scope.base || !scope.fleet){ countEl.textContent = ''; return; }
      try {
        const resp = await fetch('/pbs/layers/preview', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({opr: scope.opr, base: scope.base, fleet: scope.fleet, criteria}),
        });
        const data = await resp.json();
        if(mySeq !== previewSeq) return; // a newer edit already superseded this request
        if(!resp.ok){ countEl.textContent = data.error || 'Could not check'; return; }
        funnel = data.funnel || [];
        countEl.textContent = criteria.length
          ? (data.count + ' of ' + data.total_in_scope + ' pairings match all ' + criteria.length + ' layer' + (criteria.length === 1 ? '' : 's'))
          : (data.total_in_scope + ' pairings in scope — add a layer to narrow it');
        paintFunnel();
      } catch(e) {
        if(mySeq === previewSeq) countEl.textContent = '';
      }
    }, 400);
  }

  panel.querySelector('.lf-add').onclick = () => {
    criteria.push({field: 'days', op: 'exact', value: ''});
    renderStack(); queuePreview();
  };
  renderStack();
  refreshBases(existing && existing.base);

  const msgEl = panel.querySelector('.lf-msg');
  panel.querySelector('.lf-save').onclick = () => layerSaveForm(panel, existing, criteria, msgEl);
  if(existing){
    panel.querySelector('.lf-delete').onclick = () => layerDeleteLayer(existing.id);
  }
}
function _stationList(v){ return (v || '').split(',').map(s => s.trim().toUpperCase()).filter(Boolean); }
async function layerSaveForm(panel, existing, criteria, msgEl){
  const name = panel.querySelector('.lf-name').value.trim();
  if(!name){ msgEl.textContent = 'Name is required.'; msgEl.style.color = 'var(--red)'; return; }
  const opr = panel.querySelector('.lf-opr').value;
  const base = panel.querySelector('.lf-base').value;
  const fleet = panel.querySelector('.lf-fleet').value;
  if(!opr || !base || !fleet){ msgEl.textContent = 'Pick an operator/base/fleet.'; msgEl.style.color = 'var(--red)'; return; }
  try {
    let r;
    if(existing){
      r = await fetch('/pbs/layers/' + existing.id, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, opr, base, fleet, criteria}),
      });
    } else {
      r = await fetch('/pbs/layers', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, opr, base, fleet, criteria}),
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
// Shared by the Bid Layers pairings list and the Pairing Library's own
// Sequences list — same options, same look, same query-param contract
// against _sort_summaries() in server.py.
const _PAIRING_SORT_OPTIONS = [
  ['', 'Default'],
  ['-block', 'Block (high→low)'], ['block', 'Block (low→high)'],
  ['-tafb', 'TAFB (high→low)'], ['tafb', 'TAFB (low→high)'],
  ['-days', 'Days (high→low)'], ['days', 'Days (low→high)'],
  ['report', 'Report Time (earliest)'], ['-report', 'Report Time (latest)'],
  ['release', 'Release Time (earliest)'], ['-release', 'Release Time (latest)'],
  ['-dacv', 'DACV — Block/Day (high→low)'], ['dacv', 'DACV — Block/Day (low→high)'],
  ['seq', 'SEQ'],
];
// Styled to match .panel select (server.py's global form-select rule)
// rather than a bare unstyled <select>, which read as a stray native
// control that didn't match the rest of the app's UI.
function _pairingSortControl(sort, onChange){
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;';
  const label = document.createElement('span');
  label.textContent = 'Sort';
  label.style.cssText = 'font-size:12.5px;color:var(--label);font-weight:600;flex-shrink:0;';
  const sel = document.createElement('select');
  sel.style.cssText = 'flex:1;font-family:inherit;font-size:13.5px;padding:9px 10px;border:1px solid var(--border);border-radius:5px;box-sizing:border-box;background:var(--card);color:var(--value);';
  sel.innerHTML = _PAIRING_SORT_OPTIONS.map(([v, lbl]) => '<option value="' + v + '"' + (v === (sort || '') ? ' selected' : '') + '>' + lbl + '</option>').join('');
  sel.onchange = () => onChange(sel.value);
  wrap.appendChild(label); wrap.appendChild(sel);
  return wrap;
}
let _layerSearch = '';
async function layerShowPairings(layer, sort, q){
  sort = sort || '';
  if(q !== undefined) _layerSearch = q;
  const body = document.getElementById('layers-body');
  showLoading(body);
  try {
    const params = [];
    if(sort) params.push('sort=' + encodeURIComponent(sort));
    if(_layerSearch) params.push('q=' + encodeURIComponent(_layerSearch));
    const r = await fetch('/pbs/layers/' + layer.id + '/pairings'
                          + (params.length ? ('?' + params.join('&')) : ''));
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
    body.innerHTML = '';
    body.appendChild(libraryBackLink('Back to Bid Layers', layerShowList));
    const editBtn = document.createElement('button');
    editBtn.className = 'lib-bar-action';
    editBtn.textContent = 'Edit';
    editBtn.onclick = (e) => { e.stopPropagation(); layerShowForm(layer); };
    const {pane, list} = libraryPane(layer.name + ' (' + data.pairings.length + ')', layerShowList, editBtn);
    // The funnel is the payoff for numbering the layers — when a bid comes
    // back empty (or thinner than expected), this says which layer did it
    // rather than leaving the pilot to bisect their own stack by hand.
    const criteria = layer.criteria || [];
    if(criteria.length && (data.funnel || []).length === criteria.length){
      const funnelEl = document.createElement('div');
      funnelEl.style.cssText = 'padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;color:var(--label);line-height:1.7;';
      funnelEl.appendChild(Object.assign(document.createElement('div'), {
        textContent: data.total_in_scope + ' in scope',
        style: 'font-weight:600;',
      }));
      criteria.forEach((c, i) => {
        const line = document.createElement('div');
        const n = data.funnel[i];
        const dropped = i === 0 ? (data.total_in_scope - n) : (data.funnel[i - 1] - n);
        line.textContent = 'Layer ' + (i + 1) + ' · ' + _criterionLabel(c) + ' → ' + n
          + (dropped > 0 ? ('  (−' + dropped + ')') : '');
        if(n === 0) line.style.color = 'var(--red)';
        funnelEl.appendChild(line);
      });
      list.parentNode.insertBefore(funnelEl, list);
    }
    // Both controls stay up even with no results — a search that matches
    // nothing must not remove the box you would clear it in.
    list.parentNode.insertBefore(_pairingSortControl(sort, (v) => layerShowPairings(layer, v)), list);
    list.parentNode.insertBefore(
      _pairingSearchControl(_layerSearch, (v) => layerShowPairings(layer, sort, v)), list);
    if(!data.pairings.length){
      const none = document.createElement('p');
      none.className = 'placeholder-note';
      none.style.padding = '14px';
      // Distinguish "your stack excludes everything" from "your search does",
      // since the fix is different in each case.
      none.textContent = _layerSearch
        ? ('No match for \u201c' + _layerSearch + '\u201d among this bid\u2019s pairings.')
        : 'Nothing matches every layer in this bid.';
      list.appendChild(none);
    }
    data.pairings.forEach(s => list.appendChild(sequenceListRow(s, () => layerShowSequenceDetail(layer, s))));
    body.appendChild(pane);
    const inp = pane.querySelector('.pairing-search input');
    if(inp && _layerSearch){ inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
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
  showLoading(body);
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

// ---------------------------------------------------------------------
// Trip recovery. Reached by long-pressing the leg that went wrong in My
// Trip, or from Report a Disruption on the same screen.
//
// The pilot is asked two things only: what happened, and when. Everything
// else is derived — where they are follows from the disruption type, and
// whether the duty clock is running is read from FFD rather than asked,
// because the pilot should not have to know that it changes the answer.
// ---------------------------------------------------------------------
let _recSeq = null, _recDay = null, _recLegIndex = null, _recKind = 'late_departure';
let _recAt = '', _recStation = '';

const _REC_KINDS = [
  ['late_departure', 'Late Departure', 'Still at the gate, going later'],
  ['late_arrival',   'Late Arrival',   'Landed where planned, but late'],
  ['diverted',       'Diverted',       'Landed somewhere else'],
  ['cancelled',      'Cancelled',      'This leg is not operating'],
];

function openRecovery(seqData, day, legIndex, leg){
  _recSeq = seqData;
  _recDay = day ? day.duty_day : null;
  _recLegIndex = (legIndex === null || legIndex === undefined) ? null : legIndex;
  _recKind = 'late_departure';
  showView('recovery');
  renderRecovery();
}

function closeRecovery(){
  if(_recSeq) myTripShowDetail(_recSeq.seq, true);
  else showView('pairing');
}

function _recLeg(){
  if(!_recSeq || _recDay === null || _recLegIndex === null) return null;
  const day = (_recSeq.duty_days || []).find(d => d.duty_day === _recDay);
  return day ? (day.legs || [])[_recLegIndex] : null;
}

function renderRecovery(){
  const body = document.getElementById('recovery-body');
  body.innerHTML = '';

  // Step 1 — which leg. Skipped when a long-press already said.
  const legPanel = document.createElement('div');
  legPanel.className = 'panel';
  const legHdr = document.createElement('div');
  legHdr.className = 'panel-card-hdr';
  legHdr.textContent = 'Disrupted Leg';
  legPanel.appendChild(legHdr);
  (_recSeq.duty_days || []).forEach(day => {
    (day.legs || []).forEach((leg, i) => {
      const row = document.createElement('div');
      row.className = 'doc-row';
      row.style.cursor = 'pointer';
      const chosen = day.duty_day === _recDay && i === _recLegIndex;
      if(chosen) row.style.background = 'var(--bg)';
      const left = document.createElement('div');
      const code = document.createElement('div');
      code.className = 'code';
      code.textContent = 'DAY ' + day.duty_day + '   ' + (leg.flight_number || '----')
        + '  ' + leg.origin + '-' + leg.destination;
      const desc = document.createElement('div');
      desc.className = 'desc';
      desc.textContent = (leg.dep_local || '') + ' - ' + (leg.arr_local || '');
      left.appendChild(code); left.appendChild(desc);
      row.appendChild(left);
      if(chosen){
        const tick = document.createElement('div');
        tick.className = 'actions';
        tick.style.color = 'var(--blue)';
        tick.textContent = 'selected';
        tick.style.fontSize = '12px';
        row.appendChild(tick);
      }
      row.onclick = () => { _recDay = day.duty_day; _recLegIndex = i; renderRecovery(); };
      legPanel.appendChild(row);
    });
  });
  body.appendChild(legPanel);

  if(_recDay === null || _recLegIndex === null) return;

  // Step 2 — what happened, and when.
  const what = document.createElement('div');
  what.className = 'panel';
  const whatHdr = document.createElement('div');
  whatHdr.className = 'panel-card-hdr';
  whatHdr.textContent = 'What Happened';
  what.appendChild(whatHdr);
  _REC_KINDS.forEach(([v, label, hint]) => {
    const row = document.createElement('div');
    row.className = 'doc-row';
    row.style.cursor = 'pointer';
    if(v === _recKind) row.style.background = 'var(--bg)';
    const left = document.createElement('div');
    const c = document.createElement('div'); c.className = 'code'; c.textContent = label;
    const d = document.createElement('div'); d.className = 'desc'; d.textContent = hint;
    left.appendChild(c); left.appendChild(d);
    row.appendChild(left);
    row.onclick = () => { _recKind = v; renderRecovery(); };
    what.appendChild(row);
  });

  const timeWrap = document.createElement('div');
  timeWrap.style.cssText = 'padding:12px 14px;';
  const lbl = document.createElement('div');
  lbl.style.cssText = 'font-size:12.5px;color:var(--label);margin-bottom:6px;';
  lbl.textContent = {
    late_departure: 'New departure time (HHMM local)',
    late_arrival:   'Actual arrival time (HHMM local)',
    diverted:       'Actual arrival time (HHMM local)',
    cancelled:      'Available from (HHMM local)',
  }[_recKind];
  const input = document.createElement('input');
  input.type = 'text'; input.id = 'rec-at'; input.inputMode = 'numeric';
  input.maxLength = 4; input.placeholder = '1445';
  input.style.cssText = 'width:100%;box-sizing:border-box;padding:10px 12px;font-size:15px;'
    + 'font-family:var(--mono);border:1px solid var(--border);border-radius:7px;'
    + 'background:var(--bg);color:var(--value);';
  timeWrap.appendChild(lbl); timeWrap.appendChild(input);

  if(_recKind === 'diverted'){
    const sl = document.createElement('div');
    sl.style.cssText = 'font-size:12.5px;color:var(--label);margin:12px 0 6px;';
    sl.textContent = 'Where you actually landed';
    const si = document.createElement('input');
    si.type = 'text'; si.id = 'rec-station'; si.maxLength = 4; si.placeholder = 'DFW';
    si.style.cssText = input.style.cssText + 'text-transform:uppercase;';
    timeWrap.appendChild(sl); timeWrap.appendChild(si);
  }

  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'docs-btn';
  go.style.cssText = 'margin-top:12px;border-radius:7px;';
  go.textContent = 'Find Options';
  go.onclick = submitRecovery;
  timeWrap.appendChild(go);
  what.appendChild(timeWrap);
  body.appendChild(what);

  const results = document.createElement('div');
  results.id = 'rec-results';
  body.appendChild(results);
}

// A response that isn't JSON is the failure mode this app keeps hitting —
// a proxy error page, a redirect to the login form, a route that isn't
// deployed yet. response.json() then throws the browser's own opaque
// message ("The string did not match the expected pattern." on WebKit,
// "Unexpected token '<'" on Chrome), which says nothing about what
// actually went wrong. This reads the body as text first and reports the
// status instead, so the next occurrence is diagnosable from the screen.
async function readJson(r){
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch(e) {
    const snippet = text.replace(/<[^>]*>/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, 120);
    throw new Error('Server returned ' + r.status + ' ' + (r.statusText || '')
                    + (snippet ? (' \\u2014 ' + snippet) : ' with a non-JSON response'));
  }
}

// The pilot builds the repair, one leg at a time. Nothing here ranks or
// chooses: the server says what the rules allow from where they are
// standing, and every step of the trip is a tap. `_recPicks` is the whole
// state — each entry says which network leg and whether it was taken
// after a rest, so Back is just dropping the last one.
let _recPicks = [];
let _recStep = null;

async function submitRecovery(){
  const at = (document.getElementById('rec-at').value || '').replace(/[^0-9]/g, '');
  if(at.length !== 4){ showToast('Enter a time as HHMM'); return; }
  _recAt = at;
  _recStation = (document.getElementById('rec-station') || {}).value || '';
  _recPicks = [];
  await loadRecoveryStep();
}

async function loadRecoveryStep(){
  const results = document.getElementById('rec-results');
  results.innerHTML = '';
  showLoading(results);
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(_recSeq.seq) + '/recover-step', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: _recDay, leg_index: _recLegIndex, kind: _recKind,
                            at: _recAt, station: _recStation, picks: _recPicks}),
    });
    const data = await readJson(r);
    if(!r.ok){ results.innerHTML = '<p class="placeholder-note">' + (data.error || 'Could not read your options') + '</p>'; return; }
    _recStep = data;
    renderRecoveryStep(data);
  } catch(e){
    results.innerHTML = '<p class="placeholder-note">' + e.message + '</p>';
  }
}

function _recOptionRow(o, label, onPick){
  const row = document.createElement('div');
  row.className = 'doc-row';
  row.style.cursor = 'pointer';
  const left = document.createElement('div');
  const code = document.createElement('div');
  code.className = 'code';
  code.textContent = (o.flight_number || '----') + '   ' + o.origin + '-' + o.destination;
  const desc = document.createElement('div');
  desc.className = 'desc';
  const bits = [o.dep_local + ' - ' + o.arr_local];
  if(o.after_rest) bits.push('next duty day');
  else bits.push('leg ' + o.legs_today + ' of the day');
  bits.push(o.fdp_used.toFixed(1) + 'h of ' + o.fdp_cap.toFixed(1) + 'h FDP');
  desc.textContent = bits.join('  \u00b7  ');
  left.appendChild(code); left.appendChild(desc);
  row.appendChild(left);
  if(label){
    const tag = document.createElement('div');
    tag.className = 'actions';
    tag.style.cssText = 'color:var(--blue);font-size:12px;font-weight:600;';
    tag.textContent = label;
    row.appendChild(tag);
  }
  row.onclick = onPick;
  return row;
}

function _recPanel(title, hint){
  const panel = document.createElement('div');
  panel.className = 'panel';
  const hdr = document.createElement('div');
  hdr.className = 'panel-card-hdr';
  hdr.textContent = title;
  panel.appendChild(hdr);
  if(hint){
    const sub = document.createElement('div');
    sub.style.cssText = 'padding:8px 14px 4px;font-size:12px;color:var(--label);';
    sub.textContent = hint;
    panel.appendChild(sub);
  }
  return panel;
}

function renderRecoveryStep(d){
  const results = document.getElementById('rec-results');
  results.innerHTML = '';

  const state = document.createElement('div');
  state.style.cssText = 'padding:12px 14px;font-size:12.5px;color:var(--label);line-height:1.6;';
  state.textContent = 'You are at ' + d.station + ' from ' + d.avail_local
    + ' \u2014 day ' + d.day_number + ', ' + d.legs_today
    + (d.legs_today === 1 ? ' leg' : ' legs') + ' and ' + d.block_today.toFixed(1) + 'h block so far.'
    + (d.repair_window_end ? ' Remain contactable until ' + d.repair_window_end + '.' : '');
  results.appendChild(state);

  // What has been built so far, and a way back out of it.
  if(d.trail.length){
    const built = _recPanel('Your Repair So Far', '');
    d.trail.forEach(t => {
      const row = document.createElement('div');
      row.className = 'doc-row';
      const left = document.createElement('div');
      const code = document.createElement('div');
      code.className = 'code';
      code.textContent = 'DAY ' + t.day + '   ' + (t.flight_number || '----')
        + '   ' + t.origin + '-' + t.destination;
      const desc = document.createElement('div');
      desc.className = 'desc';
      desc.textContent = t.dep_local + ' - ' + t.arr_local + (t.after_rest ? '   after rest' : '');
      left.appendChild(code); left.appendChild(desc);
      row.appendChild(left);
      built.appendChild(row);
    });
    const undo = document.createElement('div');
    undo.style.cssText = 'padding:12px 14px;';
    const ub = document.createElement('button');
    ub.type = 'button';
    ub.className = 'docs-btn';
    ub.style.cssText = 'border-radius:7px;background:var(--card);color:var(--label);border:1px solid var(--border);';
    ub.textContent = 'Undo Last Leg';
    ub.onclick = () => { _recPicks.pop(); loadRecoveryStep(); };
    undo.appendChild(ub);
    built.appendChild(undo);
    results.appendChild(built);
  }

  // Home. Offered the moment it is true, never forced.
  if(d.at_home){
    const done = _recPanel('Back at ' + d.domicile,
                           'Your repair ends here. Accept it, or keep going.');
    const wrap = document.createElement('div');
    wrap.style.cssText = 'padding:12px 14px;';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'docs-btn';
    btn.id = 'rec-accept-btn';
    btn.textContent = 'Accept This Trip';
    btn.onclick = acceptRecoverySteps;
    wrap.appendChild(btn);
    const note = document.createElement('div');
    note.id = 'rec-accept-note';
    note.style.cssText = 'padding:8px 2px 0;font-size:12px;color:var(--label);';
    wrap.appendChild(note);
    done.appendChild(wrap);
    results.appendChild(done);
  }

  // Delaying the whole day at once. Sits above the per-leg cards because
  // when it applies it is usually the answer — pushing one departure
  // moves everything behind it, and tapping each one in turn to say "the
  // same flight, later" is work the app can do.
  if(d.cascade){
    const c = d.cascade;
    const panel = _recPanel('Delay the Rest of Today',
      'Every remaining leg at its next legal slot \u2014 same flights, later');
    c.legs.forEach(l => {
      const row = document.createElement('div');
      row.className = 'doc-row';
      const left = document.createElement('div');
      const code = document.createElement('div');
      code.className = 'code';
      code.textContent = (l.flight_number || '----') + '   ' + l.origin + '-' + l.destination;
      const desc = document.createElement('div');
      desc.className = 'desc';
      desc.textContent = l.dep_local + ' - ' + l.arr_local
        + (l.after_rest ? '   after rest' : '')
        + (l.instead_of ? ('   in place of ' + l.instead_of) : '');
      left.appendChild(code); left.appendChild(desc);
      row.appendChild(left);
      panel.appendChild(row);
    });
    if(c.stopped_because){
      const stop = document.createElement('div');
      stop.style.cssText = 'padding:8px 14px 0;font-size:12px;color:var(--label);line-height:1.5;';
      stop.textContent = 'Covers ' + c.covers + ' of ' + c.of + ' remaining legs \u2014 '
        + c.stopped_because + '. Pick the rest yourself below.';
      panel.appendChild(stop);
    }
    const wrap = document.createElement('div');
    wrap.style.cssText = 'padding:12px 14px;';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'docs-btn';
    btn.textContent = 'Delay These ' + c.covers + ' Legs';
    btn.onclick = () => { _recPicks = c.picks; loadRecoveryStep(); };
    wrap.appendChild(btn);
    panel.appendChild(wrap);
    results.appendChild(panel);
  }

  // The leg the pairing gave you, answered first and on its own terms.
  const pl = d.options.planned;
  if(pl){
    const panel = _recPanel('Your Next Planned Leg', '');
    if(pl.as_planned.legal){
      panel.appendChild(_recOptionRow(pl.as_planned, 'fly it',
        () => pickRecoveryStep(pl.index, pl.as_planned.after_rest)));
    } else {
      const why = document.createElement('div');
      why.style.cssText = 'padding:10px 14px;font-size:12.5px;color:var(--red);line-height:1.5;';
      why.textContent = (pl.as_planned.flight_number || '----') + ' ' + pl.as_planned.origin
        + '-' + pl.as_planned.destination + ' as scheduled: ' + pl.as_planned.why_not + '.';
      panel.appendChild(why);
      if(pl.delayed){
        panel.appendChild(_recOptionRow(pl.delayed, 'delay',
          () => pickRecoveryStep(pl.index, pl.delayed.after_rest)));
      } else {
        const none = document.createElement('div');
        none.style.cssText = 'padding:0 14px 12px;font-size:12.5px;color:var(--label);';
        none.textContent = 'It cannot be delayed into anything legal either \u2014 reassign below.';
        panel.appendChild(none);
      }
    }
    results.appendChild(panel);
  }

  const same = d.options.same_day || [];
  if(same.length){
    const panel = _recPanel('Reassign \u2014 Same Duty Day (' + same.length + ')',
                            'Keeps today going, from ' + d.station);
    same.forEach(o => panel.appendChild(_recOptionRow(o, '',
      () => pickRecoveryStep(o.index, false))));
    results.appendChild(panel);
  }

  const rest = d.options.after_rest || [];
  if(rest.length){
    const panel = _recPanel('Reassign \u2014 After Rest (' + rest.length + ')',
                            'Ends the day here and reports fresh at ' + d.station);
    rest.forEach(o => panel.appendChild(_recOptionRow(o, '',
      () => pickRecoveryStep(o.index, true))));
    results.appendChild(panel);
  }

  if(!pl && !same.length && !rest.length){
    const none = document.createElement('p');
    none.className = 'placeholder-note';
    none.style.padding = '14px';
    none.textContent = 'Nothing can legally be flown from ' + d.station + ' from ' + d.avail_local + '.';
    results.appendChild(none);
  }
}

function pickRecoveryStep(index, afterRest){
  _recPicks.push({index: index, after_rest: !!afterRest});
  loadRecoveryStep();
}

async function acceptRecoverySteps(){
  if(!_recStep || !_recPicks.length) return;
  const btn = document.getElementById('rec-accept-btn');
  const note = document.getElementById('rec-accept-note');
  btn.disabled = true;
  btn.textContent = 'Reassigning\u2026';
  note.textContent = '';
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(_recSeq.seq) + '/recover-accept', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        duty_day: _recDay, leg_index: _recLegIndex, kind: _recKind,
        at: _recAt, station: _recStation, tier: 'manual', picks: _recPicks,
      }),
    });
    const d = await readJson(r);
    if(!r.ok) throw new Error(d.error || 'could not apply this trip');
    note.textContent = 'Reassigned as SEQ ' + d.seq + '. Your new pairing is in Messages.';
    btn.textContent = 'Reassigned';
    paintMessageBadge(d.unacknowledged || 0);
    _recSeq = d.sequence || _recSeq;
    _recPicks = [];
    setTimeout(closeRecovery, 1400);
  } catch(e){
    btn.disabled = false;
    btn.textContent = 'Accept This Trip';
    note.style.color = 'var(--red)';
    note.textContent = e.message;
  }
}

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
// Admin stats — crew roster, activity, and per-document acknowledgement
// compliance. Admin-only; the entry point in Settings stays hidden for
// everyone else and every route behind it 403s independently, so this is
// a convenience, not the access control.
function _agoText(iso){
  if(!iso) return 'never';
  const then = new Date(iso), mins = Math.floor((Date.now() - then) / 60000);
  if(isNaN(mins)) return 'never';
  if(mins < 2) return 'just now';
  if(mins < 60) return mins + 'm ago';
  const h = Math.floor(mins / 60);
  if(h < 24) return h + 'h ago';
  const d = Math.floor(h / 24);
  return d < 30 ? (d + 'd ago') : then.toLocaleDateString();
}
function _adminSection(title){
  const wrap = document.createElement('div');
  wrap.style.cssText = 'margin:0 0 16px;';
  const bar = document.createElement('div');
  bar.className = 'section-bar';
  bar.textContent = title;
  wrap.appendChild(bar);
  const body = document.createElement('div');
  body.className = 'panel-card';
  body.style.cssText = 'margin-top:0;';
  wrap.appendChild(body);
  return {wrap, body};
}
async function initAdminView(){
  const body = document.getElementById('admin-body');
  showLoading(body);
  let data;
  try {
    const r = await fetch('/admin/stats');
    data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; return; }
  body.innerHTML = '';

  // --- Document compliance first: it's the reason acknowledgement exists,
  // and an outstanding count is the one thing worth acting on today.
  const docs = _adminSection('Document Acknowledgement');
  if(!data.documents.length){
    docs.body.innerHTML = '<p class="placeholder-note" style="padding:14px;">No documents published yet.</p>';
  }
  data.documents.forEach(d => {
    const row = document.createElement('div');
    row.className = 'doc-row lib-row';
    const left = document.createElement('div');
    const code = document.createElement('div'); code.className = 'code'; code.textContent = d.title;
    const desc = document.createElement('div'); desc.className = 'desc';
    const missing = d.user_count - d.acked_count;
    desc.textContent = [d.category, d.revision > 1 ? ('Rev ' + d.revision) : '',
                        missing ? (d.outstanding.join(', ') + ' outstanding') : 'everyone acknowledged']
                       .filter(Boolean).join(' · ');
    if(missing) desc.style.color = 'var(--red)';
    left.appendChild(code); left.appendChild(desc);
    const stats = document.createElement('div'); stats.className = 'lib-stats';
    const n = document.createElement('div');
    n.textContent = d.acked_count + '/' + d.user_count;
    n.style.cssText = 'font-weight:700;font-size:14px;color:' + (missing ? 'var(--red)' : 'var(--value)') + ';';
    stats.appendChild(n);
    row.appendChild(left); row.appendChild(stats);
    docs.body.appendChild(row);
  });
  body.appendChild(docs.wrap);

  // --- Crew roster
  const crew = _adminSection('Crew (' + data.user_count + ')');
  data.users.forEach(u => {
    const row = document.createElement('div');
    row.className = 'doc-row lib-row';
    const left = document.createElement('div');
    const code = document.createElement('div'); code.className = 'code';
    code.textContent = u.username + (u.is_admin ? '  ·  ADMIN' : '');
    const desc = document.createElement('div'); desc.className = 'desc';
    const bits = [
      u.legs + ' leg' + (u.legs === 1 ? '' : 's'),
      u.signings + ' signing' + (u.signings === 1 ? '' : 's'),
      u.trip_checkins + ' trip' + (u.trip_checkins === 1 ? '' : 's'),
    ];
    if(u.active_seq) bits.unshift('SEQ ' + u.active_seq);
    if(u.docs_outstanding > 0) bits.push(u.docs_outstanding + ' doc' + (u.docs_outstanding === 1 ? '' : 's') + ' outstanding');
    desc.textContent = bits.join(' · ');
    left.appendChild(code); left.appendChild(desc);
    const stats = document.createElement('div'); stats.className = 'lib-stats';
    const seen = document.createElement('div');
    seen.textContent = _agoText(u.last_seen);
    stats.appendChild(seen);
    row.appendChild(left); row.appendChild(stats);
    row.onclick = () => adminShowUser(u.id);
    crew.body.appendChild(row);
  });
  body.appendChild(crew.wrap);
}
async function adminShowUser(userId){
  const body = document.getElementById('admin-body');
  showLoading(body);
  let d;
  try {
    const r = await fetch('/admin/users/' + userId);
    d = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (d.error || 'Failed to load') + '</p>'; return; }
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; return; }
  body.innerHTML = '';
  body.appendChild(libraryBackLink('Back to Crew & Documents', initAdminView));

  const hdr = document.createElement('div');
  hdr.className = 'lib-total';
  hdr.textContent = [
    d.user.username + (d.user.is_admin ? ' (admin)' : ''),
    'last seen ' + _agoText(d.user.last_seen),
    d.user.active_seq ? ('SEQ ' + d.user.active_seq) : '',
    d.user.timezone, d.user.simbrief_user ? ('SimBrief ' + d.user.simbrief_user) : '',
  ].filter(Boolean).join(' · ');
  body.appendChild(hdr);

  const simple = (title, rows, empty) => {
    const s = _adminSection(title);
    if(!rows.length){
      s.body.innerHTML = '<p class="placeholder-note" style="padding:14px;">' + empty + '</p>';
    }
    rows.forEach(([main, sub, warn]) => {
      const row = document.createElement('div');
      row.className = 'doc-row';
      const left = document.createElement('div');
      const c = document.createElement('div'); c.className = 'code'; c.textContent = main;
      const s2 = document.createElement('div'); s2.className = 'desc'; s2.textContent = sub;
      if(warn) s2.style.color = 'var(--red)';
      left.appendChild(c); left.appendChild(s2);
      row.appendChild(left);
      s.body.appendChild(row);
    });
    body.appendChild(s.wrap);
  };

  simple('Documents', d.documents.map(x => [
    x.title + (x.revision > 1 ? ('  Rev ' + x.revision) : ''),
    x.acknowledged ? ('Acknowledged ' + new Date(x.at).toLocaleString()) : 'NOT acknowledged',
    !x.acknowledged,
  ]), 'No documents published.');

  simple('Recent Legs', d.legs.map(x => [
    (x.flight_number || '—') + ' ' + x.origin + '→' + x.destination,
    [x.seq ? ('SEQ ' + x.seq) : '', new Date(x.created_at).toLocaleString(),
     x.fit_for_duty ? 'FFD signed' : 'FFD not signed'].filter(Boolean).join(' · '),
    !x.fit_for_duty,
  ]), 'No legs generated.');

  simple('Trip Check-Ins', d.trip_checkins.map(x => [
    'SEQ ' + x.seq, x.signed_at ? new Date(x.signed_at).toLocaleString() : '', false,
  ]), 'No trip check-ins.');

  simple('Signing History', d.signings.map(x => [
    (x.flight_number || '—') + (x.dep_date ? ('  ' + x.dep_date) : ''),
    x.signed_at ? new Date(x.signed_at).toLocaleString() : '', false,
  ]), 'No signatures recorded.');
}
// Company Docs — instance-wide PDFs an admin publishes, which every pilot
// has to acknowledge. Distinct from Saved Docs (this leg's own bookmarked
// release pages) and from the release PDFs in All Commands: those come out
// of a SimBrief OFP, these are uploaded manuals/bulletins.
const IS_ADMIN = "$is_admin" === "1";
let _unackedDocs = parseInt("$unacked_docs", 10) || 0;
// Blocks the app until every company document is acknowledged. Keeps the
// old name because a dozen call sites already invoke it whenever the
// unacknowledged count could have moved.
//
// Two views are exempt, and only two: the Docs list and the PDF viewer.
// Locking those would leave no way to READ the documents or press
// Acknowledge, so the lock would have no exit at all.
//
// Messages is NOT exempt, and that is a decision rather than an oversight
// (asked and confirmed 2026-08-31). It is the tempting one to add, since a
// release-drift line is operational and the documents are paperwork — but
// the lock is meant to be a lock. Outstanding acknowledgements come first,
// and a message that has waited for them is still there afterwards.
const _DOC_LOCK_EXEMPT = ['doclocker', 'pdf'];

function paintDocAckBanner(){
  const el = document.getElementById('doc-lock');
  if(!el) return;
  const active = (document.querySelector('.view.active') || {}).id || '';
  const onExemptView = _DOC_LOCK_EXEMPT.some(v => active === v + '-view');
  if(_unackedDocs > 0 && !onExemptView){
    const n = _unackedDocs;
    document.getElementById('doc-lock-body').textContent =
      n + ' company document' + (n === 1 ? '' : 's') + ' need' + (n === 1 ? 's' : '') +
      ' your acknowledgement before you can use the app.';
    _positionDocLock();
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// Same geometry as the loading overlay: between the header's measured
// bottom edge and the tab bar, so both stay visible and usable.
function _positionDocLock(){
  const el = document.getElementById('doc-lock');
  if(!el) return;
  const topbar = document.querySelector('.view.active .topbar');
  const tabbar = document.querySelector('.tabbar');
  el.style.top = Math.max(0, topbar ? topbar.getBoundingClientRect().bottom : 0) + 'px';
  el.style.bottom = (tabbar ? tabbar.getBoundingClientRect().height : 0) + 'px';
}

function _fmtDocSize(bytes){
  if(!bytes) return '';
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? (mb.toFixed(1) + ' MB') : (Math.max(1, Math.round(bytes / 1024)) + ' KB');
}
async function initDocLocker(){
  const body = document.getElementById('doclocker-body');
  showLoading(body);
  try {
    const r = await fetch('/docs/list');
    const data = await r.json();
    if(!r.ok){ body.innerHTML = '<p class="placeholder-note">' + (data.error || 'Failed to load') + '</p>'; return; }
    _unackedDocs = data.unacknowledged || 0;
    paintDocAckBanner();
    body.innerHTML = '';
    if(!data.documents.length){
      body.innerHTML = '<p class="placeholder-note">No company documents published yet.</p>';
      return;
    }
    const card = document.createElement('div');
    card.className = 'panel-card';
    data.documents.forEach(doc => {
      const row = document.createElement('div');
      row.className = 'doc-row lib-row';
      const left = document.createElement('div');
      const code = document.createElement('div'); code.className = 'code'; code.textContent = doc.title;
      const desc = document.createElement('div'); desc.className = 'desc';
      const bits = [
        doc.category,
        doc.revision > 1 ? ('Rev ' + doc.revision) : '',
        _fmtDocSize(doc.size_bytes),
      ].filter(Boolean);
      desc.textContent = bits.join(' · ');
      left.appendChild(code); left.appendChild(desc);
      const stats = document.createElement('div'); stats.className = 'lib-stats';
      const status = document.createElement('div');
      if(doc.acknowledged){
        status.textContent = 'Acknowledged';
      } else {
        status.textContent = 'Needs acknowledgement';
        status.style.color = 'var(--red)';
        status.style.fontWeight = '600';
      }
      stats.appendChild(status);
      row.appendChild(left); row.appendChild(stats);
      row.onclick = () => viewCompanyDoc(doc.id);
      card.appendChild(row);
    });
    body.appendChild(card);
  } catch(e) { body.innerHTML = '<p class="placeholder-note">Request failed: ' + e + '</p>'; }
}
let _currentCompanyDoc = null;
async function viewCompanyDoc(docId){
  showToast("Opening\u2026");
  let data;
  try {
    const r = await fetch('/docs/' + docId + '/pdf');
    data = await r.json();
    if(!r.ok){ showToast(data.error || 'Could not open that document'); return; }
  } catch(e) { showToast('Request failed: ' + e); return; }
  // Company documents are deliberately NOT tabbed. Tabs are for the handful
  // of small release PDFs a pilot compares against each other; a manual is
  // one big thing you read on its own, and holding several of them open
  // would keep that many multi-hundred-page documents in memory at once.
  // Opening one therefore stands alone and leaves any release tabs intact
  // underneath — going back to a release brings its strip straight back.
  _activePdfTab = null;
  renderPdfTabs();
  _currentCompanyDoc = data;
  showView('pdf');
  document.getElementById('pdf-view-title').textContent = data.title;
  // No FFD gate on a company doc — that one is about a specific flight's
  // release, not a manual.
  document.getElementById('pdf-ffd-banner').style.display = 'none';
  // Read-in-app only: no Download, Print or Share, and deliberately no blob
  // URL minted either, since that would leave a downloadable handle to it.
  if(_pdfObjectUrl){ URL.revokeObjectURL(_pdfObjectUrl); _pdfObjectUrl = null; }
  _setPdfActions(false);
  paintCompanyDocAckBar();
  try {
    await renderPdfInline(b64ToBytes(data.pdf_b64));
  } catch(e) {
    document.getElementById('pdf-pages').innerHTML = '<p style="color:#fff;padding:20px;">Failed to render this PDF: ' + e + '</p>';
  }
}
function paintCompanyDocAckBar(){
  const bar = document.getElementById('pdf-ack-bar');
  const doc = _currentCompanyDoc;
  if(!doc){ bar.style.display = 'none'; return; }
  const txt = document.getElementById('pdf-ack-text');
  const btn = document.getElementById('pdf-ack-btn');
  bar.style.display = 'flex';
  if(doc.acknowledged){
    txt.textContent = 'Acknowledged' + (doc.revision > 1 ? (' · Rev ' + doc.revision) : '');
    txt.style.color = 'var(--label)';
    btn.style.display = 'none';
  } else {
    txt.textContent = 'You have not acknowledged this document yet.';
    txt.style.color = 'var(--red)';
    btn.style.display = '';
    btn.disabled = false;
    btn.onclick = () => acknowledgeCompanyDoc(doc.id);
  }
}
async function acknowledgeCompanyDoc(docId){
  const btn = document.getElementById('pdf-ack-btn');
  btn.disabled = true;
  try {
    const r = await fetch('/docs/' + docId + '/ack', {method: 'POST'});
    const data = await r.json();
    if(!r.ok){ btn.disabled = false; showToast(data.error || 'Could not acknowledge'); return; }
    if(_currentCompanyDoc && _currentCompanyDoc.id === docId) _currentCompanyDoc.acknowledged = true;
    _unackedDocs = data.unacknowledged || 0;
    paintDocAckBanner();
    paintCompanyDocAckBar();
    showToast('Acknowledged');
  } catch(e) { btn.disabled = false; showToast('Request failed: ' + e); }
}
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
  showLoading(body);
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
      m.style.cssText = 'font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;color:var(--value);margin-bottom:6px;';
      m.textContent = s.metar;
      card.appendChild(m);
    }
    if(s.taf){
      const t = document.createElement('div');
      t.style.cssText = 'font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;color:var(--label);';
      t.textContent = s.taf;
      card.appendChild(t);
    }
    body.appendChild(card);
  });
}

(function(){
  const params = new URLSearchParams(window.location.search);
  const view = params.get('view') || "$default_view";
  paintDocAckBanner();
  if(!LEG_ID){
    // /schedule — the leg-independent root. Nothing here is scoped to a
    // real leg, so there's no Overview to default to and no per-leg sync
    // to start. Still honor an explicit ?view= (e.g. the global settings
    // gear links here as /schedule?view=settings) — only default to
    // Schedule itself when nothing was asked for.
    showView(view || 'pairing');
    return;
  }
  // Any view that actually exists, rather than a hand-kept list. Home's tab
  // bar sends leg-scoped tabs here as /fos/<id>?view=<tab>, and 'messages'
  // was never added to the old list — so tapping Messages on Home silently
  // landed on Overview instead, which read as the release messages not
  // working at all.
  if(view && view !== 'overview' && document.getElementById(view + '-view')) showView(view);
  else initOverviewPills();
  paintReleaseState();
  // Reopen whatever documents were open last time, or seed from Saved Docs.
  // Deliberately not awaited: it does a fetch, and nothing else on this page
  // should wait on it.
  restorePdfTabs();
  // Repaint the AeroAPI panel from what is already on the leg. This is the
  // half of "no second API call" the server cannot do on its own: the
  // gates survive a regenerate via carry_gates_from, but without this the
  // panel came back blank and the obvious next move was to run the lookup
  // again.
  if(AERO_SUGGESTION) paintAeroSuggestion(AERO_SUGGESTION, "Gates applied to this flight.");
  showDateSlipModalIfPending();
  startAutoSync();
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
