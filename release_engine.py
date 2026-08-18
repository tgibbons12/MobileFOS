"""
Headless wrapper around MASTERLOG_FOS.generate_enhanced_howgozit().

MASTERLOG_FOS.py (and MASTERLOG.py, which it optionally imports for SLS field
reports) were written for interactive desktop use: they `import tkinter` at
module level and use simpledialog/filedialog for prompts. Railway's container
has no Tk/Tcl libs, so a bare `import MASTERLOG_FOS` dies before any of our
code runs. We install stub tkinter modules in sys.modules first so those
imports resolve to harmless no-ops; nothing in the code path we actually call
(generate_enhanced_howgozit with an explicit user_id + output_path) invokes
them for real — the interactive prompts are only reached from other entry
points (get_or_prompt_username, prompt_for_output_folder) that we never call.

generate_enhanced_howgozit() writes two PDFs into the directory implied by
`output_path` (filenames are derived from flight data, not from output_path's
basename) and returns parsed text/metadata, not paths or bytes. This module
points it at a throwaway temp dir, globs for the *-RLS.pdf / *-WB.pdf it
produced, reads them back as bytes, and cleans up.
"""

import glob
import os
import sys
import tempfile
import traceback
import types

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
# try/except inside MASTERLOG_FOS.py, so it fails safe on Linux without our
# help — no monkeypatch needed there.

_import_error = None
MASTERLOG_FOS = None
try:
    import MASTERLOG_FOS
except Exception as e:  # pragma: no cover - reported via /release/status
    _import_error = "".join(traceback.format_exception(type(e), e, e.__traceback__))


def is_available():
    """True if the release-generation import chain loaded cleanly."""
    return _import_error is None


def import_error():
    return _import_error


def generate_release_pdfs(user_id):
    """
    Run generate_enhanced_howgozit headlessly for the given SimBrief user_id.
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
            result = MASTERLOG_FOS.generate_enhanced_howgozit(user_id, output_path=placeholder)
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
