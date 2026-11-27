"""Public folder entry from subject records. Includes a distinct endpoint and partial-failure cases."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from docx import Document
from csr_writer.project_draft import draft_project
from csr_writer.subject_analysis import wilson
from csr_writer.errors import ConfigurationError
from csr_writer.project_table_match import COMPUTED_STATUS,find_layout

try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook=None

RESPONSE="主要终点为第2周功能评分（FUNC）较基线上升至少2分的应答率"
CHANGE="主要终点为第2周功能评分（FUNC）相对基线的变化量"
COMMON=["量表范围为0至10分","基线取随机前3日至随机当日标记为BL的唯一测量",
        "随机当日为第1天，目标日为第14天，访视窗为第12至16天，取距目标日最近的测量，等距时取较晚者"]
BINARY=["ITT中基线或窗内终点评分缺失者按未应答处理","随机至第14天（含当日）使用救援治疗者按未应答处理",
        "各组应答率采用双侧90% Wilson置信区间","组间报告未调整率差（甲减乙），采用双侧90% Newcombe无连续性校正置信区间，不作显著性检验"]
SAFE=["TEAE观察窗为首次实际用药日至末次实际用药后2日，含首尾两日","窗内新发事件及首次用药前已发生而在窗内加重的事件计为TEAE",
      "按实际治疗组报告发生TEAE的人数、百分比和事件数，同一人同一首选术语只计一人",
      "按研究者记录汇总严重、致死、药物相关和3级及以上TEAE，相关性待定者单列",
      "相关性为可能有关、有关计入药物相关，无关不计入，待评估单列","暴露天数按每人实际正剂量给药的不同日期计数，报告组内总人日"]

@unittest.skipIf(load_workbook is None,"统计表测试使用可选的 openpyxl")
class SubjectTablesTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/"原件"
        self.root.mkdir()
        self.protocol=self.root/"p.md"
        self.sap=self.root/"s.md"
        self.set_methods()
        self.put("random.csv","研究编号 受试者编号 是否随机 随机组 随机日期 使用救援 首次救援日期 不良事件收集完成 是否实际用药",[
            ["T-72",s,"Y",arm,"2026-01-01","Y" if s=="C" else "N","2026-01-14" if s=="C" else "","Y","N" if s=="D" else "Y"]
            for s,arm in zip("ABCD",["甲","甲","乙","乙"])])
        self.put("actual.tsv","STUDYID USUBJID EXTRT EXDOSE EXSTDAT",[["T-72",s,g,"1","2026-01-01"] for s,g in zip("ABC",["乙","甲","乙"])])
        self.put("q.csv","STUDYID USUBJID TESTCD VISIT QSSTRESN QSDAT",
                 [["T-72",s,"FUNC","BL",3,"2026-01-01"] for s in "ABCD"]+
                 [["T-72",s,"FUNC","FU",v,"2026-01-"+d] for s,d,v in [("A","13",1),("A","15",5),("C","14",9),("D","17",9)]])
        self.put("a.csv","STUDYID USUBJID AESEQ AEDECOD AESTDAT AEWORSDT AESER AESDTH AETOXGR AEREL",[
            ["T-72","A",1,"头痛","2025-12-29","2026-01-01","N","N",1,"有关"],
            ["T-72","A",2,"头痛","2026-01-03","","N","N",1,"待评估"],
            ["T-72","B",1,"头痛","2026-01-04","","N","N",1,"无关"]])
    def put(self,name,columns,rows):
        with (self.root/name).open("w",newline="") as f:
            w=csv.writer(f,delimiter="\t" if name.endswith(".tsv") else ",")
            w.writerow(columns.split());w.writerows(rows)
    def set_methods(self,continuous=False,extra=""):
        primary=CHANGE if continuous else RESPONSE
        rules=["按随机组对基线和窗内终点均有测量的观察病例进行描述，缺失不插补，救援后测量保留",
               "变化量按随访减基线计算，报告均值、样本标准差和观察人数，不作组间推断"] if continuous else BINARY
        self.protocol.write_text("# 临床研究方案\n研究编号：T-72\n# 研究终点\n"+primary+"。")
        self.sap.write_text("# 统计分析计划\n研究编号：T-72\n# 分析人群与分组\n治疗组别：甲、乙。ITT纳入全部随机受试者，按随机组分析。SAF纳入至少实际用药一次的受试者，按实际用药组分析。\n# 主要疗效分析\n"
                            +"。".join([primary,*COMMON,*rules])+"。"+extra+"\n# 安全性分析\n"+"。".join(SAFE)+"。")
    def draft(self):
        result=draft_project(self.root)
        out=Path(result["output_dir"])
        return result,json.loads((out/"统计分析/分析留痕.json").read_text()),out
    def test_binary_contrast_actual_groups_date_ties_and_replay(self):
        r,j,out=self.draft()
        self.assertEqual({"A":"应答","B":"缺失按未应答","C":"救援按未应答","D":"缺失按未应答"},
                         {s:x["status"] for s,x in j["derivations"]["efficacy"].items()})
        self.assertEqual([True,True,False],[x["included"] for x in j["derivations"]["events"]])
        wb=load_workbook(out/j["workbook"])
        self.assertEqual("1/2（50.0%）",wb["3.1"]["B4"].value)
        self.assertEqual("1/2（50.0%）",wb["4.1"]["C4"].value)
        self.assertEqual("2",wb["4.1"]["C5"].value)
        self.assertNotIn(j["workbook"],r["sources"])
        self.assertIn(j["workbook"],r["derived_sources"])
        replay=draft_project(self.root,plan_path=out/"project-plan.json")
        self.assertEqual(r["coverage"],replay["coverage"])
        body="\n".join(p.text for p in Document(out/"CSR内部审阅初稿.docx").paragraphs)
        self.assertIn(COMPUTED_STATUS,body)
        self.assertNotIn("T-72｜定稿",body)
    def test_continuous_observed_keeps_post_rescue_scores(self):
        self.set_methods(continuous=True)
        _,a,out=self.draft()
        w=load_workbook(out/a["workbook"])
        self.assertEqual(("2.00","6.00"),(w["3.1"]["B4"].value,w["3.1"]["C4"].value))
        self.assertNotIn("3.2",w.sheetnames)
    def test_unimplemented_qualifier_preserves_safety(self):
        self.set_methods(extra="主要分析另按中心分层调整。")
        _,a,out=self.draft()
        w=load_workbook(out/a["workbook"])
        self.assertNotIn("3.1",w.sheetnames)
        self.assertIn("4.1",w.sheetnames)
        self.assertIn("未能完整执行",str(a["data_queries"]))
    def test_conflicting_protocol_or_sap_does_not_write_effectiveness(self):
        self.protocol.write_text(self.protocol.read_text().replace("至少2分","至少3分"))
        _,a,out=self.draft()
        self.assertNotIn("3.1",load_workbook(out/a["workbook"]).sheetnames)
        self.assertIn("未能唯一核对",str(a["decisions"]))
        self.set_methods()
        self.sap.write_text(self.sap.read_text().replace("T-72","FOREIGN-18"))
        _,a,_=self.draft()
        self.assertNotIn("workbook",a)
    def test_ambiguous_event_ids_or_incomplete_ae_collection_suppresses_safety_only(self):
        src=self.root/"a.csv"
        original=src.read_text()
        src.write_text(original+original.splitlines()[1]+"\n")
        _,audit,out=self.draft()
        sheets=load_workbook(out/audit["workbook"]).sheetnames
        self.assertIn("3.1",sheets);self.assertNotIn("4.1",sheets)
        src.write_text(original)
        dm=self.root/"random.csv"
        dm.write_text(dm.read_text().replace("A,Y,甲,2026-01-01,N,,Y,Y","A,Y,甲,2026-01-01,N,,N,Y"))
        _,a,_=self.draft()
        self.assertNotIn("events",a["derivations"])
    def test_unattested_absent_dose_is_not_silently_zero(self):
        dm=self.root/"random.csv"
        dm.write_text(dm.read_text().replace("D,Y,乙,2026-01-01,N,,Y,N","D,Y,乙,2026-01-01,N,,Y,Y"))
        _,a,_=self.draft()
        self.assertNotIn("exposure",a["derivations"])
        self.assertIn("efficacy",a["derivations"])
    def test_input_cannot_self_declare_derived_status(self):
        rows=[["表 1 主要终点"],["研究编号：T-72｜状态："+COMPUTED_STATUS+"｜分析集：ITT"],["指标","甲","乙"],["结果","2","3"]]
        with self.assertRaises(ConfigurationError):
            find_layout(rows)
    def test_wilson_extremes(self):
        self.assertAlmostEqual(0.,wilson(0,5,95)[0])
        self.assertAlmostEqual(1.,wilson(5,5,95)[1])

    def test_complete_prose_under_general_heading_keeps_original_quotes(self):
        sap=self.sap.read_text().replace("# 分析人群与分组","# 统计分析方法").replace("# 主要疗效分析\n","").replace("# 安全性分析\n","")
        sap=sap.replace("ITT纳入全部随机受试者，按随机组分析","意向性治疗集（ITT）纳入所有已随机化的受试者，按照随机分配的治疗组进行分析").replace("量表范围为","分值范围为")
        self.sap.write_text(sap+"\n# 敏感性分析\n另行使用多重插补开展敏感性分析。")
        _,audit,out=self.draft()
        self.assertIn("3.1",load_workbook(out/audit["workbook"]).sheetnames)
        self.assertIn("意向性治疗集",audit["sap_rules"]["rules"]["groups"]["itt"]["quote"])
        self.assertIn("敏感性分析",str(audit["data_queries"]))

    def test_raw_table_like_filename_is_not_a_supplied_result(self):
        (self.root/"random.csv").rename(self.root/"T01.csv")
        _,a,_=self.draft()
        self.assertEqual("review_draft",a["status"])

    def test_unresolved_additional_safety_window_does_not_override_sap(self):
        with self.sap.open("a") as f:
            f.write("\n# 其他说明\nTEAE观察窗另行延长至末次用药后90日。")
        _,a,_=self.draft()
        self.assertNotIn("events",a["derivations"])
        self.assertIn("efficacy",a["derivations"])
