#!/usr/bin/env python3
import io
from PIL import Image
from reportlab.lib.utils import ImageReader
import sys
import os
import math
import json
import requests
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import simpledialog, messagebox
import textwrap
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
import subprocess
from SPEEDOTHER import get_speed_other
from write_tlr_section import write_tlr_section

# ── Per-script config file: MASTERLOG_Jepp.py → MASTERLOG_Jepp.config ─────────
CONFIG_FILE = os.path.splitext(os.path.abspath(__file__))[0] + ".config"

# ── NOTAM style: set notam_style = "graphical" or "text" in config ────────────
def _get_notam_style():
    try:
        return json.load(open(CONFIG_FILE)).get("notam_style", "graphical")
    except Exception:
        return "graphical"

if _get_notam_style() == "text":
    from notam_helpers import (
        get_departure_notams_sorted, get_arrival_notams_sorted,
        get_alternate_notams_sorted, get_enroute_notams,
        _make_runway_pair, _get_runways,
    )
    _draw_notam_section = None
else:
    from notam_helpers_graphical import (
        get_departure_notams_sorted, get_arrival_notams_sorted,
        get_alternate_notams_sorted, get_enroute_notams,
        _draw_notam_section,
        _make_runway_pair, _get_runways,
    )

global_acdata = {}

_cached_font_choice = None

# ── Default font candidates (name → list of search paths) ─────────────────────
_DEFAULT_FONT_CANDIDATES = {
    "Courier New": [
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/Library/Fonts/Courier New.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Courier_New.ttf",
    ],
    "Menlo": [
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Menlo.ttc",
    ],
    "Courier Semi-Bold": [],  # stroke-thickened courier
    "Courier Normal": [
        "/Users/tobygibbons/Library/Fonts/COURIER.TTF",
    ],
    "🌈 Rainbow Comic Sans": [
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "/Library/Fonts/Comic Sans MS.ttf",
        "C:/Windows/Fonts/comic.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Comic_Sans_MS.ttf",
    ],
}

def _load_config():
    """Load config.json, return dict (empty if missing/corrupt)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_config(cfg):
    """Save dict to config.json."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"Warning: could not save config: {e}")

def _discover_fonts():
    """
    Return an ordered list of (display_name, path_or_None) for fonts that are
    actually present on this system. Always includes the built-in Courier fallback.
    Also merges any user-defined fonts from config['fonts'].
    """
    cfg = _load_config()
    candidates = dict(_DEFAULT_FONT_CANDIDATES)
    for entry in cfg.get("fonts", []):
        name = entry.get("name", "").strip()
        path = entry.get("path", "").strip()
        if name and path:
            candidates.setdefault(name, []).insert(0, path)

    found = []
    for name, paths in candidates.items():
        if not paths:
            found.append((name, None))
            continue
        for p in paths:
            if os.path.exists(p):
                found.append((name, p))
                break

    return found if found else [("Courier (built-in)", None)]

_cached_report_type = None   # "TLR" or "TPS"


def ask_font_selection():
    """
    Combined font + report-type picker.  Results are saved to config.json and
    restored as defaults on the next run so the dialog is pre-filled.

    Returns a string: "1", "2", "3", or "4"  (font choice).
    Call get_report_type() afterwards to retrieve the TLR / TPS preference.
    """
    global _cached_font_choice, _cached_report_type
    if _cached_font_choice is not None:
        return _cached_font_choice

    cfg = _load_config()
    saved_font   = cfg.get("last_font",    None)
    saved_report = cfg.get("report_type", None)

    # ── Skip dialog if both values are already saved in config.json ───────────
    if saved_font is not None and saved_report is not None:
        _cached_font_choice = saved_font
        _cached_report_type = saved_report
        return _cached_font_choice

    # Fall back to defaults before showing dialog
    saved_font   = saved_font   or "1"
    saved_report = saved_report or "TLR"

    root = tk.Tk()
    root.title("PDF Settings")
    root.resizable(False, False)

    # ── Font section ──────────────────────────────────────────────────────────
    tk.Label(root, text="Select PDF font:", font=("Helvetica", 12)).pack(pady=(12, 4))

    options = [
        ("1", "Courier Normal"),
        ("2", "Menlo"),
        ("3", "Courier New"),
        ("4", "🌈 Rainbow Comic Sans"),
    ]

    lb = tk.Listbox(root, selectmode=tk.SINGLE, width=28, height=len(options),
                    font=("Helvetica", 11))
    for _, label in options:
        lb.insert(tk.END, label)
    # Pre-select saved font
    default_font_idx = next((i for i, (k, _) in enumerate(options) if k == saved_font), 0)
    lb.selection_set(default_font_idx)
    lb.pack(padx=16, pady=4)

    # ── Report type section ───────────────────────────────────────────────────
    tk.Frame(root, height=1, bg="gray").pack(fill="x", padx=16, pady=(8, 4))
    tk.Label(root, text="Report type:", font=("Helvetica", 12)).pack(pady=(4, 2))

    import tkinter as _tk
    report_var = _tk.StringVar(value=saved_report)
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(0, 8))
    tk.Radiobutton(btn_frame, text="TLR  (ops release only)",
                   variable=report_var, value="TLR",
                   font=("Helvetica", 11)).pack(anchor="w", padx=8)
    tk.Radiobutton(btn_frame, text="TPS  (ops release + W&B)",
                   variable=report_var, value="TPS",
                   font=("Helvetica", 11)).pack(anchor="w", padx=8)

    # ── OK button ─────────────────────────────────────────────────────────────
    result = [saved_font, saved_report]

    def on_ok():
        sel = lb.curselection()
        result[0] = options[sel[0]][0] if sel else saved_font
        result[1] = report_var.get()
        root.destroy()

    tk.Button(root, text="OK", width=10, command=on_ok).pack(pady=(4, 12))
    root.bind("<Return>", lambda e: on_ok())

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth()  - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    root.mainloop()

    _cached_font_choice  = result[0]
    _cached_report_type  = result[1]

    # Persist both choices
    cfg["last_font"]   = _cached_font_choice
    cfg["report_type"] = _cached_report_type
    _save_config(cfg)

    return _cached_font_choice


def get_report_type() -> str:
    """Return 'TLR' or 'TPS'.  Reads from cache, then config.json, defaulting to 'TLR'."""
    if _cached_report_type is not None:
        return _cached_report_type
    cfg = _load_config()
    return cfg.get("report_type", "TLR")

import os
import io
import requests
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from PIL import Image



def _cols(*fields):
    """
    Lay text out at absolute character columns.

    Each field is (col, text) or (col, text, width) or (col, text, width, align)
    where align is "<" (default) or ">".  A width with align ">" right-justifies
    the text so it *ends* at col+width, which is what numeric columns want.

    Using this for both the header lines and the data lines makes it impossible
    for the two to drift apart — the column number is stated once per field.
    """
    out = []
    for f in fields:
        col, text = f[0], "" if f[1] is None else str(f[1])
        width = f[2] if len(f) > 2 else len(text)
        align = f[3] if len(f) > 3 else "<"
        if width and len(text) > width:
            text = text[:width]
        text = text.rjust(width) if align == ">" else text.ljust(width)
        if not text.strip():
            continue
        if len(out) < col:
            out.extend(" " * (col - len(out)))
        out[col:col + len(text)] = list(text)
    return "".join(out).rstrip()


def _fmt_fuel_k(raw_lbs, decimals=1):
    """Convert raw lbs (string or number) to thousands string, e.g. 25800 → '25.8'."""
    try:
        return f"{float(raw_lbs or 0) / 1000:.{decimals}f}"
    except Exception:
        return "---.-"


def _build_frontier_flight_plan_page(root, p):
    """
    Build a Frontier Airlines-style flight plan page to match the real OFP layout:

        FRONTIER AIRLINES - FLIGHT PLAN - FLT 384
        RTE 001      PLAN 002

        DAY ORG  DEST ALTN DIST CRZ  ACFT TYPE   ENGINES      A/C
        10  DEN  FLL       1534 CI 04 A319-112   CFM56-5B6   N951FR

              FUEL    TIME         PLAN     LIMIT
        BURN      017647  03.28   TXWT  147976  167329
        ...

                                                      PLAN  ACTUAL
        CKPT    LAT       LONG      TAS          BURN
        FREQ    FL    WIND   TEMP MH  MC  ZND GS  ZNT   TIME  FRMG  FRMG

        KDEN    N39 51.7  W104 40.4 ---          ORIGIN       25800
                CL054
        TOC     N40 07.4  W103 27.4 452          02150
        112.5   CL360 272048 M51  093 089 098 498 00.18 00.18 23650 .....
        ...

    Column positions for the navlog and the fuel/weight table are declared once
    as named constants and fed to _cols(), so headers and data cannot drift.

    p = dict with pre-extracted flight params (same keys as release page).
    """
    import textwrap as _tw

    get_text = lambda path, default="": (
        root.find(path).text.strip() if root.find(path) is not None and root.find(path).text else default
    )

    def _sf(val, default=0.0):
        try:
            return float(val or default)
        except (TypeError, ValueError):
            return float(default)

    def _si(val, default=0):
        try:
            return int(float(val or default))
        except (TypeError, ValueError):
            return int(default)

    def _hhmm(sec):
        """Seconds → HH.MM string (Frontier uses periods not colons)."""
        try:
            m = round(int(sec) / 60)
            return f"{m // 60:02d}.{m % 60:02d}"
        except Exception:
            return "00.00"

    # ── Unpack params ────────────────────────────────────────────────────────
    airline       = p["airline_display"]
    flt_num       = p["flight_number"]
    origin        = p["origin"]
    orig_iata     = p["origin_iata"]
    dest          = p["destination"]
    dest_iata     = p["destination_iata"]
    aircraft_reg  = p["aircraft_reg"]
    aircraft_type = p["aircraft_type"]

    # ── Route / plan numbers ──────────────────────────────────────────────────
    # plan_num = release version (RLS field), route_num always 001
    plan_num  = str(p.get("rls", "") or get_text("general/release", "001")).strip() or "001"
    route_num = "001"

    # ── Date field (day of month) ─────────────────────────────────────────────
    try:
        from datetime import datetime as _dt
        _ts  = int(get_text("times/sched_out", "0") or "0")
        _day = _dt.utcfromtimestamp(_ts).strftime("%d").lstrip("0") or "1"
    except Exception:
        _day = "--"

    # ── Distances & CI ────────────────────────────────────────────────────────
    dist_nm   = _si(get_text("general/route_distance", "0"))
    cost_idx  = get_text("general/costindex", "04")
    altn_icao = (get_text("alternate/icao", "") or get_text("api_params/altn", "")).strip()

    # ── Engine type from SimBrief ─────────────────────────────────────────────
    engine_type = get_text("aircraft/engine_type", "")
    if not engine_type:
        engine_type = get_text("aircraft/engines", "")

    # ── Fuel figures (lbs integers) ───────────────────────────────────────────
    enrt_burn   = _si(get_text("fuel/enroute_burn",  "0"))
    cont_fuel   = _si(get_text("fuel/contingency",   "0"))
    altn_burn   = _si(get_text("fuel/alternate_burn","0"))
    resv_fuel   = _si(get_text("fuel/reserve",       "0"))
    brake_rls   = _si(get_text("fuel/min_takeoff",   "0"))
    extra_fuel  = _si(get_text("fuel/extra",         "0"))
    tanker_fuel = _si(get_text("fuel/tanker",        "0"))
    taxi_fuel   = _si(get_text("fuel/taxi",          "0"))
    plan_ramp   = _si(get_text("fuel/plan_ramp",     "0"))
    plan_ldg    = _si(get_text("fuel/plan_landing",  "0"))

    # ── Times (seconds → HH.MM) ───────────────────────────────────────────────
    enrt_secs  = _si(get_text("times/est_time_enroute", "0"))
    taxi_secs  = _si(get_text("times/taxi_out",         "0"))
    resv_secs  = _si(get_text("times/reserve_time",     "0"))
    cont_secs  = _si(get_text("times/contfuel_time",    "0"))

    enrt_fmt  = _hhmm(enrt_secs)
    taxi_fmt  = _hhmm(taxi_secs)
    resv_fmt  = _hhmm(resv_secs)
    cont_fmt  = _hhmm(cont_secs)

    # Brake-release time = enrt + cont + resv
    br_secs   = enrt_secs + cont_secs + resv_secs
    br_fmt    = _hhmm(br_secs)

    # ── Weight figures ────────────────────────────────────────────────────────
    txwt      = _si(get_text("weights/est_ramp",    "0"))
    sow       = _si(get_text("weights/oew",         "0"))
    pyld      = _si(get_text("weights/payload",     "0"))
    zfw       = _si(get_text("weights/est_zfw",     "0"))
    fob       = plan_ramp
    tow       = _si(get_text("weights/est_tow",     "0"))
    lgw       = _si(get_text("weights/est_ldw",     "0"))

    max_txwt  = txwt
    max_sow   = sow
    max_zfw   = _si(get_text("weights/max_zfw",  "0"))
    max_fob   = fob
    max_tow   = _si(get_text("weights/max_tow",  "0"))
    max_ldw   = _si(get_text("weights/max_ldw",  "0"))

    # ── Average wind (dir/spd) and temp dev ───────────────────────────────────
    avg_wind_dir = get_text("general/avg_wind_dir", "")
    avg_wind_spd = get_text("general/avg_wind_spd", "")
    try:
        avg_wind_s = f"{int(float(avg_wind_dir)):03d}/{int(float(avg_wind_spd)):03d}"
    except Exception:
        avg_wind_s = "---/---"

    avg_temp  = get_text("general/avg_temp_dev", "")
    try:
        avg_temp_i = int(float(avg_temp))
        avg_temp_s = f"M{abs(avg_temp_i):03d}" if avg_temp_i < 0 else f"P{avg_temp_i:03d}"
    except Exception:
        avg_temp_s = "M000"

    # ── Build text ────────────────────────────────────────────────────────────
    SEP = "-" * 63
    L = []

    # Title
    L.append(f"{airline} - FLIGHT PLAN - FLT {flt_num}")
    L.append(f"RTE {route_num:<8}   PLAN {plan_num}")
    L.append("")

    # Summary header row — column numbers shared with the data row below
    S_DAY, S_ORG, S_DST, S_ALT, S_DIS, S_CRZ, S_TYP, S_ENG, S_REG = \
        0, 4, 9, 14, 19, 25, 31, 44, 57
    L.append(_cols((S_DAY, "DAY"), (S_ORG, "ORG"), (S_DST, "DEST"), (S_ALT, "ALTN"),
                   (S_DIS, "DIST"), (S_CRZ, "CRZ"), (S_TYP, "ACFT TYPE"),
                   (S_ENG, "ENGINES"), (S_REG, "A/C")))
    L.append(_cols((S_DAY, _day, 3), (S_ORG, orig_iata, 4), (S_DST, dest_iata, 4),
                   (S_ALT, altn_icao, 4), (S_DIS, dist_nm, 5),
                   (S_CRZ, f"CI {cost_idx}", 5), (S_TYP, aircraft_type, 12),
                   (S_ENG, engine_type, 12), (S_REG, aircraft_reg)))
    L.append("")

    # ── Fuel / weight table ───────────────────────────────────────────────────
    # One column spec drives the header and every row.
    F_LBL, F_FUEL, F_TIME, F_WLBL, F_PLAN, F_LIM = 0, 13, 21, 29, 36, 44

    def _fw(lbl, fuel, time_s, wlbl, plan_w, lim_w=None):
        """Format one fuel/weight row.  time_s may be seconds, a string, or None."""
        if time_s is None:
            tf = "-----"
        elif isinstance(time_s, str):
            tf = time_s
        else:
            tf = _hhmm(time_s)
        return _cols((F_LBL, lbl, 12), (F_FUEL, f"{fuel:06d}", 6),
                     (F_TIME, tf, 5), (F_WLBL, wlbl, 5, ">"),
                     (F_PLAN, "" if plan_w is None else f"{plan_w}", 6, ">"),
                     (F_LIM,  "" if lim_w  is None else f"{lim_w}",  6, ">"))

    L.append(_cols((F_FUEL, "FUEL", 6, ">"), (F_TIME, "TIME", 5),
                   (F_PLAN, "PLAN", 6, ">"), (F_LIM, "LIMIT", 6, ">")))
    L.append(_fw("BURN",      enrt_burn,   enrt_fmt, "TXWT", txwt,  max_txwt))
    L.append(_fw("CONTNGNCY", cont_fuel,   cont_fmt, "SOW",  sow,   max_sow))
    L.append(_fw("ALTN",      altn_burn,   "00.00",  "PYLD", pyld))
    L.append(_fw("RESERVE",   resv_fuel,   resv_fmt, "ZFW",  zfw,   max_zfw))
    L.append(_fw("BRAKE RLS", brake_rls,   br_fmt,   "FOB",  fob,   max_fob))
    L.append(_fw("EXTRA",     extra_fuel,  None,     "TOW",  tow,   max_tow))
    L.append(_fw("TANKER",    tanker_fuel, None,     "BURN", enrt_burn))
    L.append(_fw("TAXI FUEL", taxi_fuel,   taxi_fmt, "LGW",  lgw,   max_ldw))
    L.append(_fw("RAMP FUEL", plan_ramp,   None,     "---",  None))
    L.append("")

    # Fuel-remaining at destination
    L.append(f"FUEL REMAINING AT DESTINATION {plan_ldg:06d} -INCL. PLANNED EXTRA-")
    L.append("")

    # Burn-per-1000-lb
    try:
        _zfw_p = _si(get_text("impacts/zfw_plus_1000/enroute_burn",  "0"))
        _zfw_m = _si(get_text("impacts/zfw_minus_1000/enroute_burn", "0"))
        # Only count an impact node that actually exists — a missing node reads
        # as 0 and would otherwise report the whole enroute burn as the delta.
        _diffs = [abs(v - enrt_burn) for v in (_zfw_p, _zfw_m) if v]
        _diffs = [d for d in _diffs if d > 0]
        _bpu   = int(round(sum(_diffs) / len(_diffs))) if _diffs else 0
    except Exception:
        _bpu = 0
    L.append(f"FUEL BURN INCREASE PER 1000 LBS ADDITIONAL TAKEOFF WT {_bpu:04d} LBS")
    L.append("")

    def _dd_to_dmm(dec_deg, is_lon=False):
        """Convert decimal degrees to Frontier 'Ndd mm.m' / 'Wddd mm.m' format."""
        try:
            v    = float(dec_deg)
            hemi = ("W" if v <= 0 else "E") if is_lon else ("N" if v >= 0 else "S")
            v    = abs(v)
            deg  = int(v)
            mins = (v - deg) * 60.0
            # Minutes must be zero-padded: 7.4' is "07.4", not "7.4", or the
            # decimal points stop lining up down the column.
            if is_lon:
                return f"{hemi}{deg:03d} {mins:04.1f}"
            else:
                return f"{hemi}{deg:02d} {mins:04.1f}"
        except Exception:
            return "???  ??.?"

    # Org-point lat/lon
    try:
        _orig_lat  = _sf(get_text("origin/pos_lat",  "0"))
        _orig_lon  = _sf(get_text("origin/pos_long", "0"))
        _lat_s = _dd_to_dmm(_orig_lat, is_lon=False)
        _lon_s = _dd_to_dmm(_orig_lon, is_lon=True)
        L.append(f"APT ORG PT {_lat_s} /{_lon_s}  AVG WIND. {avg_wind_s}  AVG TEMP. {avg_temp_s}")
    except Exception:
        L.append("")
    L.append("")

    # ATC FPL block
    fpl_text = get_text("atc/flightplan_text", "")
    if fpl_text:
        for _fl in fpl_text.splitlines():
            _fl = _fl.strip()
            if _fl:
                L.append(_fl)
        L.append("")

    # ── Contingency alt-change FL comparison lines ────────────────────────────
    # Format matches real Frontier OFP:
    #   FL 330 CI  0 B/O 00179 ETE 03.31
    #   FL 310 CI  0 B/O 00183 ETE 03.35
    # Source: impacts/cruisealt_plus_2000 and cruisealt_minus_2000 nodes,
    # falling back to the general/initial_altitude ± 20 FL with zero deltas.
    try:
        _fl_plan = _si(get_text("general/initial_altitude", "0")) // 100
        _fl_hi   = _fl_plan + 20
        _fl_lo   = _fl_plan - 20

        # Higher FL
        _burn_hi = _si(get_text("impacts/cruisealt_plus_2000/enroute_burn",  "0"))
        _ete_hi  = _si(get_text("impacts/cruisealt_plus_2000/ete",           "0"))
        _bo_hi   = abs(_burn_hi - enrt_burn) if _burn_hi else 0
        _ete_hi_s = _hhmm(_ete_hi) if _ete_hi else enrt_fmt

        # Lower FL
        _burn_lo = _si(get_text("impacts/cruisealt_minus_2000/enroute_burn", "0"))
        _ete_lo  = _si(get_text("impacts/cruisealt_minus_2000/ete",          "0"))
        _bo_lo   = abs(_burn_lo - enrt_burn) if _burn_lo else 0
        _ete_lo_s = _hhmm(_ete_lo) if _ete_lo else enrt_fmt

        L.append(f"FL {_fl_hi:03d} CI  {cost_idx}  {0:>2} B/O {_bo_hi:05d} ETE {_ete_hi_s}")
        L.append(f"FL {_fl_lo:03d} CI  {cost_idx}  {0:>2} B/O {_bo_lo:05d} ETE {_ete_lo_s}")
    except Exception:
        pass
    L.append("")

    # ── Navlog column spec ────────────────────────────────────────────────────
    # Line 1 (checkpoint line) and line 2 (leg-data line) share the same grid;
    # the three header lines are generated from these same constants so a column
    # can never be moved on one line without moving on the others.
    N_CKPT, N_LAT, N_LON, N_TAS, N_BURN = 0, 8, 18, 28, 41
    N_FREQ, N_FL, N_WIND, N_TEMP, N_MH  = 0, 8, 14, 21, 26
    N_MC, N_ZND, N_GS, N_ZNT            = 30, 34, 38, 42
    N_TIME, N_FRMG_P, N_FRMG_A          = 48, 54, 60

    # ── Navlog header ─────────────────────────────────────────────────────────
    L.append(_cols((N_FRMG_P, "PLAN"), (N_FRMG_A, "ACTUAL")))
    L.append(_cols((N_CKPT, "CKPT"), (N_LAT, "LAT"), (N_LON, "LONG"),
                   (N_TAS, "TAS"), (N_BURN, "BURN")))
    L.append(_cols((N_FREQ, "FREQ"), (N_FL, "FL"), (N_WIND, "WIND"),
                   (N_TEMP, "TEMP"), (N_MH, "MH"), (N_MC, "MC"),
                   (N_ZND, "ZND"), (N_GS, "GS"), (N_ZNT, "ZNT"),
                   (N_TIME, "TIME"), (N_FRMG_P, "FRMG"), (N_FRMG_A, "FRMG")))
    L.append("")

    # ── Navlog rows ───────────────────────────────────────────────────────────
    fixes = root.findall("navlog/fix")
    cum_fuel = plan_ramp  # fuel remaining starts at ramp

    def _fix_lat(f):
        try:
            v = _sf(f.findtext("pos_lat", "0"))
            return _dd_to_dmm(v, is_lon=False)
        except Exception:
            return "N?? ??.?"

    def _fix_lon(f):
        try:
            v = _sf(f.findtext("pos_long", "0"))
            return _dd_to_dmm(v, is_lon=True)
        except Exception:
            return "W??? ??.?"

    # Cumulative time tracker
    cum_secs = 0

    for idx, fix in enumerate(fixes):
        _ident   = (fix.findtext("ident") or "").strip().upper()
        _name    = (fix.findtext("name")  or "????").strip().upper()
        name_raw = _ident if _ident else _name
        if "TOP OF DESCENT" in _name:
            name_raw = "TOD"
        elif "TOP OF CLIMB" in _name:
            name_raw = "TOC"
        name = name_raw[:6]

        lat_s = _fix_lat(fix)
        lon_s = _fix_lon(fix)

        tas_raw = fix.findtext("true_airspeed", "")
        tas_s   = f"{_si(tas_raw):>3}" if _si(tas_raw) else "---"

        leg_burn = _si(fix.findtext("fuel_leg", "0"))
        cum_fuel = max(0, cum_fuel - leg_burn)

        leg_secs = _si(fix.findtext("time_leg", "0"))
        cum_secs += leg_secs

        # Fuel remaining (plan FRMG)
        frmg = _si(fix.findtext("fuel_plan_onboard", str(cum_fuel)))

        freq_raw = (fix.findtext("frequency", "") or "").strip()
        try:
            freq_s = f"{float(freq_raw):.1f}" if freq_raw else ""
        except (TypeError, ValueError):
            # Some fixes carry a non-numeric frequency (or an ident); show as-is.
            freq_s = freq_raw

        alt_raw = fix.findtext("altitude_feet", "")
        try:
            alt_fl = f"FL{_si(alt_raw) // 100:03d}" if alt_raw else ""
        except Exception:
            alt_fl = ""
        # Climb/descent legs are flagged CL/DC but keep the altitude visible.
        stage = (fix.findtext("stage", "") or "").upper()
        if stage in ("CLB", "CLIMB", "C"):
            alt_fl = f"CL{alt_fl[2:]}" if alt_fl else "CL"
        elif stage in ("DSC", "DESCENT", "D"):
            alt_fl = f"DC{alt_fl[2:]}" if alt_fl else "DC"

        wind_dir = _si(fix.findtext("wind_dir", "0"))
        wind_spd = _si(fix.findtext("wind_spd", "0"))
        wind_s   = f"{wind_dir:03d}{wind_spd:03d}"

        oat_raw  = fix.findtext("oat", "")
        try:
            oat_v = int(float(oat_raw))
            oat_s = f"M{abs(oat_v):02d}" if oat_v < 0 else f"P{oat_v:02d}"
        except Exception:
            oat_s = "---"

        hdg  = f"{_si(fix.findtext('heading_mag',  '0')):03d}"
        mc   = f"{_si(fix.findtext('track_mag',    '0')):03d}"
        # ZND = zone (leg) distance, ZNT = zone (leg) TIME.  ZNT previously
        # repeated the distance, which made the two columns identical.
        dist = f"{_si(fix.findtext('distance',     '0')):03d}"
        gs   = f"{_si(fix.findtext('groundspeed',  '0')):03d}"
        znt  = _hhmm(leg_secs)

        # Cumulative time display
        cum_min  = cum_secs // 60
        time_s   = f"{cum_min // 60:02d}.{cum_min % 60:02d}"

        if idx == 0:
            # Origin row — no leg data yet, only fuel on board
            L.append(_cols((N_CKPT, name, 6), (N_LAT, lat_s, 9), (N_LON, lon_s, 9),
                           (N_TAS, tas_s, 3, ">"), (N_BURN, "ORIGIN", 6),
                           (N_FRMG_P, f"{frmg:05d}", 5)))
            L.append(_cols((N_FREQ, freq_s, 6), (N_FL, alt_fl, 5)))
        else:
            # Line 1: checkpoint, position, TAS, leg burn
            L.append(_cols((N_CKPT, name, 6), (N_LAT, lat_s, 9), (N_LON, lon_s, 9),
                           (N_TAS, tas_s, 3, ">"), (N_BURN, f"{leg_burn:05d}", 5)))
            # Line 2: freq, FL, wind, temp, MH, MC, ZND, GS, ZNT, TIME, FRMG plan/actual
            L.append(_cols((N_FREQ, freq_s, 6), (N_FL, alt_fl, 5),
                           (N_WIND, wind_s, 6), (N_TEMP, oat_s, 3),
                           (N_MH, hdg, 3), (N_MC, mc, 3), (N_ZND, dist, 3),
                           (N_GS, gs, 3), (N_ZNT, znt, 5), (N_TIME, time_s, 5),
                           (N_FRMG_P, f"{frmg:05d}", 5), (N_FRMG_A, ".....", 5)))
        L.append("")

    L.append("")

    return "\n".join(L)


