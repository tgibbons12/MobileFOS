"""
Headless wrapper around MASTERLOG.generate_enhanced_howgozit().

Uses MASTERLOG.py, not MASTERLOG_FOS.py — the two are near-identical (same
entry point name/signature, same fetch → parse → split-into-RLS/WB flow),
but MASTERLOG.py is the newer generation: structured logging instead of
print(), a release watermark, a prepended WBD/index page, and it calls
write_field_reports() directly instead of needing a cross-module import
(MASTERLOG_FOS.py needs `from MASTERLOG import write_field_reports`, which
was silently failing on a missing write_tlr_section — calling MASTERLOG.py
directly sidesteps that entirely, no stub needed for this path).

MASTERLOG.py was written for interactive desktop use: it `import tkinter`s
at module level and uses simpledialog/filedialog for prompts. Railway's
container has no Tk/Tcl libs, so a bare `import MASTERLOG` dies before any
of our code runs. We install stub tkinter modules in sys.modules first so
those imports resolve to harmless no-ops; nothing in the code path we
actually call (generate_enhanced_howgozit with an explicit user_id +
output_path) invokes them for real — the interactive prompts are only
reached from other entry points (get_or_prompt_username,
prompt_for_output_folder) that we never call.

generate_enhanced_howgozit() writes two PDFs into the directory implied by
`output_path` (filenames are derived from flight data, not from output_path's
basename) and returns parsed text/metadata, not paths or bytes. This module
points it at a throwaway temp dir, globs for the *-RLS.pdf / *-WB.pdf it
produced, reads them back as bytes, and cleans up.
"""

import glob
import io
import logging
import os
import sys
import tempfile
import traceback
import types

LOG = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def _install_tkinter_stub():
    if "tkinter" in sys.modules:
        return

    tkinter_stub = types.ModuleType("tkinter")

    class _StubTk:
        def withdraw(self):
            pass

        def destroy(self):
            pass

    tkinter_stub.Tk = _StubTk

    simpledialog_stub = types.ModuleType("tkinter.simpledialog")
    simpledialog_stub.askstring = lambda *a, **k: None

    messagebox_stub = types.ModuleType("tkinter.messagebox")
    messagebox_stub.showinfo = lambda *a, **k: None
    messagebox_stub.showerror = lambda *a, **k: None
    messagebox_stub.showwarning = lambda *a, **k: None

    filedialog_stub = types.ModuleType("tkinter.filedialog")
    filedialog_stub.askdirectory = lambda *a, **k: None

    tkinter_stub.simpledialog = simpledialog_stub
    tkinter_stub.messagebox = messagebox_stub
    tkinter_stub.filedialog = filedialog_stub

    sys.modules["tkinter"] = tkinter_stub
    sys.modules["tkinter.simpledialog"] = simpledialog_stub
    sys.modules["tkinter.messagebox"] = messagebox_stub
    sys.modules["tkinter.filedialog"] = filedialog_stub


_install_tkinter_stub()

# Auto-open (subprocess.run(['open', path])) is already wrapped in a
# try/except inside MASTERLOG.py, so it fails safe on Linux without our
# help — no monkeypatch needed there.

_import_error = None
MASTERLOG = None
try:
    import MASTERLOG
except Exception as e:  # pragma: no cover - reported via /release/status
    _import_error = "".join(traceback.format_exception(type(e), e, e.__traceback__))


def is_available():
    """True if the release-generation import chain loaded cleanly."""
    return _import_error is None


def import_error():
    return _import_error


def generate_release_pdfs(user_id, gate="", arr_gate=""):
    """
    Run generate_enhanced_howgozit headlessly for the given SimBrief user_id.
    gate/arr_gate (if known — e.g. from an AeroAPI suggestion applied to the
    FOS leg) override the DECS pages' synthesized placeholder gates.
    Returns (rls_bytes, wb_bytes_or_None, base_filename).
    Raises RuntimeError with a human-readable message on any failure.
    """
    if not is_available():
        raise RuntimeError(f"release generation unavailable: {_import_error}")

    if not user_id:
        raise RuntimeError("no SimBrief user id given (set SIMBRIEF_USER or pass user_id)")

    with tempfile.TemporaryDirectory(prefix="fos-release-") as tmpdir:
        placeholder = os.path.join(tmpdir, "placeholder.pdf")
        try:
            result = MASTERLOG.generate_enhanced_howgozit(user_id, output_path=placeholder, gate=gate, arr_gate=arr_gate)
        except SystemExit as e:
            raise RuntimeError(f"generate_enhanced_howgozit exited: {e}")
        except Exception as e:
            raise RuntimeError(f"generate_enhanced_howgozit raised: {e}")

        if result is None:
            raise RuntimeError(
                "generate_enhanced_howgozit returned None — usually a bad "
                "SimBrief user id or no pending OFP for that user; check server logs"
            )

        rls_files = sorted(glob.glob(os.path.join(tmpdir, "*-RLS.pdf")))
        wb_files = sorted(glob.glob(os.path.join(tmpdir, "*-WB.pdf")))
        if not rls_files:
            raise RuntimeError("generate_enhanced_howgozit ran but produced no -RLS.pdf")

        with open(rls_files[-1], "rb") as f:
            rls_bytes = f.read()
        wb_bytes = None
        if wb_files:
            with open(wb_files[-1], "rb") as f:
                wb_bytes = f.read()

        return rls_bytes, wb_bytes, os.path.basename(rls_files[-1])


