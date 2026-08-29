#!/bin/bash
# Double-click launcher for upload_docs.py — publishes every PDF in
# data/DOCS/ to the live Railway server's Docs tab. Every pilot has to
# acknowledge each published document; re-uploading a file with the same
# name bumps its revision and clears those acknowledgements.
set -e
cd "$(dirname "$0")"
.venv/bin/python3 upload_docs.py --url https://mobilefos-production.up.railway.app --username jetcentric
echo
read -p "Done. Press Return to close..."
