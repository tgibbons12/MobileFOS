#!/bin/bash
# Double-click launcher for bulk_import_packs.py — pushes every bid-pack
# .txt file in data/PBS/ into the live Railway server's Pairing Library.
# Same command that's been run manually from Terminal, just double-clickable.
set -e
cd "$(dirname "$0")"
.venv/bin/python3 bulk_import_packs.py --url https://mobilefos-production.up.railway.app --username jetcentric
echo
read -p "Done. Press Return to close..."
