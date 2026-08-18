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
from datetime import datetime, timezone

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
    "signature": "", "signed_at": "",
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
  h1{font-size:18px;color:#144e94;}
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
</style></head><body>
<h1>FOS</h1>
<div class="tabs">
  <button class="tab-btn active" id="tab-pbs-btn" onclick="showTab('pbs')">Import PBS</button>
  <button class="tab-btn" id="tab-simbrief-btn" onclick="showTab('simbrief')">Load from SimBrief</button>
</div>

<div id="tab-pbs" class="tab-panel active">
  <textarea id="pbs-text" placeholder="Paste your crew pairing builder's PBS bid-pack export here"></textarea><br>
  <button onclick="importPbs()">Import</button>
  <div id="import-msg" class="msg"></div>

  <h1 style="margin-top:28px;">Sequences</h1>
  <div id="seq-list"><p class="empty">No sequences imported yet.</p></div>

  <div id="gen-form" class="panel" style="display:none;">
    <div id="gen-seq-label" style="font-weight:600;"></div>
    <label for="gen-duty-day">Duty Day</label>
    <select id="gen-duty-day" onchange="populateLegs()"></select>
    <label for="gen-leg">Leg</label>
    <select id="gen-leg"></select>
    <label for="gen-position">Position</label>
    <select id="gen-position"></select>
    <label for="gen-simbrief">SimBrief Username (optional — fills tail/crew/load/date on THIS leg from the current OFP)</label>
    <input id="gen-simbrief" type="text" placeholder="e.g. tgibbons">
    <br><button onclick="generateLeg()">Generate This Leg</button>
    <div id="gen-msg" class="msg"></div>
    <hr>
    <div style="font-size:13px;color:#6b7380;margin-bottom:4px;">Cache every leg in this sequence at once (no SimBrief enrichment — this is the whole trip's schedule, not one specific day's dispatch):</div>
    <button class="secondary" onclick="generateAllLegs()">Generate &amp; Cache All Legs in Sequence</button>
    <div id="gen-all-msg" class="msg"></div>
  </div>
</div>

<div id="tab-simbrief" class="tab-panel">
  <div class="panel">
    <div style="font-size:13px;color:#6b7380;margin-bottom:4px;">Loads whatever OFP is currently on this SimBrief account right now — for dispatching the flight you're on today, not for browsing a schedule.</div>
    <label for="sb-user">SimBrief Username</label>
    <input id="sb-user" type="text" placeholder="e.g. tgibbons">
    <br><button onclick="loadFromSimbrief()">Load Current Flight</button>
    <div id="sb-msg" class="msg"></div>
  </div>
</div>

<h1 style="margin-top:28px;">Archive</h1>
<div id="archive-list">$rows</div>
<script>
let currentSeqData = null;

function showTab(tab){
  document.getElementById('tab-pbs').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-simbrief').classList.toggle('active', tab==='simbrief');
  document.getElementById('tab-pbs-btn').classList.toggle('active', tab==='pbs');
  document.getElementById('tab-simbrief-btn').classList.toggle('active', tab==='simbrief');
}

function loadArchive(){
  fetch('/archive').then(r=>r.json()).then(rows=>{
    document.getElementById('archive-list').innerHTML = rows.map(r =>
      `<a class="arow" href="/fos/${r.id}">${r.flight_number||''} ${r.origin||''}→${r.destination||''} <span>${r.dep_date||''}</span></a>`
    ).join('') || '<p class="empty">No legs generated yet.</p>';
  });
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
      `<a class="arow" href="#" onclick="selectSeq('${s.seq}');return false;">SEQ ${s.seq} — ${s.origin||''}→${s.final_destination||''} <span>${s.days} day(s)</span></a>`
    ).join('') || '<p class="empty">No sequences imported yet.</p>';
  });
}

function selectSeq(seq){
  fetch('/pbs/sequences/' + seq).then(r=>r.json()).then(data=>{
    currentSeqData = data;
    document.getElementById('gen-seq-label').textContent = 'SEQ ' + data.seq;
    document.getElementById('gen-duty-day').innerHTML = data.duty_days.map(d =>
      `<option value="${d.duty_day}">Day ${d.duty_day} (RPT ${d.report})</option>`
    ).join('');
    document.getElementById('gen-position').innerHTML = (data.positions||[]).map(p =>
      `<option value="${p}">${p}</option>`
    ).join('');
    const saved = localStorage.getItem('fos_simbrief_user');
    if(saved) document.getElementById('gen-simbrief').value = saved;
    populateLegs();
    document.getElementById('gen-msg').textContent = '';
    document.getElementById('gen-all-msg').textContent = '';
    document.getElementById('gen-form').style.display = 'block';
  });
}

