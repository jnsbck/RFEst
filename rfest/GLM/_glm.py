import time

import jax.numpy as jnp
import jax.random as random
import numpy as np
from jax import jit
from jax import value_and_grad
from jax.config import config

try:
    from jax.example_libraries import optimizers
except ImportError:
    from jax.experimental import optimizers

from rfest.utils import build_design_matrix
from rfest.splines import build_spline_matrix
from rfest.metrics import r2, r2adj, mse, corrcoef, gcv

config.update("jax_debug_nans", True)

__all__ = ['GLM']


class GLM:

    def __init__(self, distr='poisson', output_nonlinearity='none', dtype=jnp.float64):

        """
        Initialize the GLM class with empty variables.

        Parameters
        ----------

        distr: str
            Noise distribution. Either `gaussian` or `poisson`.

        output_nonlinearity: str
            Nonlinearity for the output layer.
        """

        # initilize variables

        # Data
        self.X = {}  # design matrix
        self.S = {}  # spline matrix
        self.P = {}  # penalty matrix
        self.XS = {}  # dot product of X and S
        self.XtX = {}  # input covariance
        self.Xty = {}  # cross-covariance
        self.y = {}  # response
        self.y_pred = {}  # predicted response
        self.y_pred_upper = {}  # predicted response upper limit
        self.y_pred_lower = {}  # predicted response lower limit

        # Model parameters
        self.p = {}  # all model paremeters
        self.b = {}  # spline weights
        self.b_se = {}  # spline weights standard error
        self.w = {}  # filter weights
        self.w_se = {}  # filter weights standard error
        self.V = {}  # weights covariance
        self.intercept = {}  # intercept

        # Model hypterparameters
        self.df = {}  # number of bases for each filter
        self.edf = {}  # effective degree of freedom given lam
        self.dims = {}  # filter shapes
        self.n_features = {}  # number of features for each filter
        self.filter_names = []  # filter names

        self.shift = {}  # time shift of the design matrix
        self.filter_nonlinearity = {}
        self.output_nonlinearity = output_nonlinearity

        # Noise distribution, either gaussian or poisson
        self.distr = distr

        # others
        self.mle_computed = False
        self.lam = {}  # smoothness regularization weight
        self.scores = {}  # prediction error metric scores
        self.r2pseudo = {}
        self.corrcoef = {}
        self.dtype = dtype

    def fnl(self, x, kind, params=None):
        """
        Choose a fixed nonlinear function or fit a flexible one ('nonparametric').

        Parameters
        ----------

        x: jnp.array, (n_samples, )
            Sum of filter outputs.

        kind: str
            Choice of nonlinearity.

        params: None or jnp.array.
            For flexible nonlinearity. To be implemented.

        Return
        ------
            Transformed sum of filter outputs.
        """

        if kind == 'softplus':
            def softplus(x):
                return jnp.log(1 + jnp.exp(x))

            return softplus(x) + 1e-7

        elif kind == 'exponential':
            return jnp.exp(x)

        elif kind == 'softmax':
            def softmax(x):
                z = jnp.exp(x)
                return z / z.sum()

            return softmax(x)

        elif kind == 'sigmoid':
            def sigmoid(x):
                return 1 / (1 + jnp.exp(-x))

            return sigmoid(x)

        elif kind == 'tanh':
            return jnp.tanh(x)

        elif kind == 'relu':
            def relu(x):
                return jnp.where(x > 0., x, 1e-7)
            return relu(x)

        elif kind == 'leaky_relu':
            def leaky_relu(x):
                return jnp.where(x > 0., x, x * 0.01)
            return leaky_relu(x)
        elif kind == 'none':
            return x
        else:
            raise NotImplementedError(f'Input filter nonlinearity `{kind}` is not supported.')

    def add_design_matrix(self, X, dims=None, df=None, smooth=None, lag=True,
                          lam=0., filter_nonlinearity='none',
                          kind='train', name='stimulus', shift=0, burn_in=None):

        """
        Add input desgin matrix to the model.

        Parameters
        ----------

        X: jnp.array, shape=(n_samples, ) or (n_samples, n_pixels)
            Original input.

        dims: int, or list / jnp.array, shape=dim_t, or (dim_t, dim_x, dim_y)
            Filter shape.

        df: None, int, or list / jnp.array
            Number of spline bases. Should be the same shape as dims.

        smooth: None, or str
            Type of spline bases. If None, no basis is used.

        lag: bool
            If True, the design matrix will be build based on the dims[0].
            If False, a instantaneous RF will be fitted.

        filter_nonlinearity: str
            Nonlinearity for the stimulus filter.

        kind: str
            Datset type, should be one of `train` (training set),
            `dev` (validation set) or `test` (testing set).

        name: str
            Name of the corresponding filter.
            A receptive field (stimulus) filter should have `stimulus` in the name.
            A response-history filter should have `history` in the name.

        shift: int
            Time offset for the design matrix, positive number will shift the design
            matrix to the past, negative number will shift it to the future.

        burn_in: int or None
            Number of samples / frames to be ignore for prediction.
            (Because the first few frames in the design matrix are full of zero, which
            tend to predict poorly.)

        """

        # check X shape
        if len(X.shape) == 1:
            X = X[:, jnp.newaxis].astype(self.dtype)
        else:
            X = X.astype(self.dtype)

        if kind not in self.X:
            self.X.update({kind: {}})

        if kind == 'train':
            self.filter_nonlinearity[name] = filter_nonlinearity
            self.filter_names.append(name)

            dims = dims if type(dims) is not int else [dims, ]
            self.dims[name] = dims
            self.shift[name] = shift
        else:
            dims = self.dims[name]
            shift = self.shift[name]

        if not hasattr(self, 'burn_in'):  # if exists, ignore
            self.burn_in = dims[0] - 1 if burn_in is None else burn_in  # number of first few frames to ignore
            self.has_burn_in = True

        if lag:
            self.X[kind][name] = build_design_matrix(X, dims[0], shift=shift, dtype=self.dtype)[self.burn_in:]
        else:
            self.burn_in = 0
            self.X[kind][name] = X  # if not time lag, shouldn't it also be no burn in?
            # TODO: might need different handlings for instantenous RF.
            # conflict: history filter burned-in but the stimulus filter didn't

        if smooth is None:
            # if train set exists and used spline as basis
            # automatically apply the same basis for dev/test set
            if name in self.S:
                if kind not in self.XS:
                    self.XS.update({kind: {}})
                S = self.S[name]
                self.XS[kind][name] = self.X[kind][name] @ S

            elif kind == 'test':
                if kind not in self.XS:
                    self.XS.update({kind: {}})
                if hasattr(self, 'num_subunits') and self.num_subunits > 1:
                    S = self.S['stimulus_s0']
                else:
                    S = self.S[name]
                self.XS[kind][name] = self.X[kind][name] @ S

            else:
                if kind == 'train':
                    self.n_features[name] = self.X['train'][name].shape[1]
                    self.edf[name] = self.n_features[name]

        else:  # use spline
            if kind not in self.XS:
                self.XS.update({kind: {}})

            self.df[name] = df if type(df) is not int else [df, ]
            self.lam[name] = lam if type(lam) is list else [lam, ] * len(self.df[name])
            S, P = build_spline_matrix(dims, self.df[name], smooth, self.lam[name], return_P=True, dtype=self.dtype)
            self.S[name] = S
            self.P[name] = P  # penalty matrix, which absolved lamda already

            XS = self.X[kind][name] @ S
            self.XS[kind][name] = XS

            if kind == 'train':
                self.n_features[name] = self.XS['train'][name].shape[1]
                if (P == 0).all():
                    self.edf[name] = self.n_features[name]
                else:
                    edf = (XS.T * (jnp.linalg.inv(XS.T @ XS + P) @ XS.T)).sum()
                    self.edf[name] = edf

    def initialize(self, y=None, num_subunits=1, dt=0.033, method='random', compute_ci=True, random_seed=2046,
                   verbose=0, add_noise_to_mle=0):

        self.init_method = method  # store meta
        self.num_subunits = num_subunits
        self.compute_ci = compute_ci

        if method == 'random':

            self.b['random'] = {}
            self.w['random'] = {}
            self.intercept['random'] = {}
            if verbose:
                print('Initializing model parameters randomly...')

            for i, name in enumerate(self.filter_names):
                self.intercept['random'][name] = 0.
                key = random.PRNGKey(random_seed + i)  # change random seed for each filter
                if name in self.S:
                    self.b['random'][name] = random.normal(key, shape=(self.XS['train'][name].shape[1], 1)).astype(
                        self.dtype)
                    self.w['random'][name] = self.S[name] @ self.b['random'][name]
                else:
                    self.w['random'][name] = random.normal(key, shape=(self.X['train'][name].shape[1], 1)).astype(
                        self.dtype)
            self.intercept['random']['global'] = 0.

            if verbose:
                print('Finished.')

        elif method == 'mle':

            if verbose:
                print('Initializing model paraemters with maximum likelihood...')

            if not self.mle_computed:
                self.compute_mle(y)

            if verbose:
                print('Finished.')

        else:
            raise ValueError(f'`{method}` is not supported.')

        # rename and repmat: stimulus filter to subunits filters
        # subunit model only works with one stimulus.
        filter_names = self.filter_names.copy()
        if num_subunits != 1:
            filter_names.remove('stimulus')
            filter_names = [f'stimulus_s{i}' for i in range(num_subunits)] + filter_names

            for name in filter_names:
                if 'stimulus' in name:
                    self.dims[name] = self.dims['stimulus']
                    self.df[name] = self.dims['stimulus']
                    self.shift[name] = self.shift['stimulus']
                    self.filter_nonlinearity[name] = self.filter_nonlinearity['stimulus']
                    self.intercept[method][name] = self.intercept[method]['stimulus']
                    self.w[method][name] = self.w[method]['stimulus']

                    if method in self.w_se:
                        self.w_se[method][name] = self.w_se[method]['stimulus']
                    self.X['train'][name] = self.X['train']['stimulus']
                    self.edf[name] = self.edf['stimulus']

                    if 'dev' in self.X:
                        self.X['dev'][name] = self.X['dev']['stimulus']

                    if 'stimulus' in self.S:
                        self.b[method][name] = self.b[method]['stimulus']
                        if method in self.b_se:
                            self.b_se[method][name] = self.b_se[method]['stimulus']
                        self.XS['train'][name] = self.XS['train']['stimulus']

                        if 'dev' in self.XS:
                            self.XS['dev'][name] = self.XS['dev']['stimulus']

                        self.P[name] = self.P['stimulus']
                        self.S[name] = self.S['stimulus']

            try:
                self.b[method].pop('stimulus')
            except:
                pass

            self.w[method].pop('stimulus')
            self.intercept[method].pop('stimulus')
            self.X['train'].pop('stimulus')
            self.X['dev'].pop('stimulus')
            if self.XS != {}:
                self.XS['train'].pop('stimulus')
                self.XS['dev'].pop('stimulus')
                self.S.pop('stimulus')
                self.P.pop('stimulus')

            self.filter_names = filter_names

        self.p[method] = {}
        p0 = {}
        for i, name in enumerate(self.filter_names):
            if name in self.S:
                b = self.b[method][name]
                key = random.PRNGKey(random_seed + i)
                self.p[method].update({name: b})
                noise = add_noise_to_mle * random.normal(key, shape=b.shape).astype(self.dtype)
                p0.update({name: b + noise})

            else:
                w = self.w[method][name]
                key = random.PRNGKey(random_seed + i)
                self.p[method].update({name: w})
                noise = add_noise_to_mle * random.normal(key, shape=w.shape).astype(self.dtype)
                p0.update({name: w + noise})

            self.p[method].update({'intercept': self.intercept[method]})
            p0.update({'intercept': self.intercept[method]})

        self.dt = dt
        self.p0 = p0

    def compute_mle(self, y, compute_ci=True):

        """Compute maximum likelihood estimates.

        Parameter
        ---------

        y: jnp.array or dict, (n_samples)
            Response. if dict is
        """

        if not hasattr(self, 'compute_ci'):
            self.compute_ci = compute_ci

        if type(y) is dict:
            y_train = y['train']
            if len(y['train']) == 0:
                raise ValueError('Training set is empty after burned in.')
            if 'dev' in y:
                y_dev = y['dev']
        else:
            y = {'train': y}
            y_train = y['train']

        n_samples = len(y_train) - self.burn_in
        X = jnp.hstack([jnp.hstack(
            [jnp.ones(n_samples)[:, jnp.newaxis], self.XS['train'][name]]) if name in self.S else jnp.hstack(
            [jnp.ones(n_samples)[:, jnp.newaxis], self.X['train'][name]]) for name in self.filter_names])
        X = jnp.hstack([jnp.ones(n_samples)[:, jnp.newaxis], X])

        XtX = X.T @ X
        Xty = X.T @ y_train[self.burn_in:]

        mle = jnp.linalg.lstsq(XtX, Xty, rcond=None)[0]

        self.b['mle'] = {}
        self.w['mle'] = {}
        self.intercept['mle'] = {}

        # slicing the mle matrix into each filter
        l = jnp.cumsum(jnp.hstack([0, [self.n_features[name] + 1 for name in self.n_features]]))
        idx = [jnp.array((l[i], l[i + 1])) for i in range(len(l) - 1)]
        self.idx = idx

        for i, name in enumerate(self.filter_names):
            mle_params = mle[idx[i][0]:idx[i][1]][:, jnp.newaxis].astype(self.dtype)
            self.intercept['mle'][name] = mle_params[0]
            if name in self.S:
                self.b['mle'][name] = mle_params[1:]
                self.w['mle'][name] = self.S[name] @ self.b['mle'][name]
            else:
                self.w['mle'][name] = mle_params[1:]

        self.intercept['mle']['global'] = mle[0]

        self.p['mle'] = {}
        self.y_pred['mle'] = {}

        for name in self.filter_names:
            if name in self.S:
                self.p['mle'].update({name: self.b['mle'][name]})
            else:
                self.p['mle'].update({name: self.w['mle'][name]})

        self.p['mle']['intercept'] = self.intercept['mle']
        self.y['train'] = y_train[self.burn_in:]

        if len(self.y['train']) == 0:
            raise ValueError('Training set is empty after burned in.')

        self.y_pred['mle']['train'] = self.forwardpass(self.p['mle'], kind='train')

        # # get filter confidence interval
        if self.compute_ci:
            self._get_filter_variance(w_type='mle')
            self._get_response_variance(w_type='mle', kind='train')

        if type(y) is dict and 'dev' in y:
            self.y['dev'] = y_dev[self.burn_in:]
            if len(self.y['dev']) == 0:
                raise ValueError('Dev set is empty after burned in.')

            self.y_pred['mle']['dev'] = self.forwardpass(self.p['mle'], kind='dev')
            if self.compute_ci:
                self._get_response_variance(w_type='mle', kind='dev')

        self.mle_computed = True

    def forwardpass(self, p, kind):

        """Forward pass of the model.

        Parameters
        ----------

        p: dict
            A dictionary of the model parameters to be optimized.

        kind: str
            Dataset type, can be `train`, `dev` or `test`.
        """

        intercept = p['intercept']

        filters_output = []
        for name in self.X[kind]:
            if name in self.S:
                input_term = self.XS[kind][name]
            else:
                input_term = self.X[kind][name]

            output = self.fnl(jnp.squeeze(input_term @ p[name]) + intercept[name], kind=self.filter_nonlinearity[name])
            filters_output.append(output)

        filters_output = jnp.array(filters_output).sum(0)
        final_output = self.fnl(filters_output + intercept['global'], kind=self.output_nonlinearity).astype(self.dtype)
        return final_output

    def cost(self, p, kind='train', precomputed=None, penalize=True):

        """Cost function.

        Parameters
        ----------

        p: dict
            A dictionary of the model parameters to be optimized.

        kind: str
            Dataset type, can be `train`, `dev` or `test`.

        precomputed: None or jnp.array
            Precomputed forward pass output. For avoding duplicate computation.

        penalize : bool
            Add l1 and/or penality to loss

        """

        distr = self.distr
        y = self.y[kind]
        r = self.forwardpass(p, kind) if precomputed is None else precomputed

        # cost functions
        if distr == 'gaussian':
            loss = 0.5 * jnp.sum((y - r) ** 2)

        elif distr == 'poisson':

            r = jnp.where(r != jnp.inf, r, 0.)  # remove inf
            r = jnp.maximum(r, 1e-20)  # remove zero to avoid nan in log.

            term0 = - jnp.log(r) @ y  # spike term from poisson log-likelihood
            term1 = jnp.sum(r)  # non-spike term
            loss = term0 + term1
        else:
            raise NotImplementedError(distr)

        # regularization: elasticnet
        if penalize and (hasattr(self, 'beta') or self.beta != 0) and kind == 'train':
            # regularized all filters parameters
            w = jnp.hstack([p[name].flatten() for name in self.filter_names])

            l1 = jnp.linalg.norm(w, 1)
            l2 = jnp.linalg.norm(w, 2)
            loss += self.beta * ((1 - self.alpha) * l2 + self.alpha * l1)

        return jnp.squeeze(loss)

    @staticmethod
    def print_progress(i, time_elapsed, c_train=None, c_dev=None, m_train=None, m_dev=None):
        opt_info = f"{i}".ljust(13) + f"{time_elapsed:>.3f}".ljust(13)
        if c_train is not None and np.isfinite(c_train):
            opt_info += f"{c_train:.3g}".ljust(16)
        if c_dev is not None and np.isfinite(c_dev):
            opt_info += f"{c_dev:.3g}".ljust(16)
        if m_train is not None and np.isfinite(m_train):
            opt_info += f"{m_train:.3g}".ljust(16)
        if m_dev is not None and np.isfinite(m_dev):
            opt_info += f"{m_dev:.3g}".ljust(16)
        print(opt_info)

    @staticmethod
    def print_progress_header(c_train=False, c_dev=False, m_train=False, m_dev=False):
        opt_title = "Iters".ljust(13) + "Time (s)".ljust(13)
        if c_train:
            opt_title += "Cost (train)".ljust(16)
        if c_dev:
            opt_title += "Cost (dev)".ljust(16)
        if m_train:
            opt_title += "Metric (train)".ljust(16)
        if m_dev:
            opt_title += "Metric (dev)".ljust(16)
        print(opt_title)

    def optimize(self, p0, num_iters, metric, step_size, tolerance, verbose, return_model):

        """Workhorse of optimization.

        p0: dict
            A dictionary of the initial model parameters to be optimized.

        num_iters: int
            Maximum number of iteration.

        metric: str
            Method of model evaluation. Can be
            `mse`, `corrcoeff`, `r2`


        step_size: float or jax scheduler
            Learning rate.

        tolerance: int
            Tolerance for early stop. If the training cost doesn't change more than 1e-5
            in the last (tolerance) steps, or the dev cost monotonically increase, stop.

        verbose: int
            Print progress. If verbose=0, no progress will be print.

        return_model: str
            Return the 'best' model on dev set metrics or the 'last' model.
        """

        @jit
        def step(_i, _opt_state):
            p = get_params(_opt_state)
            l, g = value_and_grad(self.cost)(p)
            return l, opt_update(_i, g, _opt_state)

        opt_init, opt_update, get_params = optimizers.adam(step_size=step_size)
        opt_state = opt_init(p0)

        cost_train = np.full(num_iters, np.nan)
        cost_dev = np.full(num_iters, np.nan)
        metric_train = np.full(num_iters, np.nan)
        metric_dev = np.full(num_iters, np.nan)
        params_list = []

        extra = 'dev' in self.y

        if verbose:
            self.print_progress_header(
                c_train=True, c_dev=extra, m_train=metric is not None, m_dev=metric is not None and extra)

        time_start = time.time()
        i = 0

        for i in range(num_iters):
            cost_train[i], opt_state = step(i, opt_state)

            params = get_params(opt_state)
            params_list.append(params)

            y_pred_train = self.forwardpass(p=params, kind='train')
            metric_train[i] = self.compute_score(self.y['train'], y_pred_train, metric)

            if 'dev' in self.y:
                y_pred_dev = self.forwardpass(p=params, kind='dev')
                cost_dev[i] = self.cost(p=params, kind='dev', precomputed=y_pred_dev, penalize=False)
                metric_dev[i] = self.compute_score(self.y['dev'], y_pred_dev, metric)

            time_elapsed = time.time() - time_start
            if verbose:
                if i % int(verbose) == 0:
                    self.print_progress(
                        i, time_elapsed, c_train=cost_train[i], c_dev=cost_dev[i],
                        m_train=metric_train[i], m_dev=metric_dev[i])

            if tolerance and i > 300:  # tolerance = 0: no early stop.

                total_time_elapsed = time.time() - time_start

                if 'dev' in self.y and np.all(np.diff(cost_dev[i - tolerance:i]) > 0):
                    stop = 'dev_stop'
                    if verbose:
                        print('Stop at {0} steps: cost (dev) has been monotonically increasing for {1} steps.'.format(
                            i, tolerance))
                        print('Total time elapsed: {0:.3f}s.\n'.format(total_time_elapsed))
                    break

                if np.all(np.diff(cost_train[i - tolerance:i]) < 1e-5):
                    stop = 'train_stop'
                    if verbose:
                        print(
                            'Stop at {0} steps: cost (train) has been changing less than 1e-5 for {1} steps.'.format(
                                i, tolerance))
                        print('Total time elapsed: {0:.3f}s.\n'.format(total_time_elapsed))
                    break

        else:
            total_time_elapsed = time.time() - time_start
            stop = 'maxiter_stop'
            if verbose:
                print('Stop: reached {0} steps.'.format(num_iters))
                print('Total time elapsed: {0:.3f}s.\n'.format(total_time_elapsed))

        if return_model == 'best_dev_cost':
            best = np.argmin(cost_dev[:i + 1])

        elif return_model == 'best_train_cost':
            best = np.argmin(cost_train[:i + 1])

        elif return_model == 'best_dev_metric':
            if metric in ['mse', 'gcv']:
                best = np.argmin(metric_dev[:i + 1])
            else:
                best = np.argmax(metric_dev[:i + 1])

        elif return_model == 'best_train_metric':
            if metric in ['mse', 'gcv']:
                best = np.argmin(metric_train[:i + 1])
            else:
                best = np.argmax(metric_train[:i + 1])

        elif return_model == 'last':
            if stop == 'dev_stop':
                best = i - tolerance
            else:
                best = i

        else:
            print('Provided `return_model` is not supported. Fell back to `best_dev_cost`')
            best = np.argmin(cost_dev[:i + 1])

        params = params_list[best]
        metric_dev_opt = metric_dev[best]

        self.cost_train = cost_train[:i + 1]
        self.cost_dev = cost_dev[:i + 1]
        self.metric_train = metric_train[:i + 1]
        self.metric_dev = metric_dev[:i + 1]
        self.metric_dev_opt = metric_dev_opt
        self.total_time_elapsed = total_time_elapsed

        self.all_params = params_list[:i + 1]  # not sure if this will occupy a lot of RAM.

        self.y_pred['opt'].update({'train': y_pred_train})
        if 'dev' in self.y:
            self.y_pred['opt'].update({'dev': y_pred_dev})

        return params

    def fit(self, y=None, num_iters=3, alpha=1, beta=0.01, metric='corrcoef', step_size=1e-3,
            tolerance=10, verbose=True, return_model='best_dev_cost'):
        """
        Fit model.

        Parameters
        ----------

        y: jnp.array, (n_samples)
            Response.

        num_iters: int
            Maximum number of iteration.

        alpha: float
            Balance weight for L1 and L2 regularization.
            If alpha=1, only L1 applys. Otherwise, only L2 apply.

        beta: float
            Overall weight for L1 and L2 regularization.

        metric: str
            Method of model evaluation. Can be
            `mse`, `corrcoeff`, `r2`

        step_size: float or jax scheduler
            Learning rate.

        tolerance: int
            Tolerance for early stop. If the training cost doesn't change more than 1e-5
            in the last (tolerance) steps, or the dev cost monotonically increase, stop.

        verbose: int
            Print progress. If verbose=0, no progress will be print.

        return_model : str
            Model to be returned

        """

        self.alpha = alpha
        self.beta = beta
        self.metric = metric

        if not 'dev' in self.y:
            return_model = 'last'

        if y is None:
            if not 'train' in self.y:
                raise ValueError(f'No `y` is provided.')
        else:
            if type(y) is dict:
                self.y['train'] = y['train'][self.burn_in:].astype(self.dtype)
                if 'dev' in y:
                    self.y['dev'] = y['dev'][self.burn_in:].astype(self.dtype)
            else:
                self.y['train'] = y[self.burn_in:].astype(self.dtype)

        self.y_pred['opt'] = {}

        self.p['opt'] = self.optimize(self.p0, num_iters, metric, step_size, tolerance, verbose, return_model)
        self._extract_opt_params()

    def _extract_opt_params(self):
        self.b['opt'] = {}
        self.w['opt'] = {}
        for name in self.filter_names:
            if name in self.S:
                self.b['opt'][name] = self.p['opt'][name]
                self.w['opt'][name] = self.S[name] @ self.b['opt'][name]
            else:
                self.w['opt'][name] = self.p['opt'][name]

        self.intercept['opt'] = self.p['opt']['intercept']
        # get filter confidence interval
        if self.compute_ci:
            self._get_filter_variance(w_type='opt')
            # get prediction confidence interval
            self._get_response_variance(w_type='opt', kind='train')
            if 'dev' in self.y:
                self._get_response_variance(w_type='opt', kind='dev')

    def predict(self, X, w_type='opt'):
        """
        Prediction on Test set.

        Parameters
        ----------

        X: jnp.array or dict
            Stimulus. Only the named filters in the dict will be used for prediction.
            Other filters, even trained, will be ignored if no test set provided.

        w_type: str
            either `opt` or `mle`

        Note
        ----
        Use self.forwardpass() for prediction on Training / Dev set.
        """

        p = self.p[w_type]

        self.X['test'] = {}
        self.XS['test'] = {}

        if type(X) is dict:
            for name in X:
                self.add_design_matrix(X[name], dims=self.dims[name], shift=self.shift[name], name=name, kind='test')
        else:
            # if X is jnp.array, assumed it's the stimulus.
            self.add_design_matrix(X, dims=self.dims['stimulus'], shift=self.shift['stimulus'], name='stimulus',
                                   kind='test')

        # rename and repmat for test set
        if self.num_subunits != 1:
            for name in self.filter_names:
                if 'stimulus' in name:
                    self.X['test'][name] = self.X['test']['stimulus']
                    self.XS['test'][name] = self.XS['test']['stimulus']
            self.X['test'].pop('stimulus')
            self.XS['test'].pop('stimulus')

        y_pred = self.forwardpass(p, kind='test')
        if self.compute_ci:
            self._get_response_variance(w_type=w_type, kind='test')

        return y_pred

    def compute_score(self, y, y_pred, metric):
        """
        Metric score for evaluating model prediction.
        """

        if metric == 'r2':
            return r2(y, y_pred)
        elif metric == 'r2adj':
            return r2adj(y, y_pred, p=self.edf_tot)

        elif metric == 'mse':
            return mse(y, y_pred)

        elif metric == 'corrcoef':
            return corrcoef(y, y_pred)

        elif metric == 'gcv':
            return gcv(y, y_pred, edf=self.edf_tot)

        else:
            print(f'Metric `{metric}` is not supported.')

    def score(self, X_test, y_test, metric='corrcoef', w_type='opt', return_prediction=False):
        """
        Metric score for evaluating model prediction.

        X_test: jnp.array or dict
            Stimulus. Only the named filters in the dict will be used for prediction.
            Other filters, even trained, will be ignored if no test set provided.

        y_test: jnp.array
            Response.

        metric: str
            Method of model evaluation. Can be
            `mse`, `corrcoeff`, `r2`

        return_prediction: bool
            If true, will also return the predicted response `y_pred`.

        Returns
        -------
        s: float
            Metric score.

        y_pred: jnp.array.
            The predicted response. Optional.
        """

        y_test = y_test[self.burn_in:].flatten()

        if type(X_test) is dict:
            y_pred = self.predict(X_test, w_type)
        else:
            y_pred = self.predict({'stimulus': X_test}, w_type)

        s = self.compute_score(y_test, y_pred, metric)

        if return_prediction:
            return s, y_pred
        else:
            return s

    def _get_filter_variance(self, w_type='opt'):
        """
        Compute the variance and standard error of the weight of each filters.
        """

        P = self.P
        S = self.S
        XS = self.XS.get('train', None)
        X = self.X['train']

        edf = self.edf

        if self.distr == 'gaussian':

            y = self.y['train']
            y_pred = self.y_pred[w_type]['train']
            rsd = y - y_pred  # residuals
            rss = jnp.sum(rsd ** 2)  # residul sum of squares
            rss_var = {name: rss / (len(y) - edf[name]) for name in self.filter_names}

            V = {}
            b_se = {}
            w_se = {}
            for name in self.filter_names:
                if name in S:
                    # check sample size
                    if len(XS[name]) < self.edf[name]:
                        print('Sample size is too small for getting reasonable confidence interval.')
                    # compute weight covariance
                    try:
                        V[name] = jnp.linalg.inv(XS[name].T @ XS[name] + P[name]) * rss_var[name]
                    except:
                        # if inv failed, use pinv
                        V[name] = jnp.linalg.pinv(XS[name].T @ XS[name] + P[name]) * rss_var[name]

                        # remove negative correlation?
                    # https://math.stackexchange.com/q/4018326
                    V[name] = jnp.abs(V[name])

                    b_se[name] = jnp.sqrt(jnp.diag(V[name]))
                    w_se[name] = S[name] @ b_se[name]

                else:
                    if len(X[name]) < jnp.product(jnp.array(self.dims[name])):
                        print('Sample size is too small for getting reasonable confidence interval.')
                    V[name] = jnp.linalg.inv(X[name].T @ X[name]) * rss_var[name]
                    V[name] = jnp.abs(V[name])
                    w_se[name] = jnp.sqrt(jnp.diag(V[name]))

        else:

            b = {}
            w = {}
            u = {}
            U = {}
            V = {}
            w_se = {}
            b_se = {}
            for name in self.filter_names:
                if name in S:
                    # check sample size
                    if len(XS[name]) < self.edf[name]:
                        print('Sample size is too small for getting reasonable confidence interval.')

                    b[name] = self.b[w_type][name]
                    u[name] = self.fnl(XS[name] @ b[name], self.filter_nonlinearity[name])
                    U[name] = 1 / self.fnl(u[name], self.output_nonlinearity).flatten() ** 2

                    try:
                        V[name] = jnp.linalg.inv(XS[name].T * U[name] @ XS[name] + P[name])
                    except:
                        V[name] = jnp.linalg.pinv(XS[name].T * U[name] @ XS[name] + P[name])

                    V[name] = jnp.abs(V[name])
                    b_se[name] = jnp.sqrt(jnp.diag(V[name]))
                    w_se[name] = S[name] @ b_se[name]
                else:

                    if len(X[name]) < jnp.product(jnp.array(self.dims[name])):
                        print('Sample size is too small for getting reasonable confidence interval.')

                    w[name] = self.w[w_type][name]
                    u[name] = self.fnl(X[name] @ w[name], self.filter_nonlinearity[name])
                    U[name] = 1 / self.fnl(u[name], self.output_nonlinearity).flatten() ** 2

                    try:
                        V[name] = jnp.linalg.inv(X[name].T * U[name] @ X[name])
                    except:
                        V[name] = jnp.linalg.pinv(X[name].T * U[name] @ X[name])
                    V[name] = jnp.abs(V[name])
                    w_se[name] = jnp.sqrt(jnp.diag(V[name]))

        self.V[w_type] = V
        self.b_se[w_type] = b_se
        self.w_se[w_type] = w_se

    def _get_response_variance(self, w_type='opt', kind='train'):

        """
        Compute the variance and standard error of the predicted response.
        """

        P = self.P
        S = self.S
        X = self.X[kind]
        if kind in self.XS:
            XS = self.XS[kind]
        b = self.b[w_type]
        w = self.w[w_type]
        V = self.V[w_type]

        y_se = {}
        y_pred_filters = {}
        y_pred_filters_upper = {}
        y_pred_filters_lower = {}
        intercept = self.intercept[w_type]
        for name in X:
            if name in S:
                y_se[name] = jnp.sqrt(jnp.sum(XS[name] @ V[name] * XS[name], 1))
                y_pred_filters[name] = self.fnl((XS[name] @ b[name] + intercept[name]).flatten(),
                                                kind=self.filter_nonlinearity[name])

            else:
                y_se[name] = jnp.sqrt(jnp.sum(self.X[kind][name] @ V[name] * self.X[kind][name], 1))
                y_pred_filters[name] = self.fnl((X[name] @ w[name] + intercept[name]).flatten(),
                                                kind=self.filter_nonlinearity[name])

            y_pred_filters_upper[name] = self.fnl((X[name] @ w[name] + intercept[name]).flatten() + 2 * y_se[name],
                                                  kind=self.filter_nonlinearity[name])
            y_pred_filters_lower[name] = self.fnl((X[name] @ w[name] + intercept[name]).flatten() - 2 * y_se[name],
                                                  kind=self.filter_nonlinearity[name])

        y_pred = self.fnl(jnp.array([y_pred_filters[name] for name in X]).sum(0) + intercept['global'],
                          kind=self.output_nonlinearity)
        y_pred_upper = self.fnl(jnp.array([y_pred_filters_upper[name] for name in X]).sum(0) + intercept['global'],
                                kind=self.output_nonlinearity)
        y_pred_lower = self.fnl(jnp.array([y_pred_filters_lower[name] for name in X]).sum(0) + intercept['global'],
                                kind=self.output_nonlinearity)

        if w_type not in self.y_pred_lower:
            self.y_pred_lower[w_type] = {}
            self.y_pred_upper[w_type] = {}

        self.y_pred[w_type][kind] = y_pred
        self.y_pred_upper[w_type][kind] = y_pred_upper
        self.y_pred_lower[w_type][kind] = y_pred_lower

