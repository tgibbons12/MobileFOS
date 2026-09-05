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
import importlib
import inspect
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
        """Accepts anything Tk would.

        Listing methods one at a time only works until a generator reaches
        for one that is not there: JetPlan's save_as_pdf calls .title() on
        the root window, and the AttributeError was swallowed by that
        function's own except, so the release came back missing its PDF with
        nothing to say why. Anything unknown is a no-op that returns
        another no-op, so chained calls are safe too."""

        def __getattr__(self, _name):
            return _StubTk._noop

        @staticmethod
        def _noop(*_a, **_k):
            return _StubTk()

        def __bool__(self):
            return False

    tkinter_stub.Tk = _StubTk
    tkinter_stub.Toplevel = _StubTk
    tkinter_stub.StringVar = _StubTk
    tkinter_stub.IntVar = _StubTk

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


# ---------------------------------------------------------------------------
# OFP formats
#
# Three generators, each producing a different release layout from the same
# SimBrief XML:
#
#   masterlog  MASTERLOG.py          the standard release
#   jetplan    MASTERLOG_JetPlan.py  Jeppesen-style: its own page 1 ahead of
#                                    the JetPlan OFP, nav log as TIME/DIST/FUEL
#   fos        MASTERLOG_FOS.py      the older FOS layout; no operator picks it
#                                    by default, it is a Settings choice only
#
# Imported lazily and cached: MASTERLOG_JetPlan pulls in write_tlr_section at
# module level, so importing it eagerly would make a missing TLR renderer
# break plain MASTERLOG releases too.
# ---------------------------------------------------------------------------
OFP_FORMATS = {
    "masterlog": "MASTERLOG",
    "jetplan": "MASTERLOG_JetPlan",
    "fos": "MASTERLOG_FOS",
}
# What the pilot sees. The keys and module names stay as they are —
# MASTERLOG is what the file is called and what the logs say — but the
# standard release is FlightKeys to anyone using it.
OFP_FORMAT_LABELS = {
    "masterlog": "FLIGHTKEYS (AA)",
    "jetplan": "JetPlan",
    "fos": "FOS",
}
_format_cache = {}


def load_format(name):
    """The generator module for `name`, or raise with something readable.

    A missing write_tlr_section is the expected failure here — JetPlan calls
    it unconditionally — so it is named rather than surfaced as an import
    traceback about a file nobody asked about.
    """
    name = (name or "masterlog").strip().lower()
    if name not in OFP_FORMATS:
        raise RuntimeError(f"unknown OFP format {name!r}")
    if name in _format_cache:
        mod, err = _format_cache[name]
        if err:
            raise RuntimeError(err)
        return mod
    if name == "masterlog":
        if _import_error:
            _format_cache[name] = (None, f"release generation unavailable: {_import_error}")
            raise RuntimeError(_format_cache[name][1])
        _format_cache[name] = (MASTERLOG, None)
        return MASTERLOG
    try:
        mod = importlib.import_module(OFP_FORMATS[name])
    except NotImplementedError as e:
        msg = (f"{OFP_FORMAT_LABELS[name]} needs the TLR generator, which is not "
               f"installed here (write_tlr_section.py is a stub): {e}")
        _format_cache[name] = (None, msg)
        raise RuntimeError(msg)
    except Exception as e:
        msg = f"{OFP_FORMAT_LABELS[name]} could not be loaded: {e}"
        _format_cache[name] = (None, msg)
        raise RuntimeError(msg)
    _format_cache[name] = (mod, None)
    return mod


def format_status():
    """Which formats can actually run, for Settings and /release/status."""
    out = {}
    for name in OFP_FORMATS:
        try:
            load_format(name)
            out[name] = {"label": OFP_FORMAT_LABELS[name], "available": True, "error": ""}
        except RuntimeError as e:
            out[name] = {"label": OFP_FORMAT_LABELS[name], "available": False, "error": str(e)}
    return out


def is_available():
    """True if the release-generation import chain loaded cleanly."""
    return _import_error is None


def import_error():
    return _import_error


def generate_release_pdfs(user_id, gate="", arr_gate="", generation=0,
                          ofp_format="masterlog", report_type=""):
    """
    Run generate_enhanced_howgozit headlessly for the given SimBrief user_id.
    gate/arr_gate (if known — e.g. from an AeroAPI suggestion applied to the
    FOS leg) override the DECS pages' synthesized placeholder gates.
    generation is this leg's regeneration count and becomes the digit after
    the point in the page header's "RELEASE 6.7" — see the caller in
    server.py for where it is kept.
    Returns (rls_bytes, wb_bytes_or_None, base_filename).
    Raises RuntimeError with a human-readable message on any failure.
    """
    generator = load_format(ofp_format)

    # report_type ("TPS"/"TLR") normally comes from a per-script .config file
    # the desktop app writes. Here it is a per-flight decision — an EMY leg
    # wants TLR from the same generator a NAC leg wants TPS from — so the
    # module's own reader is overridden for the duration of this call rather
    # than writing a config file the next request would race on.
    _restore = None
    if report_type and hasattr(generator, "get_report_type"):
        _restore = generator.get_report_type
        generator.get_report_type = lambda _rt=report_type.strip().upper(): _rt

    if not user_id:
        raise RuntimeError("no SimBrief user id given (set SIMBRIEF_USER or pass user_id)")

    try:
        return _generate_with(generator, user_id, gate, arr_gate, generation, ofp_format)
    finally:
        if _restore is not None:
            generator.get_report_type = _restore