def _build_frontier_release_page(p):
    """
    Generate a Frontier Airlines-style dispatch release cover page (plain text).
    Matches the layout of the actual Frontier Airlines dispatch release form:

        FRONTIER AIRLINES - DISPATCH RELEASE - FLT 384

        RLSE FLT 384          MONTH 05 DAY 10
        IFR               KDEN/DEN          KFLL/FLL
        SKED TIMES        0705L/1305Z       1257L/1657Z
        FUEL COST/GAL     3.50              3.48

         //AIRCRAFT STATUS// CAT IIIB

        ALTN  N/R FOR KFLL/FLL

        FUEL DEN  25.8         SN N951FR
        BRAKE RLS 24.7
        ENRT BURN 17.6         SEATS- 138/186
        TAXI BURN 3.0 3
        FUEL REMAINING AT DESTINATION 7.9 -INCL. PLANNED EXTRA

           PMTOW 155.4/LS
             PTOW 147.7 PMRTW161.1/F01/B 0000/29.87/P12/17R
                        METW 176.4 METHOD- 1
             PLOW 130.0 MLDW 137.8/F04/S 0000/29.80/P24/27R

        MEL/CDL/NEF NONE

        RMKS - 30MIN CONTIG SCT STORM AT DEST

        CREW CA  ...
             FO  ...
             FA  ...
             FB  ...
             FC  ...

        DAVID HOOK          /RLSE TIME 101305
        DESK D2              CAPTAIN .............................

    p = dict of named parameters – see call site in parse_simbrief_data_to_howgozit_with_ofp.
    Returns a formatted string ready to be prepended to the OFP.
    """
    import textwrap as _tw
    from datetime import datetime as _dt

    # ── Inner helpers ────────────────────────────────────────────────────────
    def _qnh(raw):
        try:
            v = float(raw or 29.92)
            if v > 200:           # hPa → inHg
                v /= 33.8639
            return f"{v:.2f}"
        except Exception:
            return "29.92"

    def _temp(raw):
        try:
            t = int(float(raw or 15))
            return f"{'M' if t < 0 else 'P'}{abs(t):02d}"
        except Exception:
            return "P15"

    def _strip_icao(rwy, icao):
        r = str(rwy or "---").strip()
        if r.upper().startswith(icao.upper()):
            r = r[len(icao):].lstrip("/").strip()
        return r or "---"

    # ── Unpack ───────────────────────────────────────────────────────────────
    airline       = p["airline_display"]
    flt_num       = p["flight_number"]
    origin        = p["origin"]
    orig_iata     = p["origin_iata"]
    dest          = p["destination"]
    dest_iata     = p["destination_iata"]
    dep_loc       = p["sched_out_local"]
    dep_utc       = p["sched_out_fmt"]
    arr_loc       = p["sched_in_local"]
    arr_utc       = p["sched_in_fmt"]
    plan_ramp     = p["plan_ramp"]
    min_tkof      = p["min_takeoff"]
    enrt_burn     = p["enroute_burn"]
    taxi          = p["taxi_fuel"]
    plan_ldg      = p["plan_landing"]
    aircraft_reg  = p["aircraft_reg"]
    aircraft_type = p["aircraft_type"]
    seats         = p["pax_count_int"]
    max_pax       = p.get("max_pax_int", seats)
    cargo_lbs     = p.get("cargo_weight_int", 0)
    max_tow       = p["max_tow"]
    est_tow       = p["est_tow"]
    est_ldw       = p["est_ldw"]
    max_ldw       = p["max_ldw"]
    pmrtw_raw     = p["pmrtw_raw"]
    metw_raw      = p["metw_raw"]
    tow_lim_code  = (p.get("tow_limit_code") or "LS").strip() or "LS"
    fi            = p["flight_info"]
    cptn          = p["cptn"]
    fo_name       = p["fo"]
    pu            = p["pu"]
    fa_name       = p["fa"]
    dispatcher    = p["dispatcher"]
    dx_rmks       = p["dx_rmks"]
    altn_icao     = p["altn_icao"]
    sched_ts      = p["sched_out_ts"]
    dest_qnh_raw  = p.get("dest_qnh",  "29.92")
    dest_temp_raw = p.get("dest_temp", "15")
    dep_rwy_raw   = p.get("dep_runway", "---")
    arr_rwy_raw   = p.get("arr_runway", "---")

    # ── Month/Day and release time ────────────────────────────────────────────
    try:
        mo_day = _dt.utcfromtimestamp(int(sched_ts)).strftime("MONTH %m DAY %d")
    except Exception:
        mo_day = "MONTH -- DAY --"
    rlse_time = _dt.utcnow().strftime("%d%H%M")

    # ── Fuel cost/gal from config ─────────────────────────────────────────────
    cfg = _load_config()
    fc  = cfg.get("fuel_cost", {})
    orig_cost = fc.get(orig_iata, fc.get(origin, fc.get("default", "----")))
    dest_cost = fc.get(dest_iata, fc.get(dest,   fc.get("default", "----")))

    # ── Fuel in thousands ─────────────────────────────────────────────────────
    ramp_k = _fmt_fuel_k(plan_ramp)
    min_k  = _fmt_fuel_k(min_tkof)
    burn_k = _fmt_fuel_k(enrt_burn)
    taxi_k = _fmt_fuel_k(taxi)
    ldg_k  = _fmt_fuel_k(plan_ldg)

    # ── Performance weights ───────────────────────────────────────────────────
    pmtow_k = _fmt_fuel_k(max_tow)
    ptow_k  = _fmt_fuel_k(est_tow)
    plow_k  = _fmt_fuel_k(est_ldw)
    mldw_k  = _fmt_fuel_k(max_ldw)
    pmrtw_k = _fmt_fuel_k(pmrtw_raw)
    metw_k  = _fmt_fuel_k(metw_raw)

    # ── Departure perf params ─────────────────────────────────────────────────
    dep_qnh_s  = _qnh(fi.get("qnh",  "29.92"))
    dep_temp_s = _temp(fi.get("temp", "15"))
    dep_wind   = fi.get("wind", "0000")
    # Normalise wind to 4-digit dir + 2-digit speed: "270/15" → "2715", "000/00" → "0000"
    try:
        _wdir, _wspd = dep_wind.split("/")
        dep_wind_fmt = f"{int(_wdir):03d}{int(_wspd):02d}"
    except Exception:
        dep_wind_fmt = "00000"
    dep_rwy    = _strip_icao(dep_rwy_raw, origin)
    arr_rwy    = _strip_icao(arr_rwy_raw, dest)

    # ── Arrival perf params ───────────────────────────────────────────────────
    arr_qnh_s  = _qnh(dest_qnh_raw)
    arr_temp_s = _temp(dest_temp_raw)

    # ── Alternate line ────────────────────────────────────────────────────────
    ac = (altn_icao or "").strip()
    if ac in ("NONE", "NR", "", "0000", "N/R"):
        altn_line = f"ALTN      N/R FOR {dest}/{dest_iata}"
    else:
        altn_line = f"ALTN      {ac} FOR {dest}/{dest_iata}"

    # ── RMKS ─────────────────────────────────────────────────────────────────
    rmk_raw = " ".join(dx_rmks).strip() if dx_rmks else "NONE"
    if not rmk_raw:
        rmk_raw = "NONE"

    # ── Build text ────────────────────────────────────────────────────────────
    # Column positions match the PDF scan:
    #   Left col  ~col 0   Right col ~col 18
    # Header line width ~63 chars (matching separator)

    SEP = "-" * 63

    L = []

    # ── Title ─────────────────────────────────────────────────────────────────
    L.append(f"{airline} - DISPATCH RELEASE - FLT {flt_num}")
    L.append("")

    # ── RLSE / IFR / SKED / FUEL blocks ──────────────────────────────────────
    # Exactly matches:
    #   RLSE FLT 384          MONTH 05 DAY 10
    #   IFR               KDEN/DEN          KFLL/FLL
    #   SKED TIMES        0705L/1305Z       1257L/1657Z
    #   FUEL COST/GAL     3.50              3.48
    L.append(f"RLSE FLT {flt_num:<10}   {mo_day}")
    L.append(f"IFR{'':<14}   {origin}/{orig_iata:<13}   {dest}/{dest_iata}")
    L.append(f"SKED TIMES{'':<7}   {dep_loc}L/{dep_utc}Z{'':<6}   {arr_loc}L/{arr_utc}Z")
    L.append(f"FUEL COST/GAL{'':<4}   {orig_cost:<16}   {dest_cost}")
    L.append("")

    # ── Aircraft status ───────────────────────────────────────────────────────
    L.append(" //AIRCRAFT STATUS// CAT IIIB")
    L.append("")

    # ── Alternate ─────────────────────────────────────────────────────────────
    L.append(altn_line)
    L.append("")

    # ── Fuel summary — two-column layout ─────────────────────────────────────
    # PDF layout:
    #   FUEL DEN  25.8         SN N951FR
    #   BRAKE RLS 24.7
    #   ENRT BURN 17.6         SEATS- 138/186
    #   TAXI BURN 3.0 3        CARGO- 4500
    #   FUEL REMAINING AT DESTINATION 7.9 -INCL. PLANNED EXTRA
    L.append(f"FUEL {orig_iata:<3}  {ramp_k:<12}  SN {aircraft_reg}")
    L.append(f"BRAKE RLS {min_k}")
    L.append(f"ENRT BURN {burn_k:<12}  SEATS- {seats}/{max_pax}")
    L.append(f"TAXI BURN {taxi_k:<12}  CARGO- {cargo_lbs}")
    L.append(f"FUEL REMAINING AT DESTINATION {ldg_k} -INCL. PLANNED EXTRA")
    L.append("")

    # ── Performance weight block ──────────────────────────────────────────────
    # PDF layout (indented, exact spacing):
    #      PMTOW 155.4/LS
    #        PTOW 147.7 PMRTW161.1/F01/B 0000/29.87/P12/17R
    #                   METW 176.4 METHOD- 1
    #        PLOW 130.0 MLDW 137.8/F04/S 0000/29.80/P24/27R
    L.append(f"   PMTOW {pmtow_k}/{tow_lim_code}")
    L.append(f"     PTOW {ptow_k} PMRTW{pmrtw_k}/F01/B {dep_wind_fmt}/{dep_qnh_s}/{dep_temp_s}/{dep_rwy}")
    L.append(f"                   METW {metw_k} METHOD- 1")
    L.append(f"     PLOW {plow_k} MLDW {mldw_k}/F04/S {dep_wind_fmt}/{arr_qnh_s}/{arr_temp_s}/{arr_rwy}")
    L.append("")

    # ── MEL / RMKS ────────────────────────────────────────────────────────────
    L.append("MEL/CDL/NEF NONE")
    L.append("")
    for rmk_line in _tw.wrap(f"RMKS - {rmk_raw}", width=63):
        L.append(rmk_line)
    L.append("")

    # ── Crew block ────────────────────────────────────────────────────────────
    # PDF layout:
    #   CREW CA  DAVID HOOK
    #        FO  ...
    #        FA  ...
    #        FB  ...
    #        FC  ...
    L.append(f"CREW CA  {cptn}")
    L.append(f"     FO  {fo_name}")

    # FA row(s) — pu is the purser/lead FA; fa_name is a second FA
    fa_list = []
    if pu and str(pu).strip() not in ("N/A", "", "0"):
        fa_list.append(str(pu).strip())
    if fa_name and str(fa_name).strip() not in ("N/A", "", "0"):
        fa_list.append(str(fa_name).strip())

    fa_labels = ["FA", "FB", "FC", "FD", "FE"]
    for idx, fa in enumerate(fa_list):
        label = fa_labels[idx] if idx < len(fa_labels) else "FA"
        L.append(f"     {label}  {fa}")

    L.append("")

    # ── Dispatcher / signature block ──────────────────────────────────────────
    # PDF layout:
    #   DAVID HOOK          /RLSE TIME 101305
    #   DESK D2              CAPTAIN .............................
    disp_name = str(dispatcher).strip() if dispatcher and str(dispatcher).strip() not in ("N/A", "", "0") else "DISPATCHER"
    L.append(f"{disp_name:<20} /RLSE TIME {rlse_time}")
    L.append("")
    L.append(f"{'DESK D2':<20}  CAPTAIN .................................")
    L.append(SEP)
    L.append("")

    return "\n".join(L)


def save_as_pdf(filename, content):
    """
    Enhanced PDF generator with improved page break logic and larger image support.
    
    Improvements:
    - Smarter page breaks that avoid breaking mid-section
    - Larger images with landscape support for wide images
    - Better handling of separator lines
    - Reduced erroneous page breaks
    """
    if not content:
        print("ERROR: No content to save")
        return
    
    # Normalize content to lines — use split('\n') not splitlines() to preserve trailing spaces
    lines = []
    if isinstance(content, str):
        lines = content.split('\n')
    elif isinstance(content, (list, tuple)):
        for item in content:
            lines.extend(str(item).split('\n'))
    elif hasattr(content, '__iter__'):
        for item in content:
            lines.extend(str(item).split('\n'))
    else:
        lines = str(content).split('\n')
    
    if not lines:
        print("WARNING: No lines to write after processing")
        return
    
    try:
        # Start with portrait orientation
        _initial_buf = io.BytesIO()
        c = canvas.Canvas(_initial_buf, pagesize=letter)
        c._output_buf = _initial_buf
        width, height = letter
        font_size = 9
        left_margin = 75
        line_height = font_size + 2
        page_margin = 50
        # Image layout (separate from text margins)
        IMAGE_MARGIN = 24      # points (~0.33 inch)

        
        # Font selection
        user_choice = ask_font_selection()
        _RAINBOW_MODE = (user_choice == "4")
        _SEMI_BOLD_MODE_FONT = False  # kept for draw logic compatibility

        if user_choice == "2":
            user_font_path = "/System/Library/Fonts/Menlo.ttc"
        elif user_choice == "3":
            user_font_path = "/Users/tobygibbons/Library/Fonts/couriernew.ttf"
        elif user_choice == "4":
            user_font_path = "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"
        else:  # "1" Courier Normal
            user_font_path = "/Users/tobygibbons/Library/Fonts/CourierPrime-Regular.ttf"

        font_name = None
        if user_font_path and os.path.exists(user_font_path):
            try:
                pdfmetrics.registerFont(TTFont("CustomFont", user_font_path))
                font_name = "CustomFont"
            except Exception as e:
                print(f"DEBUG: Failed to load user font: {e}")

        if not font_name:
            fallback_paths = [
                "/Library/Fonts/Courier Prime.ttf",
                "/System/Library/Fonts/Supplemental/Courier New.ttf",
                "/Library/Fonts/Courier New.ttf",
                "/System/Library/Fonts/Courier.dfont",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
            ]
            for path in fallback_paths:
                if os.path.exists(path):
                    try:
                        pdfmetrics.registerFont(TTFont("FallbackFont", path))
                        font_name = "FallbackFont"
                        break
                    except:
                        continue

        if not font_name:
            font_name = "Courier"
        
        # Register Courier Prime for NOTAM/weather sections if non-courier font selected
        _COURIER_PRIME_PATH = "/Users/tobygibbons/Library/Fonts/CourierPrime-Regular.ttf"
        _notam_font = font_name  # default: same as body
        _courier_fonts = {"Courier", "Courier-Bold", "FallbackFont"}
        if font_name not in _courier_fonts and os.path.exists(_COURIER_PRIME_PATH):
            try:
                pdfmetrics.registerFont(TTFont("CourierPrimeNotam", _COURIER_PRIME_PATH))
                _notam_font = "CourierPrimeNotam"
            except Exception:
                pass

        c.setFont(font_name, font_size)

        # Initialize page state
        y = height - page_margin
        page_count = 1
        lines_written = 0
        i = 0
        current_orientation = "portrait"

        def is_section_header(line):
            """Check if line is a section header (contains ***)."""
            line_str = str(line).strip()
            return '***' in line_str or line_str.startswith('===')
        
        def is_separator(line):
            """Check if line is a separator (dashes)."""
            line_str = str(line).strip()
            return line_str.startswith('---') or line_str.startswith('___')
        
        def look_ahead_for_section(lines, start_idx, max_look=5):
            """Look ahead to see if we're about to start a new section."""
            for j in range(start_idx, min(start_idx + max_look, len(lines))):
                if is_section_header(lines[j]):
                    return True
            return False
        
        def get_block_size(lines, start_idx):
            """Estimate how many lines until next major break."""
            block_lines = 0
            for j in range(start_idx, len(lines)):
                line_str = str(lines[j]).strip()
                if '[PAGEBREAK]' in line_str:
                    break
                if is_section_header(lines[j]) and j > start_idx:
                    break
                block_lines += 1
                if block_lines > 10:  # Don't look too far ahead
                    break
            return block_lines

        while i < len(lines):
            line = lines[i]

            # ── NOTAM section: landscape two-column coloured layout ────────────
            if "[NOTAM_START]" in str(line):
                notam_lines = []
                i += 1
                while i < len(lines) and "[NOTAM_END]" not in str(lines[i]):
                    notam_lines.append(lines[i])
                    i += 1
                i += 1  # skip [NOTAM_END]
                notam_text = "\n".join(notam_lines)
                # Only close portrait page if content was drawn on it
                if y < height - page_margin:
                    c.showPage()
                if _draw_notam_section is not None:
                    _draw_notam_section(c, notam_text, _notam_font, font_size=8)
                    # _draw_notam_section ends with its own showPage()
                else:
                    # text mode: render NOTAM block inline as plain text
                    for notam_line in notam_lines:
                        line_str = str(notam_line).strip()
                        if not line_str:
                            y -= line_height
                            if y < page_margin:
                                c.showPage(); y = height - page_margin
                            continue
                        c.drawString(page_margin, y, line_str[:120])
                        y -= line_height
                        if y < page_margin:
                            c.showPage(); y = height - page_margin
                width, height = letter
                current_orientation = "portrait"
                c.setPageSize(letter)
                c.setFont(font_name, font_size)
                y = height - page_margin
                page_count += 1
                continue

            # ── Pre-rendered PDF blob (e.g. ETOPS / Oceanic pages) ────────────
            if str(line).startswith("[PDF_BLOB:"):
                import base64
                try:
                    from pypdf import PdfReader as _PdfReader, PdfWriter as _PdfWriter
                except ImportError:
                    print("WARNING: pypdf not installed — skipping ETOPS blob pages.")
                    i += 1
                    continue
                try:
                    b64 = str(line)[len("[PDF_BLOB:"):-1]  # strip marker
                    blob_bytes = base64.b64decode(b64)
                    # Finish the current portrait page so the blob inserts cleanly
                    if y < height - page_margin:
                        c.showPage()
                    # Write current canvas state to buffer then merge
                    _pre_buf = c._output_buf
                    c.save()
                    _pre_buf.seek(0)
                    # Merge: existing pages + blob pages
                    _writer = _PdfWriter()
                    _pre_data = _pre_buf.read()
                    if _pre_data:
                        _rdr_main = _PdfReader(io.BytesIO(_pre_data))
                        for _pg in _rdr_main.pages:
                            _writer.add_page(_pg)
                    _rdr_blob = _PdfReader(io.BytesIO(blob_bytes))
                    for _pg in _rdr_blob.pages:
                        _writer.add_page(_pg)
                    _merged_buf = io.BytesIO()
                    _writer.write(_merged_buf)
                    _merged_buf.seek(0)
                    c._pdf_prefix_pages = io.BytesIO()
                    c._pdf_prefix_pages.write(_merged_buf.read())
                    c._pdf_prefix_pages.seek(0)
                    # Start fresh canvas for remaining text
                    _saved_prefix = c._pdf_prefix_pages
                    _new_buf = io.BytesIO()
                    c = canvas.Canvas(_new_buf, pagesize=letter)
                    c._output_buf = _new_buf
                    c._pdf_prefix_pages = _saved_prefix
                    c.setFont(font_name, font_size)
                    width, height = letter
                    current_orientation = "portrait"
                    y = height - page_margin
                    page_count += 1
                    i += 1
                    continue
                except Exception as _blob_err:
                    print(f"WARNING: Could not splice PDF blob: {_blob_err}")
                    import traceback; traceback.print_exc()
                i += 1
                continue
            if "[PAGEBREAK]" in str(line):
                c.showPage()
                # Reset to portrait after manual page break
                if current_orientation == "landscape":
                    width, height = letter
                    current_orientation = "portrait"
                    c.setPageSize(letter)
                c.setFont(font_name, font_size)
                y = height - page_margin
                page_count += 1
                i += 1
                continue

            # ── Inline left-margin override: [MARGIN:n] sets left_margin to n pts ──
            if str(line).startswith("[MARGIN:") and str(line).strip().endswith("]"):
                try:
                    left_margin = float(str(line).strip()[8:-1])
                except ValueError:
                    pass
                i += 1
                continue

            # Handle images
            # Skip title line ONLY (never skip image lines)
            if (
                "[IMAGE:" not in str(line)
                and i + 1 < len(lines)
                and "[IMAGE:" in lines[i + 1]
                and lines[i + 1].strip().endswith("]")
            ):
                i += 1
                continue


            # Handle images (TITLE + IMAGE ON SAME LANDSCAPE PAGE)
            if "[IMAGE:" in str(line) and line.strip().endswith("]"):
                try:
                    url = line.strip()[7:-1]

                    next_is_image = (
                        i + 1 < len(lines)
                        and "[IMAGE:" in lines[i + 1]
                        and lines[i + 1].strip().endswith("]")
                    )

                    # Start image page
                    c.showPage()
                    width, height = landscape(letter)
                    c.setPageSize(landscape(letter))
                    current_orientation = "landscape"
                    c.setFont(font_name, font_size)
                    page_count += 1

                    y = height - IMAGE_MARGIN

                    # Draw title
                    title = lines[i - 1].strip() if i > 0 else ""
                    if title:
                        c.drawString(IMAGE_MARGIN, y, title)
                        y -= line_height * 2

                    response = requests.get(url, timeout=10)
                    response.raise_for_status()

                    img_data = io.BytesIO(response.content)
                    pil_img = Image.open(img_data)

                    available_width = width - (IMAGE_MARGIN * 2)
                    available_height = y - IMAGE_MARGIN

                    scale = min(
                        available_width / pil_img.width,
                        available_height / pil_img.height
                    )

                    img_width = pil_img.width * scale
                    img_height = pil_img.height * scale

                    x = (width - img_width) / 2
                    y_img = y - img_height
                    img_data.seek(0)

                    c.drawImage(
                        ImageReader(img_data),
                        x,
                        y_img,
                        width=img_width,
                        height=img_height
                    )

                    # ONLY switch back to portrait if next content is NOT an image

                except Exception as e:
                    print(f"WARNING: Could not embed image from {url}: {e}")
                    c.drawString(left_margin, y, f"[IMAGE FAILED: {url[:60]}...]")
                    y -= line_height
                    lines_written += 1

                i += 1
                continue

            # IMPROVED: Smart page break logic
            lines_needed = 1
            
            # Don't break before section headers
            if is_section_header(line):
                lines_needed = 3  # Header + some content
            
            # Don't break on separators if next content is close
            if is_separator(line) and look_ahead_for_section(lines, i + 1):
                lines_needed = 5  # Keep separator with next section
            
            # Check if we have enough space for upcoming block
            if i < len(lines) - 1:
                block_size = get_block_size(lines, i)
                if block_size < 3:  # Small block, try to keep together
                    lines_needed = max(lines_needed, block_size)
            
            # Calculate space needed
            space_needed = lines_needed * line_height
            
            # Automatic page break only if truly necessary
            if y - space_needed < page_margin:
                # Don't break if we're just one or two lines from bottom and it's a separator
                if not (is_separator(line) and y - line_height > page_margin):
                    c.showPage()
                    # Reset to portrait after automatic page break
                    if current_orientation == "landscape":
                        width, height = letter
                        current_orientation = "portrait"
                        c.setPageSize(letter)
                    c.setFont(font_name, font_size)
                    y = height - page_margin
                    page_count += 1

            # Draw text line
            try:
                line_str = str(line) if line is not None else ""
                line_str = line_str.encode("utf-8", errors="replace").decode("utf-8")
                if False:  # semi-bold mode removed
                    # Fake semi-bold: draw twice with tiny offset
                    c.drawString(left_margin, y, line_str)
                    c.drawString(left_margin + 0.3, y, line_str)
                elif _RAINBOW_MODE and line_str.strip():
                    _RAINBOW_COLORS = [
                        (0.93, 0.11, 0.14),  # red
                        (0.99, 0.50, 0.05),  # orange
                        (0.99, 0.85, 0.05),  # yellow
                        (0.18, 0.72, 0.22),  # green
                        (0.10, 0.46, 0.98),  # blue
                        (0.56, 0.15, 0.80),  # violet
                    ]
                    x_cursor = left_margin
                    for _ci, _ch in enumerate(line_str):
                        _r, _g, _b = _RAINBOW_COLORS[_ci % len(_RAINBOW_COLORS)]
                        c.setFillColorRGB(_r, _g, _b)
                        c.drawString(x_cursor, y, _ch)
                        x_cursor += c.stringWidth(_ch, font_name, font_size)
                    c.setFillColorRGB(0, 0, 0)  # reset to black
                else:
                    c.drawString(left_margin, y, line_str)
                lines_written += 1
            except Exception as e:
                print(f"WARNING: Could not write line {i+1}: {e}")
                c.drawString(left_margin, y, f"[LINE {i+1} ERROR]")

            y -= line_height
            i += 1

        # Save canvas and write final PDF to disk
        c.save()  # flush canvas to _output_buf
        if hasattr(c, '_pdf_prefix_pages') and hasattr(c, '_output_buf'):
            try:
                from pypdf import PdfReader as _PR, PdfWriter as _PW
                _final = _PW()
                c._pdf_prefix_pages.seek(0)
                for _pg in _PR(c._pdf_prefix_pages).pages:
                    _final.add_page(_pg)
                c._output_buf.seek(0)
                for _pg in _PR(c._output_buf).pages:
                    _final.add_page(_pg)
                with open(filename, "wb") as _fh:
                    _final.write(_fh)
            except ImportError:
                c._output_buf.seek(0)
                with open(filename, "wb") as _fh:
                    _fh.write(c._output_buf.read())
        else:
            c._output_buf.seek(0)
            with open(filename, "wb") as _fh:
                _fh.write(c._output_buf.read())
        print(f"SUCCESS: PDF saved to {filename}")
        print(f"DEBUG: Pages created: {page_count}")
        print(f"DEBUG: Total lines written: {lines_written}")

    except ImportError as e:
        print(f"ERROR: Missing library: {e}. Install reportlab and Pillow.")
    except Exception as e:
        print(f"ERROR: Failed to create PDF: {e}")
        import traceback
        traceback.print_exc()
        
import json
from tkinter import filedialog

def get_last_output_folder():
    folder = _load_config().get("output_folder", "")
    return folder if folder and os.path.exists(folder) else None

def save_last_output_folder(folder):
    cfg = _load_config()
    cfg["output_folder"] = folder
    _save_config(cfg)

def prompt_for_output_folder():
    root = tk.Tk()
    root.withdraw()
    last_folder = get_last_output_folder()
    folder_selected = filedialog.askdirectory(
        initialdir=last_folder or os.getcwd(),
        title="Select Output Folder"
    )
    root.destroy()
    if folder_selected:
        save_last_output_folder(folder_selected)
    return folder_selected

# Example usage and test function




# Test your content before saving PDF
def test_howgozit_generation():
    """Test function to debug the HOWGOZIT generation"""
    try:
        # Fetch your data
        xml_data = fetch_simbrief_data(user_id)
        print("DEBUG: XML data fetched successfully")
        
        # Get takeoff time (you can hardcode this for testing)
        takeoff_time = "1234"  # or use your prompt function
        
        # Generate HOWGOZIT
        howgozit = parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time)
        
        # Debug the result
        debug_content(howgozit, "HOWGOZIT Result")
        
        return howgozit
        
    except Exception as e:
        print(f"ERROR in test: {e}")
        import traceback
        traceback.print_exc()
        return None


import random

# First names
first_names = [
    "YUDIEL", "MARISOL", "ELVIN", "JOSELYN", "RAFAEL",
    "LISBETH", "NEYMAR", "ADALYN", "FRANCHY", "DEYSI",
    "LEANDRO", "SARAY", "ESTEFANY", "RODRIGO", "JHOAN",
    "MARIEL", "GIOVANNY", "ANTONELLA", "DARWIN", "YULIANA",
    "TRENTON", "BRIELLE", "COLTON", "PRESLEY", "JOCELYN",
    "HARRISON", "ADDISON", "GIDEON", "MARVIN", "EVELYN",
    "XIOMARA", "YORDANO", "LÁZARO", "DAYANA", "YOEL",
    "ARLETTY", "OSMANY", "YANELI", "LÁZARO", "YESSENIA",
    "ALEJANDRO", "VALENTINA", "SANTIAGO", "CAMILA", "MATEO",
    "ISABELLA", "SEBASTIÁN", "SOFÍA", "DIEGO", "VALERIA",
    "CARLOS", "GABRIELA", "JAVIER", "DANIELA", "MIGUEL",
    "CAROLINA", "ANDRÉS", "NATALIA", "MANUEL", "ADRIANA",
    "YULISSA", "YANCARLOS", "LEIDYS", "YEISON", "YENIFER",
    "DAYRON", "YANELIS", "MAIKEL", "YAIMARA", "YOANDRY",
    "GENESIS", "ANGELINA", "FERNANDO", "LUCIA", "EMILIO",
    "JAZMIN", "ORLANDO", "MELISSA", "RICARDO", "PAOLA",
    # Haitian names
    "WIDLEY", "KERVENS", "FRANTZY", "WADLEY", "ROODY",
    "WOODLY", "WEDLYNE", "LOVENCIA", "PHARA", "DJULY",
    "GUERLINE", "JOEVENS", "KENSLY", "WIDMARC", "JUNIOR",
    "WISLY", "MACKENDY", "NAIKA", "FEDNER", "LOUIDJY",
    "JHONSON", "GUERDA", "FABIOLA", "MACKENSON", "ROSELINE",
    # More Caribbean
    "KADEEM", "SHAKIRA", "JAMAR", "KEISHA", "TYRONE",
    "LAKEISHA", "DEVONTE", "TAMARA", "DARNELL", "SHANIQUA",
    "TREVOR", "AALIYAH", "JAHEIM", "LATOYA", "MARCUS",
    "TANISHA", "JAMAAL", "MONIQUE", "KEON", "BRIANNA",
    # Brazilian/South American
    "THIAGO", "LARISSA", "BRUNO", "BIANCA", "LUCAS",
    "LETICIA", "GUSTAVO", "JULIANA", "VINICIUS", "PRISCILA",
    # Murica
    "BRANDON", "ASHLEY", "TYLER", "BRITTANY", "JUSTIN",
    "MEGAN", "RYAN", "AMANDA", "KYLE", "JENNIFER",
    "ZACHARY", "JESSICA", "AUSTIN", "LAUREN", "CODY",
    "COURTNEY", "BLAKE", "MADISON", "HUNTER", "TAYLOR",
    "CONNOR", "EMMA", "PARKER", "OLIVIA", "MASON",
    "SOPHIA", "ETHAN", "CHARLOTTE", "LANDON", "ABIGAIL",
    "JACKSON", "HANNAH", "CALEB", "SARAH", "NATHAN",
    "EMILY", "COOPER", "GRACE", "LOGAN", "AVA",
    "WYATT", "CHLOE", "GAVIN", "ELLA", "OWEN",
    "LILY", "DEREK", "MIA", "TRAVIS", "ALLISON",
    "BRETT", "HALEY", "CHASE", "PAIGE", "GARRETT",
    "BROOKE", "SPENCER", "KAITLYN", "WESLEY", "ALEXIS",
    "SAWYER", "MORGAN", "DALTON", "MACKENZIE", "TANNER",
    "KELSEY", "MITCHELL", "LINDSEY", "CAMERON", "SHELBY"
]

# Last names
last_names = [
    "DE LA CRUZ", "TEJADA", "GUERRERO", "ROJAS", "CABRERA",
    "FELIZ", "MARTÍNEZ", "SÁNCHEZ", "PERALTA", "VARGAS",
    "FERNÁNDEZ", "GUTIÉRREZ", "PÉREZ", "SOTO", "LÓPEZ",
    "MENDOZA", "CHACÓN", "TORRES", "RODRÍGUEZ", "RAMÍREZ",
    "BRADLEY", "HAWTHORNE", "ELLIS", "MONTGOMERY", "KENSINGTON",
    "WINTHROP", "HOLLINGSWORTH", "TREMBLAY", "WHITAKER", "CHAMBERLAIN",
    "GARCÍA", "HERNÁNDEZ", "GONZÁLEZ", "DÍAZ", "MORALES",
    "CASTRO", "REYES", "ORTIZ", "RAMOS", "CRUZ",
    "FLORES", "JIMÉNEZ", "ÁLVAREZ", "ROMERO", "RUIZ",
    "VEGA", "MORENO", "MÉNDEZ", "SILVA", "NÚÑEZ",
    "SANTOS", "RIVERA", "ARIAS", "MEDINA", "AGUILAR",
    "DELGADO", "CASTILLO", "VALDEZ", "ESCOBAR", "SANDOVAL",
    "NAVARRO", "CORTÉS", "FUENTES", "BLANCO", "RÍOS",
    # Haitian surnames
    "JEAN", "PIERRE", "BAPTISTE", "JOSEPH", "CHARLES",
    "LOUIS", "FRANCOIS", "ETIENNE", "SAINT-JEAN", "PAUL",
    "AUGUSTIN", "MICHEL", "JEAN-LOUIS", "JEAN-BAPTISTE", "ANDRE",
    "TOUSSAINT", "SIMEON", "NOEL", "JEAN-PIERRE", "DESIR",
    "INNOCENT", "MOISE", "JULIEN", "GEORGES", "RAYMOND",
    # Caribbean surnames
    "WILLIAMS", "BROWN", "JOHNSON", "DAVIS", "THOMPSON",
    "CAMPBELL", "JONES", "FRANCIS", "BAPTISTE", "RICHARDS",
    "ALEXANDER", "THOMAS", "JACKSON", "EDWARDS", "GARCIA",
    # Brazilian/South American
    "DA SILVA", "DOS SANTOS", "OLIVEIRA", "PEREIRA", "COSTA",
    "FERREIRA", "RODRIGUES", "ALMEIDA", "NASCIMENTO", "LIMA",
    # Murica
    "SMITH", "ANDERSON", "WILSON", "MOORE", "TAYLOR",
    "MILLER", "CLARK", "WHITE", "HARRIS", "MARTIN",
    "WALKER", "HALL", "ALLEN", "YOUNG", "KING",
    "WRIGHT", "SCOTT", "GREEN", "BAKER", "ADAMS",
    "NELSON", "CARTER", "MITCHELL", "ROBERTS", "TURNER",
    "PHILLIPS", "CAMPBELL", "PARKER", "EVANS", "COLLINS",
    "STEWART", "MORRIS", "ROGERS", "REED", "COOK",
    "MORGAN", "BELL", "MURPHY", "BAILEY", "RIVERA",
    "COOPER", "RICHARDSON", "COX", "HOWARD", "WARD",
    "PETERSON", "GRAY", "JAMES", "WATSON", "BROOKS",
    "KELLY", "SANDERS", "PRICE", "BENNETT", "WOOD",
    "BARNES", "ROSS", "HENDERSON", "COLEMAN", "JENKINS",
    "PERRY", "POWELL", "LONG", "PATTERSON", "HUGHES"
]

def random_name():
    """Return a random full name from the first_names and last_names lists"""
    try:
        return f"{random.choice(first_names)} {random.choice(last_names)}"
    except IndexError:
        return "John Smith"



def get_or_prompt_username():
    """Return saved SimBrief username from config, or prompt once via tkinter and save it."""
    uid = _load_config().get("user_id", "").strip()
    if uid:
        return uid
    root = tk.Tk()
    root.withdraw()
    uid = simpledialog.askstring(
        "SimBrief Username",
        "Enter your SimBrief username:\n(saved for future runs)"
    )
    root.destroy()
    if not uid or not uid.strip():
        print("No username entered. Exiting.")
        sys.exit(1)
    uid = uid.strip()
    cfg = _load_config()
    cfg["user_id"] = uid
    _save_config(cfg)
    return uid

def pad_if_number(s):
    try:
        n = int(s)
        if n < 100:
            return f"{n:02d}"
        return str(n)
    except ValueError:
        return s  # return as-is if not a number (like '---')

def seconds_to_hhmm(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}{m:02d}"

def format_time_elapsed(seconds):
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
        if seconds <= 0:
            return ""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:02d}{minutes:02d}"
    except:
        return ""

def format_Alt_time(seconds):
    if seconds is None:
        return "0000"
    try:
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:02d}{minutes:02d}"
    except:
        return "0000"

def format_fuel(fuel_value):
    """Format fuel value consistently - no trailing zeros for whole numbers"""
    try:
        if fuel_value is None or fuel_value == "":
            return "0"
        val = float(fuel_value)
        # Return integer format if it's a whole number, otherwise 1 decimal place
        return f"{val:.0f}" if val == int(val) else f"{val:.1f}"
    except (ValueError, TypeError):
        return "0"

def format_off_time(t):
    return t.replace(":", "") if ":" in t else t

def add_times(t1, t2):
    h1, m1 = int(t1[:2]), int(t1[2:])
    h2, m2 = int(t2[:2]), int(t2[2:])
    total_m = m1 + m2
    total_h = h1 + h2 + total_m // 60
    total_m = total_m % 60
    total_h = total_h % 24
    return f"{total_h:02d}{total_m:02d}"

def calculate_time_difference(scheduled_seconds, planned_seconds):
    try:
        diff = int(planned_seconds) - int(scheduled_seconds)
        abs_diff = abs(diff)
        hours = abs_diff // 3600
        minutes = (abs_diff % 3600) // 60
        return f"{hours:02d}{minutes:02d}{'L' if diff > 0 else 'E'}"
    except:
        return "0000E"

