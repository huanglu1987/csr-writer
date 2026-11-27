"""Black-box coverage of raw discovery, evidence admissions and review-only delivery."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from csr_writer.cli import main
from csr_writer.datapack import build_datapack
from csr_writer.errors import ConfigurationError
from csr_writer.io_utils import atomic_write_json, load_data, sha256_file
from csr_writer.project_draft import draft_project
from csr_writer.tfl_map import build_tfl_map_candidate, freeze_tfl_map

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "examples/fixtures/m20_unfamiliar"


def make_result_inputs(root):
    """Simulated previously approved records for a single two-arm oncology study."""
    req = load_data(ROOT / "examples/section_inputs/efficacy_request.json")
    req["schema_version"] = "1.1"
    req["section_id"] = "11.6.1.1"
    req["tfl_references"] = ["T14.2.1"]
    locations = {}
    for docname, group in (("研究方案.docx", req["source_fragments"][:1]),
                           ("SAP.docx", req["source_fragments"][1:])):
        doc = Document()
        doc.add_heading("合成研究资料", 0)
        if docname.startswith("研究"):
            doc.add_heading("研究目标", 1)
        for i, frag in enumerate(group, 3 if docname.startswith("研究") else 2):
            doc.add_paragraph(frag["text"])
            locations[frag["source_id"]] = {
                "source": docname, "location": f"word/document.xml:/w:document/w:body/w:p[{i}]"}
        doc.save(root / docname)
    shutil.copyfile(ROOT / "examples/m4/group_contract.json", root / "group_contract.confirmed.json")
    source = "t14-2-1.rtf"
    shutil.copy2(ROOT.parent / "参考实现/fixture_t_14_2_1.rtf", root / source)
    manifest = load_data(ROOT / "examples/demo_tfl_manifest.yaml")
    manifest["sources"] = manifest["sources"][:1]
    manifest["sources"][0]["path"] = source
    atomic_write_json(root / "result_tfl_manifest.json", manifest)
    pack, _ = build_datapack(root / "result_tfl_manifest.json")
    cand = build_tfl_map_candidate(pack, {
        "schema_version": "1.0", "map_version": "synthetic-only",
        "numbering_system": {"id": "test", "description": "合成验证"},
        "routes": [{"entity_id": "T14.2.1", "ich_e3_sections": ["11.6.1.1"]}]})
    frozen = freeze_tfl_map(cand, {"status": "confirmed", "confirmation_id": "demo-only",
                                  "confirmed_by_role": "synthetic_fixture_reviewer", "confirmed_at": "2026-11-27T09:00:00+08:00",
                                  "candidate_sha256": cand["candidate_sha256"]})
    atomic_write_json(root / "frozen_tfl_map.json", frozen)
    atomic_write_json(root / "publication_reference_map.confirmed.json", {
        "schema_version": "1.0", "entries": [{"source_entity_id": "T14.2.1", "publication_entity_id": "T14.2-1",
                                             "title": "客观缓解率（合成演练）", "expected_section": "§11.6.1.1"}]})
    facts = load_data(next(MIG.glob("study_facts*")))
    facts.update(protocol_id="DEMO-ORR", study_title="合成演练：两组客观缓解率探索性研究")
    facts["design"].update(randomized=True, controlled=True, type="parallel", control_types=["placebo"],
                           blinding="double_blind", dose_levels=[{"value": 200, "unit": "mg"}])
    facts["population"].update(target_condition="晚期实体瘤", planned_sample_size=120,
                               inclusion_summary="成人晚期实体瘤受试者")
    facts["objectives"]["primary_endpoints"] = [{"id": "ORR", "name": "客观缓解率", "domain": "efficacy",
                                                "analysis_method": "按全分析集进行探索性组间比较"}]
    facts["objectives"]["secondary_endpoints"] = []
    facts["analyses"].update(descriptive_only=False, hypothesis_test=True, testing_strategy="探索性比较，P 值仅供描述")
    facts["data_domains"]["tumor_response"] = True
    facts["analyses"]["analysis_sets"] = [x for x in load_data(ROOT / "examples/demo_protocol_facts.json")["analyses"]["analysis_sets"] if x["id"] in {"FAS", "SAF"}]
    atomic_write_json(root / "study_facts.confirmed.json", facts)
    return {"request": req, "fragment_locations": locations}


class ProjectDraftTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.raw = self.base / "输入资料"
        self.raw.mkdir()

    def run_project(self, name="初稿", plan=None):
        return draft_project(self.raw, self.base / name, plan)

    def test_direct_from_raw_protocol_with_one_bad_file_and_patient_dataset(self):
        protocol = next(MIG.glob("protocol*.docx"))
        shutil.copy2(protocol, self.raw)
        (self.raw / "伦理批件.docx").write_bytes(b"bad document")
        (self.raw / "adsl.csv").write_text("subject,value\nTEST-DO-NOT-COPY,886688")
        destination = self.base / "初稿"
        self.assertEqual(0, main(["draft-project", "--input-dir", str(self.raw), "--output-dir", str(destination)]))
        report = load_data(destination / "draft-report.json")
        self.assertEqual("unreadable", report["sources"]["伦理批件.docx"]["status"])
        self.assertEqual("dataset", report["sources"]["adsl.csv"]["role"])
        self.assertEqual(sha256_file(protocol), sha256_file(self.raw / protocol.name))
        self.assertEqual(1, len(list(destination.glob("*.docx"))))
        doc = Document(destination / "CSR内部审阅初稿.docx")
        text = "\n".join(p.text for p in doc.paragraphs)
        codes = [node.text.strip() for node in doc._element.xpath(".//w:instrText")]
        self.assertIn(r"PAGEREF _Ref_Sec_8 \h", codes)
        footer = [node.text.strip() for node in doc.sections[-1].footer._element.xpath(".//w:instrText")]
        self.assertIn("PAGEREF _CSRReportEnd", footer)
        self.assertIn("不进行随机分配", text)
        self.assertNotIn("886688", text)
        self.assertIn("【待补】", text)
        self.assertTrue(set(str(n) for n in range(1, 17)) <= {r["section"] for r in report["sections"]})
        self.assertEqual(sha256_file(destination / "CSR内部审阅初稿.docx"), report["docx_sha256"])

    def test_two_column_protocol_summary_is_traced_without_copying_an_unrelated_row(self):
        d = Document()
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "研究目的"
        t.cell(0, 1).text = "评价方案规定的局部耐受性。"
        t.cell(1, 0).text = "备注"
        t.cell(1, 1).text = "会议纪要样例，不得进入方法。"
        d.save(self.raw / "研究方案.docx")
        result = self.run_project()
        section = next(s for s in result["sections"] if s["section"] == "8")
        self.assertEqual("source_excerpt", section["status"])
        self.assertIn("w:tc[2]", section["sources"][0]["location"])
        text = "\n".join(p.text for p in Document(self.base / "初稿/CSR内部审阅初稿.docx").paragraphs)
        self.assertIn("评价方案规定的局部耐受性。", text)
        self.assertNotIn("会议纪要样例", text)
        plan = load_data(self.base / "初稿/project-plan.json")
        plan["source_selections"] = {"9.1": [{"source": "研究方案.docx", "location": "missing"}]}
        atomic_write_json(self.base / "partial.json", plan)
        partial = self.run_project("部分失配", self.base / "partial.json")
        self.assertIn("§9.1 原文摘录未纳入", str(partial["review_items"]))
        self.assertEqual("source_excerpt", next(s for s in partial["sections"] if s["section"] == "8")["status"])

    def test_conflicting_versions_do_not_leak_and_stale_snapshot_is_rejected(self):
        (self.raw / "方案_v1.md").write_text("# 研究目标\n旧版目标。")
        (self.raw / "方案_v2.md").write_text("# 研究目标\n新版目标。")
        first = self.run_project()
        self.assertNotIn("protocol", first["selected_sources"])
        plan = load_data(self.base / "初稿/project-plan.json")
        plan["selected_sources"]["protocol"] = "方案_v2.md"
        location = self.base / "plan.json"
        atomic_write_json(location, plan)
        second = self.run_project("第二轮", location)
        self.assertEqual("方案_v2.md", second["selected_sources"]["protocol"])
        text = "\n".join(p.text for p in Document(self.base / "第二轮/CSR内部审阅初稿.docx").paragraphs)
        self.assertIn("新版目标。", text)
        self.assertNotIn("旧版目标。", text)
        (self.raw / "方案_v2.md").write_text("已经替换")
        with self.assertRaisesRegex(ConfigurationError, "版本已变化"):
            self.run_project("第三轮", location)
        self.assertFalse((self.base / "第三轮").exists())

    def test_frozen_result_from_original_rtf_renders_narrative_table_and_trace(self):
        job = make_result_inputs(self.raw)
        report = self.run_project()
        self.assertEqual(["T14.2.1"], next(r for r in report["sections"] if r["section"] == "11.6.1.1")["result_tables"])
        plan = load_data(self.base / "初稿/project-plan.json")
        plan["result_sections"] = [job]
        path = self.base / "plan.json"
        atomic_write_json(path, plan)
        report = self.run_project("续写", path)
        row = next(r for r in report["sections"] if r["section"] == "11.6.1.1")
        self.assertEqual("guarded_body", row["status"], report["review_items"])
        doc = Document(self.base / "续写/CSR内部审阅初稿.docx")
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("合成验收夹具", text)
        self.assertIn("例（", text)
        self.assertIn("Percentages are based", text)
        self.assertTrue(any("XYZ 200mg (N=60)" in [c.text for c in t.rows[0].cells] for t in doc.tables))
        trace = load_data(self.base / "续写/sections/result_sections-1/source-locations.json")
        self.assertEqual(6, len(trace["cells"]))
        for loc in trace["cells"].values():
            self.assertEqual("sha256:" + sha256_file(self.raw / "t14-2-1.rtf"), loc["source_id"])
        from copy import deepcopy
        swapped = deepcopy(job)
        for left, right in ((0, 1), (2, 3)):
            bindings = swapped["request"]["tfl_bindings"]
            bindings[left]["key"], bindings[right]["key"] = bindings[right]["key"], bindings[left]["key"]
        plan["result_sections"] = [swapped]
        atomic_write_json(path, plan)
        mismatch = self.run_project("组别互换", path)
        self.assertIn("组别与已确认列不符", str(mismatch["review_items"]))
        self.assertNotIn("guarded_body", mismatch["coverage"])
        job["request"]["source_fragments"][0]["text"] += "无此原文"
        plan["result_sections"] = [job]
        atomic_write_json(path, plan)
        bad = self.run_project("来源冲突", path)
        self.assertEqual(0, bad["coverage"].get("guarded_body", 0))
        self.assertIn("方法片段与原件不符", str(bad["review_items"]))

    def test_shell_queue_and_method_card_need_no_stage_pause(self):
        for pattern in ("protocol*.docx", "tfl_shell*.docx", "study_facts*.json"):
            shutil.copy2(next(MIG.glob(pattern)), self.raw)
        first = self.run_project()
        row = first["sources"][next(MIG.glob("tfl_shell*.docx")).name]
        self.assertTrue((self.base / "初稿" / row["mapping_queue"]).is_file())
        plan = load_data(self.base / "初稿/project-plan.json")
        plan["study_facts"] = next(MIG.glob("study_facts*.json")).name
        plan["fact_cards"] = [{"card": load_data(MIG / "method_fact_card.9.1.json"),
                              "draft": load_data(MIG / "method_draft.9.1.retry1.json"), "evidence_status": "demo_only"}]
        atomic_write_json(self.base / "续写.json", plan)
        result = self.run_project("第二轮", self.base / "续写.json")
        self.assertEqual("guarded_body", next(x for x in result["sections"] if x["section"] == "9.1")["status"],
                         result["review_items"])

    def test_inline_records_bind_to_original_text_locations(self):
        source = "统计核对记录.txt"
        (self.raw / source).write_text("合成资料，仅供验证。\n参考编号见研究资料。")
        self.run_project()
        plan = load_data(self.base / "初稿/project-plan.json")
        plan["publication_map"] = "@编号"
        plan["prepared_records"] = {"@编号": {
            "value": {"schema_version": "1.0", "entries": [{"source_entity_id": "T14.2.1",
                      "publication_entity_id": "T14.2-1", "title": "合成演练", "expected_section": "§11.6.1.1"}]},
            "basis": [{"source": source, "location": "line:2"}]}}
        p = self.base / "计划.json"
        atomic_write_json(p, plan)
        self.assertNotIn("本轮发表编号未采用", str(self.run_project("正确位置", p)["review_items"]))
        plan["prepared_records"]["@编号"]["basis"][0]["location"] = "line:200"
        atomic_write_json(p, plan)
        self.assertIn("未见于所提取原件", str(self.run_project("错误位置", p)["review_items"]))

    def test_refuses_overwrite_nested_output_and_source_drift(self):
        source = self.raw / "方案.txt"
        source.write_text("研究设计\n固定给药。")
        with self.assertRaises(ConfigurationError):
            draft_project(self.raw, self.raw / "输出")
        self.run_project()
        with self.assertRaises(ConfigurationError):
            self.run_project()
        from csr_writer import project_draft
        read = project_draft.read_sources

        def change(*args):
            result = read(*args)
            source.write_text("另一个版本")
            return result

        with patch.object(project_draft, "read_sources", side_effect=change):
            with self.assertRaisesRegex(ConfigurationError, "期间原始资料发生变化"):
                self.run_project("改变")
        self.assertFalse((self.base / "改变").exists())