def _generate_with(generator, user_id, gate, arr_gate, generation, ofp_format):
    with tempfile.TemporaryDirectory(prefix="fos-release-") as tmpdir:
        placeholder = os.path.join(tmpdir, "placeholder.pdf")
        try:
            # The generators do not share a signature: MASTERLOG takes
            # gate/arr_gate/generation, the JetPlan and FOS layouts take
            # only (user_id, output_path). Pass what each one actually
            # accepts rather than assuming — a mismatch here would read as
            # a TypeError from deep inside a 280KB file.
            _kwargs = {"output_path": placeholder}
            _accepts = inspect.signature(generator.generate_enhanced_howgozit).parameters
            for _k, _v in (("gate", gate), ("arr_gate", arr_gate), ("generation", generation)):
                if _k in _accepts:
                    _kwargs[_k] = _v
            result = generator.generate_enhanced_howgozit(user_id, **_kwargs)
        except SystemExit as e:
            raise RuntimeError(f"generate_enhanced_howgozit exited: {e}")
        except NotImplementedError as e:
            # write_tlr_section is a stub here, and JetPlan calls it
            # unconditionally. Importing the module succeeds, so this only
            # shows up mid-generation — say what is actually missing rather
            # than surfacing a bare NotImplementedError.
            raise RuntimeError(
                f"{OFP_FORMAT_LABELS.get(ofp_format, ofp_format)} needs the TLR "
                f"generator, which is not installed here — write_tlr_section.py "
                f"is a stub ({e})")
        except Exception as e:
            raise RuntimeError(f"generate_enhanced_howgozit raised: {e}")

        if result is None:
            raise RuntimeError(
                "generate_enhanced_howgozit returned None — usually a bad "
                "SimBrief user id or no pending OFP for that user; check server logs"
            )

        rls_files = sorted(glob.glob(os.path.join(tmpdir, "*-RLS.pdf")))
        # The companion document is named for what it holds: -WB.pdf in TPS
        # mode, -TLR.pdf in TLR mode. Globbing only for -WB.pdf meant a
        # JetPlan release silently came back without its TLR — the very
        # document that mode exists to produce.
        wb_files = (sorted(glob.glob(os.path.join(tmpdir, "*-WB.pdf")))
                    or sorted(glob.glob(os.path.join(tmpdir, "*-TLR.pdf"))))
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
      weather        — station METAR/TAF blocks; identified by the literal
                        "METAR"/"TAF" category banner text every station
                        block renders, even when NIL.
      notams         — real NOTAM records, identified by "CREATED:" (every
                        individual NOTAM carries a created timestamp) or
                        "UFN" (until-further-notice, in the effective-date
                        range). All matching pages combined, in document
                        order.
      field_report   — MASTERLOG.write_field_reports()'s "* <STA> FIELD
                        REPORT *" header, one per origin/destination
                        station. All matches combined the same way.

    First pass at this (2026-08-19) assumed weather/notams both used
    _airport_notam_header()'s '=' * 72 banner as literal text, matching
    MASTERLOG's source. Verified 2026-08-20 against a real generated
    release: that banner (however MASTERLOG draws it) doesn't actually
    show up in pypdf's extract_text() output at all — every real NOTAMs
    page came back completely unmatched. "CREATED:"/"UFN" above are real
    markers confirmed against that output, not source-inferred. The '='
    banner check stays as a fallback (harmless — it only ever fires after
    the notams check above it misses) in case some other release variant
    does render literal '=' text.

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
        # 1000 chars, not 600 — "CREATED:" (the notams marker below) can
        # sit past 600 on a page whose NOTAM number/date-range text runs
        # long before it.
        text = (page.extract_text() or "")[:1000]
        if re.search(r'\bFIL\d', text) and single["fil"] is None:
            single["fil"] = page
            page_log.append(f"page {i}: fil")
        elif re.search(r'\bFI\d', text) and single["fi"] is None:
            single["fi"] = page
            page_log.append(f"page {i}: fi")
        elif re.search(r'\bMETAR\b|\bTAF\b', text):
            multi["weather"].append(page)
            page_log.append(f"page {i}: weather")
        elif re.search(r'\bCREATED:|\bUFN\b', text) or re.search(r'={20,}', text):
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
