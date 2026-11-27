"""Regression protection for SAP-specified numerical models and their failure boundaries."""
import unittest
try:
    import numpy as np
    from scipy import stats
except ImportError:
    np=None
from csr_writer.errors import ConfigurationError
from csr_writer.sap_rules import parse_sap


@unittest.skipIf(np is None,"模型需要NumPy、SciPy")
class ModelTests(unittest.TestCase):
    def setUp(self):
        from csr_writer import longitudinal_models as models
        self.m=models
        rng=np.random.default_rng(59)
        self.a=np.tile([0.,1.],37)
        self.b=rng.normal(size=74)
        self.y=rng.normal(size=(74,3))+rng.normal(size=(74,1))+self.b[:,None]+1.81*self.a[:,None]
        self.s=dict(base_inter="与访视交互",inference="期望信息Satterthwaite",m="20",seed="261",mi_df="Barnard-Rubin")
    def test_complete_saturated_mmrm_matches_endpoint_ancova(self):
        anc,_=self.m.ancova(self.a,self.b,self.y,.95)
        mm,trace=self.m.mmrm(self.a,self.b,self.y,.95,self.s)
        for key in ("estimate","df","low","high","se","p"):
            self.assertAlmostEqual(mm[key],anc[key],places=4)
        self.assertEqual(222,trace["records"])
    def test_intermittent_mmrm_keeps_final_measure_but_monotone_mi_refuses(self):
        self.y[:7,1]=np.nan
        mm,t=self.m.mmrm(self.a,self.b,self.y,.9,{**self.s,"base_inter":"","inference":"渐近正态"})
        self.assertIsNone(mm["df"])
        self.assertEqual([74,67,74],t["visits_observed"])
        with self.assertRaisesRegex(ConfigurationError,"间歇性"):
            self.m.multiple_imputation(self.a,self.b,self.y,.9,self.s)
    def test_same_seed_pool_and_ancova_group_reversal(self):
        self.y[:13,1:]=np.nan
        x,t=self.m.multiple_imputation(self.a,self.b,self.y,.95,self.s)
        y,s=self.m.multiple_imputation(self.a,self.b,self.y,.95,self.s)
        self.assertEqual((x,t),(y,s))
        pooled=t["within"]+1.05*t["between"]
        self.assertAlmostEqual(x["se"]**2,pooled)
        self.assertGreater(t["between"],0)
        one,_=self.m.ancova(self.a,self.b,self.y,.9)
        two,_=self.m.ancova(1-self.a,self.b,self.y,.9)
        self.assertAlmostEqual(one["low"],-two["high"])
    def test_singular_and_empty_visits_fail(self):
        with self.assertRaisesRegex(ConfigurationError,"不满秩"):
            self.m.ancova(self.a,self.a,self.y,.9)
        self.y[:,2]=np.nan
        with self.assertRaisesRegex(ConfigurationError,"重复观察"):
            self.m.mmrm(self.a,self.b,self.y,.95,self.s)
    def test_cmh_direction_and_cox_without_events(self):
        from csr_writer.secondary_models import fit_secondary
        people={str(i):dict(randomized=True,arm="T" if i%2 else "C",stratum=str(i//12),loc="DM",rand_date="2026-01-01") for i in range(24)}
        spec=dict(ref="C",test="T",level="90",code="A",outcome="完成行走",margin="1",target="9",cut="15")
        protocol=[dict(text="完成行走（A）")]
        domain=dict(rows=[dict(id=s,task_date="2026-01-09",task_code="A",task_done="Y" if int(s)%4==1 or int(s)%6==2 else "N",loc=s) for s in people])
        _,trace=fit_secondary("cmh",people,domain,["C","T"],spec,protocol)
        _,rev=fit_secondary("cmh",people,domain,["C","T"],{**spec,"ref":"T","test":"C"},protocol)
        self.assertAlmostEqual(1/trace["result"]["estimate"],rev["result"]["estimate"])
        for s in people:people[s]["stratum"]=""
        with self.assertRaisesRegex(ConfigurationError,"登记层"):
            fit_secondary("cmh",people,domain,["T","C"],spec,protocol)
        domain=dict(rows=[dict(id=s,event_code="A",event_date="",last_known="2026-01-20",loc=s) for s in people])
        with self.assertRaisesRegex(ConfigurationError,"事件不足"):
            fit_secondary("cox",people,domain,["T","C"],spec,protocol)
    def test_conflicting_extra_qualifier_not_lost(self):
        rec=[dict(text=s,kind="paragraph",location=f"line:{i+1}") for i,s in enumerate([
            "# 主要疗效分析","主要分析采用ANCOVA，终点观察病例按治疗组和基线拟合普通最小二乘模型，采用残差自由度的t分布，但按中心调整。"])]
        _,ev,issues=parse_sap(rec)
        self.assertNotIn("ancova",ev["efficacy"])
        self.assertIn("但按中心调整",issues["efficacy"])

    def test_primary_mi_unavailable_marked_on_surviving_supportive_table(self):
        from unittest.mock import patch
        from csr_writer.model_tables import model_tables
        spec=dict(days="8、15、22",margin="2",target="22",start="20",end="24",
                  week="3",endpoint="另一评分",test="实验",ref="对照",model_conf="90",
                  mi_role="主要",ancova_role="支持性",**self.s)
        self.y[0,1]=np.nan
        def rows(people,domain,arms,local,protocol):
            j=[8,15,22].index(int(local['target']))
            return {f'P{i:03}':dict(source="dm",group="实验" if self.a[i] else "对照",
                     baseline="bl",baseline_value=str(self.b[i]),followup="qs",followup_value=None,
                     change=str(self.y[i,j]) if np.isfinite(self.y[i,j]) else None) for i in range(74)}
        with patch('csr_writer.model_tables.endpoint_rows',side_effect=rows):
            t,trace,errors=model_tables({}, {}, ["实验","对照"],spec,[])
        self.assertEqual(list(trace["models"]),["ancova"])
        self.assertIn("主要分析结果待补",t[0]["rows"][0][0])
        self.assertIn("间歇性缺失",errors[0])
