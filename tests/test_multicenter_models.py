"""Treatment discontinuation, intermittent missingness and small-sample inference."""
import unittest
from unittest.mock import patch
try:
    import numpy as np
    from scipy import stats
except ImportError:
    np=None
from csr_writer.errors import ConfigurationError


@unittest.skipIf(np is None,"模型检验使用NumPy和SciPy")
class MulticenterModelTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(193);self.n=90
        self.a=np.tile([1.,0.],45); self.b=rng.normal(size=self.n)
        self.centre=np.tile(np.arange(3),30)
        self.x=np.column_stack([self.centre==1,self.centre==2,rng.normal(size=self.n)])
        self.y=rng.normal(size=(self.n,3))+self.b[:,None]+self.a[:,None]+self.centre[:,None]
        self.y[::7,1]=np.nan;self.y[2::8,-1]=np.nan
        self.spec={"fcs_m":"12","sweeps":"5","fcs_seed":"7192","fcs_df":"Barnard-Rubin"}

    def test_kr_corrects_variance_and_matches_complete_saturated_limit(self):
        from csr_writer.longitudinal_models import mmrm,ancova
        spec={"base_inter":"与访视交互","inference":"Kenward-Roger"}
        fit,t=mmrm(self.a,self.b,self.y,.95,spec,self.x)
        self.assertGreater(fit["se"],t["unadjusted_se"])
        self.assertGreater(fit["df"],0)
        keep=np.isfinite(self.y).all(axis=1)
        complete,_=mmrm(self.a[keep],self.b[keep],self.y[keep],.95,spec)
        cc,_=ancova(self.a[keep],self.b[keep],self.y[keep],.95)
        for k in ("estimate","se","df","low","high"):
            self.assertAlmostEqual(cc[k],complete[k],places=4)
        with self.assertRaisesRegex(ConfigurationError,"推断规则"):
            mmrm(self.a,self.b,self.y,.95,{**spec,"inference":"unknown"})

    def test_ancova_fixed_centre_matches_explicit_ols(self):
        from csr_writer.longitudinal_models import ancova
        m,_=ancova(self.a,self.b,self.y,.95,self.x);keep=np.isfinite(self.y[:,-1])
        X=np.column_stack([np.ones(self.n),self.a,self.b,self.x])[keep]
        beta=np.linalg.lstsq(X,self.y[keep,-1],rcond=None)[0]
        self.assertAlmostEqual(m["estimate"],beta[1])
        self.assertEqual(m["df"],sum(keep)-len(beta))
        with self.assertRaisesRegex(ConfigurationError,"不满秩"):
            ancova(self.a,self.b,self.y,.95,self.a[:,None])

    def test_fcs_and_mnar_paired_scenarios(self):
        from csr_writer.fcs_models import fcs
        y=self.y.copy();original=y.copy()
        mask=np.isnan(y) & ((np.arange(self.n)%3!=0)[:,None])
        pair=(4,-1)
        fits=fcs(self.a,self.b,y,.95,self.spec,self.x,mask=mask,deltas=[pair,(0,0)])
        np.testing.assert_equal(y,original)
        self.assertEqual(fits[None][0],fits[0,0][0])
        X=np.column_stack([np.ones(self.n),self.a,self.b,self.x])
        effect=np.linalg.lstsq(X,mask[:,-1]*np.where(self.a==1,*pair),rcond=None)[0][1]
        a=np.array(fits[None][1]["completed_estimates_and_variances"])
        b=np.array(fits[pair][1]["completed_estimates_and_variances"])
        np.testing.assert_allclose(b[:,0]-a[:,0],effect,atol=1e-10)
        self.assertGreater(a[:,0].std(),0)
        self.assertEqual(fits[None][0],fcs(self.a,self.b,y,.95,self.spec,self.x)[None][0])
        with self.assertRaisesRegex(ConfigurationError,"缺失单元格"):
            fcs(self.a,self.b,y,.95,self.spec,self.x,mask=np.ones(y.shape,bool),deltas=[pair])

    def test_source_variables_stop_dates_and_conflicts(self):
        from csr_writer.fcs_models import fixed_covariates,discontinuations,parse_deltas
        p={"s1":{"loc":"dm1","rand_date":"2026-01-02","covariates":{"SITE":"BE","BMI":"22"}},
           "s2":{"loc":"dm2","rand_date":"2026-01-02","covariates":{"SITE":"AG","BMI":"26"}}}
        ids=list(p)
        fixed,settings=fixed_covariates(ids,p,{"cat_cols":"SITE","num_cols":"BMI"})
        np.testing.assert_allclose(fixed,[[1,-2],[0,2]])
        self.assertEqual(settings["SITE"]["reference"],"AG")
        d=dict(rows=[{"id":"s1","stop_yn":"Y","stop_date":"2026-01-10","loc":"ds1"},
                     {"id":"s2","stop_yn":"N","stop_date":"","loc":"ds2"}])
        s,_=discontinuations(ids,p,d,None);self.assertEqual(s.tolist(),[9,np.inf])
        with self.assertRaisesRegex(ConfigurationError,"后续给药"):
            discontinuations(ids,p,d,{"s1":{"last":"2026-01-12"}})
        p["s1"]["stop_yn"]="N";p["s1"]["stop_date"]=""
        with self.assertRaisesRegex(ConfigurationError,"冲突"):
            discontinuations(ids,p,d,None)
        del p["s1"]["covariates"]["BMI"]
        with self.assertRaisesRegex(ConfigurationError,"逐人协变量"):
            fixed_covariates(ids,p,{"cat_cols":"SITE","num_cols":"BMI"})
        self.assertEqual(parse_deltas("+1/0、-2/+1"),[(1,0),(-2,1)])
        with self.assertRaisesRegex(ConfigurationError,"成对"):
            parse_deltas("2/1/2")

    def test_named_delta_order_and_same_day_boundary(self):
        from csr_writer.model_tables import model_tables
        ids=[f"S{i:03}" for i in range(self.n)]
        people={s:dict(rand_date="2026-01-01",loc="dm",stop_yn="Y",stop_date="2026-01-15") for s in ids}
        days=[8,15,22]
        spec=dict(days="8、15、22",margin="1",target="22",start="21",end="23",week="3",endpoint="功能",
                  test="实验",ref="对照",model_conf="95",fcs_role="主要",mnar_role="敏感性",deltas="+2/+5",
                  delta_first="对照",delta_second="实验",missing_table=True,**self.spec)
        def extract(people,domain,arms,local,protocol):
            d=int(local["target"]);j=days.index(d)
            return {s:dict(source="dm",group="实验" if self.a[i] else "对照",baseline="BL",baseline_value=str(self.b[i]),followup="q",
                           followup_value=None,change=str(self.y[i,j]) if np.isfinite(self.y[i,j]) else None,
                           measurements=[dict(row="q",day=d)]) for i,s in enumerate(ids)}
        with patch("csr_writer.model_tables.endpoint_rows",side_effect=extract):
            tables,audit,errors=model_tables(people,{},["对照","实验"],spec,[])
            _,partial,blocked=model_tables(people,{},["对照","实验"],{**spec,"delta_first":"其他"},[])
        self.assertFalse(errors); self.assertTrue(tables)
        self.assertEqual((audit["models"]["mnar1"]["delta_test"],audit["models"]["mnar1"]["delta_reference"]),(5,2))
        self.assertEqual(audit["settings"]["mnar_scope"]["eligible_missing_per_visit"],[0,0,int(np.isnan(self.y[:,2]).sum())])
        self.assertTrue(blocked);self.assertIn("fcs",partial["models"]);self.assertNotIn("mnar1",partial["models"])
