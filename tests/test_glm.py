import numpy as np

from generate_data import generate_small_rf_and_data
from rfest import GLM
from rfest.utils import split_data


def test_glm_2d():
    w_true, X, y, dims, dt = generate_small_rf_and_data(noise='white')

    dims = (3,) + X.shape[1:]
    df = tuple([int(np.maximum(dim / 3, 3)) for dim in dims])

    model = GLM(distr='gaussian', output_nonlinearity='none')
    model.add_design_matrix(X, dims=dims, df=df, smooth='cr', kind='train', filter_nonlinearity='none',
                            name='stimulus')

    model.initialize(num_subunits=1, dt=dt, method='mle', random_seed=42, compute_ci=False, y=y)
    model.fit(y={'train': y}, num_iters=200, verbose=0, step_size=0.1, beta=0.1, metric='corrcoef')

    assert model.score(X, y, metric='corrcoef') > -0.5


def test_glm_2d_split_data():
    w_true, X, y, dims, dt = generate_small_rf_and_data(noise='white')
    (X_train, y_train), (X_dev, y_dev), (_, _) = split_data(X, y, dt, frac_train=0.8, frac_dev=0.2)

    dims = (3,) + X.shape[1:]
    df = tuple([int(np.maximum(dim / 3, 3)) for dim in dims])

    model = GLM(distr='gaussian', output_nonlinearity='none')
    model.add_design_matrix(X_train, dims=dims, df=df, smooth='cr', kind='train', filter_nonlinearity='none',
                            name='stimulus')
    model.add_design_matrix(X_dev, dims=dims, df=df, name='stimulus', kind='dev')

    model.initialize(num_subunits=1, dt=dt, method='mle', random_seed=42, compute_ci=False, y=y_train)
    model.fit(
        y={'train': y_train, 'dev': y_dev}, num_iters=200, verbose=100, step_size=0.1, beta=0.1, metric='corrcoef')

    assert model.score(X_train, y_train, metric='corrcoef') > -0.5
    assert model.score(X_dev, y_dev, metric='corrcoef') > -0.5


def test_glm_2d_split_dat_test():
    w_true, X, y, dims, dt = generate_small_rf_and_data(noise='white')
    (X_train, y_train), (X_dev, y_dev), (X_test, y_test) = split_data(X, y, dt, frac_train=0.6, frac_dev=0.2)

    dims = (3,) + X.shape[1:]
    df = tuple([int(np.maximum(dim / 3, 3)) for dim in dims])

    model = GLM(distr='gaussian', output_nonlinearity='none')
    model.add_design_matrix(X_train, dims=dims, df=df, smooth='cr', kind='train', filter_nonlinearity='none',
                            name='stimulus')
    model.add_design_matrix(X_dev, dims=dims, df=df, name='stimulus', kind='dev')

    model.initialize(num_subunits=1, dt=dt, method='mle', random_seed=42, compute_ci=False, y=y_train)
    model.fit(
        y={'train': y_train, 'dev': y_dev}, num_iters=200, verbose=100, step_size=0.1, beta=0.1, metric='corrcoef')

    assert model.score(X_train, y_train, metric='corrcoef') > -0.5
    assert model.score(X_dev, y_dev, metric='corrcoef') > -0.5
    assert model.score(X_test, y_test, metric='corrcoef') > -0.5


def test_glm_2d_outputnl():
    w_true, X, y, dims, dt = generate_small_rf_and_data(noise='white')

    dims = (3,) + X.shape[1:]
    df = tuple([int(np.maximum(dim / 3, 3)) for dim in dims])

    model = GLM(distr='gaussian', output_nonlinearity='exponential')
    model.add_design_matrix(X, dims=dims, df=df, smooth='cr', kind='train', filter_nonlinearity='none',
                            name='stimulus')

    model.initialize(num_subunits=1, dt=dt, method='mle', random_seed=42, compute_ci=False, y=y)
    model.fit(y={'train': y}, num_iters=200, verbose=0, step_size=0.1, beta=0.1, metric='corrcoef')

    assert model.score(X, y, metric='corrcoef') > -0.5

