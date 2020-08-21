import jax.numpy as np
from jax import grad
from jax import jit
from jax.experimental import optimizers

from jax.config import config
config.update("jax_enable_x64", True)

from ._base import splineBase, interp1d
from .._splines import build_spline_matrix

__all__ = ['splineLNP']

class splineLNP(splineBase):

    def __init__(self, X, y, dims, df, smooth='cr', nonlinearity='softplus',
            compute_mle=False, **kwargs):
        
        super().__init__(X, y, dims, df, smooth, compute_mle, **kwargs)
        self.nonlinearity = nonlinearity
    

    def forward_pass(self, p, extra):

        """
        Model ouput with current estimated parameters.
        """

        XS = self.XS if extra is None else extra['XS']

        if hasattr(self, 'bh_spl'):
            if extra is not None and 'yS' in extra:
                yS = extra['yS']
            else:
                yS = self.yS
        
        if self.fit_intercept:
            intercept = p['intercept'] 
        else:
            if hasattr(self, 'intercept'):
                intercept = self.intercept
            else:
                intercept = np.array([0.])
        
        if self.fit_R:
            R = p['R']
        else:
            R = np.array([1.])

        if self.fit_nonlinearity:
            self.fitted_nonlinearity = interp1d(self.bins, self.Snl @ p['bnl'])

        if self.fit_linear_filter:
            filter_output = XS @ p['b'] 
        else:
            if hasattr(self, 'b_opt'): 
                filter_output = XS @ self.b_opt
            else:
                filter_output = XS @ self.b_spl        

        if self.fit_history_filter:
            history_output = yS @ p['bh']  
        else:
            if hasattr(self, 'bh_opt'):

                history_output = yS @ self.bh_opt
            elif hasattr(self, 'bh_spl'):
                history_output = yS @ self.bh_spl
            else:
                history_output = np.array([0.])
        
        r = R * self.fnl(filter_output + history_output + intercept, nl=self.nonlinearity).flatten()

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