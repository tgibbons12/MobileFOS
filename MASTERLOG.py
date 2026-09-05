#!/usr/bin/env python3
"""
MASTERLOG — SimBrief OFP PDF Generator
=======================================
Fetches a SimBrief flight plan via the XML API, parses it, and produces
a formatted Operational Flight Plan (OFP) PDF matching airline dispatch
conventions.

Main entry point: ``main()`` / ``generate_enhanced_howgozit(user_id)``

Sections generated
------------------
* Flight plan header (fuel ladder, on-time analysis)
* Navigation log
* Takeoff performance (V-speeds, MTOW table)
* Weight & balance
* NOTAMs (departure, arrival, alternate, en-route)
* Weather (METAR / TAF / ATIS / SIGMETs)
* Field reports
* ETOPS / Oceanic route verification (when applicable)
"""
# --- Standard library ---
import base64
import collections
import io
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import textwrap
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# --- GUI (output-folder / username prompts) ---
import tkinter as tk
from tkinter import simpledialog

# --- Third party ---
import requests
from PIL import Image
from reportlab.lib.pagesizes import A4, letter, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# --- Local modules ---
from ENGINEFAILPROC import get_airport_specific_altitudes
from write_tps_section import load_full_runway_index as _full_runway_index
from fos_pages import (airline_iata as _fos_iata,
                       banner as _fos_banner,
                       routing_line as _fos_routing,
                       build_context as _fos_context,
                       build_fi_page, build_fil_page,
                       build_fuel_service_page, build_nsc_page,
                       build_wbd_page)
from write_tlr_section import write_tlr_section
from write_tps_section import write_takeoff_performance_string

# The data layer is shared: all three release formats read the same
# values through the same code, and differ only in how they draw them.
# Local copies of these had drifted — three get_text implementations,
# three extract_runway_data — so a fix in one never reached the others.
from masterlog_core import (  # noqa: F401  (re-exported for this module's own use)
    _pick_atc_freq,
    add_time_to_takeoff,
    add_times,
    calculate_time_difference,
    calculate_tldr,
    debug_xml_structure,
    extract_runway_data,
    fetch_simbrief_data,
    format_coord,
    format_coord_aviation,
    format_fuel,
    format_off_time,
    format_out_time,
    format_time_elapsed,
    format_time_endurance,
    format_time_hhmm,
    get_element_text,
    get_suitability_window,
    get_text,
    is_valid_runway,
    pad_if_number,
    parse_xml_string,
    prompt_for_takeoff_time_str,
    safe_float,
    seconds_to_hhmm,
)


# Optional dependency: pypdf is used for PDF page merging (ETOPS blobs).
# If not installed, those pages are silently skipped.
try:
    from pypdf import PdfReader as PdfReader, PdfWriter as PdfWriter
    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False
    PdfReader = PdfWriter = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Logging configuration
# Use LOG.debug() / LOG.info() / LOG.warning() / LOG.error() throughout.
# The root level is INFO by default; set to DEBUG for verbose run output.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)
LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intersection grouping — self-contained so output matches ACARS TPS exactly
# regardless of which version of TAKEOFF_PERF is installed.
# ---------------------------------------------------------------------------
_runway_index_cache = None

def load_runway_index():
    """Load runway_index.dat from script directory. Cached after first load."""
    global _runway_index_cache
    if _runway_index_cache is not None:
        return _runway_index_cache
    dat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runway_index.dat')
    index = {}
    if not os.path.exists(dat_path):
        _runway_index_cache = index
        return index
    try:
        with open(dat_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(';')
                if len(parts) < 3:
                    continue
                icao    = parts[0].upper()
                rwy_raw = parts[1].upper()
                try:
                    tora_m = float(parts[2])
                except ValueError:
                    continue
                tora_ft = tora_m * 3.28084
                if '_' in rwy_raw:
                    rwy_base, taxiway = rwy_raw.split('_', 1)
                else:
                    continue  # full-length entry — skip
                key = (icao, rwy_base)
                index.setdefault(key, []).append({'taxiway': taxiway, 'tora_ft': tora_ft})
    except OSError as e:
        # Transient stall reading an iCloud-backed placeholder file (ETIMEDOUT etc.)
        # Degrade gracefully instead of blowing up TPS generation — no intersection
        # data this run, same as a missing file.
        LOG.warning(f"[TPS] runway_index.dat read failed ({e}); continuing without intersection data")
        _runway_index_cache = {}
        return _runway_index_cache
    _runway_index_cache = index
    return index


def get_intersection_groups(icao, rwy_id, full_tora_ft, distance_reject_ft, index_data):
    """
    Return up to 3 intersection groups (X/Y/Z) for a runway.
    Algorithm mirrors TAKEOFF_PERF.py exactly:
      - Filter entries below distance_reject_ft
      - 10% TORA band grouping, max 3 per band
      - Post-process: split last entry off a full (3-entry) band if its gap
        from the 2nd entry exceeds 400 ft  (produces KE as its own Z group)
    """
    if not index_data or full_tora_ft <= 0:
        return []
    rwy_base = rwy_id.upper()
    entries  = index_data.get((icao.upper(), rwy_base), [])
    if not entries:
        return []
    # Log all entries (including sub-reject), then filter before band grouping
    LOG.info(f"[INTXN] {icao} {rwy_id}: {len(entries)} entries, reject={distance_reject_ft:.0f}ft: "
             f"{[(e['taxiway'], int(e['tora_ft'])) for e in sorted(entries, key=lambda x: x['tora_ft'], reverse=True)]}")
    valid = [e for e in entries if e['tora_ft'] >= distance_reject_ft]
    if not valid:
        LOG.info(f"[INTXN] {icao} {rwy_id}: no entries clear reject distance — no intersections")
        return []

    band_width   = full_tora_ft * 0.10
    MAX_PER_BAND = 3
    SPLIT_GAP_FT = 400
    valid_sorted = sorted(valid, key=lambda e: e['tora_ft'], reverse=True)

    bands = []
    for entry in valid_sorted:
        placed = False
        for band in bands:
            if (band[0]['tora_ft'] - entry['tora_ft']) <= band_width and len(band) < MAX_PER_BAND:
                band.append(entry)
                placed = True
                break
        if not placed:
            bands.append([entry])

    # Split last entry off any full band where the final gap exceeds threshold
    final_bands = []
    for band in bands:
        if len(band) == MAX_PER_BAND:
            gap = band[-2]['tora_ft'] - band[-1]['tora_ft']
            if gap > SPLIT_GAP_FT:
                final_bands.append(band[:-1])
                final_bands.append([band[-1]])
                continue
        final_bands.append(band)
    bands = final_bands[:3]

    suffixes = ['X', 'Y', 'Z']
    groups   = []
    for i, band in enumerate(bands):
        most_restrictive = min(e['tora_ft'] for e in band)
        taxiways = [e['taxiway'] for e in sorted(band, key=lambda e: e['tora_ft'], reverse=True)]
        groups.append({
            'suffix':   suffixes[i],
            'id':       rwy_base + suffixes[i],
            'tora_ft':  most_restrictive,
            'taxiways': taxiways,
            'valid':    most_restrictive >= distance_reject_ft,
        })
    return groups

_INTXN_AVAILABLE = True


# ── Per-script config file: MASTERLOG.py → MASTERLOG.config ───────────────────
CONFIG_FILE = os.path.splitext(os.path.abspath(__file__))[0] + ".config"
global_acdata = {}

_cached_font_choice = None


# ---------------------------------------------------------------------------
# Font path constants — edit these for your local system if needed.
# The font-picker UI and fallback logic will still work even when a path is
# absent (the code falls through to the built-in Courier fallback).
# ---------------------------------------------------------------------------
_FONT_PATH_COURIER_NORMAL   = "/Users/tobygibbons/Library/Fonts/COURIER.TTF"
_FONT_PATH_COURIER_NEW      = "/Users/tobygibbons/Library/Fonts/couriernew.ttf"
_FONT_PATH_COURIER_PRIME    = "/Users/tobygibbons/Library/Fonts/CourierPrime-Regular.ttf"
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
        _FONT_PATH_COURIER_NORMAL,
    ],
    "🌈 Rainbow Comic Sans": [
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "/Library/Fonts/Comic Sans MS.ttf",
        "C:/Windows/Fonts/comic.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Comic_Sans_MS.ttf",
    ],
}
# ===========================================================================
# Configuration & font discovery
# Persisted user prefs (~/.masterlog config) plus TTF registration.
# ===========================================================================


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
        LOG.warning(f"Could not save config: {e}")

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

def ask_font_selection():
    """
    Read font choice from config key 'font'.
    Values: "1" = Courier Normal, "2" = Menlo, "3" = Courier New, "4" = Rainbow Comic Sans
    Defaults to "1" if not set.
    """
    global _cached_font_choice
    if _cached_font_choice is not None:
        return _cached_font_choice
    _cached_font_choice = str(_load_config().get("font", "1")).strip() or "1"
    return _cached_font_choice


def get_report_type() -> str:
    """Return 'TLR' or 'TPS'.  Reads from config key 'report_type', defaults to 'TPS'.

    Set in MASTERLOG.config:
        "report_type": "TPS"   -- ops release + separate W&B file (default)
        "report_type": "TLR"   -- ops release only (no W&B file)
    """
    return _load_config().get("report_type", "TPS").strip().upper() or "TPS"

# ===========================================================================
# PDF rendering primitives
# Low-level canvas drawing: navlog table, NOTAM blocks, page assembly.
# ===========================================================================


def _draw_navlog_table(c, navlog_lines, font_name, font_size=8.5, footer_fn=None, header_fn=None):
    """Render the navigation log as a bordered table matching the SimBrief OFP PDF style.

    Each fix occupies two rows inside a Courier-font table:
      Row A: name | LAT | LONG | MC | MK | GS | TD | SD | ST | SB
      Row B: ident | FL | WIND | WCP | (blank) | MH | TRR | TAS | I | TLDR | TTLT | TTLB | TH

    Long navlogs flow across multiple portrait pages with the column header repeated.
    """
    from reportlab.lib import colors
    PW, PH   = A4                   # 595 × 842 pts842 pts
    L_MARGIN = 55                   # left margin — shifted left ~24pts
    R_MARGIN = 103                  # right margin — compensates to keep table width
    TOP_Y    = PH - 46
    BOT_Y    = 100
    TW       = PW - L_MARGIN - R_MARGIN   # 437 pts

    FS      = font_size
    ROW_H   = FS * 2.1          # row height
    HDR_H   = ROW_H * 1.8      # header band

    MONO    = font_name
    BOLD    = font_name

    PAD     = 4                     # increased cell padding for edge breathing room

    # Column spec: fractions tightened so text fills columns with minimal dead space
    COLS = [
        ("TO",   "IDENT", 0.155, "L"),   # fix name
        ("",     "FL",    0.040, "R"),   # FL
        ("LAT",  "WIND",  0.110, "L"),   # lat / wind
        ("LONG", "WCP",   0.110, "L"),   # long / wcp
        ("MC",   "MH",    0.055, "R"),   # course/heading
        ("MK",   "TRR",   0.055, "R"),   # mach/mora
        ("GS",   "TAS",   0.055, "R"),   # speed
        ("TD",   "I",     0.050, "R"),   # ISA dev
        ("SD",   "TLDR",  0.075, "R"),   # segment dist
        ("ST",   "TTLT",  0.080, "R"),   # segment time
        ("SB",   "TTLB",  0.080, "R"),   # segment fuel
        ("",     "TH",    0.055, "R"),   # tropopause
    ]
    _frac_sum = sum(f for _, _, f, _ in COLS)
    COLS = [(la, lb, f / _frac_sum, al) for la, lb, f, al in COLS]
    col_w   = [TW * f for _, _, f, _ in COLS]
    col_al  = [al for _, _, _, al in COLS]   # use actual alignment spec from COLS
    NC      = len(COLS)

    NAV_BG   = colors.HexColor("#d8d8d8")   # light grey header — black Courier text on top
    def _cx(i):
        return L_MARGIN + sum(col_w[:i])

    def _draw_header(canvas, y):
        """Draw the light-grey column-header band with black Courier text. Returns y after header."""
        canvas.setFillColor(NAV_BG)
        canvas.rect(L_MARGIN, y - HDR_H, TW, HDR_H, fill=1, stroke=0)
        canvas.setFont(MONO, FS)
        canvas.setFillColor(colors.black)
        for ci, (la, lb, _, al) in enumerate(COLS):
            cx = _cx(ci)
            top_y = y - HDR_H * 0.35   # shifted down from 0.25
            bot_y = y - HDR_H * 0.75   # shifted down from 0.68
            for label, ly in ((la, top_y), (lb, bot_y)):
                if not label:
                    continue
                canvas.drawString(cx + PAD, ly, _safe_latin1(label))
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(1.0)
        canvas.line(L_MARGIN + 0.5, y - HDR_H, L_MARGIN + TW - 0.5, y - HDR_H)
        return y - HDR_H

    def _draw_pair(canvas, y, vals_a, vals_b, shade):
        """Draw two-row fix entry. Returns new y."""
        pair_h = ROW_H * 2
        canvas.setFont(MONO, FS)
        for ri, vals in enumerate((vals_a, vals_b)):
            row_y = y - ROW_H * ri - ROW_H + ROW_H * 0.25
            for ci, val in enumerate(vals):
                if not val:
                    continue
                vs = _safe_latin1(str(val)); cx = _cx(ci); cw = col_w[ci]
                sw = canvas.stringWidth(vs, MONO, FS)
                tx = (cx + cw - PAD - sw) if col_al[ci] == "R" else (cx + PAD)
                canvas.drawString(tx, row_y, vs)
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(1.0)
        canvas.line(L_MARGIN + 0.5, y - pair_h, L_MARGIN + TW - 0.5, y - pair_h)
        return y - pair_h

    def _border(canvas, top, bot):
        """Draw outer border rect after all content — single clean stroke, no overlap doubling."""
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(1.0)
        canvas.rect(L_MARGIN, bot, TW, top - bot, fill=0, stroke=1)

    # ── Parse navlog lines into (row_a_text, row_b_text) pairs ───────────────
    # Strip the two header lines and leading separator; discard inter-fix dashes.
    # [ALTN_BANNER:...] markers are preserved as sentinel pairs.
    clean = []
    hdr_done = 0
    skip_next_header = False
    for ln in navlog_lines:
        s = ln.strip()
        # Alternate section banner — preserve as a sentinel
        if s.startswith("[ALTN_BANNER:"):
            clean.append(ln.rstrip())
            clean.append("")          # empty companion → banner is its own pair, no fix data consumed
            skip_next_header = True   # next 3 lines are the repeated HEADER — skip them
            hdr_done = 0
            continue
        # Skip repeated header after banner
        if skip_next_header:
            if not s or s.startswith("-") or s.startswith("TO ") or s.startswith("IDENT "):
                hdr_done += 1
                if hdr_done >= 3:
                    skip_next_header = False
                    hdr_done = 0
                continue
        if hdr_done < 3:
            if not s or s.startswith("-") or s.startswith("TO ") or s.startswith("IDENT "):
                hdr_done += 1
                continue
        if s.startswith("--"):
            continue
        if not s:          # skip blank lines anywhere — they shift pair parity
            continue
        clean.append(ln.rstrip())

    pairs = [(clean[i], clean[i + 1] if i + 1 < len(clean) else "")
             for i in range(0, len(clean), 2)]

    def _split_a(raw):
        """Parse line-1 text into NC column values.

        Classify each data token after LAT/LONG by shape:
          - Starts with M or P + digits  -> TD (ISA deviation, e.g. P05, M01)
          - Pure digits len<=3            -> MC, MK, or GS (accumulated left-to-right)
          - Pure digits len==4            -> SD, ST, SB (accumulated left-to-right)
        This handles any combination of blank MC/MK/GS/TD without relying on token count.
        """
        parts = raw.split()
        if not parts:
            return [""] * NC
        # Find first N/S lat token (exactly 6 chars starting with N or S)
        ni = next((k for k, p in enumerate(parts) if len(p) == 6 and p[0] in "NS"), 1)
        name = " ".join(parts[:ni])
        r    = parts[ni:]                 # [LAT, LONG, ...data...]
        def g(n): return r[n] if n < len(r) else ""

        mc = mk = gs = td = sd = st = sb = ""
        small_ints = []   # accumulates MC, MK, GS in order
        four_digits = []  # accumulates SD, ST, SB in order

        for tok in r[2:]:             # skip LAT and LONG
            if not tok: continue
            if tok[0] in "MP" and len(tok) > 1 and tok[1:].isdigit():
                td = tok              # signed ISA deviation
            elif tok.isdigit():
                if len(tok) <= 3:
                    small_ints.append(tok)   # MC / MK / GS
                elif len(tok) == 4:
                    four_digits.append(tok)  # SD / ST / SB

        # Assign small_ints -> rightmost is GS, middle is MK, leftmost is MC
        if len(small_ints) >= 3: mc, mk, gs = small_ints[0], small_ints[1], small_ints[2]
        elif len(small_ints) == 2: mk, gs   = small_ints[0], small_ints[1]
        elif len(small_ints) == 1: gs       = small_ints[0]

        # Assign four_digits -> SD, ST, SB in order
        if len(four_digits) >= 3: sd, st, sb = four_digits[0], four_digits[1], four_digits[2]
        elif len(four_digits) == 2: st, sb   = four_digits[0], four_digits[1]
        elif len(four_digits) == 1: sb       = four_digits[0]

        # col: 0=name 1=FL(blank) 2=LAT 3=LONG 4=MC 5=MK 6=GS 7=TD 8=SD 9=ST 10=SB 11=TH(blank)
        return [name, "", g(0), g(1), mc, mk, gs, td, sd, st, sb, ""]

    def _split_b(raw):
        """Parse line-2 text into NC column values using fixed-width positional parsing.

        _line2() writes:
          [0:18]  IDENT  (18 chars left-aligned)
          [18:21] FL     (3 chars right-aligned)
          [21:]   rest, one of two layouts:
            WITH wind:    " DDDSSS "  WCP3 "     " MH3 " " TRR3 " " tail
            WITHOUT wind: "     "     WCP3 "     " MH3 " " TRR3 " " tail
        """
        if not raw or not raw.strip():
            return [""] * NC

        # Pad to avoid index errors
        s = raw.rstrip()
        def ch(a, b): return s[a:b].strip() if len(s) >= b else s[a:].strip() if len(s) > a else ""

        ident = ch(0, 14)
        fl    = ch(14, 17)

        # Detect wind: 6 consecutive digits at positions 18-23 (between surrounding spaces)
        wind_present = (len(s) >= 24 and s[17] == ' ' and s[24] == ' '
                        and s[18:24].isdigit())

        if wind_present:
            wind = ch(18, 24)
            wcp  = ch(25, 28)
            mh   = ch(33, 36)
            trr  = ch(37, 40)
            tail_start = 41
        else:
            wind = ""
            wcp  = ch(22, 25)
            mh   = ch(30, 33)
            trr  = ch(34, 37)
            tail_start = 38

        # Tail contains TAS then optional I, TLDR, TTLT, TTLB, TH
        tail_str = s[tail_start:] if len(s) > tail_start else ""
        tail_tokens = tail_str.split()

        # TAS is 1-3 digits (never 4). If first token is 4 digits it is TTLT
        # (happens for airport fixes where TAS is blank).
        if tail_tokens and tail_tokens[0].isdigit() and len(tail_tokens[0]) < 4:
            tas = tail_tokens[0]
            rem = tail_tokens[1:]
        else:
            tas = ""
            rem = tail_tokens

        # State-machine classifier: tokens arrive in order [I] [TLDR] TTLT [TTLB] [TH]
        # TTLT is always 4 digits. Everything before the first 4-digit token is pre-TTLT.
        # Pre-TTLT:  single char or "-" -> I; 1-3 digit number -> TLDR.
        # Post-TTLT: second 4-digit -> TTLB; 1-2 digit -> TH.
        i_c = tldr = ttlt = ttlb = th = ""
        seen_ttlt = False
        for tok in rem:
            if not tok:
                continue
            if tok.isdigit():
                n = len(tok)
                if n == 4:
                    if tok[0] == "0":
                        if not ttlt: ttlt = tok; seen_ttlt = True
                        else:        ttlb = tok
                    else:
                        tldr = tok          # 4-digit TLDR >999nm never has leading zero
                elif not seen_ttlt:
                    if n == 1 and not i_c: i_c = tok
                    else: tldr = tok
                else:
                    th = tok
            elif tok in ("-", "--"):
                if not i_c: i_c = tok

        # Airport fix reclassification:
        # SimBrief puts remaining distance in the true_airspeed XML field for
        # origin/destination fixes (e.g. KORF 661nm, KORD 0nm). These rows have
        # blank MH and TRR. When those are blank and TLDR is empty, the value
        # parsed as TAS is actually TLDR — move it to the right column.
        if tas and not mh and not trr and not tldr:
            tldr = tas
            tas  = ""

        # Return exactly NC=12 values aligned to COLS:
        # 0=IDENT 1=FL 2=WIND 3=WCP 4=MH 5=TRR 6=TAS 7=I 8=TLDR 9=TTLT 10=TTLB 11=TH
        return [ident, fl, wind, wcp, mh, trr, tas, i_c, tldr, ttlt, ttlb, th]

    # ── Render pages ──────────────────────────────────────────────────────────
    c.setPageSize(A4)
    c.setFont(MONO, FS)
    if header_fn:
        header_fn(c, PW, PH)
    y      = _draw_header(c, TOP_Y)
    pg_top = TOP_Y

    for ra, rb in pairs:
        # ── Alternate section banner ──────────────────────────────────────────
        if ra.strip().startswith("[ALTN_BANNER:"):
            banner_text = ra.strip()[len("[ALTN_BANNER:"):-1]
            # Page-break check before drawing banner + header (needs ~3 rows of space)
            if y - ROW_H * 3 < BOT_Y + 4:
                _border(c, pg_top, y)
                if footer_fn:
                    footer_fn(c, PW, PH)
                c.showPage()
                c.setPageSize(A4)
                c.setFont(MONO, FS)
                if header_fn:
                    header_fn(c, PW, PH)
                y      = _draw_header(c, TOP_Y)
                pg_top = TOP_Y
            c.setStrokeColor(colors.black)
            c.setLineWidth(2.0)
            c.line(L_MARGIN + 0.5, y, L_MARGIN + TW - 0.5, y)  # thick line above banner
            # Draw banner as a full-width grey band INSIDE the table (no border close)
            BANNER_H = ROW_H * 1.17
            c.setFillColor(NAV_BG)
            c.rect(L_MARGIN, y - BANNER_H, TW, BANNER_H, fill=1, stroke=0)
            c.setFont(BOLD, FS)
            c.setFillColor(colors.black)
            c.drawString(L_MARGIN + PAD, y - BANNER_H + BANNER_H * 0.3, _safe_latin1(banner_text))
            c.setStrokeColor(colors.black)
            c.setLineWidth(2.0)
            c.line(L_MARGIN + 0.5, y - BANNER_H, L_MARGIN + TW - 0.5, y - BANNER_H)
            y -= BANNER_H
            # Redraw column header for alternate section, still inside same table
            y = _draw_header(c, y)
            continue
        if y - ROW_H * 2 < BOT_Y + 4:
            _border(c, pg_top, y)
            if footer_fn:
                footer_fn(c, PW, PH)
            c.showPage()
            c.setPageSize(A4)
            c.setFont(MONO, FS)
            if header_fn:
                header_fn(c, PW, PH)
            y      = _draw_header(c, TOP_Y)
            pg_top = TOP_Y
        va = _split_a(ra)
        vb = _split_b(rb)
        # Suppress Row B ident (col 0) when it duplicates the Row A name.
        # Keep it only when the ident is a distinct abbreviation (TOC, TOD,
        # navaid short code, airport elevation like "26FT") — i.e. when it
        # differs from the name and is not just the same word repeated.
        name_a  = va[0].strip().upper()
        ident_b = vb[0].strip().upper()
        if ident_b and ident_b == name_a:
            vb = list(vb)
            vb[0] = ""
        y = _draw_pair(c, y, va, vb, False)

    if y < TOP_Y:
        _border(c, pg_top, y)
    c.showPage()



def _safe_latin1(text):
    """
    Sanitize a string for ReportLab's built-in Courier/Helvetica fonts,
    which are Latin-1 encoded. Characters above U+00FF render as blank squares.
    Known typographic characters are replaced with ASCII equivalents; anything
    else still outside Latin-1 is silently dropped.
    """
    if not text:
        return text
    _MAP = {
        '\u2014': '--',   # em dash
        '\u2013': '-',    # en dash
        '\u2022': '*',    # bullet
        '\u2023': '>',    # triangular bullet
        '\u2192': '->',   # right arrow
        '\u2190': '<-',   # left arrow
        '\u2265': '>=',   # greater-or-equal
        '\u2264': '<=',   # less-or-equal
        '\u25B2': '^',    # up triangle
        '\u25BC': 'v',    # down triangle
        '\u25B6': '>',    # right triangle
        '\u00D7': 'x',    # multiplication sign
        '\u2018': "'",    # left single quote
        '\u2019': "'",    # right single quote
        '\u201C': '"',    # left double quote
        '\u201D': '"',    # right double quote
        '\u2026': '...',  # ellipsis
        '\u00B0': 'deg',  # degree sign
        '\u00B1': '+/-',  # plus-minus
        '\u2212': '-',    # minus sign
        '\u00A0': ' ',    # non-breaking space
        '\U0001F308': '', # rainbow emoji -> drop  (\U + 8 digits; \u only takes 4)
    }
    for uc, asc in _MAP.items():
        text = text.replace(uc, asc)
    # Keep only Latin-1 printable characters (U+0020–U+00FF) plus newline/tab.
    # Anything above U+00FF renders as a box square in Courier/Latin-1 fonts — drop it.
    text = ''.join(c for c in text if (c.isprintable() and ord(c) <= 0xFF) or c in '\n\r\t')
    return text.encode('latin-1', errors='ignore').decode('latin-1')


