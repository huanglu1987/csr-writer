#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

python -m unittest discover -s tests -v
python tests/run_m3_e2e.py
python tests/build_module_expansion_evidence.py
python tests/run_m4_e2e.py