def extract_named_pages(rls_bytes):
    """
    Pull specific pages out of the full release PDF as their own small PDFs,
    so the Documents view can show just the relevant part instead of the
    whole release. Each kind is found by a distinctive text marker rather
    than a fixed page number, since how many pages precede it (OFP body,
    weather, NOTAMs, DECS pages) varies leg to leg:

      fi / fil       — fos_pages.build_fi_page()/build_fil_page()'s header
                        lines ("FI<flt>/..." / "FIL<flt>/..."). First match
                        only — each is always exactly one page.
      weather        — MASTERLOG's weather section (_apt_header + a
                        _cat_banner("TAF")/_cat_banner("METAR") per station,
                        rendered right before NOTAMs "in NOTAM-compatible
                        token format"). All matching pages combined, in
                        document order.
      notams         — MASTERLOG._airport_notam_header()'s banner
                        ('=' * 72 rules around each airport block). All
                        matching pages are combined into one PDF, in
                        document order (dep/dest/alternate/enroute).
      field_report   — MASTERLOG.write_field_reports()'s "* <STA> FIELD
                        REPORT *" header, one per origin/destination
                        station. All matches combined the same way.

    weather and notams share the exact same '=' * 72 airport-block banner
    by design (_apt_header / _airport_notam_header both "match OFP badge
    style") — that banner alone can't tell them apart. What does: every
    weather station block always renders a "TAF"/"METAR" category banner
    right after its header (_cat_banner), even when the underlying report
    is NIL, so checking for that text first (before falling through to the
    bare '=' banner check) is what keeps weather pages out of the notams
    bucket instead of silently merged into it.

    Not independently verified against a real generated release from this
    app (needs a live SimBrief OFP this environment doesn't have) — if a
    kind turns out empty or wrong, that's a marker-pattern fix here, not a
    sign the underlying page generation is broken.

    Returns {"fi": bytes|None, "fil": bytes|None, "weather": bytes|None,
    "notams": bytes|None, "field_report": bytes|None}; a miss just means
    that page wasn't found, not an error — callers treat it as "not
    available", not fatal.
    """
    import re
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(rls_bytes))
    single = {"fi": None, "fil": None}
    multi = {"weather": [], "notams": [], "field_report": []}
    # Diagnostic only — this classification is inferred from MASTERLOG's
    # source, not verified against a real generated release (see docstring
    # above). Logging what every page actually matched (or didn't) means
    # the next time a kind comes back empty/wrong, the real page text is
    # right there in the logs instead of needing another guess.
    page_log = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "")[:600]
        if re.search(r'\bFIL\d', text) and single["fil"] is None:
            single["fil"] = page
            page_log.append(f"page {i}: fil")
        elif re.search(r'\bFI\d', text) and single["fi"] is None:
            single["fi"] = page
            page_log.append(f"page {i}: fi")
        elif re.search(r'\bMETAR\b|\bTAF\b', text):
            multi["weather"].append(page)
            page_log.append(f"page {i}: weather")
        elif re.search(r'={20,}', text):
            multi["notams"].append(page)
            page_log.append(f"page {i}: notams")
        elif re.search(r'FIELD REPORT', text):
            multi["field_report"].append(page)
            page_log.append(f"page {i}: field_report")
        else:
            page_log.append(f"page {i}: UNMATCHED — {text[:120]!r}")
    LOG.info("extract_named_pages: %d pages — %s", len(reader.pages), "; ".join(page_log))

    def _pdf_bytes(pages):
        if not pages:
            return None
        writer = PdfWriter()
        for p in pages:
            writer.add_page(p)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    found = {kind: (_pdf_bytes([p]) if p is not None else None) for kind, p in single.items()}
    found.update({kind: _pdf_bytes(pages) for kind, pages in multi.items()})
    return found
