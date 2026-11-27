"""Contracts for heterogeneous continuous and discrete reference-based longitudinal results."""
from itertools import product
import unittest
try:
    import numpy as np
    import scipy
except ImportError:
    np=None
from csr_writer.errors import ConfigurationError


@unittest.skipIf(np is None,"模型统计依赖")
class MixedCategorical(unittest.TestCase):
    def test_covariance_splice_uses_reference_conditional_regression(self):
        from csr_writer.reference_imputation import transition_covariance
        a=np.array([[4.,2.],[2.,5.]])
        b=np.array([[2.,-1.],[-1.,3.]])
        np.testing.assert_allclose(transition_covariance(a,b,1),[[4,-2],[-2,3.5]])
        np.testing.assert_array_equal(transition_covariance(a,b,0),b)
        np.testing.assert_array_equal(transition_covariance(a,b,2),a)

    def test_binary_and_multicategory_margins_and_observed_support(self):
        from csr_writer.categorical_reference import compatible,controlled,sample_paths
        for c in (2,3):
            paths=np.array(list(product(range(c),repeat=3)))
            rng=np.random.default_rng(191)
            a,b=rng.dirichlet(np.ones(len(paths)),size=2)
            def margin(dist,t):
                return np.bincount(paths[:,t],weights=dist)
            for s in ("J2R","CIR"):
                np.testing.assert_equal(controlled(a,b,paths,c,0,s),b)
                np.testing.assert_equal(controlled(a,b,paths,c,3,s),a)
                q=controlled(a,b,paths,c,2,s)
                np.testing.assert_allclose(q.reshape(c,c,c).sum(axis=2),a.reshape(c,c,c).sum(axis=2),atol=1e-9)
                desired=margin(b,2)
                if s=="CIR":desired*=margin(a,1)/margin(b,1)
                np.testing.assert_allclose(margin(q,2),desired/desired.sum(),atol=1e-9)
                y=np.tile([-1,0,1],(15,1));allowed=compatible(y,paths)
                z=paths[sample_paths(q,allowed,np.linspace(0,np.nextafter(1.,0.),15))]
                np.testing.assert_equal(z[:,1:],y[:,1:])

    def test_treatment_and_covariance_follow_arm_coding(self):
        from csr_writer.random_center import fit
        rng=np.random.default_rng(716);n=160
        group=np.tile([0,1],n//2);bl=rng.normal(size=n);site=np.repeat(np.arange(16),10)
        intercept=rng.normal(0,3,16);slope=rng.normal(0,4,16)
        error=rng.normal(size=(n,2))*(1+group[:,None])
        y=intercept[site,None]+bl[:,None]+group[:,None]*(1+slope[site,None])+error
        f,t=fit(group,bl,y,site,.95,slope=True,heterogeneous=True)
        rev,tr=fit(1-group,bl,y,site,.95,slope=True,heterogeneous=True)
        np.testing.assert_allclose([f["estimate"],f["se"]],[-rev["estimate"],rev["se"]],atol=1e-5)
        np.testing.assert_allclose(t["residual_by_arm"]["test"],tr["residual_by_arm"]["reference"],atol=.001)
        for centers,is_slope in ((None,False),(site,False),(site,True)):
            _,trace=fit(group,bl,y,centers,.95,slope=is_slope,heterogeneous=not is_slope)
            self.assertEqual("center_random_covariance" in trace,is_slope)
        with self.assertRaises(ConfigurationError):
            fit(group,bl,y,group,.95,slope=True,heterogeneous=True)
