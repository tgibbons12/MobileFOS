"""
calm_wind_tlr.py — calm-wind takeoff data from the TLR tables
=============================================================

SimBrief embeds the raw TLR text in <tlr_section>, and inside it are the
calm-wind tables the TPS is actually supposed to be built from:

    -------------------- DRY RWY - PTOW - CALM WIND --------------------
    RWY          MTOW MT CONFIG                   FLP  V1  VR  V2  LIMIT
    02           1210 50 FULL THRUST               13 124 130 139  AFM
    ...
    --------------- DRY RWY - PTOW PLUS 2000 - CALM WIND ---------------

The per-runway <takeoff><runway> blocks carry SimBrief's *wind-adjusted*
figures instead, which is why a headwind runway arrives already credited.
These tables are the authoritative no-wind source.

Two brackets matter and neither is fixed:

  * the upper table's offset above PTOW, which tracks aircraft size —
    roughly 2% of MTOW, so about +1000 lb on a small aeroplane, +4000 on a
    narrowbody and +10000 on a widebody. It is read from the header rather
    than assumed.
  * the ATOW offset above PTOW, likewise size-dependent: +1000 / +2000 /
    +3000.

The interpolation fraction is simply atow_delta / plus, so a narrowbody
bracketed at +4000 lands at 0.50 while a plan bracketed at +2000 lands at
1.00 — the ATOW row *is* the upper table.
"""

from __future__ import annotations

import logging
import re

LOG = logging.getLogger(__name__)

_HDR_RE = re.compile(
    r'-{3,}\s*(DRY|WET)\s+RWY\s*-\s*PTOW(?:\s+PLUS\s+(\d+))?\s*-\s*CALM WIND',
    re.IGNORECASE)

# RWY  MTOW  MT  CONFIG (may contain spaces)  FLP  V1  VR  V2  LIMIT
_ROW_RE = re.compile(
    r'^\s*([0-9]{1,2}[LCRWXYZ]?)\s+(\d{3,5})\s+(\d{1,3})\s+(.+?)\s+'
    r'(\d{1,2})\s+(\d{2,3})\s+(\d{2,3})\s+(\d{2,3})\s+([A-Z]{3})\s*$')

# ATOW sits this far above PTOW, by aircraft size
_SMALL_MAX_LB     = 100_000
_WIDEBODY_MIN_LB  = 300_000


def atow_delta_lbs(struct_mtow_lbs):
    """+1000 small, +2000 narrowbody, +3000 widebody."""
    try:
        mtow = float(struct_mtow_lbs or 0)
    except (TypeError, ValueError):
        mtow = 0.0
    if mtow <= 0:
        return 2000.0
    if mtow < _SMALL_MAX_LB:
        return 1000.0
    if mtow >= _WIDEBODY_MIN_LB:
        return 3000.0
    return 2000.0


def parse_calm_wind_tables(tlr_text):
    """
    Returns {'DRY': {'plus': int, 'base': {rwy: row}, 'upper': {rwy: row}}, 'WET': ...}

    A row is {'mtow_lb', 'mt', 'config', 'flp', 'v1', 'vr', 'v2', 'limit'}.
    MTOW in the table is in hundreds of pounds (1210 -> 121,000 lb).
    """
    if not tlr_text:
        return {}
    tables, surface, slot = {}, None, None
    for line in tlr_text.splitlines():
        h = _HDR_RE.search(line)
        if h:
            surface = h.group(1).upper()
            plus    = int(h.group(2) or 0)
            entry   = tables.setdefault(surface, {'plus': 0, 'base': {}, 'upper': {}})
            if plus:
                entry['plus'] = plus
                slot = 'upper'
            else:
                slot = 'base'
            continue
        if surface is None or slot is None:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        tables[surface][slot][m.group(1).upper()] = {
            'mtow_lb': int(m.group(2)) * 100,
            'mt':      int(m.group(3)),
            'config':  m.group(4).strip(),
            'flp':     m.group(5),
            'v1':      int(m.group(6)),
            'vr':      int(m.group(7)),
            'v2':      int(m.group(8)),
            'limit':   m.group(9).upper(),
        }
    return tables


def interpolate_to_atow(tables, surface, atow_delta_lb):
    """
    Calm-wind data at ATOW for every runway in the table.

    Numeric fields interpolate; CONFIG, FLP and LIMIT take the upper row,
    which is the heavier and therefore more conservative of the two.
    """
    if not tables:
        return {}, 0.0
    surf = (surface or 'DRY').upper()
    entry = tables.get(surf) or tables.get('DRY') or tables.get('WET')
    if not entry or not entry.get('base'):
        return {}, 0.0

    plus = entry.get('plus') or 0
    frac = (float(atow_delta_lb) / plus) if plus else 0.0
    frac = max(0.0, min(1.0, frac))

    out = {}
    for rwy, lo in entry['base'].items():
        hi = entry['upper'].get(rwy)
        if not hi:
            out[rwy] = dict(lo)
            continue
        def _ip(key):
            return int(round(lo[key] + frac * (hi[key] - lo[key])))
        out[rwy] = {
            'mtow_lb': _ip('mtow_lb'),
            'mt':      _ip('mt'),
            'v1':      _ip('v1'),
            'vr':      _ip('vr'),
            'v2':      _ip('v2'),
            'config':  hi['config'],
            'flp':     hi['flp'],
            'limit':   hi['limit'],
        }
    return out, frac