def add_time_to_takeoff(takeoff_time, seconds_elapsed):
    try:
        if isinstance(takeoff_time, str):
            hours, minutes = int(takeoff_time[:2]), int(takeoff_time[2:])
            base_time = datetime.utcnow().replace(hour=hours, minute=minutes, second=0, microsecond=0)
        else:
            base_time = takeoff_time
        return (base_time + timedelta(seconds=int(seconds_elapsed))).strftime("%H%M")
    except:
        return "----"

def fetch_simbrief_data(user_id):
    url = f"https://www.simbrief.com/api/xml.fetcher.php?username={user_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"Error fetching SimBrief data: {e}")
        sys.exit(1)

def format_time_endurance(seconds):
    if not isinstance(seconds, int) or seconds <= 0:
        return "00+00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}{m:02d}"

def get_element_text(parent, tag_name, default=""):
    """Safely extract text from XML elements, with optional default fallback."""
    element = parent.find(tag_name)
    return element.text.strip() if element is not None and element.text else default

def format_out_time(time_str):
    if not time_str or len(time_str) != 4 or not time_str.isdigit():
        return "0000"
    return f"{time_str[:2]}:{time_str[2:]}"

def prompt_for_takeoff_time_str(default_hhmm):
    """Disabled - automatically use scheduled departure time"""
    # Simply return the default time formatted correctly
    return format_out_time(default_hhmm)



def calculate_tldr(root, current_fix_index):
    try:
        all_fixes = root.findall("navlog/fix")
        cumulative_distance = 0.0

        for i in range(current_fix_index + 1):  # include current fix
            distance_str = all_fixes[i].findtext("distance", "0")
            try:
                distance_nm = float(distance_str)
                cumulative_distance += distance_nm
            except ValueError:
                continue  # skip invalid distances

        total_route_distance = sum(
            float(fix.findtext("distance", "0") or 0) for fix in all_fixes
        )

        tldr = total_route_distance - cumulative_distance
        return str(int(tldr)) if tldr.is_integer() else f"{tldr:.1f}"

    except Exception as e:
        print(f"Error calculating TLDR: {e}")
        return "0"


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_weight(val):
    try:
        return float(val)/1000  # Keep the fraction
    except:
        return 'ERR'


def is_valid_runway(runway, struct_wt_limit=0.0):
    """
    Returns False if company policy excludes this runway.
    Policy: if STRUCT WT LIMIT > 100,000 lbs (100.0 thousands),
    exclude runways shorter than 4,800 ft.
    """
    if struct_wt_limit > 100.0:
        try:
            length = int(float(runway.findtext('length', '0') or 0))
            if length < 4800:
                print(f"[DEBUG] Runway {runway.findtext('identifier','?')} excluded: "
                      f"length={length}ft < 4800ft (struct_wt={struct_wt_limit:.1f}k)")
                return False
        except Exception:
            pass
    return True


def extract_runway_data(xml_root):
    """
    Extract runway data, flight info, and anti-ice status from SimBrief XML.
    Returns: (valid_runways:list, flight_info:dict, anti_ice_on:bool, runway_lines:list)
    """
    try:
        import json
        from datetime import datetime

        # --- Parse acdata_parsed JSON ---
        # --- Parse aircraft JSON ---
        acdata_tag = xml_root.find('.//acdata_parsed')
        if acdata_tag is not None and acdata_tag.text and acdata_tag.text.strip():
            try:
                acdata = json.loads(acdata_tag.text.strip())
                aircraft_name = acdata.get('name', 'UNKNOWN')
                engine_type   = acdata.get('comments', 'UNKNOWN')
                maxcargo = acdata.get('maxcargo', 0)
                print(f"DEBUG: Loaded acdata for {acdata.get('reg','UNKNOWN')} - {aircraft_name} / {engine_type}")
            except json.JSONDecodeError as e:
                print(f"ERROR parsing acdata_parsed: {e}")
                aircraft_name = 'UNKNOWN'
                engine_type = 'UNKNOWN'
        else:
            print("WARNING: acdata_parsed not found or empty")
            aircraft_name = 'UNKNOWN'
            engine_type = 'UNKNOWN'



        # --- Extract takeoff conditions ---
        conditions = xml_root.find('.//tlr/takeoff/conditions')
        if conditions is not None:
            surface_condition = conditions.findtext('surface_condition', 'dry').lower()
            flight_info = {
                'temp': conditions.findtext('temperature', '0'),
                'qnh': conditions.findtext('altimeter', '0'),
                'engine': engine_type,
                'aircraft': aircraft_name,
                'surface_condition': surface_condition,
                'wind': f"{conditions.findtext('wind_direction', '0')}/{conditions.findtext('wind_speed', '0')}",
                'tow': conditions.findtext('planned_weight', '0')
            }
        else:
            print("WARNING: Takeoff conditions missing, using defaults.")
            flight_info = {
                'temp': '0',
                'qnh': '0',
                'engine': engine_type,
                'aircraft': aircraft_name,
                'surface_condition': 'dry',
                'wind': '0/0',
                'tow': '0'
            }

        # --- Extract header fields ---
        flt_node = xml_root.find('.//flight_number')
        dte_node = xml_root.find('.//date_time')
        fin_node = xml_root.find('.//fin')
        airport_node = xml_root.find('.//airport_icao')
        alt_node = xml_root.find('.//departure_airport/elevation') or xml_root.find('.//departure_airport/altitude')
        max_elevation = float(alt_node.text) if alt_node is not None and alt_node.text else 0

        # --- Process runways ---
        valid_runways = []
        anti_ice_on = False
        runways = xml_root.findall('.//tlr/takeoff/runway')
        print(f"DEBUG: Found {len(runways)} runway elements in XML")

        # --- V-speed sanitizer ---
        def sanitize_vspeed(val):
            if not val or val.upper() in ["ERR", "XXX"]:
                return "XXX"
            try:
                num = int(val)
                return str(num) if num > 0 else "XXX"
            except:
                return "XXX"

        # Struct weight limit for runway length filtering (in thousands lbs)
        try:
            _struct_wt_limit = float(xml_root.findtext('weights/max_tow_struct', '0') or 0) / 1000.0
        except Exception:
            _struct_wt_limit = 0.0
        print(f"[DEBUG] struct_wt_limit={_struct_wt_limit:.1f}k — runway length filter {'ACTIVE' if _struct_wt_limit > 100.0 else 'inactive'}")

        for runway in runways:
            anti_ice_setting = runway.findtext('anti_ice_setting', 'OFF').upper().strip()
            if anti_ice_setting and anti_ice_setting != 'OFF':
                anti_ice_on = True

            if is_valid_runway(runway, _struct_wt_limit):
                v1 = sanitize_vspeed(runway.findtext('speeds_v1', '0'))
                vr = sanitize_vspeed(runway.findtext('speeds_vr', '0'))
                v2 = sanitize_vspeed(runway.findtext('speeds_v2', '0'))

                runway_data = {
                    'other': runway.findtext('speeds_other', '0'),
                    'other_label': runway.findtext('speeds_other_id', 'XX'),
                    'id': runway.findtext('identifier', 'XX'),
                    'v1': v1,
                    'vr': vr,
                    'v2': v2,
                    'flex': runway.findtext('flex_temperature', 'XXX'),
                    'thr': runway.findtext('thrust_setting', 'TOGA'),
                    'flaps': runway.findtext('flap_setting', 'XX'),
                    'length': runway.findtext('length', '0000'),
                    'bleed': runway.findtext('bleed_setting', '0.0'),
                    'HD': runway.findtext('headwind_component', 'XX'),
                    'gradient': safe_float(runway.findtext('gradient', 'x.x')),
                    'max_weight': safe_float(runway.findtext('max_weight', 0)),
                    'max_temp': safe_float(runway.findtext('max_temperature', 'XX')),
                    'asdr': safe_float(runway.findtext('distance_margin','0.0')),
                    'elevation': safe_float(runway.findtext('elevation')) or 'ERR',
                    'limit_code': runway.findtext('limit_code', ''),
                    'runway_message': runway.findtext('runway_message', '') or runway.findtext('message', ''),
                    'est_tow': safe_float(xml_root.findtext("weights/est_tow", 0)),
                    'atow': safe_float(xml_root.findtext("fuel/plan_takeoff", 0)),
                    'est_zfw': safe_float(xml_root.findtext("weights/est_zfw", 0)),
                    'fuel': safe_float(xml_root.findtext("fuel/plan_ramp", 0)),
                    'taxi_fuel': safe_float(xml_root.findtext("fuel/taxi", 0)),
                    'airport': conditions.findtext('airport_icao', 'ERR') if conditions is not None else 'ERR',
                    'qnh': flight_info.get('qnh','ERR'),
                    'temp': flight_info.get('temp','ERR'),
                    'surface_condition': flight_info.get('surface_condition','ERR').upper(),
                    'engine': flight_info.get('engine','ERR'),
                    'aircraft': flight_info.get('aircraft','ERR'),
                    'icaocode': flight_info.get('icaocode','ERR'),
                    'wind': flight_info.get('wind','ERR'),
                    'tow': flight_info.get('tow','ERR'),
                    'flight_number': flt_node.text.strip() if flt_node is not None and flt_node.text else 'ERR',
                    'fin': fin_node.text.strip() if fin_node is not None and fin_node.text else 'ERR',
                    'dte_time': dte_node.text.strip() if dte_node is not None and dte_node.text else datetime.utcnow().strftime("%d/%H%MZ"),
                }

                valid_runways.append(runway_data)
                print(f"DEBUG: Added runway {runway_data['id']} to valid_runways")
            else:
                print(f"DEBUG: Runway {runway.findtext('identifier', 'XX')} filtered out by is_valid_runway()")

        # --- Build runway_lines for takeoff performance section ---
        runway_lines = []
        for r in valid_runways:
            line = (
                f"{r['id']:<4} {r['flaps']:<4} {r['bleed']:<4} "
                f"{r['v1']:<4} {r['vr']:<4} {r['v2']:<4} "
                f"{r['thr']:<6} {r['flex']:<6} {r['max_weight']}{r['limit_code']:<3} "
                f"{r['other_label']:<8}{r['other']:<6}"
            )
            runway_lines.append(line)

        print(f"DEBUG: runway_lines built with {len(runway_lines)} entries")
        print(f"DEBUG: Final valid_runways count: {len(valid_runways)}")

        return valid_runways, flight_info, anti_ice_on, runway_lines

    except Exception as e:
        print(f"Error in extract_runway_data: {e}")
        import traceback
        traceback.print_exc()
        return [], {}, False, []




from ENGINEFAILPROC import get_airport_specific_altitudes





# --- XML Parsing Function ---
def parse_xml_string(xml_string):
    """Parse XML string into ElementTree object"""
    import xml.etree.ElementTree as ET
    try:
        if isinstance(xml_string, str):
            return ET.fromstring(xml_string)
        return xml_string  # Already parsed
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        return None

# --- XML Text Extraction Helper ---
def get_text(xpath, root, default=""):
    """
    Safely extract text from XML element using xpath.
    Handles string XML roots by parsing them first.
    """
    try:
        if isinstance(root, str):
            root = parse_xml_string(root)
            if root is None:
                return default

        if hasattr(root, 'find'):
            elem = root.find(xpath)
            if elem is None:
                elem = root.find(".//" + xpath.split('/')[-1])
        else:
            print(f"Warning: Unexpected root type: {type(root)}")
            return default

        if elem is None:
            return default
        if not hasattr(elem, 'text'):
            return default
        return elem.text.strip() if elem.text else default

    except Exception as e:
        print(f"Error extracting text from xpath '{xpath}': {e}")
        return default

    ###HERE###
def parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time):
    try:
        root = ET.fromstring(xml_data)
        get_text = lambda path, default="": (root.find(path).text if root.find(path) is not None else default)
        get_all = lambda path: [el.text.strip() if el.text else '' for el in root.findall(path)]

        # Basic flight info
        valid_runways, flight_info, anti_ice_on, runway_lines = extract_runway_data(root)
        icao_airline = get_text("general/icao_airline")

        # ── Airline ICAO override (config.json key: "airline_override") ─────────
        # Set "airline_override": "NAC" (or any ICAO code) in config.json to force
        # the header display name and FPL prefix regardless of what SimBrief sends.
        # Remove the key or set it to "" to revert to the SimBrief value.
        _cfg_airline = _load_config().get("airline_override", "").strip().upper()
        if _cfg_airline:
            icao_airline = _cfg_airline
        flight_number = get_text("general/flight_number")
        origin = get_text("origin/icao_code")
        origin_iata = get_text("origin/iata_code")
        origin_elev = get_text("origin/elevation")
        destination = get_text("destination/icao_code")
        destination_iata = get_text("destination/iata_code")
        destination_elev = get_text("destination/elevation")
        altn_iata = get_text('alternate/iata_code', "NONE")
        release_time = get_text("general/release")
        cruise_profile = get_text("general/cruise_profile")
        avg_wind_dir = get_text("general/avg_wind_dir")
        avg_wind_spd = get_text("general/avg_wind_spd")
        avg_temp_dev = get_text("general/avg_temp_dev")
        orig_metar = get_text("weather/orig_metar")
        orig_taf = get_text("weather/orig_taf")
        orig_atis = get_text("weather/orig_atis")
        dest_metar = get_text("weather/dest_metar")
        dest_taf = get_text("weather/dest_taf")
        dest_atis = get_text("weather/dest_atis")
        altn_metar = get_text("weather/altn_metar")
        altn_taf = get_text("weather/altn_taf")
        altn_atis = get_text("weather/altn_atis")
        toaltn_metar = get_text("weather/toaltn_metar")
        toaltn_taf = get_text("weather/toaltn_taf")
        toaltn_atis = get_text("weather/toaltn_atis")
        eualtn_metar = get_text("weather/eualtn_metar")
        eualtn_taf = get_text("weather/eualtn_taf")
        eualtn_atis = get_text("weather/eualtn_atis")
        etops_metar = get_text("weather/etops_metar")
        etops_taf = get_text("weather/etops_taf")
        etops_atis = get_text("weather/etops_atis")

        # Fuel calculations
        plan_ramp = format_fuel(get_text("fuel/plan_ramp"))
        PTOF = format_fuel(get_text("fuel/plan_takeoff"))
        plan_landing = format_fuel(get_text("fuel/plan_landing"))
        alternate_burn = format_fuel(get_text("fuel/alternate_burn"))
        reserve = format_fuel(get_text("fuel/reserve"))
        enroute_time_seconds = get_text("times/est_time_enroute", "0")
        enroute_time = format_time_elapsed(enroute_time_seconds)
        route_distance = float(get_element_text(root, "general/route_distance"))  

        # Get fuel times from XML (fixed to read from correct locations)
        taxi_time = format_time_elapsed(get_text("fuel/taxi_time", "0"))
        est_enroute_time_sec = int(get_text("times/est_time_enroute", "0")) % 86400
        sched_enroute_time_sec = int(get_text("times/sched_time_enroute", "0")) % 86400

        alternate_time = format_time_elapsed(get_text("alternate/ete", "0"))
        reserve_time = format_time_elapsed(get_text("times/reserve_time", "0"))
        contingency_time = format_time_elapsed(get_text("times/contfuel_time", "0"))
        etops_time = format_time_elapsed(get_text("times/etopsfuel_time", "0"))
        extra_time = format_time_elapsed(get_text("times/extrafuel_time", "0"))

        try:
            min_fuel = f"{float(alternate_burn) + float(reserve):.1f}"
        except:
            min_fuel = "0.0"
            
        # Time calculations (all in seconds, normalized to 0–86399 range)
        sched_off_sec = int(get_text("times/sched_off", "0")) % 86400
        sched_on_sec = int(get_text("times/sched_on", "0")) % 86400
        sched_in_sec = int(get_text("times/sched_in", "0")) % 86400
        est_out_sec = int(get_text("times/est_out", "0")) % 86400
        est_off_sec = int(get_text("times/est_off", "0")) % 86400
        est_in_sec = int(get_text("times/est_in", "0")) % 86400
        sched_out_sec = int(get_text("times/sched_out", "0")) % 86400
        endurance_sec = int(get_text("times/endurance", "0")) % 86400
        sched_block_sec = int(get_text("times/sched_block", "0")) % 86400
        est_block_sec = int(get_text("times/est_block", "0")) % 86400
        taxi_out_sec = int(get_text("times/taxi_out", "0")) % 86400
        taxi_in_sec = int(get_text("times/taxi_in", "0")) % 86400

        # Get enroute times
        est_enroute_time_sec = int(get_text("times/est_time_enroute", "0")) % 86400
        sched_enroute_time_sec = int(get_text("times/sched_time_enroute", "0")) % 86400

        # Time formatting - ADD ERROR HANDLING
        def safe_format_time_elapsed(seconds):
            """Safely format time elapsed, handling None/invalid values."""
            try:
                if seconds is None or seconds == "":
                    return ""
                return format_time_elapsed(int(seconds))
            except (ValueError, TypeError):
                return ""

        def safe_seconds_to_hhmm(seconds):
            """Safely convert seconds to HHMM format."""
            try:
                if seconds is None or seconds == "":
                    return "--:--"
                return seconds_to_hhmm(int(seconds))
            except (ValueError, TypeError):
                return "--:--"

        # Use safe formatting functions
        sched_out_fmt = safe_format_time_elapsed(sched_out_sec)
        sched_in_fmt = safe_format_time_elapsed(sched_in_sec)
        sched_off_fmt = safe_format_time_elapsed(sched_off_sec)
        sched_on_fmt = safe_format_time_elapsed(sched_on_sec)
        est_in_fmt = safe_format_time_elapsed(est_in_sec)
        est_off_fmt = safe_format_time_elapsed(est_off_sec)
        endurnc = format_time_endurance(endurance_sec) if endurance_sec else "--:--"
        SKD_BLK = safe_seconds_to_hhmm(sched_block_sec)
        EST_BLK = safe_seconds_to_hhmm(est_block_sec)
        taxi_out_fmt = safe_seconds_to_hhmm(taxi_out_sec)
        taxi_in_fmt = safe_seconds_to_hhmm(taxi_in_sec)

        # Get takeoff time - FIX THE MAIN ISSUE HERE
        takeoff_time_raw = get_text("times/takeoff_time", "")

        # CRITICAL FIX: Ensure takeoff_time is always a string or proper format
        if takeoff_time_raw and str(takeoff_time_raw).strip():
            takeoff_time = str(takeoff_time_raw).strip()
        else:
            takeoff_time = est_off_sec  # This should be converted to string format

        # Convert takeoff_time to seconds if it's a string
        if isinstance(takeoff_time, str):
            if len(takeoff_time) == 4 and takeoff_time.isdigit():
                # HHMM format
                hours = int(takeoff_time[:2])
                minutes = int(takeoff_time[2:])
                takeoff_time_sec = (hours * 3600 + minutes * 60) % 86400
            elif takeoff_time.isdigit():
                # Already in seconds as string
                takeoff_time_sec = int(takeoff_time) % 86400
            else:
                takeoff_time_sec = est_off_sec
        else:
            # It's an integer
            takeoff_time_sec = int(takeoff_time) % 86400

        # Fixed time addition function
        def safe_add_time_to_takeoff(takeoff_seconds, enroute_seconds):
            """Safely add enroute time to takeoff time."""
            try:
                # Ensure both are integers
                takeoff_int = int(takeoff_seconds)
                enroute_int = int(enroute_seconds)
                
                total_sec = (takeoff_int + enroute_int) % 86400
                hours = total_sec // 3600
                minutes = (total_sec % 3600) // 60
                return f"{hours:02d}:{minutes:02d}"  # Return with colon for consistency
            except (ValueError, TypeError) as e:
                print(f"ERROR in safe_add_time_to_takeoff: {e}")
                return "____"

        # Calculate planned arrival time
        pln_arr_time = safe_add_time_to_takeoff(takeoff_time_sec, est_enroute_time_sec)

        time_diff = calculate_time_difference(sched_on_sec, est_in_sec)

        # Format additional values - ADD SAFETY CHECKS
        def safe_format_off_time(time_value):
            """Safely format off time."""
            try:
                if time_value is None:
                    return "----"
                return format_off_time(time_value)
            except Exception as e:
                print(f"ERROR in format_off_time: {e}")
                return "----"

        def safe_format_out_time(time_value):
            """Safely format out time."""
            try:
                if time_value is None or time_value == "":
                    return "----"
                return format_out_time(str(time_value))
            except Exception as e:
                print(f"ERROR in format_out_time: {e}")
                return "----"

        act_off_fmt = safe_format_off_time(takeoff_time_sec)

        # Properly format est_out time - FIX POTENTIAL STRING/INT ISSUE
        est_out_time_str = safe_seconds_to_hhmm(est_out_sec)
        if est_out_time_str and est_out_time_str != "--:--":
            est_out_hhmm = est_out_time_str.replace(":", "")
        else:
            est_out_hhmm = "----"

        est_out_fmt = safe_format_out_time(est_out_hhmm)
        enrt_fmt = safe_seconds_to_hhmm(est_enroute_time_sec)
        sched_enrt_fmt = safe_seconds_to_hhmm(sched_enroute_time_sec)

        # Fix the on_time calculations - ENSURE STRINGS
        on_time_skd = str(sched_on_fmt).replace(":", "") if sched_on_fmt and sched_on_fmt != "----" else "----"

        # Fix: Ensure pln_arr_time is properly formatted
        if pln_arr_time and pln_arr_time != "----":
            on_time_pln = str(pln_arr_time).replace(":", "")
        else:
            on_time_pln = "----"

        # Compute estimated arrivals
        skd_total = safe_add_time_to_takeoff(sched_off_sec, est_enroute_time_sec)
        flip_total = safe_add_time_to_takeoff(takeoff_time_sec, est_enroute_time_sec)

        # Build dictionary for later use
        time_data = {
            "sched_off_fmt": sched_off_fmt,
            "act_off_fmt": act_off_fmt,
            "enrt_fmt": enrt_fmt,
            "on_time_skd": on_time_skd,
            "on_time_pln": on_time_pln,
            "skd_total": skd_total,
            "flip_total": flip_total,
            "time_diff": time_diff,
        }

        print(f"DEBUG: takeoff_time_sec = {takeoff_time_sec}")
        print(f"DEBUG: on_time_pln = {on_time_pln}")
        print(f"DEBUG: All time_data values: {time_data}")

        # Additional debugging to catch the exact error location
        for key, value in time_data.items():
            if value is None:
                print(f"WARNING: {key} is None")
            if isinstance(value, int):
                print(f"INFO: {key} is an integer: {value}")
        
        # Additional OFP data from SimBrief (using dictionary structure)
        aircraft_reg = get_text("aircraft/reg", "N/A")
        fin = get_text("aircraft/fin", "N/A")
        aircraft_type = get_text("aircraft/name", "N/A")
        cost_index = get_text("general/costindex", "N/A")
        passengers = get_text("general/passengers", "N/A")
        altn = get_text("api_params/altn", "N/A")
        route = get_text("general/route")
        oew = get_text("weights/oew")
        pax_count = get_text("weights/pax_count")
        bag_count = get_text("weights/bag_count")
        pax_count_actual = get_text("weights/pax_count_actual")
        bag_count_actual = get_text("weights/bag_count_actual")
        pax_weight = get_text("weights/pax_weight")
        bag_weight = get_text("weights/bag_weight")
        freight_added = get_text("weights/freight_added")
        cargo = get_text("weights/cargo")
        payload = get_text("weights/payload")
        est_zfw = get_text("weights/est_zfw")
        max_zfw = get_text("weights/max_zfw")
        est_tow = get_text("weights/est_tow")
        max_tow = get_text("weights/max_tow")
        max_tow_struct = get_text("weights/max_tow_struct")
        tow_limit_code = get_text("weights/tow_limit_code")
        est_ldw = get_text("weights/est_ldw")
        max_ldw = get_text("weights/max_ldw")
        est_ramp = get_text("weights/est_ramp")
        tow_lim = get_text("weights/tow_limit_code")
        
        # Crew data
        cptn = (get_text("crew/cpt", "") or get_text("crew/captain", "") or
                get_text("crew/captain_name", "") or get_text("crew/cptname", "") or "N/A")
        fo = (get_text("crew/fo", "") or get_text("crew/first_officer", "") or
              get_text("crew/foname", "") or "N/A")
        PID = (get_text("crew/pilot_id", "") or get_text("crew/cpt_empno", "") or
               get_text("crew/captain_id", "") or "N/A")
        dispatcher = get_text("crew/dx", "N/A")
        dx_rmks = get_all('general/dx_rmk')

        # ATC
        fpl = get_text("atc/flightplan_text","N/A")
        
        # Route data
        dep_runway = get_text("origin/plan_rwy", "N/A")
        arr_runway = get_text("destination/plan_rwy", "N/A")

        # Get accurate fuel data from XML structure
        taxi_fuel = format_fuel(get_text("fuel/taxi"))
        enroute_burn = format_fuel(get_text("fuel/enroute_burn"))
        contingency_fuel = format_fuel(get_text("fuel/contingency"))
        alternate_burn = format_fuel(get_text("fuel/alternate_burn"))
        alternate_dist = format_fuel(get_text("alternate/distance"))
        alt_alt = format_fuel(get_text("alternate/cruise_altitude"))
        reserve_fuel = format_fuel(get_text("fuel/reserve"))
        etops_fuel = format_fuel(get_text("fuel/etops"))
        extra_fuel = format_fuel(get_text("fuel/extra"))
        min_takeoff = format_fuel(get_text("fuel/min_takeoff"))
        plan_takeoff = format_fuel(get_text("fuel/plan_takeoff"))
        plan_ramp = format_fuel(get_text("fuel/plan_ramp"))
        plan_landing = format_fuel(get_text("fuel/plan_landing"))

        # Initialize with consistent formatting
        # --- compile fuel numbers first ---
        # NOTE: ACF90/ACF99/PBCF are intentionally NOT pre-initialised here.
        # They are only added to fuel_dict when a non-zero value is found,
        # preventing spurious zero-value rows in the ladder.
        fuel_dict = {
            "DISP ADD":   0,
            "DISP EXTRA": 0,
            "MEL":        0,
            "HOLD":       0,
            "TANKERING":  0,
        }

        time_dict = {}  # optional time per bucket

        fuel_extra_section = root.find("fuel_extra")
        if fuel_extra_section is not None:
            for bucket in fuel_extra_section.findall("bucket"):
                label = (bucket.findtext("label") or "").strip().upper()
                fuel_raw = (bucket.findtext("fuel") or "0").strip()
                time_raw = (bucket.findtext("time") or "").strip()

                try:
                    fuel_val = float(fuel_raw)
                except ValueError:
                    fuel_val = 0
                if fuel_val == 0:
                    continue  # skip zero buckets

                time_fmt = format_time_elapsed(int(time_raw)) if time_raw.isdigit() else ""

                # map XML labels to OFP labels
                if label in ("MEL",):
                    fuel_dict["MEL"] += fuel_val
                    if time_fmt:
                        time_dict["MEL"] = time_fmt
                elif label == "EXTRA":
                    fuel_dict["DISP ADD"] += fuel_val
                    if time_fmt:
                        time_dict["DISP ADD"] = time_fmt
                elif label in ("ATC", "WXX"):
                    fuel_dict["HOLD"] += fuel_val
                    if time_fmt:
                        time_dict["HOLD"] = time_fmt
                elif label in ("FOD ADD", "FOB ADD"):
                    fuel_dict["DISP EXTRA"] += fuel_val
                    if time_fmt:
                        time_dict["DISP EXTRA"] = time_fmt
                elif label == "TANKERING":
                    fuel_dict["TANKERING"] += fuel_val
                    if time_fmt:
                        time_dict["TANKERING"] = time_fmt
                elif label in ("ACF90", "ACF 90", "ACF_90"):
                    fuel_dict["ACF90"] = fuel_dict.get("ACF90", 0) + fuel_val
                    if time_fmt:
                        time_dict["ACF90"] = time_fmt
                elif label in ("ACF99", "ACF 99", "ACF_99", "PBCF"):
                    fuel_dict["ACF99"] = fuel_dict.get("ACF99", 0) + fuel_val
                    if time_fmt:
                        time_dict["ACF99"] = time_fmt
                else:
                    # Unknown buckets, including ACF90, stay separate
                    fuel_dict[label] = fuel_val
                    if time_fmt:
                        time_dict[label] = time_fmt


                    
        # Get weight and performance data
        # Get weight and performance data
        try:
            ramp_weight = get_text('weights/est_ramp', '0')
            ramp_weight_formatted = f"{int(ramp_weight):06d}" if ramp_weight else "000000"
        except:
            ramp_weight_formatted = "000000"

        # Calculate required OEI cruise altitude from highest MORA
        try:
            all_fixes = root.findall("navlog/fix")
            max_mora = 0
            for fix in all_fixes:
                mora_text = fix.findtext("mora", "0")
                try:
                    mora_value = float(mora_text)
                    if mora_value > max_mora:
                        max_mora = mora_value
                except (ValueError, TypeError):
                    continue
            
            # Round up to nearest 1000 (no margin added)
            if max_mora > 0:
                oei_cruise_alt = int(math.ceil(max_mora / 1000.0) * 1000)
            else:
                oei_cruise_alt = 10000
            
            # No minimum enforcement - use calculated value directly
                
        except Exception as e:
            print(f"Warning: Could not calculate OEI altitude: {e}")
            oei_cruise_alt = 18000  # Fallback to default

        # Build enhanced HOWGOZIT with OFP header - INITIALIZE AS EMPTY STRING

        # ============================================================================
        # BUILD HOWGOZIT REPORT  (Jeppesen style)
        # ============================================================================

        howgozit = ""

        # Generate header data
        random_plan_number = random.randint(100000, 999999)
        date_formatted = datetime.now().strftime('%d%b%y').upper()

        # Additional variables needed for Jepp-style page 1
        RLS = get_text("general/release")
        pu = get_text("crew/pu", "N/A")
        fa = get_text("crew/fa", "N/A")
        takeoff_alt = get_text("origin/alternate_icao", "NR")
        alt2 = get_text("alternate2/icao_code", "NR")
        altn_icao = get_text("alternate/icao_code", "NONE")

        def parse_time(t):
            """Parse HHMM string → (hours, mins) integers."""
            try:
                t = str(t).strip()
                if len(t) <= 2:
                    return 0, int(t)
                return int(t[:-2]), int(t[-2:])
            except Exception:
                return 0, 0

        # Airline name lookup (ICAO code → full name)
        _AIRLINE_NAMES = {
            # Major US carriers
            "AAL": "AMERICAN AIRLINES",
            "UAL": "UNITED AIRLINES",
            "DAL": "DELTA AIR LINES",
            "SWA": "SOUTHWEST AIRLINES",
            "ASA": "ALASKA AIRLINES",
            "JBU": "JETBLUE AIRWAYS",
            "FFT": "FRONTIER AIRLINES",
            "NKS": "SPIRIT AIRLINES",
            "SCX": "SUN COUNTRY AIRLINES",
            "HAL": "HAWAIIAN AIRLINES",
            # US Regional & Commuter
            "SKW": "SKYWEST AIRLINES",
            "OO":  "SKYWEST AIRLINES",
            "NAC": "NORTHERN AIRWAYS",
            "NEN": "PILGRIM AIRWAYS",
            "RVF": "CARDINAL AIRLINES",
            "RBD": "CARDINAL AIRLINES",
            "RBA": "CARDINAL AIRLINES",
            "SSX": "CASCADES AIRLINES",
            "PNW": "CASCADES AIRLINES",
            "ATX": "LONE STAR AIRWAYS",
            "PFT": "PIONEER AIRLINES",
            "EGF": "EAGLE AIRWAYS",
            "ENY": "ENVOY AIR",
            "PDT": "PIEDMONT AIRLINES",
            "PSA": "PSA AIRLINES",
            "RPA": "REPUBLIC AIRWAYS",
            "FLG": "MESA AIRLINES",
            "YV":  "MESA AIRLINES",
            "CPZ": "COMPASS AIRLINES",
            "LOF": "ENDEAVOR AIR",
            "GJS": "GOJET AIRLINES",
            "SXJ": "SUN AIR EXPRESS",
            "CHQ": "CHAUTAUQUA AIRLINES",
            "TCF": "TRANS STATES AIRLINES",
            "MQ":  "ENVOY AIR",
            "OH":  "PSA AIRLINES",
            "YX":  "REPUBLIC AIRWAYS",
            "QX":  "HORIZON AIR",
            "QXE": "HORIZON AIR",
            "AS":  "ALASKA AIRLINES",
            "ERA": "ERA ALASKA",
            "PAN": "PENINSULA AIRWAYS",
            "PEN": "PENINSULA AIRWAYS",
            # Canada
            "ACA": "AIR CANADA",
            "WJA": "WESTJET",
            "JAZ": "AIR CANADA ROUGE",
        }
        print(f"DEBUG AIRLINE: icao_airline={repr(icao_airline)}, key={repr(str(icao_airline).upper())}")
        _airline_display = _AIRLINE_NAMES.get(str(icao_airline).upper(), "COMPUTER FLIGHT PLAN")
        print(f"DEBUG AIRLINE: resolved={repr(_airline_display)}")

        # (flight plan title and header now on Frontier-style page 2 — see _build_frontier_flight_plan_page)

        # (Route, speed schedules, and fuel table are on the Frontier flight plan page 2)

        # Passenger and cargo data (needed for downstream calculations)
        pax_count_int = int(float(get_text('weights/pax_count', '0')))
        max_pax_int = int(float(get_text('aircraft/max_passengers', '0'))) if get_text('aircraft/max_passengers', '0') != '0' else pax_count_int
        cargo_weight_int = int(float(get_text('weights/cargo', '0')))

        # ============================================================================
        # ON TIME ANALYSIS
        # ============================================================================

        # Function to convert UTC time to local time
        def convert_to_local_time(utc_time_str, timezone_offset):
            """
            Convert UTC time (HHMMZ format) to local time with timezone offset
            timezone_offset: hours difference from UTC (e.g., -4, -5)
            """
            if utc_time_str.endswith('Z'):
                utc_time_str = utc_time_str[:-1]  # Remove Z
            hours = int(utc_time_str[:2])
            minutes = int(utc_time_str[2:])
            local_hours = (hours + timezone_offset) % 24
            if local_hours < 0:
                local_hours += 24
            return f"{local_hours:02d}{minutes:02d}"

        # Get timezone offsets and convert times to local BEFORE using them
        orig_timezone = get_text('times/orig_timezone')  # e.g., -4
        dest_timezone = get_text('times/dest_timezone')  # e.g., -5

        # Convert times to local
        sched_out_local = convert_to_local_time(sched_out_fmt, int(orig_timezone))
        sched_in_local = convert_to_local_time(sched_in_fmt, int(dest_timezone))
        est_out_local = convert_to_local_time(est_out_hhmm, int(orig_timezone))
        est_in_local = convert_to_local_time(est_in_fmt, int(dest_timezone))

        header_line = "       TXO   AIR   TXI  TOTAL    DEP GMT/LCL  ARR GMT/LCL"
        line_skdblk = (
            f"SKDBLK {taxi_out_fmt}  {sched_enrt_fmt}  {taxi_in_fmt}  {SKD_BLK}"
            f"    {sched_out_fmt}Z/{sched_out_local}L  {sched_in_fmt}Z/{sched_in_local}L"
        )
        line_flipln = (
            f"FLIPLN {taxi_out_fmt}  {enrt_fmt}  {taxi_in_fmt}  {EST_BLK}"
            f"    {est_out_hhmm}Z/{est_out_local}L  {est_in_fmt}Z/{est_in_local}L"
        )

        # ============================================================================
        # FRONTIER-STYLE DISPATCH RELEASE COVER PAGE
        # Prepended as page 1; Jeppesen OFP follows after [PAGEBREAK].
        # ============================================================================

        # Pull landing conditions for arrival QNH / OAT
        _ld_node   = root.find('.//tlr/landing')
        _dest_qnh  = "29.92"
        _dest_temp = "15"
        if _ld_node is not None:
            _ld_cond = _ld_node.find('conditions')
            if _ld_cond is not None:
                _dest_qnh  = _ld_cond.findtext('altimeter',   '29.92') or '29.92'
                _dest_temp = _ld_cond.findtext('temperature', '15')    or '15'

        _pmrtw_raw = (get_text("weights/max_tow_struct", None)
                      or get_text("weights/max_tow", "0"))
        _metw_raw  = _pmrtw_raw   # METW proxied to structural MTOW

        _fr_page = _build_frontier_release_page({
            "airline_display":   _airline_display,
            "flight_number":     flight_number,
            "origin":            origin,
            "origin_iata":       origin_iata,
            "destination":       destination,
            "destination_iata":  destination_iata,
            "dep_runway":        dep_runway,
            "arr_runway":        arr_runway,
            "sched_out_local":   sched_out_local,
            "sched_out_fmt":     sched_out_fmt,
            "sched_in_local":    sched_in_local,
            "sched_in_fmt":      sched_in_fmt,
            "plan_ramp":         plan_ramp,
            "min_takeoff":       min_takeoff,
            "enroute_burn":      enroute_burn,
            "taxi_fuel":         taxi_fuel,
            "plan_landing":      plan_landing,
            "aircraft_reg":      aircraft_reg,
            "aircraft_type":     aircraft_type,
            "pax_count_int":     pax_count_int,
            "max_pax_int":       max_pax_int,
            "cargo_weight_int":  cargo_weight_int,
            "max_tow":           get_text("weights/max_tow", "0"),
            "est_tow":           est_tow,
            "est_ldw":           est_ldw,
            "max_ldw":           get_text("weights/max_ldw", "0"),
            "pmrtw_raw":         _pmrtw_raw,
            "metw_raw":          _metw_raw,
            "tow_limit_code":    get_text("weights/tow_limit_code", "LS"),
            "flight_info":       flight_info,
            "cptn":              cptn,
            "fo":                fo,
            "pu":                pu,
            "fa":                fa,
            "dispatcher":        dispatcher,
            "dx_rmks":           dx_rmks,
            "altn_icao":         altn_icao,
            "RLS":               RLS,
            "sched_out_ts":      get_text("times/sched_out", "0"),
            "dest_qnh":          _dest_qnh,
            "dest_temp":         _dest_temp,
        })
        # ── Frontier-style flight plan page (page 2, after dispatch release) ──
        _ffp_page = _build_frontier_flight_plan_page(root, {
            "airline_display":   _airline_display,
            "flight_number":     flight_number,
            "origin":            origin,
            "origin_iata":       origin_iata,
            "destination":       destination,
            "destination_iata":  destination_iata,
            "aircraft_reg":      aircraft_reg,
            "aircraft_type":     aircraft_type,
            "rls":               RLS,
        })

        # Prepend: release page → Frontier flight plan → rest of OFP
        howgozit = (
            "[MARGIN:36]\n"
            + _fr_page
            + "[MARGIN:36]\n[PAGEBREAK]\n"
            + _ffp_page
            + "[MARGIN:75]\n[PAGEBREAK]\n"
            + howgozit
        )

        # Navigation log is on the Frontier flight plan page (page 2)


        # --- Add forecast winds safely ---
        winds_result = write_forecast_winds(
            root,
            orig_metar=orig_metar, orig_taf=orig_taf, orig_atis=orig_atis,
            dest_metar=dest_metar, dest_taf=dest_taf, dest_atis=dest_atis,
            altn_metar=altn_metar, altn_taf=altn_taf, altn_atis=altn_atis,
            toaltn_metar=toaltn_metar, toaltn_taf=toaltn_taf, toaltn_atis=toaltn_atis,
            eualtn_metar=eualtn_metar, eualtn_taf=eualtn_taf, eualtn_atis=eualtn_atis,
            etops_metar=etops_metar, etops_taf=etops_taf, etops_atis=etops_atis,
        )
        winds_text, weather_wx, images_text = winds_result if isinstance(winds_result, tuple) else (winds_result, "", "")
        # Remove any trailing page-break so winds run on continuously without a forced new page
        if winds_text:
            howgozit = howgozit.rstrip()
            if howgozit.endswith("[PAGEBREAK]"):
                howgozit = howgozit[:-len("[PAGEBREAK]")].rstrip() + "\n"
            howgozit += str(winds_text)

        xml_root = root

        # === ETOPS / NAT TRACKS / FIELD REPORTS — before weather ===
        print("DEBUG: About to call write_etops_section")
        etops_output = write_etops_section(root)
        print(f"DEBUG: write_etops_section returned length = {len(etops_output)}")
        if etops_output:
            howgozit += etops_output
            print("DEBUG: ETOPS section added to howgozit")
        else:
            print("DEBUG: No ETOPS section generated")

        nat_output = write_nat_tracks_section(root)
        if nat_output:
            howgozit += nat_output
            print("DEBUG: NAT tracks section added to howgozit")

        field_output = write_field_reports(root)
        if field_output:
            howgozit += field_output

        # --- Weather (METAR/TAF/ATIS/SIGMET) — BEFORE NOTAMs ---
        if weather_wx and weather_wx.strip():
            howgozit += "[NOTAM_START]\n" + weather_wx + "[NOTAM_END]\n"

        # --- NOTAMs ---
        departure_notams   = get_departure_notams_sorted(xml_root, "origin")
        destination_notams = get_arrival_notams_sorted(xml_root, "destination")
        alternate_notams   = get_alternate_notams_sorted(xml_root, "alternate")
        enroute_notams     = get_enroute_notams(xml_root)

        notams_section = ""
        if departure_notams:
            notams_section += departure_notams + "\n"
        if destination_notams:
            notams_section += destination_notams + "\n"
        if alternate_notams:
            notams_section += alternate_notams + "\n"
        if enroute_notams:
            notams_section += enroute_notams + "\n"

        if notams_section.strip():
            howgozit += "[NOTAM_START]\n" + notams_section + "[NOTAM_END]\n"

        # --- Weather charts (images) ---
        if images_text:
            howgozit += images_text




        # --- Add takeoff performance section safely ---
