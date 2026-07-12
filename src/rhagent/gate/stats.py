"""Pure statistics for the out-of-sample gate."""

from __future__ import annotations

import math

_EULER_GAMMA = 0.5772156649015329

_A = [
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
]
_B = [
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
]
_C = [
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
]
_D = [
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
]


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_ppf(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p}")

    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (
            (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
            / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
        )
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (
            (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
            * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
        )

    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
        / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    )


def bonferroni(icir: float, n_eff: int, n_tested: int, alpha: float = 0.05):
    t_stat = abs(icir) * math.sqrt(n_eff)
    p_value = 2.0 * (1.0 - norm_cdf(t_stat))
    threshold = alpha / max(n_tested, 1)
    return p_value, threshold, p_value < threshold


def deflated_sharpe(
    sr: float,
    n_eff: int,
    skew: float,
    kurt_excess: float,
    n_trials: int,
    var_trials: float,
) -> float:
    if n_eff <= 1:
        return 0.0

    if n_trials >= 2 and var_trials > 0:
        sr0 = math.sqrt(var_trials) * (
            (1.0 - _EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n_trials)
            + _EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        )
    else:
        sr0 = 0.0

    gamma4 = kurt_excess + 3.0
    denom = 1.0 - skew * sr + (gamma4 - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return 0.0

    z = (sr - sr0) * math.sqrt(n_eff - 1.0) / math.sqrt(denom)
    return norm_cdf(z)
