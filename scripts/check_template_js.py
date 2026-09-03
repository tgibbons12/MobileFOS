#!/usr/bin/env python3
"""Static checks on the JS embedded in server.py's two page templates.

`node --check` validates syntax only. It cannot see that a function called
in LAUNCHER_TEMPLATE is defined in FOS_TEMPLATE — the two are separate
documents sharing no scope — and that mistake has now shipped three times
(_setPdfActions, generation, _dayCalendarNumber). This checks each
template as its own world:

  1. every inline <script> parses (node --check)
  2. every onclick/onchange/oninput/onsubmit handler is defined in that
     same template
  3. every app-looking function called is defined in that same template

Run before any push:  .venv/bin/python scripts/check_template_js.py
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Anything the browser, the language, or a loaded library provides. Names
# here are *not* expected to be defined in the template itself.
KNOWN = set("""
if for while switch catch return typeof function new delete void throw case do else
Array Object String Number Boolean Math JSON Date RegExp Map Set Promise Error
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent encodeURI
decodeURI setTimeout setInterval clearTimeout clearInterval requestAnimationFrame
fetch alert confirm prompt console document window navigator location history
localStorage sessionStorage FormData FileReader Blob URL Intl Symbol BigInt Proxy
addEventListener removeEventListener querySelector querySelectorAll getElementById
createElement appendChild String raw structuredClone AbortController Headers
Request Response TextEncoder TextDecoder atob btoa CustomEvent Event MutationObserver
IntersectionObserver ResizeObserver DOMParser XMLHttpRequest WebSocket Worker
performance crypto matchMedia getComputedStyle scrollTo scrollBy print open close
""".split())

def strip_comments(js):
    """Drop comment text, line by line, so prose in comments is not read as
    code. Line-scoped on purpose: a mis-detection costs one line rather
    than cascading to the end of the file the way a naive string/regex
    scanner does. A "//" is only a comment start when it is not part of a
    URL and not inside a quoted string on that line."""
    out, in_block = [], False
    for line in js.split("\n"):
        if in_block:
            end = line.find("*/")
            if end == -1:
                out.append("")
                continue
            line, in_block = line[end + 2:], False
        start = line.find("/*")
        if start != -1:
            end = line.find("*/", start + 2)
            if end == -1:
                line, in_block = line[:start], True
            else:
                line = line[:start] + line[end + 2:]
        i = 0
        while True:
            i = line.find("//", i)
            if i == -1:
                break
            if i and line[i - 1] == ":":          # https://
                i += 2
                continue
            quotes = sum(line[:i].count(q) for q in "\"'`")
            if quotes % 2:                         # inside a string
                i += 2
                continue
            line = line[:i]
            break
        out.append(line)
    return "\n".join(out)


def strip_noise(js):
    """Remove comments and string/template literals.

    Without this the scan reads English prose — "…placed in (the day)…" —
    as a call to in(), and CSS inside a style string as a call to rgba().
    """
    out, i, n = [], 0, len(js)
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            i = js.find("\n", i)
            if i == -1:
                break
        elif c == "/" and nxt == "*":
            i = js.find("*/", i)
            i = n if i == -1 else i + 2
        elif c in "\"'`":
            quote, i = c, i + 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
        else:
            out.append(c)
            i += 1
    return "".join(out)


DEF_RE = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)')
ASSIGN_RE = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()')
BIND_RE = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)')
CALL_RE = re.compile(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(')
HANDLER_RE = re.compile(r'on(?:click|change|input|submit)="([A-Za-z_$][\w$]*)\(')
PARAMS_RE = re.compile(r'(?:function\s*[A-Za-z_$\w]*\s*\(|\(\s*)([^()]*?)\)\s*(?:=>|\{)')
CATCH_RE = re.compile(r'catch\s*\(\s*([A-Za-z_$][\w$]*)')
ARROW_RE = re.compile(r'([A-Za-z_$][\w$]*)\s*=>')
# Leading underscore required: this codebase names its module-level
# constants _DDMMMYY_MONTHS, _REC_KINDS, _MSG_FIELD. Without it the
# scan reads bare words inside string literals — 'POST', 'LIFR',
# 'TAFB' — as undefined constants.
CONST_REF_RE = re.compile(r'(?<![.\w$])(_[A-Z][A-Z0-9_]{2,})(?![\w$])')
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def main():
    import server  # noqa: E402  (imported late so sys.path is set)

    ok = True
    calls, defs, consts = {}, {}, {}
    tmp = tempfile.mkdtemp(prefix="tplcheck-")
    for name in ("LAUNCHER_TEMPLATE", "FOS_TEMPLATE"):
        tpl = getattr(server, name)
        blocks = SCRIPT_RE.findall(tpl)
        for i, block in enumerate(blocks):
            path = os.path.join(tmp, f"{name}_{i}.js")
            with open(path, "w") as fh:
                fh.write(block)
            proc = subprocess.run(["node", "--check", path],
                                  capture_output=True, text=True)
            if proc.returncode:
                ok = False
                print(f"SYNTAX  {name} block {i}:\n{proc.stderr[:600]}")

        raw = "\n".join(blocks)
        # Definitions are read from the raw source: stripping is lossy
        # around regex literals (/[&<>"']/g opens a "string" and swallows
        # the code after it), and a definition lost that way reads as a
        # missing function. Calls are read from the stripped source, where
        # that same loss only costs a check rather than inventing one.
        js = strip_noise(raw)
        defined = set(DEF_RE.findall(raw)) | set(ASSIGN_RE.findall(raw)) | set(BIND_RE.findall(raw))
        # Parameters and catch bindings are local names, not app functions.
        for params in PARAMS_RE.findall(raw):
            for part in params.split(","):
                m = re.match(r'\s*([A-Za-z_$][\w$]*)', part)
                if m:
                    defined.add(m.group(1))
        defined |= set(CATCH_RE.findall(raw)) | set(ARROW_RE.findall(raw))

        for handler in sorted(set(HANDLER_RE.findall(tpl))):
            if handler not in defined:
                ok = False
                print(f"HANDLER {name}: on*=\"{handler}(\" is not defined in this template")

        # Calls are read from the raw source too. Stripping was meant to
        # keep prose out of this set, but it is lossy around regex
        # literals — one /[&<>"']/g swallows every line after it, which
        # silently blinded the cross-template check below. The
        # "defined in the other template" filter removes prose on its own:
        # a word from a comment is not a function name over there.
        body = strip_comments(raw)
        calls[name] = set(CALL_RE.findall(body))
        # Constants are referenced, not called, so the call scan never sees
        # them: deleting _DDMMMYY_MONTHS left isoDateToDDMMMYY throwing a
        # ReferenceError that nothing reported, because an async handler
        # turns a throw into a silent rejected promise. SCREAMING_SNAKE
        # names are unambiguous enough to check on their own.
        consts[name] = {m for m in CONST_REF_RE.findall(body) if m not in KNOWN}
        defs[name] = defined

    # The bug this exists for: a name called in one template and defined
    # only in the other. Checking "defined nowhere" instead drowns in
    # false positives — prose inside comments, regex literals, DOM and
    # library globals — and a name that is genuinely defined in neither is
    # almost always one of those, not a real call.
    for name in ("LAUNCHER_TEMPLATE", "FOS_TEMPLATE"):
        for const in sorted(consts[name] - defs[name]):
            ok = False
            print(f"UNDEF   {name}: {const} is referenced but defined nowhere "
                  f"in this template")

    for name, other in (("LAUNCHER_TEMPLATE", "FOS_TEMPLATE"),
                        ("FOS_TEMPLATE", "LAUNCHER_TEMPLATE")):
        for called in sorted(calls[name] - defs[name] - KNOWN):
            # Only names shaped like this codebase's functions: a leading
            # underscore or an internal capital. Every real one has that
            # (_dayCalendarNumber, loadSequences, showTab); the words that
            # leak in from prose in comments — "state", "destination",
            # "of" — have neither.
            if not re.search(r"[A-Z_]", called):
                continue
            if called in defs[other]:
                ok = False
                print(f"CROSS   {name}: {called}() is called here but defined only "
                      f"in {other} — the templates are separate documents")

    print("JS OK" if ok else "JS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