# --- Add takeoff performance section safely ---
        valid_runways, flight_info, anti_ice_on, runway_lines = extract_runway_data(root)

        # Get airport altitudes
        origin_icao = flight_info.get('origin', '')
        alt_node = root.find('.//departure_airport/elevation') or root.find('.//departure_airport/altitude')
        max_elevation = float(alt_node.text) if alt_node is not None and alt_node.text else 0
        airport_altitudes = get_airport_specific_altitudes(origin_icao, max_elevation)

        # --- EXTRACT ICAO CODE FOR TAKEOFF PERFORMANCE (speeds) ---
        icao_code_for_speeds = get_text("aircraft/icaocode", "XXXX")
        print(f"DEBUG: ICAO code for speeds: '{icao_code_for_speeds}'")

        # TPS + TLR now produced by a single unified call
        howgozit += write_tlr_section(root)
        pax_weight = float(pax_weight)

        # --- Cabin config lookup table ---
        CABIN_CONFIGS = {
            # A319 family
            "A319-111": {"F": 8, "C": 0, "W": 0, "Y": 130},
            "A319-132": {"F": 8, "C": 0, "W": 0, "Y": 130},
            "A319-115": {"F": 8, "C": 0, "W": 0, "Y": 124},
            "A319-115WL": {"F": 8, "C": 0, "W": 0, "Y": 124},

            # A320 family
            "A320": {"F": 8, "C": 0, "W": 0, "Y": 162},
            "A320-232": {"F": 8, "C": 0, "W": 0, "Y": 162},
            "A320-271N": {"F": 8, "C": 0, "W": 0, "Y": 162},
            "A320-251": {"F": 0, "C": 0, "W": 0, "Y": 188},

            # A321 family
            "A321T": {"F": 16, "C": 0, "W": 0, "Y": 143},
            "A321-231": {"F": 12, "C": 0, "W": 0, "Y": 182},
            "A321-211": {"F": 12, "C": 0, "W": 0, "Y": 184},
            "A321-253N":  {"F": 20, "C": 0, "W": 0, "Y": 176},
            "A321-253NY": {"F": 20, "C": 0, "W": 0, "Y": 176},
            "A321-272NX": {"F": 0,  "C": 0, "W": 0, "Y": 240},
            "A321-253NX": {"F": 20, "C": 0, "W": 0, "Y": 176},
            "A321-271NY": {"F": 12, "C": 12, "W": 0, "Y": 144},
            "A330-941": {"F": 28, "C": 12, "W": 0, "Y": 269},

            # B737 family
            "B737": {"F": 12, "C": 0, "W": 0, "Y": 114},
            "B738": {"F": 16, "C": 0, "W": 0, "Y": 156},
            "B738F": {"F": 0, "C": 0, "W": 0, "Y": 0},
            "B737 MAX 8": {"F": 16, "C": 0, "W": 0, "Y": 156},

            # B757/767
            "B757-2B7": {"F": 16, "C": 0, "W": 0, "Y": 154},
            "B763": {"F": 16, "C": 32, "W": 0, "Y": 200},

            # E-Jets
            "EMB 170-100": {"F": 6, "C": 0, "W": 0, "Y": 64},
            "E170": {"F": 6, "C": 0, "W": 0, "Y": 64},
            "E175": {"F": 12, "C": 0, "W": 0, "Y": 64},
            "E75L": {"F": 8, "C": 0, "W": 0, "Y": 72},
            "EMB-175STD": {"F": 8, "C": 0, "W": 0, "Y": 72},
            "E190": {"F": 0, "C": 0, "W": 0, "Y": 100},
            "E195": {"F": 0, "C": 0, "W": 0, "Y": 120},
            "MD80": {"F": 16, "C": 0, "W": 0, "Y": 124},
            "MD-83": {"F": 16, "C": 0, "W": 0, "Y": 124},
            "DH8D": {"F": 0, "C": 0, "W": 0, "Y": 76},
            "DHC8-402": {"F": 0, "C": 0, "W": 0, "Y": 76},
        }

        # --- Extract passenger data from XML ---
        raw_pax_count = get_text("weights/pax_count")
        raw_pax_weight = get_text("weights/pax_weight")

        # Debug: Print what we're getting from XML
        print(f"DEBUG: Raw pax_count from XML: '{raw_pax_count}'")
        print(f"DEBUG: Raw pax_weight from XML: '{raw_pax_weight}'")

        # Try alternative XML paths if the standard ones are empty
        if not raw_pax_count or raw_pax_count.strip() == "" or raw_pax_count == "0":
            # Try alternative paths
            alternative_paths = [
                "passenger_count",
                "passengers",
                "pax",
                "total_passengers",
                "manifest/passengers",
                "load/passengers"
            ]
            for path in alternative_paths:
                test_value = get_text(path)
                print(f"DEBUG: Testing path '{path}': '{test_value}'")
                if test_value and test_value.strip() != "" and test_value != "0":
                    raw_pax_count = test_value
                    print(f"DEBUG: Using passenger count from '{path}': {raw_pax_count}")
                    break

        # --- Aircraft config ---
        import re

        # --- Parse acdata_parsed early — used for cabin config fallback and cargo ---
        acdata = {}
        try:
            _acdata_tag = xml_root.find('.//api_params/acdata_parsed')
            if _acdata_tag is not None and _acdata_tag.text and _acdata_tag.text.strip():
                acdata = json.loads(_acdata_tag.text.strip())
                print(f"DEBUG: Loaded acdata for {acdata.get('reg', 'UNKNOWN')} ({acdata.get('icao', '?')})")
            else:
                print("WARNING: acdata_parsed tag is missing or empty.")
        except Exception as e:
            print(f"WARNING: Failed to parse acdata_parsed: {e}")
            acdata = {}

        # --- Normalize aircraft string ---
        def normalize_aircraft(ac_type):
            if not ac_type:
                return "UNKNOWN"
            
            # Convert to uppercase for matching
            ac_type_upper = ac_type.upper().strip()
            
            # SPECIFIC MAPPINGS - Check these FIRST before generic normalization
            specific_mappings = {
                "B737-800": "B738",
                "737-800": "B738",
                "B737-700": "B737",
                "737-700": "B737",
                "B737-900": "B739",
                "737-900": "B739",
                "B737-8": "B38M",  # 737 MAX 8
                "737-8": "B38M",
                "737-8 MAX": "B38M",
                "B737-9": "B39M",  # 737 MAX 9
                "737-9": "B39M",
                "A320NEO": "A20N",
                "A321NEO": "A21N",
                "A321-200": "A321",
            }
            
            # Check for exact matches in specific mappings
            for key, value in specific_mappings.items():
                if key in ac_type_upper:
                    print(f"DEBUG: Specific mapping match: '{ac_type}' -> '{value}'")
                    return value
            
            # Remove parentheses and content inside
            ac_type = re.sub(r"\(.*?\)", "", ac_type_upper)
            # Remove trailing non-alphanumeric suffixes like WL, NX, NY
            ac_type = re.sub(r"[^A-Z0-9-]+$", "", ac_type.strip())
            return ac_type

        # --- Get aircraft NAME for cabin config (NOT icaocode) ---
        aircraft_name_from_xml = get_text("aircraft/name", "")
        print(f"DEBUG: Aircraft name from XML aircraft/name: '{aircraft_name_from_xml}'")

        # Fall back to flight_info aircraft if name not found = get_text("aircraft/icaocode", "XXXX")
        aircraft_type_raw = aircraft_name_from_xml if aircraft_name_from_xml else get_text("aircraft/icaocode", "XXXX")
        print(f"DEBUG: Raw aircraft type before normalization: '{aircraft_type_raw}'")
        
        aircraft_type = normalize_aircraft(aircraft_type_raw)
        print(f"DEBUG: Normalized aircraft type for cabin config: '{aircraft_type}'")

        # --- Lookup config ---
        config = CABIN_CONFIGS.get(aircraft_type)
        if not config:
            print(f"DEBUG: No direct match for '{aircraft_type}', trying ICAO fallback...")
            # Try ICAO code lookup as fallback
            ICAO_FALLBACK = {
                "A319": {"F": 8, "C": 0, "W": 0, "Y": 124},
                "A320": {"F": 8, "C": 0, "W": 0, "Y": 162},
                "A321": {"F": 12, "C": 0, "W": 0, "Y": 182},
                "A21N": {"F": 8, "C": 0, "W": 0, "Y": 212},
                "A339": {"F": 28, "C": 12, "W": 0, "Y": 269},
                "B737": {"F": 12, "C": 0, "W": 0, "Y": 114},
                "B738": {"F": 16, "C": 0, "W": 0, "Y": 156},
                "B38M": {"F": 16, "C": 0, "W": 0, "Y": 156},
                "B752": {"F": 14, "C": 28, "W": 0, "Y": 150},
                "B763": {"F": 16, "C": 32, "W": 0, "Y": 200},
                "E170": {"F": 6, "C": 0, "W": 0, "Y": 64},
                "E175": {"F": 12, "C": 0, "W": 0, "Y": 64},
                "E75L": {"F": 8, "C": 0, "W": 0, "Y": 72},
                "E75S": {"F": 8, "C": 0, "W": 0, "Y": 72},
                "E190": {"F": 0, "C": 0, "W": 0, "Y": 100},
                "E195": {"F": 0, "C": 0, "W": 0, "Y": 120},
                "MD80": {"F": 16, "C": 0, "W": 0, "Y": 124},
                "MD83": {"F": 16, "C": 0, "W": 0, "Y": 124},
                "MD88": {"F": 16, "C": 0, "W": 0, "Y": 124},
                "DH8D": {"F": 0, "C": 0, "W": 0, "Y": 76},
            }
            
            for icao_code, icao_config in ICAO_FALLBACK.items():
                if icao_code in aircraft_type:
                    config = icao_config
                    print(f"DEBUG: ICAO fallback match '{icao_code}': {config}")
                    break
            
            # Final fallback — use acdata_parsed maxpax if available, else 300
            if not config:
                _maxpax = int(acdata.get('maxpax', 0) or 0)
                _total_y = _maxpax if _maxpax > 0 else 300
                config = {"F": 0, "C": 0, "W": 0, "Y": _total_y}
                print(f"DEBUG: acdata_parsed maxpax fallback → Y={_total_y} for '{aircraft_type_raw}'")
                # Also use acdata paxwgt as default pax weight if not already set by XML
                if not (raw_pax_weight and raw_pax_weight.strip()):
                    try:
                        _paxwgt = float(acdata.get('paxwgt', 0) or 0)
                        if _paxwgt > 0:
                            raw_pax_weight = str(_paxwgt)
                            print(f"DEBUG: Using acdata_parsed paxwgt={_paxwgt} as default pax weight")
                    except Exception:
                        pass
        else:
            print(f"DEBUG: Found config for '{aircraft_type}': {config}")

        # Ensure numbers are integers with fallback values
        try:
            pax_count = int(raw_pax_count) if raw_pax_count and raw_pax_count.strip() else 0
        except ValueError:
            print(f"WARNING: Could not convert pax_count '{raw_pax_count}' to integer, using 0")
            pax_count = 0

        try:
            pax_weight = float(raw_pax_weight) if raw_pax_weight and raw_pax_weight.strip() else 84.0
        except ValueError:
            print(f"WARNING: Could not convert pax_weight '{raw_pax_weight}' to float, using default 84.0")
            pax_weight = 84.0

        print(f"DEBUG: Final pax_count: {pax_count}, pax_weight: {pax_weight}")
        config = {k: int(v) for k,v in config.items()}

        # --- Distribute passengers class by class (F->C->W->Y) ---
        distributed_pax = {"F":0,"C":0,"W":0,"Y":0}
        remaining_pax = pax_count

        for cls in ["F","C","W","Y"]:
            seats = config.get(cls, 0)
            if remaining_pax <= 0:
                break
            assigned = min(seats, remaining_pax)
            distributed_pax[cls] = assigned
            remaining_pax -= assigned

        # --- Total passenger weight based on distributed pax ---
        total_pax_weight = sum(distributed_pax[cls] * pax_weight for cls in ["F","C","W","Y"])
        # ---- Add WEIGHT AND BALANCE section ----
        max_seats = sum(config.values())
        total_assigned = sum(distributed_pax.values())

        # --- Get total cargo from XML ---
        try:
            cargo = int(get_text("weights/cargo", "0") or "0")
        except (ValueError, TypeError):
            cargo = 0

        # acdata already parsed above — used here for max cargo
        def calculate_cargo_distribution(total_cargo):
            """Split cargo into forward and aft compartments, both rounded to nearest 100 (up from 30)."""
            
            # Round to nearest 100, rounding up from 30
            def round_to_100(val):
                remainder = val % 100
                if remainder >= 30:
                    return (val // 100 + 1) * 100
                else:
                    return (val // 100) * 100
            
            # Calculate the split
            base_split = total_cargo // 2
            adjustment = random.choice([-200, 0, 200])
            fwd_cargo = max(0, min(base_split + adjustment, total_cargo))
            aft_cargo = total_cargo - fwd_cargo
            
            # Round BOTH compartments
            fwd_cargo = round_to_100(fwd_cargo)
            aft_cargo = round_to_100(aft_cargo)
            
            return fwd_cargo, aft_cargo

        def _wb_int(val, default="0"):
            """Safely convert a W&B field to int string."""
            try:
                return str(int(float(val))) if val not in (None, '', 'N/A') else default
            except (ValueError, TypeError):
                return default

        def _wb_float(val, default="0.0"):
            """Safely convert a W&B field to float string."""
            try:
                return f"{float(val):.0f}" if val not in (None, '', 'N/A') else default
            except (ValueError, TypeError):
                return default

        try:
            max_cargo     = float(acdata.get("maxcargo", 0) or 0)
            max_cargo_lbs = int(max_cargo * 1000)
        except (ValueError, TypeError):
            max_cargo_lbs = 0
        fwd_max, aft_max       = calculate_cargo_distribution(max_cargo_lbs)
        actual_cargo           = cargo
        fwd_actual, aft_actual = calculate_cargo_distribution(actual_cargo)
        cargo_rounded          = fwd_actual + aft_actual

        oew_s            = _wb_int(oew)
        est_zfw_s        = _wb_int(est_zfw)
        max_zfw_s        = _wb_int(max_zfw)
        est_tow_s        = _wb_int(est_tow)
        max_tow_struct_s = _wb_int(max_tow_struct)
        try:
            ramp_weight_formatted_s = ramp_weight_formatted
        except NameError:
            ramp_weight_formatted_s = _wb_int(est_ramp)
        try:
            plan_ramp_s = plan_ramp
        except NameError:
            plan_ramp_s = "0"
        try:
            taxi_fuel_s = taxi_fuel
        except NameError:
            taxi_fuel_s = "0"
        try:
            total_pax_wt_s = str(int(total_pax_weight))
        except (ValueError, TypeError):
            total_pax_wt_s = "0"

        howgozit += (
            "[PAGEBREAK]\n"
            + "\n" * 4
            + "******************** WEIGHT AND BALANCE DATA ******************\n\n"
            "------LOAD----------TOTALS-------LIMITS-----CMPT MAX--AS LDED--\n"
            f"EOW     {oew_s}  ZFW  {est_zfw_s}  MZFW {max_zfw_s}       F1 {fwd_max:<5}    {fwd_actual:<5}\n"
            f"PSGR WT {total_pax_wt_s}   FUEL {plan_ramp_s + 'P'}  *** STD ***       A1 {aft_max:<5}    {aft_actual:<5}\n"
            f"CGO WT  {cargo_rounded}    RMP  {ramp_weight_formatted_s}  MRMP {max_tow_struct_s}\n"
            f"BALLAST 0       TXI  {taxi_fuel_s}\n"
            f"                {'TOW':<5}{est_tow_s:<5}  MTOW  {max_tow_struct_s:<9}\n"
            f"                        {'CNFIG':<8}F {config.get('F',0):<3} C {config.get('C',0):<3} W {config.get('W',0):<3} Y {config.get('Y',0):<3} CAP {max_seats:<3}\n"
            f"                        {'PSGRS':<8}F {distributed_pax.get('F',0):<3} C {distributed_pax.get('C',0):<3} W {distributed_pax.get('W',0):<3} Y {distributed_pax.get('Y',0):<3} TOT {total_assigned:<3}\n"
            f"CRT ADDRESS L036   AGENT {random_name():<20}PHONE 614-454-5685\n"
            + "*" * 63 + "\n\n"
        )

        # --- Normalize line endings ---
        howgozit = howgozit.replace('\r\n', '\n').replace('\r', '\n')
        
        # --- Return the result ---
        return howgozit, flight_info, valid_runways, anti_ice_on, runway_lines
        
    except Exception as e:
        print(f"ERROR building HOWGOZIT: {e}")
        print(f"ERROR type: {type(e)}")
        import traceback
        traceback.print_exc()
        
        try:
            print(f"DEBUG: icao_airline = {icao_airline}")
            print(f"DEBUG: flight_number = {flight_number}")  
            print(f"DEBUG: origin = {origin}")
            print(f"DEBUG: destination = {destination}")
        except:
            print("DEBUG: Could not access basic flight info variables")
        
        # Return a safe tuple with error message
        error_message = f"Error generating flight plan: {e}"
        return error_message, {}, [], False, []


def _load_ourairports_freqs():
    """
    Download (and disk-cache for 7 days) the OurAirports airport-frequencies CSV.

    Returns a dict:  { 'KJFK': [{'type':'ATIS','freq':'128.72','desc':'...'}, ...], ... }

    Source (CC0 public domain):
      https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv
    Columns: id, airport_ident, type, description, frequency_mhz

    Cache file written next to config.json; refreshed when older than 7 days.
    Falls back gracefully to {} on any network or parse error.
    """
    import csv, io, os, time

    cfg_dir    = os.path.dirname(os.path.abspath(CONFIG_FILE))
    CACHE_PATH = os.path.join(cfg_dir, 'ourairports_freqs.csv')
    URL        = 'https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv'
    MAX_AGE    = 7 * 24 * 3600   # 7 days in seconds

    raw = None

    # ── Use cache if fresh ────────────────────────────────────────────────────
    if os.path.exists(CACHE_PATH):
        try:
            if time.time() - os.path.getmtime(CACHE_PATH) < MAX_AGE:
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    raw = f.read()
        except Exception:
            raw = None

    # ── Download when cache absent or stale ───────────────────────────────────
    if raw is None:
        try:
            import urllib.request
            with urllib.request.urlopen(URL, timeout=12) as resp:
                raw = resp.read().decode('utf-8')
            try:                                      # write cache (best-effort)
                with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                    f.write(raw)
            except Exception:
                pass
        except Exception as e:
            print(f"[OurAirports] Frequency download failed: {e}")
            return {}

    # ── Parse CSV → {ICAO: [freq_dict, ...]} ─────────────────────────────────
    try:
        db = {}
        for row in csv.DictReader(io.StringIO(raw)):
            ident = (row.get('airport_ident') or '').strip().upper()
            if not ident:
                continue
            db.setdefault(ident, []).append({
                'type': (row.get('type')          or '').strip().upper(),
                'freq': (row.get('frequency_mhz') or '').strip(),
                'desc': (row.get('description')   or '').strip(),
            })
        return db
    except Exception as e:
        print(f"[OurAirports] Parse error: {e}")
        return {}


# Module-level cache — CSV parsed once per Python session
_OURAIRPORTS_FREQ_DB = None


def _get_airport_atc_freqs(icao):
    """
    Return list of freq dicts for icao from OurAirports.
    Lazily loads the DB on first call and caches it for the session.
    Tries full ICAO (KJFK) then K-stripped (JFK) for non-prefixed entries.
    """
    global _OURAIRPORTS_FREQ_DB
    if _OURAIRPORTS_FREQ_DB is None:
        _OURAIRPORTS_FREQ_DB = _load_ourairports_freqs()
    icao = (icao or '').strip().upper()
    return (_OURAIRPORTS_FREQ_DB.get(icao) or
            _OURAIRPORTS_FREQ_DB.get(icao.lstrip('K')) or [])


def _pick_atc_freq(freq_list, *type_codes):
    """
    Return frequency_mhz for the first matching type code from freq_list, else ''.
    Multiple codes tried in priority order, e.g.:
      _pick_atc_freq(freqs, 'ATIS', 'ASOS', 'AWOS')
    """
    for code in type_codes:
        for entry in freq_list:
            if entry['type'] == code:
                return entry['freq']
    return ''


def write_field_reports(root):
    """
    Build field report pages for origin and destination airports.

    Data sources:
      1. TLR (tlr/takeoff, tlr/landing)  — surface condition, runways, wind/OAT/QNH
      2. NOTAM scan (Q-code primary)     — runway closures (MR), taxiway closures (MX),
                                           approach-aid outages (IC/IL/IG/LE/LF/LP)
      3. origin/destination XML          — ICAO, IATA, plan_rwy, METAR
      4. config.json station_ops[icao]   — comms freqs/phones, special advisories, remarks

    Layout matches real AA OFP field report exactly:
      * XXXX FIELD REPORT *
      SEP
      DATE DDMMMxYY  TIME HHMM LOCAL
      SEP
      EXISTING TAA SEE NOTAMS
      SEP
      RWY (col 0)  STATUS (15)  CONDITIONS (26)  REMARKS (37)
        — planned rwy annotated with -TAKEOFF RWY xx- / -LANDING RWY xx-
        — closed rwy annotated with  DDMMMYYhhmm DDMMMYYhhmm  [NID]
      RAMP/TXWY  SURFACE  {cond}
      SEP
      [TAXIWAY ADVISORIES — MX Q-code closures]
      [APPROACH AID ADVISORIES — IC/IL/IG/LE/LF/LP Q-code NOTAMs on planned rwy]
      SPECIAL ADVISORIES  (config station_ops text, or blank section)
      SEP
      OPS RADIO FREQ   {freq}    OPS PHONE  {phone}
      ATIS FREQ        {freq}    ATIS PHONE {phone}
      MAINT FREQ       {freq}   MAINT PHONE {phone}
      PHONE PATCH FREQ {freq}   PATCH PHONE {phone}
      ACARS FREQ       {freq}   GROUND PWR {gpu}  GROUND AIR {gnd}
      SEP
      REMARKS
      {free-text remarks lines}
      UPDATED  HHMM/DDMMM  INITIALS
    """
    import re as _re
    import textwrap as _tw

    SEP = "--------------------------------------------------------------"

    # ── Regex patterns ─────────────────────────────────────────────────────────
    _RWY_PAT  = _re.compile(
        r'\b(?:R/?W(?:Y)?S?\.?\s*)(\d{1,2}[LRC]?)(?:[/\\-](\d{1,2}[LRC]?))?',
        _re.IGNORECASE)
    _CLSD_PAT = _re.compile(r'\bCLS[DE]D?\b', _re.IGNORECASE)
    _EFF_PAT  = _re.compile(
        r'(\d{2}[A-Z]{3}\d{2,4}(?:/\d{4})?)\s*(?:TO|[-\u2013])\s*'
        r'(\d{2}[A-Z]{3}\d{2,4}(?:/\d{4})?)',
        _re.IGNORECASE)

    # ── Q-code classification sets ─────────────────────────────────────────────
    _RUNWAY_QCODES     = frozenset(['MR'])
    _TAXIWAY_QCODES    = frozenset(['MX', 'MY'])   # taxiway + rapid exit
    _NON_RUNWAY_QCODES = frozenset([
        'MA','MB','MC','MD','MG','MH','MK','MM','MN',
        'MO','MP','MS','MT','MU','MW','MX','MY',
        'FA','FB','FC','FD','FE','FF','FG','FH',
        'FI','FJ','FL','FM','FO','FP','FS','FT','FU','FW','FZ',
    ])
    # Approach aid Q-codes: ILS/LOC/GP/MLS + approach lights/PAPI/VASIS
    _APPROACH_QCODES   = frozenset([
        'IC','ID','IG','II','IL','IM','IN','IO','IS','IT','IU','IW','IX','IY',  # ILS/MLS
        'LC','LE','LF','LG','LP','LR','LS',                                      # approach lights, PAPI, VASIS
    ])

    # ── Q-code extractor ───────────────────────────────────────────────────────
    def _qcode_subject(notam_node):
        raw = (notam_node.findtext('notam_qcode') or '').strip().upper()
        if raw:
            if raw.startswith('Q') and len(raw) >= 3: return raw[1:3]
            if len(raw) >= 2: return raw[:2]
        text = (notam_node.findtext('notam_text') or '').strip().upper()
        m = _re.search(r'Q\)\s*\w+/Q([A-Z]{2})', text)
        if m: return m.group(1).upper()
        subj = (notam_node.findtext('notam_qcode_subject') or '').strip().upper()
        _MAP = {'RUNWAY':'MR','RWY':'MR','TAXIWAY':'MX','TWY':'MX',
                'MOVEMENT AREA':'MM','APRON':'MA','RAPID EXIT':'MY'}
        for key, code in _MAP.items():
            if key in subj: return code
        return ''

    # ── ISO/NOTAM date → "DDMMMYYhhmm" (no Z, matches real report format) ─────
    def _fmt_zulu(s):
        s = (s or '').strip()
        m = _re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', s)
        if m:
            yr, mo, dy, hh, mm = m.groups()
            months = ['JAN','FEB','MAR','APR','MAY','JUN',
                      'JUL','AUG','SEP','OCT','NOV','DEC']
            return f"{dy}{months[int(mo)-1]}{yr[2:]}/{hh}{mm}"
        m2 = _re.match(r'(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', s)
        if m2:
            yy, mo, dy, hh, mm = m2.groups()
            months = ['JAN','FEB','MAR','APR','MAY','JUN',
                      'JUL','AUG','SEP','OCT','NOV','DEC']
            return f"{dy}{months[int(mo)-1]}{yy}/{hh}{mm}"
        return s

    # ── Runway closure extractor (Q-code primary) ──────────────────────────────
    _RWY_CLSD_DIRECT = _re.compile(
        r'\bRW?Y\s+(\d{1,2}[LRC]?)(?:[/\\-](\d{1,2}[LRC]?))?\s+CLSD\b',
        _re.IGNORECASE)

    def _extract_closures(notams_list):
        """Return {pair: {'nid', 'eff_start', 'eff_end', 'text'}}"""
        closed = {}
        for n in notams_list:
            text = (n.findtext('notam_text') or '').strip().upper()
            if not text: continue
            qcode = _qcode_subject(n)
            if qcode in _RUNWAY_QCODES:
                if not _CLSD_PAT.search(text): continue
                rwy_matches = list(_RWY_PAT.finditer(text))
                if not rwy_matches: continue
            elif qcode in _NON_RUNWAY_QCODES:
                continue
            else:
                if not _CLSD_PAT.search(text): continue
                rwy_matches = list(_RWY_CLSD_DIRECT.finditer(text))
                if not rwy_matches: continue

            nid      = (n.findtext('notam_id') or '').strip()
            date_eff = (n.findtext('date_effective') or n.findtext('notam_effective_dtg') or '').strip()
            date_exp = (n.findtext('date_expire')    or n.findtext('notam_expire_dtg')    or '').strip()

            if date_eff and date_exp:
                eff_s, eff_e = _fmt_zulu(date_eff), _fmt_zulu(date_exp)
            else:
                em = _EFF_PAT.search(text)
                eff_s = em.group(1) if em else ''
                eff_e = em.group(2) if em else ''

            for m in rwy_matches:
                end1 = m.group(1).upper()
                end2 = (m.group(2) or '').upper()
                pair = _make_runway_pair(end1) or (f"{end1}/{end2}" if end2 else end1)
                closed[pair] = {'nid': nid, 'eff_start': eff_s, 'eff_end': eff_e, 'text': text}
        return closed

    # ── Taxiway closure extractor (MX/MY Q-code) ──────────────────────────────
    def _extract_twy_closures(notams_list):
        """
        Return list of {'nid', 'twy'} for closed taxiways.
        Output is intentionally terse — just TWY ID + NOTAM ID, matching the
        real report style where NOTAMs are referenced by ID only, not text dump.
        Only TWY IDs that appear BEFORE the CLSD token are captured, so
        location references like "BTN TWY A" after CLSD are excluded.
        """
        _TWY_ID_PAT = _re.compile(
            r'\bTWY\s+([A-Z]{1,3}\d*)', _re.IGNORECASE)
        seen = set()
        closures = []
        for n in notams_list:
            text = (n.findtext('notam_text') or '').strip().upper()
            if not text: continue
            qcode = _qcode_subject(n)
            if qcode not in _TAXIWAY_QCODES: continue
            if not _CLSD_PAT.search(text): continue
            nid = (n.findtext('notam_id') or '').strip()
            if nid in seen: continue
            seen.add(nid)
            # Only capture IDs before the first CLSD token
            clsd_m = _CLSD_PAT.search(text)
            pre = text[:clsd_m.start()] if clsd_m else text
            twy_ids = list(dict.fromkeys(
                m.group(1) for m in _TWY_ID_PAT.finditer(pre)))
            twy_str = ' '.join(twy_ids) if twy_ids else '?'
            closures.append({'nid': nid, 'twy': twy_str})
        return closures

    # ── Approach/lighting NOTAM extractor ─────────────────────────────────────
    def _extract_approach_notams(notams_list, planned_rwy):
        """
        Return list of {'nid'} for ILS/LOC/lighting NOTAMs that affect the
        planned runway.  Only the NOTAM ID is returned — matching the real
        report convention of listing bare IDs (e.g. 'KZAB A0181/19') rather
        than dumping text.  Filtered to planned runway heading when known;
        if planned_rwy is blank, returns all approach Q-code NOTAMs.
        """
        # Extract the numeric heading to filter on (e.g. '22' from '22L')
        planned_num = _re.search(r'(\d{1,2})', planned_rwy or '')
        planned_hdg = planned_num.group(1).lstrip('0') if planned_num else ''
        # Also accept the reciprocal to catch LOC/GP annotations on both ends
        if planned_hdg:
            try:
                recip_hdg = str((int(planned_hdg) + 18) % 36 or 36)
            except ValueError:
                recip_hdg = ''
        else:
            recip_hdg = ''

        seen = set()
        adv  = []
        for n in notams_list:
            text = (n.findtext('notam_text') or '').strip().upper()
            if not text: continue
            qcode = _qcode_subject(n)
            if qcode not in _APPROACH_QCODES: continue
            # If we know the planned runway, require its heading to appear in text
            if planned_hdg:
                # Use (?<!\d) / (?!\d) instead of \b so '22L' still yields '22'
                nums = set(_re.findall(r'(?<!\d)(\d{1,2})(?!\d)', text))
                if planned_hdg not in nums and recip_hdg not in nums:
                    continue
            nid = (n.findtext('notam_id') or '').strip()
            if not nid or nid in seen: continue
            seen.add(nid)
            adv.append({'nid': nid})
        return adv

    # ── METAR quick-parse for wind/alt/temp ────────────────────────────────────
    def _parse_metar_brief(metar):
        """Return dict with wind_dir, wind_spd, wind_gst, altimeter, temp."""
        r = {}
        if not metar: return r
        m = _re.search(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT', metar)
        if m:
            r['wind_dir'] = m.group(1)
            r['wind_spd'] = int(m.group(2))
            if m.group(3): r['wind_gst'] = int(m.group(3))
        m = _re.search(r'\bA(\d{4})\b', metar)
        if m: r['altimeter'] = f"{m.group(1)[:2]}.{m.group(1)[2:]}"
        m = _re.search(r'\b(M?\d{2})/(M?\d{2})\b', metar)
        if m: r['temp'] = m.group(1).replace('M', '-')
        return r

    def _build_airport_report(icao, iata, tlr_section, notams_list,
                              local_date, local_time,
                              plan_rwy, metar, role, station_ops, flt_num=''):
        """
        Build one airport block matching the real AA OFP field report format.

        Parameters
        ----------
        icao        : str   ICAO code
        iata        : str   IATA code (used as display label when available)
        tlr_section : Element or None  — tlr/takeoff or tlr/landing node
        notams_list : list  — notam Elements for this airport
        local_date  : str   — local date string e.g. '18DEC19'
        local_time  : str   — local time HHMM e.g. '0844'
        plan_rwy    : str   — planned runway from XML (origin/plan_rwy or dest/plan_rwy)
        metar       : str   — raw METAR string for wind/QNH/temp line
        role        : str   — 'DEP' or 'ARR'
        station_ops : dict  — loaded from config.json station_ops[icao], or {}
        flt_num     : str   — ICAO airline + flight number for header line
        """
        if not icao:
            return ""
        display = iata if iata else icao

        # ── Surface condition from TLR ─────────────────────────────────────────
        cond_node = tlr_section.find('conditions') if tlr_section is not None else None
        _SURF = {'DRY':'DRY','WET':'WET','SNOW':'SNOW/ICE',
                 'ICE':'SNOW/ICE','SLUSH':'WET','CONTAMINATED':'WET'}
        raw_surf = (cond_node.findtext('surface_condition') or 'DRY').upper() \
                   if cond_node is not None else 'DRY'
        surface = _SURF.get(raw_surf, raw_surf)

        # ── Wind / QNH / OAT — TLR conditions preferred, METAR fallback ───────
        wx = {}
        if cond_node is not None:
            wd = (cond_node.findtext('wind_direction') or '').strip()
            ws = (cond_node.findtext('wind_speed')     or '').strip()
            if wd and ws:
                wx['wind_dir'] = wd.zfill(3)
                wx['wind_spd'] = int(float(ws))
            qnh = (cond_node.findtext('altimeter') or '').strip()
            if qnh:
                try:
                    wx['altimeter'] = f"{float(qnh):.2f}"
                except ValueError:
                    wx['altimeter'] = qnh
            tmp = (cond_node.findtext('temperature') or '').strip()
            if tmp: wx['temp'] = tmp
        if not wx and metar:
            wx = _parse_metar_brief(metar)

        # ── All runways from TLR ───────────────────────────────────────────────
        # Canonicalize each pair so the lower-numbered heading always comes first
        # (e.g. both '22L' and '4R' → '04R/22L', never the reverse).
        def _canon_pair(pair_str):
            parts = pair_str.split('/')
            if len(parts) != 2: return pair_str
            import re as _re2
            def _num(p): m = _re2.search(r'(\d+)', p); return int(m.group(1)) if m else 99
            return pair_str if _num(parts[0]) <= _num(parts[1]) else f"{parts[1]}/{parts[0]}"

        seen_pairs = {}
        if tlr_section is not None:
            for rwy in tlr_section.findall('runway'):
                rid = (rwy.findtext('identifier') or '').strip().upper()
                if not rid: continue
                pair = _canon_pair(_make_runway_pair(rid) or rid)
                if pair not in seen_pairs:
                    seen_pairs[pair] = surface

        # ── Overlay runway closures from NOTAMs ───────────────────────────────
        closed_map = {_canon_pair(k): v for k, v in _extract_closures(notams_list).items()}
        all_pairs  = dict(seen_pairs)
        for pair in closed_map:
            if pair not in all_pairs:
                all_pairs[pair] = surface

        if not all_pairs:
            return ""

        def _sort_key(p):
            m = _re.search(r'(\d+)', p)
            return int(m.group(1)) if m else 99

        sorted_pairs = sorted(all_pairs.keys(), key=_sort_key)

        # ── Normalise planned runway to a canonical pair ───────────────────────
        planned_pair = _canon_pair(_make_runway_pair(plan_rwy)) if plan_rwy else ''
        # The annotation text uses the specific filed end, not the full pair:
        #   dep → "-TAKEOFF RWY 31L-"   arr → "-LANDING RWY 22L-"
        planned_end  = plan_rwy.strip().upper() if plan_rwy else ''

        # ── Taxiway closure and approach aid NOTAMs ────────────────────────────
        twy_closures = _extract_twy_closures(notams_list)
        appr_notams  = _extract_approach_notams(notams_list, planned_end)

        # ── Station ops data from config ───────────────────────────────────────
        comms    = station_ops.get('comms', {})
        sp_advs  = station_ops.get('special_advisories', [])
        remarks  = station_ops.get('remarks', [])
        updated  = station_ops.get('updated', '')
        initials = station_ops.get('initials', '')

        # ── ATC freqs from OurAirports (fill gaps not covered by station_ops) ──
        atc_freqs = _get_airport_atc_freqs(icao)
        # ATIS: station_ops wins; OurAirports fills when absent
        atis_freq = (comms.get('atis_freq') or
                     _pick_atc_freq(atc_freqs, 'ATIS', 'ASOS', 'AWOS'))
        # TWR / GND for the optional ATC line in remarks when no station_ops
        twr_freq  = _pick_atc_freq(atc_freqs, 'TWR')
        gnd_freq  = _pick_atc_freq(atc_freqs, 'GND', 'GROUND')
        cld_freq  = _pick_atc_freq(atc_freqs, 'CLD')

        # ══ Assemble report ════════════════════════════════════════════════════

        # Header: "JFK 110416" (IATA/ICAO + flight number, matching real report)
        hdr_id = f"{display} {flt_num}".strip() if flt_num else display
        out  = f"\n{hdr_id}\n"
        out += "/FC\n"
        out += f"* {display} FIELD REPORT *\n"
        out += SEP + "\n"

        # Date / Time — use pre-computed local values
        if local_date or local_time:
            hdr = (f"DATE {local_date}" if local_date else "") + \
                  (f"  TIME {local_time} LOCAL" if local_time else "")
            out += hdr.strip() + "\n"
            out += SEP + "\n"

        # Wind / QNH / OAT line (if data available from TLR or METAR)
        if wx.get('wind_dir') or wx.get('altimeter') or wx.get('temp'):
            parts = []
            if wx.get('wind_dir'):
                spd = str(wx.get('wind_spd', '?')).zfill(2)
                gst = f"G{wx['wind_gst']:02d}" if wx.get('wind_gst') else ''
                parts.append(f"WIND {wx['wind_dir']}/{spd}{gst}KT")
            if wx.get('altimeter'):
                parts.append(f"ALT {wx['altimeter']}")
            if wx.get('temp'):
                parts.append(f"OAT {wx['temp']}C")
            out += "  ".join(parts) + "\n"
            out += SEP + "\n"

        out += "EXISTING TAA SEE NOTAMS\n"
        out += SEP + "\n"

        # ── Runway table ───────────────────────────────────────────────────────
        # Exact column layout from real reports:
        #   RWY col 0  (14 wide), STATUS col 15 (11 wide),
        #   CONDITIONS col 26 (11 wide), REMARKS col 37+
        C_RWY   = 14
        C_STAT  = 11
        C_COND  = 11

        out += f"{'RWY':{C_RWY}} {'STATUS':{C_STAT}} {'CONDITIONS':{C_COND}} REMARKS\n"

        for pair in sorted_pairs:
            info      = closed_map.get(pair)
            is_closed = info is not None
            status    = "CLOSED" if is_closed else "OPEN"
            cond_val  = all_pairs[pair]

            # Build remark:
            #   closed  → "DDMMMYYhhmm DDMMMYYhhmm  [NID]"
            #   planned → "-TAKEOFF RWY xx-" or "-LANDING RWY xx-"
            #   else    → ""
            if is_closed:
                s = info.get('eff_start', '')
                e = info.get('eff_end',   '')
                nid = info.get('nid', '')
                eff_str = f"{s} {e}".strip() if (s or e) else ''
                remark  = f"{eff_str}  {nid}".strip() if (eff_str or nid) else ''
            elif planned_pair and pair == planned_pair:
                verb   = "TAKEOFF" if role == 'DEP' else "LANDING"
                remark = f"-{verb} RWY {planned_end}-"
            else:
                remark = ""

            out += (f"{pair:{C_RWY}} {status:{C_STAT}} {cond_val:{C_COND}} {remark}").rstrip() + "\n"

        out += f"{'RAMP/TXWY':{C_RWY}} {'SURFACE':{C_STAT}} {surface}\n"
        out += SEP + "\n"

        # ── Taxiway closures (MX NOTAMs) — ID reference only ──────────────────
        if twy_closures:
            out += "TAXIWAY ADVISORIES\n"
            for tc in twy_closures:
                nid_str = f"  {tc['nid']}" if tc.get('nid') else ''
                out += f"TWY {tc['twy']} CLSD{nid_str}\n"
            out += SEP + "\n"

        # ── Approach/lighting NOTAMs — ID references only ──────────────────────
        if appr_notams:
            out += "APPROACH/LIGHTING ADVISORIES\n"
            for aa in appr_notams:
                out += f"{aa['nid']}\n"
            out += SEP + "\n"

        # ── Special advisories (station ops free text, or placeholder) ─────────
        out += "SPECIAL ADVISORIES\n"
        if sp_advs:
            for line in sp_advs:
                out += _tw.fill(str(line), width=76,
                                subsequent_indent="     ",
                                break_long_words=False, break_on_hyphens=False) + "\n"
        else:
            out += "SEE STATION OPS\n"
        out += SEP + "\n"

        # ── Comms / frequencies block ──────────────────────────────────────────
        # Real report column layout:
        #   label (16 wide)  freq (13 wide)  PHONE-LABEL  phone
        def _cline(lbl, freq, plbl, phone):
            f  = (str(freq)  if freq  else '').upper()
            ph = (str(phone) if phone else '').upper()
            return f"{lbl:<16} {f:<13}{plbl} {ph}".rstrip()

        out += _cline("OPS RADIO FREQ",   comms.get('ops_freq'),
                      "OPS PHONE",        comms.get('ops_phone'))   + "\n"
        # ATIS: station_ops config wins; OurAirports fills the gap
        out += _cline("ATIS FREQ",        atis_freq,
                      "ATIS PHONE",       comms.get('atis_phone'))  + "\n"
        out += _cline("MAINT FREQ",       comms.get('maint_freq'),
                      "MAINT PHONE",      comms.get('maint_phone')) + "\n"
        out += _cline("PHONE PATCH FREQ", comms.get('patch_freq'),
                      "PATCH PHONE",      comms.get('patch_phone')) + "\n"

        acars = (comms.get('acars_freq') or 'NONE').upper()
        gpu   = ('YES' if comms.get('gpu')     else ('NO' if 'gpu'     in comms else '')).ljust(6)
        gnd   = ('YES' if comms.get('gnd_air') else ('NO' if 'gnd_air' in comms else ''))
        out += f"{'ACARS FREQ':<16} {acars:<13}GROUND PWR {gpu} GROUND AIR {gnd}\n".rstrip() + "\n"
        out += SEP + "\n"

        # ── Remarks block ──────────────────────────────────────────────────────
        # If no station_ops remarks, fall back to ATC freq info from OurAirports
        # so the block is never completely empty.
        atc_lines = []
        if twr_freq: atc_lines.append(f"TOWER {twr_freq}")
        if gnd_freq: atc_lines.append(f"GROUND {gnd_freq}")
        if cld_freq: atc_lines.append(f"CLEARANCE {cld_freq}")

        if remarks or updated or initials or (atc_lines and not remarks):
            out += "REMARKS\n"
            for line in remarks:
                out += _tw.fill(str(line), width=76,
                                subsequent_indent="     ",
                                break_long_words=False, break_on_hyphens=False) + "\n"
            # Append OurAirports ATC freqs when station_ops has no remarks
            if atc_lines and not remarks:
                out += "ATC FREQS (OURAIRPORTS):  " + "  ".join(atc_lines) + "\n"
            if updated or initials:
                out += f"UPDATED  {updated}      {initials}".strip() + "\n"

        return out

    # ── Main ──────────────────────────────────────────────────────────────────
    try:
        out = "\n[PAGEBREAK]\n"  # one page-break for the entire field reports section

        # Flight identifier for the "ICAO FLTNUM" header line (e.g. "JFK 110416")
        _override_icao = _load_config().get("airline_override", "").strip().upper()
        _icao_pfx = _override_icao or (root.findtext('general/icao_airline') or '')
        _flt_num = (_icao_pfx + (root.findtext('general/flight_number') or '')).strip()

        # Timezone offsets for local time display
        try:
            _orig_tz = int(root.findtext('times/orig_timezone') or '0')
        except (ValueError, TypeError):
            _orig_tz = 0
        try:
            _dest_tz = int(root.findtext('times/dest_timezone') or '0')
        except (ValueError, TypeError):
            _dest_tz = 0

        def _local_hhmm(utc_ts_str, tz_offset):
            """Convert UTC Unix timestamp string to local HHMM string."""
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                ts  = int(utc_ts_str or '0')
                utc = _dt.utcfromtimestamp(ts)
                loc = utc + _td(hours=tz_offset)
                return loc.strftime("%H%M")
            except Exception:
                return ''

        def _local_date(utc_ts_str, tz_offset):
            """Convert UTC Unix timestamp string to local DDMMMxx date string."""
            try:
                from datetime import datetime as _dt, timedelta as _td
                months = ['JAN','FEB','MAR','APR','MAY','JUN',
                          'JUL','AUG','SEP','OCT','NOV','DEC']
                ts  = int(utc_ts_str or '0')
                utc = _dt.utcfromtimestamp(ts)
                loc = utc + _td(hours=tz_offset)
                return f"{loc.day:02d}{months[loc.month-1]}{str(loc.year)[2:]}"
            except Exception:
                return ''

        _sched_out_ts = (root.findtext('times/sched_out') or
                         root.findtext('general/sched_out') or '0').strip()
        _sched_in_ts  = (root.findtext('times/sched_in') or '0').strip()

        # dep_time used only as fallback when timestamp parsing fails
        dte_node = root.find('.//date_time')
        dep_time_raw = (dte_node.text or '').strip() if dte_node is not None else ''

        # Load station ops config (keyed by ICAO, accepts K-prefix or bare 3-letter)
        _all_ops = _load_config().get('station_ops', {})
        def _get_ops(icao):
            return _all_ops.get(icao, _all_ops.get(icao.lstrip('K'), {}))

        # ── Origin ────────────────────────────────────────────────────────────
        orig_icao     = (root.findtext('origin/icao_code') or '').strip().upper()
        orig_iata     = (root.findtext('origin/iata_code')  or '').strip().upper()
        orig_plan_rwy = (root.findtext('origin/plan_rwy')   or '').strip().upper()
        orig_metar    = (root.findtext('weather/orig_metar') or
                         root.findtext('origin/metar')       or '').strip()
        to_node       = root.find('.//tlr/takeoff')
        orig_section  = root.find('origin')
        orig_notams   = (orig_section.find('notams') or orig_section).findall('.//notam') \
                        if orig_section is not None else []

        orig_date  = _local_date(_sched_out_ts, _orig_tz) or dep_time_raw
        orig_time  = _local_hhmm(_sched_out_ts, _orig_tz)
        out += _build_airport_report(
            orig_icao, orig_iata, to_node, orig_notams,
            orig_date, orig_time,
            orig_plan_rwy, orig_metar, 'DEP', _get_ops(orig_icao), _flt_num)

        # ── Destination ───────────────────────────────────────────────────────
        dest_icao     = (root.findtext('destination/icao_code') or '').strip().upper()
        dest_iata     = (root.findtext('destination/iata_code')  or '').strip().upper()
        dest_plan_rwy = (root.findtext('destination/plan_rwy')   or '').strip().upper()
        dest_metar    = (root.findtext('weather/dest_metar') or
                         root.findtext('destination/metar') or '').strip()
        ld_node       = root.find('.//tlr/landing')
        dest_section  = root.find('destination')
        dest_notams   = (dest_section.find('notams') or dest_section).findall('.//notam') \
                        if dest_section is not None else []

        dest_date  = _local_date(_sched_in_ts, _dest_tz) or dep_time_raw
        dest_time  = _local_hhmm(_sched_in_ts, _dest_tz)
        out += _build_airport_report(
            dest_icao, dest_iata, ld_node, dest_notams,
            dest_date, dest_time,
            dest_plan_rwy, dest_metar, 'ARR', _get_ops(dest_icao), _flt_num)

        if out.strip():
            out += "END DATA\n"
        return out

    except Exception as e:
        import traceback
        print(f"Error generating field reports: {e}")
        traceback.print_exc()
        return ""


def write_nat_tracks_section(root):
    """
    Build the NAT-TRACKS page, matching the real OFP layout (e.g. page 50).

    Sources checked (in order):
      tracks/nat_notams/eggx  – Shanwick westbound OTS message
      tracks/nat_notams/czqx  – Gander/Shanwick eastbound OTS message
      tracks/nat_notams/czqo  – Gander OCA (alternate tag)
      tracks/nat               – individual parsed NAT records (fallback)

    The raw NOTAM text from SimBrief already contains the full formatted
    message (track letters, waypoints, levels, remarks).  We just need to:
      • wrap it in a [PAGEBREAK]
      • print the "NAT-TRACKS" header
      • re-flow the text so it wraps at ~75 chars with no mid-word breaks

    If none of the above elements contain text the function returns "".
    """
    try:
        import textwrap

        # ── Collect raw NOTAM text blocks ─────────────────────────────────────
        raw_blocks = []

        # Determine flight direction from first/last navlog fix longitude
        fixes_all = root.findall("navlog/fix")
        is_eastbound = True  # default
        if len(fixes_all) >= 2:
            def _flon(f):
                n = f.find("pos_long")
                try: return float(n.text.strip()) if n is not None and n.text else None
                except: return None
            lo_first = _flon(fixes_all[0])
            lo_last  = _flon(fixes_all[-1])
            if lo_first is not None and lo_last is not None:
                is_eastbound = lo_last > lo_first

        # Only include the block that matches the flight direction
        # eggx = Shanwick westbound OTS, czqx/czqo/bird = eastbound OTS
        WESTBOUND_TAGS = ("eggx", "kzwy")
        EASTBOUND_TAGS = ("czqx", "czqo", "bird")
        TAG_LABEL = {
            "eggx": "WESTBOUND OTS (SHANWICK)",
            "kzwy": "WESTBOUND OTS (NEW YORK OCEANIC)",
            "czqx": "EASTBOUND OTS (GANDER/SHANWICK)",
            "czqo": "EASTBOUND OTS (GANDER)",
            "bird": "EASTBOUND OTS (REYKJAVIK)",
        }
        wanted_tags = EASTBOUND_TAGS if is_eastbound else WESTBOUND_TAGS

        nat_notams = root.find("tracks/nat_notams")
        if nat_notams is not None:
            for tag in wanted_tags:
                el = nat_notams.find(tag)
                if el is not None and el.text and el.text.strip():
                    raw_blocks.append((TAG_LABEL.get(tag, ""), el.text.strip()))

        # Fallback: individual <nat> elements under <tracks>
        if not raw_blocks:
            for nat_el in root.findall("tracks/nat"):
                txt = nat_el.text or ""
                if txt.strip():
                    raw_blocks.append(("", txt.strip()))

        if not raw_blocks:
            return ""

        # ── Format each block ─────────────────────────────────────────────────
        # The raw text is a single long string. Split on the natural sentence
        # boundaries: each sentence ends with a dash "-" or a period ".".
        # We preserve lines that look like headings (ALL CAPS short lines) and
        # wrap prose at 75 chars.
        PAGE_W = 75

        def format_block(raw):
            """
            Clean and re-flow a single NAT NOTAM text block.
            Produces one line per logical item, wrapped at PAGE_W chars.
            """
            # Normalise whitespace
            text = " ".join(raw.split())

            # Split on common sentence delimiters used in NAT NOTAMs:
            #   "- " before a new track letter (A GOMUP …)
            #   numbered remarks "1. " "2. " etc.
            # We'll split on "  " (double space) and "-" preceded by word chars
            import re
            # Insert newlines before track letters: "- A " → newline + "A "
            text = re.sub(r'-\s+([A-Z])\s+', r'\n\1 ', text)
            # Insert newlines before "REMARKS" and numbered items
            # Use lookahead: only split "N. " when followed by an uppercase word
            text = re.sub(r'\s+(REMARKS[\.\:])', r'\nREMARKS.', text)
            text = re.sub(r'\s+(\d{1,2}\.\s+(?=[A-Z]))', lambda m: '\n' + m.group(1), text)
            # Insert newlines before END OF PART
            text = re.sub(r'\s*(END OF PART)', r'\nEND OF PART', text)
            text = re.sub(r'\s*(PART (ONE|TWO|THREE) OF)', r'\nPART \2 OF', text)

            lines = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Wrap long lines
                wrapped = textwrap.fill(
                    line,
                    width=PAGE_W,
                    subsequent_indent="   ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                lines.append(wrapped)
            return "\n".join(lines)

        # ── Build output ──────────────────────────────────────────────────────
        out = "\n[PAGEBREAK]\n\n"
        out += "NAT-TRACKS\n\n"

        for i, (direction, block) in enumerate(raw_blocks):
            if direction:
                out += f"{direction}\n"
                out += "-" * len(direction) + "\n"
            formatted = format_block(block)
            if formatted:
                out += formatted + "\n"
            if i < len(raw_blocks) - 1:
                out += "\n"  # blank line between blocks

        return out

    except Exception as e:
        import traceback
        print(f"Error generating NAT tracks section: {e}")
        traceback.print_exc()
        return ""


def _build_etops_oceanic_pdf(root):
    """
    Exact visual match to SimBrief OFP:
    - Plain page header line
    - Single black-bordered box
    - Grey header bars (title + column headers), left-aligned labels
    - White data rows, thin grey rules between fix groups
    - Footer: - Not for real world navigation -   Page N of ?
    """
    import io, math
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    PW, PH = letter          # 612 x 792 pts
    LM = 50; RM = 50; TM = 55; BM = 45
    CW = PW - LM - RM        # 512 pts content width

    # ── Font ─────────────────────────────────────────────────────────────────
    _MONO_PATHS = [
        "/Users/tobygibbons/Library/Fonts/CourierPrime-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/Library/Fonts/Courier New.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    _mono = "Courier"
    for _p in _MONO_PATHS:
        if os.path.exists(_p):
            try:
                pdfmetrics.registerFont(TTFont("_EtopsMono", _p))
                _mono = "_EtopsMono"
            except Exception:
                pass
            break
    _bold = "Courier-Bold"   # always available

    buf = io.BytesIO()
    c   = _rl_canvas.Canvas(buf, pagesize=letter)

    # ── Colours ───────────────────────────────────────────────────────────────
    BLACK    = colors.black
    GREY_HD  = colors.HexColor("#D0D0D0")   # grey header bar fill
    WHITE    = colors.white
    RULE_CLR = colors.HexColor("#AAAAAA")   # thin horizontal rule

    FS   = 8.0    # base font size
    LH   = 12     # row height (pts)
    HDR_H = 13    # column-header row height
    PAD  = 4      # left padding inside box

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _sfloat(v, d=0.0):
        try: return float(v)
        except: return d

    def _gf(elem, tag):
        n = elem.find(tag)
        return n.text.strip() if n is not None and n.text else ""

    def fmt_coord(val_str, is_lon=False):
        try:
            val = float(val_str)
            hem = ("W" if val < 0 else "E") if is_lon else ("N" if val >= 0 else "S")
            a = abs(val); d = int(a); m = round((a - d) * 600)
            return f"{hem}{d:03d}{m:03d}" if is_lon else f"{hem}{d:02d}{m:03d}"
        except: return "-------"

    def fmt_eet(sec_str):
        try: s = int(sec_str); return f"{s//3600:02d}:{(s%3600)//60:02d}"
        except: return "--:--"

    def fmt_fuel(v):
        try: return f"{int(float(v)):06d}"
        except: return "------"

    def fmt_signed(v):
        try: vv = int(float(v)); return f"{'P' if vv>=0 else 'M'}{abs(vv):02d}"
        except: return "P00"

    def fmt_wind_comp(v):
        try: vv = int(float(v)); return f"{'T' if vv>=0 else 'H'}{abs(vv):03d}"
        except: return "T000"

    def gc_bearing(la1, lo1, la2, lo2):
        try:
            la1,lo1,la2,lo2 = map(math.radians,[la1,lo1,la2,lo2])
            dlo = lo2-lo1
            x_ = math.sin(dlo)*math.cos(la2)
            y_ = math.cos(la1)*math.sin(la2) - math.sin(la1)*math.cos(la2)*math.cos(dlo)
            return round(math.degrees(math.atan2(x_,y_)) % 360)
        except: return None

    # ── Page furniture ────────────────────────────────────────────────────────
    def draw_page_header(page_num):
        """Plain text header line, no box."""
        rel  = (root.findtext("general/release") or "").strip()
        orig = (root.findtext("origin/icao_code") or "").strip()
        dest = (root.findtext("destination/icao_code") or "").strip()
        _etops_icao_pfx = _load_config().get("airline_override", "").strip().upper() or \
                          (root.findtext("general/icao_airline") or "")
        flt  = (_etops_icao_pfx + (root.findtext("general/flight_number") or "")).strip()
        try:
            from datetime import datetime, timezone
            ts = int(root.findtext("general/sched_out") or "0")
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        except: dt = ""
        c.setFont(_bold, FS)
        c.setFillColor(BLACK)
        c.drawString(LM, PH - 35, f"PAGE {page_num}   RELEASE {rel}   {dt}   {orig}-{dest}   {flt}")

    def draw_footer(page_num, total_pages):
        c.setFont(_mono, FS - 0.5)
        c.setFillColor(BLACK)
        c.drawCentredString(PW/2, BM - 12, "- Not for real world navigation -")
        c.drawRightString(LM + CW, BM - 12, f"Page {page_num} of {total_pages}")

    def grey_bar(x, y, w, h):
        """Filled grey rectangle, black bottom border."""
        c.setFillColor(GREY_HD)
        c.rect(x, y, w, h, fill=1, stroke=0)
        c.setStrokeColor(BLACK)
        c.setLineWidth(0.5)
        c.line(x, y, x+w, y)

    def thin_rule(y):
        c.setStrokeColor(RULE_CLR)
        c.setLineWidth(0.3)
        c.line(LM, y, LM+CW, y)

    def black_rule(y):
        c.setStrokeColor(BLACK)
        c.setLineWidth(0.6)
        c.line(LM, y, LM+CW, y)

    def txt(x, y, s, bold=False, fs=FS):
        c.setFont(_bold if bold else _mono, fs)
        c.setFillColor(BLACK)
        c.drawString(x, y, s)

    def close_box(top_y, bot_y):
        """Draw outer black rect from top_y down to bot_y."""
        h = top_y - bot_y
        c.setStrokeColor(BLACK)
        c.setLineWidth(0.8)
        c.rect(LM, bot_y, CW, h, fill=0, stroke=1)

    # ── Pull ETOPS data ───────────────────────────────────────────────────────
    is_etops      = (root.findtext("general/is_etops") or "0").strip()
    etops_section = root.find("etops")
    if is_etops != "1" or etops_section is None:
        c.save(); return buf.getvalue()

    etops_rule  = etops_section.findtext("rule", "")
    entry_point = etops_section.find("entry")
    etp_point   = etops_section.find("equal_time_point")
    exit_point  = etops_section.find("exit")

    # suitability windows
    suitable_windows = {}
    for apt in etops_section.findall("suitable_airport"):
        icao  = apt.findtext("icao_code", "")
        s_raw = apt.findtext("suitability_start", "")
        e_raw = apt.findtext("suitability_end", "")
        if icao and s_raw and e_raw:
            try:
                from datetime import datetime, timezone
                sd_ = datetime.fromisoformat(s_raw.replace("Z","+00:00"))
                ed_ = datetime.fromisoformat(e_raw.replace("Z","+00:00"))
                suitable_windows[icao] = f"{sd_.strftime('%H:%M')}-{ed_.strftime('%H:%M')}"
            except: suitable_windows[icao] = ""

    _cont = root.findtext("general/cont_rule","")
    try: ai_pct = f"{int(_cont):03d}%"
    except: ai_pct = "005%"

    # scenario string
    scenario_str = etops_section.findtext("scenario_string","")
    if not scenario_str:
        cc = ""
        for pt in [entry_point, etp_point, exit_point]:
            if pt is not None:
                cc = pt.findtext("etops_condition",""); break
        scenario_str = {"DX":"1EO AUTO DCP+","D":"1EO AUTO DCP+",
                        "X":"1EO MANUAL DCP+","N":"1EO AUTO NORMAL"
                        }.get(cc, f"1EO AUTO {cc}" if cc else "1EO AUTO DCP+")

    # header NM - exact SimBrief format with full precision
    header_nm = ""
    if etp_point is not None:
        try:
            dists = [float(d.findtext("distance","0")) for d in etp_point.findall("div_airport")]
            if dists: header_nm = f"{max(dists):.10g}NM"
        except: pass

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 1 — ETOPS CRITICAL FUEL SCENARIO
    # ═══════════════════════════════════════════════════════════════════════════
    page_num = [6]

    # Column x-positions — measured to match SimBrief spacing
    # Row 1 headers: SEQ TYPE LAT/LONG REMF MIN_REQ EET SCENARIO
    E = {                       # ETOPS point row
        "SEQ":      LM + PAD,
        "TYPE":     LM + 26,
        "LATLONG":  LM + 118,
        "REMF":     LM + 226,
        "MINREQ":   LM + 280,
        "EET":      LM + 334,
        "SCENARIO": LM + 372,
    }
    # Row 2 headers: (blank) ALTN GCD FL COMP TMP TD AI TRIPF TIME SWX_WINDOW
    D = {                       # diversion airport row
        "E":        LM + PAD,
        "ALTN":     LM + 22,
        "GCD":      LM + 66,
        "FL":       LM + 100,
        "COMP":     LM + 128,
        "TMP":      LM + 168,
        "TD":       LM + 200,
        "AI":       LM + 228,
        "TRIPF":    LM + 258,
        "MINREQ":   LM + 308,
        "TIME":     LM + 360,
        "WINDOW":   LM + 398,
    }

    # y starts just below page header
    y = PH - TM - 10
    box_top = y   # top of outer box

    # ── Title row ─────────────────────────────────────────────────────────────
    TITLE_H = HDR_H + 2
    title_txt = f"CRITICAL FUEL SCENARIO BASED ON {etops_rule}MIN / {header_nm}" if header_nm \
                else f"CRITICAL FUEL SCENARIO  ETOPS-{etops_rule}"
    grey_bar(LM, y - TITLE_H, CW, TITLE_H)
    c.setFont(_bold, FS)
    c.setFillColor(BLACK)
    c.drawCentredString(PW/2, y - TITLE_H + (TITLE_H - FS)/2 + 1, title_txt)
    y -= TITLE_H

    # ── Column header row 1 ───────────────────────────────────────────────────
    grey_bar(LM, y - HDR_H, CW, HDR_H)
    c.setFont(_bold, FS - 0.5)
    c.setFillColor(BLACK)
    for label, x in [("SEQ", E["SEQ"]), ("TYPE", E["TYPE"]), ("LAT/LONG", E["LATLONG"]),
                     ("REMF", E["REMF"]), ("MIN REQ", E["MINREQ"]),
                     ("EET", E["EET"]), ("SCENARIO", E["SCENARIO"])]:
        c.drawString(x, y - HDR_H + (HDR_H - (FS-0.5))/2 + 1, label)
    y -= HDR_H

    # ── Column header row 2 ───────────────────────────────────────────────────
    grey_bar(LM, y - HDR_H, CW, HDR_H)
    c.setFont(_bold, FS - 0.5)
    for label, x in [("ALTN GCD", D["ALTN"]), ("FL", D["FL"]),
                     ("COMP TMP TD", D["COMP"]), ("AI", D["AI"]),
                     ("TRIPF", D["TRIPF"]), ("TIME", D["TIME"]),
                     ("SWX WINDOW", D["WINDOW"])]:
        c.drawString(x, y - HDR_H + (HDR_H - (FS-0.5))/2 + 1, label)
    y -= HDR_H

    # ── ETOPS point emitter ───────────────────────────────────────────────────
    seq = [1]

    def emit_point(pt, label):
        nonlocal y
        if pt is None: return

        lat_s = fmt_coord(_gf(pt,"pos_lat_fix") or _gf(pt,"pos_lat_apt") or _gf(pt,"pos_lat"))
        lon_s = fmt_coord(_gf(pt,"pos_long_fix") or _gf(pt,"pos_long_apt") or _gf(pt,"pos_long"), True)
        remf_s  = fmt_fuel(_gf(pt,"fuel_min_reqd") or _gf(pt,"remf"))
        minreq_s= ""   # min req is on diversion rows
        eet_s   = fmt_eet(_gf(pt,"eet"))

        # blank white row above each point group
        c.setFillColor(WHITE)
        c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
        y -= LH

        # Main point row
        c.setFillColor(WHITE)
        c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
        txt(E["SEQ"],      y - FS + 1, str(seq[0]), bold=True)
        txt(E["TYPE"],     y - FS + 1, label,        bold=True)
        txt(E["LATLONG"],  y - FS + 1, f"{lat_s} {lon_s}")
        txt(E["REMF"],     y - FS + 1, remf_s)
        txt(E["EET"],      y - FS + 1, eet_s)
        txt(E["SCENARIO"], y - FS + 1, scenario_str)
        y -= LH
        seq[0] += 1

        # Diversion rows
        for div in pt.findall("div_airport"):
            dicao    = _gf(div, "icao_code")
            dist_i   = str(int(_sfloat(_gf(div, "distance"))))
            fl_s     = _gf(div, "fl") or _gf(div, "cruise_fl") or "100"
            comp_s   = fmt_wind_comp(_gf(div, "wind_comp"))
            tmp_s    = fmt_signed(_gf(div, "oat"))
            td_s     = fmt_signed(_gf(div, "oat_isa_dev"))
            burn_s   = fmt_fuel(_gf(div, "fuel_trip"))
            minfob_s = fmt_fuel(_gf(div, "fuel_min_reqd") or _gf(div, "min_fob"))
            eet_d    = fmt_eet(_gf(div, "eet"))
            win_s    = suitable_windows.get(dicao, "")

            c.setFillColor(WHITE)
            c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
            txt(D["E"],      y - FS + 1, "E")
            txt(D["ALTN"],   y - FS + 1, f"{dicao} {dist_i}")
            txt(D["FL"],     y - FS + 1, fl_s)
            txt(D["COMP"],   y - FS + 1, comp_s)
            txt(D["TMP"],    y - FS + 1, tmp_s)
            txt(D["TD"],     y - FS + 1, td_s)
            txt(D["AI"],     y - FS + 1, ai_pct)
            txt(D["TRIPF"],  y - FS + 1, burn_s)
            txt(D["MINREQ"], y - FS + 1, minfob_s)
            txt(D["TIME"],   y - FS + 1, eet_d)
            txt(D["WINDOW"], y - FS + 1, win_s)
            y -= LH

    emit_point(entry_point, "ETOPS_ENTRY")
    emit_point(etp_point,   "ETOPS_ETP")
    emit_point(exit_point,  "ETOPS_EXIT")

    # blank row at bottom of box
    c.setFillColor(WHITE)
    c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
    y -= LH

    close_box(box_top, y)

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 2 — OCEANIC ROUTE VERIFICATION
    # ═══════════════════════════════════════════════════════════════════════════
    def _pt_latlon(pt):
        if pt is None: return None, None
        ls = _gf(pt,"pos_lat_fix") or _gf(pt,"pos_lat_apt") or _gf(pt,"pos_lat")
        lo = _gf(pt,"pos_long_fix") or _gf(pt,"pos_long_apt") or _gf(pt,"pos_long")
        try: return float(ls), float(lo)
        except: return None, None

    entry_lat, entry_lon = _pt_latlon(entry_point)
    exit_lat,  exit_lon  = _pt_latlon(exit_point)

    OCEANIC_FIRS = {"EGGX","BIRD","CZQO","CZQX","KZWY","KZAK","ENOB","LPPO","YMOR","NZZO","YMMM"}
    fixes = root.findall("navlog/fix")

    ocn_fixes = []
    if fixes and entry_lat is not None and exit_lat is not None:
        ocn_idx = [i for i,f in enumerate(fixes)
                   if (_gf(f,"fir") or "").strip().upper() in OCEANIC_FIRS]
        if ocn_idx:
            ei, xi = ocn_idx[0], ocn_idx[-1]
        else:
            PROX = 0.40
            def cdist(la1,lo1,la2,lo2):
                try: return abs(float(la1)-float(la2))+abs(float(lo1)-float(lo2))
                except: return 9999
            ei = xi = None
            for ii, ff in enumerate(fixes):
                flat,flon = _gf(ff,"pos_lat"),_gf(ff,"pos_long")
                if ei is None and cdist(flat,flon,entry_lat,entry_lon)<PROX: ei=ii
                if cdist(flat,flon,exit_lat,exit_lon)<PROX: xi=ii
            if ei is None or xi is None:
                try:
                    lmin=min(float(entry_lon),float(exit_lon))-1.0
                    lmax=max(float(entry_lon),float(exit_lon))+1.0
                    cands=[i for i,f in enumerate(fixes)
                           if lmin<=_sfloat(_gf(f,"pos_long"))<=lmax]
                    if cands:
                        if ei is None: ei=cands[0]
                        if xi is None: xi=cands[-1]
                except: pass
            if ei is None or xi is None: ei=xi=None
            if ei is not None and xi is not None and ei>xi: ei,xi=xi,ei
        ocn_fixes = fixes[ei:xi+1] if (ei is not None and xi is not None) else []

    if ocn_fixes:
        c.showPage()
        page_num[0] += 1
        # ORV column x-positions — match SimBrief exactly
        O = {
            "TO":   LM + PAD,
            "LAT":  LM + 100,
            "LONG": LM + 170,
            "TC":   LM + 248,
            "MC":   LM + 282,
            "MH":   LM + 316,
            "DIST": LM + 350,
        }

        y = PH - TM - 10
        box_top2 = y

        # Title
        grey_bar(LM, y - TITLE_H, CW, TITLE_H)
        c.setFont(_bold, FS)
        c.setFillColor(BLACK)
        c.drawCentredString(PW/2, y - TITLE_H + (TITLE_H - FS)/2 + 1, "OCEANIC ROUTE VERIFICATION")
        y -= TITLE_H

        def draw_orv_col_headers():
            nonlocal y
            # Row 1: TO  LAT  LONG  TC  MC  MH  DIST
            grey_bar(LM, y - HDR_H, CW, HDR_H)
            c.setFont(_bold, FS - 0.5)
            c.setFillColor(BLACK)
            for label, x in [("TO", O["TO"]), ("LAT", O["LAT"]), ("LONG", O["LONG"]),
                              ("TC", O["TC"]), ("MC", O["MC"]), ("MH", O["MH"]),
                              ("DIST", O["DIST"])]:
                c.drawString(x, y - HDR_H + (HDR_H-(FS-0.5))/2+1, label)
            y -= HDR_H
            # Row 2: IDENT
            grey_bar(LM, y - HDR_H, CW, HDR_H)
            c.setFont(_bold, FS - 0.5)
            c.setFillColor(BLACK)
            c.drawString(O["TO"], y - HDR_H + (HDR_H-(FS-0.5))/2+1, "IDENT")
            y -= HDR_H

        draw_orv_col_headers()

        def fmt_lat_ocn(lf):
            hem="N" if lf>=0 else "S"; a=abs(lf); d=int(a); m=round((a-d)*600)
            return f"{hem}{d:02d}{m:03d}"
        def fmt_lon_ocn(lf):
            hem="E" if lf>=0 else "W"; a=abs(lf); d=int(a); m=round((a-d)*600)
            return f"{hem}{d:03d}{m:03d}"

        for ii, ff in enumerate(ocn_fixes):
            flat_f = _sfloat(_gf(ff,"pos_lat"))
            flon_f = _sfloat(_gf(ff,"pos_long"))
            mc_r   = _gf(ff,"track_mag")
            mh_r   = _gf(ff,"heading_mag")
            dr_r   = _gf(ff,"distance")
            name   = (_gf(ff,"name") or _gf(ff,"ident") or "").upper().strip()
            ident  = (_gf(ff,"ident") or "").upper().strip()

            lat_s2 = fmt_lat_ocn(flat_f)
            lon_s2 = fmt_lon_ocn(flon_f)
            is_last = (ii+1 == len(ocn_fixes))

            if not is_last:
                nf  = ocn_fixes[ii+1]
                tcv = gc_bearing(flat_f, flon_f,
                                 _sfloat(_gf(nf,"pos_lat")),
                                 _sfloat(_gf(nf,"pos_long")))
                tc_s = f"{tcv:03d}" if tcv is not None else "---"
            else:
                tc_s = "---"

            try: mc_s = f"{int(round(float(mc_r))):03d}"
            except: mc_s = "---"
            try: mh_s = f"{int(round(float(mh_r))):03d}"
            except: mh_s = "---"

            if is_last:
                dist_s = "----"; tc_s = mc_s = mh_s = "---"
            else:
                try: dist_s = f"{int(round(float(dr_r))):04d}"
                except: dist_s = "----"

            # page overflow check (need 3 rows: blank + name + ident)
            if y - 3*LH < BM + 20:
                close_box(box_top2, y)
                c.showPage()
                page_num[0] += 1
                y = PH - TM - 10
                box_top2 = y
                grey_bar(LM, y - TITLE_H, CW, TITLE_H)
                c.setFont(_bold, FS)
                c.drawCentredString(PW/2, y-TITLE_H+(TITLE_H-FS)/2+1,
                                    "OCEANIC ROUTE VERIFICATION (cont.)")
                y -= TITLE_H
                draw_orv_col_headers()

            # blank spacer row
            c.setFillColor(WHITE)
            c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
            y -= LH

            # Name row (bold name, then data)
            c.setFillColor(WHITE)
            c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
            txt(O["TO"],   y - FS + 1, name,   bold=True)
            txt(O["LAT"],  y - FS + 1, lat_s2)
            txt(O["LONG"], y - FS + 1, lon_s2)
            txt(O["TC"],   y - FS + 1, tc_s)
            txt(O["MC"],   y - FS + 1, mc_s)
            txt(O["MH"],   y - FS + 1, mh_s)
            txt(O["DIST"], y - FS + 1, dist_s)
            y -= LH

            # Ident row
            c.setFillColor(WHITE)
            c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
            txt(O["TO"], y - FS + 1, ident if ident != name else "")
            black_rule(y - LH)
            y -= LH

        # blank closing row
        c.setFillColor(WHITE)
        c.rect(LM, y - LH, CW, LH, fill=1, stroke=0)
        y -= LH

        close_box(box_top2, y)

    c.save()
    return buf.getvalue()

def write_oceanic_route_verification(root, entry_lat, entry_lon, exit_lat, exit_lon):
    """
    Legacy stub — oceanic rendering is now handled inside _build_etops_oceanic_pdf()
    which is called by write_etops_section().  Returns "" so callers don't double-render.
    """
    return ""


def write_etops_section(root):
    """
    Generate ETOPS Critical Fuel Scenario + Oceanic Route Verification pages
    using ReportLab for rich layout.  Returns a [PDF_BLOB:...] marker string
    that save_as_pdf() knows how to splice in, or "" if ETOPS is not active.
    """
    import base64

    try:
        print("DEBUG ETOPS: Starting write_etops_section (ReportLab)")
        is_etops = (root.findtext("general/is_etops") or "0").strip()
        print(f"DEBUG ETOPS: is_etops = '{is_etops}'")
        if is_etops != "1":
            print("DEBUG ETOPS: ETOPS not active")
            return ""

        etops_section = root.find("etops")
        if etops_section is None:
            print("DEBUG ETOPS: No <etops> element")
            return ""

        if not etops_section.findtext("rule","").strip():
            print("DEBUG ETOPS: No ETOPS rule")
            return ""

        pdf_bytes = _build_etops_oceanic_pdf(root)
        if not pdf_bytes:
            print("DEBUG ETOPS: Empty PDF returned")
            return ""

        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        marker = f"[PDF_BLOB:{b64}]"
        print(f"DEBUG ETOPS: ReportLab PDF built, {len(pdf_bytes)} bytes")
        return "\n" + marker + "\n"

    except Exception as e:
        print(f"Error generating ETOPS section: {e}")
        import traceback; traceback.print_exc()
        return ""



    """
    Build the OCEANIC ROUTE VERIFICATION table, matching the real SimBrief OFP
    layout (page 8 of the EGLL-KMIA example):

    ┌─────────────────────────────────────────────────────────────────────────┐
    │              OCEANIC ROUTE VERIFICATION                                 │
    │ TO              LAT    LONG     TC  MC  MH  DIST                        │
    │ IDENT                                                                   │
    ├─────────────────────────────────────────────────────────────────────────┤
    │ MALOT           N53000 W015000 ---  --- --- ----                        │
    │                                                                         │
    │ N5230W02000     N52300 W020000  262 267 261 0184                        │
    │ H5220                                                                   │
    ...

    Column notes
    ────────────
    TO      navlog `name` field (e.g. MALOT, N5230W02000, N50W030)
    LAT     {N|S}{deg:02d}{min*10:03d}  →  N53000, N52300, N48300
    LONG    {E|W}{deg:03d}{min*10:03d}  →  W015000, W020000, W060161
    TC      initial great-circle bearing to next fix (computed), "---" for last
    MC      track_mag from navlog fix, "---" if absent
    MH      heading_mag from navlog fix, "---" if absent
    DIST    4-digit zero-padded leg distance (nm), "----" for last fix

    IDENT   navlog `ident` field, printed on line 2 ONLY when it differs from
            the `name` (i.e. the fix has a separate short computer ID like H5220)

    Oceanic segment detection (priority order)
    ───────────────────────────────────────────
    1. Oceanic FIR codes: EGGX (Shanwick), BIRD (Reykjavik), CZQO/CZQX (Gander),
       KZWY (NY Oceanic), ENOB (Bodø), LPPO (Santa Maria), KZAK (Oakland)
    2. ETOPS entry/exit coordinate proximity (±0.4°)
    3. Longitude bracket fallback

    The section starts on a fresh [PAGEBREAK] to match the real OFP.
    """
    try:
        # ── Known North Atlantic / Pacific oceanic FIR codes ──────────────────
        OCEANIC_FIRS = {
            "EGGX",   # Shanwick OCA (North Atlantic, eastbound)
            "BIRD",   # Reykjavik OCA
            "CZQO",   # Gander OCA (North Atlantic, westbound)
            "CZQX",   # Gander OCA (extension)
            "KZWY",   # New York Oceanic East
            "KZAK",   # Oakland Oceanic (Pacific)
            "ENOB",   # Bodø Oceanic (Norwegian Sea)
            "LPPO",   # Santa Maria Oceanic (mid-Atlantic)
            "YMOR",   # Australia NZZO
            "NZZO",   # New Zealand Oceanic
            "YMMM",   # Melbourne (Pac)
        }

        def safe_float(v, d=0.0):
            try:
                return float(v)
            except Exception:
                return d

        def get_f(elem, tag):
            n = elem.find(tag)
            return n.text.strip() if n is not None and n.text else ""

        # ── Coordinate formatters ─────────────────────────────────────────────
        def fmt_lat(lat_f):
            """N53000  (N|S + 2-digit deg + 3-digit min×10)"""
            hem = "N" if lat_f >= 0 else "S"
            a   = abs(lat_f)
            d   = int(a)
            m   = round((a - d) * 600)
            return f"{hem}{d:02d}{m:03d}"

        def fmt_lon(lon_f):
            """W015000  (E|W + 3-digit deg + 3-digit min×10)"""
            hem = "E" if lon_f >= 0 else "W"
            a   = abs(lon_f)
            d   = int(a)
            m   = round((a - d) * 600)
            return f"{hem}{d:03d}{m:03d}"

        # ── Find oceanic fixes in the navlog ──────────────────────────────────
        fixes = root.findall("navlog/fix")
        if not fixes:
            return ""

        # Strategy 1: FIR-based detection
        oceanic_indices = [
            i for i, f in enumerate(fixes)
            if (get_f(f, "fir") or "").strip().upper() in OCEANIC_FIRS
        ]

        if oceanic_indices:
            entry_idx = oceanic_indices[0]
            exit_idx  = oceanic_indices[-1]
            print(f"DEBUG OCN: FIR-based detection found {len(oceanic_indices)} oceanic fixes "
                  f"(idx {entry_idx}–{exit_idx})")
        else:
            # Strategy 2: proximity to ETOPS entry/exit coords
            PROX = 0.40

            def coord_dist(la1, lo1, la2, lo2):
                try:
                    return abs(float(la1) - float(la2)) + abs(float(lo1) - float(lo2))
                except Exception:
                    return 9999

            entry_idx = exit_idx = None
            for i, f in enumerate(fixes):
                flat = get_f(f, "pos_lat")
                flon = get_f(f, "pos_long")
                if (entry_idx is None and entry_lat is not None
                        and coord_dist(flat, flon, entry_lat, entry_lon) < PROX):
                    entry_idx = i
                if (exit_lat is not None
                        and coord_dist(flat, flon, exit_lat, exit_lon) < PROX):
                    exit_idx = i

            # Strategy 3: longitude bracket fallback
            if entry_idx is None or exit_idx is None:
                try:
                    lo_min = min(float(entry_lon), float(exit_lon)) - 1.0
                    lo_max = max(float(entry_lon), float(exit_lon)) + 1.0
                    cands  = [i for i, f in enumerate(fixes)
                               if lo_min <= safe_float(get_f(f, "pos_long")) <= lo_max]
                    if cands:
                        if entry_idx is None:
                            entry_idx = cands[0]
                        if exit_idx is None:
                            exit_idx  = cands[-1]
                except Exception:
                    pass

            if entry_idx is None or exit_idx is None:
                print("DEBUG OCN: Could not determine oceanic segment — skipping table")
                return ""

            if entry_idx > exit_idx:
                entry_idx, exit_idx = exit_idx, entry_idx

            print(f"DEBUG OCN: Coord/lon-based detection: idx {entry_idx}–{exit_idx}")

        oceanic_fixes = fixes[entry_idx : exit_idx + 1]
        if not oceanic_fixes:
            return ""

        # ── Build bordered table (matches real OFP box style) ─────────────────
        # Inner width chosen to match the ETOPS box (73 chars)
        BOX_W = 73

        def bline(text=""):
            return f"|{str(text):<{BOX_W}}|\n"

        def bsep():
            return f"+{'-' * BOX_W}+\n"

        out = "\n[PAGEBREAK]\n\n"
        out += bsep()
        # Title — centred
        title = "OCEANIC ROUTE VERIFICATION"
        out += bline(f"{title:^{BOX_W}}")
        out += bsep()
        # Column header rows (two lines, matching real OFP)
        out += bline(" TO              LAT    LONG     TC  MC  MH  DIST")
        out += bline(" IDENT")
        out += bsep()

        for i, f in enumerate(oceanic_fixes):
            flat_f = safe_float(get_f(f, "pos_lat"))
            flon_f = safe_float(get_f(f, "pos_long"))
            mc_raw = get_f(f, "track_mag")
            mh_raw = get_f(f, "heading_mag")
            dist_raw = get_f(f, "distance")
            name  = (get_f(f, "name")  or get_f(f, "ident") or "").upper().strip()
            ident = (get_f(f, "ident") or "").upper().strip()

            lat_s = fmt_lat(flat_f)
            lon_s = fmt_lon(flon_f)

            # TC — great-circle bearing to next fix; "---" for last
            is_last = (i + 1 == len(oceanic_fixes))
            if not is_last:
                nf    = oceanic_fixes[i + 1]
                nlat  = safe_float(get_f(nf, "pos_lat"))
                nlon  = safe_float(get_f(nf, "pos_long"))
                tc_v  = _gc_bearing(flat_f, flon_f, nlat, nlon)
                tc_s  = f"{tc_v:03d}" if tc_v is not None else "---"
            else:
                tc_s = "---"

            # MC and MH from navlog
            try:
                mc_s = f"{int(round(float(mc_raw))):03d}"
            except Exception:
                mc_s = "---"
            try:
                mh_s = f"{int(round(float(mh_raw))):03d}"
            except Exception:
                mh_s = "---"

            # DIST — 4-digit zero-padded; "----" for last fix
            if is_last:
                dist_s = "----"
            else:
                try:
                    dist_s = f"{int(round(float(dist_raw))):04d}"
                except Exception:
                    dist_s = "----"

            # If last fix, suppress TC/MC/MH too
            if is_last:
                tc_s = mc_s = mh_s = "---"

            # Data row
            out += bline(f" {name:<16} {lat_s:<6} {lon_s:<7}  {tc_s}  {mc_s} {mh_s} {dist_s}")

            # IDENT line — only when ident differs from name (e.g. H5220 ≠ N5230W02000)
            if ident and ident != name:
                out += bline(f" {ident}")
            else:
                out += bline()

            out += bline()  # blank separator row between fixes

        out += bsep()
        return out

    except Exception as e:
        import traceback
        print(f"Error generating oceanic route verification: {e}")
        traceback.print_exc()
        return ""



def format_coord_aviation(coord_str, is_lon=False):
    """
    Format a decimal degree coordinate into compact aviation style (legacy helper).
    Lat:  N3444.6  or  S0327.1
    Lon:  W12035.1 or  E01423.8
    """
    if not coord_str:
        return "-------"
    try:
        val = float(coord_str)
        if is_lon:
            hem = "W" if val < 0 else "E"
            deg = int(abs(val))
            mins = (abs(val) - deg) * 60
            return f"{hem}{deg:03d}{mins:04.1f}"
        else:
            hem = "N" if val >= 0 else "S"
            deg = int(abs(val))
            mins = (abs(val) - deg) * 60
            return f"{hem}{deg:02d}{mins:04.1f}"
    except Exception:
        return "-------"


def format_time_hhmm(seconds_str):
    """Convert seconds to HH:MM format"""
    if not seconds_str:
        return "00:00"
    try:
        total_seconds = int(seconds_str)
        minutes = total_seconds // 60
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"
    except:
        return "00:00"


def format_coord(coord_str, width):
    """Format coordinate to specified width, removing decimal point (legacy helper)"""
    if not coord_str:
        return "0" * width
    try:
        val = abs(float(coord_str))
        formatted = f"{val:.10f}".replace('.', '')[:width]
        return formatted.ljust(width, '0')
    except:
        return "0" * width


def get_suitability_window(etops_section, icao_code):
    """Extract suitability window for airport from suitable_airport elements"""
    if not icao_code:
        return ""
    try:
        suitable_airports = etops_section.findall("suitable_airport")
        for apt in suitable_airports:
            if apt.findtext("icao_code") == icao_code:
                start = apt.findtext("suitability_start", "")
                end = apt.findtext("suitability_end", "")
                if start and end:
                    from datetime import datetime
                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                    return f"{start_dt.strftime('%H%M')}-{end_dt.strftime('%H%M')}Z"
        return ""
    except:
        return ""
    
def write_navigation_log(root, flight_info, takeoff_time, fixes_per_page=None):
    """Navigation log: reformatted to match TIME DIST FUEL layout (Jeppesen style)."""

    # Column grid — shared by the header and both data lines of every fix.
    C_WPT, C_TIME, C_DIST, C_IAS, C_MCH = 0, 16, 22, 28, 33
    C_MC, C_ALT, C_WIND, C_FUEL, C_SCORE = 38, 43, 48, 56, 64

    def navlog_header():
        return "\n".join([
            _cols((C_TIME, "TIME"), (C_DIST, "DIST"), (C_FUEL, "FUEL")),
            _cols((C_TIME, "LEG"), (C_DIST, "LEG"), (C_IAS, "IAS"), (C_MCH, "MCH"),
                  (C_MC, "MC"), (C_ALT, "ALT", 4, ">"), (C_WIND, "WIND"),
                  (C_FUEL, "LEG", 6, ">"), (C_SCORE, "SCORE")),
            _cols((C_WPT, "WAYPOINT"), (C_TIME, "TOTAL"), (C_DIST, "TOTAL", 5, ">"),
                  (C_IAS, "TAS"), (C_MCH, "G/S"), (C_MC, "HDG"), (C_ALT, "IOAT", 4, ">"),
                  (C_WIND, "ISA"), (C_FUEL, "TOTAL", 6, ">"), (C_SCORE, "TIME/FUEL")),
            "", "",
        ])

    def safe_float(val, default=0.0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def get_text(elem, tag):
        n = elem.find(tag)
        return n.text.strip() if n is not None and n.text else ""

    def format_time(sec):
        sec = safe_float(sec)
        mins = round(sec / 60)
        return f"{int(mins // 60):02d}.{int(mins % 60):02d}"

    def format_fuel(fuel):
        # Fuel is already in pounds, just format it
        return f"{int(round(safe_float(fuel))):>3}"

    def format_wind(wd, ws):
        # No wind data at all (e.g. the departure fix) reads better as dashes
        # than as a spurious "000000" calm.
        if not wd and not ws:
            return "------"
        wd_val = int(safe_float(wd)) if wd else 0
        ws_val = int(safe_float(ws)) if ws else 0
        return f"{wd_val:03d}{ws_val:03d}"

    def num3(val):
        """3-char right-justified integer, or '---' when absent/zero."""
        try:
            v = int(round(float(val)))
        except (TypeError, ValueError):
            return "---"
        return f"{v:>3}" if v else "---"

    def deg3(val):
        """Zero-padded 3-digit bearing (008, 089, 141), or '---'."""
        try:
            return f"{int(round(float(val))) % 360:03d}"
        except (TypeError, ValueError):
            return "---"

    def format_isa(val):
        val = safe_float(val, None)
        if val is None:
            return "---"
        prefix = 'P' if val >= 0 else 'M'
        return f"{prefix}{abs(int(val)):02d}"

    fixes = root.findall("navlog/fix")

    # Calculate cumulatives
    cumulative_dist = []
    cumulative_time = []

    run_dist = 0
    run_time = 0

    for f in fixes:
        run_dist += safe_float(get_text(f, "distance"))
        run_time += safe_float(get_text(f, "time_leg"))
        cumulative_dist.append(run_dist)
        cumulative_time.append(run_time)

    nav_log = navlog_header()

    for idx, f in enumerate(fixes):
        ident = (get_text(f, "ident") or "").upper()
        name_raw = (get_text(f, "name") or "XXXX").upper()
        name = ident if ident else name_raw

        # Rename waypoints
        if "TOP OF DESCENT" in name_raw:
            name = "TOD"
        elif "TOP OF CLIMB" in name_raw:
            name = "TOC"

        # Limit to 14 characters
        name = name[:14]

        # Get all values
        leg_time = format_time(get_text(f, "time_leg"))
        leg_dist = int(safe_float(get_text(f, "distance")))

        ias_str = num3(get_text(f, "ind_airspeed"))

        # Mach formatting (.52 format)
        mach_raw = get_text(f, "mach")
        try:
            mach_val = float(mach_raw)
            mach_str = f".{int(mach_val * 100):02d}" if mach_val > 0 else "---"
        except (TypeError, ValueError):
            mach_str = "---"

        mc_str = deg3(get_text(f, "track_mag"))

        alt_feet = safe_float(get_text(f, "altitude_feet"))
        alt_str = f"{int(alt_feet // 100):>3}" if alt_feet else "---"

        wind_str = format_wind(get_text(f, "wind_dir"), get_text(f, "wind_spd"))

        # LEG fuel is segment burn (fuel_leg)
        leg_fuel = format_fuel(get_text(f, "fuel_leg"))

        total_time = format_time(cumulative_time[idx])
        total_dist = int(cumulative_dist[idx])

        # TOTAL fuel is fuel remaining (from fuel_plan_onboard)
        total_fuel = format_fuel(get_text(f, "fuel_plan_onboard"))

        tas_str = num3(get_text(f, "true_airspeed"))
        gs_str  = num3(get_text(f, "groundspeed"))
        hdg_str = deg3(get_text(f, "heading_mag"))

        ioat = get_text(f, "oat")
        ioat_str = ioat.rjust(3) if ioat else "---"

        isa_dev = format_isa(get_text(f, "oat_isa_dev"))

        # Get frequency if available
        freq = get_text(f, "frequency")
        freq_str = freq if freq else ""

        # Line 1: leg values.  Line 2: cumulative / secondary values.
        nav_log += _cols(
            (C_WPT, name, 15), (C_TIME, leg_time, 5), (C_DIST, leg_dist, 5, ">"),
            (C_IAS, ias_str, 3, ">"), (C_MCH, mach_str, 3, ">"),
            (C_MC, mc_str, 3, ">"), (C_ALT, alt_str, 4, ">"),
            (C_WIND, wind_str, 6), (C_FUEL, leg_fuel, 6, ">"),
            (C_SCORE, "..../...."),
        ) + "\n"
        nav_log += _cols(
            (C_WPT, freq_str, 15), (C_TIME, total_time, 5),
            (C_DIST, total_dist, 5, ">"), (C_IAS, tas_str, 3, ">"),
            (C_MCH, gs_str, 3, ">"), (C_MC, hdg_str, 3, ">"),
            (C_ALT, ioat_str, 4, ">"), (C_WIND, isa_dev, 3),
            (C_FUEL, total_fuel, 6, ">"),
        ) + "\n\n"

    return nav_log


import textwrap
from collections import OrderedDict




def write_forecast_winds(root,
                         orig_metar=None, orig_taf=None, orig_atis=None,
                         dest_metar=None, dest_taf=None, dest_atis=None,
                         altn_metar=None, altn_taf=None, altn_atis=None,
                         toaltn_metar=None, toaltn_taf=None, toaltn_atis=None,
                         eualtn_metar=None, eualtn_taf=None, eualtn_atis=None,
                         etops_metar=None, etops_taf=None, etops_atis=None):
    """
    Returns (winds_text, weather_notam_text, images_text).
    - winds_text:        forecast winds table (plain text, portrait)
    - weather_notam_text: METAR/TAF/SIGMET formatted like NOTAMs (landscape two-col)
    - images_text:       weather chart image markers for appending after NOTAMs
    """
    BW = 78

    def get_text_local(path, default="NIL"):
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else default

    # ── Forecast winds table ──────────────────────────────────────────────────
    winds = ""
    winds += "\nFORECAST WINDS AND TEMP SUMMARY\n"
    target_altitudes = [10000, 24000, 30000, 34000, 39000]
    col_width = 12

    def pad_wind(s):
        try:
            return f"{int(s):03d}"
        except (ValueError, TypeError):
            return "---"

    def wind_table_header():
        hdr = "        " + "".join(f"{alt:<{col_width}}" for alt in target_altitudes)
        return hdr + "\n" + "-"*68 + "\n"

    from collections import OrderedDict
    fix_idents = []
    altitude_wind_table = {alt: OrderedDict() for alt in target_altitudes}
    for fix in root.findall("navlog/fix"):
        fix_ident = fix.findtext("ident")
        if not fix_ident:
            continue
        wind_data_section = fix.find("wind_data")
        if wind_data_section is None:
            continue
        fix_idents.append(fix_ident)
        wind_levels = {
            int(lvl.findtext("altitude")): (
                lvl.findtext("wind_dir", "---"),
                lvl.findtext("wind_spd", "---"),
                lvl.findtext("oat", "---"),
            )
            for lvl in wind_data_section.findall("level")
            if lvl.findtext("altitude", "").isdigit()
        }
        for alt in target_altitudes:
            altitude_wind_table[alt][fix_ident] = wind_levels.get(alt, ("---", "---", "---"))

    winds += wind_table_header()
    for fix in fix_idents:
        winds += f"{fix:<8}"
        for alt in target_altitudes:
            wind_dir, wind_spd, oat = altitude_wind_table[alt].get(fix, ("---", "---", "---"))
            cell = f"{pad_wind(wind_dir)}/{pad_wind(wind_spd)}{oat.rjust(3)}"
            winds += f"{cell:<{col_width}}"
        winds += "\n"

    # ── Pull weather directly from airport XML sections ───────────────────────
    NETWORK_PRIORITY = {"real-world": 0, "pilotedge": 1, "vatsim": 2, "ivao": 3}

    def _from_section(section_tag):
        sec = root.find(section_tag)
        if sec is None:
            return ("", "", "", "", "", "", "", "", "")
        icao  = (sec.findtext("icao_code") or "").strip().upper()
        iata  = (sec.findtext("iata_code") or "").strip().upper()
        name  = (sec.findtext("name")      or "").strip()
        metar = (sec.findtext("metar")     or "").strip()
        taf   = (sec.findtext("taf")       or "").strip()
        runways_str = _get_runways(root, sec)
        def _fmt_wx_time(iso):
            if not iso:
                return ""
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return f"{dt.day:02d} {dt.strftime('%H:%M')}"
            except Exception:
                return ""
        taf_t   = (sec.findtext("taf_time")   or "").strip()
        metar_t = (sec.findtext("metar_time") or "").strip()
        t_start = _fmt_wx_time(taf_t   or metar_t)
        t_end   = _fmt_wx_time(metar_t or taf_t)
        wx_window = f"{t_start} - {t_end}" if t_start and t_end and t_start != t_end else t_start
        best = {}
        for atis_el in sec.findall("atis"):
            network = (atis_el.findtext("network") or "").strip().lower()
            # SimBrief may use: type, atis_type, or positional (first=DEP)
            raw_type = (atis_el.findtext("type") or
                        atis_el.findtext("atis_type") or "DEP").strip().upper()
            # Normalise: anything containing ARR maps to ARR, else DEP
            atype = "ARR" if "ARR" in raw_type else "DEP"
            msg   = (atis_el.findtext("message") or
                     atis_el.findtext("text")    or
                     atis_el.text               or "")
            msg = (msg or "").strip()
            if not msg:
                continue
            pri = NETWORK_PRIORITY.get(network, 99)
            if atype not in best or pri < best[atype][0]:
                best[atype] = (pri, msg)
        dep_atis = best.get("DEP", (0, ""))[1]
        arr_atis = best.get("ARR", (0, ""))[1]
        # Final fallback: flat <atis> text directly on the section element
        if not dep_atis and not arr_atis:
            flat = (sec.findtext("atis") or "").strip()
            if flat:
                dep_atis = flat
        return icao, iata, name, metar, taf, dep_atis, arr_atis, runways_str, wx_window

    # Map section tag → role label + fallback weather from passed-in args
    SECTIONS = [
        ("origin",            "DEPARTURE",    orig_metar,   orig_taf,   orig_atis),
        ("destination",       "DESTINATION",  dest_metar,   dest_taf,   dest_atis),
        ("alternate",         "ALTN 1",       altn_metar,   altn_taf,   altn_atis),
        ("takeoff_alternate", "TO ALTN",      toaltn_metar, toaltn_taf, toaltn_atis),
        ("eu_alternate",      "EU ALTN",      eualtn_metar, eualtn_taf, eualtn_atis),
        ("etops_alternate",   "ETOPS ALTN",   etops_metar,  etops_taf,  etops_atis),
    ]

    def _apt_header(icao, iata, name, role, runways_str="", wx_window=""):
        role_field = f"{role}  {wx_window}" if wx_window else role
        iata_name  = f"{iata} - {name}" if iata and name else name
        # Encode: ICAO   role_field    iata_name (4-space separator)
        # Parser splits on 4+ spaces: rm[0]=role_field (->role param->RIGHT display)
        #                              rm[1]=iata_name (->iata_name param->LEFT display)
        if role_field:
            left_part = f"{icao}   {role_field}"
            line1     = f"{left_part:<46}    {iata_name}"
        else:
            # FIR: just ICAO   name (no double-space, parser detects as FIR)
            line1 = f"{icao}   {iata_name}"
        indent     = " " * (len(icao) + 3)
        out = f"\n{'═' * BW}\n{line1}\n{indent}IA\n"
        if runways_str:
            rwy_parts = runways_str.split()
            lines_rwy = []
            for j in range(0, len(rwy_parts), 2):
                lines_rwy.append(" ".join(rwy_parts[j:j+2]))
            out += f"{indent}RWYS: {lines_rwy[0]}\n"
            for extra in lines_rwy[1:]:
                out += f"{indent}      {extra}\n"
        out += f"{'═' * BW}\n"
        return out

    def _cat_banner(label):
        inner = f" {label} "
        pad   = BW - len(inner)
        return f"{'═' * (pad//2)}{inner}{'═' * (pad - pad//2)}\n"

    def _render_wx(text, label="", icao=""):
        if not text or text.strip() in ("", "NIL"):
            nil_msg = f"THERE ARE NO ACTIVE {label} FOR AIRPORT {icao} WITHIN THE GIVEN TIME PERIOD." if (label and icao) else ""
            return f"[NIL_WX {nil_msg}]\n"
        HANG = "     "
        out = []
        for raw_line in text.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            wrapped = textwrap.wrap(raw_line, width=BW)
            if not wrapped:
                continue
            out.append(wrapped[0])
            for cont in wrapped[1:]:
                out.append(HANG + cont)
        return "\n".join(out) + "\n"

    def _render_atis_block(msg, icao=""):
        out = _cat_banner("ATIS")
        if not msg or not msg.strip():
            nil_msg = f"THERE ARE NO ACTIVE ATIS FOR AIRPORT {icao} WITHIN THE GIVEN TIME PERIOD." if icao else ""
            out += f"[NIL_WX {nil_msg}]\n"
            return out
        HANG = "     "
        for raw_line in msg.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            # Use full column width for ATIS (wider than BW text-gen width)
            wrapped = textwrap.wrap(raw_line, width=BW + 10)
            if wrapped:
                out += wrapped[0] + "\n"
                for cont in wrapped[1:]:
                    out += HANG + cont + "\n"
        return out

    def _render_station(icao, iata, name, role, metar, taf, dep_atis, arr_atis,
                        runways_str="", wx_window=""):
        # Skip the entire block if the station doesn't exist in the flight plan
        if not icao:
            return ""
        has_any = any(v and v.strip() for v in (metar, taf, dep_atis, arr_atis))
        if not has_any:
            return ""
        s = _apt_header(icao, iata, name, role, runways_str, wx_window)
        # TAF
        s += _cat_banner("TAF")
        s += _render_wx(taf, label="TAF", icao=icao)
        # METAR
        s += _cat_banner("METAR")
        s += _render_wx(metar, label="METAR", icao=icao)
        # ATIS — only show banner if station has some wx data
        atis_combined = ""
        if dep_atis and dep_atis.strip():
            atis_combined += dep_atis.strip()
        if arr_atis and arr_atis.strip() and arr_atis != dep_atis:
            atis_combined += ("\n" if atis_combined else "") + arr_atis.strip()
        s += _render_atis_block(atis_combined, icao=icao)
        return s

    # ── Build weather section in NOTAM-compatible token format ───────────────
    wx = ""
    for section_tag, role, fb_metar, fb_taf, fb_atis in SECTIONS:
        icao, iata, name, metar, taf, dep_atis, arr_atis, runways_str, wx_window = _from_section(section_tag)
        metar    = metar    or fb_metar or ""
        taf      = taf      or fb_taf   or ""
        # Merge fallback ATIS: XML <atis> elements take priority,
        # but if none found, use the flat fallback string as dep ATIS
        if not dep_atis and not arr_atis and fb_atis:
            dep_atis = fb_atis
        wx += _render_station(icao, iata, name, role, metar, taf, dep_atis, arr_atis,
                              runways_str, wx_window)

    # ── SIGMETs — build FIR list from navlog, then populate with any active SIGMETs ──
    # 1. Walk navlog/fix elements to get ordered, deduplicated list of FIRs on route
    from collections import OrderedDict
    route_firs = OrderedDict()   # fir_code -> fir_name (populated from sigmets if available)
    for fix in root.findall("navlog/fix"):
        fcode = (fix.findtext("fir") or "").strip().upper()
        if fcode and fcode not in route_firs:
            route_firs[fcode] = ""   # name filled in below

    # 2. Index all active sigmets by FIR code, and collect FIR names
    sigmet_map = OrderedDict()   # fir_code -> list of sigmet elements
    _all_sigs = root.findall("weather/sigmets/sigmet")
    print(f"DEBUG SIGMET COUNT: {len(_all_sigs)} sigmets found at weather/sigmets/sigmet")
    if not _all_sigs:
        # Try without weather/ prefix in case root is already at <weather>
        _all_sigs = root.findall("sigmets/sigmet")
        print(f"DEBUG SIGMET COUNT (no weather/ prefix): {len(_all_sigs)}")
    for sig in _all_sigs:
        fcode = (sig.findtext("fir") or "").strip().upper()
        fname = (sig.findtext("fir_name") or "").strip()
        if not fcode:
            continue
        if fcode not in sigmet_map:
            sigmet_map[fcode] = []
        sigmet_map[fcode].append(sig)
        # Back-fill FIR name wherever we can
        if fname:
            route_firs[fcode] = fname   # update name from sigmet data
        if fcode not in route_firs:
            route_firs[fcode] = fname   # FIR in sigmets but not on navlog — add it

    # 3. Render one block per FIR
    if route_firs:
        HANG = "     "
        for fcode, fname in route_firs.items():
            display_name = fname or f"{fcode} FIR/UIR"
            wx += _apt_header(fcode, "", display_name, "", "", "")
            wx += _cat_banner("SIGMET")
            sigs = sigmet_map.get(fcode, [])
            if sigs:
                for sig in sigs:
                    sig_id = (sig.findtext("id")     or "").strip()
                    hazard = (sig.findtext("hazard") or "").strip()
                    text   = (sig.findtext("text")   or "").strip()
                    hdr    = f"SIGMET {sig_id} ({hazard})" if sig_id else "SIGMET"
                    wx += hdr + "\n"
                    if text:
                        # Strip leading/trailing whitespace including internal newlines
                        text = " ".join(text.split())
                        wrapped = textwrap.wrap(text, width=BW)
                        wx += wrapped[0] + "\n"
                        for cont in wrapped[1:]:
                            wx += HANG + cont + "\n"
                    wx += "\n"
            else:
                wx += "[NIL_SIGMET]\n"

    # ── Images ───────────────────────────────────────────────────────────────
    images_text = ""
    images_section = root.find("images")
    if images_section is not None:
        base_dir = images_section.findtext("directory", "").strip()
        maps     = images_section.findall("map")
        if maps:
            for map_elem in maps:
                map_name = (map_elem.findtext("name") or "Image").strip()
                link     = (map_elem.findtext("link") or "").strip()
                if not link:
                    continue
                url = base_dir.rstrip("/") + "/" + link.lstrip("/") if base_dir else link
                images_text += f"{map_name}\n[IMAGE:{url}]\n\n"
            images_text += "\n[PAGEBREAK]\n"

    return winds, wx, images_text

def write_takeoff_performance_string(flight_info, valid_runways, anti_ice_on, 
                                     runway_lines, airport_altitudes=None, max_elevation=0, icao_code="XXXX",
                                     xml_root=None):
    """
    Returns the takeoff performance section as a string, including TAKEOFF DATA, 
    runway table, airport analysis data, and optional airport notes. 
    
    Weights/fuel are divided by 1000 with 1 decimal, flex temp gets C, ALT gets ft.
    """
    try:
        # Aircraft UI name mapping - add registrations here
        AIRCRAFT_UI_NAMES = {
            "N123NA": {"name": "A319 CFM", "engine": "CFM56-5B5"},
        }
        
        # Special airports requiring MAX-SPCL designation
        SPECIAL_AIRPORTS = {
            "KSNA", "MROC", "KEGE", "KJAC", "KGUC", "KDRO",
            "PAJN", "PAWG", "PAPG", "MHTG", "TNCM", "TIST", "KEYW", "KASE"
        }
        
        # Airbus aircraft detection
        AIRBUS_TYPES = {'A318', 'A319', 'A320', 'A20N', 'A321', 'A21N', 
                       'A332', 'A333', 'A339', 'A346'}
        is_airbus = icao_code.upper() in AIRBUS_TYPES
        
        # Set labels based on aircraft type
        if is_airbus:
            flap_label = "CONF"
            ac_label = "APU"
        else:
            flap_label = "FLAP"
            ac_label = "BLD"
        
        output = ""
        
        # ===================================================================
        # AIRCRAFT TYPE DETECTION
        # ===================================================================
        icaocode = icao_code.upper().replace('-', '').replace(' ', '')
        is_737_ng  = icaocode in ['B736', 'B737', 'B738', 'B739']
        is_737_max = icaocode == 'B38M'
        is_boeing_737 = is_737_ng or is_737_max
        is_md8x = icaocode.startswith('MD8')   # MD80, MD83, MD88, MD87 etc.
        
        # Extract and format basic flight data
        if valid_runways:
            first_runway = valid_runways[0]
            aircraft_reg = first_runway.get('fin', 'N/A')
            
            # Get aircraft display name and engine type
            if aircraft_reg in AIRCRAFT_UI_NAMES:
                aircraft_display = AIRCRAFT_UI_NAMES[aircraft_reg]["name"]
                engine_type = AIRCRAFT_UI_NAMES[aircraft_reg]["engine"]
                print(f"DEBUG: Using custom name for {aircraft_reg}: {aircraft_display}")
            else:
                aircraft_display = first_runway.get('aircraft', 'UNKNOWN')
                engine_type = first_runway.get('engine', 'UNKNOWN')
                print(f"DEBUG: Aircraft {aircraft_reg} not in mapping, using XML: {aircraft_display}")
            
            # Extract flight parameters
            sta = first_runway.get('airport', 'ERR')
            pres = first_runway.get('qnh', 'ERR')
            alt = first_runway.get('elevation', 'ERR')
            flt_dte = first_runway.get('flight_number', 'ERR')
            airpl = first_runway.get('fin', 'ERR')
            dte_time = first_runway.get('dte_time', 'ERR')
            surface = first_runway.get('surface_condition', 'ERR').upper()
            temp = first_runway.get('temp', 'ERR')
            
            # Convert weights and fuel
            # ATOW (Assumed Takeoff Weight) = PTOW + 2.0 (2,000 lb margin),
            # capped at structural MTOW only — not runway-specific MTOW.
            est_tow = safe_weight(first_runway.get('est_tow', 0))

            try:
                _struct_mtow = safe_weight(float(xml_root.findtext('weights/max_tow_struct', '0') or 0))
            except Exception:
                _struct_mtow = None

            if isinstance(est_tow, float):
                atow = est_tow + 2.0
                if isinstance(_struct_mtow, float) and _struct_mtow > 0:
                    atow = min(atow, _struct_mtow)
            else:
                atow = est_tow
            est_zfw = safe_weight(first_runway.get('est_zfw', 0))
            plan_takeoff = safe_weight(first_runway.get('fuel', 0))
            taxi_fuel = safe_weight(first_runway.get('taxi_fuel', 0))
            weight_lbs = first_runway.get('est_tow', 0)  # Keep in lbs for lookups
        else:
            # Fallback values if no runways
            engine_type = aircraft_display = sta = pres = alt = 'ERR'
            flt_dte = airpl = dte_time = surface = temp = 'ERR'
            est_tow = atow = est_zfw = plan_takeoff = taxi_fuel = 'ERR'
            weight_lbs = 0
        
        # ===================================================================
        # ALTITUDE & BLEED ADJUSTMENT
        # ===================================================================
        alt_val = 0
        if alt != 'ERR':
            try:
                alt_val = float(alt)
            except (ValueError, TypeError):
                alt_val = 0
        
        if alt_val <= 8000:
            pack_off_adj = 0.8
        elif alt_val <= 9000:
            pack_off_adj = 0.9
        else:
            pack_off_adj = 1.0
        
        # ===================================================================
        # THRUST TABLE (for Boeing 737)
        # ===================================================================
        THRUST_TABLE = {
            "B736": {"TO": 22, "TO1": 20, "TO2": 18},
            "B737": {"TO": 24, "TO1": 22, "TO2": 20},
            "B738": {"TO": 26, "TO1": 24, "TO2": 22},
            "B739": {"TO": 27, "TO1": 25, "TO2": 23},
            "B38M": {"TO": 26, "TO1": 24, "TO2": 22},
        }
        
        rwy = valid_runways[0] if valid_runways else {}
        derate_label = rwy.get('thr', '').upper().strip()
        
        effective_thrust = None
        thrust_label = "N/A"
        
        if is_boeing_737 and icaocode in THRUST_TABLE:
            # Normalise key: SimBrief sends TO/TO1/TO2 or D-TO/D-TO1/D-TO2
            # Strip D- prefix so lookup works for both variants
            key = derate_label.lstrip("D-") if derate_label.startswith("D-") else derate_label

            effective_thrust = THRUST_TABLE[icaocode].get(key) \
                            or list(THRUST_TABLE[icaocode].values())[0]

            # B738: show rated thrust in lbs (26K/24K/22K)
            # All others: show setting as-is (TO / TO1 / TO2)
            if icaocode == "B738":
                thrust_label = f"{effective_thrust}K" if effective_thrust is not None else key
            else:
                thrust_label = key or "TO"
        
        # ===================================================================
        # GLOBAL THRUST/SPEED DATA (for header section)
        # ===================================================================
        n1_pack_on = "XXX"
        n1_pack_off = "XXX"
        epr_max = "XXX"
        speed_data_dict = None
        
        # === TAKEOFF DATA PAGE ===
        output += "\n[PAGEBREAK]\n[TPS_START]\n"
        output += f"{'STA':<6} {'PRES ALT':<11} {'FLT':<8} {'AIRPL':<9} {'DTE/TIME':<10}\n"
        # Format pressure alt as integer (no .0)
        try:
            alt_disp = str(int(float(alt))) if alt not in ('ERR', '') else alt
        except Exception:
            alt_disp = alt
        output += f"{sta:<6} {alt_disp:<10}  {flt_dte:<8} {airpl:<9} {dte_time:<10}\n\n"
        _ai_label = ""
        if is_boeing_737 and thrust_label and thrust_label != "N/A":
            output += f"*** {engine_type} {thrust_label} {surface} ***\n\n"
        else:
            output += f"*** {engine_type} {surface} ***\n\n"
        
        # Weight and fuel data
        output += f"{'TEMP':<6} {'PTOW':>6} {'ATOW':>6} {'ZFW':>6} {'FUEL':>7} {'TXI FUEL':>8}\n"
        
        # Format each value with 1 decimal if float
        est_tow_str = f"{est_tow:.1f}" if isinstance(est_tow, float) else 'ERR'
        atow_str = f"{atow:.1f}" if isinstance(atow, float) else 'ERR'
        est_zfw_str = f"{est_zfw:.1f}" if isinstance(est_zfw, float) else 'ERR'
        plan_takeoff_str = f"{plan_takeoff:.1f}" if isinstance(plan_takeoff, float) else 'ERR'
        taxi_fuel_str = f"{taxi_fuel:.1f}" if isinstance(taxi_fuel, float) else 'ERR'
        
        output += f"{temp}C    {est_tow_str:>7}{atow_str:>7} {est_zfw_str:>7} "
        output += f"{plan_takeoff_str + 'P':>6}{taxi_fuel_str:>5}\n\n"
        
        # === THRUST / V-SPEED SECTION ===
        banner = "************* THRUST / V-SPEED ****************************"
        output += banner + "\n\n"
        if anti_ice_on:
            output += "  *****************\n"
            output += "   * ANTI-ICE ON *\n"
            output += "  *****************\n\n"
        
        # --- Use ICAO passed from caller ---
        print(f"[DEBUG] ICAO from parameter: {icao_code}")
        print(f"[DEBUG] Final ICAO for speed lookup: {icaocode}")
        
        # --- Weight setup ---
        print(f"[DEBUG] Weight for speed lookup: {weight_lbs} lbs")
        
        # --- Custom speed data (check if available first) ---
        if is_boeing_737:
            # Boeing uses OAT and altitude for N1 lookup
            speed_data_dict = get_speed_other(icaocode, oat=temp, altitude=alt)
            print(f"[DEBUG] N1 lookup with OAT={temp}, ALT={alt}: {speed_data_dict}")
            
            n1_pack_on = speed_data_dict.get('n1', 'XXX') if speed_data_dict else "XXX"
            if n1_pack_on != "XXX":
                try:
                    n1_pack_off = round(float(n1_pack_on) + pack_off_adj, 1)
                except (ValueError, TypeError):
                    n1_pack_off = "XXX"
        elif is_md8x:
            # MD-80 series (MD83/MD88 etc.): Get MAX EPR and speed data
            epr_max_data = get_speed_other(icaocode, oat=temp, altitude=alt_val)
            if epr_max_data and 'epr' in epr_max_data:
                epr_max = epr_max_data['epr']
            
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            print(f"[DEBUG] MD83 EPR/speed lookup: {speed_data_dict}")
        else:
            # Other aircraft use weight
            speed_data_dict = get_speed_other(icaocode, weight=weight_lbs)
            print(f"[DEBUG] Custom speed data lookup result: {speed_data_dict}")
        
        # --- MD-80 VsR/VMM SPEEDS ---
        if is_md8x and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            vsr_val = speed_data_dict['speed'].get('VsR', 'XXX')
            vmm_val = speed_data_dict['speed'].get('VMM', 'XXX')
            
            # Get CG from speed_data_dict if available
            cg_display = speed_data_dict.get('cg', 'XX.X')
            
            output += f"      TOW CG  O/RET    MM\n"
            output += f"       {cg_display:<6} {vsr_val:<7} {vmm_val}\n\n"

        # --- AIRBUS F/S/GRN DOT (A318/A319/A320 ceo+neo, A321 ceo+neo) ---
        elif icaocode in ['A318', 'A319', 'A320', 'A321', 'A20N', 'A21N'] and speed_data_dict and isinstance(speed_data_dict.get('speed'), dict):
            # Get CG and trim from speed_data_dict if available
            cg_display = speed_data_dict.get('cg', 'XX.X')
            trim_display = speed_data_dict.get('trim', 'X.X')
            
            # First line: TOW CG and STAB header
            if trim_display and trim_display != 'X.X':
                output += f"      TOW CG       STAB\n"
                output += f"       {cg_display:<10} {trim_display}\n"
            else:
                output += f"      TOW CG\n"
                output += f"       {cg_display}\n"
            
            # Second section: F/S/GRN DOT speeds
            f_val = speed_data_dict['speed'].get('F', 'XXX')
            s_val = speed_data_dict['speed'].get('S', 'XXX')
            gd_val = speed_data_dict['speed'].get('GRN DOT', 'XXX')
            output += f"       F     S    GRN DOT\n"
            output += f"      {f_val:<5} {s_val:<5} {gd_val:^8}\n\n"

        # --- BOEING N1 ---
        elif speed_data_dict and 'n1' in speed_data_dict:
            # Display N1 only (no CG/trim in flight plan)
            output += f"         *MAX* N1\n"
            output += f"      BLD ON  {n1_pack_on}\n"
            output += f"      BLD OFF {n1_pack_off}\n\n"
        
        # --- OTHER AIRCRAFT (E-Jets, etc.) ---
        elif speed_data_dict and 'name' in speed_data_dict and 'speed' in speed_data_dict:
            output += f"      {speed_data_dict['name']} {speed_data_dict['speed']}\n\n"
        
        # ===================================================================
        # RUNWAY TABLE WITH PER-RUNWAY N1/EPR
        # ===================================================================
        
        # Determine THR column label based on aircraft type
        if is_md8x:
            thr_column_label = "EPR"
        elif is_boeing_737:
            thr_column_label = "N1"
        else:
            thr_column_label = "THR"
        
        output += f"{'RWY':<5} {flap_label:<5} {ac_label:<4} {'V1':>3} {'VR':>3} {'V2':>3}   {thr_column_label:<7} {'AT':<8}   {'MTOW':<6}\n"
        output += "-" * 58 + "\n"
        
        # Get airport code for special airport check
        airport_code = sta.strip().upper() if sta != 'ERR' else ""
        print(f"[DEBUG] Airport code for special airport check: {airport_code}")
        
        # Get OAT for temperature comparison
        oat = None
        try:
            oat = float(temp)
        except (ValueError, TypeError):
            pass

        # ===================================================================
        # WEIGHT OVERRIDE LIMITS  (computed once, applied per runway)
        # ===================================================================
        #
        # L — Landing Weight Limit
        #   The maximum TOW that will not cause the estimated landing weight
        #   to exceed the destination MLW.
        #
        #   Formula:  L_limit = MLW_dest + enroute_fuel_burn
        #
        #   Where:
        #     MLW_dest       = weights/max_ldw   (lbs, from SimBrief)
        #     enroute_burn   = fuel/enroute_burn  (lbs, trip fuel consumed
        #                       from brake release to touchdown)
        #
        #   If atow > L_limit → cap mtow at L_limit, suffix = 'L'
        #   This overrides SimBrief's limit_code regardless of what it sends.
        #
        # E — Enroute Limit  (placeholder — logic to be added)
        #   Reserved for dispatcher enroute constraints (driftdown, ETOPS,
        #   depressurisation, fuel). Will override S/T/L when applicable.
        #   suffix = 'E'
        # ===================================================================

        _l_limit_tow = None   # L limit in thousands lbs — set when PTOW - burn hits MLW ceiling
        _e_limit_tow = None   # E limit in thousands lbs (placeholder)

        if xml_root is not None:
            try:
                _mlw_lbs     = float(xml_root.findtext('weights/max_ldw',   '0') or 0)
                _fbrn_lbs    = float(xml_root.findtext('fuel/enroute_burn', '0') or 0)
                _ptow_lbs    = est_tow * 1000.0 if isinstance(est_tow, float) else 0.0
                if _mlw_lbs > 0 and _fbrn_lbs > 0 and _ptow_lbs > 0:
                    _plw_lbs = _ptow_lbs - _fbrn_lbs
                    _mlw_ceil = _mlw_lbs * 1.01
                    if _plw_lbs >= _mlw_lbs:
                        # PTOW - burn is at or above MLW → L limited
                        # L_limit_TOW uses MLW×1.01 to give the 1% dispatch margin
                        _l_limit_tow = (_mlw_ceil + _fbrn_lbs) / 1000.0
                        print(f"[DEBUG L-LIMIT] PLW={_plw_lbs:.0f} ≥ MLW={_mlw_lbs:.0f} "
                              f"→ L_limit={_l_limit_tow:.1f}k lbs (MLW×1.01 + burn)")
                    else:
                        print(f"[DEBUG L-LIMIT] PLW={_plw_lbs:.0f} < MLW={_mlw_lbs:.0f} "
                              f"→ L override not applicable")
            except Exception as _e:
                print(f"[DEBUG L-LIMIT] Calculation failed: {_e}")

        # E limit — groundwork only, no override logic yet
        # _e_limit_tow = ...  (to be implemented)

        # ── First pass: collect all runway row data ──────────────────────────
        runway_rows = []
        for line in runway_lines:
            parts = line.split()
            rwy, flap, bld, v1, vr, v2, thr, at = (parts + [""] * 8)[:8]
            
            # If any of the v-speeds came through unsanitized, force XXX
            v1 = v1 if v1.isdigit() and int(v1) > 0 else "XXX"
            vr = vr if vr.isdigit() and int(vr) > 0 else "XXX"
            v2 = v2 if v2.isdigit() and int(v2) > 0 else "XXX"
            
            # For Airbus, invert bleed to APU logic (bleed ON = APU OFF)
            if is_airbus:
                apu_status = 'OFF' if bld.upper() == 'ON' else 'ON'
            else:
                apu_status = bld
            
            matched_rwy = next((r for r in valid_runways if r['id'] == rwy), None)
            mtow_val = None
            original_limit_code = ''
            rwy_message = ""
            if matched_rwy:
                max_w_raw  = matched_rwy.get('max_weight', 0)
                raw_code   = str(matched_rwy.get('limit_code', '') or '').strip().upper()
                rwy_message = str(matched_rwy.get('runway_message', '') or '').strip()

                _CODE_MAP = {
                    'T': 'T', 'F': 'T', 'S': 'S', 'A': 'S',
                    'L': 'L', 'D': 'D', 'E': 'E', 'X': 'X', '': '',
                }
                _lookup_code = raw_code.strip()
                display_code = _CODE_MAP.get(_lookup_code, _lookup_code[:1] if _lookup_code else '')
                original_limit_code = display_code
                print(f"[DEBUG MTOW] raw_code={raw_code!r} → display_code={display_code!r}")

                try:
                    mtow_val = safe_weight(max_w_raw)
                except Exception:
                    mtow_val = None

                if _l_limit_tow is not None and isinstance(mtow_val, float):
                    if _l_limit_tow < mtow_val:
                        _orig_mtow   = mtow_val
                        mtow_val     = round(_l_limit_tow, 1)
                        display_code = 'L'
                        print(f"[DEBUG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} < mtow={_orig_mtow:.1f} → override to L")
                    else:
                        print(f"[DEBUG L-LIMIT] {rwy}: L_limit={_l_limit_tow:.1f} ≥ mtow={mtow_val:.1f} → no L override")

                try:
                    mtow = f"{mtow_val:.1f}{display_code}" if isinstance(mtow_val, float) else ""
                except Exception:
                    mtow = ""
            else:
                mtow = ""

            _MSG_MAP = {
                'PTOW EXCEEDS MTOW ALL FLAPS':    'PTOW EXCEEDS MTOW ALL FLAPS',
                'PTOW EXCEEDS MTOW':              'PTOW EXCEEDS MTOW - RQST NEW TPS',
                'FLAP N/A':                       'FLAP N/A THIS RWY - RQST NEW TPS',
                'WIND ADJ':                       'WIND ADJM-CALL LOADS OR COMPUTE DATA',
                'WIND ADJM':                      'WIND ADJM-CALL LOADS OR COMPUTE DATA',
            }
            if rwy_message:
                for key, display in _MSG_MAP.items():
                    if key in rwy_message.upper():
                        rwy_message = display
                        break

            at_display = ""
            at_override_occurred = False
            at_numeric = None
            if at and "TOGA" not in str(at).upper():
                try:
                    temp_val = float(re.sub(r"[^0-9.-]", "", str(at)))
                    if temp_val < 100:
                        at_numeric = temp_val
                except (ValueError, TypeError):
                    pass

            _max_thrust_codes = {'T', 'S', 'A'}
            _code_requires_max_thrust = original_limit_code in _max_thrust_codes
            # L is a planning/dispatch limit only — never affects thrust or AT display
            _is_l_limited = (display_code == 'L')

            if _is_l_limited:
                # Pass SimBrief's AT straight through unchanged
                at_display = f"{int(at_numeric)}C" if at_numeric is not None else at
                at_override_occurred = False
            elif _code_requires_max_thrust and mtow_val and isinstance(atow, float) and atow > mtow_val:
                at_display = "MAX-WT"
                at_override_occurred = True
            elif "TOGA" in str(at).upper():
                at_display = "MAX-WT"
                at_override_occurred = True
            elif airport_code in SPECIAL_AIRPORTS:
                at_display = "MAX-SPCL"
                at_override_occurred = True
                print(f"[DEBUG] Special airport detected: {airport_code}")
            elif oat is not None and at_numeric is not None and abs(oat - at_numeric) <= 5:
                at_display = "MAX-TEMP"
                at_override_occurred = True
            elif at_numeric is not None:
                at_display = f"{int(at_numeric)}C"
            else:
                at_display = "MAX-WT"
                at_override_occurred = True

            thr_display = thr

            if is_md8x:
                if at_override_occurred:
                    thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)
                else:
                    if at_numeric is not None:
                        epr_takeoff_data = get_speed_other(
                            icaocode, oat=temp, altitude=alt_val, assumed_temp=int(at_numeric)
                        )
                        if epr_takeoff_data and 'epr' in epr_takeoff_data:
                            thr_display = f"{epr_takeoff_data['epr']:.2f}"
                        else:
                            thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)
                    else:
                        thr_display = f"{epr_max:.2f}" if isinstance(epr_max, (int, float)) else str(epr_max)

            elif is_boeing_737:
                if at_override_occurred:
                    thr_display = str(n1_pack_on)
                else:
                    if at_numeric is not None and effective_thrust is not None:
                        reduced_n1_data = get_reduced_thrust_n1(
                            icaocode, effective_thrust, int(at_numeric), alt_val
                        )
                        if reduced_n1_data and 'n1' in reduced_n1_data:
                            thr_display = str(reduced_n1_data['n1'])
                        else:
                            thr_display = str(n1_pack_on)
                    else:
                        thr_display = str(n1_pack_on)

            elif is_airbus:
                if at_override_occurred or at_display.startswith("MAX"):
                    thr_display = "TOGA"
                else:
                    thr_display = "FLEX"

            else:
                if at_override_occurred and thr not in ["TO-1", "TO-2", "TO-3"]:
                    thr_display = "TOGA"

            def _is_no_data(val):
                if val is None:
                    return True
                if str(val).strip() in ('', '0', 'XXX', 'XX', '---'):
                    return True
                try:
                    return float(val) == 0.0
                except (ValueError, TypeError):
                    return False

            if not rwy_message:
                v1_bad = _is_no_data(v1)
                v2_bad = _is_no_data(v2)
                mtow_bad = _is_no_data(mtow_val)
                if v1_bad and v2_bad and mtow_bad:
                    rwy_message = 'PTOW EXCEEDS MTOW ALL FLAPS'
                elif v1_bad and v2_bad:
                    rwy_message = 'PTOW EXCEEDS MTOW - RQST NEW TPS'
                elif mtow_bad:
                    rwy_message = 'FLAP N/A THIS RWY - RQST NEW TPS'

            if not mtow and rwy_message and xml_root is not None:
                _struct_raw = xml_root.findtext('weights/max_tow_struct', '0') or '0'
                _struct_w   = safe_weight(float(_struct_raw))
                if isinstance(_struct_w, float):
                    mtow = f"{_struct_w:.1f}S"
                    print(f"[DEBUG MTOW-FALLBACK] {rwy}: using max_tow_struct={_struct_w:.1f}S")

            try:
                flap_int = int(float(flap))
                flap_fmt = f"{flap_int:02d}" if 0 < flap_int <= 9 else str(flap_int)
            except Exception:
                flap_fmt = flap

            runway_rows.append({
                'rwy': rwy, 'flap': flap, 'flap_fmt': flap_fmt,
                'apu_status': apu_status, 'v1': v1, 'vr': vr, 'v2': v2,
                'thr_display': thr_display, 'at_display': at_display,
                'mtow': mtow, 'rwy_message': rwy_message,
            })

        # ── Second pass: flag improved-performance runways ───────────────────
        # SimBrief can assign the same flap setting on multiple runways but bump
        # the V-speeds upward to achieve a better climb gradient — trading runway
        # margin for climb performance. The MTOW may be identical so there is no
        # other visible indicator. We detect this by comparing VR within each flap
        # group: if a runway's VR is >5 kts above the lowest VR for that flap
        # setting, it is using improved performance and its flap label gets an X.
        from collections import defaultdict
        flap_vr_map = defaultdict(list)
        for row in runway_rows:
            if not row['rwy_message']:
                try:
                    flap_vr_map[row['flap']].append(int(row['vr']))
                except (ValueError, TypeError):
                    pass

        for row in runway_rows:
            if not row['rwy_message'] and row['flap'] in flap_vr_map:
                vr_list = flap_vr_map[row['flap']]
                if len(vr_list) > 1:
                    try:
                        this_vr = int(row['vr'])
                        min_vr  = min(vr_list)
                        if this_vr - min_vr > 5:
                            row['flap_fmt'] = row['flap_fmt'] + 'X'
                    except (ValueError, TypeError):
                        pass

        # ── Output pass ──────────────────────────────────────────────────────
        for row in runway_rows:
            rwy        = row['rwy']
            flap_fmt   = row['flap_fmt']
            apu_status = row['apu_status']
            v1         = row['v1']
            vr         = row['vr']
            v2         = row['v2']
            thr_display = row['thr_display']
            at_display  = row['at_display']
            mtow        = row['mtow']
            rwy_message = row['rwy_message']

            # Write runway data row
            if rwy_message:
                # No perf data — show message spanning the speed/thr columns, but still print MTOW
                # Fixed width of 33 chars matches: V1(3) VR(3) V2(3) + spaces + THR(7) + AT(8) + spaces
                msg_field = f"{rwy_message:<33}"
                mtow_suffix = f"{mtow:<6}" if mtow else ""
                output += f"{rwy:<5} {flap_fmt:<5} {apu_status:<4} {msg_field}{mtow_suffix}\n"
            else:
                output += f"{rwy:<5} {flap_fmt:<5} {apu_status:<4} {v1:>3} {vr:>3} {v2:>3}   {thr_display:<7} {at_display:<8}   {mtow:<6}\n"
            output += "-" * 58 + "\n"
        
        # === OPTIONAL AIRPORT NOTES ===
        airport_altitudes = get_airport_specific_altitudes(sta, max_elevation)
        if airport_altitudes:
            efp_text = airport_altitudes.get('EFP', "")
            if efp_text:
                output += "\n"
                output += "************* AIRPORT NOTES ********************************\n"
                output += f"{efp_text}\n\n"

        # === AIRPORT ANALYSIS DATA SECTION ===
        if valid_runways:
            output += "********* AIRPORT ANALYSIS DATA ****************************\n"

            from collections import OrderedDict

            PRE      = 22   # prefix width: " CONF/FLAP(4) APU/BLD(5) LIMIT(5) C(4) " = 1+4+1+5+1+5+1+4=22
            CW       = 7    # each runway column width
            MAX_RWYS = 5    # max runways per block

            def _fmt_conf(flap_val):
                s = str(flap_val).strip()
                try:
                    n = int(float(s))
                    return f"{n:02d}" if 0 < n <= 9 else str(n)
                except Exception:
                    return s

            def _sep(n):
                return "-" * (PRE + CW * n) + "\n"

            def _div(n):
                total = PRE + CW * n
                d = (" - -" * ((total // 4) + 2))[:total]
                return d + "\n"

            def _fmt_slope(sv):
                try:
                    sf = float(sv) if sv is not None else 0.0
                    if sf == 0.0:
                        return ".0"
                    s = f"{sf:.1f}"
                    if s.startswith("0."):   s = s[1:]
                    elif s.startswith("-0."): s = "-" + s[2:]
                    return s
                except Exception:
                    return "x.x"

            # Struct weight — from SimBrief's certified structural limit (weights/max_tow_struct)
            try:
                _struct_raw = xml_root.findtext('weights/max_tow_struct', '0') or '0'
                struct_wt_limit = safe_weight(float(_struct_raw))
                struct_wt_limit = struct_wt_limit if isinstance(struct_wt_limit, float) else 0.0
            except Exception:
                struct_wt_limit = 0.0
            struct_str = f"{struct_wt_limit:.1f}" if struct_wt_limit > 0 else "0.0"
            output += f"\n STRUCT WT LIMIT {struct_str}\n\n"

            def _cap_wt(val_thou):
                """Cap a weight (in thousands) at the structural weight limit."""
                if isinstance(val_thou, float) and struct_wt_limit > 0:
                    return min(val_thou, struct_wt_limit)
                return val_thou

            # E/O ACCEL height (same for all)
            eo_afl_int = 0
            if airport_altitudes:
                try:
                    eo_afl_int = int(float(airport_altitudes.get('eo_acc', '0')))
                except Exception:
                    pass

            import math as _math
            TEMP_PENALTY = 0.3
            from collections import OrderedDict as _OD

            # Build unique-runway lookup dict
            unique_rwy_by_id = {}
            for r in valid_runways:
                rid = r.get('id', 'XX')
                if rid not in unique_rwy_by_id:
                    unique_rwy_by_id[rid] = r

            # Build conf_groups once across all runways
            conf_groups = _OD()
            for r in valid_runways:
                conf_k  = _fmt_conf(r.get('flaps', 'XX'))
                bleed_k = str(r.get('bleed', '')).upper()
                apu_k   = ('OFF' if bleed_k == 'ON' else 'ON') if is_airbus \
                          else (bleed_k if bleed_k in ('ON', 'OFF') else 'OFF')
                key = (conf_k, apu_k)
                if key not in conf_groups:
                    conf_groups[key] = {}
                conf_groups[key][r.get('id', 'XX')] = r

            # Sort: by flap setting numerically, then APU OFF before ON
            def _conf_sort_key(c):
                try: return int(float(c))
                except: return 999
            conf_groups = _OD(
                sorted(conf_groups.items(),
                       key=lambda kv: (_conf_sort_key(kv[0][0]), 0 if kv[0][1] == 'OFF' else 1))
            )
            unique_confs = sorted(dict.fromkeys(ck for (ck, _) in conf_groups), key=_conf_sort_key)

            atow_lbs = (atow * 1000.0) if isinstance(atow, float) else 0.0

            for conf_only in unique_confs:
                pairs = [(ck, ak) for (ck, ak) in conf_groups if ck == conf_only]
                conf_rids_all = set()
                for pair in pairs:
                    conf_rids_all.update(conf_groups[pair].keys())

                # All unique runway IDs for this conf, in valid_runways order
                conf_all_ids = list(dict.fromkeys(
                    r.get('id', 'XX') for r in valid_runways if r.get('id', 'XX') in conf_rids_all
                ))

                # Chunk this conf's runways if > MAX_RWYS
                conf_id_chunks = [conf_all_ids[i:i+MAX_RWYS]
                                  for i in range(0, len(conf_all_ids), MAX_RWYS)]

                off_rid_map = conf_groups.get((conf_only, 'OFF'), {})
                on_rid_map  = conf_groups.get((conf_only, 'ON'),  {})
                base_rid_map = off_rid_map if off_rid_map else on_rid_map

                try:
                    first_r  = base_rid_map[next(iter(base_rid_map))]
                    oat_base = int(float(first_r.get('max_temp', first_r.get('temp', 23))))
                except Exception:
                    oat_base = 23

                for chunk_idx, conf_chunk_ids in enumerate(conf_id_chunks):
                    nc = len(conf_chunk_ids)

                    if chunk_idx > 0:
                        # Only print CONT. when a single flap setting overflows MAX_RWYS columns
                        output += "\n****** AIRPORT ANALYSIS DATA (CONT.) **********************\n"
                        output += f"\n STRUCT WT LIMIT {struct_str}\n\n"

                    # Per-conf column header
                    rwy_label_row = "".join(f"{'RWY':>{CW}}" for _ in conf_chunk_ids)
                    rwy_id_row    = "".join(f"{rid:>{CW}}" for rid in conf_chunk_ids)
                    output += f" {'':4} {'':5} {'CLIMB':>5} {'TEMP':>4}" + rwy_label_row + "\n"
                    output += f" {flap_label:<4} {ac_label:<5} {'LIMIT':>5} {'C':>4}" + rwy_id_row + "\n"
                    output += _sep(nc)

                    APU_ON_BOOST = 0.022

                    for apu_row, t_off in [('OFF', 0), ('OFF', 2), ('ON', 0), ('ON', 2)]:
                        conf_lbl = conf_only if (apu_row == 'OFF' and t_off == 2) else ""
                        apu_lbl  = apu_row
                        penalty  = TEMP_PENALTY if t_off == 2 else 0.0
                        try:
                            oat_display = int(float(temp)) + t_off
                        except Exception:
                            oat_display = oat_base + t_off

                        src_map = off_rid_map if apu_row == 'OFF' else on_rid_map

                        adj_wts, rwy_cols = [], []
                        for cid in conf_chunk_ids:
                            if cid in src_map:
                                mw = _cap_wt(safe_weight(src_map[cid].get('max_weight', 0)))
                            elif apu_row == 'ON' and cid in off_rid_map:
                                mw_off = safe_weight(off_rid_map[cid].get('max_weight', 0))
                                mw = _cap_wt(round((mw_off if isinstance(mw_off, float) else 0.0)
                                                   * (1 + APU_ON_BOOST), 1))
                            elif apu_row == 'OFF' and cid in on_rid_map:
                                mw_on = safe_weight(on_rid_map[cid].get('max_weight', 0))
                                mw = _cap_wt(round((mw_on if isinstance(mw_on, float) else 0.0)
                                                   / (1 + APU_ON_BOOST), 1))
                            else:
                                mw = None
                            if mw is not None:
                                adj = round(max(0.0, mw - penalty), 1)
                                adj_wts.append(adj)
                                rwy_cols.append(f"{adj:>{CW}.1f}")
                            else:
                                rwy_cols.append(" " * CW)

                        if not adj_wts:
                            continue
                        climb_limit = min(adj_wts)
                        output += f" {conf_lbl:<4} {apu_lbl:<5} {climb_limit:>5.1f} {oat_display:>4}" + "".join(rwy_cols) + "\n"

                    # HDWND / TLWND
                    primary_rid_map = conf_groups.get((conf_only, 'OFF'),
                                      conf_groups.get((conf_only, 'ON'), {}))
                    hw_cols, tl_cols = [], []
                    for cid in conf_chunk_ids:
                        r = primary_rid_map.get(cid)
                        if r:
                            mw_thou = _cap_wt(safe_weight(r.get('max_weight', 0)))
                            if isinstance(mw_thou, float) and mw_thou > 0:
                                mtow_lbs   = mw_thou * 1000.0
                                margin_lbs = max(0.0, mtow_lbs - atow_lbs)

                                # Weight margin ratio (0=at limit, 1=empty runway)
                                wt_margin = margin_lbs / mtow_lbs if mtow_lbs > 0 else 0.0

                                # Distance margin ratio
                                try:
                                    rwy_length  = float(unique_rwy_by_id[cid].get('length', 0) or 0)
                                    dist_margin = float(r.get('asdr', 0) or 0)
                                    dist_ratio  = (dist_margin / rwy_length) if rwy_length > 0 else 0.0
                                except Exception:
                                    dist_ratio  = 0.0

                                # Combined margin: geometric mean of weight and distance margins.
                                # Curved with sqrt so mid-range margins don't collapse too fast.
                                combined = (wt_margin * dist_ratio) ** 0.5

                                # HDWND: base rate 0.00287 of MTOW per knot (chosen to produce
                                # naturally non-round numbers). Scales up with margin — more
                                # margin means more headwind credit available.
                                # Curve: linear between 0.6× (tight) and 1.0× (loose) of base.
                                hw_scale = 0.60 + 0.40 * min(combined * 3.0, 1.0)
                                hw_lbs   = max(100, round(mtow_lbs * 0.00287 * hw_scale))

                                # TLWND: base rate 0.00671 of MTOW per knot.
                                # Scales inversely — tight margins mean bigger tailwind penalty.
                                # Curve: linear between 1.4× (tight) and 1.0× (loose) of base.
                                # Zero if ≥30% distance margin (ample runway).
                                tlwnd_zeroed = dist_ratio >= 0.30
                                tw_scale = 1.40 - 0.40 * min(combined * 3.0, 1.0)
                                tw_lbs   = 0 if tlwnd_zeroed else max(200, round(mtow_lbs * 0.00671 * tw_scale))
                            else:
                                hw_lbs = 1
                                tw_lbs = 0
                        else:
                            hw_lbs = 1
                            tw_lbs = 0
                        hw_cols.append(f"{hw_lbs:>{CW}}")
                        tl_cols.append(f"{tw_lbs:>{CW}}")
                    output += f" {'HDWND ADD / KT':<{PRE - 1}}" + "".join(hw_cols) + "\n"
                    output += f" {'TLWND SUB / KT':<{PRE - 1}}" + "".join(tl_cols) + "\n"
                    output += _div(nc)

                    # E/O ACCEL AFL / MSL
                    afl_cols, msl_cols = [], []
                    for cid in conf_chunk_ids:
                        r = unique_rwy_by_id.get(cid)
                        afl_cols.append(f"{eo_afl_int:>{CW}}")
                        try:
                            raw_elev = r.get('elevation', None) if r else None
                            if raw_elev in (None, '', 'ERR', 0, '0'):
                                elev = float(max_elevation)
                            else:
                                elev = float(raw_elev)
                            if elev <= 0:
                                elev = float(max_elevation)
                            msl_rounded = int(_math.ceil((elev + eo_afl_int) / 10.0)) * 10
                            msl_cols.append(f"{msl_rounded:>{CW}}")
                        except Exception:
                            msl_cols.append(f"{int(max_elevation):>{CW}}")
                    output += f" {'E/O ACCEL /AFL/ FT':<{PRE - 1}}" + "".join(afl_cols) + "\n"
                    output += f" {'          /MSL/ FT':<{PRE - 1}}" + "".join(msl_cols) + "\n"
                    output += _div(nc)

                    # PLANNED WIND
                    wind_cols = []
                    for cid in conf_chunk_ids:
                        r = unique_rwy_by_id.get(cid)
                        try:
                            hw = int(round(float(r.get('HD', r.get('headwind_component', 0))))) if r else 0
                            wind_cols.append(f"{'H'+str(hw) if hw >= 0 else 'T'+str(abs(hw)):>{CW}}")
                        except Exception:
                            wind_cols.append(f"{'H0':>{CW}}")
                    output += f" {'PLANNED WIND KT':<{PRE - 1}}" + "".join(wind_cols) + "\n"
                    output += _sep(nc)

                    # LENGTH / SLOPE
                    len_cols   = "".join(f"{int(unique_rwy_by_id[cid].get('length', 0)):>{CW}}" for cid in conf_chunk_ids)
                    slope_cols = "".join(f"{_fmt_slope(unique_rwy_by_id[cid].get('gradient')):>{CW}}" for cid in conf_chunk_ids)
                    output += f" {'LENGTH - FT':<{PRE - 1}}" + len_cols + "\n"
                    output += f" {'SLOPE - PCT':<{PRE - 1}}" + slope_cols + "\n"
                    output += _sep(nc)

                # ── A/I SUB — only when temp ≤ 15°C ──────────────────────
                try:
                    first_any = next(iter(unique_rwy_by_id.values()))
                    base_temp_ai = int(float(first_any.get('max_temp', first_any.get('temp', 23))))
                except Exception:
                    base_temp_ai = 23
                if base_temp_ai <= 15:
                    try:
                        tow_lbs = float(first_any.get('est_tow', 0))
                        ai_val  = round(tow_lbs * 0.0015 / 1000, 1)
                        ai_str  = f"{ai_val:.1f}".lstrip('0') or '.0'
                    except Exception:
                        ai_str = ".0"
                    output += f" A/I ON SUB FROM CLB {ai_str} RWY {ai_str}\n"
                    output += _sep(len(unique_rwy_by_id))

            output += "END\n"

        return output

    except Exception as e:
        print(f"Error in write_takeoff_performance_string: {e}")
        import traceback
        traceback.print_exc()
        return "ERR"
    
def generate_enhanced_howgozit(user_id, output_path=None):
    """Fetch SimBrief XML, parse it, generate enhanced HOWGOZIT text with OFP, and save as PDF."""
    try:
        # --- Fetch and parse XML ---
        xml_data = fetch_simbrief_data(user_id)
        xml_root = parse_xml_string(xml_data) if isinstance(xml_data, str) else xml_data
        if xml_root is None:
            print("Failed to parse XML data")
            return None

        # --- Scheduled departure time ---
        sched_out = get_text("times/sched_out", xml_root, "")
        if sched_out:
            try:
                timestamp = int(sched_out)
                sched_out_fmt = datetime.utcfromtimestamp(timestamp).strftime("%H%M")
            except (ValueError, OSError):
                sched_out_fmt = format_time_elapsed(sched_out)
        else:
            sched_out_fmt = "0000"

        takeoff_time = prompt_for_takeoff_time_str(sched_out_fmt)

        # --- Generate HOWGOZIT data ---
        print("DEBUG: About to call parse_simbrief_data_to_howgozit_with_ofp")
        result = parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time)
        
        # Add detailed debugging
        print(f"DEBUG: Result type: {type(result)}")
        print(f"DEBUG: Result is None: {result is None}")
        if result is not None:
            try:
                print(f"DEBUG: Result length: {len(result)}")
                if hasattr(result, '__iter__') and not isinstance(result, str):
                    print(f"DEBUG: Result items: {[type(item) for item in result]}")
                else:
                    print(f"DEBUG: Result content preview: {str(result)[:200]}")
            except Exception as e:
                print(f"DEBUG: Error examining result: {e}")
        
        # More specific validation
        if result is None:
            print("ERROR: parse_simbrief_data_to_howgozit_with_ofp returned None")
            return None
            
        if not isinstance(result, (tuple, list)):
            print(f"ERROR: Expected tuple/list, got {type(result)}")
            return None
            
        if len(result) < 5:
            print(f"ERROR: Expected 5 items, got {len(result)}")
            return None

        # --- Unpack the result properly ---
        try:
            raw_howgozit, flight_info, valid_runways, anti_ice_on, runway_lines = result
            print("DEBUG: Successfully unpacked result")
            print(f"DEBUG: raw_howgozit type: {type(raw_howgozit)}")
            print(f"DEBUG: flight_info type: {type(flight_info)}")
            print(f"DEBUG: valid_runways length: {len(valid_runways) if hasattr(valid_runways, '__len__') else 'N/A'}")
        except ValueError as e:
            print(f"ERROR: Failed to unpack result: {e}")
            return None

        # --- Normalize HOWGOZIT string ---
        if isinstance(raw_howgozit, list):
            howgozit_text = "\n".join(str(line) for line in raw_howgozit)
        elif isinstance(raw_howgozit, str):
            howgozit_text = raw_howgozit.replace('\r\n', '\n').replace('\r', '\n')
        else:
            howgozit_text = str(raw_howgozit)

        print(f"DEBUG: Final howgozit_text length: {len(howgozit_text)}")
        print(f"DEBUG: First 200 chars: {repr(howgozit_text[:200])}")

        # --- Build filename base: FROMTOFLTNUMDATE ---
        origin_icao      = get_text("origin/icao_code", xml_root) or "ORIG"
        destination_icao = get_text("destination/icao_code", xml_root) or "DEST"
        flight_number    = get_text("general/flight_number", xml_root) or "FL001"

        origin_clean = ''.join(c for c in str(origin_icao)      if c.isalnum())[:4]  or "ORIG"
        dest_clean   = ''.join(c for c in str(destination_icao) if c.isalnum())[:4]  or "DEST"
        flt_clean    = ''.join(c for c in str(flight_number)    if c.isalnum())[:10] or "FL001"

        # Date from scheduled departure timestamp → DDMMM (e.g. 19FEB)
        try:
            _sched_ts  = int(get_text("times/sched_out", xml_root) or "0")
            _date_str  = datetime.utcfromtimestamp(_sched_ts).strftime("%d%b").upper()  # e.g. 19FEB
        except Exception:
            _date_str = "NODATE"

        _base_name = f"{origin_clean}{dest_clean}{flt_clean}{_date_str}"

        if not output_path:
            folder = get_last_output_folder()
            if not folder:
                folder = prompt_for_output_folder()
                if not folder:
                    print("No folder selected. Aborting.")
                    return None
                save_last_output_folder(folder)
        else:
            folder = os.path.dirname(output_path)

        rls_path = os.path.join(folder, f"{_base_name}-RLS.pdf")
        wb_path  = os.path.join(folder, f"{_base_name}-WB.pdf")

        # --- Slice TPS and TLR blocks using explicit markers ---
        # Layout of howgozit_text:
        #   ... OFP body ...
        #   [PAGEBREAK][TPS_START]  ← start of TPS block
        #   ... TPS pages ...
        #   [PAGEBREAK][TLR_START]  ← start of TLR block
        #   ... TLR pages ...
        _TPS_MARKER = '[TPS_START]\n'
        _TLR_MARKER = '[TLR_START]\n'

        _tps_pos = howgozit_text.find(_TPS_MARKER)
        _tlr_pos = howgozit_text.find(_TLR_MARKER)

        if _tps_pos != -1 and _tlr_pos != -1:
            _tps_content = howgozit_text[_tps_pos + len(_TPS_MARKER):_tlr_pos]  # TPS only
            _tlr_content = howgozit_text[_tlr_pos + len(_TLR_MARKER):]          # TLR only
        else:
            # Fallback: markers missing — use full text for both
            _tps_content = howgozit_text
            _tlr_content = howgozit_text

        # WBD header prepended to the TPS standalone file
        _zulu_now   = datetime.utcnow().strftime("%H%M")
        _sta_code   = (get_text("origin/iata_code", xml_root) or get_text("origin/icao_code", xml_root) or "XXX")[:4].upper()
        _sep_line   = "\u2014" * 60 + "\n"
        _wbd_header = f"WBD*{flt_clean}/{_date_str}/{_zulu_now} {_sta_code}\n\n"

        _report_type = get_report_type()   # "TLR" or "TPS"

        if _report_type == "TPS":
            # Standalone file = TPS only (with WBD header), no TLR
            _standalone_path = os.path.join(folder, f"{_base_name}-WB.pdf")
            _standalone_text = _sep_line + _wbd_header + _tps_content
        else:
            # Standalone file = TLR only, no TPS
            _standalone_path = os.path.join(folder, f"{_base_name}-TLR.pdf")
            _standalone_text = _tlr_content

        print(f"DEBUG: Saving RLS to: {rls_path}")
        print(f"DEBUG: Saving {_report_type} to: {_standalone_path}")

        # --- Save PDFs ---
        save_as_pdf(rls_path,         howgozit_text)    # full OFP (always)
        save_as_pdf(_standalone_path, _standalone_text) # TLR -or- TPS, never both
        print(f"RLS saved: {rls_path}")
        print(f"{_report_type} saved: {_standalone_path}")

        # --- Auto-open both files ---
        for _path in (rls_path, _standalone_path):
            if os.path.exists(_path):
                try:
                    if os.name == 'nt':
                        os.startfile(_path)
                    else:
                        subprocess.run(['open', _path], check=False)
                except Exception as _e:
                    print(f"Could not open {_path}: {_e}")

        # --- Return full parsed content ---
        return howgozit_text, flight_info, valid_runways, anti_ice_on, runway_lines

    except Exception as e:
        print(f"Error in generate_enhanced_howgozit: {e}")
        traceback.print_exc()
        return None

def debug_xml_structure(xml_root, max_depth=2, current_depth=0):
    """Helper function to debug XML structure"""
    if current_depth >= max_depth or xml_root is None:
        return
    if isinstance(xml_root, str):
        xml_root = parse_xml_string(xml_root)
        if xml_root is None:
            return
    if hasattr(xml_root, 'tag'):
        indent = "  " * current_depth
        print(f"{indent}Tag: {xml_root.tag}")
        if xml_root.text and xml_root.text.strip():
            text_preview = xml_root.text.strip()[:50]
            print(f"{indent}Text: {text_preview}{'...' if len(xml_root.text.strip()) > 50 else ''}")
        if xml_root.attrib:
            print(f"{indent}Attributes: {xml_root.attrib}")
        children = list(xml_root)[:5]
        for child in children:
            debug_xml_structure(child, max_depth, current_depth + 1)
        if len(list(xml_root)) > 5:
            print(f"{indent}... and {len(list(xml_root)) - 5} more children")

def main():
    # Username: use command-line arg if provided, otherwise load/prompt and save
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        user_id = sys.argv[1]
    else:
        user_id = get_or_prompt_username()

    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    debug_mode = "--debug" in sys.argv

    print(f"Using SimBrief username: {user_id}")
    print(f"DEBUG: debug_mode = {debug_mode}")

    try:
        if debug_mode:
            xml_data = fetch_simbrief_data(user_id)
            xml_root = parse_xml_string(xml_data) if isinstance(xml_data, str) else xml_data
            if xml_root is not None:
                debug_xml_structure(xml_root)
            else:
                print("Failed to parse XML")
        else:
            # Generate enhanced HOWGOZIT and save PDF
            generate_enhanced_howgozit(user_id, output_path)

    except Exception as e:
        print(f"Main execution error: {e}")
        traceback.print_exc()

__version__ = "1.0.0"

if __name__ == "__main__":
    main()
