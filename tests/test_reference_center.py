"""Regression tests for intercurrent events, conditional means and center grouping."""
import unittest
try:
    import numpy as np
    import scipy
except ImportError:
    np = None
from csr_writer.errors import ConfigurationError


@unittest.skipIf(np is None,"需要模型统计依赖")
class ReferenceCenterTests(unittest.TestCase):
    def test_pre_stop_anchor_and_retrieved_followup(self):
        from csr_writer.reference_imputation import trajectory, conditional, patterns
        own = np.tile([2.,5.,9.],(3,1))
        ref = np.tile([1.,1.,3.],(3,1))
        on = np.array([[False]*3,[True,True,False],[True]*3])
        j2r = trajectory(own,ref,on,"J2R")
        cir = trajectory(own,ref,on,"CIR")
        np.testing.assert_array_equal(j2r,[[1,1,3],[2,5,3],[2,5,9]])
        np.testing.assert_array_equal(cir,[[1,1,3],[2,5,7],[2,5,9]])
        y = np.array([[np.nan,np.nan,5.],[np.nan,np.nan,7.],[2.,5.,9.]])
        cov = np.array([[2.,0.,1.],[0.,1.,0.],[1.,0.,2.]])
        filled = conditional(y,j2r,cov,patterns(y),np.zeros(y.shape))
        np.testing.assert_allclose(filled,[[2,1,5],[4,5,7],[2,5,9]])

    def test_feedback_and_zero_delta(self):
        from csr_writer.fcs_models import fcs
        rng=np.random.default_rng(847);n=80
        a=np.tile([1,0],n//2);b=rng.normal(size=n)
        y=rng.normal(size=(n,3))+rng.normal(size=(n,1))*3
        y[:20,0]=np.nan;y[::7,2]=np.nan
        mask=np.isnan(y);mask[:,1:]=False
        spec=dict(fcs_m="5",sweeps="4",fcs_seed="6831",fcs_df="Rubin")
        mar=fcs(a,b,y,.95,spec,mask=mask,deltas=[(6,0)])
        self.assertEqual(mar[None][0],mar[(6,0)][0])
        self.assertEqual(mar[None][0],fcs(a,b,y,.95,spec,mask=mask,in_chain=(0,0))[None][0])
        self.assertGreater(abs(mar[None][0]["estimate"]-fcs(a,b,y,.95,spec,mask=mask,in_chain=(6,0))[None][0]["estimate"]),.001)

    def test_single_center_and_boundary(self):
        from csr_writer.random_center import fit
        x=np.tile([0,1],30);b=np.tile(np.arange(10),6);y=np.random.default_rng(44).normal(size=(10,1))
        with self.assertRaisesRegex(ConfigurationError,"中心内重复数不足"):
            fit(x,b,np.tile(y,(6,1)),np.zeros(60),.95,endpoint=True)
        with self.assertRaisesRegex(ConfigurationError,"方差落在边界"):
            fit(x,b,np.tile(y,(6,1)),np.repeat(range(6),10),.95,endpoint=True)
