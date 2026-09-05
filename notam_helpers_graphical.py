#!/usr/bin/env python3
"""
notam_helpers_graphical.py  — Graphical (ReportLab landscape two-column) NOTAM rendering.

Exports used by MASTERLOG_Jepp.py:
    _draw_notam_section(c, notam_text, font_name, font_size=7)
    get_departure_notams_sorted(xml_root, section_name) -> str
    get_arrival_notams_sorted(xml_root, section_name)   -> str
    get_alternate_notams_sorted(xml_root, section_name) -> str
    get_enroute_notams(xml_root)                        -> str
"""

import os
from datetime import datetime
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

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

