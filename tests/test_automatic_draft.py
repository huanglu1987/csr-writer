"""Public raw-folder entry, physical cell provenance and ambiguity behavior."""
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.cli import main
from csr_writer.project_draft import draft_project
from csr_writer.project_table_match import find_layout, study_matches
from csr_writer.errors import ConfigurationError

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None


@unittest.skipUnless(Workbook, "Excel intake requires openpyxl")
class AutomaticDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root/"unknown"
        self.raw.mkdir()
        (self.raw/"A17.md").write_text("# 临床研究方案\n研究编号：NX-47-C\n文件状态：现行有效\n# 研究目标\n评价第八周变化。\n")
        (self.raw/"B54.md").write_text("# 统计分析计划\n研究编号：NX-47-C\n# 统计分析方法\n治疗组别：甲、乙、丙。主要终点分析集：FAS。")

    def workbook(self, name="C19.xlsx", code="NX-47-C", state="Final", value=0.125, groups=("甲","乙","丙")):
        book=Workbook()
        ws=book.active
        for ix,text in enumerate(["Table 7.9 Primary endpoint","Study ID: "+code,"Status: "+state,"Population: FAS"],3):
            ws.cell(ix,2,text)
        for i,text in enumerate(("Measure",*groups),2):
            ws.cell(8,i,text)
        for i,text in enumerate(("应答率",value,.25,.5),2):
            cell=ws.cell(9,i,text)
            if i>2:
                cell.number_format="0.0%"
        book.save(self.raw/name)

    def run_draft(self):
        report=draft_project(self.raw)
        out=Path(report["output_dir"])
        text="\n".join(p.text for p in Document(out/"CSR内部审阅初稿.docx").paragraphs)
        return report,out,text

    def test_raw_only_public_cli_and_native_percent_trace(self):
        self.workbook()
        (self.raw/"A01.md").write_text("# 临床研究方案\n研究编号：NX-47-C\n文件状态：已被替代\n# 研究目标\n不得并入的历史目标。")
        self.assertEqual(0,main(["draft-project","--input-dir",str(self.raw)]))
        out=self.root/"unknown_CSR初稿"
        report=json.loads((out/"draft-report.json").read_text())
        row=next(r for r in report["sections"] if r["section"]=="11.4.1")
        self.assertEqual(("Sheet!C9","12.5%"),(row["sources"][0]["numeric_cells"][0]["location"],row["sources"][0]["numeric_cells"][0]["value"]))
        rendered="\n".join(p.text for p in Document(out/"CSR内部审阅初稿.docx").paragraphs)
        self.assertIn("甲为12.5%",rendered)
        self.assertNotIn("不得并入的历史目标",rendered)
        self.assertEqual("A17.md",report["selected_sources"]["protocol"])
        report2,out2,_=self.run_draft()
        self.assertNotEqual(out,out2)
        self.assertEqual(report["source_snapshot"],report2["source_snapshot"])

    def test_foreign_study_draft_formula_and_wrong_treatments(self):
        for options, marker in [
            ({"code":"OTHER-11-Z"},"研究编号"),
            ({"state":"Draft"},"定稿状态"),
            ({"value":"=1234567"},"公式"),
            ({"groups":("甲","乙","陌生组")},"治疗组"),
        ]:
            with self.subTest(options=options):
                self.workbook(**options)
                report,_,body=self.run_draft()
                self.assertNotIn("甲为",body)
                self.assertIn(marker, str(report["review_items"]))

    def test_duplicate_releases_and_sap_conflict(self):
        self.workbook()
        self.workbook("another.xlsx",value=.875)
        report,_,body=self.run_draft()
        self.assertNotIn("甲为",body)
        self.assertIn("多个定稿候选", str(report["human_decisions"]))
        (self.raw/"B54.md").write_text("# 统计分析计划\n研究编号：ZZ-12-B\n# 统计分析方法\n错误研究的方法句。")
        report,_,body=self.run_draft()
        self.assertNotIn("错误研究的方法句",body)
        self.assertNotIn("甲为",body)
        self.assertIn("身份冲突",str(report["human_decisions"]))

    def test_patient_table_and_contradictory_metadata(self):
        with self.assertRaises(ConfigurationError):
            find_layout([["Table 3 Primary endpoint"],["NX-47-C｜定稿｜FAS"],["指标","受试者编号"],["值","P004"]])
        layout=find_layout([["Table 3 Primary endpoint"],["Study ID: NX-47-C"],["Study ID: AX-21"],["Final｜FAS"],["指标","甲","乙"],["变化","2","3"]])
        self.assertFalse(study_matches(layout,"NX-47-C"))

    def test_agent_response_for_an_unfamiliar_caption_uses_physical_cells(self):
        from openpyxl import load_workbook
        self.workbook()
        f=self.raw/"C19.xlsx"
        wb=load_workbook(f)
        wb.active["B3"]="Table 7.9 日间觉醒量表"
        wb.save(f)
        report,out,body=self.run_draft()
        self.assertNotIn("甲为12.5%",body)
        task=json.loads((out/report["agent_work"]).read_text())
        self.assertEqual(["应答率"],task["unmatched"][0]["row_labels"])
        answer={"source_snapshot":task["source_snapshot"],"tables":[{"source":"C19.xlsx","table":"Sheet","topic":"efficacy","row_labels":["应答率"]}]}
        reply=self.root/"答复.json"
        reply.write_text(json.dumps(answer))
        result=draft_project(self.raw,matches_path=reply)
        row=next(r for r in result["sections"] if r["section"]=="11.4.1")
        self.assertEqual("12.5%",row["sources"][0]["numeric_cells"][0]["value"])
        answer["tables"][0]["row_labels"]=["捏造的指标"]
        reply.write_text(json.dumps(answer))
        blocked=draft_project(self.raw,matches_path=reply)
        self.assertIn("未唯一匹配",str(blocked["source_followup"]))
        self.assertEqual("pending",next(r["status"] for r in blocked["sections"] if r["section"]=="11.4.1"))
        (self.raw/"B54.md").write_text("统计分析计划已改版")
        with self.assertRaisesRegex(ConfigurationError,"快照"):
            draft_project(self.raw,matches_path=reply)

    def test_original_rtf_can_be_selected_by_content(self):
        (self.raw/"B54.md").write_text("# 统计分析计划\n研究编号：NX-47-C\n# 统计分析方法\n主要终点分析集：FAS。")
        desc=r"\trowd\cellx3000\cellx5500\cellx8000"
        header=desc+r"\pard\intbl\b Measure\cell\pard\intbl\b Arm A\cell\pard\intbl\b Arm B\cell\row"
        row=desc+r"\pard\intbl Change\cell\pard\intbl -2.5\cell\pard\intbl -1.0\cell\row"
        (self.raw/"G20.rtf").write_bytes((r"{\rtf1\ansi\ansicpg1252 Table 2.8 Primary endpoint\par Study ID: NX-47-C\par Status: Final\par Population: FAS\par "+header+row+"}").encode("ascii"))
        report,_,body=self.run_draft()
        r=next(s for s in report["sections"] if s["section"]=="11.4.1")
        self.assertIn("Arm A为-2.5",body,report["review_items"])
        self.assertEqual("row:2/cell:2",r["sources"][0]["numeric_cells"][0]["location"])

    def test_sole_obsolete_version_isnt_adopted(self):
        self.workbook()
        (self.raw/"B54.md").write_text("# 统计分析计划\n研究编号：NX-47-C\n文件状态：已被替代\n# 统计分析方法\n历史计划不可纳入。")
        report,_,body = self.run_draft()
        self.assertNotIn("历史计划不可纳入",body)
        self.assertNotIn("甲为12.5%",body)
        self.assertNotIn("sap",report["selected_sources"])
        self.assertIn("需提供有效版本",str(report["source_followup"]))

    def test_routing_answer_uses_selected_documents_and_verifies_quotes(self):
        fixture = Path(__file__).resolve().parents[1]/"examples/fixtures/m20_unfamiliar/study_facts.single_arm_migraine.deidentified.json"
        facts = json.loads(fixture.read_text())
        facts["protocol_id"] = "NX-47-C"
        protocol = self.raw/"A17.md"
        fields = ("design","objectives","population","data_domains","analyses")
        lines = [json.dumps(facts[k],ensure_ascii=False) for k in fields]
        protocol.write_text("# 临床研究方案\n研究编号：NX-47-C\n# 研究设计\n"+"\n".join(lines))
        report,_,_ = self.run_draft()
        answer={"source_snapshot":report["source_snapshot"],"tables":[],"study_facts":{
            "value":facts,"basis":[{"field":field,"source":"A17.md","location":f"line:{n}","quote":quote}
                                  for n,(field,quote) in enumerate(zip(fields,lines),4)]}}
        path=self.root/"reply.json"
        path.write_text(json.dumps(answer))
        routed=draft_project(self.raw,matches_path=path)
        self.assertTrue((Path(routed["output_dir"])/"routing.json").is_file())
        self.assertTrue(any(r["section"]=="10.3" for r in routed["sections"]))
        answer["study_facts"]["basis"][0]["quote"]="原件中没有的一段文本"
        path.write_text(json.dumps(answer))
        with self.assertRaisesRegex(ConfigurationError,"引文"):
            draft_project(self.raw,matches_path=path)

    def test_specialized_protocol_emits_structure_task(self):
        (self.raw/"A17.md").write_text("# 临床研究方案\n研究编号：NX-47-C\n# 研究目标\n评价晚期实体瘤的客观缓解率。")
        report,out,_=self.run_draft()
        work=json.loads((out/report["agent_work"]).read_text())
        self.assertEqual("line:4",work["review_project_structure"][0]["location"])

    def test_extra_estimate_column_does_not_mask_wrong_arm(self):
        self.workbook(groups=("甲","陌生组","估计值"))
        report,_,body=self.run_draft()
        self.assertNotIn("甲为12.5%",body)
        self.assertIn("治疗组与",str(report["human_decisions"]))

    def test_generic_directory_retains_schedule_and_safety_endpoints(self):
        (self.raw/"A17.md").write_text("# 临床研究方案\n研究编号：NX-47-C\n# 访视与评价时间表\n访视每四周一次。\n# 安全性终点\n收集干预期间事件并随访。")
        report,out,_=self.run_draft()
        section=next(r for r in report["sections"] if r["section"]=="9.5.1")
        self.assertEqual(["line:4","line:6"],[s["location"] for s in section["sources"]])
