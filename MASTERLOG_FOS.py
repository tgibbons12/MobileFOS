#!/usr/bin/env python3
import io
from PIL import Image
from reportlab.lib.utils import ImageReader
import sys
import os
import math
import json
import re
import requests
import traceback
import base64
import collections
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import simpledialog, messagebox
import textwrap
import logging
from write_tps_section import write_takeoff_performance_string
from fos_pages import (build_context as _fos_context,
                       build_fi_page, build_fil_page,
                       build_fuel_service_page, build_nsc_page,
                       build_wbd_page)
# The SLS field reports live in MASTERLOG; import rather than fork them so the
# two releases cannot drift. The local write_field_reports below is the older
# layout and is left in place for anything still calling it directly.
try:
    from MASTERLOG import write_field_reports as _write_field_reports_sls
except Exception as _wfr_e:  # pragma: no cover - fall back to the local one
    _write_field_reports_sls = None
    logging.getLogger(__name__).warning(
        f"SLS field reports unavailable, using the local layout: {_wfr_e}")

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
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)
LOG = logging.getLogger(__name__)

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
import subprocess
from SPEEDOTHER import get_speed_other

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
    # Show all intersections; flag those below reject distance but don't filter them out
    LOG.info(f"[INTXN] {icao} {rwy_id}: {len(entries)} entries, reject={distance_reject_ft:.0f}ft: "
             f"{[(e['taxiway'], int(e['tora_ft'])) for e in sorted(entries, key=lambda x: x['tora_ft'], reverse=True)]}")
    valid = entries   # all intersections shown regardless of reject distance

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

CONFIG_FILE = "config.json"
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

def ask_font_selection():
    """
    Font selection, taken from the config rather than prompted for:
    1=Courier Normal, 2=Menlo, 3=Courier New, 4=Rainbow Comic Sans.

    The picker dialog was removed — set "font" in the config file to change it.
    Defaults to "1" (Courier Normal). Cached for the session.
    """
    global _cached_font_choice
    if _cached_font_choice is not None:
        return _cached_font_choice
    _cached_font_choice = str(_load_config().get("font", "1")).strip() or "1"
    return _cached_font_choice

import os
import io
import requests
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from PIL import Image



