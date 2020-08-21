import jax.numpy as np
import jax.random as random
from jax import grad
from jax import jit
from jax.experimental import optimizers
from jax.config import config
config.update("jax_enable_x64", True)

import time
import itertools
from ._base import splineBase, interp1d
from .._utils import build_design_matrix
from .._splines import build_spline_matrix

from ..MF import KMeans, semiNMF

__all__ = ['splineLNLN']

class splineLNLN(splineBase):

    def __init__(self, X, y, dims, df, smooth='cr', filter_nonlinearity='softplus', output_nonlinearity='softplus', 
                 compute_mle=False, **kwargs):
        
        super().__init__(X, y, dims, df, smooth, compute_mle, **kwargs)
        self.filter_nonlinearity = filter_nonlinearity
        self.output_nonlinearity = output_nonlinearity

    def forward_pass(self, p, extra):

        XS = self.XS if extra is None else extra['XS']
 
        if hasattr(self, 'h_spl'):
            yS = self.yS if extra is None else extra['yS']

        if self.fit_intercept:
            intercept = p['intercept'] 
        else:
            if hasattr(self, 'intercept'):
                intercept = self.intercept
            else:
                intercept = np.array([0.])
        
        if self.fit_R: # maximum firing rate / scale factor
            R = p['R']
        else:
            R = np.array([1.])

        if self.fit_nonlinearity:
            if np.ndim(p['bnl']) != 1:
                self.fitted_nonlinearity = [interp1d(self.bins, self.Snl @ p['bnl'][i]) for i in range(self.n_subunits)]
            else:
                self.fitted_nonlinearity = interp1d(self.bins, self.Snl @ p['bnl']) 

        if self.fit_linear_filter:
            filter_output = np.mean(self.fnl(XS @ p['b'].reshape(self.n_b, self.n_subunits), nl=self.filter_nonlinearity), 1) 
        else:
            filter_output = np.mean(self.fnl(XS @ self.b_opt.reshape(self.n_b, self.n_subunits) , nl=self.filter_nonlinearity), 1) 
  
        if self.fit_history_filter:
            history_output = yS @ p['bh']  
        else:
            if hasattr(self, 'bh_opt'):
                history_output = yS @ self.bh_opt
            elif hasattr(self, 'bh_spl'):
                history_output = yS @ self.bh_spl
            else:
                history_output = np.array([0.])
        
        r = R * self.fnl(filter_output + history_output + intercept, nl=self.output_nonlinearity).flatten()

        return r

    def cost(self, p, extra=None, precomputed=None):

        """
        Negetive Log Likelihood.
        """
        y = self.y if extra is None else extra['y']
        r = self.forward_pass(p, extra) if precomputed is None else precomputed 

        term0 = - np.log(r) @ y
        term1 = np.sum(r) * self.dt

        neglogli = term0 + term1
        
        if self.beta and extra is None:
            l1 = np.linalg.norm(p['b'], 1) 
            l2 = np.linalg.norm(p['b'], 2)
            neglogli += self.beta * ((1 - self.alpha) * l2 + self.alpha * l1)

        return neglogli

    def fit(self, p0=None, extra=None, num_subunits=2, num_iters=5, metric=None,
            alpha=1, beta=0.05, 
            fit_linear_filter=True, fit_intercept=True, fit_R=True,
            fit_history_filter=False, fit_nonlinearity=False, 
            step_size=1e-2, tolerance=10, verbose=1, random_seed=2046):

        self.metric = metric

        self.alpha = alpha # elastic net parameter (1=L1, 0=L2)
        self.beta = beta # elastic net parameter - global penalty weight

        self.n_subunits = num_subunits
        self.num_iters = num_iters   

        self.fit_linear_filter = fit_linear_filter
        self.fit_history_filter = fit_history_filter
        self.fit_nonlinearity = fit_nonlinearity
        self.fit_intercept = fit_intercept
        self.fit_R = fit_R

        # initialize parameters
        if p0 is None:
            p0 = {}

        dict_keys = p0.keys()
        if 'b' not in dict_keys:
            key = random.PRNGKey(random_seed)
            b0 = 0.01 * random.normal(key, shape=(self.n_b, self.n_subunits)).flatten()
            p0.update({'b': b0})

        if 'intercept' not in dict_keys:
            p0.update({'intercept': np.zeros(1)})

        if 'R' not in dict_keys:
            p0.update({'R': np.array([1.])})

        if 'bh' not in dict_keys:
            try:
                p0.update({'bh': self.bh_spl})  
            except:
                p0.update({'bh': None}) 
                
        if 'bnl' not in dict_keys:
            try:
                p0.update({'bnl': self.bnl})
            except:
                p0.update({'bnl': None})
        else:
            if np.ndim(p0['bnl']) != 1:
                self.fitted_nonlinearity = [interp1d(self.bins, self.Snl @ p0['bnl'][i]) for i in range(num_subunits)]
            else:
                self.fitted_nonlinearity = interp1d(self.bins, self.Snl @ p0['bnl'])

        if extra is not None:
            extra.update({'XS': extra['X'] @ self.S})

            if hasattr(self, 'h_spl'):
                
                yh = np.array(build_design_matrix(extra['y'][:, np.newaxis], self.Sh.shape[0], shift=1))
                yS = yh @ self.Sh
                extra.update({'yS': yS}) 

            extra = {key: np.array(extra[key]) for key in extra.keys()}

        self.p0 = p0
        self.p_opt = self.optimize_params(p0, extra, num_iters, metric, step_size, tolerance, verbose)   
        
        self.R = self.p_opt['R'] if fit_R else np.array([1.])        

        if fit_linear_filter:
            self.b_opt = self.p_opt['b'].reshape(self.n_b, self.n_subunits)
            self.w_opt = self.S @ self.b_opt
        
        if fit_history_filter:
            self.h_opt = self.Sh @ self.p_opt['bh']
        
        if fit_intercept:
            self.intercept = self.p_opt['intercept']
        
        if fit_nonlinearity:
            self.bnl_opt = self.p_opt['bnl']
