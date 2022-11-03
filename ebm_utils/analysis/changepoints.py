"""
Utilities for finding discontinuities and non-monotonicities.
"""

import numpy as np
import pandas as pd
from sdt.changepoint import BayesOffline

from ebm_utils.analysis.embeddings import calc_embeddings
from ebm_utils.fit import fit_ebm


def find_non_monotonicities(
    X_train, Y_train, ebm_constructor_kwargs=None, ebm_fit_kwargs=None, **kwargs
):
    """Find non-monotonicities in a DataFrame of predictors and outcomes."""
    ebm = fit_ebm(X_train, Y_train, ebm_constructor_kwargs, ebm_fit_kwargs)
    return find_non_monotonicities_from_ebm(ebm.explain_global(), X_train, **kwargs)


def find_discontinuities(
    X_train, Y_train, ebm_constructor_kwargs=None, ebm_fit_kwargs=None, **kwargs
):
    """Find non-monotonicities in a DataFrame of predictors and outcomes."""
    ebm = fit_ebm(X_train, Y_train, ebm_constructor_kwargs, ebm_fit_kwargs)
    return find_discontinuities_from_ebm(
        ebm.explain_global(), X_train, Y_train, **kwargs
    )


def calc_slopes(x_arr, y_arr):
    """
    Calculate slopes from x and y arrays.
    """
    np.seterr(invalid="ignore")  # Silence the divide by 0 err.
    slopes = (y_arr[1:] - y_arr[:-1]) / (x_arr[1:] - x_arr[:-1])
    slopes[~np.isfinite(slopes)] = 0.0
    np.seterr(invalid="warn")
    return slopes


def calculate_slopes(ebm_global, data_df):
    """Calculate slopes."""
    all_slopes = []
    embeddings = calc_embeddings(ebm_global, data_df.values)
    for j, predictor in enumerate(data_df.columns):
        order = np.argsort(data_df[predictor].values)
        sorted_x = data_df[predictor].values[order]
        sorted_y = embeddings[:, j][order]
        slopes = calc_slopes(sorted_x, sorted_y)
        all_slopes.append(slopes)
    return np.array(all_slopes).T


def is_counter_causal(my_slopes, changepoint):
    """
    Check whether the changepoint is "counter-causal", where
    it's not counter-causal for low risk -> high risk.
    but is counter-causal for high risk -> low risk.
    """
    prev_changepoint = np.max([0, changepoint - 1])
    next_changepoint = np.min([changepoint + 1, len(my_slopes) - 1])
    prev_slopes = np.mean(my_slopes[prev_changepoint:changepoint])
    next_slopes = np.mean(my_slopes[changepoint:next_changepoint])
    return prev_slopes > next_slopes


def find_non_monotonicities_from_ebm(
    ebm_global, data_df, prob_threshold=0.2, counter_causal_only=False
):
    """
    Find Non-Monotonicities from a trained EBM.
    """
    slopes = calculate_slopes(ebm_global, data_df)
    results = []
    det = BayesOffline("const", "gauss")
    for j, predictor in enumerate(data_df.columns):
        # Find where slope goes from consistent positive to consistent negative.
        order = np.argsort(data_df[predictor].values)
        my_slopes = slopes[:, j]
        nonzero_idx = np.where(np.abs(my_slopes) > 1e-8)[0]
        sorted_x_nz = data_df[predictor].values[order][nonzero_idx]
        my_slopes = my_slopes[nonzero_idx]
        my_slopes /= np.abs(my_slopes)  # Only looking for sign, not magnitude
        if len(my_slopes) < 3:
            continue
        for changepoint in det.find_changepoints(
            my_slopes, prob_threshold=prob_threshold
        ):
            if counter_causal_only and not is_counter_causal(my_slopes, changepoint):
                continue
            results.append([predictor, sorted_x_nz[changepoint]])
    results_df = pd.DataFrame(
        np.array(results),
        columns=["Feature", "Value"],
    )
    return results_df


