"""
Scheduled batch job: for every (country, sector) pair in common.seed.COUNTRY_SECTOR_PAIRS,
joins fact_macro_indicator (pivoted wide by indicator) with fact_commodity_price and
fact_equity_valuation on date_key, then runs Pearson correlation, partial correlation,
and OLS multiple regression — for both rate bases (nominal/real) and both targets
(equity_valuation/commodity_price), sweeping lags t, t+1, t+2, t+3 and keeping the
strongest-fitting lag per combination. Results are appended to fact_correlation_result
(never overwritten, so estimates form a time series and exports stay reproducible).

Two interchangeable statistics engines, selected by STATS_ENGINE:
  numpy    (default) closed-form NumPy/SciPy kernels — 2× faster, and keeps
           Pingouin's statsmodels/scikit-learn/matplotlib/seaborn dependency
           tree out of the deployed images.
  pingouin pg.corr / pg.partial_corr / pg.linear_regression, the reference
           implementation (Vallat, 2018); requires requirements-notebooks.txt.
Both give the same r, partial r, p and R² to ~1e-14 (asserted in
notebooks/02_correlation_regression.ipynb).

Run directly: `python -m analysis.correlation_regression`
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats

from common.db import get_store, new_id
from common.seed import COUNTRY_SECTOR_PAIRS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("correlation_regression")

RATE_INDICATORS = {"nominal": "policy_rate", "real": "real_interest_rate"}
# Every country-level control this project ingests (see common/schema.py's INDICATORS).
# The three FRED-sourced global benchmarks (fed_funds_rate_global, usd_index,
# treasury_yield_10y) are deliberately excluded here: build_country_panel joins
# fact_macro_indicator filtered to a single country_code, and those three are
# stored with country_code IS NULL, so they never appear in a country panel —
# a real, separate gap from the "which control is best" question this module
# answers, not something papered over by silently including them.
CANDIDATE_CONTROLS = ["fdi_net_inflow", "cpi_inflation", "gdp_growth", "unemployment_rate", "trade_balance", "reer", "gov_debt_pct_gdp"]
LAGS = [0, 1, 2, 3]
MIN_OBS = 8  # below this, correlation/regression output is not meaningful
STATS_ENGINE = os.environ.get("STATS_ENGINE", "numpy").lower()
if STATS_ENGINE not in ("numpy", "pingouin"):
    raise ValueError(f"STATS_ENGINE must be 'numpy' or 'pingouin', got {STATS_ENGINE!r}")


def _in_list(values) -> str:
    # Values come from common.seed / the API allowlist, never free text.
    return ", ".join(f"'{v}'" for v in values)


def _fetch_frames(store, country_codes, sector_ids) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Three queries total, however many pairs — on BigQuery each query is a
    round-trip plus a 10 MB minimum billed scan, so the batch job fetching
    once and slicing in pandas beats three queries per pair."""
    macro = store.query_df(
        "SELECT country_code, date_key, indicator_id, value_change FROM fact_macro_indicator "
        f"WHERE country_code IN ({_in_list(country_codes)})"
    )
    price = store.query_df(
        f"SELECT sector_id, date_key, price_change FROM fact_commodity_price WHERE sector_id IN ({_in_list(sector_ids)})"
    )
    valuation = store.query_df(
        f"SELECT sector_id, date_key, valuation_change FROM fact_equity_valuation WHERE sector_id IN ({_in_list(sector_ids)})"
    )
    return macro, price, valuation


def _country_wide(macro: pd.DataFrame) -> pd.DataFrame:
    """One country's long macro rows -> one column per indicator, keyed by date_key."""
    return macro.pivot_table(index="date_key", columns="indicator_id", values="value_change", aggfunc="first").reset_index()


