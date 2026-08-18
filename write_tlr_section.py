"""
Stub. MASTERLOG.py imports write_tlr_section unconditionally at module
level, but only calls it when get_report_type() == "TLR" — an alternate
"ops release only, no separate W&B file" mode this app never sets (default
and only configured mode here is "TPS", handled by write_tps_section.py).
The real TLR-section generator doesn't exist anywhere in this codebase or
its siblings; this stub exists solely to satisfy the import so MASTERLOG's
newer write_field_reports() layout is reachable instead of MASTERLOG_FOS's
older local fallback. If report_type is ever set to "TLR", this will raise
rather than silently produce a blank section.
"""


def write_tlr_section(root):
    raise NotImplementedError(
        "write_tlr_section is a stub — TLR report mode isn't implemented, "
        "this app only supports the default TPS mode"
    )
