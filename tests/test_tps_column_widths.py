#!/usr/bin/env python3
"""The Airbus runway table is a fixed-column teletype block. This asserts
the columns actually line up, by re-evaluating the same format strings the
generator uses -- read out of write_tps_section.py rather than copied, so
this fails when someone edits the widths there and not here.

What it guards, all of which shipped broken at least once:
  * THR too narrow for "100.0", shoving every later column right by one on
    exactly the rows that reach three digits.
  * AT too narrow for MAX-TEMP/MAX-SPCL, printing "MAX-TEMP205.0S".
  * Non-standard-message rows using the non-Airbus layout, putting MTOW in
    a third column on the same table.
"""
import re, sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "write_tps_section.py"
SEP_WIDTH = 58

def _fmt(pattern, **kw):
    return eval("f" + repr(pattern).replace("\\n", ""), {}, kw)

def formats():
    """Pull the three Airbus row formats straight out of the generator."""
    src = SRC.read_text(encoding="utf-8")
    hdr = re.search(r'output \+= \(f"(\{\'RWY\'.*?)"\s*\n\s*f"(.*?)\\n"\)', src, re.S)
    row = re.search(r'output \+= \(f"(\{rwy:<6\}\{flap_fmt.*?)"\s*\n\s*f"(.*?)\\n"\) if is_airbus', src, re.S)
    msg = re.search(r'output \+= f"(\{rwy:<6\}\{flap_fmt:<6\}\{apu_status:<5\}\{rwy_message.*?)\\n"', src)
    assert hdr and row and msg, "could not find the Airbus formats in write_tps_section.py"
    return hdr.group(1) + hdr.group(2), row.group(1) + row.group(2), msg.group(1)

HDR, ROW, MSG = formats()

def header(thr="N1"):
    return _fmt(HDR, flap_label="CONF", ac_label="APU", thr_column_label=thr)

def data(rwy, flap, apu, thr, v1, vr, v2, at, mtow):
    return _fmt(ROW, rwy=rwy, flap_fmt=flap, apu_status=apu, thr_display=thr,
                v1=v1, vr=vr, v2=v2, at_display=at, mtow=mtow)

def message(rwy, flap, apu, m, mtow):
    return _fmt(MSG, rwy=rwy, flap_fmt=flap, apu_status=apu, rwy_message=m, mtow=mtow)

ROWS = [
    ("header N1",  header("N1")),
    ("header EPR", header("EPR")),
    ("N1 100.0",   data("23R","02","OFF","100.0","144","152","153","44C","205.0S")),
    ("N1 96.3",    data("01","03","ON","96.3","119","121","125","MAX-IMP","152.3X")),
    ("N1 103.0",   data("05L","02","ON","103.0","144","152","152","MAX-SPCL","200.4T")),
    ("EPR 1.50",   data("10LY","02","OFF","1.50","141","151","151","44C","184.6L")),
    ("MAX-TEMP",   data("23R","02*","OFF","100.0","144","152","153","MAX-TEMP","205.0S")),
    ("MAX-WT",     data("19","03","OFF","95.7","125","130","136","MAX-WT","142.6T")),
    ("PTOW",       message("05R","03","ON","PTOW EXCEEDS MTOW-RQST NEW TPS","179.8T")),
    ("ALL FLAPS",  message("23L","03","ON","PTOW EXCEEDS MTOW ALL FLAPS","184.7T")),
    ("FLAP N/A",   message("14","02","OFF","FLAP N/A THIS RWY-RQST NEW TPS","171.2L")),
]

def main():
    fails = []
    print("".join(str(i // 10 % 10) for i in range(60)))
    print("".join(str(i % 10) for i in range(60)))
    for _, line in ROWS:
        print(line)
    print()

    widths = {len(l) for _, l in ROWS}
    if len(widths) != 1:
        fails.append(f"rows are not all the same width: {sorted(widths)}")
    if max(widths) > SEP_WIDTH:
        fails.append(f"a row is wider than the {SEP_WIDTH}-char separator: {max(widths)}")

    # MTOW is the last field on every row and is right-aligned, so its end
    # column is simply the line length -- one number for the whole table.
    ends = {len(l) for _, l in ROWS}
    print(f"every row width          : {sorted(widths)}  (separator is {SEP_WIDTH})")
    print(f"MTOW right edge          : {sorted(ends)}")

    # No two adjacent fields may touch: the MAX-TEMP205.0S failure.
    for name, line in ROWS:
        if name.startswith("header"):
            continue
        body = line.rstrip()
        if re.search(r"[A-Z]\d+\.\d", body):
            fails.append(f"{name}: AT runs into MTOW -> {body[35:]!r}")

    # AT column must start in the same place on every non-message data row.
    at_cols = set()
    for name, line in ROWS:
        for tok in ("44C", "MAX-IMP", "MAX-SPCL", "MAX-TEMP", "MAX-WT"):
            i = line.find(tok)
            if i >= 0:
                at_cols.add(i)
                break
    print(f"AT column start          : {sorted(at_cols)}")
    if len(at_cols) != 1:
        fails.append(f"AT starts in more than one column: {sorted(at_cols)}")

    print()
    if fails:
        for f in fails:
            print("FAIL " + f)
        return 1
    print("PASS  every row type is the same width, inside the separator, "
          "with AT and MTOW each in one column.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