function populateLegs(){
  const dutyDay = parseInt(document.getElementById('gen-duty-day').value, 10);
  const day = (currentSeqData.duty_days || []).find(d => d.duty_day === dutyDay);
  document.getElementById('gen-leg').innerHTML = (day ? day.legs : []).map((l, i) =>
    `<option value="${i}">${l.flight_number || '—'} ${l.origin}→${l.destination} ${l.dep_local}/${l.arr_local}</option>`
  ).join('');
}

function generateLeg(){
  const el = document.getElementById('gen-msg');
  const simbrief = document.getElementById('gen-simbrief').value.trim();
  if(simbrief) localStorage.setItem('fos_simbrief_user', simbrief);
  const body = {
    duty_day: parseInt(document.getElementById('gen-duty-day').value, 10),
    leg_index: parseInt(document.getElementById('gen-leg').value, 10),
    position: document.getElementById('gen-position').value,
  };
  if(simbrief) body.simbrief_user = simbrief;
  fetch('/pbs/sequences/' + currentSeqData.seq + '/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(r => r.json().then(data => ({ok:r.ok, data})))
    .then(({ok, data}) => {
      if(!ok){ el.textContent = data.error || 'Generate failed'; el.style.color = '#c0392b'; return; }
      window.location.href = data.fos_url;
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = '#c0392b'; });
}

async function generateAllLegs(){
  const el = document.getElementById('gen-all-msg');
  const position = document.getElementById('gen-position').value;
  const seq = currentSeqData.seq;
  let total = 0, ok = 0;
  const fails = [];
  for(const day of (currentSeqData.duty_days || [])){
    for(let i = 0; i < day.legs.length; i++){
      total++;
      try {
        const r = await fetch('/pbs/sequences/' + seq + '/generate', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({duty_day: day.duty_day, leg_index: i, position: position})
        });
        const data = await r.json();
        if(r.ok) ok++; else fails.push(`Day ${day.duty_day} leg ${i+1}: ${data.error}`);
      } catch(e) { fails.push(`Day ${day.duty_day} leg ${i+1}: ${e}`); }
      el.textContent = `Caching… ${ok}/${total} done`;
      el.style.color = '';
    }
  }
  el.textContent = `Cached ${ok} of ${total} legs.` + (fails.length ? ' Failures: ' + fails.join('; ') : '');
  el.style.color = fails.length ? '#c0392b' : '#2fa355';
  loadArchive();
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
      window.location.href = data.fos_url;
    })
    .catch(e=>{ el.textContent = 'Request failed: ' + e; el.style.color = '#c0392b'; });
}

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
      <button class="docs-btn" id="pairing-btn" style="background:var(--blue-dark);border-top:1px solid rgba(255,255,255,.2);" onclick="showView('pairing')">View Full Pairing</button>
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
  document.getElementById('pdf-view').classList.toggle('active', view==='pdf');
  document.getElementById('sign-view').classList.toggle('active', view==='sign');
  document.getElementById('pairing-view').classList.toggle('active', view==='pairing');
  document.getElementById('nav-home').classList.toggle('active', view==='overview');
  document.getElementById('nav-docs').classList.toggle('active', view==='documents');
  document.getElementById('nav-release').classList.toggle('active', view==='release');
  document.getElementById('nav-pairing').classList.toggle('active', view==='pairing');
  window.scrollTo(0,0);
  if(view === 'release') initReleaseView();
  if(view === 'sign') initSignPad();
  if(view === 'pairing') initPairingView();
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
      actions.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>';
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
async function generatePairingLeg(seq, dutyDay, legIndex, position){
  try {
    const r = await fetch('/pbs/sequences/' + encodeURIComponent(seq) + '/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({duty_day: dutyDay, leg_index: legIndex, position: position}),
    });
    const data = await r.json();
    if(!r.ok){ showToast(data.error || 'Generate failed'); return; }
    window.location.href = data.fos_url;
  } catch(e) { showToast('Request failed: ' + e); }
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
  stations.forEach(s => {
    const card = document.createElement('div');
    card.style.cssText = 'padding:11px 14px;border-bottom:1px solid var(--border);background:var(--card);';
    const header = document.createElement('div');
    header.style.cssText = 'font-weight:700;font-size:13px;margin-bottom:6px;';
    header.textContent = (roleLabel[s.role] || s.role) + ' — ' + s.icao;
    card.appendChild(header);
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
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
