#!/bin/sh
set -eu

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
workspace_root=$(CDPATH= cd -- "$plugin_root/.." && pwd)
qa_root=${QA_ROOT:-$(mktemp -d /private/tmp/csr-m1m2-qa.XXXXXX)}
python_bin=${PYTHON_BIN:-python3}

mkdir -p "$qa_root"
cd "$plugin_root"

"$python_bin" -m unittest discover -s tests -v
"$python_bin" -m csr_writer create-template \
  --output "$qa_root/csr-template-zh.docx"
"$python_bin" -m csr_writer render-skeleton \
  --template "$qa_root/csr-template-zh.docx" \
  --output "$qa_root/CSR_ICH_E3_骨架示例.docx" \
  --manifest-output "$qa_root/skeleton_manifest.json"
"$python_bin" -m csr_writer qc-format \
  --docx "$qa_root/CSR_ICH_E3_骨架示例.docx" \
  --report "$qa_root/qc_format_report.json" \
  > "$qa_root/qc_format_report.txt"
"$python_bin" "$workspace_root/参考实现/poc4_docx_qc.py" \
  "$qa_root/CSR_ICH_E3_骨架示例.docx" \
  > "$qa_root/poc4_qc_report.txt"

"$python_bin" -m csr_writer build-datapack \
  --manifest examples/demo_tfl_manifest.yaml \
  --output "$qa_root/datapack.first.json" \
  --parsed-output "$qa_root/parsed_tables.json"
"$python_bin" -m csr_writer build-datapack \
  --manifest examples/demo_tfl_manifest.yaml \
  --output "$qa_root/datapack.second.json"
cmp -s "$qa_root/datapack.first.json" "$qa_root/datapack.second.json"

"$python_bin" -m csr_writer resolve \
  --draft examples/demo_draft.md \
  --datapack "$qa_root/datapack.first.json" \
  --protocol-facts examples/demo_protocol_facts.json \
  --output "$qa_root/resolved.txt" \
  --report "$qa_root/guard_report.json"

"$python_bin" - "$workspace_root" "$qa_root" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
qa = Path(sys.argv[2])
fixtures = [
    workspace / "参考实现" / "fixture_t_14_2_1.rtf",
    workspace / "参考实现" / "fixture_t_pk_nca.rtf",
]
current = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixtures}
datapack = json.loads((qa / "datapack.first.json").read_text(encoding="utf-8"))
recorded = {
    item["filename"]: item["source_id"].removeprefix("sha256:")
    for item in datapack["sources"]
}
if current != recorded:
    raise SystemExit(f"fixture hash mismatch: current={current}, recorded={recorded}")
qc = json.loads((qa / "qc_format_report.json").read_text(encoding="utf-8"))
guard = json.loads((qa / "guard_report.json").read_text(encoding="utf-8"))
if qc["pass_count"] != 21 or qc["fail_count"] != 0:
    raise SystemExit(f"QC failed: {qc}")
if guard["status"] != "pass":
    raise SystemExit(f"guard failed: {guard}")
print(f"M1+M2 e2e PASS: {qa}")
PY

