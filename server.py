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
import logging
import os

from flask import Flask, request, jsonify, Response
from string import Template
import pbs_parser
import release_engine
import simbrief_ofp

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
    record = _store_leg(build_leg_from_sources(payload))
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
        })
    return jsonify(out)


@app.route("/pbs/sequences/<seq_number>")
def get_pbs_sequence(seq_number):
    seq = next((s for s in _pbs_store["sequences"] if s["seq"] == seq_number), None)
    if not seq:
        return jsonify({"error": "not found"}), 404
    return jsonify(seq)


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
    ctx["signed_in_class"] = "" if ctx.get("signed_in") else "inactive"
    ctx["ffd_class"] = "" if ctx.get("fit_for_duty") else "inactive"
    str_ctx = {k: ("" if v is None else str(v)) for k, v in ctx.items()}
    return Template(FOS_TEMPLATE).safe_substitute(**str_ctx)


LAUNCHER_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FOS</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#eef1f4;margin:0;padding:24px;color:#1a1f29;}
  h1{font-size:18px;color:#144e94;}
  textarea{width:100%;max-width:640px;height:220px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;padding:10px;border:1px solid #e3e6ea;border-radius:6px;box-sizing:border-box;}
  button{margin-top:10px;background:#1c63b7;color:#fff;border:none;padding:10px 18px;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer;}
  .arow{display:block;background:#fff;border:1px solid #e3e6ea;border-radius:6px;padding:10px 14px;margin-bottom:8px;text-decoration:none;color:#1a1f29;font-size:13.5px;max-width:640px;}
  .arow span{color:#6b7380;float:right;}
  .empty{color:#6b7380;font-style:italic;}
  #msg{margin-top:8px;font-size:13px;}
</style></head><body>
<h1>FOS &mdash; paste a leg JSON to generate</h1>
<textarea id="payload">{
  "seq": "6610", "date": "WED 24APR19", "flight_number": "3543",
  "origin": "BNA", "destination": "JFK",
  "dep_date": "4/24/19", "arr_date": "4/24/19",
  "sched_out": "07:56", "sched_in": "11:27", "est_out": "07:56", "est_in": "11:27",
  "dep_gate": "C10", "arr_gate": "32G",
  "fleet_type": "EMJ", "equipment_type": "E140",
  "tail_number": "843", "tail_routing": "From 3420/23 IN 2125/23",
  "status": "MISFFD", "customer_load": 31, "position": "FO",
  "crew": ["M. Anderson", "T. Reyes"],
  "flight_time": "2:37", "ground_time": "1:33", "tz_diff": "+1:00"
}</textarea><br>
<button onclick="gen()">Generate</button>
<div id="msg"></div>
<h1 style="margin-top:28px;">Archive</h1>
$rows
<script>
function gen(){
  const el = document.getElementById('msg');
  let body;
  try { body = JSON.parse(document.getElementById('payload').value); }
  catch(e){ el.textContent = 'Invalid JSON: ' + e.message; el.style.color = '#c0392b'; return; }
  fetch('/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(r=>r.json())
    .then(data=>{ window.location.href = data.fos_url; })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = '#c0392b'; });
}
</script>
</body></html>"""


FOS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Flight $flight_number \u2013 FOS</title>
<style>
  :root{
    --navy:#142c52; --blue:#1c63b7; --blue-dark:#144e94; --green:#2fa355;
    --bg:#eef1f4; --card:#fff; --border:#e3e6ea; --label:#6b7380; --value:#1a1f29;
    --red:#e0393e; --inactive:#9aa1ab; --radius:6px;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--value);-webkit-font-smoothing:antialiased;}
  button{font-family:inherit;}
  :focus-visible{outline:2px solid var(--blue-dark);outline-offset:2px;}
  @media (prefers-reduced-motion: reduce){ *{transition:none !important;animation:none !important;} }
  .app-shell{display:flex;min-height:100vh;max-width:900px;margin:0 auto;box-shadow:0 0 0 1px var(--border);}
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
  .search-block input{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:5px;font-size:13.5px;background:#fbfbfc;}
  .section-bar{display:flex;align-items:center;justify-content:space-between;background:var(--blue);color:#fff;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer;border:none;width:100%;text-align:left;}
  .section-bar svg{width:16px;height:16px;transition:transform .15s ease;}
  .section-bar.collapsed svg.chevron{transform:rotate(180deg);}
  .no-prefs{padding:10px 14px;color:var(--label);font-size:13px;font-style:italic;background:var(--card);border-bottom:1px solid var(--border);}
  .doc-list{background:var(--card);}
  .doc-row{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-bottom:1px solid var(--border);gap:10px;}
  .doc-row .code{font-weight:700;font-size:13px;}
  .doc-row .desc{font-size:12px;color:var(--label);margin-top:1px;}
  .doc-row .actions{display:flex;align-items:center;gap:12px;flex:0 0 auto;}
  .doc-row .actions svg{width:19px;height:19px;color:#5b6472;cursor:pointer;}
  .doc-row .check{color:var(--blue);}
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
    <button class="side-btn" title="Pairing" onclick="showToast('Pairing')">
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
        <a class="back-link" href="/">Back</a>
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
      <div class="card">
        <div class="content-grid">
          <div class="col-divider">
            <div class="info-row"><span class="lbl">Arrival Date</span><span class="val">$arr_date</span></div>
            <div class="info-row"><span class="lbl">Departure Gate</span><span class="val">$dep_gate</span></div>
            <div class="info-row"><span class="lbl">Arrival Gate</span><span class="val">$arr_gate</span></div>
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
        <div class="col"><span>$dep_gate</span><span>$arr_gate</span></div>
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
            <svg class="check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Acknowledged')"><path d="M20 6L9 17l-5-5"/></svg>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Opening eFlight Plan')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          </div>
        </div>
        <div class="doc-row">
          <div><div class="code">FI</div><div class="desc">Flight Details \u2013 GMT</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Opening Flight Details')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
        <div class="doc-row" style="border-bottom:none;">
          <div><div class="code">G*L/SS</div><div class="desc">Customers Requiring Special Services</div></div>
          <div class="actions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" onclick="showToast('Opening special services list')"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></div>
        </div>
      </div>
    </section>
    <section id="release-view" class="view">
      <div class="topbar">
        <button class="back-link" onclick="showView('overview')">Back</button>
        <div class="topbar-title">
          <h1>Flight $flight_number</h1>
          <p>Release Builder</p>
        </div>
      </div>
      <div class="status-bar"><span>SEQ $seq</span><span>$date</span></div>
      <div class="search-block">
        <label for="release-user">SimBrief Username</label>
        <input id="release-user" type="text" placeholder="e.g. tgibbons">
      </div>
      <div style="padding:14px;background:var(--card);">
        <button class="docs-btn" id="release-gen-btn" style="border-radius:5px;" onclick="generateRelease()">Generate Release</button>
        <div id="release-status" style="margin-top:10px;font-size:13px;color:var(--label);"></div>
        <div id="release-downloads" style="display:none;margin-top:10px;gap:10px;flex-wrap:wrap;">
          <a id="release-rls-link" style="display:none;background:var(--green);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download RLS PDF</a>
          <a id="release-wb-link" style="display:none;background:var(--blue-dark);color:#fff;text-decoration:none;padding:9px 14px;border-radius:5px;font-size:13px;font-weight:600;">Download W&amp;B PDF</a>
        </div>
      </div>
    </section>
  </main>
</div>
<div id="toast"></div>
<script>
const LEG_ID = "$leg_id";
function showView(view){
  document.getElementById('overview-view').classList.toggle('active', view==='overview');
  document.getElementById('documents-view').classList.toggle('active', view==='documents');
  document.getElementById('release-view').classList.toggle('active', view==='release');
  document.getElementById('nav-home').classList.toggle('active', view==='overview');
  document.getElementById('nav-docs').classList.toggle('active', view==='documents');
  document.getElementById('nav-release').classList.toggle('active', view==='release');
  window.scrollTo(0,0);
  if(view === 'release') initReleaseView();
}
let _releaseStatusChecked = false;
function initReleaseView(){
  const input = document.getElementById('release-user');
  const saved = localStorage.getItem('fos_simbrief_user');
  if(saved && !input.value) input.value = saved;
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
function generateRelease(){
  const btn = document.getElementById('release-gen-btn');
  const status = document.getElementById('release-status');
  const userId = document.getElementById('release-user').value.trim();
  if(!userId){ status.textContent = 'Enter a SimBrief username first.'; status.style.color = '#c0392b'; return; }
  localStorage.setItem('fos_simbrief_user', userId);
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
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
