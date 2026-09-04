#!/usr/bin/env python3
"""
AA PBS bid-pack formatter — text and PDF.

Column map measured off the real pack
(494061144-PBS-PHX-March-2021), page 12:

           1         2         3         4         5         6         7         8         9        10
 0....5....0....5....0....5....0....5....0....5....0....5....0....5....0....5....0....5....0....5....0....5..
     DAY         --DEPARTURE--    ---ARRIVAL---                 GRND/        REST/
 DP D/A EQ FLT# STA DLCL/DHBT ML STA ALCL/AHBT BLOCK SYNTH        TPAY  DUTY TAFB    FDP CALENDAR 03/02-03/31
 -----------------------------------------------------------------------------------------------------------

field      starts   numerics right-align at
DP           0
D/A          2
EQ           6
FLT#         9      (left-aligned, width 4)
STA orig    14
DLCL/DHBT   18      "HHMM/HHMM"
ML          28      (single char, blank when none)
STA dest    30
ALCL/AHBT   34
BLOCK                      54
SYNTH                      61
TPAY                       69
DUTY                       75
GRND / TAFB                81
FDP                        87
CALENDAR    88      seven 3-char columns: 88 91 94 97 100 103 106
separator   107 chars
"""
from __future__ import annotations
import calendar as _cal
from datetime import date, timedelta

W_SEP   = 107
C_ML, C_DEST, C_ADEP = 30, 33, 37
R_BLOCK, R_SYNTH, R_TPAY, R_DUTY, R_TAFB, R_FDP = 56, 63, 71, 77, 83, 89
C_CAL, CAL_W = 90, 3
DOW = ['MO','TU','WE','TH','FR','SA','SU']

def _put(buf, col, text):
    if text is None: return
    text = str(text)
    if col + len(text) > len(buf): buf.extend([' '] * (col + len(text) - len(buf)))
    buf[col:col+len(text)] = list(text)

def _rt(buf, right, text):
    if text is None or text == '': return
    _put(buf, right - len(str(text)), str(text))

def _blank(): return [' '] * W_SEP

def _line(buf): return ''.join(buf).rstrip()

def hhmm(h):
    h %= 24
    return f'{int(h):02d}{int(round((h - int(h)) * 60)):02d}'

def bid(h):
    """PBS money/duration format: HH.MM where MM are real minutes (4.01 = 4h01)."""
    if h is None: return ''
    neg = h < 0; h = abs(h)
    hh = int(h); mm = int(round((h - hh) * 60))
    if mm == 60: hh, mm = hh + 1, 0
    return f'{"-" if neg else ""}{hh}.{mm:02d}'

# ── calendar ────────────────────────────────────────────────────────────────
def bid_period(year, month):
    """PBS periods run roughly the 2nd of the month to the 1st of the next."""
    s = date(year, month, 2)
    e = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return s, e

def calendar_rows(start_dow, period, n_weeks=5):
    """Grid of 7 columns x n_weeks. Cell = day number when the pairing starts
    that date, '--' when it does not, '' when outside the period."""
    s, e = period
    first = s + timedelta(days=(start_dow - s.weekday()) % 7)
    dates = []
    d = first
    while d <= e:
        dates.append(d); d += timedelta(days=7)
    rows, ops = [], len(dates)
    grid_start = s - timedelta(days=s.weekday())
    for w in range(n_weeks):
        row = []
        for c in range(7):
            day = grid_start + timedelta(days=w * 7 + c)
            if day < s or day > e: row.append('')
            elif day in dates:     row.append(str(day.day))
            else:                  row.append('--')
        rows.append(row)
    return rows, ops