def _draw_notam_section(c, notam_text, font_name, font_size=7, footer_fn=None):
    """
    Render NOTAM section in landscape two-column layout, closely matching real OFP style:
      - Compact 7pt monospaced text, zero inter-entry gap
      - Airport header: ICAO left large, role right, airport name centre
      - RWYS inline in header bar (no separate sub-bar)
      - Category banner: thin, centred label
      - Each NOTAM is an unbreakable box with alternating white/light-grey background
      - Subtle vertical rule between columns
      - Expired section flagged with muted divider
    """
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib import colors

    # ── Attempt to load DIN / condensed sans font for airport headers ─────────
    _DIN_PATHS = [
        "/Library/Fonts/DINNextLTPro-Regular.otf",
        "/Library/Fonts/DIN Next LT Pro Regular.otf",
        "/Library/Fonts/DINPro-Regular.otf",
        "/Library/Fonts/DIN Alternate Bold.ttf",
        "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
        "/Library/Fonts/DINCondensed-Bold.ttf",
        "/Library/Fonts/D-DIN.otf",
        "/Library/Fonts/D-DIN Condensed.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ]
    _DIN_BOLD_PATHS = [
        "/Library/Fonts/DINNextLTPro-Bold.otf",
        "/Library/Fonts/DIN Next LT Pro Bold.otf",
        "/Library/Fonts/DINPro-Bold.otf",
        "/Library/Fonts/DIN Alternate Bold.ttf",
        "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
        "/Library/Fonts/DINCondensed-Bold.ttf",
        "/Library/Fonts/D-DIN Bold.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    ]
    HDR_FONT = None
    HDR_FONT_BOLD = None
    for _p in _DIN_PATHS:
        if os.path.exists(_p):
            try:
                pdfmetrics.registerFont(TTFont("DINHdr", _p))
                HDR_FONT = "DINHdr"
            except Exception:
                pass
            break
    for _p in _DIN_BOLD_PATHS:
        if os.path.exists(_p):
            try:
                pdfmetrics.registerFont(TTFont("DINHdrBold", _p))
                HDR_FONT_BOLD = "DINHdrBold"
            except Exception:
                pass
            break
    if not HDR_FONT:
        HDR_FONT      = "Helvetica"
    if not HDR_FONT_BOLD:
        HDR_FONT_BOLD = "Helvetica-Bold"

    # ── Page geometry ─────────────────────────────────────────────────────────
    LW, LH  = landscape(letter)          # 792 × 612 pts
    MARGIN  = 24
    COL_GAP = 10
    COL_W   = (LW - 2 * MARGIN - COL_GAP) / 2
    TOP_Y   = LH - MARGIN - 12      # leaves room for the running page header
    BOT_Y   = MARGIN

    # ── Typography ────────────────────────────────────────────────────────────
    FS        = font_size          # body font size
    FS_HDR    = FS - 0.5           # NOTAM ID header line — slightly smaller/bold
    FS_LABEL  = FS + 0.5           # category banner label
    LH_TEXT   = FS + 3             # line height — extra spacing between lines
    BOLD      = "Helvetica-Bold"
    MONO      = font_name

    # ── Colours ───────────────────────────────────────────────────────────────
    C_AIRPORT_BG   = colors.HexColor("#38474F")   # airport/FIR header
    C_CATEGORY_BG  = colors.HexColor("#566E7A")   # category banner
    C_NOTAM_HDR_BG = colors.HexColor("#DDE4E8")   # individual NOTAM header row
    C_WHITE        = colors.white
    C_BODY_BG      = colors.white                 # NOTAM body background
    C_EXP_HDR_BG   = colors.HexColor("#E8E0D8")   # expired NOTAM header row
    C_EXP_BODY_BG  = colors.HexColor("#F5F2EE")   # expired NOTAM body
    C_TEXT         = colors.HexColor("#111111")
    C_TEXT_CREATED = colors.HexColor("#555555")   # dimmer for CREATED line
    C_TEXT_EXP     = colors.HexColor("#999999")
    C_RULE         = colors.HexColor("#CCCCCC")   # column divider

    # ── Banner heights ─────────────────────────────────────────────────────────
    BH_RWYS     = 16    # RWYS sub-bar — same colour as airport, more height
    BH_CATEGORY = 16    # category banner
    PAD_X       = 8     # left text padding

    # ── Running page header ───────────────────────────────────────────────────
    # Flight ident left, generation timestamp right, on every NOTAM page:
    #   ENY3936 / KORF - KORD / 2024-09-25 / N772MR        2024-Sep-25 20:56
    _page_ident = ""
    for _l in notam_text.splitlines():
        _ls = _l.strip()
        if _ls.startswith("ID:"):
            _page_ident = _ls[3:].strip()
            break
    _page_stamp = datetime.now(timezone.utc).strftime("%Y-%b-%d %H:%M")
    HDR_H = 14                      # vertical space reserved for the header

    def draw_page_header():
        if not _page_ident:
            return
        c.setFont(HDR_FONT_BOLD, 7.5)
        c.setFillColor(colors.HexColor("#444444"))
        c.drawString(MARGIN, LH - MARGIN + 3, _safe_latin1(_page_ident))
        c.drawRightString(LW - MARGIN, LH - MARGIN + 3, _safe_latin1(_page_stamp))
        c.setStrokeColor(colors.HexColor("#BBBBBB"))
        c.setLineWidth(0.4)
        c.line(MARGIN, LH - MARGIN - 1, LW - MARGIN, LH - MARGIN - 1)

    # ── Column state ──────────────────────────────────────────────────────────
    col_idx = 0
    col_x   = [MARGIN, MARGIN + COL_W + COL_GAP]

    def cx():
        return col_x[col_idx]

    def draw_col_rule():
        """Draw subtle vertical rule on right edge of left column."""
        rx = MARGIN + COL_W + COL_GAP / 2
        c.setStrokeColor(C_RULE)
        c.setLineWidth(0.4)
        c.line(rx, BOT_Y, rx, TOP_Y)

    def advance_column():
        nonlocal col_idx, y
        if col_idx == 0:
            col_idx = 1
            y = TOP_Y
        else:
            col_idx = 0
            draw_col_rule()
            if footer_fn:
                footer_fn(c, LW, LH)
            c.showPage()
            pages_drawn[0] += 1
            c.setPageSize(landscape(letter))
            c.setFont(MONO, FS)
            draw_page_header()
            y = TOP_Y

    def ensure(pts):
        if y - pts < BOT_Y:
            advance_column()

    # ── Drawing helpers ────────────────────────────────────────────────────────

    def draw_airport_banner(icao, role, iata_name, rwy_lines, ident_line=""):
        """
        Real OFP layout:
          Left strip: APT rotated
          ICAO: large, vertically centred, non-bold DIN
          Right of ICAO top line: "IATA - CITY NAME"  (IATA white, city muted grey-blue)
          Below name: RWYS: xx/xx xx/xx  (all on same indent, wrapping)
          Far right: DEPARTURE (bold, top-aligned)
        Single colour block — no sub-bar.
        """
        nonlocal y
        n_rwy    = len(rwy_lines) if rwy_lines else 0
        ICAO_FS  = 22
        LINE_GAP = BH_RWYS - 2          # gap between text lines in header
        TOP_PAD  = 3
        BOT_PAD  = 0
        # Lines after ICAO: name (1) + rwy lines (n_rwy)
        text_lines_h = (1 + n_rwy) * LINE_GAP
        apt_h    = TOP_PAD + ICAO_FS + max(0, text_lines_h - LINE_GAP) + BOT_PAD
        FIR_MIN  = TOP_PAD + ICAO_FS + LINE_GAP + 4   # FIR gets one extra line of height
        total_h  = max(FIR_MIN, apt_h) if not role else apt_h
        ensure(total_h + BH_CATEGORY + LH_TEXT * 3)

        APT_W = 18

        # Single dark block
        c.setFillColor(C_AIRPORT_BG)
        c.rect(cx(), y - total_h, COL_W, total_h, fill=1, stroke=0)

        # APT/FIR badge — rotated 90°, top-aligned to match ICAO
        badge_label    = "FIR" if not role else "APT"
        badge_centre_y = y - TOP_PAD - ICAO_FS / 2
        c.setFillColor(C_WHITE)
        c.setFont(HDR_FONT_BOLD, 10)
        c.saveState()
        c.translate(cx() + APT_W / 2 + 1, badge_centre_y)
        c.rotate(90)
        c.drawCentredString(0, -3, _safe_latin1(badge_label))
        c.restoreState()

        text_x  = cx() + APT_W + PAD_X
        icao_y  = y - TOP_PAD - ICAO_FS    # top-aligned
        c.setFillColor(C_WHITE)
        c.setFont(HDR_FONT, ICAO_FS)
        c.drawString(text_x, icao_y, _safe_latin1(icao))
        after_icao = text_x + c.stringWidth(_safe_latin1(icao), HDR_FONT, ICAO_FS) + PAD_X + 2

        C_MUTED = colors.HexColor("#8BAABB")
        line_y  = y - TOP_PAD - 7

        if not role:
            # FIR block: show facility name to the right of the FIR code, all white
            if iata_name:
                c.setFont(HDR_FONT_BOLD, 7.5)
                c.setFillColor(C_WHITE)
                c.drawString(after_icao, line_y, _safe_latin1(iata_name))
        else:
            # Role may be "DEPARTURE  19 17:20 - 19 19:53" — split on double-space
            role_parts = re.split(r'\s{2,}', role, maxsplit=1)
            role_label = role_parts[0].strip()
            role_time  = role_parts[1].strip() if len(role_parts) > 1 else ""

            c.setFont(HDR_FONT_BOLD, 7.5)
            right_x = cx() + COL_W - PAD_X

            if role_time:
                # Draw time right-aligned: date digits muted, HH:MM white
                time_tokens = re.split(r'(\d{2}:\d{2})', role_time)
                full_w = c.stringWidth(_safe_latin1(role_time), HDR_FONT_BOLD, 7.5)
                tx = right_x - full_w
                for tp in time_tokens:
                    if re.match(r'^\d{2}:\d{2}$', tp):
                        c.setFillColor(C_WHITE)
                    else:
                        c.setFillColor(C_MUTED)
                    c.drawString(tx, line_y, _safe_latin1(tp))
                    tx += c.stringWidth(_safe_latin1(tp), HDR_FONT_BOLD, 7.5)
                right_x = right_x - full_w - 6

            role_w = c.stringWidth(_safe_latin1(role_label), HDR_FONT_BOLD, 7.5)
            c.setFillColor(C_WHITE)
            c.drawString(right_x - role_w, line_y, _safe_latin1(role_label))

            # Name line: "IATA - " white + city name muted
            if iata_name:
                safe_iata_name = _safe_latin1(iata_name)
                dash_idx = safe_iata_name.find(" - ")
                if dash_idx >= 0:
                    prefix = safe_iata_name[:dash_idx + 3]
                    suffix = safe_iata_name[dash_idx + 3:]
                else:
                    prefix = safe_iata_name
                    suffix = ""
                c.setFont(HDR_FONT_BOLD, 7.5)
                c.setFillColor(C_WHITE)
                c.drawString(after_icao, line_y, prefix)
                px = after_icao + c.stringWidth(prefix, HDR_FONT_BOLD, 7.5)
                if suffix:
                    c.setFillColor(C_MUTED)
                    c.drawString(px, line_y, suffix)

        line_y -= LINE_GAP

        # RWYS lines
        c.setFont(HDR_FONT_BOLD, 7)
        c.setFillColor(C_WHITE)
        for rl in rwy_lines:
            c.drawString(after_icao, line_y, _safe_latin1(rl))
            line_y -= LINE_GAP

        y -= total_h

    def draw_category_banner(label):
        """Thin steel-blue banner with centred label."""
        nonlocal y
        ensure(BH_CATEGORY + LH_TEXT * 2)
        c.setFillColor(C_CATEGORY_BG)
        c.rect(cx(), y - BH_CATEGORY, COL_W, BH_CATEGORY, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont(HDR_FONT, FS_LABEL)
        c.drawCentredString(cx() + COL_W / 2,
                            y - BH_CATEGORY + (BH_CATEGORY - FS_LABEL) / 2 + 1,
                            _safe_latin1(label))
        y -= BH_CATEGORY

    def draw_notam_entry(lines, row_index, is_expired):
        """
        Unbreakable NOTAM box. Line 0 = header (bold), line 1 = CREATED (dimmer),
        remaining lines = body text (normal).
        """
        nonlocal y
        if not lines:
            return

        col_height = TOP_Y - BOT_Y
        PAD_V      = 2   # vertical padding top+bottom combined

        # Wrap long body lines to column width (0.601 × 0.90 = ~10% tighter)
        max_chars = int(COL_W / (FS * 0.665))
        wrapped = []
        for li, ln in enumerate(lines):
            if li <= 1:
                # Header and CREATED lines — keep as-is (truncate if needed)
                wrapped.append(ln[:max_chars + 10])
            else:
                # Body — word-wrap
                if len(ln) <= max_chars:
                    wrapped.append(ln)
                else:
                    import textwrap as _tw2
                    for wl in _tw2.wrap(ln, max_chars):
                        wrapped.append(wl)

        entry_h = len(wrapped) * LH_TEXT + PAD_V

        # Truncate if taller than full column
        if entry_h > col_height:
            max_lines = int((col_height - PAD_V) / LH_TEXT) - 1
            wrapped   = wrapped[:max_lines] + ["[...]"]
            entry_h   = len(wrapped) * LH_TEXT + PAD_V

        # Move whole box to next column if it won't fit
        if y - entry_h < BOT_Y:
            advance_column()

        # Draw body background (white / expired-tint)
        body_bg = C_EXP_BODY_BG if is_expired else C_BODY_BG
        c.setFillColor(body_bg)
        c.rect(cx(), y - entry_h, COL_W, entry_h, fill=1, stroke=0)

        # Dark header band covering NOTAM ID line + CREATED line
        # Add PAD_V/2 extra below so descenders aren't clipped
        HDR_PAD    = 3
        hdr_lines  = 2 if len(wrapped) >= 2 and wrapped[1].startswith("CREATED") else 1
        hdr_h      = LH_TEXT * hdr_lines + HDR_PAD
        hdr_bg     = C_EXP_HDR_BG if is_expired else C_NOTAM_HDR_BG
        c.setFillColor(hdr_bg)
        c.rect(cx(), y - hdr_h, COL_W, hdr_h, fill=1, stroke=0)

        # Text
        text_y = y - LH_TEXT + 0.5
        for li, ln in enumerate(wrapped):
            if li == 0:
                # NOTAM ID line — bold, dark text on light band
                c.setFont(BOLD, FS_HDR)
                c.setFillColor(C_TEXT_EXP if is_expired else C_TEXT)
            elif li == 1 and ln.startswith("CREATED"):
                # CREATED line — smaller, dimmer
                c.setFont(MONO, FS - 1)
                c.setFillColor(C_TEXT_EXP if is_expired else C_TEXT_CREATED)
            else:
                c.setFont(MONO, FS)
                c.setFillColor(C_TEXT_EXP if is_expired else C_TEXT)
            c.drawString(cx() + PAD_X, text_y, _safe_latin1(ln[:max_chars + 10]))
            text_y -= LH_TEXT

        y -= entry_h
        c.setFillColor(C_TEXT)

    def draw_text_lines(text_lines):
        """Render plain text lines (weather/SIGMET body content)."""
        nonlocal y
        max_chars = int(COL_W / (FS * 0.665))
        import textwrap as _tw2
        for ln in text_lines:
            if not ln:
                continue
            wrapped = _tw2.wrap(ln, max_chars) if len(ln) > max_chars else [ln]
            for wl in wrapped:
                ensure(LH_TEXT + 1)
                c.setFont(MONO, FS)
                c.setFillColor(C_TEXT)
                c.drawString(cx() + PAD_X, y - LH_TEXT + 0.5, _safe_latin1(wl))
                y -= LH_TEXT
        # Bottom margin after text block
        y -= 5

    def draw_nil_wx(msg_line=""):
        """Compact grey NIL box: large NIL left, wrapped message text right."""
        nonlocal y
        C_NIL_BG   = colors.HexColor("#C8D0D4")
        C_NIL_TEXT = colors.HexColor("#4A5A60")
        nil_fs = 13
        msg_fs = FS - 0.5
        msg_lh = msg_fs + 2.5

        # Calculate available width for message and wrap it
        nil_w    = 28   # approx width of "NIL" at 13pt
        msg_x    = cx() + PAD_X + nil_w + 12
        max_msg_w = cx() + COL_W - PAD_X - msg_x
        # Estimate chars that fit in available width
        avg_char_w = msg_fs * 0.62
        max_chars  = max(10, int(max_msg_w / avg_char_w))

        import textwrap as _twnil
        msg_lines = _twnil.wrap(msg_line, max_chars) if msg_line else []
        n_lines   = max(1, len(msg_lines))
        NIL_H     = max(22, msg_lh * n_lines + 10)

        ensure(NIL_H + 2)
        c.setFillColor(C_NIL_BG)
        c.rect(cx(), y - NIL_H, COL_W, NIL_H, fill=1, stroke=0)

        # Large NIL, vertically centred
        c.setFillColor(C_NIL_TEXT)
        c.setFont(HDR_FONT_BOLD, nil_fs)
        c.drawString(cx() + PAD_X, y - NIL_H / 2 - nil_fs / 3, "NIL")

        # Message lines
        if msg_lines:
            c.setFont(MONO, msg_fs)
            msg_y = y - (NIL_H / 2) - (msg_lh * (n_lines - 1) / 2) - msg_fs / 3
            for ml in msg_lines:
                c.drawString(msg_x, msg_y, _safe_latin1(ml))
                msg_y -= msg_lh

        y -= NIL_H + 8

    def draw_nil_sigmet():
        """Greyed NIL box with large NIL left and small message text right."""
        nonlocal y
        C_NIL_BG   = colors.HexColor("#C8D0D4")
        C_NIL_TEXT = colors.HexColor("#4A5A60")

        # Calculate height: enough for two lines of small text
        msg_fs   = FS - 0.5
        msg_lh   = msg_fs + 2.5
        NIL_H    = max(36, msg_lh * 2 + 14)
        ensure(NIL_H + 4)

        # Grey background
        c.setFillColor(C_NIL_BG)
        c.rect(cx(), y - NIL_H, COL_W, NIL_H, fill=1, stroke=0)

        # Large "NIL" on the left, vertically centred
        nil_fs = 22
        c.setFillColor(C_NIL_TEXT)
        c.setFont(HDR_FONT_BOLD, nil_fs)
        nil_w = c.stringWidth("NIL", HDR_FONT_BOLD, nil_fs)
        c.drawString(cx() + PAD_X, y - NIL_H / 2 - nil_fs / 3, "NIL")

        # Small message lines to the right of NIL
        msg_x = cx() + PAD_X + nil_w + 14
        msg_lines = [
            "THERE ARE NO ACTIVE SIGMET FOR FIR WITHIN THE GIVEN TIME",
            "PERIOD.",
        ]
        msg_y = y - (NIL_H / 2) - msg_lh / 2 + msg_lh * (len(msg_lines) - 1) / 2
        c.setFont(MONO, msg_fs)
        for line in msg_lines:
            c.drawString(msg_x, msg_y, _safe_latin1(line))
            msg_y -= msg_lh

        y -= NIL_H + 4

    def draw_expired_divider():
        """Draw the red 'EXPIRED' divider banner between active and expired NOTAMs."""
        nonlocal y
        ensure(BH_CATEGORY + 2)
        c.setFillColor(colors.HexColor("#C8392B"))
        c.rect(cx(), y - BH_CATEGORY, COL_W, BH_CATEGORY, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont(BOLD, FS_LABEL)
        c.drawCentredString(cx() + COL_W / 2,
                            y - BH_CATEGORY + (BH_CATEGORY - FS_LABEL) / 2 + 1,
                            "--- EXPIRED ---")
        y -= BH_CATEGORY

    # ── Parse tokens ──────────────────────────────────────────────────────────
    CATEGORY_RE  = re.compile(r'^=+\s+(.+?)\s+=+$')
    AIRBLK_RE    = re.compile(r'^={10,}$')
    # Matches both airport NOTAMs  "KLAS LAS  A0209/26  2026-..."
    # and enroute NOTAMs           "ZLA   ZLA2025/0012   2025-..."
    NOTAM_HDR_RE = re.compile(r'^[A-Z]{2,5}(?:\s+[A-Z]{2,5})?\s+\S+/\d{2,4}\s+\d{4}-')

    draw_page_header()

    pages_drawn = [1]

    tokens     = []
    raw_lines  = notam_text.splitlines()
    i          = 0

    while i < len(raw_lines):
        line    = raw_lines[i]
        stripped = line.strip()

        # Airport / FIR bordered block
        if AIRBLK_RE.match(stripped):
            j           = i + 1
            header_line = ""
            rwy_lines   = []
            in_rwys     = False
            ident_line  = ""
            while j < len(raw_lines) and not AIRBLK_RE.match(raw_lines[j].strip()):
                s = raw_lines[j].strip()
                if s.startswith("ID:"):
                    ident_line = s[3:].strip()
                    in_rwys = False
                elif not header_line and s and not s.startswith("IA"):
                    header_line = s
                    in_rwys = False
                elif "RWYS" in s:
                    rwy_lines.append(s.strip())
                    in_rwys = True
                elif in_rwys and s and not s.startswith("IA"):
                    # Continuation runway line (2nd, 3rd row of pairs)
                    rwy_lines.append(s.strip())
                else:
                    in_rwys = False
                j += 1
            if j < len(raw_lines) and AIRBLK_RE.match(raw_lines[j].strip()):
                j += 1

            parts     = header_line.split(None, 1)
            icao      = parts[0] if parts else "????"
            rest      = (parts[1].strip() if len(parts) > 1 else "")

            # APT blocks: encoded as "role_field    iata_name" (4-space separator)
            # FIR blocks:  encoded as "facility name" (no 4-space gap)
            rm = re.split(r'\s{4,}', rest, maxsplit=1)
            if len(rm) == 1:
                # No 4-space gap → FIR block: whole rest is the facility name
                role      = ""
                iata_name = rest.strip()
            else:
                role      = rm[0].strip()   # role+time → rendered RIGHT
                iata_name = rm[1].strip()   # IATA - Name → rendered LEFT
            tokens.append(('airport', icao, role, iata_name, rwy_lines, ident_line))
            i = j
            continue

        # Category banner
        m = CATEGORY_RE.match(stripped)
        if m:
            tokens.append(('category', m.group(1).strip()))
            i += 1
            continue

        # Expired divider
        if '--- EXPIRED ---' in line:
            tokens.append(('expired_divider',))
            i += 1
            continue

        # NOTAM entry
        if NOTAM_HDR_RE.match(stripped):
            entry_lines = [stripped]
            i += 1
            blank_count = 0
            while i < len(raw_lines):
                nl = raw_lines[i]
                ns = nl.strip()
                if AIRBLK_RE.match(ns) or CATEGORY_RE.match(ns) or '--- EXPIRED ---' in nl:
                    break
                if NOTAM_HDR_RE.match(ns) and blank_count > 0:
                    break
                if ns == "":
                    blank_count += 1
                    if blank_count >= 2:
                        i += 1
                        break
                else:
                    blank_count = 0
                    entry_lines.append(ns)
                i += 1
            tokens.append(('notam', entry_lines))
            continue

        # NIL WX box (missing TAF / METAR / ATIS) — format: [NIL_WX optional message]
        if stripped.startswith('[NIL_WX'):
            msg = stripped[7:].rstrip(']').strip()
            tokens.append(('nil_wx', msg))
            i += 1
            continue

        # NIL SIGMET box
        if stripped == '[NIL_SIGMET]':
            tokens.append(('nil_sigmet',))
            i += 1
            continue

        # Plain text (weather body: METAR/TAF/ATIS/SIGMET content)
        if stripped:
            text_lines = [stripped]
            i += 1
            while i < len(raw_lines):
                ns = raw_lines[i].strip()
                if (AIRBLK_RE.match(ns) or CATEGORY_RE.match(ns)
                        or '--- EXPIRED ---' in raw_lines[i]
                        or NOTAM_HDR_RE.match(ns)
                        or ns == '[NIL_SIGMET]'
                        or ns.startswith('[NIL_WX')):
                    break
                text_lines.append(ns)
                i += 1
            tokens.append(('text', text_lines))
            continue

        i += 1

    # ── Render ────────────────────────────────────────────────────────────────
    c.setPageSize(landscape(letter))
    c.setFont(MONO, FS)
    y = TOP_Y

    # Draw column rule on first page
    draw_col_rule()

    notam_row_counter = 0
    in_expired        = False

    for idx, tok in enumerate(tokens):
        kind = tok[0]

        if kind == 'airport':
            _, icao, role, iata_name, rwy_lines = tok[:5]
            ident_line        = tok[5] if len(tok) > 5 else ""
            in_expired        = False
            notam_row_counter = 0
            draw_airport_banner(icao, role, iata_name, rwy_lines, ident_line)

        elif kind == 'category':
            _, label = tok
            draw_category_banner(label)

        elif kind == 'expired_divider':
            # Only draw the banner if there is at least one notam token
            # following this divider before the next airport block.
            has_expired = any(
                tokens[j][0] == 'notam'
                for j in range(idx + 1, len(tokens))
                if tokens[j][0] not in ('airport',)
            )
            if has_expired:
                in_expired        = True
                notam_row_counter = 0
                draw_expired_divider()

        elif kind == 'notam':
            _, entry_lines = tok
            draw_notam_entry(entry_lines, notam_row_counter, in_expired)
            notam_row_counter += 1

        elif kind == 'nil_wx':
            draw_nil_wx(tok[1] if len(tok) > 1 else "")

        elif kind == 'nil_sigmet':
            draw_nil_sigmet()

        elif kind == 'text':
            _, text_lines = tok
            draw_text_lines(text_lines)

    draw_col_rule()
    if footer_fn:
        footer_fn(c, LW, LH)
    c.showPage()
    return pages_drawn[0]


def build_index_page(entries):
    """
    Release index page, matching the reference layout:

        Index Page
        WBD*3936/25SEP/1427 ORF                        2
        Flight Plan                                    3
        ...

    `entries` is [(label, page), ...] as collected on the first render pass.
    Every page number is shifted by one because the index itself becomes
    page 1, and a Flight Plan entry is inserted after the WBD page.
    """
    if not entries:
        return ""
    rows = []
    for label, page in entries:
        rows.append((label, page + 1))
        if label.upper().startswith("WBD*"):
            rows.append(("Flight Plan", page + 2))
    out = "Index Page\n\n"
    for label, page in rows:
        out += f"[INDEX_ROW:{label}|{page}]\n"
    return out


# Populated by save_as_pdf on every render: [(label, page_number), ...] for the
# FOS command pages, so the release index can be built on a second pass.
LAST_INDEX_ENTRIES = []

_BANNER_RE = re.compile(r'^\*{6,}\s+(\S.*?)\s+\*{6,}$')

# Only FOS command pages belong on the index — the star banners inside the TPS
# and W&B blocks (THRUST / V-SPEED, AIRPORT ANALYSIS DATA, ...) are section
# headings, not pages the crew looks up.
_INDEX_CMD_RE = re.compile(r'^(WBD\*|NSC/|FIL?\d|SLS\*)')


def _add_index_links(path, links, page_offset=0):
    """
    Add the index page's internal links to a finished PDF.

    reportlab resolves named destinations when the document is serialised, so a
    link written before its target page exists aborts the save — and the blob
    splice path serialises mid-render, long before the later pages are drawn.
    Adding the annotations with pypdf afterwards sidesteps destination
    resolution completely: by then every page is real and addressable by index.

    `links` is [(row_page, (x0, y0, x1, y1), target_page), ...], 1-based.
    """
    if not links or PdfReader is None:
        return
    try:
        from pypdf.annotations import Link
    except Exception as exc:
        LOG.debug(f"[INDEX] pypdf Link unavailable, links skipped: {exc}")
        return
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        for pg in reader.pages:
            writer.add_page(pg)
        n_pages = len(writer.pages)

        added = 0
        for row_page, rect, target in links:
            src = row_page + page_offset - 1
            dst = target + page_offset - 1
            if not (0 <= src < n_pages):
                continue
            if not (0 <= dst < n_pages):
                LOG.warning(f"[INDEX] target page {target} outside document "
                            f"({n_pages} pages) — link skipped")
                continue
            writer.add_annotation(page_number=src,
                                  annotation=Link(rect=rect, target_page_index=dst))
            added += 1
        if added:
            with open(path, "wb") as fh:
                writer.write(fh)
            LOG.debug(f"[INDEX] {added} index links added")
    except Exception as exc:
        LOG.warning(f"[INDEX] could not add index links: {exc}")


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
        LOG.error("ERROR: No content to save")
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
        LOG.warning("No lines to write after processing")
        return

    try:
        # Start with portrait orientation
        _initial_buf = io.BytesIO()
        c = canvas.Canvas(_initial_buf, pagesize=A4)
        # Index links are collected here and written after the file is closed.
        _index_links = []
        c._output_buf = _initial_buf
        width, height = A4
        font_size = 9
        left_margin = 75
        line_height = font_size + 2   # 11pts — original spacing
        page_margin = 50
        # Image layout (separate from text margins)
        IMAGE_MARGIN = 24      # points (~0.33 inch)


        # Font selection
        user_choice = ask_font_selection()
        _RAINBOW_MODE = (user_choice == "4")
        if user_choice == "2":
            user_font_path = "/System/Library/Fonts/Menlo.ttc"
        elif user_choice == "3":
            user_font_path = _FONT_PATH_COURIER_NEW
        elif user_choice == "4":
            user_font_path = "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"
        else:  # "1" Courier Normal
            user_font_path = _FONT_PATH_COURIER_NORMAL

        font_name = None
        if user_font_path and os.path.exists(user_font_path):
            try:
                pdfmetrics.registerFont(TTFont("CustomFont", user_font_path))
                font_name = "CustomFont"
            except Exception as e:
                LOG.debug("Failed to load user font: %s", e)

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
                    except Exception:
                        continue

        if not font_name:
            font_name = "Courier"

        # NOTAM section uses the same font as the rest of the document
        _notam_font = font_name

        c.setFont(font_name, font_size)

        # Initialize page state — no graphical header offset needed
        # PAGE X RELEASE... is injected as body text at each page break
        y = height - page_margin
        page_count = 1
        lines_written = 0
        i = 0
        current_orientation = "portrait"

        global LAST_INDEX_ENTRIES
        LAST_INDEX_ENTRIES = []
        _bookmarked = set()

        # ── Release watermark footer ──────────────────────────────────────────
        # OFP ID: visible body text on page 1 AND light-grey footer on all pages
        _release_code_footer = ""
        if lines and str(lines[0]).startswith("[RELEASE_CODE:"):
            _release_code_footer = str(lines[0])[len("[RELEASE_CODE:"):-1].strip()
            lines = lines[1:]

        # ── Parse page-header template ────────────────────────────────────────
        # Only L1 repeats: "PAGE {PAGE}  RELEASE 1.0  DATE  ROUTE  FLT"
        _ph_l1_template = ""
        _ph_active      = True  # draw page header until [WEATHER_MARKER]

        # The WBD page now sits ahead of the flight plan, so the marker is no
        # longer inside the first few lines — scan the whole release and count
        # the page breaks before it so PAGE 1 lands on the flight plan.
        _ph_offset     = 0
        _ph_line_index  = -1     # where the header template sits in `lines`
        _strip_i        = 0
        while _strip_i < len(lines):
            _sl = str(lines[_strip_i]).strip()
            if "[PAGEBREAK]" in _sl and not _ph_l1_template:
                _ph_offset += 1
            if _sl.startswith("[PAGE_HEADER_L2:") or _sl.startswith("[DESK_LINE_PLACEHOLDER]"):
                lines.pop(_strip_i)  # strip silently
            elif _sl.startswith("[PAGE_HEADER_L1:"):
                # Extract template — strip the surrounding [...] cleanly
                _ph_l1_template = _safe_latin1(_sl[len("[PAGE_HEADER_L1:"):].rstrip("]").strip())
                lines[_strip_i] = _ph_l1_template.replace("{PAGE}", "1")
                _ph_line_index  = _strip_i
                break
            else:
                _strip_i += 1

        def _draw_page_header(cv, pg_w, pg_h, page_n, fn=None, fs=9):
            """No-op — PAGE header is injected as body text lines, not drawn graphically."""
            pass

        # Grab navlog page header from the parsed template (already extracted above)

        # The Flightkeys ID stops once the NOTAMs end — the DECS pages that
        # follow are FOS output and carry no release watermark.
        _wm_active = [True]

        def _draw_release_footer(cv, pg_w, pg_h):
            """Draw the OFP release code in light grey at the bottom centre of every page."""
            if not _release_code_footer or not _wm_active[0]:
                return
            cv.saveState()
            cv.setFont(font_name, font_size)
            cv.setFillColorRGB(0.72, 0.72, 0.72)   # slightly lighter than body text
            _tw  = cv.stringWidth(_release_code_footer, font_name, font_size)
            cv.drawString((pg_w - _tw) / 2, 14, _release_code_footer)
            cv.restoreState()

        def is_section_header(line):
            """Check if line is a section header (contains ***)."""
            line_str = str(line).strip()
            return '***' in line_str or line_str.startswith('===')

        # Draw header on page 1
        _draw_page_header(c, width, height, 1)

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
            line_str_raw = str(line)

            # Skip any PAGE_HEADER or placeholder marker lines
            if (line_str_raw.startswith("[PAGE_HEADER_L1:") or
                    line_str_raw.startswith("[PAGE_HEADER_L2:") or
                    line_str_raw.startswith("[DESK_LINE_PLACEHOLDER]")):
                i += 1
                continue

            # Split markers are consumed by the file-splitting logic, never drawn.
            if line_str_raw.strip() in ("[TPS_START]", "[TLR_START]"):
                i += 1
                continue

            # Every page gets a named destination so the index can link to it
            if page_count not in _bookmarked:
                _bookmarked.add(page_count)
                try:
                    c.bookmarkPage(f"pg{page_count}")
                except Exception:
                    pass

            # Index rows: blue, and clickable through to the page
            if line_str_raw.strip().startswith("[INDEX_ROW:"):
                _body = line_str_raw.strip()[len("[INDEX_ROW:"):].rstrip("]")
                _lbl, _, _pg = _body.rpartition("|")
                _txt = f"{_lbl:<44}{_pg:>4}"
                if y - line_height < page_margin:
                    _draw_release_footer(c, width, height)
                    c.showPage()
                    page_count += 1
                    c.setFont(font_name, font_size)
                    y = height - page_margin
                c.saveState()
                c.setFillColorRGB(0.05, 0.20, 0.65)
                c.setFont(font_name, font_size)
                c.drawString(left_margin, y, _safe_latin1(_txt))
                _w = c.stringWidth(_safe_latin1(_txt), font_name, font_size)
                try:
                    _index_links.append(
                        (page_count,
                         (left_margin, y - 2, left_margin + _w, y + font_size),
                         int(_pg.strip())))
                except ValueError:
                    pass
                c.restoreState()
                y -= line_height
                i += 1
                continue

            if line_str_raw.strip() == "[WATERMARK_END]":
                _wm_active[0] = False
                i += 1
                continue

            # Index bookkeeping — explicit markers are consumed, FOS command
            # banners are recorded and still drawn.
            if line_str_raw.strip().startswith("[INDEX_ENTRY:"):
                LAST_INDEX_ENTRIES.append(
                    (line_str_raw.strip()[len("[INDEX_ENTRY:"):].rstrip("]").strip(),
                     page_count))
                i += 1
                continue
            _bm = _BANNER_RE.match(line_str_raw.strip())
            if _bm and _INDEX_CMD_RE.match(_bm.group(1).strip()):
                LAST_INDEX_ENTRIES.append((_bm.group(1).strip(), page_count))

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
                _closed_portrait = y < height - page_margin
                if _closed_portrait:
                    _draw_release_footer(c, width, height)
                    c.showPage()
                _notam_pages = _draw_notam_section(
                    c, notam_text, _notam_font, font_size=8,
                    footer_fn=_draw_release_footer) or 1
                # _draw_notam_section ends with its own showPage()
                width, height = A4
                current_orientation = "portrait"
                c.setPageSize(A4)
                c.setFont(font_name, font_size)
                y = height - page_margin
                # The landscape pages the NOTAM renderer just produced have to
                # be counted, or every page number after the NOTAMs — and the
                # index that points at them — comes out short.
                page_count += _notam_pages + (1 if _closed_portrait else 0)
                continue

            # ── Navlog table renderer ─────────────────────────────────────────
            if "[NAVLOG_START]" in str(line):
                navlog_lines = []
                i += 1
                while i < len(lines) and "[NAVLOG_END]" not in str(lines[i]):
                    navlog_lines.append(lines[i])
                    i += 1
                i += 1  # skip [NAVLOG_END]
                if y < height - page_margin:
                    _draw_release_footer(c, width, height)
                    c.showPage()
                # Build correct page header: replace "PAGE 1" with actual navlog page number
                _navlog_page_num = max(1, page_count + 1 - _ph_offset)
                _navlog_page_offset = [0]  # mutable so inner fn can increment it

                def _draw_navlog_page_header(cv, pg_w, pg_h, _fn=font_name,
                                             _tmpl=_ph_l1_template, _base=_navlog_page_num):
                    _pn  = _base + _navlog_page_offset[0]
                    _txt = _tmpl.replace("{PAGE}", str(_pn)) if _tmpl else f"PAGE {_pn}"
                    _navlog_page_offset[0] += 1
                    cv.saveState()
                    cv.setFont(_fn, 8)
                    cv.setFillColorRGB(0, 0, 0)
                    cv.drawString(79, pg_h - 36, _txt)
                    cv.restoreState()

                _draw_navlog_table(c, navlog_lines, font_name, font_size=font_size,
                                   footer_fn=_draw_release_footer,
                                   header_fn=_draw_navlog_page_header)
                # _draw_navlog_table ends with its own showPage()
                width, height = A4
                current_orientation = "portrait"
                c.setPageSize(A4)
                c.setFont(font_name, font_size)
                page_count += 1
                # Pages after navlog (signature, winds) still need the OFP header
                _draw_page_header(c, width, height, page_count)
                y = height - page_margin
                continue

            # ── Pre-rendered PDF blob (e.g. ETOPS / Oceanic pages) ────────────
            if str(line).startswith("[PDF_BLOB:"):
                if not _PYPDF_AVAILABLE:
                    LOG.warning("pypdf not installed — skipping ETOPS blob pages.")
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
                    _writer = PdfWriter()
                    _pre_data = _pre_buf.read()
                    if _pre_data:
                        _rdr_main = PdfReader(io.BytesIO(_pre_data))
                        for _pg in _rdr_main.pages:
                            _writer.add_page(_pg)
                    _rdr_blob = PdfReader(io.BytesIO(blob_bytes))
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
                    c = canvas.Canvas(_new_buf, pagesize=A4)
                    c._output_buf = _new_buf
                    c._pdf_prefix_pages = _saved_prefix
                    c.setFont(font_name, font_size)
                    width, height = A4
                    current_orientation = "portrait"
                    y = height - page_margin
                    page_count += 1
                    i += 1
                    continue
                except Exception as _blob_err:
                    LOG.warning(f"Could not splice PDF blob: {_blob_err}")

                    traceback.print_exc()
                i += 1
                continue
            # ── Weather marker — stop drawing release footer after this point ──
            if "[WEATHER_MARKER]" in str(line):
                _ph_active     = False
                i += 1
                continue

            if "[PAGEBREAK]" in str(line):
                if y < height - page_margin:  # only turn page if content was drawn
                    _draw_release_footer(c, width, height)
                    c.showPage()
                if current_orientation == "landscape":
                    width, height = A4
                    current_orientation = "portrait"
                    c.setPageSize(A4)
                c.setFont(font_name, font_size)
                page_count += 1
                y = height - page_margin
                # Inject page header as body text at top of new page
                if _ph_active and _ph_l1_template and i > _ph_line_index:
                    _hdr_line = _ph_l1_template.replace("{PAGE}", str(max(1, page_count - _ph_offset)))
                    lines.insert(i + 1, _hdr_line)
                    lines.insert(i + 2, "")  # blank line after header
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

                    # Start image page
                    _draw_release_footer(c, width, height)
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
                        c.drawString(IMAGE_MARGIN, y, _safe_latin1(title))
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
                    LOG.warning(f"Could not embed image from {url}: {e}")
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
                    _draw_release_footer(c, width, height)
                    c.showPage()
                    if current_orientation == "landscape":
                        width, height = A4
                        current_orientation = "portrait"
                        c.setPageSize(A4)
                    c.setFont(font_name, font_size)
                    page_count += 1
                    y = height - page_margin
                    # Inject page header as body text at top of new page
                    if _ph_active and _ph_l1_template and i > _ph_line_index:
                        _hdr_line = _ph_l1_template.replace("{PAGE}", str(max(1, page_count - _ph_offset)))
                        lines.insert(i, _hdr_line)
                        lines.insert(i + 1, "")

            # Draw text line
            try:
                line_str = (str(line) if line is not None else "").rstrip("\n\r")
                # Solid horizontal rule — draw a PDF line, not dashes
                if line_str.strip() == "[HRULE]":
                    c.setStrokeColorRGB(0, 0, 0)
                    c.setLineWidth(0.5)
                    c.line(left_margin, y + font_size * 0.3, left_margin + 480, y + font_size * 0.3)
                    y -= line_height
                    i += 1
                    continue
                # Sanitize: ReportLab's built-in Courier/Helvetica are Latin-1.
                # Characters above U+00FF render as blank squares.
                line_str = _safe_latin1(line_str)
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
                LOG.warning(f"Could not write line {i+1}: {e}")
                c.drawString(left_margin, y, f"[LINE {i+1} ERROR]")

            y -= line_height
            i += 1

        # Save canvas and write final PDF to disk
        c.save()  # flush canvas to _output_buf
        _prefix_pages = 0
        if hasattr(c, '_pdf_prefix_pages') and hasattr(c, '_output_buf'):
            try:
                # pypdf imported at module level
                _final = PdfWriter()
                c._pdf_prefix_pages.seek(0)
                for _pg in PdfReader(c._pdf_prefix_pages).pages:
                    _final.add_page(_pg)
                    _prefix_pages += 1
                c._output_buf.seek(0)
                for _pg in PdfReader(c._output_buf).pages:
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
        _add_index_links(filename, _index_links, page_offset=_prefix_pages)
        LOG.info(f"PDF saved: {filename}")
        LOG.debug(f"[DBG: Pages created: {page_count}")
        LOG.debug(f"[DBG: Total lines written: {lines_written}")

    except ImportError as e:
        LOG.error(f"ERROR: Missing library: {e}. Install reportlab and Pillow.")
    except Exception as e:
        LOG.error(f"ERROR: Failed to create PDF: {e}")
        traceback.print_exc()

from tkinter import filedialog
# ===========================================================================
# Output folder & user identity
# Where the finished OFP PDF is written, and who it is stamped for.
# ===========================================================================


def get_last_output_folder():
    """Return the last-used output folder from config, or None if unset/missing."""
    folder = _load_config().get("output_folder", "")
    return folder if folder and os.path.exists(folder) else None

def save_last_output_folder(folder):
    """Persist *folder* as the last-used output folder in config.json."""
    cfg = _load_config()
    cfg["output_folder"] = folder
    _save_config(cfg)

def prompt_for_output_folder():
    """Open a Tk folder-picker dialog and return the selected path (persisted to config)."""
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
    """Standalone test helper — fetches a live SimBrief plan and logs the result.

    Call from a REPL or add ``if __name__ == "__main__": test_howgozit_generation()``
    to exercise the full pipeline without running ``main()``.
    """
    user_id = get_or_prompt_username()
    try:
        xml_data = fetch_simbrief_data(user_id)
        LOG.debug("XML data fetched successfully")

        takeoff_time = "1234"  # override as needed
        howgozit = parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time)

        if howgozit:
            LOG.info("test_howgozit_generation OK — %d chars generated", len(str(howgozit)))
        else:
            LOG.warning("test_howgozit_generation returned empty result")
        return howgozit

    except Exception as e:
        LOG.error("test_howgozit_generation failed: %s", e)
        traceback.print_exc()
        return None


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
        LOG.error("No username provided. Exiting.")
        sys.exit(1)
    uid = uid.strip()
    cfg = _load_config()
    cfg["user_id"] = uid
    _save_config(cfg)
    return uid
# ===========================================================================
# Formatting helpers
# Pure functions: time, fuel and numeric formatting for fixed-width output.
# ===========================================================================





def format_alt_time(seconds):
    """Convert a seconds integer to a zero-padded HHMM string; returns '0000' on failure."""
    if seconds is None:
        return "0000"
    try:
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:02d}{minutes:02d}"
    except Exception:
        return "0000"





# ===========================================================================
# SimBrief fetch & XML parsing
# Network fetch, XML helpers, runway extraction, and the main OFP builder.
# ===========================================================================


















# --- XML Parsing Function ---

# --- XML Text Extraction Helper ---

def parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time, gate="", arr_gate="", generation=0):
    """
    Parse a SimBrief XML string and build the full OFP text blob.

    Parameters
    ----------
    xml_data : str
        Raw XML from the SimBrief API.
    takeoff_time : str
        Scheduled or actual off-blocks time in HH:MM or HHMM format.

    Returns
    -------
    tuple
        (howgozit_text, flight_info, valid_runways, anti_ice_on, runway_lines)
        or None on parse failure.
    """
    try:
        root = ET.fromstring(xml_data)
        # Local `get_text` shadows the module-level helper intentionally here:
        # it binds `root` and avoids passing it as an argument to every call.
        get_text = lambda path, default="": (
            (root.find(path).text or default).strip() if root.find(path) is not None else default
        )
        get_all = lambda path: [el.text.strip() if el.text else '' for el in root.findall(path)]

        # Basic flight info
        valid_runways, flight_info, anti_ice_on, runway_lines = extract_runway_data(root)
        icao_airline = get_text("general/icao_airline").strip()
        flight_number = ''.join(c for c in get_text("general/flight_number").strip() if c.isalnum() or c in '-/ ')
        origin = get_text("origin/icao_code")
        origin_iata = get_text("origin/iata_code")
        origin_elev = get_text("origin/elevation")
        destination = get_text("destination/icao_code")
        destination_iata = get_text("destination/iata_code")
        destination_elev = get_text("destination/elevation")
        # ── Collect ALL alternates from XML (SimBrief may return 2+) ────────────
        _altn_nodes = root.findall('alternate')
        # Build a list of dicts, one per alternate, in XML order
        _alternates = []
        for _an in _altn_nodes:
            _a_icao = (_an.findtext('icao_code') or '').strip()
            _a_iata = (_an.findtext('iata_code') or '').strip()
            if not _a_icao:
                continue  # skip empty placeholders
            try:
                _a_burn = int(float(_an.findtext('burn') or '0'))
            except (ValueError, TypeError):
                _a_burn = 0
            try:
                _a_ete  = int(_an.findtext('ete')  or '0')
            except (ValueError, TypeError):
                _a_ete  = 0
            try:
                _a_dist = int(float(_an.findtext('distance') or '0'))
            except (ValueError, TypeError):
                _a_dist = 0
            _a_route = (_an.findtext('route_ifps') or _an.findtext('route') or '').strip()
            _a_fl    = (_an.findtext('cruise_altitude') or '').strip()
            _alternates.append({
                'icao': _a_icao,
                'iata': _a_iata,
                'burn': _a_burn,
                'ete':  _a_ete,
                'dist': _a_dist,
                'route': _a_route,
                'fl':   _a_fl,
            })

        # Legacy single-alternate shorthands (kept for backward compat)
        altn_iata = _alternates[0]['iata'] if _alternates else get_text('alternate/iata_code', "NONE")
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
        except Exception:
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
                LOG.error(f"ERROR in safe_add_time_to_takeoff: {e}")
                return "____"

        # Calculate planned arrival time
        pln_arr_time = safe_add_time_to_takeoff(takeoff_time_sec, est_enroute_time_sec)

        time_diff = calculate_time_difference(sched_on_sec, est_in_sec)

        # Format additional values - ADD SAFETY CHECKS
        def safe_format_off_time(time_value):
            """Safely format off time. Accepts int (seconds) or HHMM string."""
            try:
                if time_value is None:
                    return "----"
                if isinstance(time_value, int):
                    # Convert seconds-since-midnight to HHMM string
                    time_value = f"{(time_value % 86400) // 3600:02d}{((time_value % 86400) % 3600) // 60:02d}"
                return format_off_time(str(time_value))
            except Exception as e:
                LOG.debug(f"format_off_time: {e}")
                return "----"

        def safe_format_out_time(time_value):
            """Safely format out time."""
            try:
                if time_value is None or time_value == "":
                    return "----"
                return format_out_time(str(time_value))
            except Exception as e:
                LOG.error(f"ERROR in format_out_time: {e}")
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

        LOG.debug(f"[DBG: takeoff_time_sec = {takeoff_time_sec}")
        LOG.debug(f"[DBG: on_time_pln = {on_time_pln}")
        LOG.debug(f"[DBG: All time_data values: {time_data}")

        # Additional debugging to catch the exact error location
        for key, value in time_data.items():
            if value is None:
                LOG.warning(f"{key} is None")
            if isinstance(value, int):
                LOG.debug(f"time_data key {key!r} is int: {value}")

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
        cptn = get_text("crew/cpt", "N/A")
        fo = get_text("crew/fo", "N/A")
        PID = get_text("crew/pilot_id", "N/A")
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
            "TANKER":     0,
        }

        time_dict = {}  # optional time per bucket

        # ── E/RSV (contingency) — populate from XML fuel/contingency ────────────
        # contingency_fuel is fetched above but was never written into fuel_dict,
        # so the ladder always showed zero.  Fix: seed the dict entry here so that
        # the ladder section can read it via fuel_dict.get("E/RSV", 0).
        try:
            _ersv_lbs = int(float(contingency_fuel or 0))
        except (ValueError, TypeError):
            _ersv_lbs = 0
        # Build E/RSV label with percentage if contpct is set
        try:
            _cont_pct_val = float(get_text('api_params/contpct') or '0')
            _ersv_label = f"E/RSV {_cont_pct_val * 100:.1f}PCT" if _cont_pct_val > 0 else "E/RSV"
        except (ValueError, TypeError):
            _ersv_label = "E/RSV"
        if _ersv_lbs > 0:
            fuel_dict[_ersv_label] = _ersv_lbs
            _ersv_t_raw = get_text("times/contfuel_time", "0") or "0"
            _ersv_t_secs = int(_ersv_t_raw) if _ersv_t_raw.isdigit() else 0
            if _ersv_t_secs > 0:
                time_dict[_ersv_label] = format_time_elapsed(_ersv_t_secs)

        # ETOPS fuel sits in fuel/etops and is part of fuel/min_takeoff; like
        # contingency it was fetched nowhere, so the ladder was short by the
        # whole ETOPS allowance on any flight that carried one.
        try:
            _etops_lbs_x = int(float(get_text('fuel/etops') or 0))
        except (ValueError, TypeError):
            _etops_lbs_x = 0
        if _etops_lbs_x > 0:
            fuel_dict["ETOPS ADD"] = _etops_lbs_x
            _et_t_raw = get_text("times/etopsfuel_time", "0") or "0"
            if _et_t_raw.isdigit() and int(_et_t_raw) > 0:
                time_dict["ETOPS ADD"] = format_time_elapsed(int(_et_t_raw))

        _required_buckets = set()
        _seen_extra_labels = set()
        fuel_extra_section = root.find("fuel_extra")
        if fuel_extra_section is not None:
            # Only blank labels are noise. "EXTRA" and "TANKERING" were listed
            # here, which skipped them before the mapping below could route them
            # to DISP ADD and TANKER — so tankered fuel silently vanished from
            # the ladder and RLS FUEL no longer reconciled with MIN T/O + TAXI.
            _noise_labels = {""}
            # SimBrief marks each extra bucket required or optional, and
            # fuel/min_takeoff already contains the required ones:
            #   min_takeoff = burn + cont + rsv + altn + etops + extra_required
            # A required bucket therefore belongs ABOVE the MIN T/O line and an
            # optional one below it. Getting this backwards double-counts.
            _required_buckets = set()
            for bucket in fuel_extra_section.findall("bucket"):
                label = (bucket.findtext("label") or "").strip().upper()
                if label in _noise_labels:
                    continue  # suppress before any further processing
                _seen_extra_labels.add(label)
                fuel_raw = (bucket.findtext("fuel") or "0").strip()
                time_raw = (bucket.findtext("time") or "").strip()

                try:
                    fuel_val = float(fuel_raw)
                except ValueError:
                    fuel_val = 0
                if fuel_val == 0:
                    continue  # skip zero buckets

                time_fmt = format_time_elapsed(int(time_raw)) if time_raw.isdigit() else ""
                _is_required = bool((bucket.findtext("required") or "").strip())

                # map XML labels to OFP labels
                if label in ("MEL",):
                    fuel_dict["MEL"] += fuel_val
                    if _is_required:
                        _required_buckets.add("MEL")
                    if time_fmt:
                        time_dict["MEL"] = time_fmt
                elif label == "EXTRA":
                    # Discretionary extra — it belongs to DISP EXTRA below the
                    # MIN T/O line, not DISP ADD above it. The bucket used to be
                    # filtered out as noise, which hid this mapping.
                    fuel_dict["DISP EXTRA"] += fuel_val
                    if time_fmt:
                        time_dict["DISP EXTRA"] = time_fmt
                elif label in ("ATC", "WXX"):
                    fuel_dict["HOLD"] += fuel_val
                    if _is_required:
                        _required_buckets.add("HOLD")
                    if time_fmt:
                        time_dict["HOLD"] = time_fmt
                elif label in ("FOD ADD", "FOB ADD"):
                    fuel_dict["DISP EXTRA"] += fuel_val
                    if _is_required:
                        _required_buckets.add("DISP EXTRA")
                    if time_fmt:
                        time_dict["DISP EXTRA"] = time_fmt
                elif label in ("TANKER", "TANKERING"):
                    fuel_dict["TANKER"] += fuel_val
                    if time_fmt:
                        time_dict["TANKER"] = time_fmt
                elif label in ("ACF90", "ACF 90", "ACF_90"):
                    fuel_dict["ACF90"] = fuel_dict.get("ACF90", 0) + fuel_val
                    if time_fmt:
                        time_dict["ACF90"] = time_fmt
                elif label in ("ACF99", "ACF 99", "ACF_99", "PBCF"):
                    fuel_dict["ACF99"] = fuel_dict.get("ACF99", 0) + fuel_val
                    if time_fmt:
                        time_dict["ACF99"] = time_fmt
                else:
                    # Skip known noise labels that have no OFP representation
                    if label not in ("EXTRA", "TANKERING"):
                        fuel_dict[label] = fuel_val
                        if _is_required:
                            _required_buckets.add(label)
                        if time_fmt:
                            time_dict[label] = time_fmt


        # Get weight and performance data
        try:
            ramp_weight = get_text('weights/est_ramp', '0')
            ramp_weight_formatted = f"{int(ramp_weight):06d}" if ramp_weight else "000000"
        except Exception:
            ramp_weight_formatted = "000000"

        # Calculate required OEI cruise altitude from highest MORA
        # Also track the fix name and fuel burnt at the most-limiting point
        try:
            all_fixes = root.findall("navlog/fix")
            max_mora = 0
            _oei_limiting_fix = ""
            _oei_limiting_fuel = 0   # fuel_totalused at the limiting fix (lbs)
            for fix in all_fixes:
                mora_text = fix.findtext("mora", "0")
                try:
                    mora_value = float(mora_text)
                    if mora_value > max_mora:
                        max_mora = mora_value
                        _oei_limiting_fix = (fix.findtext("ident") or fix.findtext("name") or "").strip().upper()
                        try:
                            _oei_limiting_fuel = int(float(fix.findtext("fuel_totalused") or "0"))
                        except (ValueError, TypeError):
                            _oei_limiting_fuel = 0
                except (ValueError, TypeError):
                    continue

            # Round MORA up to nearest 1000
            if max_mora > 0:
                oei_cruise_alt = int(math.ceil(max_mora / 1000.0) * 1000)
            else:
                oei_cruise_alt = 10000

        except Exception as e:
            LOG.warning(f"Could not calculate OEI altitude: {e}")
            oei_cruise_alt = 18000  # Fallback to default
            _oei_limiting_fix = ""

        # Build enhanced HOWGOZIT with OFP header - INITIALIZE AS EMPTY STRING
        howgozit = ""

        # ── Build header lines (repeated on every page until weather) ─────────
        try:
            _hdr_ts = int(get_text("times/sched_out") or "0")
            _hdr_dt = datetime.fromtimestamp(_hdr_ts, tz=timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        except Exception:
            _hdr_dt = datetime.now(timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        _hdr_route = f"{origin}-{destination}"
        _hdr_flt   = ''.join(c for c in f"{icao_airline.strip()}{flight_number.strip()}" if c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789- /').strip()

        # Page header: use plan generation time and release number from XML
        try:
            _gen_ts  = int(get_text("params/time_generated") or "0")
            _hdr_dt  = datetime.fromtimestamp(_gen_ts, tz=timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        except Exception:
            _hdr_dt = datetime.now(timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        # "RELEASE 6.7" is two independent counters, not a decimal version.
        # The integer part is SimBrief's own release number for this flight;
        # the digit after the point is how many times WE have generated a
        # release off it, so a dispatcher comparing two printouts of the
        # same SimBrief release can tell which one came out later. It is a
        # single digit and wraps 9 -> 0 (see `generation` on this function).
        _rls_num  = (get_text("general/release") or "1").strip()
        try:
            _rls_ver = f"{int(_rls_num)}.{int(generation) % 10}"
        except (ValueError, TypeError):
            _rls_ver = _rls_num
        _hdr_route = f"{origin}-{destination}"
        _hdr_flt   = ''.join(c for c in f"{icao_airline}{flight_number}" if c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789- /')

        # Template for page header line — {PAGE} replaced per page by save_as_pdf
        _ph_template = f"PAGE {{PAGE}}  RELEASE {_rls_ver}  {_hdr_dt}  {_hdr_route}  {_hdr_flt}"

        # Inject L1 marker so save_as_pdf repeats the header on every page
        howgozit += f"[PAGE_HEADER_L1:{_ph_template}]\n"
        howgozit += "\n"  # blank line after PAGE header

        # Desk line deferred — written after ACF key resolves below
        _DESK_LINE_PLACEHOLDER = "[DESK_LINE_PLACEHOLDER]\n\n"
        howgozit += _DESK_LINE_PLACEHOLDER

        # IFR line — release suffix is departure day (DD), strip leading zero
        try:
            # UTC, not the machine's local time — a Tokyo 0350Z departure came
            # out as /11 on a US-local clock while the page header said AUG-12.
            _dep_day = str(int(datetime.fromtimestamp(
                int(get_text("times/sched_out") or "0"),
                tz=timezone.utc).strftime("%d")))
        except Exception:
            _dep_day = ""
        _ifr_flt = f"{icao_airline}{flight_number}/{_dep_day}" if _dep_day else f"{icao_airline}{flight_number}"
        # Build ALTN suffix: 'ALTN GDL QRO' from all alternates
        _altn_iata_list = [a['iata'] for a in _alternates if a['iata']]
        _altn_sfx = f"  ALTN {' '.join(_altn_iata_list)}" if _altn_iata_list else ""
        # T/O ALTN: extract from ATC FPL TALT field
        import re as _re
        _fpl_text = get_text('atc/flightplan_text') or ""
        _toaltn_m = _re.search(r'TALT/([A-Z]{4})', _fpl_text)
        _toaltn_iata = ""
        if _toaltn_m:
            _toaltn_icao = _toaltn_m.group(1)
            # Convert ICAO to IATA (strip leading K for US airports, else use ICAO)
            _toaltn_iata = _toaltn_icao[1:] if _toaltn_icao.startswith('K') and len(_toaltn_icao) == 4 else _toaltn_icao
        _toaltn_sfx = f"  T/O ALTN {_toaltn_iata}" if _toaltn_iata else ""
        howgozit += f"- IFR {_ifr_flt}  {fin}/{aircraft_reg} {origin_iata}  {destination_iata}{_altn_sfx}{_toaltn_sfx}\n"
        _tanker_lbs = int(float(fuel_dict.get("TANKER", 0)))
        _tanker_sfx = f"  TANKER {_tanker_lbs:05d}" if _tanker_lbs > 0 else ""
        # SimBrief's plan_ramp already contains any tankered fuel; the release
        # ladder shows RLS FUEL without it and adds TANKER back in at TOTAL.
        _rls_fuel_lbs = max(0, int(float(plan_ramp)) - _tanker_lbs)
        howgozit += f" MIN T/O FUEL {int(float(min_takeoff)):06d}  RLS FUEL {_rls_fuel_lbs:06d}{_tanker_sfx}\n"
        _paf_lbs_hdr  = int(get_text('fuel/plan_landing') or 0)
        _paf_flow     = int(get_text('fuel/avg_fuel_flow') or 1) or 1
        _paf_res_s    = int(get_text('times/reserve_time') or 0)
        _paf_altn_s   = int(get_text('alternate/ete') or 0) or \
                        int(int(get_text('fuel/alternate_burn') or 0) / _paf_flow * 3600)
        _paf_etops_s  = int(get_text('times/etopsfuel_time') or 0)
        _paf_extra_s  = int(get_text('times/extrafuel_time') or 0)
        _paf_cont_s   = int(get_text('times/contfuel_time') or 0)
        _paf_secs     = _paf_res_s + _paf_altn_s + _paf_etops_s + _paf_extra_s + _paf_cont_s
        _paf_hrs_hdr  = _paf_secs // 3600
        _paf_min_hdr  = (_paf_secs % 3600) // 60
        howgozit += f" TOT BRN {enroute_burn}  PLAN ARR FUEL {_paf_lbs_hdr}  {_paf_hrs_hdr:02d}HR/{_paf_min_hdr:02d}MIN  COST INDEX: {cost_index:0>3}\n"
        howgozit += "\n"  # blank line after TOT BRN block
        altn_route = get_text('alternate/route')
        _route_str = get_text('general/route') or ""
        if _route_str:
            _route_wrapped = textwrap.fill(
                f"{origin} {_route_str} {destination}",
                width=50, subsequent_indent="",
                break_long_words=False, break_on_hyphens=False
            )
            howgozit += f"ROUTE: {_route_wrapped}\n"
        # Emit ALTN RTE / ALT2 RTE for every alternate in XML order
        for _ai, _a in enumerate(_alternates):
            if _a['route']:
                _rte_prefix = "ALTN RTE" if _ai == 0 else f"ALT{_ai+1} RTE"
                _fl_val = int(float(_a['fl'])) // 100
                _fl_sfx = f"FL{_fl_val:03d} / " if _a['fl'] and _a['fl'] != '0' else ""
                howgozit += f"{_rte_prefix} - {_fl_sfx}{_a['route']} {_a['icao']}\n"
        howgozit += "\n"  # blank line after route block

        # ── SECTION ORDER RESTRUCTURE ──────────────────────────────────────────
        # Immediately after IFR header: fuel ladder, then RAMP WT, then PLD,
        # then ON TIME ANALYSIS, then ENGINE OUT DRIFTDOWN, then [PAGEBREAK],
        # then MEL/RMKS, then ATC FPL, then [PAGEBREAK], then NAVLOG,
        # then signature + winds.
        # ── Fuel ladder helper formatters ──────────────────────────────────────
        _resvrule    = (get_text("api_params/resvrule") or "").strip().upper()
        _added_raw   = (get_text("api_params/addedfuel")       or "0").strip()
        _added_units = (get_text("api_params/addedfuel_units") or "min").strip().lower()
        _added_lbl   = (get_text("api_params/addedfuel_label") or "").strip().upper()

        if not _added_lbl:
            _acf_display_key = None
        else:
            _PBCF_RULES = {"B343", "B747", "B757", "B767", "B777", "B787",
                           "A330", "A340", "A350", "A380"}
            _is_pbcf_rule = any(_resvrule.startswith(r) for r in _PBCF_RULES) or "PBCF" in _resvrule
            if _is_pbcf_rule:
                _acf_display_key = f"{_resvrule} PBCF"
            elif "99" in _added_lbl:
                _acf_display_key = "ACF99"
            elif "90" in _added_lbl:
                _acf_display_key = "ACF90"
            elif _added_lbl in ("PBCF", "ACF"):
                _acf_display_key = "ACF99"
            elif _added_lbl in ("EXTRA", "TANKERING"):
                _acf_display_key = None   # generic fuel labels, not a fleet/ACF type
            else:
                _acf_display_key = _added_lbl

        # Only synthesise an ACF/PBCF figure when SimBrief has not accounted for
        # it. A bucket reported as zero is an explicit "none", not a gap — and
        # deriving lbs from api_params/addedfuel in that case invents fuel that
        # is in no total, which is what broke the ladder on RJTT-KLAX.
        _acf_reported = bool(_seen_extra_labels & {
            (_added_lbl or "").upper(), "ACF90", "ACF99", "PBCF",
            (_acf_display_key or "").upper()})
        if _acf_display_key and not _acf_reported:
            _acf_already = fuel_dict.get(_acf_display_key, None)
            if _acf_already is None:
                try:
                    _added_val = float(_added_raw)
                except Exception:
                    _added_val = 0.0
                try:
                    _avg_flow = float(get_text("fuel/avg_fuel_flow") or "0") or 0.0
                except Exception:
                    _avg_flow = 0.0
                if _added_val > 0 and _avg_flow > 0:
                    if _added_units == "min":
                        _acf_lbs  = (_added_val / 60.0) * _avg_flow
                        _acf_secs = int(round(_added_val * 60))
                    else:
                        _acf_lbs  = _added_val
                        _acf_secs = int(round(_acf_lbs / _avg_flow * 3600))
                else:
                    _acf_lbs = 0.0; _acf_secs = 0
                fuel_dict[_acf_display_key] = _acf_lbs
                _acf_t = format_time_elapsed(_acf_secs)
                if _acf_t:
                    time_dict[_acf_display_key] = _acf_t

        # ── Now ACF is resolved — build and inject the desk line ─────────────
        # For PBCF rules, the value may be stored under ACF99 from fuel_extra buckets
        # or directly in fuel/pbcf XML field
        if _acf_display_key and float(fuel_dict.get(_acf_display_key, 0)) == 0:
            # The fuel_extra bucket arrives as ACF90/ACF99; moving it onto the
            # rule's display name is a relabel, not a synthesis, so it happens
            # whether or not SimBrief reported the bucket.
            for _src in ("ACF99", "ACF90"):
                if float(fuel_dict.get(_src, 0)) > 0:
                    fuel_dict[_acf_display_key] = fuel_dict.pop(_src)
                    if _src in time_dict:
                        time_dict[_acf_display_key] = time_dict.pop(_src)
                    break
            else:
                # fuel/pbcf only — fuel/etops belongs to the ETOPS row.
                _pbcf_xml = float(get_text("fuel/pbcf") or "0") if not _acf_reported else 0.0
                if _pbcf_xml > 0:
                    fuel_dict[_acf_display_key] = _pbcf_xml
                    _pbcf_t_raw = get_text("times/pbcffuel_time") or "0"
                    _pbcf_t = format_time_elapsed(int(_pbcf_t_raw)) if _pbcf_t_raw.isdigit() else ""
                    if _pbcf_t:
                        time_dict[_acf_display_key] = _pbcf_t

        _acf_active = _acf_display_key and (float(fuel_dict.get(_acf_display_key, 0)) > 0)
        _is_domestic = (
            origin.upper().startswith("K")
            and destination.upper().startswith("K")
        )
        _is_international = (
            not origin.upper().startswith("K")
            and not destination.upper().startswith("K")
        )
        _cfg_desk   = f"FD{random.randint(1, 99):02d}"
        _desk_phone = "800-555-0199"
        _desk_right = f"DESK: {_cfg_desk}    PHONE: {_desk_phone}"
        if _is_domestic:
            _desk_label = "US DOMESTIC"
        elif _acf_display_key:
            # ACF/PBCF fuel is carried as the enroute reserve, which is an
            # international reserve rule — the flight type reads with it. This
            # follows the rule in force, not whether this particular leg ended
            # up carrying any ACF fuel.
            _desk_label = f"{_acf_display_key} INTERNATIONAL"
        else:
            _desk_label = "FLAG"
        _desk_pad  = max(1, 68 - len(_desk_label) - len(_desk_right))
        _desk_line = f"{_desk_label}{' ' * _desk_pad}{_desk_right}"
        howgozit = howgozit.replace(_DESK_LINE_PLACEHOLDER, f"{_desk_line}\n\n", 1)

        try:
            _bufr_delta = max(0, int(float(plan_takeoff)) - int(float(min_takeoff)))
            _da_val   = fuel_dict.get("DISP ADD", 0)
            _hold_val = fuel_dict.get("HOLD", 0)
            _total_disc = _da_val + _hold_val
            if _bufr_delta > 0 and _total_disc > 0:
                disp_add_bufr = min(int(_da_val   / _total_disc * _bufr_delta), int(_da_val))
                hold_bufr     = min(int(_hold_val / _total_disc * _bufr_delta), int(_hold_val))
                if disp_add_bufr + hold_bufr > _bufr_delta and hold_bufr > 0:
                    hold_bufr = _bufr_delta - disp_add_bufr
            else:
                disp_add_bufr = hold_bufr = 0
        except Exception:
            disp_add_bufr = hold_bufr = 0

        _paf_lbs_ldr = int(get_text('fuel/plan_landing') or 0)
        _paf_flow2   = int(get_text('fuel/avg_fuel_flow') or 1) or 1
        _paf_res_s2  = int(get_text('times/reserve_time') or 0)
        _paf_altn_s2 = int(get_text('alternate/ete') or 0) or \
                       int(int(get_text('fuel/alternate_burn') or 0) / _paf_flow2 * 3600)
        _paf_etops_s2 = int(get_text('times/etopsfuel_time') or 0)
        _paf_xtra_s2 = int(get_text('times/extrafuel_time') or 0)
        _paf_cont_s2 = int(get_text('times/contfuel_time') or 0)
        _paf_secs2   = (_paf_res_s2 + _paf_altn_s2 + _paf_etops_s2
                        + _paf_xtra_s2 + _paf_cont_s2)
        _paf_hhmm    = f"{_paf_secs2 // 3600:02d}{(_paf_secs2 % 3600) // 60:02d}"

        def fz6(v):
            """5-digit fuel value — SimBrief standard for ladder rows."""
            try:   return f"{int(round(float(v))):05d}"
            except: return "00000"

        def fz_hdr(v):
            """Six-digit fuel value — PLAN ARR FUEL, ENRT BRN, MIN T/O,
            RLS FUEL and TOTAL. The component rows use five (see _row)."""
            try:   return f"{int(round(float(v))):06d}"
            except: return "000000"

        def fz5(v):
            try:   return f"{int(round(float(v))):05d}"
            except: return "00000"

        SEP = "--------------------------------------------------------------------"

        # ── FUEL LADDER ───────────────────────────────────────────────────────
        _rdist = int(float(get_text('general/route_distance') or 0))
        # ── Row formatter: label(19) fuel(6) time(4) [dist(4)] [BUFR(5)] ───────
        def _row(label, fuel, time_="", dist=None, bufr=None):
            f = f"{int(float(fuel or 0)):05d}"   # 5 digits — aligns with ENRT BRN and MIN T/O
            t = f"  {time_:>4}" if time_ else ""  # 2 spaces before time → time at col 27
            d = f"  {int(dist):04d}" if dist is not None else ""
            b = f"       BUFR {fz5(bufr)}" if bufr else ""
            return f"{label:<19}{f}{t}{d}{b}\n"

        # Reserve-rule fuel (B343 PBCF, ACF90/99) is a required component on
        # some rules and discretionary on others. SimBrief settles it in the
        # arithmetic: whatever fuel/min_takeoff holds beyond burn + cont + rsv +
        # altn + etops is required, so if the ACF figure fits in that gap it
        # belongs above the line with the rest of the minimum.
        _acf_val = int(float(fuel_dict.get(_acf_display_key, 0) or 0)) \
                   if _acf_display_key else 0
        try:
            _core_min = sum(int(float(get_text(k) or 0)) for k in
                            ('fuel/enroute_burn', 'fuel/contingency', 'fuel/reserve',
                             'fuel/alternate_burn', 'fuel/etops'))
            _req_flagged = sum(int(float(fuel_dict.get(b, 0) or 0))
                               for b in _required_buckets)
            _req_gap = int(float(min_takeoff)) - _core_min - _req_flagged
        except Exception:
            _req_gap = 0
        _acf_is_required = bool(_acf_val) and _req_gap >= _acf_val

        howgozit += f"{'PLAN ARR FUEL':<19}{fz_hdr(_paf_lbs_ldr)}  {_paf_hhmm}\n"
        howgozit += SEP + "\n"
        howgozit += "         ARPT      FUEL    TIME  DIST\n"
        howgozit += f"{'ENRT BRN ' + destination_iata:<19}{fz_hdr(enroute_burn)}  {enroute_time}  {_rdist:04d}\n"
        howgozit += SEP + "\n"

        # ENROUTE RESERVE — the first row under ENRT BRN. Under an ACF/PBCF
        # reserve rule the ACF fuel *is* the enroute reserve, so it takes this
        # slot under the rule's name; otherwise it is the contingency figure.
        if _acf_is_required and _acf_val > 0:
            howgozit += _row(_acf_display_key, _acf_val,
                             time_dict.get(_acf_display_key, ""))
        elif fuel_dict.get(_ersv_label, 0):
            howgozit += _row(_ersv_label, fuel_dict.get(_ersv_label, 0),
                             time_dict.get(_ersv_label, ""))

        howgozit += _row("RSV", reserve_fuel, reserve_time)

        # MAF — always shown
        _maf_fuel = fuel_dict.get("MAF", 0)
        _maf_time = time_dict.get("MAF", "0000")
        howgozit += _row("MAF", _maf_fuel, _maf_time)

        # DISP ADD — always shown
        _da_fuel = fuel_dict.get("DISP ADD", 0)
        _da_time = time_dict.get("DISP ADD", "0000")
        howgozit += _row("DISP ADD", _da_fuel, _da_time, bufr=disp_add_bufr if disp_add_bufr else None)

        # ALTN rows — one per alternate, all from _alternates list
        for _ai, _a in enumerate(_alternates):
            _lbl_pfx = "ALTN" if _ai == 0 else f"-ALT{_ai+1}"
            _altn_lbl = f"{_lbl_pfx}     {_a['iata']:<3}"
            _a_t  = format_time_elapsed(_a['ete']) if _a['ete'] > 0 else "0000"
            _a_d  = _a['dist'] if _a['burn'] > 0 and _a['dist'] > 0 else None
            howgozit += _row(_altn_lbl, _a['burn'], _a_t, dist=_a_d)

        # ETOPS ADD
        _etops_add_lbs = fuel_dict.get("ETOPS ADD", 0)
        if _etops_add_lbs:
            howgozit += _row("ETOPS", _etops_add_lbs, time_dict.get("ETOPS ADD", ""))

        # HOLD — above the line only when SimBrief flags it required
        _hf_fuel = fuel_dict.get("HOLD", 0)
        _hf_time = time_dict.get("HOLD", "")
        if _hf_fuel and "HOLD" in _required_buckets:
            howgozit += _row("HOLD", _hf_fuel, _hf_time, bufr=hold_bufr if hold_bufr else None)

        # Required extras (MEL and friends) are inside fuel/min_takeoff, so they
        # are itemised above the line, not below it with the discretionary fuel.
        _mel_fuel = int(float(fuel_dict.get("MEL", 0) or 0))
        if _mel_fuel and "MEL" in _required_buckets:
            howgozit += _row("MEL", _mel_fuel, time_dict.get("MEL", ""))

        howgozit += SEP + "\n"
        howgozit += f"{'MIN T/O':<19}{fz_hdr(min_takeoff)}\n"
        howgozit += SEP + "\n"

        # ── Above-minimum block ───────────────────────────────────────────
        # MIN T/O + these rows + TAXI must equal RLS FUEL. SimBrief buckets do
        # not always account for the whole difference, so whatever is left over
        # is carried as BASELINE rather than silently breaking the ladder.
        try:
            _taxi_for_ladder = int(float(get_text('fuel/taxi') or '0'))
        except Exception:
            _taxi_for_ladder = 0

        _de_fuel = int(float(fuel_dict.get("DISP EXTRA", 0) or 0))
        _de_time = time_dict.get("DISP EXTRA", "0000")
        _mel_for_sum = (int(float(fuel_dict.get("MEL", 0) or 0))
                        if "MEL" not in _required_buckets else 0)
        _hold_for_sum = (int(float(fuel_dict.get("HOLD", 0) or 0))
                         if "HOLD" not in _required_buckets else 0)
        _acf_for_sum = 0 if _acf_is_required else _acf_val
        _other_for_sum = 0
        _skip_pre = {_acf_display_key, _ersv_label, "E/RSV", "DISP ADD", "ETOPS ADD",
                     "HOLD", "DISP EXTRA", "MEL", "TANKER", "TANKERING", "RSV",
                     "MAF", "EXTRA", "ACF90", "ACF99", None}
        _skip_pre |= _required_buckets
        for _k in fuel_dict:
            if _k not in _skip_pre:
                _other_for_sum += int(float(fuel_dict.get(_k, 0) or 0))

        _above_min = (_rls_fuel_lbs - _taxi_for_ladder - int(float(min_takeoff)))
        _baseline  = _above_min - (_de_fuel + _mel_for_sum + _hold_for_sum
                                   + _acf_for_sum + _other_for_sum)
        if _baseline > 0:
            try:
                _bl_flow = float(get_text('fuel/avg_fuel_flow') or 0) or 0.0
                _bl_secs = int(_baseline / _bl_flow * 3600) if _bl_flow > 0 else 0
                _bl_time = format_time_elapsed(_bl_secs) if _bl_secs else ""
            except Exception:
                _bl_time = ""
            howgozit += _row("BASELINE", _baseline, _bl_time)
        elif _baseline < 0:
            # Print the whole reconciliation so the offending bucket is obvious
            # rather than just the size of the discrepancy.
            _bkt = ", ".join(f"{k}={int(float(v or 0))}"
                             for k, v in sorted(fuel_dict.items())
                             if int(float(v or 0)) != 0) or "none"
            LOG.warning(
                f"[FUEL-LADDER] above-min block over-accounts by {-_baseline} lbs\n"
                f"    plan_ramp={int(float(plan_ramp))} tanker={_tanker_lbs} "
                f"rls={_rls_fuel_lbs} taxi={_taxi_for_ladder} "
                f"min_t/o={int(float(min_takeoff))} -> above_min={_above_min}\n"
                f"    counted: disp_extra={_de_fuel} mel={_mel_for_sum} "
                f"hold={_hold_for_sum} required={sorted(_required_buckets)} "
                f"acf[{_acf_display_key}]={_acf_for_sum} other={_other_for_sum}\n"
                f"    buckets: {_bkt}")

        # ACF/PBCF/APU — discretionary, so it belongs above MIN T/O in value
        # but below it in the ladder, alongside the other extras.
        if _acf_val > 0 and not _acf_is_required:
            howgozit += _row(_acf_display_key, _acf_val,
                             time_dict.get(_acf_display_key, ""))

        # DISP EXTRA — always shown
        howgozit += _row("DISP EXTRA", _de_fuel, _de_time)

        # MEL — only when it is discretionary
        if _mel_fuel and "MEL" not in _required_buckets:
            howgozit += _row("MEL", _mel_fuel, time_dict.get("MEL", ""))

        # HOLD — discretionary ATC/WXX allowance
        if _hf_fuel and "HOLD" not in _required_buckets:
            howgozit += _row("HOLD", _hf_fuel, _hf_time,
                             bufr=hold_bufr if hold_bufr else None)

        # Any remaining unknown non-zero buckets (exclude known + noise labels)
        _skip = {_acf_display_key, _ersv_label, "E/RSV", "DISP ADD", "ETOPS ADD", "HOLD",
                 "DISP EXTRA", "MEL", "TANKER", "TANKERING", "RSV", "MAF",
                 "EXTRA", "ACF90", "ACF99", None}
        for label in sorted(k for k in fuel_dict if k not in _skip):
            fuel_val = fuel_dict.get(label, 0)
            if fuel_val:
                howgozit += _row(label, fuel_val, time_dict.get(label, ""))

        howgozit += SEP + "\n"
        try:
            _taxi_lbs = int(float(get_text('fuel/taxi') or '0'))
        except Exception:
            _taxi_lbs = 0
        howgozit += _row("TAXI     " + origin_iata, _taxi_lbs, taxi_out_fmt)
        howgozit += f"{'RLS FUEL ' + origin_iata:<19}{fz_hdr(_rls_fuel_lbs)}\n"
        howgozit += SEP + "\n"
        _tanker_fuel = int(float(fuel_dict.get("TANKER", 0)))
        _tanker_time = time_dict.get("TANKER", "")
        if _tanker_fuel > 0:
            howgozit += _row("TANKER", _tanker_fuel, _tanker_time)
        howgozit += f"{'TOTAL    ' + origin_iata:<19}{fz_hdr(_rls_fuel_lbs + _tanker_fuel)}\n"
        howgozit += "\n"  # blank line after TOTAL

        # ── Time helpers needed for ON TIME ANALYSIS ─────────────────────────
        sched_off = format_time_elapsed(get_text("times/sched_off", "0"))
        sched_on  = format_time_elapsed(get_text("times/sched_on",  "0"))

        if 'takeoff_time_sec' in locals() and takeoff_time_sec:
            actual_takeoff_hours   = takeoff_time_sec // 3600
            actual_takeoff_minutes = (takeoff_time_sec % 3600) // 60
            actual_off_fmt = f"{actual_takeoff_hours:02d}{actual_takeoff_minutes:02d}"
        else:
            actual_off_fmt = est_off_fmt.replace(":", "").replace("Z", "")

        def convert_to_local_time(utc_time_str, timezone_offset):
            if not utc_time_str or len(utc_time_str.rstrip('Z')) < 4:
                return "0000"
            if utc_time_str.endswith('Z'):
                utc_time_str = utc_time_str[:-1]
            try:
                hours   = int(utc_time_str[:2])
                minutes = int(utc_time_str[2:])
            except ValueError:
                return "0000"
            local_hours = (hours + timezone_offset) % 24
            if local_hours < 0:
                local_hours += 24
            return f"{local_hours:02d}{minutes:02d}"

        orig_timezone = get_text('times/orig_timezone')
        dest_timezone = get_text('times/dest_timezone')
        sched_out_local = convert_to_local_time(sched_out_fmt, int(float(orig_timezone or '0')))
        sched_in_local  = convert_to_local_time(sched_in_fmt,  int(float(dest_timezone or '0')))
        est_out_local   = convert_to_local_time(est_out_hhmm,  int(float(orig_timezone or '0')))
        est_in_local    = convert_to_local_time(est_in_fmt,    int(float(dest_timezone or '0')))

        # ── RAMP WT ±1000 — computed here, written after PAYLOAD ─────────────
        try:
            _enrt_secs  = int(get_text("times/est_time_enroute") or get_text("times/sched_time_enroute") or "0")
            _enrt_hh    = _enrt_secs // 3600
            _enrt_mm    = (_enrt_secs % 3600) // 60
            _enrt_hhmm  = f"{_enrt_hh:02d}:{_enrt_mm:02d}"
        except Exception:
            _enrt_hhmm  = "00:00"
        zfw_plus  = root.find("impacts/zfw_plus_1000")
        zfw_minus = root.find("impacts/zfw_minus_1000")
        _ramp_wt_lines = ""
        if zfw_plus is not None:
            _bd_p   = abs(int(zfw_plus.findtext("burn_difference", "0"))) * 2
            _te_p   = int(zfw_plus.findtext("time_enroute", "0") or "0")
            _th_p   = _te_p // 3600
            _tm_p   = (_te_p % 3600) // 60
            _ramp_wt_lines += f"RAMP WT P2000 TIME {_th_p:02d}:{_tm_p:02d} FUEL P{_bd_p}\n"
        if zfw_minus is not None:
            _bd_m   = abs(int(zfw_minus.findtext("burn_difference", "0"))) * 2
            _te_m   = int(zfw_minus.findtext("time_enroute", "0") or "0")
            _th_m   = _te_m // 3600
            _tm_m   = (_te_m % 3600) // 60
            _ramp_wt_lines += f"RAMP WT M2000 TIME {_th_m:02d}:{_tm_m:02d} FUEL M{_bd_m}\n"

        # ── ON TIME ANALYSIS ──────────────────────────────────────────────────
        _ota_sep = "----------------------------------"
        header_line = "       TXO   AIR   TXI   TOTAL      DEP GMT/LCL  ARR GMT/LCL"
        line_skdblk = (
            f"SKDBLK {taxi_out_fmt}  {sched_enrt_fmt}  {taxi_in_fmt}  {SKD_BLK}"
            f"  SKD  {sched_out_fmt}Z/{sched_out_local}L  {sched_in_fmt}Z/{sched_in_local}L"
        )
        line_flipln = (
            f"FLIPLN {taxi_out_fmt}  {enrt_fmt}  {taxi_in_fmt}  {EST_BLK}"
            f"  EST  {est_out_hhmm}Z/{est_out_local}L  {est_in_fmt}Z/{est_in_local}L"
        )
        howgozit += (
            f"ON TIME ANALYSIS\n"
            f"{_ota_sep}\n"
            f"{header_line}\n"
            f"{line_skdblk}\n"
            f"{line_flipln}\n"
            f"\n\n"  # 2 blank lines after FLIPLN
        )

        _bias_pct = "0.0"
        try:
            _ff_raw = float(get_text("fuel/fuelfactor") or get_text("general/fuelfactor") or "1.00")
            _bias_pct = f"{(_ff_raw - 1.0) * 100:.1f}"
        except Exception:
            _bias_pct = "0.0"
        howgozit += f"BIAS {_bias_pct}PCT ENDURNC {endurnc}\n"
        howgozit += f"{_ota_sep}\n"
        howgozit += "\n"  # blank line after BIAS block

        # PAYLOAD / RAMP WT
        # Weights are zero-padded to six digits on the real release
        howgozit += (f"PAYLOAD {int(payload)} "
                     f"ZFW {int(float(get_text('weights/est_zfw') or 0)):06d} "
                     f"RAMP WT {int(float(ramp_weight)):06d} "
                     f"PTOW {int(float(est_tow)):06d}\n")
        howgozit += f"AVG WIND DIR/COMP {avg_wind_dir}/{avg_wind_spd} AVG TD {avg_temp_dev:0>3}  CI{cost_index:0>4}\n"
        howgozit += "\n\n"  # 2 blank lines after PAYLOAD
        howgozit += _ramp_wt_lines
        howgozit += "\n"  # blank line after RAMP WT before ENGINE OUT

        # ── ENGINE OUT DRIFTDOWN ──────────────────────────────────────────────
        howgozit += "ENGINE OUT DRIFTDOWN\n"

        # ── OEI Driftdown Registry ────────────────────────────────────────────
        # Each entry: ICAO_codes tuple -> dict with keys "ai_off", "eng_ai_on", "wing_ai_on"
        # Each sub-table: { gross_weight_lb: (ISA+10&below, ISA+15, ISA+20) }
        # To add a new fleet: add an entry below with the ICAO codes and table data.
        _OEI_REGISTRY = {
            # ── Boeing 737 (NG + MAX) ──────────────────────────────────
            ('B736', 'B737', 'B738', 'B739', 'B38M', 'B39M'): {
                "ai_off": {
                    180000: (19100, 17900, 16600),
                    170000: (20600, 19600, 18300),
                    160000: (22000, 21100, 20000),
                    150000: (23500, 22600, 21600),
                    140000: (25100, 24200, 23300),
                    130000: (27200, 26200, 25100),
                    120000: (29300, 28500, 27400),
                    110000: (31300, 30600, 29700),
                    100000: (33300, 32700, 31800),
                },
                "eng_ai_on": {
                    180000: (18300, 17100, 15800),
                    170000: (19800, 18800, 17500),
                    160000: (21400, 20400, 19300),
                    150000: (22800, 22000, 20900),
                    140000: (24500, 23600, 22600),
                    130000: (26500, 25500, 24400),
                    120000: (28800, 27700, 26700),
                    110000: (30900, 30000, 29000),
                    100000: (32900, 32200, 31300),
                },
                "wing_ai_on": {
                    180000: (15900, 14500, 12600),
                    170000: (17700, 16300, 14900),
                    160000: (19500, 18100, 16700),
                    150000: (21000, 19900, 18400),
                    140000: (22600, 21600, 20300),
                    130000: (24400, 23400, 22200),
                    120000: (26400, 25400, 24200),
                    110000: (28700, 27700, 26500),
                    100000: (30900, 30000, 29000),
                },
            },
            # ── Airbus A319 ──────────────────────────────────────────
            ('A319',): {
                "ai_off": {
                    150000: (21700, 20900, 19800),
                    140000: (23600, 22900, 21900),
                    130000: (25500, 24900, 24100),
                    120000: (27500, 26900, 26200),
                    110000: (29600, 29100, 28500),
                    100000: (31800, 31300, 30900),
                },
                "eng_ai_on": {
                    150000: (21100, 19900, 18700),
                    140000: (23000, 22100, 21000),
                    130000: (24900, 24200, 23200),
                    120000: (27000, 26300, 25400),
                    110000: (29100, 28500, 27900),
                    100000: (31300, 30900, 30200),
                },
                "wing_ai_on": {
                    150000: (19600, 18300, 17300),
                    140000: (21600, 20500, 19200),
                    130000: (23700, 22700, 21600),
                    120000: (25800, 24900, 23900),
                    110000: (28000, 27300, 26300),
                    100000: (30300, 29600, 28800),
                },
            },
            # ── Airbus A321 ──────────────────────────────────────────
            ('A321',): {
                "ai_off": {
                    195000: (18900, 17900, 16900),
                    185000: (20200, 19300, 18000),
                    175000: (21500, 20600, 19500),
                    165000: (22900, 21900, 20900),
                    155000: (24300, 23300, 22200),
                    145000: (25900, 24800, 23600),
                    135000: (27700, 26500, 25200),
                    125000: (29500, 28400, 27000),
                    115000: (31400, 30300, 28900),
                },
                "eng_ai_on": {
                    195000: (17900, 16800, 15900),
                    185000: (19200, 18000, 17000),
                    175000: (20600, 19500, 18200),
                    165000: (21900, 22000, 19800),  # note: ISA+15 anomaly in source
                    155000: (23200, 22100, 21100),
                    145000: (24700, 23500, 22500),
                    135000: (26400, 25000, 23900),
                    125000: (28100, 26700, 25500),
                    115000: (30000, 28600, 27300),
                },
                "wing_ai_on": {
                    195000: (16700, 15800, 14400),
                    185000: (17800, 16900, 16000),
                    175000: (19200, 18000, 17200),
                    165000: (20600, 19600, 18400),
                    155000: (21800, 20900, 20000),
                    145000: (23200, 22200, 21300),
                    135000: (24600, 23600, 22700),
                    125000: (26200, 25100, 24100),
                    115000: (27900, 26800, 25700),
                },
            },
            # ── Airbus A321 ──────────────────────────────────────────
            ('A21N',): {
                "ai_off": {
                    195000: (18900, 17900, 16900),
                    185000: (20200, 19300, 18000),
                    175000: (21500, 20600, 19500),
                    165000: (22900, 21900, 20900),
                    155000: (24300, 23300, 22200),
                    145000: (25900, 24800, 23600),
                    135000: (27700, 26500, 25200),
                    125000: (29500, 28400, 27000),
                    115000: (31400, 30300, 28900),
                },
                "eng_ai_on": {
                    195000: (17900, 16800, 15900),
                    185000: (19200, 18000, 17000),
                    175000: (20600, 19500, 18200),
                    165000: (21900, 22000, 19800),  # note: ISA+15 anomaly in source
                    155000: (23200, 22100, 21100),
                    145000: (24700, 23500, 22500),
                    135000: (26400, 25000, 23900),
                    125000: (28100, 26700, 25500),
                    115000: (30000, 28600, 27300),
                },
                "wing_ai_on": {
                    195000: (16700, 15800, 14400),
                    185000: (17800, 16900, 16000),
                    175000: (19200, 18000, 17200),
                    165000: (20600, 19600, 18400),
                    155000: (21800, 20900, 20000),
                    145000: (23200, 22200, 21300),
                    135000: (24600, 23600, 22700),
                    125000: (26200, 25100, 24100),
                    115000: (27900, 26800, 25700),
                },
            },
        }

        # ── Shared interpolation helper ───────────────────────────────────
        def _interp_metow(table, col_idx, required_alt, struct_mtow):
            """Return max engine-out takeoff weight via linear interpolation of OEI table."""
            _sw = sorted(table.keys(), reverse=True)
            _result = _sw[-1]
            for _i, _w in enumerate(_sw):
                _alt_w = table[_w][col_idx]
                if _alt_w >= required_alt:
                    # Heaviest tabulated weight already clears required_alt -- no
                    # OEI restriction applies. Report the true structural MTOW,
                    # not the table's own top sampling weight; the two are only
                    # the same number if someone set the table up that way.
                    _result = struct_mtow if _i == 0 else _w
                    break
                else:
                    if _i + 1 < len(_sw):
                        _w_lo   = _sw[_i + 1]
                        _alt_lo = table[_w_lo][col_idx]
                        _alt_hi = _alt_w
                        if _alt_lo != _alt_hi:
                            _frac   = (required_alt - _alt_hi) / (_alt_lo - _alt_hi)
                            _result = int(round(_w + _frac * (_w_lo - _w)))
                        else:
                            _result = _w_lo
                    else:
                        _result = _sw[-1]
                    break
            return min(_result, struct_mtow)

        # ── Resolve aircraft type and pick registry entry ────────────────────
        _dd_actype = get_text("aircraft/icaocode", "").upper().replace("-", "").replace(" ", "")
        _dd_entry  = None
        for _icao_tuple, _dd_tables in _OEI_REGISTRY.items():
            if _dd_actype in _icao_tuple:
                _dd_entry = _dd_tables
                break
        _is_known_dd = _dd_entry is not None

        try:
            _struct_mtow = int(float(max_tow_struct or max_tow or 0))
        except Exception:
            _struct_mtow = 0

        if _struct_mtow > 0:
            howgozit += "M1 INFORMATION\n"

            if _is_known_dd and max_mora > 0:
                # Select anti-ice sub-table based on flight plan anti-ice state
                if anti_ice_on == "wing":
                    _ai_key = "wing_ai_on"
                    _ai_label = "ENG & WING A/I ON"
                elif anti_ice_on:
                    _ai_key = "eng_ai_on"
                    _ai_label = "ENG A/I ON"
                else:
                    _ai_key = "ai_off"
                    _ai_label = "A/I OFF"
                _dd_table = _dd_entry.get(_ai_key) or _dd_entry.get("ai_off", {})

                _m1_clear_alt = int(math.ceil(max_mora / 100.0) * 100)
                try:
                    _isa_dev = float(avg_temp_dev or "0")
                except Exception:
                    _isa_dev = 0.0
                col_idx = 0 if _isa_dev <= 10 else (1 if _isa_dev <= 15 else 2)
                _isa_col_label = ("ISA+10 & BELOW" if col_idx == 0 else ("ISA+15" if col_idx == 1 else "ISA+20"))

                _metow = _interp_metow(_dd_table, col_idx, _m1_clear_alt, _struct_mtow)

                try:
                    _wt_at_fix = int(float(ramp_weight or 0)) - _oei_limiting_fuel
                except Exception:
                    _wt_at_fix = None
                LOG.info(f"[METW] OEI calc: actype={_dd_actype} ({_ai_label}) max_mora={int(max_mora)} "
                         f"clearance_alt={_m1_clear_alt}ft ISA_dev={_isa_dev:+.1f}C "
                         f"col={_isa_col_label} struct_MTOW={_struct_mtow} "
                         f"-> METW={_metow} | "
                         f"fix={_oei_limiting_fix} fuel_burnt={_oei_limiting_fuel} "
                         f"wt_at_fix={_wt_at_fix}")
            else:
                # Unknown type or no significant terrain
                if max_mora <= 9999:
                    _metow = _struct_mtow
                else:
                    try:
                        _metow = min(_struct_mtow, int(float(ramp_weight or 0)))
                    except Exception:
                        _metow = _struct_mtow

            howgozit += f" METW - {_metow}\n"
            if max_mora <= 9999:
                howgozit += " MOST LIMITING POINT - NONE - NO SIGNIFICANT TERRAIN\n"
            else:
                _lim_pt = _oei_limiting_fix if _oei_limiting_fix else "TERRAIN"
                howgozit += f" MOST LIMITING POINT - {_lim_pt} - MIN CRUISE ALT {oei_cruise_alt} FT\n"
        else:
            howgozit += "NOT AVAILABLE\n"
        # OFP ID drawn as light-grey footer on every page — no body text needed

        # ── PAGE BREAK ───────────────────────────────────────────────────────
        howgozit += "[PAGEBREAK]\n"

        # ── MEL / CDL / NEF / SEL / RMKS / ACFT RSTR ─────────────────────────
        howgozit += "MEL ITEMS: - NONE\n\n"
        howgozit += "CDL ITEMS: - NONE\n\n"
        howgozit += "NEF ITEMS: - NONE\n\n"

        # SEL DATABASE
        SEL_DATABASE = {
            "A319-111": ["03", "09", "12", "21", "32"],
            "A319-132": ["03", "09", "12", "21", "32"],
            "A319-115(WL)": ["03", "10", "11", "22", "32"],
            "A320-214": ["03", "09", "12", "21", "32"],
            "A320-232": ["03", "09", "12", "20", "32"],
            "A321-211": ["03", "09", "12", "21", "32"],
            "A321-211(WL)": ["03", "10", "12", "21", "32"],
            "A321-231": ["03", "10", "12", "21", "32", "25"],
            "A321-231(WL)": ["03", "10", "12", "21", "25"],
            "A321-253N": ["03", "10", "13", "18", "23", "31", "32", "34"],
            "A321-253NY": ["03", "10", "13", "18", "23", "31", "32", "34"],
            "A321-271NY": ["03", "10", "13", "18", "23", "31", "32", "34"],
            "A321-272NX": ["03", "10", "13", "22", "23", "31", "32", "34"],
            "A321-253NX": ["03", "10", "13", "18", "23", "31", "32", "34"],
        }
        SEL_DESCRIPTIONS = {
            "03": "RUNWAY OVERRUN PREVENTION SYSTEM",
            "09": "RNAV/RNP OR RNP/AR .3 CERT",
            "10": "RNAV/RNP OR RNP/AR .1 CERT",
            "11": "AUTOLAND MAX ELEVATION 9200 FT MSL",
            "12": "AUTOLAND MAX ELEVATION 5750 FT MSL",
            "13": "AUTOLAND MAX ELEVATION 6500 FT MSL",
            "14": "AUTOLAND MAX ELEVATION 2500 FT MSL",
            "17": "NON-STANDARD LIVERY",
            "18": "OVERWATER: ETOPS",
            "20": "NO OVERWATER: MAX 50 NM FROM SHORE",
            "21": "LTD EXTD OVRWTR: MAX 50/100/162NM",
            "22": "OVRWTR: MAX 400 NM FRM ADEQUATE ARPT",
            "23": "AP/FD TCAS",
            "25": "THRUST BUMP UNAVAILABLE",
            "26": "PWS NOT INSTALLED",
            "29": "SPD BRK LTD WITH AFT CG OR OVERWT",
            "31": "FMS S8 INSTALLED",
            "32": "AUTO BRAKE OFF CALLOUT INSTALLED",
            "34": "FLS INSTALLED",
        }
        # Fleets outside the Airbus narrowbody list carry their own SEL items,
        # written out in full rather than as coded entries.
        SEL_FLEET_ITEMS = {
            "E170": ["E7502 WIFI ATG4 INSTALLED",
                     "E7503 CABIN AC ELECT OUTLETS - ISPS",
                     "E7504 LIMITED OVERWATER OPS EQUIPPED",
                     "E7505 TCAS V7.1",
                     "E7506 RA C-BAND FILTER"],
            "E175": ["E7502 WIFI ATG4 INSTALLED",
                     "E7503 CABIN AC ELECT OUTLETS - ISPS",
                     "E7504 LIMITED OVERWATER OPS EQUIPPED",
                     "E7505 TCAS V7.1",
                     "E7506 RA C-BAND FILTER"],
            "E190": ["E9502 WIFI ATG4 INSTALLED",
                     "E9503 CABIN AC ELECT OUTLETS - ISPS",
                     "E9505 TCAS V7.1",
                     "E9506 RA C-BAND FILTER"],
            "E195": ["E9502 WIFI ATG4 INSTALLED",
                     "E9503 CABIN AC ELECT OUTLETS - ISPS",
                     "E9505 TCAS V7.1",
                     "E9506 RA C-BAND FILTER"],
            "E145": ["E4502 LIMITED OVERWATER OPS EQUIPPED",
                     "E4505 TCAS V7.1"],
            "E135": ["E4502 LIMITED OVERWATER OPS EQUIPPED",
                     "E4505 TCAS V7.1"],
            "B738": ["737 03 RUNWAY OVERRUN PREVENTION SYSTEM",
                     "737 09 RNAV/RNP OR RNP/AR .3 CERT",
                     "737 12 AUTOLAND MAX ELEVATION 5750 FT MSL",
                     "737 23 AP/FD TCAS"],
            "B38M": ["737 03 RUNWAY OVERRUN PREVENTION SYSTEM",
                     "737 10 RNAV/RNP OR RNP/AR .1 CERT",
                     "737 18 OVERWATER: ETOPS",
                     "737 23 AP/FD TCAS"],
            "B752": ["757 09 RNAV/RNP OR RNP/AR .3 CERT",
                     "757 18 OVERWATER: ETOPS"],
            "B763": ["767 10 RNAV/RNP OR RNP/AR .1 CERT",
                     "767 18 OVERWATER: ETOPS"],
            "B772": ["777 10 RNAV/RNP OR RNP/AR .1 CERT",
                     "777 18 OVERWATER: ETOPS",
                     "777 23 AP/FD TCAS"],
            "B712": ["717 09 RNAV/RNP OR RNP/AR .3 CERT",
                     "717 21 LTD EXTD OVRWTR: MAX 50/100/162NM"],
            "DH8D": ["DH8 21 LTD EXTD OVRWTR: MAX 50/100/162NM"],
            "CRJ9": ["CRJ 09 RNAV/RNP OR RNP/AR .3 CERT",
                     "CRJ 21 LTD EXTD OVRWTR: MAX 50/100/162NM"],
            "A339": ["330 10 RNAV/RNP OR RNP/AR .1 CERT",
                     "330 18 OVERWATER: ETOPS"],
        }

        def _resolve_sel(name, icao_type):
            """Exact type match first, then the fleet table, then a prefix match."""
            if name in SEL_DATABASE:
                return [f"320 {c} {SEL_DESCRIPTIONS.get(c, 'UNKNOWN SEL ITEM')}"
                        for c in SEL_DATABASE[name]]
            key = (icao_type or "").strip().upper()
            if key in SEL_FLEET_ITEMS:
                return list(SEL_FLEET_ITEMS[key])
            # Tolerate variant suffixes: 'A321-253NX(WL)' → 'A321-253NX' → 'A321-253N'
            base = (name or "").split('(')[0].strip()
            for _k in sorted(SEL_DATABASE, key=len, reverse=True):
                if base.startswith(_k) or _k.startswith(base):
                    return [f"320 {c} {SEL_DESCRIPTIONS.get(c, 'UNKNOWN SEL ITEM')}"
                            for c in SEL_DATABASE[_k]]
            for _k, _items in SEL_FLEET_ITEMS.items():
                if key.startswith(_k[:3]):
                    return list(_items)
            return []

        sel_items = _resolve_sel(aircraft_type,
                                 get_text("aircraft/icao_code") or
                                 get_text("aircraft/base_type"))
        if sel_items:
            howgozit += "SEL ITEMS:\n"
            for sel in sel_items:
                howgozit += f"{sel}\n"
            howgozit += "\n"
        else:
            howgozit += "SEL ITEMS: - NONE\n\n"

        howgozit += "TAC ITEMS: NONE\n\n"

        howgozit += "RMKS/\n"
        for _rmk in dx_rmks:
            howgozit += f"{_rmk}\n"
        howgozit += "\n"
        howgozit += "ACFT RSTR: - NONE\n\n"

        # ── FF filing address line ────────────────────────────────────────────
        _fir_orig     = get_text('atc/fir_orig') or ""
        _fir_dest     = get_text('atc/fir_dest') or ""
        _fir_enrts    = [e.text.strip() for e in root.findall('atc/fir_enroute')
                         if e.text and e.text.strip()]
        _ff_seen = set()
        _ff_addrs = []
        for _fir in [_fir_orig] + _fir_enrts + [_fir_dest]:
            if _fir and _fir not in _ff_seen:
                _ff_seen.add(_fir)
                _ff_addrs.append(_fir + "ZQZX")
        if _ff_addrs:
            howgozit += "FF " + " ".join(_ff_addrs) + "\n"

        # ── ATC FLIGHT PLAN ───────────────────────────────────────────────────
        for _ai, _a in enumerate(_alternates):
            if _a['route']:
                _rte_prefix = "ALTN RTE" if _ai == 0 else f"ALT{_ai+1} RTE"
                _fl_val = int(float(_a['fl'])) // 100
                _fl_sfx = f"FL{_fl_val:03d} / " if _a['fl'] and _a['fl'] != '0' else ""
                howgozit += f"{_rte_prefix} - {_fl_sfx}{_a['route']} {_a['icao']}\n"
        if _alternates:
            howgozit += "\n"
        if fpl:
            howgozit += fpl.strip() + "\n"
        howgozit += "\n\n"

        # ── NAVLOG ───────────────────────────────────────────────────────────
        # Note: no [PAGEBREAK] here — the navlog renderer manages its own
        # page transition via showPage(), so inserting one would create a
        # blank page between the FPL block and the first navlog page.
        nav_log = write_navigation_log(root, flight_info, takeoff_time)
        if nav_log:
            howgozit += "[NAVLOG_START]\n" + nav_log + "[NAVLOG_END]\n"

        # ── FLIGHT PROGRESS REPORT ───────────────────────────────────────────
        # Format matches SimBrief page 8: ident / time / FL+MACH / FOB
        howgozit += "[PAGEBREAK]\n"
        try:
            _fp_plan_ramp   = int(float(get_text('fuel/plan_ramp') or '0'))
            _fp_taxi        = int(float(get_text('fuel/taxi')      or '0'))
            _fp_plan_ldg    = int(float(get_text('fuel/plan_landing') or '0'))
            _fp_sched_out   = int(get_text('times/sched_out') or '0')
            _fp_sched_off   = int(get_text('times/sched_off') or '0')
            _fp_sched_in    = int(get_text('times/sched_in')  or '0')
            _fp_est_in      = int(get_text('times/est_in')    or '0')
            _fp_est_on      = int(get_text('times/est_on')    or '0')
            _fp_dest_tz     = int(float(get_text('times/dest_timezone') or '0'))
            _fp_flt         = f"{icao_airline}{flight_number}"
            _fp_late_min    = (_fp_est_in - _fp_sched_in) // 60

            def _fp_utc(ts):
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%H%M')
            def _fp_local(ts, tz):
                return datetime.fromtimestamp(ts + tz * 3600, tz=timezone.utc).strftime('%H%M')

            # Header block
            howgozit += f"- FLT {_fp_flt} {origin_iata}-{destination_iata}"
            howgozit += f"   SKD ARR {_fp_local(_fp_sched_in, _fp_dest_tz)}L\n"
            howgozit += f"{'':26}ETA      {_fp_local(_fp_est_in, _fp_dest_tz)}L\n"
            howgozit += f"{'':26}LATE     {abs(_fp_late_min):04d}\n"
            howgozit += "\n"
            howgozit += f"{'OUT':<6} {_fp_utc(_fp_sched_out)}           {_fp_plan_ramp/1000:>5.1f}    TOT / CTR\n"
            howgozit += f"{'OFF':<6} {_fp_utc(_fp_sched_off)}           {(_fp_plan_ramp - _fp_taxi)/1000:>5.1f}\n"
            howgozit += "\n"

            # Fix rows — skip TOC/TOD/RWY-type pseudo-fixes with no ident
            _seen_fp = set()
            for _fpf in root.findall('navlog/fix'):
                _fp_ident = (_fpf.findtext('ident') or _fpf.findtext('name') or '').strip().upper()
                if not _fp_ident or _fp_ident in _seen_fp:
                    continue
                _seen_fp.add(_fp_ident)

                _fp_tt  = int(_fpf.findtext('time_total', '0'))
                _fp_fpo = int(float(_fpf.findtext('fuel_plan_onboard', '0')))
                _fp_fl  = int(float(_fpf.findtext('altitude_feet', '0'))) // 100
                _fp_mach_raw = _fpf.findtext('mach_thousandths') or _fpf.findtext('mach') or '0'
                try:
                    _fp_mach_i = round(float(_fp_mach_raw) * 1000)
                except (ValueError, TypeError):
                    _fp_mach_i = 0

                _fp_fix_utc = _fp_utc(_fp_sched_off + _fp_tt)

                if _fp_mach_i > 0:
                    _fp_mid = f"{_fp_fl:03d}/{_fp_mach_i:03d}"
                else:
                    _fp_mid = f"{_fp_fl:03d}/---"

                howgozit += f"{_fp_ident:<6} {_fp_fix_utc}  {_fp_mid}  {_fp_fpo/1000:>5.1f}\n"

            # ON / IN rows using last navlog fix wind dir for runway
            _fp_last_fix = root.findall('navlog/fix')[-1] if root.findall('navlog/fix') else None
            _fp_rwy_wd = int(_fp_last_fix.findtext('wind_dir', '0')) if _fp_last_fix is not None else 0
            _fp_rwy_s  = f"{_fp_rwy_wd:03d}" if _fp_rwy_wd else "073"
            howgozit += f"{'ON':<6} {_fp_utc(_fp_est_on)}  {_fp_rwy_s}/---  {_fp_plan_ldg/1000:>5.1f}\n"
            howgozit += f"{'IN':<6} {_fp_utc(_fp_est_in)}  {_fp_rwy_s}/---  {_fp_plan_ldg/1000:>5.1f}\n"
        except Exception as _fpe:
            LOG.warning(f"Flight progress report error: {_fpe}")

        # ── DISPATCH SIGNATURE AND CREW ───────────────────────────────────────
        howgozit += "[PAGEBREAK]\n"
        howgozit += f"DISP SIGNED BY: {dispatcher}\n"
        howgozit += f"DESK: {_cfg_desk}    PHONE: {_desk_phone}\n\n"
        howgozit += f"CAPT* {format(cptn, '<20')}      CAT1 YES\n"
        howgozit += f"F/O   {format(fo, '<20')}      CAT2 YES\n"
        howgozit += f"{'':32}CAT3 YES\n\n"
        howgozit += "AUZD CAPTAIN SIGNATURE....................\n\n"
        #howgozit += f"BY SIGNING OFF THIS FLIGHT PLAN YOU ARE ACKNOWLEDGING\n"
        #howgozit += f"** FIT FOR DUTY BASED ON FAR 117.5 REQUIREMENTS.\n"
        #howgozit += f"** FOR YOUR AWARENESS, THE *MOT* TIME DISPLAYED INCLUDES PILOT\n"
        #howgozit += f"AUTHORIZED FDP EXTENSION. THE *LMT* TIME INCLUDES THE MAXIMUM\n"
        #howgozit += f"FDP EXTENSION POSSIBLE BASED UPON PLAN DEPARTURE TIME, FOR\n"
        #howgozit += f"UNFORSEEN OPERATIONAL CIRCUMSTANCES.\n"
        #howgozit += f"IF APPROACHING ACTUAL DUTY LIMITATION, CAPTAIN MUST CONTACT\n"
        #howgozit += f"DISPATCH TO COORDINATE FDP EXTENSION.\n\n\n"


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
        howgozit += str(winds_text) if winds_text else ""

        xml_root = root

        # === ETOPS / NAT TRACKS / FIELD REPORTS — before weather ===
        LOG.debug("About to call write_etops_section")
        etops_output = write_etops_section(root)
        LOG.debug(f"[DBG: write_etops_section returned length = {len(etops_output)}")
        if etops_output:
            howgozit += etops_output
            LOG.debug("ETOPS section added to howgozit")
        else:
            LOG.debug("No ETOPS section generated")

        oceanic_output = write_oceanic_section(root)
        if oceanic_output:
            howgozit += oceanic_output
            LOG.debug("[OCN] Oceanic ORV section added to howgozit")

        nat_output = write_nat_tracks_section(root)
        if nat_output:
            howgozit += nat_output
            LOG.debug("NAT tracks section added to howgozit")

        # --- Weather (METAR/TAF/ATIS/SIGMET) — BEFORE NOTAMs ---
        howgozit += "[WEATHER_MARKER]\n"  # footer watermark stops here
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

        # === DECS pages — last, after the NOTAMs ===
        # The Flightkeys release ID stops here: everything below is FOS output.
        howgozit += "[WATERMARK_END]\n"

        try:
            _fos_ctx = _fos_context(root)
            howgozit += build_nsc_page(root, _fos_ctx, cpt=cptn, fo=fo, gate=gate, arr_gate=arr_gate)
            howgozit += build_fi_page(root, _fos_ctx, cpt=cptn, gate=gate, arr_gate=arr_gate)
            howgozit += build_fil_page(root, _fos_ctx)
        except Exception as _fos_e:
            LOG.warning(f"FOS pages (NSC/FI/FIL) skipped: {_fos_e}")

        field_output = write_field_reports(root)
        if field_output:
            howgozit += field_output

        # === Jet fuel service record (form 10012) ===
        try:
            howgozit += build_fuel_service_page(root, _fos_ctx, gate=gate)
        except Exception as _fuel_e:
            LOG.warning(f"Fuel service page skipped: {_fuel_e}")

        # --- Weather charts (images) ---
        if images_text:
            howgozit += images_text


        # --- Add takeoff performance section safely ---
        # Re-fetch runway data here: `valid_runways` from the first call above
        # (line ~1819) may have been overwritten by intermediate assignments.
        valid_runways, flight_info, anti_ice_on, runway_lines = extract_runway_data(root)

        # Get airport altitudes
        origin_icao = flight_info.get('origin', '')
        alt_node = root.find('.//departure_airport/elevation') or root.find('.//departure_airport/altitude')
        max_elevation = float(alt_node.text) if alt_node is not None and alt_node.text else 0
        airport_altitudes = get_airport_specific_altitudes(origin_icao, max_elevation)

        # --- EXTRACT ICAO CODE FOR TAKEOFF PERFORMANCE (speeds) ---
        icao_code_for_speeds = get_text("aircraft/icaocode", "XXXX")
        LOG.debug(f"[DBG: ICAO code for speeds: '{icao_code_for_speeds}'")

        # Pass everything into the builder INCLUDING ICAO
        # Combine SimBrief orig_atis with the already-assembled OFP text so that
        # externally-fetched ATIS (which lands in howgozit via weather_wx) is
        # available for departure runway parsing even if orig_atis is empty.
        if get_report_type() == "TPS":
            _atis_for_perf = orig_atis or ''
            if not _atis_for_perf:
                _atis_for_perf = howgozit   # full OFP text — contains rendered ATIS block
            takeoff_perf = write_takeoff_performance_string(
                flight_info,
                valid_runways,
                anti_ice_on,
                runway_lines,
                airport_altitudes=airport_altitudes,
                max_elevation=max_elevation,
                icao_code=icao_code_for_speeds,
                xml_root=root,
                atis_text=_atis_for_perf,
                field_condition_text=None,  # set via config or future prompt; NOTAMs fill this automatically
            )
            howgozit += takeoff_perf
        else:
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
        LOG.debug(f"[DBG: Raw pax_count from XML: '{raw_pax_count}'")
        LOG.debug(f"[DBG: Raw pax_weight from XML: '{raw_pax_weight}'")

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
                LOG.debug(f"[DBG: Testing path '{path}': '{test_value}'")
                if test_value and test_value.strip() != "" and test_value != "0":
                    raw_pax_count = test_value
                    LOG.debug(f"[DBG: Using passenger count from '{path}': {raw_pax_count}")
                    break

        # --- Aircraft config ---

        # --- Parse acdata_parsed early — used for cabin config fallback and cargo ---
        acdata = {}
        try:
            _acdata_tag = xml_root.find('.//api_params/acdata_parsed')
            if _acdata_tag is not None and _acdata_tag.text and _acdata_tag.text.strip():
                acdata = json.loads(_acdata_tag.text.strip())
                LOG.debug(f"[DBG: Loaded acdata for {acdata.get('reg', 'UNKNOWN')} ({acdata.get('icao', '?')})")
            else:
                LOG.warning("acdata_parsed tag is missing or empty.")
        except Exception as e:
            LOG.warning(f"Failed to parse acdata_parsed: {e}")
            acdata = {}

        # --- Normalize aircraft string ---
        def normalize_aircraft(ac_type):
            """Normalise a raw aircraft type string to a standard ICAO code."""
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
                    LOG.debug(f"[DBG: Specific mapping match: '{ac_type}' -> '{value}'")
                    return value

            # Remove parentheses and content inside
            ac_type = re.sub(r"\(.*?\)", "", ac_type_upper)
            # Remove trailing non-alphanumeric suffixes like WL, NX, NY
            ac_type = re.sub(r"[^A-Z0-9-]+$", "", ac_type.strip())
            return ac_type

        # --- Get aircraft NAME for cabin config (NOT icaocode) ---
        aircraft_name_from_xml = get_text("aircraft/name", "")
        LOG.debug(f"[DBG: Aircraft name from XML aircraft/name: '{aircraft_name_from_xml}'")

        # Fall back to flight_info aircraft if name not found = get_text("aircraft/icaocode", "XXXX")
        aircraft_type_raw = aircraft_name_from_xml if aircraft_name_from_xml else get_text("aircraft/icaocode", "XXXX")
        LOG.debug(f"[DBG: Raw aircraft type before normalization: '{aircraft_type_raw}'")

        aircraft_type = normalize_aircraft(aircraft_type_raw)
        LOG.debug(f"[DBG: Normalized aircraft type for cabin config: '{aircraft_type}'")

        # --- Lookup config ---
        config = CABIN_CONFIGS.get(aircraft_type)
        if not config:
            LOG.debug(f"[DBG: No direct match for '{aircraft_type}', trying ICAO fallback...")
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
                    LOG.debug(f"[DBG: ICAO fallback match '{icao_code}': {config}")
                    break

            # Final fallback — use acdata_parsed maxpax if available, else 300
            if not config:
                _maxpax = int(acdata.get('maxpax', 0) or 0)
                _total_y = _maxpax if _maxpax > 0 else 300
                config = {"F": 0, "C": 0, "W": 0, "Y": _total_y}
                LOG.debug(f"[DBG: acdata_parsed maxpax fallback → Y={_total_y} for '{aircraft_type_raw}'")
                # Also use acdata paxwgt as default pax weight if not already set by XML
                if not (raw_pax_weight and raw_pax_weight.strip()):
                    try:
                        _paxwgt = float(acdata.get('paxwgt', 0) or 0)
                        if _paxwgt > 0:
                            raw_pax_weight = str(_paxwgt)
                            LOG.debug(f"[DBG: Using acdata_parsed paxwgt={_paxwgt} as default pax weight")
                    except Exception:
                        pass
        else:
            LOG.debug(f"[DBG: Found config for '{aircraft_type}': {config}")

        # Ensure numbers are integers with fallback values
        try:
            pax_count = int(raw_pax_count) if raw_pax_count and raw_pax_count.strip() else 0
        except ValueError:
            LOG.warning(f"Could not convert pax_count '{raw_pax_count}' to integer, using 0")
            pax_count = 0

        try:
            pax_weight = float(raw_pax_weight) if raw_pax_weight and raw_pax_weight.strip() else 84.0
        except ValueError:
            LOG.warning(f"Could not convert pax_weight '{raw_pax_weight}' to float, using default 84.0")
            pax_weight = 84.0

        LOG.debug(f"[DBG: Final pax_count: {pax_count}, pax_weight: {pax_weight}")
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
            "\n"
            + "\n" * 4
            + "******************** WEIGHT AND BALANCE DATA ******************\n\n"
            "------LOAD----------TOTALS-------LIMITS-----CMPT MAX--AS LDED--\n"
            # Fixed-width numeric fields — a 5-digit weight must not drag the
            # TOTALS and LIMITS columns left.
            # LOAD/TOTALS occupy a fixed 44 columns so the CMPT block that
            # follows starts in the same place on both rows.
            f"{f'EOW     {str(oew_s):>6}  ZFW  {str(est_zfw_s):>6}  MZFW {str(max_zfw_s):>6}':<44}"
            f"F1 {str(fwd_max):>5} {str(fwd_actual):>7}\n"
            f"{f'PSGR WT {str(total_pax_wt_s):>6}  FUEL {str(plan_ramp_s):>6}P *** STD ***':<44}"
            f"A1 {str(aft_max):>5} {str(aft_actual):>7}\n"
            f"CGO WT  {str(cargo_rounded):>6}  RMP  {str(ramp_weight_formatted_s):>6}"
            f"  MRMP {str(max_tow_struct_s):>6}\n"
            f"BALLAST {'0':>6}  TXI  {str(taxi_fuel_s):>6}\n"
            f"{'':8}{'':6}  TOW  {str(est_tow_s):>6}  MTOW {str(max_tow_struct_s):>6}\n"
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
        LOG.error(f"ERROR building HOWGOZIT: {e}")
        LOG.error(f"ERROR type: {type(e)}")
        traceback.print_exc()

        try:
            LOG.debug(f"[DBG: icao_airline = {icao_airline}")
            LOG.debug(f"[DBG: flight_number = {flight_number}")
            LOG.debug(f"[DBG: origin = {origin}")
            LOG.debug(f"[DBG: destination = {destination}")
        except Exception:
            LOG.debug("Could not access basic flight info variables")

        # Return a safe tuple with error message
        error_message = f"Error generating flight plan: {e}"
        return error_message, {}, [], False, []

# ===========================================================================
# Field reports & ATC frequencies
# OurAirports frequency lookup and the FIELD REPORTS section.
# ===========================================================================


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
            import urllib.request, ssl
            # Build a verified SSL context; fall back to unverified if certs not
            # installed (common on fresh macOS Python installs without certifi).
            try:
                import certifi
                _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                try:
                    _ssl_ctx = ssl.create_default_context()
                except Exception:
                    _ssl_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(URL, timeout=12, context=_ssl_ctx) as resp:
                raw = resp.read().decode('utf-8')
            try:                                      # write cache (best-effort)
                with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                    f.write(raw)
            except Exception:
                pass
        except Exception as e:
            LOG.debug(f"[OurAirports] Frequency download failed: {e}")
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
        LOG.warning(f"[OurAirports] Parse error: {e}")
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
    SEP    = "--------------------------------------------------------------"
    PAGE_W = 62

    # ── Company (airline-internal) frequencies ────────────────────────────────
    # OurAirports carries ATC only. Company OPS / MAINT / patch / deice
    # frequencies live in the 128.825-132.000 MHz band and are assigned per
    # station; they are derived deterministically from the ICAO so a station
    # always shows the same set, and are overridden by station_ops when set.
    def _company_freqs(icao):
        seed = sum(ord(ch) * (i + 3) for i, ch in enumerate((icao or 'XXXX').upper()))
        rng  = random.Random(seed)
        band = [128.825 + 0.025 * n for n in range(0, 128)]   # 128.825-132.000
        picks = rng.sample(band, 4)
        ops, maint, patch, deice = (f"{p:.2f}" for p in picks)
        return {'ops': ops, 'maint': maint, 'patch': patch, 'deice': deice}

    # Present-weather groups only — a bare substring test matches things like
    # the IC in PACIFIC or the GR in GRADIENT, which would fire deicing in
    # Hawaii in May.
    _WX_GROUP = re.compile(
        r'(?:^|\s)([+-]?(?:VC)?'
        r'(?:MI|PR|BC|DR|BL|SH|TS|FZ)?'
        r'(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP){1,3})(?=\s|$)')
    _FROZEN_CODES = ('SN', 'SG', 'PL', 'GS', 'GR', 'IC', 'UP')

    def _deice_conditions(icao, metar, taf, wx, local_date):
        """
        Decide whether a deicing report belongs on this release.

        Season:  01 OCT - 30 APR at any station.
        Weather: OAT <= 5C, or frozen/freezing precipitation in METAR or TAF.

        Returns (needed: bool, reasons: list[str], oat: int|None).
        """
        reasons = []
        month = None
        try:
            month = datetime.now(timezone.utc).month
            if local_date and len(local_date) >= 5:
                _mon = local_date[2:5].upper()
                _MON = ['JAN','FEB','MAR','APR','MAY','JUN',
                        'JUL','AUG','SEP','OCT','NOV','DEC']
                if _mon in _MON:
                    month = _MON.index(_mon) + 1
        except Exception:
            pass
        if month is not None and (month >= 10 or month <= 4):
            reasons.append('SEASON')

        oat = None
        try:
            oat = int(float(wx.get('temp')))
        except Exception:
            oat = None
        if oat is not None and oat <= 5:
            reasons.append('OAT')

        blob = f"{metar or ''} {taf or ''}".upper()
        for _grp in _WX_GROUP.findall(blob):
            _g = _grp.lstrip('+-')
            if _g.startswith('FZ') or 'FZ' in _g or any(c in _g for c in _FROZEN_CODES):
                reasons.append('PRECIP')
                break

        return (bool(reasons), reasons, oat)

    def _sls_banner(cmd):
        """****************** SLS*ORF/FC ******************"""
        return "*" * 18 + f" {cmd} " + "*" * 18 + "\n"

    def _msg_dtg():
        """DDHHMM zulu stamp used on the FOS message header line."""
        return datetime.now(timezone.utc).strftime("%d%H%M")

    def _centered(text, width=48):
        return " " * max(0, (width - len(text)) // 2) + text + "\n"

    def _sls_head(cmd, display, icao, flt_num):
        out  = "\n[PAGEBREAK]\n"
        out += _sls_banner(cmd)
        out += _fos_routing(_op_iata) + "\n"
        out += f"{display} {icao} {_msg_dtg()}\n".replace("  ", " ")
        if (flt_num or "").strip():
            out += f"{flt_num.strip()}\n"
        out += f"/{cmd.split('/')[-1]}\n"
        return out

    # ── Regex patterns ─────────────────────────────────────────────────────────
    _RWY_PAT  = re.compile(
        r'\b(?:R/?W(?:Y)?S?\.?\s*)(\d{1,2}[LRC]?)(?:[/\\-](\d{1,2}[LRC]?))?',
        re.IGNORECASE)
    _CLSD_PAT = re.compile(r'\bCLS[DE]D?\b', re.IGNORECASE)
    _EFF_PAT  = re.compile(
        r'(\d{2}[A-Z]{3}\d{2,4}(?:/\d{4})?)\s*(?:TO|[-\u2013])\s*'
        r'(\d{2}[A-Z]{3}\d{2,4}(?:/\d{4})?)',
        re.IGNORECASE)

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
        m = re.search(r'Q\)\s*\w+/Q([A-Z]{2})', text)
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
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', s)
        if m:
            yr, mo, dy, hh, mm = m.groups()
            months = ['JAN','FEB','MAR','APR','MAY','JUN',
                      'JUL','AUG','SEP','OCT','NOV','DEC']
            return f"{dy}{months[int(mo)-1]}{yr[2:]}/{hh}{mm}"
        m2 = re.match(r'(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', s)
        if m2:
            yy, mo, dy, hh, mm = m2.groups()
            months = ['JAN','FEB','MAR','APR','MAY','JUN',
                      'JUL','AUG','SEP','OCT','NOV','DEC']
            return f"{dy}{months[int(mo)-1]}{yy}/{hh}{mm}"
        return s

    # ── Runway closure extractor (Q-code primary) ──────────────────────────────
    _RWY_CLSD_DIRECT = re.compile(
        r'\bRW?Y\s+(\d{1,2}[LRC]?)(?:[/\\-](\d{1,2}[LRC]?))?\s+CLSD\b',
        re.IGNORECASE)

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
        _TWY_ID_PAT = re.compile(
            r'\bTWY\s+([A-Z]{1,3}\d*)', re.IGNORECASE)
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
        planned_num = re.search(r'(\d{1,2})', planned_rwy or '')
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
                nums = set(re.findall(r'(?<!\d)(\d{1,2})(?!\d)', text))
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
        m = re.search(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT', metar)
        if m:
            r['wind_dir'] = m.group(1)
            r['wind_spd'] = int(m.group(2))
            if m.group(3): r['wind_gst'] = int(m.group(3))
        m = re.search(r'\bA(\d{4})\b', metar)
        if m: r['altimeter'] = f"{m.group(1)[:2]}.{m.group(1)[2:]}"
        m = re.search(r'\b(M?\d{2})/(M?\d{2})\b', metar)
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
            def _num(p): m = re.search(r'(\d+)', p); return int(m.group(1)) if m else 99
            return pair_str if _num(parts[0]) <= _num(parts[1]) else f"{parts[1]}/{parts[0]}"

        seen_pairs = {}
        if tlr_section is not None:
            for rwy in tlr_section.findall('runway'):
                rid = (rwy.findtext('identifier') or '').strip().upper()
                if not rid: continue
                pair = _canon_pair(_make_runway_pair(rid) or rid)
                if pair not in seen_pairs:
                    seen_pairs[pair] = surface

        # ── Fill in runways the TLR did not analyse ───────────────────────────
        # A real field report lists every runway at the field, not just the ones
        # SimBrief planned for — ORF must show 14/32 alongside 05/23.
        try:
            for _rid in _full_runway_index().get((icao or '').strip().upper(), {}):
                pair = _canon_pair(_make_runway_pair(_rid) or _rid)
                if pair not in seen_pairs:
                    seen_pairs[pair] = surface
        except Exception as _rw_e:
            LOG.debug(f"[FIELD-RWY] index fill skipped: {_rw_e}")

        # ── Overlay runway closures from NOTAMs ───────────────────────────────
        closed_map = {_canon_pair(k): v for k, v in _extract_closures(notams_list).items()}
        all_pairs  = dict(seen_pairs)
        for pair in closed_map:
            if pair not in all_pairs:
                all_pairs[pair] = surface

        if not all_pairs:
            return ""

        def _sort_key(p):
            m = re.search(r'(\d+)', p)
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

        # == Assemble report ====================================================

        # FOS message frame:  SLS*<STA>/FC
        out  = _sls_head(f"SLS*{display}/FC", display, icao, flt_num)
        out += _centered(f"* {display} FIELD REPORT *")
        out += SEP + "\n"

        # Date / Time — use pre-computed local values
        if local_date or local_time:
            hdr = (f"DATE  {local_date}" if local_date else "") + \
                  (f"   TIME {local_time} LOCAL" if local_time else "")
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
                out += textwrap.fill(str(line), width=76,
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

        # Company frequencies: station_ops config wins, synthesised set fills
        # the gaps so the grid is never blank.
        _cf = _company_freqs(icao)
        out += _cline("OPS RADIO FREQ",   comms.get('ops_freq') or _cf['ops'],
                      "OPS PHONE",        comms.get('ops_phone'))   + "\n"
        # ATIS: station_ops config wins; OurAirports fills the gap
        out += _cline("ATIS FREQ",        atis_freq,
                      "ATIS PHONE",       comms.get('atis_phone'))  + "\n"
        out += _cline("MAINT FREQ",       comms.get('maint_freq') or _cf['maint'],
                      "MAINT PHONE",      comms.get('maint_phone')) + "\n"
        out += _cline("PHONE PATCH FREQ", comms.get('patch_freq') or _cf['patch'],
                      "PATCH PHONE",      comms.get('patch_phone')) + "\n"

        acars = (comms.get('acars_freq') or 'NONE').upper()
        gpu   = ('YES' if comms.get('gpu')     else ('NO' if 'gpu'     in comms else '')).ljust(6)
        gnd   = ('YES' if comms.get('gnd_air') else ('NO' if 'gnd_air' in comms else ''))
        out += f"{'ACARS FREQ':<16} {acars:<13}GROUND PWR {gpu} GROUND AIR {gnd}\n".rstrip() + "\n"
        out += SEP + "\n"

        # ── Remarks block ──────────────────────────────────────────────────────
        # If no station_ops remarks, fall back to ATC freq info from OurAirports
        # so the block is never completely empty.
        app_freq = _pick_atc_freq(atc_freqs, 'APP')
        dep_freq = _pick_atc_freq(atc_freqs, 'DEP')

        _atc_pairs = [("TOWER", twr_freq), ("GROUND", gnd_freq),
                      ("CLEARANCE", cld_freq), ("APPROACH", app_freq),
                      ("DEPARTURE", dep_freq)]
        _atc_pairs = [(lbl, f) for lbl, f in _atc_pairs if f]

        out += "REMARKS\n"
        for line in remarks:
            out += textwrap.fill(str(line), width=76,
                            subsequent_indent="     ",
                            break_long_words=False, break_on_hyphens=False) + "\n"
        # ATC frequencies, two per line, always shown
        for _i in range(0, len(_atc_pairs), 2):
            _chunk = _atc_pairs[_i:_i + 2]
            out += "".join(f"{_l + ' FREQ':<20}{_f:<12}" for _l, _f in _chunk).rstrip() + "\n"
        out += f"{'DEICE FREQ':<20}{comms.get('deice_freq') or _cf['deice']}\n"
        if updated or initials:
            out += f"UPDATED  {updated}      {initials}".strip() + "\n"
        out += "END DATA\n"

        return out

    # ── Deicing report page (SLS*<STA>/DI) ────────────────────────────────────
    def _build_deice_report(icao, iata, local_date, local_time, station_ops,
                            flt_num, wx=None, metar='', taf='', role='DEP'):
        """
        Emitted only when the season (01 OCT - 30 APR) or the weather
        (OAT <= 5C, or frozen/freezing precipitation) calls for it.
        Returns '' when deicing is not a factor for this station.
        """
        display = iata or icao
        di      = station_ops.get('deice', {}) if isinstance(station_ops, dict) else {}
        wx      = wx or {}

        needed, reasons, oat = _deice_conditions(icao, metar, taf, wx, local_date)
        if not needed and not di.get('always'):
            return ""

        _cf   = _company_freqs(icao)
        freq  = di.get('freq') or (station_ops.get('comms', {}) or {}).get('deice_freq') or _cf['deice']
        rng   = random.Random(sum(ord(c) * (i + 5) for i, c in enumerate((icao or 'XXXX').upper())))

        out  = _sls_head(f"SLS*{display}/DI", display, icao, flt_num)
        out += _centered(f"* {display} DEICING REPORT *")
        out += SEP + "\n"
        if local_date or local_time:
            hdr = (f"DATE  {local_date}" if local_date else "") + \
                  (f"   TIME {local_time} LOCAL" if local_time else "")
            out += hdr.strip() + "\n"
            out += SEP + "\n"

        lines = di.get('lines') or []
        if lines:
            for line in lines:
                out += textwrap.fill(str(line), width=76,
                                     subsequent_indent="     ",
                                     break_long_words=False,
                                     break_on_hyphens=False) + "\n"
            out += SEP + "\n"
        else:
            _trig = "/".join(reasons) if reasons else "SEASON"
            _oat_s = f"{oat}C" if oat is not None else "N/A"
            out += f"DEICE OPS ACTIVE          TRIGGER {_trig}\n"
            out += f"OAT {_oat_s:<8}              DEICE FREQ  {freq}\n"
            out += SEP + "\n"
            _loc = di.get('location') or rng.choice([
                "DEICE PAD - APPROACH END RWY IN USE",
                "AT GATE - RAMP CONTROL COORDINATED",
                "REMOTE DEICE PAD - CONTACT ICEMAN ON DEICE FREQ",
            ])
            out += "DEICE LOCATION\n"
            out += f"  {_loc}\n"
            out += SEP + "\n"
            out += "FLUID AVAILABILITY\n"
            out += f"  TYPE I    AVAILABLE      TYPE IV   {di.get('type4', 'AVAILABLE')}\n"
            out += f"  TYPE II   {di.get('type2', 'NOT AVAILABLE'):<14} BLEND     {di.get('blend', '55/45')}\n"
            out += SEP + "\n"
            out += "HOLDOVER TIMES\n"
            out += "  REF FOM HOT TABLES - CURRENT SEASON. HOT BEGINS AT START\n"
            out += "  OF FINAL FLUID APPLICATION. PRETAKEOFF CONTAMINATION CHECK\n"
            out += "  REQUIRED WITHIN 5 MIN OF TAKEOFF WHEN HOT EXCEEDED.\n"
            out += SEP + "\n"
            out += "REMARKS\n"
            out += f"  ANTI-ICE CODE REQUIRED PRIOR TO PUSHBACK.\n"
            if 'PRECIP' in reasons:
                out += "  ACTIVE FROZEN PRECIPITATION REPORTED - EXPECT DELAYS.\n"
            if role == 'ARR':
                out += "  ARRIVAL STATION - INFORMATION ONLY.\n"

        upd = di.get('updated', '')
        ini = di.get('initials', '')
        if upd or ini:
            out += f"UPDATED  {upd}      {ini}".strip() + "\n"
        out += "END DATA\n"
        return out

    # ── Main ──────────────────────────────────────────────────────────────────
    try:
        out = ""   # each SLS page emits its own [PAGEBREAK]

        # Flight identifier for the "ICAO FLTNUM" header line (e.g. "JFK 110416")
        _flt_num = ((root.findtext('general/icao_airline') or '') +
                    (root.findtext('general/flight_number') or '')).strip()

        # Two-letter operator code for the FOS routing line
        _op_iata = _fos_iata((root.findtext('general/icao_airline') or ''),
                             (root.findtext('general/iata_airline') or ''))

        # Timezone offsets for local time display
        try:
            _orig_tz = int(float(root.findtext('times/orig_timezone') or '0'))
        except (ValueError, TypeError):
            _orig_tz = 0
        try:
            _dest_tz = int(float(root.findtext('times/dest_timezone') or '0'))
        except (ValueError, TypeError):
            _dest_tz = 0

        def _local_hhmm(utc_ts_str, tz_offset):
            """Convert UTC Unix timestamp string to local HHMM string."""
            try:
                ts  = int(utc_ts_str or '0')
                utc = datetime.fromtimestamp(ts, tz=timezone.utc)
                loc = utc + timedelta(hours=tz_offset)
                return loc.strftime("%H%M")
            except Exception:
                return ''

        def _local_date(utc_ts_str, tz_offset):
            """Convert UTC Unix timestamp string to local DDMMMxx date string."""
            try:
                months = ['JAN','FEB','MAR','APR','MAY','JUN',
                          'JUL','AUG','SEP','OCT','NOV','DEC']
                ts  = int(utc_ts_str or '0')
                utc = datetime.fromtimestamp(ts, tz=timezone.utc)
                loc = utc + timedelta(hours=tz_offset)
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
        _orig_taf = (root.findtext('weather/orig_taf') or '').strip()
        out += _build_deice_report(
            orig_icao, orig_iata, orig_date, orig_time,
            _get_ops(orig_icao), _flt_num,
            wx=_parse_metar_brief(orig_metar), metar=orig_metar,
            taf=_orig_taf, role='DEP')

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
        _dest_taf = (root.findtext('weather/dest_taf') or '').strip()
        out += _build_deice_report(
            dest_icao, dest_iata, dest_date, dest_time,
            _get_ops(dest_icao), _flt_num,
            wx=_parse_metar_brief(dest_metar), metar=dest_metar,
            taf=_dest_taf, role='ARR')

        return out

    except Exception as e:
        LOG.error(f"Error generating field reports: {e}")
        traceback.print_exc()
        return ""

# ===========================================================================
# ETOPS / Oceanic
# NAT tracks, oceanic route verification, and ETOPS adequate-airport pages.
# ===========================================================================


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
        LOG.error(f"Error generating NAT tracks section: {e}")
        traceback.print_exc()
        return ""


def _build_etops_oceanic_pdf(root):
    """
    ETOPS Critical Fuel Scenario + Oceanic Route Verification pages.
    Uses navlog-table drawing primitives:
      - A4, LM=55, RM=103, same font/FS/ROW_H/HDR_H as _draw_navlog_table
      - canvas.line() horizontal rules after every row
      - _border2() outer rect drawn once at end
      - stringWidth() for right-aligned numeric fields
      - Absolute x positions (measured from SimBrief OFP reference) for both
        main-point rows and diversion rows — no unified column grid
    """
    import io, math
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors

    PW, PH = A4
    LM = 46; RM = 46; TM = 46; BM = 45
    CW = PW - LM - RM          # 437 pts — same as navlog TW

    # ── Font ─────────────────────────────────────────────────────────────────
    if _cached_font_choice == "2":
        _user_font_path = "/System/Library/Fonts/Menlo.ttc"
    elif _cached_font_choice == "3":
        _user_font_path = _FONT_PATH_COURIER_NEW
    elif _cached_font_choice == "4":
        _user_font_path = "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"
    else:
        _user_font_path = _FONT_PATH_COURIER_NORMAL

    _mono = "Courier"
    if _user_font_path and os.path.exists(_user_font_path):
        try:
            pdfmetrics.registerFont(TTFont("_EtopsMono", _user_font_path))
            _mono = "_EtopsMono"
        except Exception:
            pass

    buf = io.BytesIO()
    c   = _rl_canvas.Canvas(buf, pagesize=A4)

    # ── Colours — identical to _draw_navlog_table ────────────────────────────
    NAV_BG = colors.HexColor("#D8D8D8")
    BLACK  = colors.black

    FS    = 9.0
    ROW_H = FS * 1.8          # tighter rows to reduce table height
    HDR_H = ROW_H * 1.05     # compact — just over one row height per header line
    PAD   = 3

    # ── Column positions — left-edge, full-width spread (CW≈523pt, 20pt gaps) ──
    # All columns left-anchored from x + PAD; right edge = left + chars*CH
    # Main-point row
    X_SEQ      = LM + 0.000 * CW
    X_TYPE     = LM + 0.030 * CW
    X_LATLONG  = LM + 0.160 * CW
    X_REMF     = LM + 0.440 * CW
    X_MINREQ   = LM + 0.565 * CW
    X_EET      = LM + 0.680 * CW
    X_SCENARIO = LM + 0.790 * CW

    # Diversion sub-columns — all left-anchored
    X_E        = LM + 0.000 * CW   # "E"
    X_ALTN     = LM + 0.018 * CW
    X_GCD      = LM + 0.082 * CW
    X_FL       = LM + 0.148 * CW
    X_COMP     = LM + 0.205 * CW
    X_TMP      = LM + 0.272 * CW
    X_TD       = LM + 0.328 * CW
    X_AI       = LM + 0.382 * CW
    X_TRIPF    = X_REMF             # same column as REMF
    X_TIME     = X_EET              # same column as EET
    X_WINDOW   = X_SCENARIO         # same column as SCENARIO

    # ── Shared helpers ────────────────────────────────────────────────────────
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
            y_ = math.cos(la1)*math.sin(la2)-math.sin(la1)*math.cos(la2)*math.cos(dlo)
            return round(math.degrees(math.atan2(x_,y_)) % 360)
        except: return None

    # ── Drawing primitives (navlog style) ────────────────────────────────────
    class PageState:
        def __init__(self):
            self.y        = PH - TM
            self.page_num = 6
            self.box_top  = PH - TM
    _s = PageState()

    def _str_at(x, s, right_anchor=False):
        """Draw string left-justified at x + PAD."""
        if not s: return
        vs = _safe_latin1(str(s))
        c.setFont(_mono, FS)
        c.setFillColor(BLACK)
        c.drawString(x + PAD, _s.y - ROW_H + ROW_H * 0.25, vs)

    def _rule(lw=1.0):
        c.setStrokeColor(BLACK); c.setLineWidth(lw)
        c.line(LM + 0.5, _s.y - ROW_H, LM + CW - 0.5, _s.y - ROW_H)

    def _advance(rule=True):
        if rule: _rule()
        _s.y -= ROW_H

    def _border2(top_y, bot_y):
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.rect(LM, bot_y, CW, top_y - bot_y, fill=0, stroke=1)

    def _title_band(text, grey=False):
        c.setFillColor(NAV_BG if grey else colors.white)
        c.rect(LM, _s.y - HDR_H, CW, HDR_H, fill=1, stroke=0)
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        c.drawCentredString(PW / 2, _s.y - HDR_H + (HDR_H - FS) / 2 + 1, _safe_latin1(text))
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.line(LM + 0.5, _s.y - HDR_H, LM + CW - 0.5, _s.y - HDR_H)
        _s.y -= HDR_H

    def _col_header_etops():
        """Two-row column header matching SimBrief ETOPS table."""
        total_h = HDR_H * 2
        c.setFillColor(colors.white)
        c.rect(LM, _s.y - total_h, CW, total_h, fill=1, stroke=0)
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        # Row 1 — main point row labels (vertically centred in top half)
        row1_y = _s.y - HDR_H * 0.70
        for x, label, right in [
            (X_SEQ,      "SEQ",      False),
            (X_TYPE,     "TYPE",     False),
            (X_LATLONG,  "LAT/LONG", False),
            (X_REMF,     "REMF",     False),
            (X_MINREQ,   "MIN REQ",  False),
            (X_EET,      "EET",      False),
            (X_SCENARIO, "SCENARIO", False),
        ]:
            vs = _safe_latin1(label)
            if right:
                c.drawString(x - c.stringWidth(vs, _mono, FS), row1_y, vs)
            else:
                c.drawString(x + PAD, row1_y, vs)
        # Row 2 — diversion row sub-column labels (vertically centred in bottom half)
        row2_y = _s.y - HDR_H * 0.70 - HDR_H + FS * 0.3
        for x, label, right in [
            (X_ALTN,    "ALTN",       False),
            (X_GCD,     "GCD",        False),
            (X_FL,      "FL",         False),
            (X_COMP,    "COMP",       False),
            (X_TMP,     "TMP",        False),
            (X_TD,      "TD",         False),
            (X_AI,      "AI",         False),
            (X_TRIPF,   "TRIPF",      False),
            (X_TIME,    "TIME",       False),
            (X_WINDOW,  "SWX WINDOW", False),
        ]:
            vs = _safe_latin1(label)
            if right:
                c.drawString(x - c.stringWidth(vs, _mono, FS), row2_y, vs)
            else:
                c.drawString(x + PAD, row2_y, vs)
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.line(LM + 0.5, _s.y - total_h, LM + CW - 0.5, _s.y - total_h)
        _s.y -= total_h

    def draw_page_header(page_num):
        rel  = (root.findtext("general/release") or "").strip()
        orig = (root.findtext("origin/icao_code") or "").strip()
        dest = (root.findtext("destination/icao_code") or "").strip()
        flt  = ''.join(ch for ch in ((root.findtext("general/icao_airline") or "") +
               (root.findtext("general/flight_number") or "")).strip()
               if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789- /")
        try:
            ts = int(root.findtext("times/sched_out") or "0")
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        except: dt = ""
        _rel_ver = rel or __version__
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        c.drawString(LM, PH - 36,
                     _safe_latin1(f"PAGE {page_num}  RELEASE {_rel_ver}  {dt}  {orig}-{dest}  {flt}"))

    def draw_footer(page_num, total_pages):
        pass  # footer suppressed

    # ── Pull ETOPS data ───────────────────────────────────────────────────────
    is_etops      = (root.findtext("general/is_etops") or "0").strip()
    etops_section = root.find("etops")
    if is_etops != "1" or etops_section is None:
        c.save(); return buf.getvalue()

    etops_rule  = etops_section.findtext("rule", "")
    entry_point = etops_section.find("entry")
    etp_point   = etops_section.find("equal_time_point")
    exit_point  = etops_section.find("exit")

    # suitability windows keyed by ICAO
    suitable_windows = {}
    for apt in etops_section.findall("suitable_airport"):
        icao  = apt.findtext("icao_code", "")
        s_raw = apt.findtext("suitability_start", "")
        e_raw = apt.findtext("suitability_end", "")
        if icao and s_raw and e_raw:
            try:
                sd_ = datetime.fromisoformat(s_raw.replace("Z","+00:00"))
                ed_ = datetime.fromisoformat(e_raw.replace("Z","+00:00"))
                suitable_windows[icao] = f"{sd_.strftime('%H:%M')}-{ed_.strftime('%H:%M')}"
            except Exception:
                suitable_windows[icao] = ""

    _cont = root.findtext("general/cont_rule","")
    try: ai_pct = f"{int(_cont):03d}%"
    except: ai_pct = "005%"

    scenario_str = etops_section.findtext("scenario_string","")
    if not scenario_str:
        cc = ""
        for pt in [entry_point, etp_point, exit_point]:
            if pt is not None:
                cc = pt.findtext("etops_condition",""); break
        scenario_str = {"DX":"1EO AUTO DCP+","D":"1EO AUTO DCP+",
                        "X":"1EO MANUAL DCP+","N":"1EO AUTO NORMAL"
                        }.get(cc, f"1EO AUTO {cc}" if cc else "1EO AUTO DCP+")

    header_nm = ""
    if etp_point is not None:
        try:
            dists = [float(d.findtext("distance","0"))
                     for d in etp_point.findall("div_airport")]
            if dists: header_nm = f"{int(round(float(max(dists))))}NM"
        except: pass

    # ===========================================================================
    # PAGE 1 — ETOPS CRITICAL FUEL SCENARIO
    # ===========================================================================
    draw_page_header(_s.page_num)
    _s.box_top = _s.y

    title_txt = (f"CRITICAL FUEL SCENARIO BASED ON {etops_rule}MIN / {header_nm}"
                 if header_nm else f"CRITICAL FUEL SCENARIO  ETOPS-{etops_rule}")
    _title_band(title_txt)
    _col_header_etops()

    seq = [1]

    def emit_point(pt, label):
        if pt is None: return

        lat_s  = fmt_coord(_gf(pt,"pos_lat_fix") or _gf(pt,"pos_lat_apt") or _gf(pt,"pos_lat"))
        lon_s  = fmt_coord(_gf(pt,"pos_long_fix") or _gf(pt,"pos_long_apt") or _gf(pt,"pos_long"),True)
        remf_s = fmt_fuel(_gf(pt,"fuel_reqd") or _gf(pt,"fob") or _gf(pt,"est_fob") or _gf(pt,"fuel_min_reqd") or _gf(pt,"remf") or _gf(pt,"critical_fuel"))
        eet_s  = fmt_eet(_gf(pt,"elapsed_time") or _gf(pt,"eet"))

        pt_div_burn  = _gf(pt, "div_burn")
        pt_crit_fuel = _gf(pt, "critical_fuel")
        pt_div_time  = _gf(pt, "div_time")
        pt_div_alt   = _gf(pt, "div_altitude")

        # main point row
        _str_at(X_SEQ,      str(seq[0]))
        _str_at(X_TYPE,     label)
        # Clip lat/long string to available space before REMF column
        _ll_max_w = X_REMF - X_LATLONG - PAD
        _ll_vs    = _safe_latin1(f"{lat_s} {lon_s}")
        while _ll_vs and c.stringWidth(_ll_vs, _mono, FS) > _ll_max_w:
            _ll_vs = _ll_vs[:-1]
        _str_at(X_LATLONG,  _ll_vs)
        _str_at(X_REMF,     remf_s)
        _str_at(X_EET,      eet_s)
        # Clip scenario_str to available width so it doesn't overflow the right margin
        _scen_max_w = (LM + CW) - X_SCENARIO - PAD
        _scen_vs    = _safe_latin1(str(scenario_str))
        while _scen_vs and c.stringWidth(_scen_vs, _mono, FS) > _scen_max_w:
            _scen_vs = _scen_vs[:-1]
        _str_at(X_SCENARIO, _scen_vs)
        _advance(rule=False)
        seq[0] += 1

        # diversion rows
        for div in pt.findall("div_airport"):
            dicao  = _gf(div, "icao_code")
            dist_i = str(int(_sfloat(_gf(div, "distance"))))
            # FL — plain flight level number e.g. '100' for FL100
            _fl_raw = _gf(div, "fl") or _gf(div, "cruise_fl")
            if not _fl_raw:
                _alt_ft = _sfloat(_gf(div, "div_altitude") or pt_div_alt)
                _fl_raw = str(int(_alt_ft / 100)) if _alt_ft else "100"
            try:
                fl_s = str(int(_fl_raw))
            except (ValueError, TypeError):
                fl_s = str(_fl_raw)
            # COMP
            comp_s = fmt_wind_comp(_gf(div,"avg_wind_comp") or _gf(div,"wind_comp"))
            # TMP (OAT = ISA std + ISA dev)
            _isa_dev = _sfloat(_gf(div,"avg_temp_dev") or _gf(div,"oat_isa_dev"))
            _alt_ft  = _sfloat(_gf(div,"div_altitude") or pt_div_alt)
            _isa_std = (15.0 - (_alt_ft/1000.0)*1.9812) if _alt_ft <= 36090 else -56.5
            tmp_s  = fmt_signed(str(_isa_std + _isa_dev))
            # TD
            td_s   = fmt_signed(_gf(div,"avg_temp_dev") or _gf(div,"oat_isa_dev"))
            # TRIPF
            burn_s = fmt_fuel(_gf(div,"fuel_trip") or _gf(div,"div_burn") or pt_div_burn)
            # MINREQ
            minfob_s = fmt_fuel(_gf(div,"fuel_min_reqd") or _gf(div,"min_fob")
                                 or _gf(div,"critical_fuel") or pt_crit_fuel)
            # TIME
            eet_d  = fmt_eet(_gf(div,"eet") or _gf(div,"div_time") or pt_div_time)
            win_s  = suitable_windows.get(dicao, "")

            _str_at(X_E,       "E")
            _str_at(X_ALTN,    dicao)
            _str_at(X_GCD,     dist_i)
            _str_at(X_FL,      fl_s)
            _str_at(X_COMP,    comp_s)
            _str_at(X_TMP,     tmp_s)
            _str_at(X_TD,      td_s)
            _str_at(X_AI,      ai_pct)
            _str_at(X_TRIPF,   burn_s)
            _str_at(X_MINREQ,  minfob_s)
            _str_at(X_TIME,    eet_d)
            # Clip window string to available right-margin space
            _win_max_w = (LM + CW - PAD) - X_WINDOW
            _win_vs    = _safe_latin1(str(win_s))
            while _win_vs and c.stringWidth(_win_vs, _mono, FS) > _win_max_w:
                _win_vs = _win_vs[:-1]
            _str_at(X_WINDOW, _win_vs)
            _advance(rule=False)

    def _section_rule():
        """Advance one blank gap row then draw a full-width rule below it."""
        _s.y -= ROW_H                          # blank gap row (no rule drawn)
        c.setStrokeColor(BLACK); c.setLineWidth(0.75)
        c.line(LM + 0.5, _s.y, LM + CW - 0.5, _s.y)

    _section_rule()
    emit_point(entry_point, "ETOPS_ENTRY")
    _section_rule()
    emit_point(etp_point,   "ETOPS_ETP")
    _section_rule()
    emit_point(exit_point,  "ETOPS_EXIT")

    # closing blank row + outer border
    _advance(rule=False)
    _border2(_s.box_top, _s.y)
    draw_footer(_s.page_num, _s.page_num + 1)

    # ===========================================================================
    # PAGE 2 — OCEANIC ROUTE VERIFICATION
    # ===========================================================================
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
            # Expand entry side only: SimBrief tags the entry fix with the
            # domestic (departure) FIR, so raw ocn_idx[0] is one fix too late.
            # The exit fix IS the last oceanic-FIR fix — do NOT expand xi,
            # or we pull in the first domestic fix after the ocean (e.g. TOD).
            raw_ei, raw_xi = ocn_idx[0], ocn_idx[-1]
            ei = max(0, raw_ei - 1)
            xi = raw_xi
        else:
            PROX = 0.40
            def cdist(la1,lo1,la2,lo2):
                try: return abs(float(la1)-float(la2))+abs(float(lo1)-float(lo2))
                except: return 9999
            ei = xi = None
            for ii, ff in enumerate(fixes):
                flat,flon = _gf(ff,"pos_lat"),_gf(ff,"pos_long")
                # entry: first fix near entry point
                if ei is None and cdist(flat,flon,entry_lat,entry_lon)<PROX: ei=ii
                # exit: keep updating so we get the LAST fix near exit point
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

    def fmt_lat_ocn(lf):
        hem="N" if lf>=0 else "S"; a=abs(lf); d=int(a); m=round((a-d)*600)
        return f"{hem}{d:02d}{m:03d}"
    def fmt_lon_ocn(lf):
        hem="E" if lf>=0 else "W"; a=abs(lf); d=int(a); m=round((a-d)*600)
        return f"{hem}{d:03d}{m:03d}"

    if ocn_fixes:
        # ORV absolute x positions
        XO_TO   = LM + 0.000 * CW + PAD
        XO_LAT  = LM + 0.220 * CW + PAD
        XO_LONG = LM + 0.370 * CW + PAD
        XO_TC   = LM + 0.540 * CW
        XO_MC   = LM + 0.640 * CW
        XO_MH   = LM + 0.740 * CW
        XO_DIST = LM + 0.840 * CW

        def _orv_col_header():
            total_h = HDR_H * 2
            c.setFillColor(NAV_BG)
            c.rect(LM, _s.y - total_h, CW, total_h, fill=1, stroke=0)
            c.setFont(_mono, FS); c.setFillColor(BLACK)
            row1_y = _s.y - HDR_H * 0.35
            row2_y = row1_y - HDR_H
            for x, label, row_y, right in [
                (XO_TO,   "TO",    row1_y, False),
                (XO_LAT,  "LAT",   row1_y, False),
                (XO_LONG, "LONG",  row1_y, False),
                (XO_TC,   "TC",    row1_y, True),
                (XO_MC,   "MC",    row1_y, True),
                (XO_MH,   "MH",    row1_y, True),
                (XO_DIST, "DIST",  row1_y, True),
                (XO_TO,   "IDENT", row2_y, False),
            ]:
                vs = _safe_latin1(label)
                if right:
                    sw = c.stringWidth(vs, _mono, FS)
                    c.drawString(x - sw, row_y, vs)
                else:
                    c.drawString(x, row_y, vs)
            c.setStrokeColor(BLACK); c.setLineWidth(1.0)
            c.line(LM+0.5, _s.y-total_h, LM+CW-0.5, _s.y-total_h)
            _s.y -= total_h

        def _orv_str(x, s, right_anchor=False):
            if not s: return
            vs = _safe_latin1(str(s))
            c.setFont(_mono, FS); c.setFillColor(BLACK)
            if right_anchor:
                sw = c.stringWidth(vs, _mono, FS)
                c.drawString(x - sw, _s.y - ROW_H + ROW_H * 0.25, vs)
            else:
                c.drawString(x, _s.y - ROW_H + ROW_H * 0.25, vs)

        c.showPage()
        _s.page_num += 1
        _s.y = PH - TM

        draw_page_header(_s.page_num)
        _s.box_top = _s.y

        _title_band("OCEANIC ROUTE VERIFICATION", grey=True)
        _orv_col_header()

        BOT_Y = BM + 20

        for ii, ff in enumerate(ocn_fixes):
            flat_f = _sfloat(_gf(ff,"pos_lat"))
            flon_f = _sfloat(_gf(ff,"pos_long"))
            name   = (_gf(ff,"name") or _gf(ff,"ident") or "").upper().strip()
            ident  = (_gf(ff,"ident") or "").upper().strip()
            lat_s2 = fmt_lat_ocn(flat_f)
            lon_s2 = fmt_lon_ocn(flon_f)
            is_last = (ii + 1 == len(ocn_fixes))
            has_ident_row = bool(ident and ident != name)
            row_lines = 2 if has_ident_row else 1

            if not is_last:
                nf  = ocn_fixes[ii+1]
                tcv = gc_bearing(flat_f,flon_f,
                                 _sfloat(_gf(nf,"pos_lat")),_sfloat(_gf(nf,"pos_long")))
                tc_s = f"{tcv:03d}" if tcv is not None else "---"
            else:
                tc_s = "---"

            try: mc_s = f"{int(round(float(_gf(ff,'track_mag')))):03d}"
            except: mc_s = "---"
            try: mh_s = f"{int(round(float(_gf(ff,'heading_mag')))):03d}"
            except: mh_s = "---"

            if is_last:
                dist_s = "----"; tc_s = mc_s = mh_s = "---"
            else:
                try: dist_s = f"{int(round(float(_gf(ff,'distance')))):04d}"
                except: dist_s = "----"

            if _s.y - ROW_H * (row_lines + 1) < BOT_Y:
                _border2(_s.box_top, _s.y)
                draw_footer(_s.page_num, _s.page_num)
                c.showPage()
                _s.page_num += 1; _s.y = PH - TM
                draw_page_header(_s.page_num)
                _s.box_top = _s.y
                _title_band("OCEANIC ROUTE VERIFICATION (cont.)", grey=True)
                _orv_col_header()

            # name row
            _orv_str(XO_TO,   name)
            _orv_str(XO_LAT,  lat_s2)
            _orv_str(XO_LONG, lon_s2)
            _orv_str(XO_TC,   tc_s,   right_anchor=True)
            _orv_str(XO_MC,   mc_s,   right_anchor=True)
            _orv_str(XO_MH,   mh_s,   right_anchor=True)
            _orv_str(XO_DIST, dist_s, right_anchor=True)
            _advance(rule=False)

            # ident row only when different from name
            if has_ident_row:
                _orv_str(XO_TO, ident)
                _advance(rule=False)

            # separator line + gap row between fixes
            _advance(rule=True)

        _advance(rule=False)
        _border2(_s.box_top, _s.y)
        draw_footer(_s.page_num, _s.page_num)

    c.save()
    return buf.getvalue()

def write_oceanic_route_verification(root, entry_lat, entry_lon, exit_lat, exit_lon):
    """
    Legacy stub — oceanic rendering is now handled inside _build_etops_oceanic_pdf()
    which is called by write_etops_section().  Returns "" so callers don't double-render.
    """
    return ""


# ---------------------------------------------------------------------------
# True oceanic FIR codes — used for both OEP detection and section gating.
# TJZS (San Juan) is deliberately excluded: it is a domestic FIR that
# borders the oceanic segment but is not itself an oceanic FIR.
# The ei-1 backtrack in _build_oceanic_only_pdf() pulls in the fix
# immediately before the first oceanic-FIR fix (e.g. KEEKA before NUBUS/KZWY).
# ---------------------------------------------------------------------------
_OCEANIC_FIRS = {
    "EGGX", "BIRD", "CZQO", "CZQX", "KZWY", "KZAK",
    "ENOB", "LPPO", "YMOR", "NZZO", "YMMM",
}


def _build_oceanic_only_pdf(root):
    """
    Render a standalone Oceanic Route Verification page for non-ETOPS flights.
    Uses identical styling to the ORV page inside _build_etops_oceanic_pdf().
    Returns raw PDF bytes, or None if no oceanic fixes are found.
    """
    import io
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors

    PW, PH = A4
    LM = 46; RM = 46; TM = 46; BM = 45
    CW = PW - LM - RM

    # ── Font — mirrors _build_etops_oceanic_pdf ───────────────────────────────
    if _cached_font_choice == "2":
        _fp = "/System/Library/Fonts/Menlo.ttc"
    elif _cached_font_choice == "3":
        _fp = _FONT_PATH_COURIER_NEW
    elif _cached_font_choice == "4":
        _fp = "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf"
    else:
        _fp = _FONT_PATH_COURIER_NORMAL
    _mono = "Courier"
    if _fp and os.path.exists(_fp):
        try:
            pdfmetrics.registerFont(TTFont("_OcnMono", _fp))
            _mono = "_OcnMono"
        except Exception:
            pass

    buf = io.BytesIO()
    c   = _rl_canvas.Canvas(buf, pagesize=A4)

    NAV_BG = colors.HexColor("#D8D8D8")
    BLACK  = colors.black
    FS     = 9.0
    ROW_H  = FS * 1.8
    HDR_H  = ROW_H * 1.05
    PAD    = 3

    # ORV column positions — identical to _build_etops_oceanic_pdf
    XO_TO   = LM + 0.000 * CW + PAD
    XO_LAT  = LM + 0.220 * CW + PAD
    XO_LONG = LM + 0.370 * CW + PAD
    XO_TC   = LM + 0.540 * CW
    XO_MC   = LM + 0.640 * CW
    XO_MH   = LM + 0.740 * CW
    XO_DIST = LM + 0.840 * CW

    class _PS:
        y        = PH - TM
        page_num = 1
        box_top  = PH - TM
    _s = _PS()

    def _gf(elem, tag):
        n = elem.find(tag)
        return n.text.strip() if n is not None and n.text else ""

    def _sf(v, d=0.0):
        try: return float(v)
        except: return d

    def fmt_lat(lf):
        hem = "N" if lf >= 0 else "S"; a = abs(lf); d = int(a); m = round((a - d) * 600)
        return f"{hem}{d:02d}{m:03d}"

    def fmt_lon(lf):
        hem = "E" if lf >= 0 else "W"; a = abs(lf); d = int(a); m = round((a - d) * 600)
        return f"{hem}{d:03d}{m:03d}"

    def gc_bearing(la1, lo1, la2, lo2):
        try:
            la1, lo1, la2, lo2 = map(math.radians, [la1, lo1, la2, lo2])
            dlo = lo2 - lo1
            x_ = math.sin(dlo) * math.cos(la2)
            y_ = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
            return round(math.degrees(math.atan2(x_, y_)) % 360)
        except: return None

    def _rule():
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.line(LM + 0.5, _s.y - ROW_H, LM + CW - 0.5, _s.y - ROW_H)

    def _advance(rule=True):
        if rule: _rule()
        _s.y -= ROW_H

    def _border2(top_y, bot_y):
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.rect(LM, bot_y, CW, top_y - bot_y, fill=0, stroke=1)

    def _title_band(text, grey=False):
        c.setFillColor(NAV_BG if grey else colors.white)
        c.rect(LM, _s.y - HDR_H, CW, HDR_H, fill=1, stroke=0)
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        c.drawCentredString(PW / 2, _s.y - HDR_H + (HDR_H - FS) / 2 + 1, _safe_latin1(text))
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.line(LM + 0.5, _s.y - HDR_H, LM + CW - 0.5, _s.y - HDR_H)
        _s.y -= HDR_H

    def draw_page_header(pg):
        rel  = (root.findtext("general/release") or "").strip()
        orig = (root.findtext("origin/icao_code") or "").strip()
        dest = (root.findtext("destination/icao_code") or "").strip()
        flt  = ''.join(ch for ch in ((root.findtext("general/icao_airline") or "") +
               (root.findtext("general/flight_number") or "")).strip()
               if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789- /")
        try:
            ts = int(root.findtext("times/sched_out") or "0")
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%b-%d %H:%M:%SZ").upper()
        except: dt = ""
        _rel_ver = rel or __version__
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        c.drawString(LM, PH - 36,
                     _safe_latin1(f"PAGE {pg}  RELEASE {_rel_ver}  {dt}  {orig}-{dest}  {flt}"))
        _s.y = PH - TM

    def _orv_col_header():
        total_h = HDR_H * 2
        c.setFillColor(NAV_BG)
        c.rect(LM, _s.y - total_h, CW, total_h, fill=1, stroke=0)
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        row1_y = _s.y - HDR_H * 0.35
        row2_y = row1_y - HDR_H
        for x, label, ry, right in [
            (XO_TO,   "TO",    row1_y, False),
            (XO_LAT,  "LAT",   row1_y, False),
            (XO_LONG, "LONG",  row1_y, False),
            (XO_TC,   "TC",    row1_y, True),
            (XO_MC,   "MC",    row1_y, True),
            (XO_MH,   "MH",    row1_y, True),
            (XO_DIST, "DIST",  row1_y, True),
            (XO_TO,   "IDENT", row2_y, False),
        ]:
            vs = _safe_latin1(label)
            if right:
                c.drawString(x - c.stringWidth(vs, _mono, FS), ry, vs)
            else:
                c.drawString(x, ry, vs)
        c.setStrokeColor(BLACK); c.setLineWidth(1.0)
        c.line(LM + 0.5, _s.y - total_h, LM + CW - 0.5, _s.y - total_h)
        _s.y -= total_h

    def _orv_str(x, s, right_anchor=False):
        if not s: return
        vs = _safe_latin1(str(s))
        c.setFont(_mono, FS); c.setFillColor(BLACK)
        if right_anchor:
            c.drawString(x - c.stringWidth(vs, _mono, FS),
                         _s.y - ROW_H + ROW_H * 0.25, vs)
        else:
            c.drawString(x, _s.y - ROW_H + ROW_H * 0.25, vs)

    # ── Locate oceanic fixes (same logic as _build_etops_oceanic_pdf pg 2) ────
    all_fixes = root.findall("navlog/fix")
    ocn_idx = [i for i, f in enumerate(all_fixes)
               if (_gf(f, "fir") or "").strip().upper() in _OCEANIC_FIRS]
    if not ocn_idx:
        LOG.debug("[OCN] No fixes in oceanic FIRs — skipping ORV")
        return None
    # Back up one fix on the entry side so the domestic OEP (e.g. KEEKA in TJZS)
    # is included as the first row of the table.
    ei = max(0, ocn_idx[0] - 1)
    xi = ocn_idx[-1]
    ocn_fixes = all_fixes[ei:xi + 1]
    if not ocn_fixes:
        return None

    LOG.debug(f"[OCN] ORV: {len(ocn_fixes)} fixes, "
              f"OEP={_gf(ocn_fixes[0],'ident')} OXP={_gf(ocn_fixes[-1],'ident')}")

    # ── Render ────────────────────────────────────────────────────────────────
    BOT_Y = BM + 20
    draw_page_header(_s.page_num)
    _s.box_top = _s.y
    _title_band("OCEANIC ROUTE VERIFICATION", grey=True)
    _orv_col_header()

    for ii, ff in enumerate(ocn_fixes):
        flat  = _sf(_gf(ff, "pos_lat"))
        flon  = _sf(_gf(ff, "pos_long"))
        name  = (_gf(ff, "name")  or _gf(ff, "ident") or "").upper().strip()
        ident = (_gf(ff, "ident") or "").upper().strip()
        is_last = (ii + 1 == len(ocn_fixes))

        if not is_last:
            nf  = ocn_fixes[ii + 1]
            tcv = gc_bearing(flat, flon, _sf(_gf(nf, "pos_lat")), _sf(_gf(nf, "pos_long")))
            tc_s = f"{tcv:03d}" if tcv is not None else "---"
        else:
            tc_s = "---"

        try: mc_s = f"{int(round(float(_gf(ff, 'track_mag')))):03d}"
        except: mc_s = "---"
        try: mh_s = f"{int(round(float(_gf(ff, 'heading_mag')))):03d}"
        except: mh_s = "---"

        if is_last:
            dist_s = "----"; tc_s = mc_s = mh_s = "---"
        else:
            try: dist_s = f"{int(round(float(_gf(ff, 'distance')))):04d}"
            except: dist_s = "----"

        has_ident_row = bool(ident and ident != name)
        row_lines = 2 if has_ident_row else 1

        if _s.y - ROW_H * (row_lines + 1) < BOT_Y:
            _border2(_s.box_top, _s.y)
            c.showPage()
            _s.page_num += 1; _s.y = PH - TM
            draw_page_header(_s.page_num)
            _s.box_top = _s.y
            _title_band("OCEANIC ROUTE VERIFICATION (cont.)", grey=True)
            _orv_col_header()

        _orv_str(XO_TO,   name)
        _orv_str(XO_LAT,  fmt_lat(flat))
        _orv_str(XO_LONG, fmt_lon(flon))
        _orv_str(XO_TC,   tc_s,   right_anchor=True)
        _orv_str(XO_MC,   mc_s,   right_anchor=True)
        _orv_str(XO_MH,   mh_s,   right_anchor=True)
        _orv_str(XO_DIST, dist_s, right_anchor=True)
        _advance(rule=False)

        if has_ident_row:
            _orv_str(XO_TO, ident)
            _advance(rule=False)

        _advance(rule=True)   # separator rule between fixes

    _advance(rule=False)
    _border2(_s.box_top, _s.y)
    c.save()
    return buf.getvalue()


def write_oceanic_section(root):
    """
    Emit a standalone ORV page for non-ETOPS oceanic routes.
    Skipped when is_etops=1 (write_etops_section already renders ORV).
    Triggered when any fir_enroute in the XML is in _OCEANIC_FIRS.
    Returns [PDF_BLOB:...] marker string or ''.
    """
    try:
        if (root.findtext("general/is_etops") or "0").strip() == "1":
            return ""  # ETOPS path handles ORV
        enroute_firs = {
            f.text.strip().upper()
            for f in root.findall("general/fir_enroute")
            if f.text and f.text.strip()
        }
        if not enroute_firs & _OCEANIC_FIRS:
            LOG.debug("[OCN] No oceanic FIR in fir_enroute — skipping")
            return ""
        pdf_bytes = _build_oceanic_only_pdf(root)
        if not pdf_bytes:
            return ""
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        LOG.debug(f"[OCN] Standalone ORV PDF built, {len(pdf_bytes)} bytes")
        return "\n[PDF_BLOB:" + b64 + "]\n"
    except Exception as e:
        LOG.error(f"[OCN] Error generating oceanic section: {e}")
        traceback.print_exc()
        return ""


def write_etops_section(root):
    """
    Generate ETOPS Critical Fuel Scenario + Oceanic Route Verification pages
    using ReportLab for rich layout.  Returns a [PDF_BLOB:...] marker string
    that save_as_pdf() knows how to splice in, or "" if ETOPS is not active.
    """

    try:
        LOG.debug("[ETOPS] Starting write_etops_section (ReportLab)")
        is_etops = (root.findtext("general/is_etops") or "0").strip()
        LOG.debug(f"[DBG ETOPS: is_etops = '{is_etops}'")
        if is_etops != "1":
            LOG.debug("[ETOPS] Not active")
            return ""

        etops_section = root.find("etops")
        if etops_section is None:
            LOG.debug("[ETOPS] No <etops> element in XML")
            return ""

        if not etops_section.findtext("rule","").strip():
            LOG.debug("[ETOPS] No ETOPS rule found")
            return ""

        pdf_bytes = _build_etops_oceanic_pdf(root)
        if not pdf_bytes:
            LOG.debug("[ETOPS] Empty PDF returned")
            return ""

        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        marker = f"[PDF_BLOB:{b64}]"
        LOG.debug(f"[DBG ETOPS: ReportLab PDF built, {len(pdf_bytes)} bytes")
        return "\n" + marker + "\n"

    except Exception as e:
        LOG.error(f"Error generating ETOPS section: {e}")

        traceback.print_exc()
        return ""


    # NOTE: Legacy text-based oceanic route verification removed.
    # Rendering is handled by _build_etops_oceanic_pdf() via write_etops_section().








# ===========================================================================
# Navigation log & forecast winds
# The NAVLOG section and the winds-aloft forecast table.
# ===========================================================================


def write_navigation_log(root, flight_info, takeoff_time, fixes_per_page=None):
    """Navigation log matching SimBrief OFP PDF column layout exactly.

    Two lines per fix:
      Line 1: TO / LAT / LONG / MC / MK / GS / TD / SD / ST / SB
      Line 2: IDENT / FL / WIND / WCP / MH / TRR / TAS / [I] / [TLDR] / TTLT / TTLB / TH

    Column spec verified character-by-character against the SimBrief OFP PDF.
    """

    HEADER = (
        "TO            LAT    LONG    MC  MK  GS  TD   SD   ST   SB\n"
        "IDENT      FL WIND   WCP     MH  TRR TAS I   TLDR TTLT TTLB  TH\n"
        + "-" * 68 + "\n"
    )

    def _sf(val, default=0.0):
        """Safe float conversion."""
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _gt(elem, tag):
        """Get text content of a child element."""
        n = elem.find(tag)
        return n.text.strip() if n is not None and n.text else ""

    def _fmt_lat(lat_str):
        """Decimal degrees → NDDMMT (hemisphere, degrees, minutes, tenths).
        e.g. '36.8946' → 'N36537'  (36 deg 53.7 min)
        """
        if not lat_str:
            return "      "
        try:
            v   = float(lat_str)
            hem = "N" if v >= 0 else "S"
            a   = abs(v)
            d   = int(a)
            m10 = round((a - d) * 600)          # minutes x10
            if m10 >= 600:                      # 59.95+ rounds up a degree
                d  += 1
                m10 = 0
            return f"{hem}{d:02d}{m10:03d}"
        except (TypeError, ValueError):
            return "      "

    def _fmt_lon(lon_str):
        """Decimal degrees → EDDDMMT (hemisphere, degrees, minutes, tenths).
        e.g. '-76.2012' → 'W076121'  (76 deg 12.1 min)
        """
        if not lon_str:
            return "       "
        try:
            v   = float(lon_str)
            hem = "E" if v >= 0 else "W"
            a   = abs(v)
            d   = int(a)
            m10 = round((a - d) * 600)
            if m10 >= 600:
                d  += 1
                m10 = 0
            return f"{hem}{d:03d}{m10:03d}"
        except (TypeError, ValueError):
            return "       "

    def _fmt_signed(val_str, width=2):
        """Signed value → M/P + zero-padded absolute.  e.g. '-11' → 'M11', '4' → 'P04'."""
        if not val_str:
            return " " * (width + 1)
        try:
            v = float(val_str)
            sign = "P" if v >= 0 else "M"
            return f"{sign}{abs(int(round(v))):0{width}d}"
        except (TypeError, ValueError):
            return " " * (width + 1)

    def _fmt_hhmm(sec_str):
        """Seconds → HHMM string.  e.g. '300' → '0005', '3600' → '0100'."""
        if not sec_str:
            return "    "
        try:
            s = int(float(sec_str))
            h = s // 3600
            m = (s % 3600) // 60
            return f"{h:02d}{m:02d}"
        except (TypeError, ValueError):
            return "    "

    def _fmt_fuel(fuel_str):
        """Fuel lbs → 4-digit value in hundreds.  e.g. '3900' → '0039'."""
        if not fuel_str:
            return "    "
        try:
            return f"{int(round(float(fuel_str) / 100)):04d}"
        except (TypeError, ValueError):
            return "    "

    def _line1(name, lat, lon, mc, mk, gs, td, sd, st, sb):
        """Build line 1: name(≥10 left-padded, no truncation) SP lat(6) SP lon(7) SP mc(3) SP mk(3) SP gs(3) SP td(3) SP sd(4) SP st(4) SP sb(4).

        Long names like "TOP OF CLIMB" (12 chars) overflow naturally — consistent with the PDF.
        """
        n_s   = f"{name[:14]:<14}"   # hard clip at 14 chars (e.g. CHICAGO O'HARE)
        lat_s = _fmt_lat(lat)   # 6 chars
        lon_s = _fmt_lon(lon)   # 7 chars
        mc_s  = f"{int(float(mc)):>3}"  if mc  else "   "
        mk_s  = f"{int(float(mk)):>3}"  if mk  else "   "
        gs_s  = f"{int(float(gs)):>3}"  if gs  else "   "
        td_s  = _fmt_signed(td)         if td  else "   "
        sd_s  = f"{int(float(sd)):04d}" if sd  else "    "
        st_s  = _fmt_hhmm(st)
        sb_s  = _fmt_fuel(sb)
        return f"{n_s} {lat_s} {lon_s} {mc_s} {mk_s} {gs_s} {td_s} {sd_s} {st_s} {sb_s}"

    def _line2(ident, fl, wind_dir, wind_spd, wcp, mh, trr, tas, i_col, ttlt, ttlb, tldr, th):
        """Build line 2: ident(14) fl(3) SP wind(6) SP wcp(3) SP*5 mh(3) SP trr(3) SP tail...

        Tail section is variable-width (I and TLDR only appear when present):
          - No I, no TLDR : TAS SP TTLT SP TTLB SP TH
          - TLDR only      : TAS SP*4 TLDR(3) SP TTLT SP TTLB SP TH
          - I + TLDR       : TAS SP*3 I(1) SP*2 TLDR(3) SP TTLT SP TTLB SP TH
        """
        id_s   = f"{(ident or ''):<14}"[:14]
        fl_s   = f"{int(float(fl)):>3}"  if fl  else "   "
        if wind_dir and wind_spd:
            w_s    = f"{int(float(wind_dir)):03d}{int(float(wind_spd)):03d}"
            w_gap  = f" {w_s} "   # space + 6-char wind + space → 8 chars total before WCP
        else:
            w_s    = ""
            w_gap  = "     "      # 5 spaces when no wind (matches PDF: FL→WCP gap = 5)
        wcp_s  = _fmt_signed(wcp)        if wcp  else "   "
        mh_s   = f"{int(float(mh)):>3}"  if mh   else "   "
        trr_v  = int(float(trr)) if trr else 0
        trr_s  = f"{trr_v:03d}"          if trr_v > 0 else "   "
        tas_s  = f"{int(float(tas)):>3}" if tas  else "   "
        ttlt_s = _fmt_hhmm(ttlt)
        ttlb_s = _fmt_fuel(ttlb)
        th_s   = f"{int(float(th)):>2}"  if th   else "  "

        has_tldr = bool(tldr and _sf(tldr) > 0)
        has_i    = bool(i_col and str(i_col).strip() not in ("", "0", "---"))

        if has_i and has_tldr:
            i_s    = str(i_col).strip()[:1]
            tldr_s = f"{int(float(tldr)):3d}"
            tail   = f"{tas_s}   {i_s} {tldr_s} {ttlt_s} {ttlb_s} {th_s}"
        elif has_tldr:
            tldr_s = f"{int(float(tldr)):3d}"
            tail   = f"{tas_s}     {tldr_s} {ttlt_s} {ttlb_s} {th_s}"
        elif has_i:
            i_s    = str(i_col).strip()[:1]
            tail   = f"{tas_s}   {i_s}   {ttlt_s} {ttlb_s} {th_s}"
        else:
            tail   = f"{tas_s} {ttlt_s} {ttlb_s} {th_s}"

        return f"{id_s}{fl_s}{w_gap}{wcp_s}     {mh_s} {trr_s} {tail}"

    # ── Build the navlog ─────────────────────────────────────────────────────
    fixes = root.findall("navlog/fix")
    total_dist = sum(_sf(_gt(f, "distance")) for f in fixes)
    cum_dist = []
    run = 0.0
    for f in fixes:
        run += _sf(_gt(f, "distance"))
        cum_dist.append(run)

    nav_log = HEADER

    # ── Helper: read any XML path, return stripped string or "" ──────────────
    def _rp(*paths):
        for p in paths:
            v = (root.findtext(p) or "").strip()
            if v: return v
        return ""

    # ── Departure airport row (e.g. KORF) ─────────────────────────────────────
    # SimBrief does not include origin/dest airports in navlog/fix — build synthetically.
    orig_icao    = _rp("origin/icao_code").upper()
    orig_elev    = _rp("origin/elevation", "departure_airport/elevation")
    orig_lat_raw = ""
    orig_lon_raw = ""
    _rwy_lat = _rwy_lon = ""
    for _f in fixes:
        _fname = (_gt(_f, "name") or "").strip().upper()
        _flat  = _gt(_f, "pos_lat")
        _flon  = _gt(_f, "pos_long")
        if _fname == orig_icao and _flat and _flon:
            orig_lat_raw, orig_lon_raw = _flat, _flon
            break
        if _fname == "RWY" and _flat and not _rwy_lat:
            _rwy_lat, _rwy_lon = _flat, _flon
    if not orig_lat_raw:
        orig_lat_raw = _rwy_lat or _rp("departure_airport/pos_lat", "origin/pos_lat")
        orig_lon_raw = _rwy_lon or _rp("departure_airport/pos_long", "origin/pos_long")
    # Elevation ident: "26FT". FL: always 0 (ground). TLDR: total route distance.
    try:    orig_elev_ft = f"{int(float(orig_elev))}FT" if orig_elev else ""
    except: orig_elev_ft = ""
    orig_tldr = str(int(round(total_dist))) if total_dist > 0 else ""
    # Strip letter suffixes from runway (e.g. "14L" -> "14", "22R" -> "22")
    orig_rwy_num = "0"  # departure airport always on ground (FL=0)

    if orig_icao and orig_lat_raw and orig_lon_raw:
        nav_log += _line1(orig_icao, orig_lat_raw, orig_lon_raw,
                          "", "", "", "", "", "", "") + "\n"
        nav_log += _line2(orig_elev_ft, orig_rwy_num, "", "", "", "", "", "", "",
                          "", "", orig_tldr, "") + "\n"
        nav_log += "-" * 68 + "\n"

    for idx, f in enumerate(fixes):
        name = (_gt(f, "name") or "XXXX").upper()
        ident = _gt(f, "ident") or ""

        # SimBrief uses "TOP OF DESCENT" internally; keep it
        # (PDF shows "TOP OF DESCENT" on line 1, "TOD" as ident on line 2)

        lat      = _gt(f, "pos_lat")
        lon      = _gt(f, "pos_long")
        mc       = _gt(f, "track_mag")
        gs       = _gt(f, "groundspeed")
        td       = _gt(f, "oat_isa_dev")
        sd_raw   = _gt(f, "distance")
        st_raw   = _gt(f, "time_leg")
        sb_raw   = _gt(f, "fuel_leg")

        # MK: SimBrief stores Mach (e.g. 0.780) → display as integer×1000 (780)
        mk_raw = _gt(f, "mach")
        try:
            mk = str(int(float(mk_raw) * 1000)) if mk_raw else ""
        except (TypeError, ValueError):
            mk = ""

        # SD: leg distance in nm, 4-digit zero-padded
        try:
            sd = f"{int(round(_sf(sd_raw))):04d}"
        except (TypeError, ValueError):
            sd = "0000"

        fl_raw = _gt(f, "altitude_feet")
        try:
            fl = str(int(_sf(fl_raw) // 100))   # altitude_feet/100 → FL
        except (TypeError, ValueError):
            fl = "0"

        wind_dir = _gt(f, "wind_dir")
        wind_spd = _gt(f, "wind_spd")
        wcp      = _gt(f, "wind_component")
        mh       = _gt(f, "heading_mag")

        mora_raw = _gt(f, "mora")
        try:
            trr = str(int(_sf(mora_raw) // 100)) if mora_raw else ""
        except (TypeError, ValueError):
            trr = ""

        tas     = _gt(f, "true_airspeed")
        i_col   = _gt(f, "shear") or ""

        ttlt    = _gt(f, "time_total")
        ttlb    = _gt(f, "fuel_totalused")
        th_raw  = _gt(f, "tropopause_feet")
        try:
            th = str(int(_sf(th_raw) // 1000)) if th_raw else ""
        except (TypeError, ValueError):
            th = ""

        # TLDR: remaining distance (total − cumulative so far)
        tldr_val = total_dist - cum_dist[idx]
        tldr = str(int(round(tldr_val))) if tldr_val > 0 else ""

        nav_log += _line1(name, lat, lon, mc, mk, gs, td, sd, st_raw, sb_raw) + "\n"
        nav_log += _line2(ident, fl, wind_dir, wind_spd, wcp, mh, trr, tas,
                          i_col, ttlt, ttlb, tldr, th) + "\n"
        nav_log += "-" * 68 + "\n"

    # ── Destination airport row (e.g. KORD) ───────────────────────────────────
    dest_icao    = _rp("destination/icao_code").upper()
    dest_elev    = _rp("destination/elevation", "arrival_airport/elevation")
    dest_lat_raw = ""
    dest_lon_raw = ""
    _dest_rwy_lat = _dest_rwy_lon = ""
    for _f in reversed(fixes):
        _fname = (_gt(_f, "name") or "").strip().upper()
        _flat  = _gt(_f, "pos_lat")
        _flon  = _gt(_f, "pos_long")
        if _fname == dest_icao and _flat and _flon:
            dest_lat_raw, dest_lon_raw = _flat, _flon
            break
        if _fname == "RWY" and _flat and not _dest_rwy_lat:
            _dest_rwy_lat, _dest_rwy_lon = _flat, _flon
    if not dest_lat_raw:
        dest_lat_raw = _dest_rwy_lat or _rp("arrival_airport/pos_lat", "destination/pos_lat")
        dest_lon_raw = _dest_rwy_lon or _rp("arrival_airport/pos_long", "destination/pos_long")
    dest_rwy     = _rp("destination/plan_rwy")
    try:    dest_elev_ft = f"{int(float(dest_elev))}FT" if dest_elev else ""
    except: dest_elev_ft = ""
    # Strip letter suffixes from runway (e.g. "22L" -> "22", "4R" -> "4")
    dest_rwy_num = re.sub(r"[^0-9]", "", dest_rwy) or "0"  # strip e.g. '22L'->'22'
    # TTLT/TTLB for dest = cumulative trip time/fuel from last fix
    last_fix   = fixes[-1] if fixes else None
    trip_time  = _gt(last_fix, "time_total")     if last_fix is not None else ""
    trip_fuel  = _gt(last_fix, "fuel_totalused") if last_fix is not None else ""
    last_td    = _gt(last_fix, "oat_isa_dev")    if last_fix is not None else ""

    if dest_icao and dest_lat_raw and dest_lon_raw:
        nav_log += _line1(dest_icao, dest_lat_raw, dest_lon_raw,
                          "", "", "", last_td, "0", "", "") + "\n"
        nav_log += _line2(dest_elev_ft, dest_rwy_num, "", "", "", "", "", "", "",
                          trip_time, trip_fuel, "", "") + "\n"
        nav_log += "-" * 68 + "\n"

    # ── Alternate route navlogs — loop over ALL alternate_navlog elements ────────
    # SimBrief emits one <alternate_navlog> per alternate, in the same order as
    # the <alternate> elements.  We pair them by index so the banner shows the
    # correct destination airport name for each alternate.
    _altn_nl_nodes  = root.findall("alternate_navlog")
    _altn_ap_nodes  = root.findall("alternate")          # for airport metadata

    for _ani, _altn_nl in enumerate(_altn_nl_nodes):
        altn_fixes = _altn_nl.findall("fix")
        if not altn_fixes:
            continue

        # Resolve the alternate airport node for this index (if available)
        _altn_ap = _altn_ap_nodes[_ani] if _ani < len(_altn_ap_nodes) else None

        altn_icao = (
            (_altn_ap.findtext("icao_code") if _altn_ap is not None else None)
            or (_altn_ap.findtext("iata_code") if _altn_ap is not None else None)
            or "ALTN"
        ).strip().upper()

        # Banner label: "DESTINATION ALTERNATE ROUTE TO MMGL"
        #               "DESTINATION ALTERNATE 2 ROUTE TO MMQT"
        _altn_seq  = f" {_ani + 1}" if _ani > 0 else ""
        nav_log += f"[ALTN_BANNER:DESTINATION ALTERNATE{_altn_seq} ROUTE TO {altn_icao}]\n"
        nav_log += HEADER

        # Cumulative distance for this alternate leg
        altn_total_dist = sum(_sf(_gt(f, "distance")) for f in altn_fixes)
        altn_cum_dist   = []
        _run = 0.0
        for f in altn_fixes:
            _run += _sf(_gt(f, "distance"))
            altn_cum_dist.append(_run)

        # Departure point = destination airport (same for every alternate)
        if dest_icao and dest_lat_raw and dest_lon_raw:
            altn_rwy     = _rp("destination/plan_rwy")
            altn_rwy_num = re.sub(r"[^0-9]", "", altn_rwy) or "0"
            altn_tldr    = str(int(round(altn_total_dist))) if altn_total_dist > 0 else ""
            nav_log += _line1(dest_icao, dest_lat_raw, dest_lon_raw,
                              "", "", "", "", "", "", "") + "\n"
            nav_log += _line2(dest_elev_ft, altn_rwy_num, "", "", "", "", "", "", "",
                              "", "", altn_tldr, "") + "\n"
            nav_log += "-" * 68 + "\n"

        for idx, f in enumerate(altn_fixes):
            name  = (_gt(f, "name")  or "XXXX").upper()
            ident = (_gt(f, "ident") or "")

            lat    = _gt(f, "pos_lat")
            lon    = _gt(f, "pos_long")
            mc     = _gt(f, "track_mag")
            gs     = _gt(f, "groundspeed")
            td     = _gt(f, "oat_isa_dev")
            sd_raw = _gt(f, "distance")
            st_raw = _gt(f, "time_leg")
            sb_raw = _gt(f, "fuel_leg")

            mk_raw = _gt(f, "mach")
            try:    mk = str(int(float(mk_raw) * 1000)) if mk_raw else ""
            except: mk = ""

            fl_raw = _gt(f, "altitude_feet")
            try:    fl = str(int(_sf(fl_raw) // 100))
            except: fl = "0"

            wind_dir = _gt(f, "wind_dir")
            wind_spd = _gt(f, "wind_spd")
            wcp      = _gt(f, "wind_component")
            mh       = _gt(f, "heading_mag")

            mora_raw = _gt(f, "mora")
            try:    trr = str(int(_sf(mora_raw) // 100)) if mora_raw else ""
            except: trr = ""

            tas    = _gt(f, "true_airspeed")
            i_col  = _gt(f, "shear") or ""
            ttlt   = _gt(f, "time_total")
            ttlb   = _gt(f, "fuel_totalused")
            th_raw = _gt(f, "tropopause_feet")
            try:    th = str(int(_sf(th_raw) // 1000)) if th_raw else ""
            except: th = ""

            tldr_val = altn_total_dist - altn_cum_dist[idx]
            tldr = str(int(round(tldr_val))) if tldr_val > 0 else ""

            nav_log += _line1(name, lat, lon, mc, mk, gs, td, sd_raw, st_raw, sb_raw) + "\n"
            nav_log += _line2(ident, fl, wind_dir, wind_spd, wcp, mh, trr, tas,
                              i_col, ttlt, ttlb, tldr, th) + "\n"
            nav_log += "-" * 68 + "\n"

        # Alternate destination airport row
        altn_elev    = (_altn_ap.findtext("elevation") or "" if _altn_ap is not None else "").strip()
        altn_lat_raw = (_altn_ap.findtext("pos_lat")   or "" if _altn_ap is not None else "").strip()
        altn_lon_raw = (_altn_ap.findtext("pos_long")  or "" if _altn_ap is not None else "").strip()
        altn_rwy_dest     = (_altn_ap.findtext("plan_rwy") or "" if _altn_ap is not None else "").strip()
        altn_rwy_dest_num = re.sub(r"[^0-9]", "", altn_rwy_dest) or "0"
        try:    altn_elev_ft = f"{int(float(altn_elev))}FT" if altn_elev else ""
        except: altn_elev_ft = ""
        last_af    = altn_fixes[-1] if altn_fixes else None
        altn_ttime = _gt(last_af, "time_total")     if last_af is not None else ""
        altn_tfuel = _gt(last_af, "fuel_totalused") if last_af is not None else ""
        if altn_icao and altn_lat_raw and altn_lon_raw:
            nav_log += _line1(altn_icao, altn_lat_raw, altn_lon_raw,
                              "", "", "", "", "0", "", "") + "\n"
            nav_log += _line2(altn_elev_ft, altn_rwy_dest_num, "", "", "", "", "", "", "",
                              altn_ttime, altn_tfuel, "", "") + "\n"
            nav_log += "-" * 68 + "\n"

    return nav_log




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

    # ── Forecast winds table — ENRT WX format matching SimBrief ─────────────
    winds = ""

    # ISA standard temperature at altitude (2.0°C per 1000ft up to 36089ft)
    def _isa_temp(alt_ft):
        return (15.0 - 2.0 * (alt_ft / 1000.0)) if alt_ft <= 36089 else -56.5

    # Format one cell: sign(P/M) + 2-digit ISA deviation + '/' + 3-digit dir + 3-digit spd
    def _wind_cell(oat_str, dir_str, spd_str, alt_ft):
        try:
            oat = int(oat_str)
            td  = round(oat - _isa_temp(alt_ft))
            sign = 'P' if td >= 0 else 'M'
            td_s = f"{sign}{abs(td):02d}"
        except (ValueError, TypeError):
            td_s = "---"
        try:
            d = f"{int(dir_str):03d}"
        except (ValueError, TypeError):
            d = "---"
        try:
            s = f"{int(spd_str):03d}"
        except (ValueError, TypeError):
            s = "---"
        return f"{td_s}/{d}{s}"

    TARGET_ALTS = [18000, 24000, 30000, 34000]
    FL_LABELS   = [f"FL{a // 100}" for a in TARGET_ALTS]
    COL_W = 16   # wider columns match SimBrief spacing

    # Build header
    hdr1 = "ENRT WX  " + "".join(f"{lbl:<{COL_W}}" for lbl in FL_LABELS)
    hdr2 = "         " + "".join(f"{'TD WIND':<{COL_W}}" for _ in FL_LABELS)
    winds += hdr1 + "\n" + hdr2 + "\n"

    # Build per-fix wind lookup: {ident: {alt: (oat, dir, spd)}}
    fix_wind_data = {}
    fix_order     = []   # ordered list of (origin_icao?, ident) to match SimBrief layout
    origin_icao   = (root.findtext("origin/icao_code") or "").strip().upper()
    dest_icao     = (root.findtext("destination/icao_code") or "").strip().upper()

    for fix in root.findall("navlog/fix"):
        ident = (fix.findtext("ident") or fix.findtext("name") or "").strip().upper()
        if not ident:
            continue
        wd = fix.find("wind_data")
        if wd is None:
            continue
        levels = {}
        for lvl in wd.findall("level"):
            try:
                a = int(lvl.findtext("altitude", "0"))
            except ValueError:
                continue
            levels[a] = (
                lvl.findtext("oat",      "0"),
                lvl.findtext("wind_dir", "0"),
                lvl.findtext("wind_spd", "0"),
            )
        fix_wind_data[ident] = levels
        fix_order.append(ident)

    # Deduplicate while preserving order
    seen_idents = set()
    ordered_idents = []
    for ident in fix_order:
        if ident not in seen_idents:
            seen_idents.add(ident)
            ordered_idents.append(ident)

    # Emit origin airport label, then all fixes, then destination label
    winds += f"{origin_icao}\n"
    for ident in ordered_idents:
        levels = fix_wind_data[ident]
        row = f"{ident:<9}"
        for alt in TARGET_ALTS:
            if alt in levels:
                oat_s, dir_s, spd_s = levels[alt]
                row += f"{_wind_cell(oat_s, dir_s, spd_s, alt):<{COL_W}}"
            else:
                row += f"{'---/------':<{COL_W}}"
        winds += row + "\n"
    winds += f"{dest_icao}\n"
    winds += "\n"

    # ── Pull weather directly from airport XML sections ───────────────────────
    NETWORK_PRIORITY = {"real-world": 0, "pilotedge": 1, "vatsim": 2, "ivao": 3}

    # Index alternates by position — must be defined before _from_section
    _altn_ap_by_idx = {i: node for i, node in enumerate(root.findall("alternate"))}

    def _from_section(section_tag):
        # Resolve sentinel tags for indexed alternates
        import re as _re2
        _altn_idx_m = _re2.match(r'^__altn_(\d+)__$', section_tag)
        if _altn_idx_m:
            sec = _altn_ap_by_idx.get(int(_altn_idx_m.group(1)))
        else:
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
    # Build alternate entries dynamically from all <alternate> nodes
    _altn_wx_entries = []
    _altn_wx_nodes   = root.findall("weather/altn_metar")
    _altn_taf_nodes  = root.findall("weather/altn_taf")
    _altn_atis_nodes = root.findall("weather/altn_atis")
    _altn_ap_nodes   = root.findall("alternate")
    _num_altns = max(len(_altn_ap_nodes), len(_altn_wx_nodes))
    for _ai in range(_num_altns):
        _lbl   = "ALTN 1" if _ai == 0 else f"ALTN {_ai + 1}"
        _am    = _altn_wx_nodes[_ai].text.strip()   if _ai < len(_altn_wx_nodes) and _altn_wx_nodes[_ai].text   else (altn_metar if _ai == 0 else None)
        _at    = _altn_taf_nodes[_ai].text.strip()  if _ai < len(_altn_taf_nodes) and _altn_taf_nodes[_ai].text  else (altn_taf  if _ai == 0 else None)
        _aa    = _altn_atis_nodes[_ai].text.strip() if _ai < len(_altn_atis_nodes) and _altn_atis_nodes[_ai].text else (altn_atis if _ai == 0 else None)
        # Use the indexed alternate XML node as the section root for airport metadata
        _altn_tag = f"__altn_{_ai}__"   # sentinel — resolved below in _parse_section
        _altn_wx_entries.append((_altn_tag, _lbl, _am, _at, _aa, _ai))

    SECTIONS = [
        ("origin",            "DEPARTURE",    orig_metar,   orig_taf,   orig_atis),
        ("destination",       "DESTINATION",  dest_metar,   dest_taf,   dest_atis),
    ]
    # Insert each alternate as its own section
    for _altn_tag, _lbl, _am, _at, _aa, _ai in _altn_wx_entries:
        SECTIONS.append((_altn_tag, _lbl, _am, _at, _aa))
    SECTIONS += [
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
        out = f"\n{'=' * BW}\n{line1}\n{indent}IA\n"
        if runways_str:
            rwy_parts = runways_str.split()
            lines_rwy = []
            for j in range(0, len(rwy_parts), 2):
                lines_rwy.append(" ".join(rwy_parts[j:j+2]))
            out += f"{indent}RWYS: {lines_rwy[0]}\n"
            for extra in lines_rwy[1:]:
                out += f"{indent}      {extra}\n"
        out += f"{'=' * BW}\n"
        return out

    def _cat_banner(label):
        inner = f" {label} "
        pad   = BW - len(inner)
        return f"{'=' * (pad//2)}{inner}{'=' * (pad - pad//2)}\n"

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
    route_firs = collections.OrderedDict()   # fir_code -> fir_name (populated from sigmets if available)
    for fix in root.findall("navlog/fix"):
        fcode = (fix.findtext("fir") or "").strip().upper()
        if fcode and fcode not in route_firs:
            route_firs[fcode] = ""   # name filled in below

    # 2. Index all active sigmets by FIR code, and collect FIR names
    sigmet_map = collections.OrderedDict()   # fir_code -> list of sigmet elements
    _all_sigs = root.findall("weather/sigmets/sigmet")
    LOG.debug(f"[DBG SIGMET COUNT: {len(_all_sigs)} sigmets found at weather/sigmets/sigmet")
    if not _all_sigs:
        # Try without weather/ prefix in case root is already at <weather>
        _all_sigs = root.findall("sigmets/sigmet")
        LOG.debug(f"[DBG SIGMET COUNT (no weather/ prefix): {len(_all_sigs)}")
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

# ===============================================================================
# NOTAM HELPERS — shared logic used by departure, arrival, alternate, enroute
# ===============================================================================


# ── Date helpers ──────────────────────────────────────────────────────────────
# ===========================================================================
# NOTAMs
# Date parsing, Q-code decoding, categorisation, and per-airport rendering.
# ===========================================================================


def _parse_iso_date(raw):
    """Parse ISO-8601 date string → UTC-aware datetime, or None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except Exception:
        return None


def _parse_dtg(raw):
    """Parse SimBrief enroute DTG (YYYYMMDDHHmm) → UTC-aware datetime, or None."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if len(s) >= 12 and s[:12].isdigit():
            _tz = timezone
            return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                            int(s[8:10]), int(s[10:12]), tzinfo=_tz.utc)
    except Exception:
        pass
    return None


def _fmt_ofp_date(dt):
    """Format datetime → '2026-Feb-19 06:59' (OFP style). None → 'UFN'."""
    if dt is None:
        return "UFN"
    # strftime %b gives 'Feb' on most platforms; ensure capitalised
    return f"{dt.year}-{dt.strftime('%b')}-{dt.strftime('%d')} {dt.strftime('%H:%M')}"


def _is_expired(eff_dt, exp_dt):
    """Return True if this NOTAM has definitively expired."""
    _tz = timezone
    if exp_dt and exp_dt < datetime.now(_tz.utc):
        return True
    return False


def _expire_str(date_exp_raw, is_estimated):
    """
    Build the expiry display string and return the exp_dt used for expiry checking.

      '2026-Feb-21 14:30'           — known hard expiry  -> exp_dt returned for checking
      'UFN'                          — no expiry          -> None returned (never expired)
      'UFN(EST 2026-Feb-25 18:00)'  — UFN with estimate  -> None returned (UFN = still active)

    UFN NOTAMs (is_estimated=True, or no date at all) are NEVER considered expired:
    the estimated date is display-only and must not trigger the expired flag.
    """
    exp_dt = _parse_iso_date(date_exp_raw)
    if is_estimated:
        # UFN with optional estimated date — display it but never mark as expired
        if exp_dt:
            return f"UFN(EST {_fmt_ofp_date(exp_dt)})", None
        return "UFN", None
    if exp_dt:
        # Hard expiry date — use for both display and expiry checking
        return _fmt_ofp_date(exp_dt), exp_dt
    return "UFN", None


# ── Category mapping ──────────────────────────────────────────────────────────

# ── Airport NOTAM Q-code subject → display category ──────────────────────────
# notam_qcode_subject values come directly from SimBrief (e.g. "Apron", "Runway")
# Exact match first, then substring fallback via _QCODE_CAT_MAP
_QCODE_SUBJECT_EXACT = {
    # Movement & landing area
    'Runway':                   'RUNWAY',
    'Taxiway':                  'TAXIWAY',
    'Apron':                    'APRON',
    'Movement Area':            'APRON',
    'Parking Area':             'APRON',
    'Bearing Strength':         'RUNWAY',
    'Declared Distances':       'RUNWAY',
    'Threshold':                'RUNWAY',
    'Stopway':                  'RUNWAY',
    'Clearway':                 'RUNWAY',
    'Rapid Exit Taxiway':       'TAXIWAY',
    # Approach & landing
    'Approach Lighting':        'APPROACH AND LANDING',
    'PAPI':                     'APPROACH AND LANDING',
    'VASIS':                    'APPROACH AND LANDING',
    'ILS':                      'APPROACH AND LANDING',
    'Localizer':                'APPROACH AND LANDING',
    'Glide Path':               'APPROACH AND LANDING',
    'Instrument Approach':      'APPROACH AND LANDING',
    'Approach Procedures':      'APPROACH AND LANDING',
    'Landing':                  'APPROACH AND LANDING',
    'MLS':                      'APPROACH AND LANDING',
    # Departure
    'SID':                      'DEPARTURE PROCEDURES',
    'Standard Instrument Departure': 'DEPARTURE PROCEDURES',
    'Departure Procedures':     'DEPARTURE PROCEDURES',
    # Navigation aids
    'VOR':                      'NAVIGATION AIDS',
    'DME':                      'NAVIGATION AIDS',
    'NDB':                      'NAVIGATION AIDS',
    'TACAN':                    'NAVIGATION AIDS',
    'VORTAC':                   'NAVIGATION AIDS',
    'Navigation Aid':           'NAVIGATION AIDS',
    'GNSS':                     'NAVIGATION AIDS',
    # Communication
    'Communication':            'COMMUNICATION',
    'Radio':                    'COMMUNICATION',
    'SELCAL':                   'COMMUNICATION',
    'Radar':                    'COMMUNICATION',
    # Lighting
    'Approach Lights':          'APPROACH AND LANDING',
    'Runway Lights':            'RUNWAY',
    'Taxiway Lights':           'TAXIWAY',
    'Lighting':                 'GENERAL',
    # Services
    'Services':                 'GENERAL',
    'Fuel':                     'GENERAL',
    'De-icing':                 'GENERAL',
    'Fire and Rescue':          'GENERAL',
    'Customs':                  'GENERAL',
    # Obstacles / warnings
    'Obstacle':                 'GENERAL',
    'Warning':                  'WARNING',
    'Other':                    'GENERAL',
    'Airport':                  'GENERAL',
}

# Substring fallback (casefolded key must appear in casefolded subject)
_QCODE_CAT_MAP = {
    'approach':       'APPROACH AND LANDING',
    'landing':        'APPROACH AND LANDING',
    'ils':            'APPROACH AND LANDING',
    'runway':         'RUNWAY',
    'apron':          'APRON',
    'taxiway':        'TAXIWAY',
    'navigation aid': 'NAVIGATION AIDS',
    'vor':            'NAVIGATION AIDS',
    'dme':            'NAVIGATION AIDS',
    'ndb':            'NAVIGATION AIDS',
    'communication':  'COMMUNICATION',
    'radio':          'COMMUNICATION',
    'sid':            'DEPARTURE PROCEDURES',
    'departure proc': 'DEPARTURE PROCEDURES',
    'obstacle':       'GENERAL',
    'warning':        'WARNING',
    'services':       'GENERAL',
    'other':          'GENERAL',
    'airport':        'GENERAL',
}

# Text keyword scan → category (tried when q-code mapping fails or gives GENERAL)
_KEYWORD_CAT = [
    (['SID ', 'DEPARTURE (RNAV)', 'ODP ', 'OBSTACLE DEPARTURE',
      'STANDARD INSTRUMENT DEPARTURE'],             'DEPARTURE PROCEDURES'),
    (['ILS ', 'LOC ', 'IAP ', 'APPROACH', 'PAPI',
      'ALS ', 'RVR ', ' LANDING'],                 'APPROACH AND LANDING'),
    [['RWY ', 'RUNWAY '],                           'RUNWAY'],
    [['TWY ', 'TAXI', 'TAXIWAY'],                   'TAXIWAY'],
    [['APRON', ' RAMP', 'STAND ', 'GATE '],         'APRON'],
    [['COM ', 'COMM ', 'RADIO', 'FREQ '],            'COMMUNICATION'],
    [['VORTAC', 'VOR ', 'DME ', 'NDB ', 'NAVAID',
      'TACAN', ' ILS '],                            'NAVIGATION AIDS'],
]


def _map_notam_category(qcode_category, qcode_subject, text, valid_cats):
    """
    Map a NOTAM to a display category from valid_cats.
    Priority:
      1. Exact match on notam_qcode_subject  (SimBrief pre-decoded value e.g. "Apron")
      2. Exact match on notam_qcode_category (e.g. "Airport")
      3. Substring scan via _QCODE_CAT_MAP
      4. Keyword scan of NOTAM body text
      5. GENERAL / OTHER fallback
    """
    # 1. Exact subject lookup — highest fidelity
    exact = _QCODE_SUBJECT_EXACT.get(qcode_subject)
    if exact and exact in valid_cats:
        return exact

    # 2. Exact category lookup
    exact_cat = _QCODE_SUBJECT_EXACT.get(qcode_category)
    if exact_cat and exact_cat in valid_cats:
        return exact_cat

    # 3. Substring fallback on both fields
    for raw in (qcode_subject, qcode_category):
        key = raw.casefold()
        for pat, cat in _QCODE_CAT_MAP.items():
            if pat in key and cat in valid_cats:
                return cat

    # 4. Keyword scan of body text
    text_upper = " " + text.upper() + " "
    for entry in _KEYWORD_CAT:
        keywords, cat = entry[0], entry[1]
        if cat in valid_cats and any(kw in text_upper for kw in keywords):
            return cat

    return 'GENERAL' if 'GENERAL' in valid_cats else 'OTHER'


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render_notam_entry(loc_icao, loc_iata, nid, eff_dt, exp_str, cre_dt, text):
    """
    Render one NOTAM matching OFP layout exactly:

    KPHX PHX   A0339/26   2026-Feb-17 06:59 - 2026-Feb-21 14:30
    CREATED:2026-Feb-15 10:12   FL: SFC - UNL

    PHX RWY 07L/25R CLSD
    """
    iata  = loc_iata if loc_iata else ""
    eff_s = _fmt_ofp_date(eff_dt) if eff_dt else "UFN"

    # Column-align: "KPHX PHX" left-padded to 9 chars, NID to 10, then dates
    id_col  = f"{loc_icao} {iata}"        # e.g. "KPHX PHX"
    hdr     = f"{id_col:<9} {nid:<12} {eff_s} - {exp_str}"

    cre_s   = f"CREATED:{_fmt_ofp_date(cre_dt)}   " if cre_dt else "   "
    line2   = f"{cre_s}FL: SFC - UNL"

    body_lines = []
    for para in text.splitlines():
        para = para.strip()
        if para:
            body_lines.append(textwrap.fill(para, width=72))
    body = "\n".join(body_lines)

    return f"{hdr}\n{line2}\n\n{body}\n"


def _cat_banner(label):
    """
    Dark-banner category label matching OFP style.
    Rendered as a solid border line with centred label, e.g.:

    =============================== GENERAL ================================
    """
    BW = 72
    inner = f" {label} "
    pad   = BW - len(inner)
    left  = pad // 2
    right = pad - left
    return f"\n{'=' * left}{inner}{'=' * right}\n"


# ── Collection ────────────────────────────────────────────────────────────────

def _get_airport_code_name(section):
    """Extract ICAO code and name from an XML section element."""
    code, name = "", ""
    for f in ('icao_code', 'icao', 'code', 'airport_code'):
        code = (section.findtext(f) or "").strip()
        if code:
            break
    for f in ('name', 'airport_name', 'location_name'):
        name = (section.findtext(f) or "").strip()
        if name:
            break
    return code, name


def _collect_airport_notams(notams_list, category_order):
    """
    Parse <notam> elements → {category: [(nid, rendered, is_expired), ...]}
    Expired NOTAMs are included and flagged; renderer puts them at the bottom.
    """
    categorized = {cat: [] for cat in category_order}

    for n in notams_list:
        qcode_cat  = (n.findtext("notam_qcode_category") or "").strip()
        qcode_subj = (n.findtext("notam_qcode_subject")  or "").strip()
        text       = (n.findtext("notam_text")           or "").strip()
        nid        = (n.findtext("notam_id")             or "---").strip()
        date_eff   = (n.findtext("date_effective")       or "").strip()
        date_exp   = (n.findtext("date_expire")          or "").strip()
        date_cre   = (n.findtext("date_created")
                      or n.findtext("date_modified")     or "").strip()
        loc_icao   = (n.findtext("location_icao")        or "").strip()
        loc_iata   = (n.findtext("location_id")          or "").strip()
        # date_expire_is_estimated is an empty tag — present means estimated
        is_est     = n.find("date_expire_is_estimated") is not None

        if not text:
            continue

        eff_dt         = _parse_iso_date(date_eff)
        cre_dt         = _parse_iso_date(date_cre)
        exp_s, exp_dt  = _expire_str(date_exp, is_est)
        expired        = _is_expired(eff_dt, exp_dt)

        cat = _map_notam_category(qcode_cat, qcode_subj, text, category_order)
        if cat not in category_order:
            cat = 'GENERAL' if 'GENERAL' in category_order else 'OTHER'

        rendered = _render_notam_entry(loc_icao, loc_iata, nid,
                                        eff_dt, exp_s, cre_dt, text)
        categorized[cat].append((nid, rendered, expired))

    return categorized


# ── Section builder ───────────────────────────────────────────────────────────

def _build_notam_section(category_order, categorized, page_break_after=False):
    """
    Render categorised NOTAMs matching OFP layout.
    Active/future first, expired at the bottom of each category section.
    Empty categories are omitted entirely.
    """
    result = ""

    for cat in category_order:
        items = categorized.get(cat, [])
        if not items:
            continue

        active  = [(nid, r) for nid, r, exp in items if not exp]
        expired = [(nid, r) for nid, r, exp in items if exp]

        if not active and not expired:
            continue

        result += _cat_banner(cat)

        for nid, rendered in sorted(active, key=lambda x: x[0], reverse=True):
            result += rendered + "\n"

        # Only emit the expired divider and expired entries when expired NOTAMs
        # actually exist in this category — never emit an empty expired section.
        if expired:
            result += f"\n{'--- EXPIRED ---':^72}\n\n"
            for nid, rendered in sorted(expired, key=lambda x: x[0], reverse=True):
                result += rendered + "\n"

    if page_break_after and result:
        result += "\n[PAGEBREAK]\n"

    return result


# ── Airport block header ──────────────────────────────────────────────────────

def _notam_flight_ident(xml_root):
    """
    Flight identity strip carried at the top of every NOTAM airport block:

        ENY3936 / KORF - KORD / 2024-09-25 / N772MR
    """
    if xml_root is None:
        return ""
    try:
        airline = (xml_root.findtext('general/icao_airline') or
                   xml_root.findtext('general/iata_airline') or '').strip().upper()
        fltnum  = (xml_root.findtext('general/flight_number') or '').strip()
        orig    = (xml_root.findtext('origin/icao_code') or '').strip().upper()
        dest    = (xml_root.findtext('destination/icao_code') or '').strip().upper()
        tail    = (xml_root.findtext('aircraft/reg') or
                   xml_root.findtext('general/reg') or '').strip().upper()
        try:
            _ts   = int(xml_root.findtext('times/sched_out') or '0')
            _date = datetime.fromtimestamp(_ts, tz=timezone.utc).strftime('%Y-%m-%d')
        except Exception:
            _date = ''
        parts = [f"{airline}{fltnum}".strip()]
        if orig and dest:
            parts.append(f"{orig} - {dest}")
        if _date:
            parts.append(_date)
        if tail:
            parts.append(tail)
        parts = [p for p in parts if p]
        return " / ".join(parts)
    except Exception:
        return ""


def _airport_notam_header(airport_code, airport_name, role, runways="", iata="",
                          xml_root=None):
    """
    Render the airport block header matching OFP badge style.
    iata is the real IATA code passed in from the XML section.
    """
    BW    = 72
    right = f"{iata} - {airport_name}" if iata and airport_name else airport_name
    left_part  = f"{airport_code}   {role}"
    line1 = f"{left_part:<42}{right}"
    indent = " " * (len(airport_code) + 3)

    ident = _notam_flight_ident(xml_root)

    out = f"\n{'=' * BW}\n"
    out += f"{line1}\n"
    out += f"{indent}IA\n"
    if runways:
        # Group runway pairs: max 2 pairs per line
        rwy_parts = runways.split()
        lines_rwy = []
        for j in range(0, len(rwy_parts), 2):
            lines_rwy.append(" ".join(rwy_parts[j:j+2]))
        out += f"{indent}RWYS: {lines_rwy[0]}\n"
        for extra in lines_rwy[1:]:
            out += f"{indent}      {extra}\n"
    if ident:
        # Consumed by the NOTAM renderer's airport-block parser and drawn as a
        # muted strip in the banner — never rendered as literal text.
        out += f"{indent}ID: {ident}\n"
    out += f"{'=' * BW}\n\n"
    return out


def _make_runway_pair(rwy_id):
    """
    Given a single runway end identifier (e.g. '07L'), return the full paired
    designator (e.g. '07L/25R'). Returns None if the input is not a valid runway.
    Always puts the lower-numbered end first.
    """
    m = re.match(r'^(\d{1,2})([LRC]?)$', rwy_id.strip().upper())
    if not m:
        return None
    num    = int(m.group(1))
    suffix = m.group(2)
    if not (1 <= num <= 36):
        return None
    recip_num    = num + 18 if num <= 18 else num - 18
    flip         = {'L': 'R', 'R': 'L', 'C': 'C', '': ''}
    recip_suffix = flip.get(suffix, '')
    end1 = f"{num:02d}{suffix}"
    end2 = f"{recip_num:02d}{recip_suffix}"
    return f"{end1}/{end2}" if num <= recip_num else f"{end2}/{end1}"


def _get_runways(xml_root, section):
    """
    Build the full airport runway list from TLR data (most reliable source).
    Each TLR <runway><identifier> gives one end; we compute the reciprocal pair
    and deduplicate so '07L' and '25R' both resolve to '07L/25R' (once).

    TLR has full runway data for both departure (tlr/takeoff) and destination
    (tlr/landing). The correct sub-section is chosen by matching the airport
    ICAO code in <conditions><airport_icao>.
    Alternates fall back to plan_rwy (no TLR data available for them).

    Returns a space-separated string like "07L/25R 07R/25L 08/26".
    """

    def sort_key(r):
        n = re.search(r'\d+', r)
        return int(n.group()) if n else 99

    def collect_runways(rwy_elements):
        """Deduplicate runway identifiers and return a sorted space-separated string."""
        seen_pairs = set()
        ordered    = []
        for rwy in rwy_elements:
            rwy_id = (rwy.findtext('identifier') or "").strip().upper()
            if not rwy_id:
                continue
            pair = _make_runway_pair(rwy_id)
            if pair and pair not in seen_pairs:
                seen_pairs.add(pair)
                ordered.append(pair)
        if ordered:
            return " ".join(sorted(ordered, key=sort_key))
        return ""

    # Get this section's airport ICAO
    airport_icao = (section.findtext("icao_code") or
                    section.findtext("icao")      or
                    section.findtext("icao_id")   or "").strip().upper()

    # Try tlr/takeoff — matches if conditions/airport_icao == this airport
    takeoff_node = xml_root.find('.//tlr/takeoff')
    if takeoff_node is not None:
        to_icao = (takeoff_node.findtext('conditions/airport_icao') or "").strip().upper()
        if to_icao and (to_icao == airport_icao or not airport_icao):
            result = collect_runways(takeoff_node.findall('runway'))
            if result:
                return result

    # Try tlr/landing — matches if conditions/airport_icao == this airport
    landing_node = xml_root.find('.//tlr/landing')
    if landing_node is not None:
        ld_icao = (landing_node.findtext('conditions/airport_icao') or "").strip().upper()
        if ld_icao and (ld_icao == airport_icao or not airport_icao):
            result = collect_runways(landing_node.findall('runway'))
            if result:
                return result

    # Alternate (no TLR data): fall back to plan_rwy
    plan = (section.findtext("plan_rwy") or "").strip().upper()
    if plan:
        pair = _make_runway_pair(plan)
        return pair if pair else plan
    return ""


# ── Departure NOTAMs ──────────────────────────────────────────────────────────

def get_departure_notams_sorted(xml_root, section_name):
    """Return formatted departure NOTAMs string for the given XML section."""
    section = xml_root.find(f".//{section_name}")
    if section is None:
        return ""

    notams_list = (section.find("notams") or section).findall(".//notam")
    if not notams_list:
        return ""

    airport_code, airport_name = _get_airport_code_name(section)
    airport_iata = (section.findtext("iata_code") or "").strip()
    if not airport_code and notams_list:
        n0 = notams_list[0]
        airport_code = (n0.findtext('location_icao') or n0.findtext('account_id') or "").strip()
        airport_name = (n0.findtext('location_name') or "").strip()

    # OFP departure category order: GENERAL first, then operational
    category_order = [
        'GENERAL', 'RUNWAY', 'TAXIWAY', 'APRON',
        'DEPARTURE PROCEDURES', 'COMMUNICATION', 'NAVIGATION AIDS',
        'APPROACH AND LANDING', 'SERVICES', 'WARNING', 'OTHER',
    ]
    categorized = _collect_airport_notams(notams_list, category_order)

    header = _airport_notam_header(airport_code, airport_name, "DEPARTURE",
                                    _get_runways(xml_root, section), iata=airport_iata,
                                    xml_root=xml_root)
    body = _build_notam_section(category_order, categorized, page_break_after=False)
    return header + body if body else ""


# ── Arrival NOTAMs ────────────────────────────────────────────────────────────

def get_arrival_notams_sorted(xml_root, section_name):
    """Return formatted arrival NOTAMs string for the given XML section."""
    section = xml_root.find(f".//{section_name}")
    if section is None:
        return ""

    notams_list = (section.find("notams") or section).findall(".//notam")
    if not notams_list:
        return ""

    airport_code, airport_name = _get_airport_code_name(section)
    airport_iata = (section.findtext("iata_code") or "").strip()
    if not airport_code and notams_list:
        n0 = notams_list[0]
        airport_code = (n0.findtext('location_icao') or n0.findtext('account_id') or "").strip()
        airport_name = (n0.findtext('location_name') or "").strip()

    category_order = [
        'GENERAL', 'APPROACH AND LANDING', 'RUNWAY', 'NAVIGATION AIDS',
        'TAXIWAY', 'APRON', 'COMMUNICATION', 'DEPARTURE PROCEDURES',
        'SERVICES', 'WARNING', 'OTHER',
    ]
    categorized = _collect_airport_notams(notams_list, category_order)

    header = _airport_notam_header(airport_code, airport_name, "DESTINATION",
                                    _get_runways(xml_root, section), iata=airport_iata,
                                    xml_root=xml_root)
    body = _build_notam_section(category_order, categorized, page_break_after=False)
    return header + body if body else ""


# ── Alternate NOTAMs ──────────────────────────────────────────────────────────

def get_alternate_notams_sorted(xml_root, section_name):
    """Return formatted alternate NOTAMs string for all alternate XML sections."""
    all_sections = xml_root.findall("alternate")
    if not all_sections:
        # Fallback: try the section_name directly
        sec = xml_root.find(f".//{section_name}")
        all_sections = [sec] if sec is not None else []

    category_order = [
        'GENERAL', 'APPROACH AND LANDING', 'RUNWAY', 'NAVIGATION AIDS',
        'TAXIWAY', 'APRON', 'COMMUNICATION', 'DEPARTURE PROCEDURES',
        'SERVICES', 'WARNING', 'OTHER',
    ]

    combined = ""
    for _idx, section in enumerate(all_sections):
        notams_list = (section.find("notams") or section).findall(".//notam")
        if not notams_list:
            continue

        airport_code, airport_name = _get_airport_code_name(section)
        airport_iata = (section.findtext("iata_code") or "").strip()
        if not airport_code and notams_list:
            n0 = notams_list[0]
            airport_code = (n0.findtext('location_icao') or n0.findtext('account_id') or "").strip()
            airport_name = (n0.findtext('location_name') or "").strip()

        _altn_label = "ALTN 1" if _idx == 0 else f"ALTN {_idx + 1}"
        categorized = _collect_airport_notams(notams_list, category_order)
        header = _airport_notam_header(airport_code, airport_name, _altn_label,
                                        _get_runways(xml_root, section), iata=airport_iata,
                                        xml_root=xml_root)
        body = _build_notam_section(category_order, categorized, page_break_after=True)
        if body:
            combined += header + body

    return combined


# ── Enroute NOTAMs ────────────────────────────────────────────────────────────


# ── FAA/ICAO Q-code subject decode (2nd+3rd letters) ─────────────────────────
# Source: FAA Order 7930.2 Appendix B — https://www.faa.gov/air_traffic/publications/atpubs/notam_html/appendix_b.html
_QCODE_SUBJECT_FULL = {
    # A — ATM Airspace Organisation
    "AA": "AIRSPACE",        "AC": "AIRSPACE",       "AD": "AIRSPACE",
    "AE": "AIRSPACE",        "AF": "AIRSPACE",       "AH": "AIRSPACE",
    "AL": "AIRSPACE",        "AN": "AIRSPACE",        "AO": "AIRSPACE",
    "AP": "AIRSPACE",        "AR": "AIRSPACE",        "AT": "AIRSPACE",
    "AU": "AIRSPACE",        "AV": "AIRSPACE",        "AX": "AIRSPACE",
    "AZ": "AIRSPACE",
    # C — CNS Communications & Surveillance
    "CA": "COMMUNICATION",   "CB": "COMMUNICATION",  "CC": "COMMUNICATION",
    "CD": "COMMUNICATION",   "CE": "COMMUNICATION",  "CG": "COMMUNICATION",
    "CL": "COMMUNICATION",   "CM": "COMMUNICATION",  "CP": "COMMUNICATION",
    "CR": "COMMUNICATION",   "CS": "COMMUNICATION",  "CT": "COMMUNICATION",
    # F — AGA Facilities & Services
    "FA": "SERVICES",        "FB": "SERVICES",        "FC": "SERVICES",
    "FD": "SERVICES",        "FE": "SERVICES",        "FF": "SERVICES",
    "FG": "SERVICES",        "FH": "SERVICES",        "FI": "SERVICES",
    "FJ": "SERVICES",        "FL": "SERVICES",        "FM": "SERVICES",
    "FO": "SERVICES",        "FP": "SERVICES",        "FS": "SERVICES",
    "FT": "SERVICES",        "FU": "SERVICES",        "FW": "SERVICES",
    "FZ": "SERVICES",
    # G — GNSS
    "GA": "NAVIGATION AIDS", "GW": "NAVIGATION AIDS",
    # I — ILS / MLS
    "IC": "APPROACH PROCEDURES",  "ID": "APPROACH PROCEDURES",
    "IG": "APPROACH PROCEDURES",  "II": "APPROACH PROCEDURES",
    "IL": "APPROACH PROCEDURES",  "IM": "APPROACH PROCEDURES",
    "IN": "APPROACH PROCEDURES",  "IO": "APPROACH PROCEDURES",
    "IS": "APPROACH PROCEDURES",  "IT": "APPROACH PROCEDURES",
    "IU": "APPROACH PROCEDURES",  "IW": "APPROACH PROCEDURES",
    "IX": "APPROACH PROCEDURES",  "IY": "APPROACH PROCEDURES",
    # L — AGA Lighting
    "LA": "LIGHTING",  "LB": "LIGHTING",  "LC": "LIGHTING",
    "LD": "LIGHTING",  "LE": "LIGHTING",  "LF": "LIGHTING",
    "LG": "LIGHTING",  "LH": "LIGHTING",  "LI": "LIGHTING",
    "LJ": "LIGHTING",  "LK": "LIGHTING",  "LL": "LIGHTING",
    "LM": "LIGHTING",  "LP": "LIGHTING",  "LR": "LIGHTING",
    "LS": "LIGHTING",  "LT": "LIGHTING",  "LU": "LIGHTING",
    "LV": "LIGHTING",  "LW": "LIGHTING",  "LX": "LIGHTING",
    "LY": "LIGHTING",  "LZ": "LIGHTING",
    # M — AGA Movement & Landing Area
    "MA": "MOVEMENT AREA",  "MB": "MOVEMENT AREA",  "MC": "MOVEMENT AREA",
    "MD": "MOVEMENT AREA",  "MG": "MOVEMENT AREA",  "MH": "MOVEMENT AREA",
    "MK": "MOVEMENT AREA",  "MM": "MOVEMENT AREA",  "MN": "MOVEMENT AREA",
    "MO": "MOVEMENT AREA",  "MP": "MOVEMENT AREA",  "MR": "RUNWAY",
    "MS": "MOVEMENT AREA",  "MT": "MOVEMENT AREA",  "MU": "MOVEMENT AREA",
    "MW": "MOVEMENT AREA",  "MX": "MOVEMENT AREA",  "MY": "MOVEMENT AREA",
    # N — Navigation Facilities
    "NA": "NAVIGATION AIDS",  "NB": "NAVIGATION AIDS",  "NC": "NAVIGATION AIDS",
    "ND": "NAVIGATION AIDS",  "NF": "NAVIGATION AIDS",  "NL": "NAVIGATION AIDS",
    "NM": "NAVIGATION AIDS",  "NN": "NAVIGATION AIDS",  "NO": "NAVIGATION AIDS",
    "NT": "NAVIGATION AIDS",  "NV": "NAVIGATION AIDS",
    # O — Other Information
    "OA": "SERVICES",   "OB": "OBSTACLE",  "OE": "SERVICES",
    "OL": "OBSTACLE",   "OR": "SERVICES",
    # P — ATM Air Traffic Procedures
    "PA": "PROCEDURES",  "PB": "PROCEDURES",  "PC": "PROCEDURES",
    "PD": "PROCEDURES",  "PE": "PROCEDURES",  "PF": "PROCEDURES",
    "PH": "PROCEDURES",  "PI": "APPROACH PROCEDURES",
    "PK": "PROCEDURES",  "PL": "PROCEDURES",  "PM": "PROCEDURES",
    "PN": "PROCEDURES",  "PO": "PROCEDURES",  "PR": "PROCEDURES",
    "PT": "PROCEDURES",  "PU": "APPROACH PROCEDURES",
    "PX": "PROCEDURES",  "PZ": "PROCEDURES",
    # R — Navigation Warnings: Airspace Restrictions
    "RA": "AIRSPACE RESTRICTIONS",  "RD": "AIRSPACE RESTRICTIONS",
    "RM": "AIRSPACE RESTRICTIONS",  "RO": "AIRSPACE RESTRICTIONS",
    "RP": "AIRSPACE RESTRICTIONS",  "RR": "AIRSPACE RESTRICTIONS",
    "RT": "AIRSPACE RESTRICTIONS",
    # S — ATM Air Traffic & VOLMET Services
    "SA": "SERVICES",  "SB": "SERVICES",  "SC": "SERVICES",
    "SE": "SERVICES",  "SF": "SERVICES",  "SL": "SERVICES",
    "SO": "SERVICES",  "SP": "SERVICES",  "SS": "SERVICES",
    "ST": "SERVICES",  "SU": "SERVICES",  "SV": "SERVICES",
    "SY": "SERVICES",
    # W — Navigation Warnings
    "WA": "WARNING",  "WB": "WARNING",  "WC": "WARNING",
    "WD": "WARNING",  "WE": "WARNING",  "WF": "WARNING",
    "WG": "WARNING",  "WH": "WARNING",  "WJ": "WARNING",
    "WL": "WARNING",  "WM": "WARNING",  "WP": "WARNING",
    "WR": "WARNING",  "WS": "WARNING",  "WT": "WARNING",
    "WU": "WARNING",  "WV": "WARNING",  "WW": "WARNING",
    "WY": "WARNING",  "WZ": "WARNING",
}

_ENRT_CATEGORY_ORDER = [
    "AIRSPACE RESTRICTIONS", "AIRSPACE", "PROCEDURES",
    "APPROACH PROCEDURES", "NAVIGATION AIDS", "COMMUNICATION",
    "LIGHTING", "MOVEMENT AREA", "RUNWAY", "OBSTACLE",
    "WARNING", "SERVICES", "GENERAL",
]

def _decode_qcode_enrt(raw_text, qcode_str=""):
    """
    Decode Q-code subject category.
    Tries qcode_str first (from XML notam_qcode field), then parses Q) line from raw text.
    Returns one of the categories in _ENRT_CATEGORY_ORDER.
    """
    # import re as _rq  # use module-level `re`
    def _subj_from_code(code):
        code = code.upper().strip()
        # Strip leading Q if present: QMNXX -> MN, or just take first 2 chars
        if code.startswith("Q") and len(code) >= 3:
            code = code[1:3]
        elif len(code) >= 2:
            code = code[:2]
        return _QCODE_SUBJECT_FULL.get(code, "GENERAL")

    # Try the pre-parsed qcode field first
    if qcode_str:
        cat = _subj_from_code(qcode_str)
        if cat != "GENERAL":
            return cat

    # Parse Q) field from raw NOTAM text
    # Formats: "Q) KZOB/QRTCA/..." or "Q) KZZZ/QWLWS/..."
    m = re.search(r"Q\)\s*\w*/Q([A-Z]{2})", raw_text)
    if m:
        return _QCODE_SUBJECT_FULL.get(m.group(1).upper(), "GENERAL")

    return "GENERAL"

def get_enroute_notams(xml_root):
    """
    Parse top-level <notams>/<notamdrec> enroute NOTAM block.
    Filters out any record whose icao_id matches the origin, destination,
    or alternate airport — those are already shown in the airport sections.
    Groups remaining NOTAMs by FIR, ordered by first appearance in navlog.
    """
    notams_root = xml_root.find("notams")
    if notams_root is None:
        return ""

    recs = notams_root.findall("notamdrec")
    if not recs:
        return ""

    # Build set of airport ICAOs to exclude — direct targeted lookups
    airport_icaos = set()
    for xpath in ("origin/icao_code", "destination/icao_code",
                  "alternate/icao_code", "altn1/icao_code"):
        v = (xml_root.findtext(xpath) or "").strip().upper()
        if v:
            airport_icaos.add(v)


    # Build navlog-ordered FIR list
    navlog_fir_order = []
    seen_firs = set()
    for fix in xml_root.findall("navlog/fix"):
        fcode = (fix.findtext("fir") or "").strip().upper()
        if fcode and fcode not in seen_firs:
            navlog_fir_order.append(fcode)
            seen_firs.add(fcode)

    by_facility = collections.OrderedDict()  # facility → {'icao_id':..., 'active': [...], 'expired': [...]}

    def _parse_notam_raw(raw, eff_dtg, exp_dtg, cre_dtg):
        """
        Parse a raw ICAO NOTAM text block. Returns (body, fl_str, eff_dt, exp_dt, cre_dt, is_est).
        Extracts fields directly from raw text when DTG attributes are missing/wrong.
        """
        # E) body — everything between E) and next field marker or end
        em = re.search(r'\nE\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, re.DOTALL)
        if not em:
            em = re.search(r'^E\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, re.DOTALL | re.MULTILINE)
        body = em.group(1).strip() if em else raw.strip()

        # F)/G) flight levels
        f_m = re.search(r'\nF\)\s*(\S+)', raw)
        g_m = re.search(r'\nG\)\s*(\S+)', raw)
        fl_str = f"{f_m.group(1).upper() if f_m else 'SFC'} - {g_m.group(1).upper() if g_m else 'UNL'}"

        # B) effective — prefer DTG attribute, fall back to raw field
        eff_dt = _parse_dtg(eff_dtg)
        if not eff_dt:
            bm = re.search(r'\nB\)\s*(\d{10})', raw)
            if bm:
                eff_dt = _parse_dtg(bm.group(1))

        # C) expiry + EST flag — parse from raw text for accuracy
        cm = re.search(r'\nC\)\s*(\d{10}|PERM)\s*(EST)?', raw, re.IGNORECASE)
        is_est = False
        exp_dt = None
        if cm:
            c_val = cm.group(1).upper()
            is_est = bool(cm.group(2))
            if c_val == "PERM":
                exp_dt = None     # permanent — never expires
            else:
                exp_dt = _parse_dtg(c_val) if not is_est else None  # EST = UFN, don't use for expiry
        else:
            # Fall back to DTG attribute
            exp_dt = _parse_dtg(exp_dtg)

        # CREATED — prefer DTG attribute
        cre_dt = _parse_dtg(cre_dtg)

        return body, fl_str, eff_dt, exp_dt, cre_dt, is_est

    for rec in recs:
        raw_text = (rec.findtext("notam_text") or "").strip()
        if not raw_text:
            continue

        nid      = (rec.findtext("notam_id")            or "---").strip()
        icao_id  = (rec.findtext("icao_id")             or "").strip().upper()

        # Skip if this NOTAM belongs to an airport already shown in airport sections
        if icao_id and icao_id in airport_icaos:
            continue

        facility = (rec.findtext("icao_name")           or
                    rec.findtext("cns_location_id")     or "ENROUTE").strip()
        eff_raw  = (rec.findtext("notam_effective_dtg") or "").strip()
        exp_raw  = (rec.findtext("notam_expire_dtg")    or "").strip()
        cre_raw  = (rec.findtext("notam_created_dtg")   or "").strip()

        body_text, fl_str, eff_dt, exp_dt, cre_dt, is_est = _parse_notam_raw(
            raw_text, eff_raw, exp_raw, cre_raw
        )
        is_expired = _is_expired(eff_dt, exp_dt)

        # Build expiry display string — match airport NOTAM style
        eff_str = _fmt_ofp_date(eff_dt) if eff_dt else "UFN"
        if is_est and exp_dt is None:
            # C) had EST — find the raw date for display only
            cm2 = re.search(r'\nC\)\s*(\d{10})', raw_text)
            est_dt = _parse_dtg(cm2.group(1)) if cm2 else None
            exp_str = f"UFN(EST {_fmt_ofp_date(est_dt)})" if est_dt else "UFN"
        else:
            exp_str = _fmt_ofp_date(exp_dt) if exp_dt else "UFN"

        # Header: just ICAO + NOTAM ID + dates (no duplicate IATA code)
        header_line  = f"{icao_id}   {nid}   {eff_str} - {exp_str}"
        created_line = f"CREATED:{_fmt_ofp_date(cre_dt)}   FL: {fl_str}" if cre_dt else f"FL: {fl_str}"

        body_lines = []
        for para in body_text.splitlines():
            para = para.strip()
            if para:
                body_lines.append(textwrap.fill(para, width=72))
        body = "\n".join(body_lines)

        rendered = f"{header_line}\n{created_line}\n\n{body}\n"

        # Decode Q-code category — use XML notam_qcode field if present, else parse raw text
        qcode_str = (rec.findtext("notam_qcode") or rec.findtext("notam_qcode_subject") or "").strip()
        qcat = _decode_qcode_enrt(raw_text, qcode_str)

        # Key on FIR code from the record's icao_id (which is the FIR code for enroute NOTAMs)
        fir_key = icao_id or facility
        if fir_key not in by_facility:
            by_facility[fir_key] = {
                'icao_id': icao_id, 'name': facility,
                'cats': {c: [] for c in _ENRT_CATEGORY_ORDER},
                'cats_exp': {c: [] for c in _ENRT_CATEGORY_ORDER},
            }
        if is_expired:
            by_facility[fir_key]['cats_exp'].setdefault(qcat, []).append((nid, rendered))
        else:
            by_facility[fir_key]['cats'].setdefault(qcat, []).append((nid, rendered))

    if not by_facility:
        return ""

    BW = 72
    result = ""

    # Render in navlog FIR order, then any remainder not on navlog
    ordered_keys = [k for k in navlog_fir_order if k in by_facility]
    remainder    = [k for k in by_facility if k not in seen_firs]
    for fir_key in ordered_keys + remainder:
        buckets  = by_facility[fir_key]
        fir_code = buckets.get('icao_id') or fir_key
        facility = buckets.get('name', fir_key)
        cats     = buckets['cats']
        cats_exp = buckets['cats_exp']

        # Skip FIR if truly empty
        has_any = any(v for v in cats.values()) or any(v for v in cats_exp.values())
        if not has_any:
            continue

        # FIR banner
        result += f"\n{'=' * BW}\n"
        result += f"{fir_code}   {facility}\n"
        result += f"{'=' * BW}\n\n"

        # Render active NOTAMs by category
        for cat in _ENRT_CATEGORY_ORDER:
            entries = sorted(cats.get(cat, []), key=lambda x: x[0], reverse=True)
            if not entries:
                continue
            inner = f" {cat} "
            pad   = BW - len(inner)
            result += f"{'=' * (pad//2)}{inner}{'=' * (pad - pad//2)}\n"
            for nid, rendered in entries:
                result += rendered + "\n"

        # Expired section
        exp_entries = []
        for cat in _ENRT_CATEGORY_ORDER:
            for item in sorted(cats_exp.get(cat, []), key=lambda x: x[0], reverse=True):
                exp_entries.append(item)
        if exp_entries:
            result += f"{'--- EXPIRED ---':^{BW}}\n"
            for nid, rendered in exp_entries:
                result += rendered + "\n"

    return result


# ---------------------------------------------------------------------------
# Takeoff performance — wind adjustment coefficients
# These values calibrate the per-knot headwind credit and tailwind penalty
# applied to the MTOW table.  See `write_takeoff_performance_string` for the
# full calculation.
# ---------------------------------------------------------------------------
_HDWND_BASE_RATE   = 0.00287   # lbs-per-lbs-MTOW per knot of headwind
_TLWND_BASE_RATE   = 0.00671   # lbs-per-lbs-MTOW per knot of tailwind
_AI_SUB_RATE       = 0.0015    # anti-ice climb penalty (fraction of TOW per 1000 lbs)

# ===========================================================================
# Entry points
# Top-level orchestration: fetch -> parse -> render -> save.
# ===========================================================================


def generate_enhanced_howgozit(user_id, output_path=None, gate="", arr_gate="", generation=0):
    """Fetch SimBrief XML, parse it, generate enhanced HOWGOZIT text with OFP, and save as PDF.

    gate/arr_gate override the DECS pages' (NSC/FI/fuel-service) synthesized
    placeholder gates — a real SimBrief OFP never carries gate data of its
    own, so this is the only way a real, dispatcher-assigned gate reaches
    the release rather than a random placeholder."""
    try:
        # --- Fetch and parse XML ---
        xml_data = fetch_simbrief_data(user_id)
        xml_root = parse_xml_string(xml_data) if isinstance(xml_data, str) else xml_data
        if xml_root is None:
            LOG.error("Failed to parse XML data")
            return None

        # --- Scheduled departure time ---
        sched_out = get_text("times/sched_out", xml_root, "")
        if sched_out:
            try:
                timestamp = int(sched_out)
                sched_out_fmt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%H%M")
            except (ValueError, OSError):
                sched_out_fmt = format_time_elapsed(sched_out)
        else:
            sched_out_fmt = "0000"

        takeoff_time = prompt_for_takeoff_time_str(sched_out_fmt)

        # --- Generate HOWGOZIT data ---
        LOG.debug("About to call parse_simbrief_data_to_howgozit_with_ofp")
        result = parse_simbrief_data_to_howgozit_with_ofp(xml_data, takeoff_time, gate=gate, arr_gate=arr_gate, generation=generation)

        # Add detailed debugging
        LOG.debug(f"[DBG: Result type: {type(result)}")
        LOG.debug(f"[DBG: Result is None: {result is None}")
        if result is not None:
            try:
                LOG.debug(f"[DBG: Result length: {len(result)}")
                if hasattr(result, '__iter__') and not isinstance(result, str):
                    LOG.debug(f"[DBG: Result items: {[type(item) for item in result]}")
                else:
                    LOG.debug(f"[DBG: Result content preview: {str(result)[:200]}")
            except Exception as e:
                LOG.debug(f"[DBG: Error examining result: {e}")

        # More specific validation
        if result is None:
            LOG.error("ERROR: parse_simbrief_data_to_howgozit_with_ofp returned None")
            return None

        if not isinstance(result, (tuple, list)):
            LOG.error(f"ERROR: Expected tuple/list, got {type(result)}")
            return None

        if len(result) < 5:
            LOG.error(f"ERROR: Expected 5 items, got {len(result)}")
            return None

        # --- Unpack the result properly ---
        try:
            raw_howgozit, flight_info, valid_runways, anti_ice_on, runway_lines = result
            LOG.debug("Successfully unpacked result")
            LOG.debug(f"[DBG: raw_howgozit type: {type(raw_howgozit)}")
            LOG.debug(f"[DBG: flight_info type: {type(flight_info)}")
            LOG.debug(f"[DBG: valid_runways length: {len(valid_runways) if hasattr(valid_runways, '__len__') else 'N/A'}")
        except ValueError as e:
            LOG.error(f"ERROR: Failed to unpack result: {e}")
            return None

        # --- Normalize HOWGOZIT string ---
        if isinstance(raw_howgozit, list):
            howgozit_text = "\n".join(str(line) for line in raw_howgozit)
        elif isinstance(raw_howgozit, str):
            howgozit_text = raw_howgozit.replace('\r\n', '\n').replace('\r', '\n')
        else:
            howgozit_text = str(raw_howgozit)

        LOG.debug(f"[DBG: Final howgozit_text length: {len(howgozit_text)}")
        LOG.debug(f"[DBG: First 200 chars: {repr(howgozit_text[:200])}")

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
            _date_str  = datetime.fromtimestamp(_sched_ts, tz=timezone.utc).strftime("%d%b").upper()  # e.g. 19FEB
        except Exception:
            _date_str = "NODATE"

        _base_name = f"{origin_clean}{dest_clean}{flt_clean}{_date_str}"

        # ── Generate deterministic release watermark code ──────────────────────
        # Format: AAAABBBB-9000-FFFF-MMDD-YYYYCCCCCCCC
        # AAAA = airline ICAO hex (4 chars), BBBB = padded flight num,
        # FFFF = 4-digit flight number, MMDD = month+day, YYYY = year,
        # CCCCCCCC = 00 + destination IATA hex (6 hex chars)
        def _make_release_code(origin_i, dest_i, flt_i, ts):
            try:
                _airline  = (get_text("general/icao_airline", xml_root) or "").upper()[:4].ljust(4)
                _flt_n    = ''.join(c for c in flt_i if c.isdigit())[:4].zfill(4)
                _origin_i = (origin_i or "XXX")[:4].upper()
                # Airline first 2 chars → hex (e.g. EN → 454E)
                _al_hex   = ''.join(f"{ord(c):02X}" for c in _airline[:2])
                # Flight number as 4-digit hex (e.g. 3936 → 0F60)
                _flt_hex  = f"{int(_flt_n or 0):04X}"
                # Date from timestamp
                _dt       = datetime.fromtimestamp(int(ts)) if ts else datetime.now(timezone.utc)
                _mmdd     = _dt.strftime("%m%d")
                _yyyy     = _dt.strftime("%Y")
                # Origin first 3 chars → hex (e.g. KOR → 4B4F52)
                _orig_hex = ''.join(f"{ord(c):02X}" for c in _origin_i[:3])
                return f"{_al_hex}{_flt_hex}-9000-{_flt_n}-{_mmdd}-{_yyyy}00{_orig_hex}"
            except Exception:
                return "00000000-9000-0000-0000-000000000000"

        _release_ts  = get_text("times/sched_out", xml_root) or "0"
        _release_code = _make_release_code(origin_clean, dest_clean, flt_clean, _release_ts)
        # Inject release code as grey footer watermark on all pages
        howgozit_text = f"[RELEASE_CODE:{_release_code}]\n" + howgozit_text

        if not output_path:
            folder = get_last_output_folder()
            if not folder:
                folder = prompt_for_output_folder()
                if not folder:
                    LOG.warning("No output folder selected. Aborting.")
                    return None
                save_last_output_folder(folder)
        else:
            folder = os.path.dirname(output_path)

        rls_path = os.path.join(folder, f"{_base_name}-RLS.pdf")
        wb_path  = os.path.join(folder, f"{_base_name}-WB.pdf")
        tlr_path = os.path.join(folder, f"{_base_name}-TLR.pdf")

        # --- Extract TPS+WB text for the separate WB file ---
        # Slice from just after [TPS_START] — skipping the [PAGEBREAK] to avoid blank first page.
        # Prepend the WBD identification header.
        _wb_text = ""
        try:
            _marker     = '[TPS_START]\n'
            _marker_pos = howgozit_text.find(_marker)
            if _marker_pos != -1:
                _wb_content = howgozit_text[_marker_pos + len(_marker):]
            else:
                LOG.debug("[WB-SPLIT] [TPS_START] not found — using text from first PAGEBREAK")
                _pb = howgozit_text.rfind('[PAGEBREAK]')
                _wb_content = howgozit_text[_pb + len('[PAGEBREAK]'):] if _pb != -1 else howgozit_text

            # WBD header — identical banner and time basis as the WBD page
            # carried at the front of the release (departure time, not the
            # time the PDF happened to be generated).
            _fos_ctx_wb = _fos_context(xml_root)
            _wbd_cmd    = (f"WBD*{_fos_ctx_wb['flt_disp']}/{_fos_ctx_wb['ddmmm']}"
                           f"/{_fos_ctx_wb['dep_hhmm']} {_fos_ctx_wb['orig']}")
            _sep_line   = "[HRULE]\n"
            _wbd_header = _fos_banner(_wbd_cmd) + "\n"
            _wb_text    = _sep_line + _wbd_header + _wb_content

        except Exception as _ex:
            LOG.debug(f"[DBG WB-SPLIT] Error: {_ex} — using full text")
            _wb_text = howgozit_text

        _report_type = get_report_type()   # "TLR" or "TPS"

        # --- Strip legacy TPS block and split off the Jeppesen TLR block ---
        # write_tlr_section() always returns tps_out + out, i.e. the legacy
        # [TPS_START] summary block immediately followed by the real Jeppesen
        # [TLR_START] block. In TLR mode the legacy tps_out span is dropped
        # entirely (it's WB-mode-only content) and the [TLR_START] block is
        # peeled off into its own file rather than staying concatenated onto
        # the release page.
        _tps_full_marker = "[PAGEBREAK]\n[TPS_START]\n"
        _tlr_full_marker = "[PAGEBREAK]\n[TLR_START]\n"
        _tps_pos = howgozit_text.find(_tps_full_marker)
        _tlr_pos = howgozit_text.find(_tlr_full_marker)

        _tlr_text = ""
        if _report_type == "TLR" and _tps_pos != -1 and _tlr_pos != -1:
            # Release page = everything before the legacy TPS block.
            _rls_text = howgozit_text[:_tps_pos]
            # TLR file = everything from [TLR_START] onward, minus the
            # leading [PAGEBREAK] (it would otherwise draw a blank first page).
            _tlr_text = howgozit_text[_tlr_pos + len(_tlr_full_marker):]
        elif _report_type == "TLR" and _tps_pos != -1 and _tlr_pos == -1:
            # No TLR block present (e.g. SimBrief XML had no <tlr> data) —
            # fall back to just dropping the TPS block; no TLR file to split off.
            _rls_text = howgozit_text[:_tps_pos]
        else:
            _rls_text = howgozit_text

        # --- WBD identification page goes at the very front of the release ---
        try:
            _wbd_page = build_wbd_page(xml_root, gate=gate)
            _wbd_page = _wbd_page.lstrip("\n").replace("[PAGEBREAK]\n", "", 1)
            # The [RELEASE_CODE:...] marker must stay on line 1 for the
            # footer watermark, so the WBD page is inserted after it.
            if _rls_text.startswith("[RELEASE_CODE:"):
                _code_line, _sep_nl, _rest = _rls_text.partition("\n")
                _rls_text = _code_line + _sep_nl + _wbd_page + "[PAGEBREAK]\n" + _rest
            else:
                _rls_text = _wbd_page + "[PAGEBREAK]\n" + _rls_text
        except Exception as _wbd_e:
            LOG.warning(f"WBD page not prepended to release: {_wbd_e}")

        # --- Index page: render once to learn where each FOS page lands, then
        # --- prepend the index and render for real. The index is exactly one
        # --- page, so every recorded page number shifts by one.
        try:
            import tempfile as _tempfile
            with _tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as _probe:
                save_as_pdf(_probe.name, _rls_text)
            _index_text = build_index_page(list(LAST_INDEX_ENTRIES))
            if _index_text:
                if _rls_text.startswith("[RELEASE_CODE:"):
                    _cl, _nl, _rest2 = _rls_text.partition("\n")
                    _rls_text = _cl + _nl + _index_text + "[PAGEBREAK]\n" + _rest2
                else:
                    _rls_text = _index_text + "[PAGEBREAK]\n" + _rls_text
        except Exception as _idx_e:
            LOG.warning(f"Index page not generated: {_idx_e}")

        LOG.debug(f"[DBG: report_type={_report_type}")
        LOG.debug(f"[DBG: Saving RLS to: {rls_path}")

        # --- Save PDFs ---
        save_as_pdf(rls_path, _rls_text)
        LOG.info(f"RLS saved: {rls_path}")

        _paths_to_open = [rls_path]

        if _report_type == "TPS":
            save_as_pdf(wb_path, _wb_text)
            LOG.info(f"WB  saved: {wb_path}")
            _paths_to_open.append(wb_path)
        elif _report_type == "TLR" and _tlr_text:
            save_as_pdf(tlr_path, _tlr_text)
            LOG.info(f"TLR saved: {tlr_path}")
            _paths_to_open.append(tlr_path)
        else:
            LOG.debug("[DBG: TLR mode — no TLR content to split into a separate file")

        # --- Auto-open output files ---
        for _path in _paths_to_open:
            if os.path.exists(_path):
                try:
                    if os.name == 'nt':
                        os.startfile(_path)
                    else:
                        subprocess.run(['open', _path], check=False)
                except Exception as _e:
                    LOG.warning(f"Could not open {_path}: {_e}")

        # --- Return full parsed content ---
        return howgozit_text, flight_info, valid_runways, anti_ice_on, runway_lines

    except Exception as e:
        LOG.error(f"Error in generate_enhanced_howgozit: {e}")
        traceback.print_exc()
        return None


def main():
    """CLI entry point: parse args, fetch SimBrief data, and generate the OFP PDFs."""
    # Username: use command-line arg if provided, otherwise load/prompt and save
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        user_id = sys.argv[1]
    else:
        user_id = get_or_prompt_username()

    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    debug_mode = "--debug" in sys.argv

    LOG.info(f"Using SimBrief username: {user_id}")
    LOG.debug(f"[DBG: debug_mode = {debug_mode}")

    try:
        if debug_mode:
            xml_data = fetch_simbrief_data(user_id)
            xml_root = parse_xml_string(xml_data) if isinstance(xml_data, str) else xml_data
            if xml_root is not None:
                debug_xml_structure(xml_root)
            else:
                LOG.error("Failed to parse XML")
        else:
            # Generate enhanced HOWGOZIT and save PDF
            generate_enhanced_howgozit(user_id, output_path)

    except Exception as e:
        LOG.error(f"Main execution error: {e}")
        traceback.print_exc()

__version__ = "2.9.0"

if __name__ == "__main__":
    main()