def _sector_targets(price: pd.DataFrame, valuation: pd.DataFrame, sector_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    price = price.loc[price["sector_id"] == sector_id, ["date_key", "price_change"]]
    valuation = valuation.loc[valuation["sector_id"] == sector_id, ["date_key", "valuation_change"]]
    return (price.rename(columns={"price_change": "commodity_price_change"}),
            valuation.rename(columns={"valuation_change": "equity_valuation_change"}))


def _join_panel(wide: pd.DataFrame, price: pd.DataFrame, valuation: pd.DataFrame) -> pd.DataFrame:
    panel = wide.merge(price, on="date_key", how="left").merge(valuation, on="date_key", how="left")
    panel = panel.sort_values("date_key").reset_index(drop=True)
    panel["time_index"] = range(len(panel))
    return panel


def build_country_panel(store, country_code: str, sector_id: str) -> pd.DataFrame:
    macro, price, valuation = _fetch_frames(store, [country_code], [sector_id])
    if macro.empty:
        return pd.DataFrame()
    return _join_panel(_country_wide(macro), *_sector_targets(price, valuation, sector_id))


def load_panels(store, pairs) -> dict[tuple[str, str], pd.DataFrame]:
    """Every pair's panel from one bulk fetch (see _fetch_frames). Pivots once
    per country and slices once per sector — pairs share both — instead of
    re-scanning the full macro table for each of the ~10^3 pairs."""
    macro, price, valuation = _fetch_frames(store, sorted({c for c, _ in pairs}), sorted({s for _, s in pairs}))
    wide = {cc: _country_wide(g) for cc, g in macro.groupby("country_code", sort=False)}
    targets = {sid: _sector_targets(price, valuation, sid) for sid in {s for _, s in pairs}}
    return {(c, s): _join_panel(wide[c], *targets[s]) if c in wide else pd.DataFrame() for c, s in pairs}


def apply_lag(panel: pd.DataFrame, target_col: str, lag: int) -> pd.DataFrame:
    """rate/control changes at month t vs. target change at month t+lag."""
    df = panel.copy()
    df["target_shifted"] = df[target_col].shift(-lag)
    return df


def _pvalue(r: float, n: int, k: int = 0) -> float:
    """Two-sided p-value for a (partial) correlation with k covariates —
    t = r·sqrt(dof / (1 − r²)), dof = n − k − 2 (same as Pingouin/SciPy)."""
    dof = n - k - 2
    if dof <= 0 or not np.isfinite(r):
        return float("nan")
    if abs(r) >= 1:
        return 0.0
    t = r * np.sqrt(dof / (1 - r * r))
    return float(2 * stats.t.sf(abs(t), dof))


def _pearson_np(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    xd, yd = x - x.mean(), y - y.mean()
    denom = np.sqrt((xd @ xd) * (yd @ yd))
    r = float(xd @ yd / denom) if denom > 0 else float("nan")
    return r, _pvalue(r, len(x))


def _partial_corr_np(x: np.ndarray, y: np.ndarray, covar: np.ndarray) -> tuple[float, float]:
    """Partial correlation of x and y given the columns of covar, from the
    pseudo-inverse of the covariance (precision) matrix:
    r_xy·Z = −P_xy / sqrt(P_xx · P_yy). Identical to pg.partial_corr."""
    data = np.column_stack([x, y, covar])
    with np.errstate(divide="ignore", invalid="ignore"):
        precision = np.linalg.pinv(np.cov(data, rowvar=False), hermitian=True)
        r = float(-precision[0, 1] / np.sqrt(precision[0, 0] * precision[1, 1]))
    return r, _pvalue(r, data.shape[0], data.shape[1] - 2)


def _partial_corr_1_np(rxy: float, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> tuple[float, float]:
    """Single-covariate special case in O(n), no matrix inverse:
    r_xy·z = (r_xy − r_xz·r_yz) / sqrt((1 − r_xz²)(1 − r_yz²))."""
    rxz, _ = _pearson_np(x, z)
    ryz, _ = _pearson_np(y, z)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = float((rxy - rxz * ryz) / np.sqrt((1 - rxz * rxz) * (1 - ryz * ryz)))
    return r, _pvalue(r, len(x), 1)


def _ols_r2_np(X: np.ndarray, y: np.ndarray) -> float:
    """R² of y ~ 1 + X via least squares (pg.linear_regression's r2)."""
    design = np.column_stack([np.ones(len(y)), X])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ beta
    yd = y - y.mean()
    return float(1 - (resid @ resid) / (yd @ yd))


def bh_qvalues(p_values) -> np.ndarray:
    """Benjamini–Hochberg (1995) q-values; NaN p-values stay NaN and are
    excluded from the family. Same result as pg.multicomp(method='fdr_bh')."""
    p = np.asarray(p_values, dtype=float)
    q = np.full_like(p, np.nan)
    ok = np.isfinite(p)
    m = int(ok.sum())
    if m == 0:
        return q
    order = np.argsort(p[ok])
    scaled = p[ok][order] * m / np.arange(1, m + 1)
    adjusted = np.minimum(np.minimum.accumulate(scaled[::-1])[::-1], 1.0)
    out = np.empty(m)
    out[order] = adjusted
    q[ok] = out
    return q


def _pg():
    import pingouin as pg  # imported lazily: only the pingouin engine needs it
    return pg


def _frame(x, y, covar=None) -> pd.DataFrame:
    df = pd.DataFrame({"x": x, "y": y})
    for i, col in enumerate(np.atleast_2d(np.asarray(covar).T) if covar is not None else []):
        df[f"z{i}"] = col
    return df


def pearson(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if STATS_ENGINE == "pingouin":
        res = _pg().corr(x, y, method="pearson")
        return float(res["r"].iloc[0]), float(res["p-val"].iloc[0])
    return _pearson_np(x, y)


def partial_corr(x: np.ndarray, y: np.ndarray, covar: np.ndarray) -> tuple[float, float]:
    if STATS_ENGINE == "pingouin":
        df = _frame(x, y, covar)
        res = _pg().partial_corr(data=df, x="x", y="y", covar=[c for c in df.columns if c.startswith("z")])
        return float(res["r"].iloc[0]), float(res["p-val"].iloc[0])
    return _partial_corr_np(x, y, covar)


def partial_corr_1(rxy: float, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> tuple[float, float]:
    if STATS_ENGINE == "pingouin":
        return partial_corr(x, y, z.reshape(-1, 1))
    return _partial_corr_1_np(rxy, x, y, z)


def ols_r2(X: np.ndarray, y: np.ndarray) -> float:
    if STATS_ENGINE == "pingouin":
        return float(_pg().linear_regression(X, y)["r2"].iloc[0])
    return _ols_r2_np(X, y)


def select_best_control(sub: pd.DataFrame, rate_col: str, candidates: list[str]) -> tuple[str | None, dict | None]:
    """Empirically picks whichever single candidate control most strengthens
    the rate/target relationship at this lag — not a fixed FDI default. Each
    candidate is tried as the sole covariate in a partial correlation; the one
    with the largest |partial r| wins (ties broken by the smaller p-value).
    Returns (None, None) if no candidate has enough overlapping data, in which
    case the caller falls back to the raw (uncontrolled) correlation."""
    best_name, best = None, None
    for control in candidates:
        if control not in sub.columns:
            continue
        candidate_sub = sub.dropna(subset=[control])
        if len(candidate_sub) < MIN_OBS or candidate_sub[control].std() == 0:
            # A constant control (e.g. an annual WDI series flat across the
            # window) adjusts for nothing: the numpy kernel returns NaN for it,
            # while pg.partial_corr's pseudo-inverse silently returns the raw r,
            # which then "wins" whenever real controls shrink |r|.
            continue
        x = candidate_sub[rate_col].to_numpy(float)
        y = candidate_sub["target_shifted"].to_numpy(float)
        rxy, _ = pearson(x, y)
        r, p = partial_corr_1(rxy, x, y, candidate_sub[control].to_numpy(float))
        if not np.isfinite(r):
            continue  # NaN never compares greater, so it would stick if it came first
        result = {"r": r, "p": p, "n": len(candidate_sub)}
        if best is None or abs(result["r"]) > abs(best["r"]) or (
            abs(result["r"]) == abs(best["r"]) and result["p"] < best["p"]
        ):
            best_name, best = control, result
    return best_name, best


def run_one(store, country_code: str, sector_id: str, rate_basis: str, target_variable: str,
            panel: pd.DataFrame, controls: list[str] | None = None) -> dict | None:
    """controls: explicit override (skips dynamic selection) — used when a caller
    wants a specific control rather than the empirically-best one, e.g. for
    comparison. Leave None for the normal dynamic-selection path."""
    rate_col = RATE_INDICATORS[rate_basis]
    target_col = "equity_valuation_change" if target_variable == "equity_valuation" else "commodity_price_change"
    if rate_col not in panel.columns or target_col not in panel.columns:
        return None

    best = None
    for lag in LAGS:
        df = apply_lag(panel, target_col, lag)
        sub = df.dropna(subset=[rate_col, "target_shifted"])
        if len(sub) < MIN_OBS:
            continue

        x = sub[rate_col].to_numpy(float)
        y = sub["target_shifted"].to_numpy(float)
        pearson_r, p_value = pearson(x, y)

        if controls is not None:
            chosen_control = controls[0] if controls else None
        else:
            chosen_control, _ = select_best_control(sub, rate_col, CANDIDATE_CONTROLS)

        if chosen_control and chosen_control in sub.columns:
            control_sub = sub.dropna(subset=[chosen_control])
            cx = control_sub[rate_col].to_numpy(float)
            cy = control_sub["target_shifted"].to_numpy(float)
            covar = control_sub[[chosen_control, "time_index"]].to_numpy(float)
            if len(control_sub) > 2:  # pg.partial_corr's own minimum
                partial_r, _ = partial_corr(cx, cy, covar)
                r_squared = ols_r2(np.column_stack([cx, covar[:, 0]]), cy)
            else:
                partial_r, r_squared = pearson_r, pearson_r ** 2
            if not np.isfinite(partial_r):
                partial_r = pearson_r
        else:
            chosen_control = None
            partial_r = pearson_r
            r_squared = pearson_r ** 2

        candidate = {
            "lag": lag, "pearson_r": pearson_r, "partial_r": partial_r,
            "p_value": p_value, "r_squared": r_squared, "n_obs": len(sub),
            "control": chosen_control,
        }
        if best is None or abs(candidate["pearson_r"]) > abs(best["pearson_r"]):
            best = candidate

    if best is None:
        return None

    return {
        "result_id": new_id(),
        "country_code": country_code,
        "sector_id": sector_id,
        "rate_basis": rate_basis,
        "target_variable": target_variable,
        "control_set": best["control"] or "",
        # Actual date objects, not "YYYY-MM-01" strings: DuckDB implicitly casts a
        # date-shaped string to DATE on INSERT, but BigQuery's
        # load_table_from_dataframe (used by BigQueryStore.upsert_df) only infers
        # a DATE column from an actual datetime64/date dtype, not an object/string
        # column — a plain string load fails with "cannot be assigned to
        # date_range_start, which has type DATE".
        "date_range_start": date.fromisoformat(f"{panel['date_key'].min()}-01"),
        "date_range_end": date.fromisoformat(f"{panel['date_key'].max()}-01"),
        "lag": best["lag"],
        "pearson_r": best["pearson_r"],
        "partial_r": best["partial_r"],
        "p_value": best["p_value"],
        "r_squared": best["r_squared"],
        "computed_at": datetime.now(timezone.utc),
    }


def run_analysis(controls: list[str] | None = None) -> dict:
    """controls: explicit override applied to every pair — leave None (the
    normal path) to let each pair pick its own best control dynamically."""
    store = get_store()
    results = []
    panels = load_panels(store, COUNTRY_SECTOR_PAIRS)

    for (country_code, sector_id), panel in panels.items():
        if panel.empty or len(panel) < MIN_OBS:
            log.info("%s/%s: insufficient joined data (%d rows) — skipping", country_code, sector_id, len(panel))
            continue

        for rate_basis in ("nominal", "real"):
            for target_variable in ("equity_valuation", "commodity_price"):
                res = run_one(store, country_code, sector_id, rate_basis, target_variable, panel, controls=controls)
                if res:
                    results.append(res)
                    log.info(
                        "%s/%s %-7s vs %-16s: r=%.2f partial_r=%.2f (control=%s) p=%.3f R2=%.2f lag=t+%d (n=%d)",
                        country_code, sector_id, rate_basis, target_variable,
                        res["pearson_r"], res["partial_r"], res["control_set"] or "none",
                        res["p_value"], res["r_squared"], res["lag"], panel.shape[0],
                    )

    if results:
        df = pd.DataFrame(results)
        # One family per batch run: every specification tested in this run.
        df["q_value_bh"] = bh_qvalues(df["p_value"])
        store.upsert_df("fact_correlation_result", df, ["result_id"])
        log.info("BH-FDR: %d of %d results have q < 0.05", int((df["q_value_bh"] < 0.05).sum()), len(df))

    log.info("Wrote %d correlation results across %d country/sector pairs", len(results), len(COUNTRY_SECTOR_PAIRS))
    return {"results_written": len(results)}


if __name__ == "__main__":
    run_analysis()
    # The hike forecast needs scikit-learn (analysis image only), so it is
    # imported here rather than at module level, where api/main.py imports
    # this module. A forecast failure must not fail the correlation batch.
    try:
        from analysis.forecast import run_forecast
        run_forecast()
    except Exception:
        log.exception("Hike forecast failed; correlation results were still written")