# ── one sequence ────────────────────────────────────────────────────────────
def sequence_lines(seq_no, days, ops, cal_rows, note=''):
    """days = [ {rpt, rls, legs:[...], duty, fdp, block, synth, tpay, hotel, rest} ]"""
    out = []
    b = _blank()
    _put(b, 0, f'SEQ {seq_no:04d}')
    _rt(b, 13, str(ops)); _put(b, 14, 'OPS')
    _put(b, 20, 'POSN CA FO')
    if note: _put(b, 33, note.upper())
    for i, d in enumerate(DOW): _put(b, C_CAL + i * CAL_W, d)
    out.append(_line(b))

    cal = [c for row in cal_rows for c in [row]]
    ci = 0
    def cal_for(idx):
        return cal[idx] if idx < len(cal) else None

    tafb_total = 0.0
    for di, day in enumerate(days):
        b = _blank()
        _put(b, 16, 'RPT'); _put(b, 20, day['rpt'])
        r = cal_for(ci)
        if r:
            for i, c in enumerate(r): _put(b, C_CAL + i * CAL_W, c)
            ci += 1
        out.append(_line(b))

        for lg in day['legs']:
            b = _blank()
            # EQ is four wide, as the real packs print it ("321R"), so the
            # flight number starts at 11 rather than 9. Writing a four-char
            # code into a three-char slot let the flight number overwrite
            # its last character.
            _put(b, 0, str(lg['dp'])); _put(b, 2, lg['da']); _put(b, 6, lg['eq'][:4])
            _put(b, 11, lg['flt'])
            _put(b, 16, lg['orig']); _put(b, 20, lg['dep'])
            if lg.get('ml'): _put(b, C_ML, lg['ml'])
            _put(b, C_DEST, lg['dest']); _put(b, C_ADEP, lg['arr'])
            _rt(b, R_BLOCK, bid(lg['blk']))
            if lg.get('grnd'): _rt(b, R_TPAY, bid(lg['grnd']))
            r = cal_for(ci)
            if r:
                for i, c in enumerate(r): _put(b, C_CAL + i * CAL_W, c)
                ci += 1
            out.append(_line(b))

        b = _blank()
        _put(b, 33, 'RLS'); _put(b, 37, day['rls'])
        _rt(b, R_BLOCK, bid(day['block'])); _rt(b, R_SYNTH, bid(day.get('synth', 0)))
        _rt(b, R_TPAY, bid(day.get('tpay', day['block'])))
        _rt(b, R_DUTY, bid(day['duty'])); _rt(b, R_FDP, bid(day['fdp']))
        r = cal_for(ci)
        if r:
            for i, c in enumerate(r): _put(b, C_CAL + i * CAL_W, c)
            ci += 1
        out.append(_line(b))

        if day.get('hotel'):
            b = _blank()
            _put(b, 16, f'{day["hotel"]} HOTEL TBD')
            _rt(b, R_TAFB, bid(day['rest']))
            r = cal_for(ci)
            if r:
                for i, c in enumerate(r): _put(b, C_CAL + i * CAL_W, c)
                ci += 1
            out.append(_line(b))

    b = _blank()
    _put(b, 0, 'TTL')
    tb = sum(d['block'] for d in days)
    ts = sum(d.get('synth', 0) for d in days)
    _rt(b, R_BLOCK, bid(tb)); _rt(b, R_SYNTH, bid(ts))
    _rt(b, R_TPAY - 2, bid(tb + ts))
    _rt(b, R_TAFB, bid(days[-1].get('tafb', 0)))
    r = cal_for(ci)
    if r:
        for i, c in enumerate(r): _put(b, C_CAL + i * CAL_W, c)
    out.append(_line(b))
    out.append('-' * W_SEP)
    return out

# ── page assembly ───────────────────────────────────────────────────────────
def page_header(base, base_name, month_name, period):
    s, e = period
    a = _blank()
    _put(a, 4, 'DAY'); _put(a, 16, '--DEPARTURE--'); _put(a, 33, '---ARRIVAL---')
    _put(a, 63, 'GRND/'); _put(a, 76, 'REST/')
    b = _blank()
    _put(b, 0, 'DP D/A EQ FLT# STA DLCL/DHBT ML STA ALCL/AHBT BLOCK SYNTH')
    _put(b, 65, 'TPAY'); _put(b, 71, 'DUTY'); _put(b, 76, 'TAFB'); _put(b, 84, 'FDP')
    _put(b, 88, f'CALENDAR {s.month:02d}/{s.day:02d}-{e.month:02d}/{e.day:02d}')
    return [f'{base} - {base_name}  {month_name}'.upper(), '', _line(a), _line(b), '-' * W_SEP]

def build_text(seq_blocks, base, base_name, month_name, period):
    out = page_header(base, base_name, month_name, period)
    for blk in seq_blocks: out += blk
    return '\n'.join(out) + '\n'

# ── PDF ─────────────────────────────────────────────────────────────────────
def write_pdf(seq_blocks, path, base, base_name, month_name, period,
              fleet='', issued=None, eff=None, lines_per_page=58):
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.pdfgen import canvas
    W, H = landscape(letter)
    c = canvas.Canvas(path, pagesize=(W, H))
    LEFT, TOP, LEAD, FS = 24, H - 34, 8.6, 7.1
    hdr = page_header(base, base_name, month_name, period)
    issued = issued or date.today()
    eff = eff or period[0]

    body = [l for blk in seq_blocks for l in blk]
    # never split a sequence across a page
    pages, cur, n = [], [], 0
    for blk in seq_blocks:
        if n + len(blk) > lines_per_page and cur:
            pages.append(cur); cur, n = [], 0
        cur += blk; n += len(blk)
    if cur: pages.append(cur)

    for pi, page in enumerate(pages, 1):
        c.setFont('Courier-Bold', FS)
        y = TOP
        for l in hdr:
            c.drawString(LEFT, y, l); y -= LEAD
        c.setFont('Courier', FS)
        for l in page:
            c.drawString(LEFT, y, l); y -= LEAD
        c.setFont('Courier', 6.2)
        foot = (f'COCKPIT   ISSUED {issued.strftime("%d%b%Y").upper()}   '
                f'EFF {eff.strftime("%d%b%Y").upper()}   {base}{fleet}')
        c.drawString(LEFT, 20, foot)
        c.drawRightString(W - 24, 20, f'PAGE {pi} OF {len(pages)}')
        c.showPage()
    c.save()
    return len(pages)