def evaluate_discontinuity_sample(x_val, y_vals, y_true, idx_before_mid, slope):
    """
    Evaluate a single discontinuity for a single sample.
    """
    [begin_y_val, end_y_val] = y_vals
    if idx_before_mid:
        disc_val = begin_y_val
    else:
        disc_val = end_y_val
    cont_val = begin_y_val + x_val * slope
    if y_true == 0.0:
        log_p_diff = cont_val - disc_val
    else:
        log_p_diff = disc_val - cont_val
    return log_p_diff


def evaluate_discontinuity(sorted_x, sorted_y, y_true, changepoints):
    """
    Evaluate a single discontinuity.
    """
    [prev_changepoint, changepoint, next_changepoint] = changepoints
    assert changepoint > 0
    begin_y_val = sorted_y[prev_changepoint]
    end_y_val = sorted_y[next_changepoint]
    begin_x_val = sorted_x[prev_changepoint]
    end_x_val = sorted_x[next_changepoint]
    log_p_diff = 0.0
    cont_slope = (end_y_val - begin_y_val) / (end_x_val - begin_x_val)
    for sample in range(prev_changepoint, next_changepoint):
        log_p_diff += evaluate_discontinuity_sample(
            sorted_x[sample] - begin_x_val,
            [begin_y_val, end_y_val],
            sample <= changepoint,
            cont_slope,
            y_true[sample],
        )
    return log_p_diff


def find_discontinuities_in_sorted(sorted_x, sorted_y, y_true, min_samples=100):
    """
    Find discontinuities in a stream of xs and ys, evaluated based on how it
    changes the likelihood of y_true.
    """
    discontinuities = []
    slopes = calc_slopes(sorted_x, sorted_y)
    changepoints = np.where(np.abs(slopes) > 0)[0]  # sample idxs
    for i, changepoint in enumerate(changepoints):
        # Compare probability of true y under discontinuity vs under continuous version.
        # prev_changepoint = np.max([0, changepoints[i - 1]])
        if i == 0:
            continue
        prev_changepoint = changepoints[i - 1]
        try:
            next_changepoint = changepoints[i + 1]
        except IndexError:
            next_changepoint = len(slopes) - 1
        # next_changepoint = np.min([changepoints[i + 1], len(slopes) - 1])
        log_p_diff = evaluate_discontinuity(
            sorted_x,
            sorted_y,
            y_true,
            [prev_changepoint, changepoint, next_changepoint],
        )
        n_samples = next_changepoint - prev_changepoint
        if log_p_diff > 0 and n_samples > min_samples:
            discontinuities.append(
                np.array(
                    [
                        sorted_x[changepoint],
                        n_samples,
                        np.exp(log_p_diff / n_samples),
                        log_p_diff,
                    ]
                )
            )
    return np.array(discontinuities)


def find_discontinuities_from_ebm(ebm_global, data_df, y_true, min_samples=100):
    """FInd Discontinuities in the EBM components.
    Return a DataFrame of results.
    """
    embeddings = calc_embeddings(ebm_global, data_df.values)
    discontinuities = []
    for j, predictor in enumerate(data_df.columns):
        order = np.argsort(data_df[predictor].values)
        sorted_x = data_df[predictor].values[order]
        sorted_y = embeddings[:, j][order]
        feature_discontinuities = find_discontinuities_in_sorted(
            sorted_x, sorted_y, y_true, min_samples
        )
        if len(feature_discontinuities) == 0:
            continue
        discontinuities.extend(
            np.hstack(
                (
                    np.expand_dims(
                        np.array(
                            [predictor for _ in range(len(feature_discontinuities))]
                        ),
                        1,
                    ),
                    feature_discontinuities,
                )
            ).tolist()
        )
    discontinuities_df = pd.DataFrame(
        np.array(discontinuities),
        columns=["Feature", "Value", "# Samples", "Effect Size", "P-Ratio"],
    )
    return discontinuities_df.sort_values("P-Ratio", ascending=False)