def _draw_notam_section(c, notam_text, font_name, font_size=7):
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
    TOP_Y   = LH - MARGIN
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
    C_RWYS_BG      = colors.HexColor("#38474F")   # RWYS sub-bar — same colour as airport header
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
    BH_AIRPORT  = 32    # airport/FIR main bar
    BH_RWYS     = 16    # RWYS sub-bar — same colour as airport, more height
    BH_CATEGORY = 16    # category banner
    PAD_X       = 8     # left text padding

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
            c.showPage()
            c.setPageSize(landscape(letter))
            c.setFont(MONO, FS)
            y = TOP_Y

    def ensure(pts):
        if y - pts < BOT_Y:
            advance_column()

    # ── Drawing helpers ────────────────────────────────────────────────────────

    def draw_airport_banner(icao, role, iata_name, rwy_lines):
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
        c.drawCentredString(0, -3, badge_label)
        c.restoreState()

        text_x  = cx() + APT_W + PAD_X
        icao_y  = y - TOP_PAD - ICAO_FS    # top-aligned
        c.setFillColor(C_WHITE)
        c.setFont(HDR_FONT, ICAO_FS)
        c.drawString(text_x, icao_y, icao)
        after_icao = text_x + c.stringWidth(icao, HDR_FONT, ICAO_FS) + PAD_X + 2

        C_MUTED = colors.HexColor("#8BAABB")
        line_y  = y - TOP_PAD - 7

        if not role:
            # FIR block: show facility name to the right of the FIR code, all white
            if iata_name:
                c.setFont(HDR_FONT_BOLD, 7.5)
                c.setFillColor(C_WHITE)
                c.drawString(after_icao, line_y, iata_name)
        else:
            # Role may be "DEPARTURE  19 17:20 - 19 19:53" — split on double-space
            import re as _re_role
            role_parts = _re_role.split(r'\s{2,}', role, maxsplit=1)
            role_label = role_parts[0].strip()
            role_time  = role_parts[1].strip() if len(role_parts) > 1 else ""

            c.setFont(HDR_FONT_BOLD, 7.5)
            right_x = cx() + COL_W - PAD_X

            if role_time:
                # Draw time right-aligned: date digits muted, HH:MM white
                time_tokens = _re_role.split(r'(\d{2}:\d{2})', role_time)
                full_w = c.stringWidth(role_time, HDR_FONT_BOLD, 7.5)
                tx = right_x - full_w
                for tp in time_tokens:
                    if _re_role.match(r'^\d{2}:\d{2}$', tp):
                        c.setFillColor(C_WHITE)
                    else:
                        c.setFillColor(C_MUTED)
                    c.drawString(tx, line_y, tp)
                    tx += c.stringWidth(tp, HDR_FONT_BOLD, 7.5)
                right_x = right_x - full_w - 6

            role_w = c.stringWidth(role_label, HDR_FONT_BOLD, 7.5)
            c.setFillColor(C_WHITE)
            c.drawString(right_x - role_w, line_y, role_label)

            # Name line: "IATA - " white + city name muted
            if iata_name:
                dash_idx = iata_name.find(" - ")
                if dash_idx >= 0:
                    prefix = iata_name[:dash_idx + 3]
                    suffix = iata_name[dash_idx + 3:]
                else:
                    prefix = iata_name
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
            c.drawString(after_icao, line_y, rl)
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
                            label)
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
            c.drawString(cx() + PAD_X, text_y, ln[:max_chars + 10])
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
                c.drawString(cx() + PAD_X, y - LH_TEXT + 0.5, wl)
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
                c.drawString(msg_x, msg_y, ml)
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
            c.drawString(msg_x, msg_y, line)
            msg_y -= msg_lh

        y -= NIL_H + 4

    def draw_expired_divider():
        nonlocal y
        ensure(BH_CATEGORY + 2)
        c.setFillColor(colors.HexColor("#C8392B"))
        c.rect(cx(), y - BH_CATEGORY, COL_W, BH_CATEGORY, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont(BOLD, FS_LABEL)
        c.drawCentredString(cx() + COL_W / 2,
                            y - BH_CATEGORY + (BH_CATEGORY - FS_LABEL) / 2 + 1,
                            "▲  EXPIRED  ▲")
        y -= BH_CATEGORY

    # ── Parse tokens ──────────────────────────────────────────────────────────
    CATEGORY_RE  = _re.compile(r'^═+\s+(.+?)\s+═+$')
    AIRBLK_RE    = _re.compile(r'^═{10,}$')
    # Matches both airport NOTAMs  "KLAS LAS  A0209/26  2026-..."
    # and enroute NOTAMs           "ZLA   ZLA2025/0012   2025-..."
    NOTAM_HDR_RE = _re.compile(r'^[A-Z]{2,5}(?:\s+[A-Z]{2,5})?\s+\S+/\d{2,4}\s+\d{4}-')

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
            while j < len(raw_lines) and not AIRBLK_RE.match(raw_lines[j].strip()):
                s = raw_lines[j].strip()
                if not header_line and s and not s.startswith("IA"):
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
            rm = _re.split(r'\s{4,}', rest, maxsplit=1)
            if len(rm) == 1:
                # No 4-space gap → FIR block: whole rest is the facility name
                role      = ""
                iata_name = rest.strip()
            else:
                role      = rm[0].strip()   # role+time → rendered RIGHT
                iata_name = rm[1].strip()   # IATA - Name → rendered LEFT
            tokens.append(('airport', icao, role, iata_name, rwy_lines))
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
            _, icao, role, iata_name, rwy_lines = tok
            in_expired        = False
            notam_row_counter = 0
            draw_airport_banner(icao, role, iata_name, rwy_lines)

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
    c.showPage()


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
                _draw_notam_section(c, notam_text, _notam_font, font_size=8)
                # _draw_notam_section ends with its own showPage()
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
        howgozit = ""    
        # Column geometry from the real FOS release:
        #   "- IFR " 0-5, flight 6, tail 16, orig 27, dest 31, ALTN 35
        #   MIN T/O value at 15 (unpadded), RLS FUEL value at 30 (6 digits)
        try:
            _dep_day_f = datetime.fromtimestamp(
                int(get_text("times/sched_out") or "0"), tz=timezone.utc).strftime("%d")
        except Exception:
            _dep_day_f = ""
        _flt_f = f"{icao_airline}{flight_number}" + (f"/{_dep_day_f}" if _dep_day_f else "")
        howgozit += ("- IFR " + f"{_flt_f:<10}" + f"{fin + '/' + aircraft_reg:<11}"
                     + f"{origin_iata:<4}" + f"{destination_iata:<4}"
                     + f"ALTN {altn_iata}\n")
        howgozit += (f"  MIN T/O FUEL {int(float(min_takeoff or 0)):<6}"
                     f"RLS FUEL {int(float(plan_ramp or 0)):06d}\n")
        # ARR fuel time = reserve_time + alternate_burn_time + extra_time
        _paf_lbs_hdr  = int(get_text('fuel/plan_landing') or 0)
        _paf_flow     = int(get_text('fuel/avg_fuel_flow') or 1) or 1
        _paf_res_s    = int(get_text('times/reserve_time') or 0)
        _paf_altn_s   = int(int(get_text('fuel/alternate_burn') or 0) / _paf_flow * 3600)
        _paf_extra_s  = int(get_text('times/extrafuel_time') or 0)
        _paf_secs     = _paf_res_s + _paf_altn_s + _paf_extra_s
        _paf_hrs_hdr  = _paf_secs // 3600
        _paf_min_hdr  = (_paf_secs % 3600) // 60
        howgozit += (f"  TOT BRN {int(float(enroute_burn or 0)):<6}"
                     f"PLAN ARR FUEL {_paf_lbs_hdr:<5}"
                     f"{_paf_hrs_hdr:02d}HR/{_paf_min_hdr:02d}MIN\n\n")
        altn_route = get_text('alternate/route')

        # RIGHT START FLIGHT banner — 50% chance if local arrival is before 10:00
        import random as _random
        try:
            _dest_tz_early = int(get_text('times/dest_timezone') or 0)
            _sched_in_sec_early = int(get_text('times/sched_in', '0')) % 86400
            _si_hh = _sched_in_sec_early // 3600
            _si_mm = (_sched_in_sec_early % 3600) // 60
            _arr_local_early = f"{(_si_hh + _dest_tz_early) % 24:02d}{_si_mm:02d}"
            _arr_hour = int(_arr_local_early[:2])
            _show_rsf = (_arr_hour < 10) and (_random.random() < 0.5)
        except Exception:
            _show_rsf = False
        if _show_rsf:
            _rsf_label = " RIGHT START FLIGHT "
            _rsf_total = 52
            _rsf_pad = _rsf_total - len(_rsf_label)
            _rsf_left = _rsf_pad // 2
            _rsf_right = _rsf_pad - _rsf_left
            howgozit += f"{'*' * _rsf_left}{_rsf_label}{'*' * _rsf_right}\n\n"
        # ALTN RTE above FPL line
        if altn_route and altn_route.strip() and altn_route != "0":
            howgozit += f"ALTN RTE - {altn_route} {altn}\n\n"
        howgozit += f"FPL - PLAN 1 OF 1 - RTE 1 - CTLD CALC/RTE - RVSN 0{get_text('general/release')} - NON FAA PREF\n\n"
        howgozit += f"       *** 1 ENGINE INOPERATIVE ENROUTE ALTERNATES ***\n"

        # Conditional messaging based on terrain (9999 ft threshold)
        if max_mora <= 9999:
            howgozit += f"                *** NOT REQUIRED *** NO TERRAIN ***\n\n\n"
        else:
            howgozit += f"  *** NOT REQUIRED FOR RAMP WEIGHT AT OR BELOW {ramp_weight_formatted} LBS ***\n"
            howgozit += f"         ** CRUISE ALT MUST BE AT LEAST {oei_cruise_alt} FT **\n\n\n"

        # --- Add FPL section safely ---
        # --- Add FPL section safely ---
        if fpl:
            # Replace " -" with newline + "-" to break FPL into separate lines
            fpl_formatted = fpl.replace(" -", "\n-")
            # Wrap each line to 55 characters with proper indentation
            lines = fpl_formatted.split("\n")
            wrapped_lines = [
                textwrap.fill(line, width=55, 
                             subsequent_indent="  ",
                             break_long_words=False,
                             break_on_hyphens=False) 
                for line in lines
            ]
            fpl_final = "\n".join(wrapped_lines)
            howgozit += str(fpl_final)
        else:
            howgozit += ""
        howgozit += "\n\n"

        # --- Add navigation log safely ---
        nav_log = write_navigation_log(root, flight_info, takeoff_time)
        howgozit += str(nav_log) if nav_log else ""

        # Add each section carefully

        
        # Get scheduled times
        sched_off = format_time_elapsed(get_text("times/sched_off", "0"))
        sched_on = format_time_elapsed(get_text("times/sched_on", "0"))
        howgozit += "\n"
        # --- ZFW ±1000 impacts, HOWGOZIT single-line format (delta fuel, no COST) ---
        zfw_plus = root.find("impacts/zfw_plus_1000")
        zfw_minus = root.find("impacts/zfw_minus_1000")

        if zfw_plus is not None:
            time_diff_sec = int(zfw_plus.findtext("time_difference", "0"))  # use time_difference, not time_enroute
            time_diff_min = abs(time_diff_sec) // 60  # convert seconds delta to minutes
            time_sign = "M" if time_diff_sec < 0 else "P"  # negative = faster (M), positive = slower (P)
            burn_diff = int(zfw_plus.findtext("burn_difference", "0"))
            fl = int(zfw_plus.findtext("initial_fl", "0"))
            howgozit += f"RAMP WT P1000 TIME {time_sign}{time_diff_min:02d} FUEL P{burn_diff:04d} FL {fl}\n"

        if zfw_minus is not None:
            time_diff_sec = int(zfw_minus.findtext("time_difference", "0"))
            time_diff_min = abs(time_diff_sec) // 60
            time_sign = "M" if time_diff_sec < 0 else "P"
            burn_diff = int(zfw_minus.findtext("burn_difference", "0"))
            fl = int(zfw_minus.findtext("initial_fl", "0"))
            howgozit += f"RAMP WT M1000 TIME {time_sign}{time_diff_min:02d} FUEL M{abs(burn_diff):04d} FL {fl}\n\n"              
        # Fuel Planning Section (OFP Style)
        # FOS summary line: RWT 0-9, PLD 11-20, GND at 30, Q at 39,
        # CI at 44, SKD at 51.
        def _hhmm_to_min(v):
            try:
                v = str(v).strip().zfill(4)
                return int(v[:2]) * 60 + int(v[2:])
            except Exception:
                return 0
        _gnd_out = _hhmm_to_min(taxi_out_fmt)
        _gnd_in  = _hhmm_to_min(taxi_in_fmt)
        _ci_val  = int(float(get_text('general/costindex') or 0))
        # Q carries a sign only when negative: the references show "Q-02" and
        # a bare "Q00".
        _q_min   = _hhmm_to_min(est_out_hhmm) - _hhmm_to_min(sched_out_fmt)
        _q_str   = f"Q-{abs(_q_min):02d}" if _q_min < 0 else f"Q{_q_min:02d}"

        # The field after Q is the cruise profile: a cost index on a CI plan
        # ("CI0021"), otherwise the planned Mach ("M665" for .665).
        _cmode = (get_text('api_params/cruisemode') or '').strip().upper()
        if _cmode and _cmode != 'CI':
            _mach_raw = (get_text('general/cruise_mach') or '').strip()
            _mach_dig = _mach_raw.lstrip('0').lstrip('.')[:3]
            _perf_fld = f"M{_mach_dig:0<3}" if _mach_dig else f"CI{_ci_val:04d}"
        else:
            _perf_fld = f"CI{_ci_val:04d}"

        howgozit += (f"RWT {int(float(ramp_weight or 0)):06d} "
                     f"PLD {int(float(payload or 0)):06d}{'':9}"
                     f"GND{_gnd_out:02d}/{_gnd_in:02d} {_q_str} "
                     f"{_perf_fld} SKD{sched_out_fmt}/{sched_in_fmt}\n")
        try:
            _bias_v = (float(get_text('api_params/fuelfactor') or 1.0) - 1.0) * 100.0
        except Exception:
            _bias_v = 0.0
        howgozit += f"BIAS {'M' if _bias_v < 0 else 'P'}{abs(_bias_v):04.1f}\n"
        howgozit += f"AVG WIND DIR/COMP {avg_wind_dir}/{avg_wind_spd} AVG TD {avg_temp_dev:0>3}  CI{cost_index:0>4}\n\n"
        #ELEV {destination}:{destination_elev}FT\n\n"
        #STEP {get_text('general/stepclimb_string')}
        #wrapped_route = textwrap.fill(route, width=55)
        #howgozit += f"ROUTE: {wrapped_route}\n\n"

        _paf_lbs_ldr = int(get_text('fuel/plan_landing') or 0)
        _paf_flow2   = int(get_text('fuel/avg_fuel_flow') or 1) or 1
        _paf_res_s2  = int(get_text('times/reserve_time') or 0)
        _paf_altn_s2 = int(int(get_text('fuel/alternate_burn') or 0) / _paf_flow2 * 3600)
        _paf_xtra_s2 = int(get_text('times/extrafuel_time') or 0)
        _paf_secs2   = _paf_res_s2 + _paf_altn_s2 + _paf_xtra_s2
        _paf_hhmm    = f"{_paf_secs2 // 3600:02d}{(_paf_secs2 % 3600) // 60:02d}"

        # ── Fuel ladder formatters ────────────────────────────────────────────
        # fz6: any numeric value → 6-digit zero-padded integer string (raw lbs)
        # fz5: same but 5-digit (for BUFR sub-column)
        def fz6(v):
            try:
                return f"{int(round(float(v))):06d}"
            except Exception:
                return "000000"

        def fz5(v):
            try:
                return f"{int(round(float(v))):05d}"
            except Exception:
                return "00000"

        SEP = "---------------------------------------------------------------"

        # ── Resolve ACF/PBCF fuel FIRST (before any display) ─────────────────
        # Rules:
        #  • The ACF/PBCF line ONLY appears when api_params/addedfuel_label is
        #    non-empty. That field is set by SimBrief only when the operator has
        #    explicitly configured an ACF/PBCF policy for this aircraft type.
        #    A bare resvrule (e.g. "b343" with no label) is just SimBrief's
        #    reserve math choice and must NOT trigger the line.
        #  • When the label IS present and resvrule matches a PBCF aircraft
        #    family, display as "{RESVRULE} PBCF" (e.g. "B343 PBCF").
        #  • The line always prints even when fuel = 0 (trip within limits).

        _resvrule    = (get_text("api_params/resvrule") or "").strip().upper()
        _added_raw   = (get_text("api_params/addedfuel")       or "0").strip()
        _added_units = (get_text("api_params/addedfuel_units") or "min").strip().lower()
        _added_lbl   = (get_text("api_params/addedfuel_label") or "").strip().upper()

        # Gate: no label → no ACF line at all
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
            else:
                _acf_display_key = _added_lbl

        # Compute fuel amount when label is present
        if _acf_display_key:
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
                    _acf_lbs  = 0.0
                    _acf_secs = 0

                # fuel/etops proxy fallback
                if _acf_lbs == 0:
                    try:
                        _etops_lbs = float(get_text("fuel/etops") or "0")
                    except Exception:
                        _etops_lbs = 0.0
                    if _etops_lbs > 0:
                        _acf_lbs = _etops_lbs
                        _etops_t_raw = get_text("times/etopsfuel_time") or "0"
                        _acf_secs = int(_etops_t_raw) if _etops_t_raw.isdigit() else 0

                # Register (even at 0) so the line prints
                fuel_dict[_acf_display_key] = _acf_lbs
                _acf_t = format_time_elapsed(_acf_secs)
                if _acf_t:
                    time_dict[_acf_display_key] = _acf_t
                print(f"DEBUG ACF: '{_acf_display_key}'={_acf_lbs:.0f}lbs/{_acf_t} "
                      f"(rule={_resvrule} label={_added_lbl} addedfuel={_added_raw}{_added_units})")

        # ── BUFR calculation ──────────────────────────────────────────────────
        try:
            _bufr_delta = max(0, int(float(plan_takeoff)) - int(float(min_takeoff)))
            _da_val     = fuel_dict.get("DISP ADD", 0)
            _hold_val   = fuel_dict.get("HOLD", 0)
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

        # ── PLAN ARR FUEL / ENRT BRN header ──────────────────────────────────
        # Layout rule: label zone = exactly 15 chars (left-aligned, space-padded)
        #              fuel value = 6 digits starting at col 15
        #              time = 2 spaces + 4 digits
        #              dist = 1 space + 4 digits
        # Header: 'ARPT' ends at col 13, 'FUEL' at col 15 — matches all data rows.
        _rdist = int(float(get_text('general/route_distance') or 0))
        # Column geometry taken from the real FOS release:
        #   label 0-15, six-digit totals 16-21, five-digit components 17-21,
        #   TIME 23-26, DIST 28-31, BUFR label 36-39 with its value 47-51.
        def _fos_row5(label, fuel, time_="", dist=None, bufr=None):
            out = f"{label:<17}{int(float(fuel or 0)):05d}"
            out += f" {time_:>4}" if time_ else ""
            if dist is not None:
                out += f" {int(dist):04d}"
            if bufr:
                pad = 36 - len(out)
                out += " " * max(1, pad) + "BUFR" + " " * 7 + f"{int(float(bufr)):05d}"
            return out + "\n"

        howgozit += f"{'PLAN ARR FUEL':<16}{fz6(_paf_lbs_ldr)} {_paf_hhmm}\n"
        howgozit += SEP + "\n"
        howgozit += "         ARPT     FUEL TIME DIST\n"
        howgozit += (f"{'ENRT BRN ' + destination_iata:<16}{fz6(enroute_burn)} "
                     f"{enroute_time} {_rdist:04d}\n")
        howgozit += SEP + "\n"

        # ── ACF/PBCF — above RSV when the reserve rule carries one ─────────
        # EXTRA and TANKERING are generic SimBrief labels, not reserve rules,
        # and a zero bucket is not a ladder row: both produced a stray
        # "EXTRA 000000" line that the real release has no equivalent for.
        _acf_v = float(fuel_dict.get(_acf_display_key, 0) or 0) if _acf_display_key else 0.0
        if (_acf_display_key and _acf_v > 0
                and _acf_display_key.upper() not in ('EXTRA', 'TANKERING')):
            howgozit += _fos_row5(_acf_display_key, _acf_v,
                                  time_dict.get(_acf_display_key, ""))

        # ── Reserve ───────────────────────────────────────────────────────────
        howgozit += _fos_row5('RSV', reserve_fuel, reserve_time)

        # ── Contingency / E/RSV ───────────────────────────────────────────────
        try:
            _cont_lbs = int(float(get_text('fuel/contingency', '0') or '0'))
        except Exception:
            _cont_lbs = 0
        if _cont_lbs > 0:
            _cont_time = format_time_elapsed(int(get_text('times/contfuel_time', '0') or 0))
            howgozit += _fos_row5('E/RSV', _cont_lbs, _cont_time)

        # ── DISP ADD ──────────────────────────────────────────────────────────
        # A skeleton row: it prints whether or not it carries fuel, the way the
        # real release shows "ALTN NONE 00000 0000 0000" on a flight with no
        # alternate. Suppressing the empty rows left the ladder looking bare.
        _da_fuel = fuel_dict.get("DISP ADD", 0)
        _da_time = time_dict.get("DISP ADD", "") or "0000"
        howgozit += _fos_row5('DISP ADD', _da_fuel, _da_time,
                              bufr=disp_add_bufr if disp_add_bufr > 0 else None)

        # ── Alternate ─────────────────────────────────────────────────────────
        try:
            _altn_dist = int(float(get_text('alternate/distance') or '0'))
        except Exception:
            _altn_dist = 0
        _altn_lbl = f"ALTN  {altn_iata:>7}  "   # 4+2+7+2 = 15
        howgozit += _fos_row5(_altn_lbl.rstrip()[:16], alternate_burn,
                              alternate_time, dist=_altn_dist)

        # ── ETOPS ADD ─────────────────────────────────────────────────────────
        try:
            _etops_add_lbs = float(get_text("fuel/etops") or "0")
        except Exception:
            _etops_add_lbs = 0.0
        if _etops_add_lbs > 0 and not any(
            fuel_dict.get(l, 0) == _etops_add_lbs for l in ("ACF90", "ACF99")
        ):
            _ea_t = format_time_elapsed(int(get_text("times/etopsfuel_time") or 0))
            howgozit += _fos_row5('ETOPS', _etops_add_lbs, _ea_t)

        # ── HOLD ──────────────────────────────────────────────────────────────
        # Skeleton row, printed at zero like DISP ADD and ALTN.
        _hf_fuel = fuel_dict.get("HOLD", 0)
        _hf_time = time_dict.get("HOLD", "") or "0000"
        howgozit += _fos_row5('HOLD', _hf_fuel, _hf_time,
                              bufr=hold_bufr if hold_bufr > 0 else None)

        # ── Remaining dynamic buckets (DISP EXTRA, MEL, TANKERING) ───────────
        for label in ("DISP EXTRA", "MEL", "TANKERING"):
            fuel_val = fuel_dict.get(label, 0)
            if fuel_val > 0:
                howgozit += _fos_row5(label, fuel_val, time_dict.get(label, ""))

        # Unknown buckets
        _known = {"DISP ADD", "HOLD", "ACF90", "ACF99", "PBCF",
                  "DISP EXTRA", "MEL", "TANKERING"}
        if _acf_display_key:
            _known.add(_acf_display_key)
        for label in (k for k in fuel_dict if k not in _known):
            fuel_val = fuel_dict[label]
            # EXTRA is not a FOS ladder row, and zero buckets are not printed
            if fuel_val > 0 and label.upper() != 'EXTRA':
                howgozit += _fos_row5(label, fuel_val, time_dict.get(label, ""))

        # ── T/O fuel, taxi, totals ────────────────────────────────────────────
        howgozit += SEP + "\n"
        howgozit += (f"{'T/O FUEL':<16}{fz6(plan_takeoff)}{'':14}"
                     f"MIN T/O{'':3}{fz6(min_takeoff)}\n")
        howgozit += SEP + "\n"
        try:
            _taxi_lbs = int(float(get_text('fuel/taxi') or '0'))
        except Exception:
            _taxi_lbs = 0
        howgozit += _fos_row5('TAXI     ' + origin_iata, _taxi_lbs, taxi_out_fmt)
        howgozit += " " * 16 + "------\n"
        howgozit += f"{'TOTAL':<16}{fz6(plan_ramp)}\n\n"
        howgozit += f"{'RLS FUEL ' + origin_iata:<18}{fz6(plan_ramp)}\n\n"





        # After getting the user's takeoff time, update the display format
        if 'takeoff_time_sec' in locals() and takeoff_time_sec:
            # Convert the actual takeoff time to display format
            actual_takeoff_hours = takeoff_time_sec // 3600
            actual_takeoff_minutes = (takeoff_time_sec % 3600) // 60
            actual_off_fmt = f"{actual_takeoff_hours:02d}{actual_takeoff_minutes:02d}"
            print(f"DEBUG: Updated actual_off_fmt = {actual_off_fmt}")
        else:
            # Fall back to estimated off time
            actual_off_fmt = est_off_fmt.replace(":", "").replace("Z", "")

        # Function to convert UTC time to local time
        def convert_to_local_time(utc_time_str, timezone_offset):
            """
            Convert UTC time (HHMMZ format) to local time with timezone offset
            timezone_offset: hours difference from UTC (e.g., -4, -5)
            """
            # Extract hours and minutes from HHMMZ format
            if utc_time_str.endswith('Z'):
                utc_time_str = utc_time_str[:-1]  # Remove Z
            
            hours = int(utc_time_str[:2])
            minutes = int(utc_time_str[2:])
            
            # Add timezone offset
            local_hours = (hours + timezone_offset) % 24
            if local_hours < 0:
                local_hours += 24
            
            return f"{local_hours:02d}{minutes:02d}"

        # Get timezone offsets (these would come from your flight data)
        orig_timezone = get_text('times/orig_timezone')  # e.g., -4
        dest_timezone = get_text('times/dest_timezone')  # e.g., -5

        # Convert times to local
        sched_out_local = convert_to_local_time(sched_out_fmt, int(orig_timezone))
        sched_in_local = convert_to_local_time(sched_in_fmt, int(dest_timezone))
        est_out_local = convert_to_local_time(est_out_hhmm, int(orig_timezone))
        est_in_local = convert_to_local_time(est_in_fmt, int(dest_timezone))

        # BUILD THE ON TIME ANALYSIS SECTION ONLY ONCE - PUT THIS AT THE END
        # Make sure this is the ONLY place where you build this section
        # Build time analysis section
        # FOS layout: taxi times in whole minutes, air and block as decimal
        # hours, no DEP/ARR columns.
        #   tag 0-5, TXO 7-8, AIR 10-13, TXI 20-21, TOTAL 25-28
        def _mins(v):
            try:
                v = str(v).strip().zfill(4)
                return int(v[:2]) * 60 + int(v[2:])
            except Exception:
                return 0

        def _hdec(v):
            m = _mins(v)
            return f"{m // 60}.{m % 60:02d}"

        def _ota_row(tag, txo, air, txi, total):
            return (f"{tag:<6} {_mins(txo):>2} {_hdec(air):>4}{'':6}"
                    f"{_mins(txi):>2}{'':3}{_hdec(total):>4}")

        title_text  = "ON-TIME ANALYSIS   **********"
        header_line = f"{'':7}TXO{'':3}AIR{'':3}TXI TOTAL"
        line_skdblk = _ota_row("SKDBLK", taxi_out_fmt, sched_enrt_fmt, taxi_in_fmt, SKD_BLK)
        line_flipln = _ota_row("FLIPLN", taxi_out_fmt, enrt_fmt, taxi_in_fmt, EST_BLK)
        # Calculate width and create borders
        title_line = title_text
        # Assemble section - ADD TO howgozit ONLY ONCE
        howgozit += (
            f"{title_line}\n"
            f"{header_line}\n"
            f"{line_skdblk}\n"
            f"{line_flipln}\n\n"
        )


        # === SEL DATABASE (REFERENCE TABLE) ===
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
            "A321-253NX": ["03", "10", "13", "18", "23", "31", "32", "34"]
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
            "34": "FLS INSTALLED"
        }

     
        # === RMKS / ADDITIONAL SECTIONS ===
        howgozit += "RMKS/:\n"
        wrapped_remarks = textwrap.fill(' '.join(dx_rmks), width=80)
        howgozit += wrapped_remarks + "\n\n"
        howgozit += "ACFT RSTR: - NONE\n\n"
        howgozit += "MEL ITEMS: - NONE\n\n"
        howgozit += "NEF ITEMS: - NONE\n\n"

        # === SEL ITEMS SECTION ===
        if aircraft_type in SEL_DATABASE:
            sel_items = []
            for sel_code in SEL_DATABASE[aircraft_type]:
                desc = SEL_DESCRIPTIONS.get(sel_code, "UNKNOWN SEL ITEM")
                sel_items.append(f"320 {sel_code} {desc}")

            if sel_items:
                howgozit += "SEL ITEMS\n"
                for sel in sel_items:
                    howgozit += f"{sel}\n"
                howgozit += "\n"
            else:
                howgozit += "SEL ITEMS: - NONE\n\n"
        else:
            howgozit += "SEL ITEMS: - NONE\n\n"

        # === DISPATCH SIGNATURE AND CREW ===
        howgozit += f"\n"*3
        howgozit += f"DISP SIGNED BY  {dispatcher}\n"
        howgozit += f"FD 09 (512)-555-0198\n\n"
        howgozit += f"CAPT   {PID} {format(cptn, '<17')}       CAT1 YES\n"
        howgozit += f"F/O    375451 {format(fo, '<17')}       CAT2 YES\n\n"
        howgozit += f"FAR117 MAX TAXI 181 MIN. UP TO 30 MIN EARLY DPTR OK IAW FM PT1\n\n\n\n"



        howgozit += f"AUZD CAPTAIN SIGNATURE.........................\n\n"
        howgozit += f"BY SIGNING OFF THIS FLIGHT PLAN YOU ARE ACKNOWLEDGING\n"
        howgozit += f"** FIT FOR DUTY BASED ON FAR 117.5 REQUIREMENTS.\n"
        howgozit += f"** FOR YOUR AWARENESS, THE *MOT* TIME DISPLAYED INCLUDES PILOT\n"
        howgozit += f"AUTHORIZED FDP EXTENSION. THE *LMT* TIME INCLUDES THE MAXIMUM\n"
        howgozit += f"FDP EXTENSION POSSIBLE BASED UPON PLAN DEPARTURE TIME, FOR\n"
        howgozit += f"UNFORSEEN OPERATIONAL CIRCUMSTANCES.\n"
        howgozit += f"IF APPROACHING ACTUAL DUTY LIMITATION, CAPTAIN MUST CONTACT\n"
        howgozit += f"DISPATCH TO COORDINATE FDP EXTENSION.\n\n\n"
        

        


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

        # === DECS pages — last, after the NOTAMs ===
        # DECS is the FOS-era terminal, so WBD*/NSC/FI/FIL/SLS are native to
        # this release rather than bolted on. No [WATERMARK_END] here: the
        # Flightkeys release ID is a post-2022 artefact and this format has
        # none, so there is nothing to stop.
        try:
            _fos_ctx = _fos_context(root)
            howgozit += build_nsc_page(root, _fos_ctx, cpt=cptn, fo=fo)
            howgozit += build_fi_page(root, _fos_ctx, cpt=cptn)
            howgozit += build_fil_page(root, _fos_ctx)
        except Exception as _fos_e:
            print(f"DEBUG: FOS pages (NSC/FI/FIL) skipped: {_fos_e}")

        _fr = _write_field_reports_sls or write_field_reports
        field_output = _fr(root)
        if field_output:
            howgozit += field_output

        # === Jet fuel service record (form 10012) ===
        # This release has no index page, so the index marker is dropped rather
        # than left to print as literal text.
        try:
            _fuel_pg = build_fuel_service_page(root, _fos_ctx)
            howgozit += "\n".join(l for l in _fuel_pg.splitlines()
                                  if not l.strip().startswith("[INDEX_ENTRY:")) + "\n"
        except Exception as _fuel_e:
            print(f"DEBUG: Fuel service page skipped: {_fuel_e}")

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

        # Pass everything into the builder INCLUDING ICAO
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
            "A320-271N": {"F": 0, "C": 0, "W": 0, "Y": 186},
            "A320-251N": {"F": 0, "C": 0, "W": 0, "Y": 186},

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
        _flt_num = ((root.findtext('general/icao_airline') or '') +
                    (root.findtext('general/flight_number') or '')).strip()

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
        flt  = ((root.findtext("general/icao_airline") or "") +
                (root.findtext("general/flight_number") or "")).strip()
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
    """Navigation log: two physical lines per fix, stacked pairs, fixed-width columns."""

    def navlog_header():
        return (
            "TO            LAT    LONG    MC  MK  GS  TD  SD   ST   SB\n"
            "IDENT      FL WIND   WCP     MH  TRR TAS I   TLDR TTLT TTLB  TH\n"
            "\n"
        )

    def safe_float(val, default=0.0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def get_text(elem, tag):
        n = elem.find(tag)
        return n.text.strip() if n is not None and n.text else ""

    def format_lat(lat):
        """NDDMMT — degrees, minutes and tenths, as the real FOS release
        prints them (RBV at 40.2019 N is N40121, not decimal degrees)."""
        prefix = 'N' if lat >= 0 else 'S'
        a = abs(lat); d = int(a); m10 = round((a - d) * 600)
        if m10 >= 600:
            d += 1; m10 = 0
        return f"{prefix}{d:02d}{m10:03d}"

    def format_lon(lon):
        """EDDDMMT — degrees, minutes and tenths."""
        prefix = 'E' if lon >= 0 else 'W'
        a = abs(lon); d = int(a); m10 = round((a - d) * 600)
        if m10 >= 600:
            d += 1; m10 = 0
        return f"{prefix}{d:03d}{m10:03d}"

    def format_wind(wd, ws):
        return f"{wd or '0':>3}{ws or '0':>2}"

    def format_wcp(val):
        val = safe_float(val, None)
        if val is None:
            return "  0"
        prefix = 'P' if val >= 0 else 'M'
        return f"{prefix}{abs(int(val)):02d}"

    def format_td(val):
        val = safe_float(val, None)
        if val is None:
            return "  0"
        prefix = 'P' if val >= 0 else 'M'
        return f"{prefix}{abs(int(val)):02d}"

    def format_time(sec):
        sec = safe_float(sec)
        mins = round(sec / 60)
        return f"{int(mins // 60):02d}{int(mins % 60):02d}"

    def format_fuel(fuel):
        return f"{int(round(safe_float(fuel)/100)):04d}"

    fixes = root.findall("navlog/fix")
    total_distance = sum(safe_float(get_text(f, "distance")) for f in fixes)
    cumulative = []
    run = 0
    for f in fixes:
        run += safe_float(get_text(f, "distance"))
        cumulative.append(run)

    nav_log = navlog_header()

    for idx, f in enumerate(fixes):
        name = (get_text(f, "name") or "XXXX").upper()

        if "TOP OF DESCENT" in name:
            name = "BGN DESCENT"

        is_climb_or_descent = any(x in name.upper() for x in ["TOP OF CLIMB", "BGN DESCENT"])

        name_print = name[:12]

        lat = format_lat(safe_float(get_text(f, "pos_lat"))) if not is_climb_or_descent else "     "
        lon = format_lon(safe_float(get_text(f, "pos_long"))) if not is_climb_or_descent else "      "
        mc = get_text(f, "track_mag").rjust(3)

        mk_raw = get_text(f, "mach")
        try:
            mk_val = float(mk_raw)
            mk = str(int(mk_val * 1000)).rjust(3)
        except (TypeError, ValueError):
            mk = "000"

        gs = get_text(f, "groundspeed").rjust(3)
        td = format_td(get_text(f, "oat_isa_dev"))
        sd = str(int(safe_float(get_text(f, "distance")))).zfill(4)
        st = format_time(get_text(f, "time_leg"))
        sb = format_fuel(get_text(f, "fuel_leg"))

        ident = get_text(f, "ident") or "----"
        fl = str(int(safe_float(get_text(f, "altitude_feet"))//1000)).rjust(3)
        wind = format_wind(get_text(f, "wind_dir"), get_text(f, "wind_spd"))
        wcp = format_wcp(get_text(f, "wind_component"))
        mh = get_text(f, "heading_mag").rjust(3)
        trr = str(int(safe_float(get_text(f, "mora"))//100)).zfill(3)
        tas = get_text(f, "true_airspeed").rjust(3)
        i_col = get_text(f, "shear") or "----"
        tldr = str(int(total_distance - cumulative[idx])).zfill(4)
        ttlt = format_time(get_text(f, "time_total"))
        ttlb = format_fuel(get_text(f, "fuel_totalused"))
        tropopause = get_text(f, "tropopause_feet")
        th = str(int(safe_float(tropopause)/1000)).rjust(3) if tropopause else "   "

        nav_log += (
            f"{name_print:<13} {lat:<6} {lon:<7} {mc:<3} {mk:<3} {gs:<3} {td:<3} {sd:<4} {st:<4} {sb:<4}\n"
            f"{ident:<9} {fl:<3} {wind:<6} {wcp:<7} {mh:<3} {trr:<3} {tas:<3} {i_col:<3} {tldr:<4} {ttlt:<4} {ttlb:<4} {th:>3}\n"
            "---------------------------------------------------------------\n"
        )

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

# ═══════════════════════════════════════════════════════════════════════════════
# NOTAM HELPERS — shared logic used by departure, arrival, alternate, enroute
# ═══════════════════════════════════════════════════════════════════════════════

import textwrap as _tw
import re as _re

# ── Date helpers ──────────────────────────────────────────────────────────────

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
            from datetime import timezone as _tz
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
    from datetime import timezone as _tz
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
            body_lines.append(_tw.fill(para, width=72))
    body = "\n".join(body_lines)

    return f"{hdr}\n{line2}\n\n{body}\n"


def _cat_banner(label):
    """
    Dark-banner category label matching OFP style.
    Rendered as a solid border line with centred label, e.g.:

    ═══════════════════════════════ GENERAL ════════════════════════════════
    """
    BW = 72
    inner = f" {label} "
    pad   = BW - len(inner)
    left  = pad // 2
    right = pad - left
    return f"\n{'═' * left}{inner}{'═' * right}\n"


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

def _airport_notam_header(airport_code, airport_name, role, runways="", iata=""):
    """
    Render the airport block header matching OFP badge style.
    iata is the real IATA code passed in from the XML section.
    """
    BW    = 72
    right = f"{iata} - {airport_name}" if iata and airport_name else airport_name
    left_part  = f"{airport_code}   {role}"
    line1 = f"{left_part:<42}{right}"
    indent = " " * (len(airport_code) + 3)

    out = f"\n{'═' * BW}\n"
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
    out += f"{'═' * BW}\n\n"
    return out


def _make_runway_pair(rwy_id):
    """
    Given a single runway end identifier (e.g. '07L'), return the full paired
    designator (e.g. '07L/25R'). Returns None if the input is not a valid runway.
    Always puts the lower-numbered end first.
    """
    import re as _re3
    m = _re3.match(r'^(\d{1,2})([LRC]?)$', rwy_id.strip().upper())
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
    import re as _re3

    def sort_key(r):
        n = _re3.search(r'\d+', r)
        return int(n.group()) if n else 99

    def collect_runways(rwy_elements):
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
                                    _get_runways(xml_root, section), iata=airport_iata)
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
                                    _get_runways(xml_root, section), iata=airport_iata)
    body = _build_notam_section(category_order, categorized, page_break_after=False)
    return header + body if body else ""


# ── Alternate NOTAMs ──────────────────────────────────────────────────────────

def get_alternate_notams_sorted(xml_root, section_name):
    """Return formatted alternate NOTAMs string for the given XML section."""
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

    header = _airport_notam_header(airport_code, airport_name, "ALTN 1",
                                    _get_runways(xml_root, section), iata=airport_iata)
    body = _build_notam_section(category_order, categorized, page_break_after=True)
    return header + body if body else ""


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
    import re as _rq
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
    m = _rq.search(r"Q\)\s*\w*/Q([A-Z]{2})", raw_text)
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

    from datetime import timezone as _tz
    from collections import OrderedDict
    import textwrap as _tw

    # Build navlog-ordered FIR list
    navlog_fir_order = []
    seen_firs = set()
    for fix in xml_root.findall("navlog/fix"):
        fcode = (fix.findtext("fir") or "").strip().upper()
        if fcode and fcode not in seen_firs:
            navlog_fir_order.append(fcode)
            seen_firs.add(fcode)

    now = datetime.now(_tz.utc)
    by_facility = OrderedDict()  # facility → {'icao_id':..., 'active': [...], 'expired': [...]}

    import re as _re_enrt

    def _parse_notam_raw(raw, eff_dtg, exp_dtg, cre_dtg):
        """
        Parse a raw ICAO NOTAM text block. Returns (body, fl_str, eff_dt, exp_dt, cre_dt, is_est).
        Extracts fields directly from raw text when DTG attributes are missing/wrong.
        """
        # E) body — everything between E) and next field marker or end
        em = _re_enrt.search(r'\nE\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, _re_enrt.DOTALL)
        if not em:
            em = _re_enrt.search(r'^E\)\s*(.*?)(?=\n[A-GQ]\)|$)', raw, _re_enrt.DOTALL | _re_enrt.MULTILINE)
        body = em.group(1).strip() if em else raw.strip()

        # F)/G) flight levels
        f_m = _re_enrt.search(r'\nF\)\s*(\S+)', raw)
        g_m = _re_enrt.search(r'\nG\)\s*(\S+)', raw)
        fl_str = f"{f_m.group(1).upper() if f_m else 'SFC'} - {g_m.group(1).upper() if g_m else 'UNL'}"

        # B) effective — prefer DTG attribute, fall back to raw field
        eff_dt = _parse_dtg(eff_dtg)
        if not eff_dt:
            bm = _re_enrt.search(r'\nB\)\s*(\d{10})', raw)
            if bm:
                eff_dt = _parse_dtg(bm.group(1))

        # C) expiry + EST flag — parse from raw text for accuracy
        cm = _re_enrt.search(r'\nC\)\s*(\d{10}|PERM)\s*(EST)?', raw, _re_enrt.IGNORECASE)
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
            cm2 = _re_enrt.search(r'\nC\)\s*(\d{10})', raw_text)
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
                body_lines.append(_tw.fill(para, width=72))
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
        result += f"\n{'═' * BW}\n"
        result += f"{fir_code}   {facility}\n"
        result += f"{'═' * BW}\n\n"

        # Render active NOTAMs by category
        for cat in _ENRT_CATEGORY_ORDER:
            entries = sorted(cats.get(cat, []), key=lambda x: x[0], reverse=True)
            if not entries:
                continue
            inner = f" {cat} "
            pad   = BW - len(inner)
            result += f"{'═' * (pad//2)}{inner}{'═' * (pad - pad//2)}\n"
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


def is_numeric(val):
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False

def safe_weight(weight):
    """
    Convert weight to thousands with proper handling.
    Returns float value divided by 1000, or 0.0 if conversion fails.
    """
    try:
        return float(weight) / 1000.0
    except (ValueError, TypeError):
        return 0.0
from SPEEDOTHER import get_speed_other
from ENGINEFAILPROC import get_airport_specific_altitudes
import re
from SPEEDOTHER import get_speed_other, get_reduced_thrust_n1
from ENGINEFAILPROC import get_airport_specific_altitudes

def safe_weight(value):
    """Convert weight value (lbs) to thousands with 1 decimal. Returns None on failure or
    zero input so callers can distinguish 'no data' from a genuine 0-lb weight."""
    try:
        result = float(value) / 1000.0
        return result if result != 0.0 else None
    except (ValueError, TypeError):
        return None

    
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
                print("[DEBUG WB-SPLIT] [TPS_START] not found — using text from first PAGEBREAK")
                _pb = howgozit_text.rfind('[PAGEBREAK]')
                _wb_content = howgozit_text[_pb + len('[PAGEBREAK]'):] if _pb != -1 else howgozit_text

            # WBD header: WBD*FLTNUMDATE/DDMMM/HHMM STA
            _zulu_now   = datetime.utcnow().strftime("%H%M")
            _sta_code   = (get_text("origin/iata_code", xml_root) or get_text("origin/icao_code", xml_root) or "XXX")[:4].upper()
            _sep_line   = "\u2014" * 60 + "\n"
            _wbd_header = f"WBD*{flt_clean}/{_date_str}/{_zulu_now} {_sta_code}\n\n"
            _wb_text    = _sep_line + _wbd_header + _wb_content

        except Exception as _ex:
            print(f"[DEBUG WB-SPLIT] Error: {_ex} — using full text")
            _wb_text = howgozit_text

        print(f"DEBUG: Saving RLS to: {rls_path}")
        print(f"DEBUG: Saving WB  to: {wb_path}")

        # --- Save PDFs ---
        save_as_pdf(rls_path, howgozit_text)          # full OFP
        save_as_pdf(wb_path,  _wb_text)               # TPS + W&B only
        print(f"RLS saved: {rls_path}")
        print(f"WB  saved: {wb_path}")

        # --- Auto-open both files ---
        for _path in (rls_path, wb_path):
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

__version__ = "1.1.0"

if __name__ == "__main__":
    main()
