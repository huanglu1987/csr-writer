"""Replay the public tutorial fixture and inspect the saved Word, using only this checkout."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from docx import Document
from docx.oxml.ns import qn


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "antigravity-demo"


def report(folder, relative):
    return json.loads((folder / relative).read_text(encoding="utf-8"))


def word_sections(path):
    sections = {}
    section = ""
    for paragraph in Document(path).paragraphs:
        for bookmark in paragraph._p.findall(qn("w:bookmarkStart")):
            name = bookmark.get(qn("w:name"), "")
            if name.startswith("_Ref_Sec_"):
                section = name.removeprefix("_Ref_Sec_").replace("_", ".")
        sections.setdefault(section, []).append(paragraph.text)
    return {key: "\n".join(lines) for key, lines in sections.items()}


class AntigravityQuickstartTest(unittest.TestCase):
    def test_folder_and_one_batch_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            inputs = project / "输入资料"
            shutil.copytree(DEMO / "输入资料", inputs)
            first, second = project / "首稿", project / "答复后"
            for folder, options in ((first, []), (second, ["--answers", str(DEMO / "模拟答复.json")])):
                run = subprocess.run(
                    [sys.executable, "-m", "csr_writer", "draft-project", "--input-dir", str(inputs),
                     "--output-dir", str(folder), *options],
                    cwd=ROOT, capture_output=True, text=True, timeout=90
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                tables = {item["number"]: item for item in report(folder, "统计分析/逐表对账.json")}
                self.assertEqual(tables["1.1"]["status"], "采用定稿表")
                self.assertEqual(tables["3.1"]["status"], "逐例补算")
                diff = tables["2.1"]["external"][0]["differences"]
                self.assertEqual([(d["external_cell"], d["external"], d["calculated"]) for d in diff],
                                 [("暴露!C5", "4", "3")])
                self.assertEqual(tables["2.1"]["status"], "差异待裁定" if folder == first else "采用逐例计算")
                printed = report(folder, "统计分析/Word单元格复核.json")
                self.assertEqual(len(printed), 6 if folder == first else 7)
                self.assertTrue(all(t["word_matches_source"] for t in printed))
            initial = report(first, "draft-report.json")
            revised = report(second, "draft-report.json")
            self.assertEqual(initial["source_snapshot"], revised["source_snapshot"])
            self.assertEqual(revised["decision_summary"], {"mode": "simulation", "answered": 2, "outstanding": 3})
            sections = word_sections(second / "CSR内部审阅初稿.docx")
            for section in ("2", "12.1", "14.3"):
                self.assertIn("甲组 3 人日", sections[section])
            for section in ("2", "12.2.3", "14.3.1"):
                self.assertIn("甲组 0/3", sections[section])
