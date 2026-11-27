import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from csr_writer.errors import ConfigurationError
from csr_writer.project_answers import Decisions
from csr_writer.project_reconcile import same_value
from csr_writer.sap_rules import parse_sap


class SourceBoundReviewTests(unittest.TestCase):
    def test_split_reordered_clauses_have_two_anchors(self):
        records=[{"text":s,"location":f"line:{n}"} for n,s in enumerate(
            ["# 分析人群及一般统计约定","疗效分析始终按最初随机分配的组别归组。","所有已随机的受试者均进入ITT。"],1)]
        _,ev,_=parse_sap(records)
        self.assertEqual({a["location"] for a in ev["groups"]["itt"]["anchors"]},{"line:2","line:3"})
        records[-1]["text"]+="但应排除失访者。"
        _,_,issues=parse_sap(records)
        self.assertIn("排除失访者",issues["groups"])

    def test_host_semantic_clause_is_anchored_and_cannot_change_week(self):
        quote="第12周以腰部功能受限评分（LFI）相对基线的改变作为首要疗效指标"
        records=[dict(location="line:1",text="# 主要疗效分析"),dict(location="line:2",text=quote)]
        normalized="主要终点为第12周腰部功能受限评分（LFI）相对基线的变化量"
        reading=dict(section="efficacy",location="line:2",quote=quote,rule="change",normalized=normalized,rationale="明确给出了周次、量表及变化量")
        _,evidence,_=parse_sap(records,[reading])
        self.assertEqual(evidence["efficacy"]["change"]["quote"],quote)
        for altered in [dict(normalized=normalized.replace("12","16")),dict(location="line:3")]:
            with self.assertRaises(ConfigurationError):
                parse_sap(records,[{**reading,**altered}])

    def test_only_changed_evidence_reopens(self):
        first=Decisions(None)
        first.bind("DEMO-1",True)
        def ask(review,key,data):
            return review.ask(key,"统计负责人","采用结果",["14"],data,["采用"])
        ask(first,"TFL-A",[1])
        ask(first,"TFL-B",[2])
        with TemporaryDirectory() as tmp:
            path=Path(tmp)
            first.save(path)
            batch=json.loads((path/"集中答复模板.json").read_text())
            for a in batch["answers"]:
                a.update(choice="采用",statement="经原始资料核对",date="2026-11-27",reviewer="演练统计负责人")
            f=path/"response.json"
            f.write_text(json.dumps(batch))
            next_run=Decisions(f)
            next_run.bind("DEMO-1",True)
            self.assertIsNotNone(ask(next_run,"TFL-A",[1]))
            self.assertIsNone(ask(next_run,"TFL-B",[3]))
            self.assertEqual([r["status"] for r in next_run.items],["answered","stale"])
            with self.assertRaises(ConfigurationError):
                Decisions(f).bind("DEMO-1",False)

    def test_display_comparison_retains_inequalities_and_denominators(self):
        self.assertTrue(same_value("42.00","42"))
        self.assertFalse(same_value("<0.001","0.001"))
        self.assertFalse(same_value("1/5（20%）","2/10（20%）"))
