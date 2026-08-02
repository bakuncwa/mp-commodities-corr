"""
Scheduled batch job: for every (country, sector) pair in common.seed.COUNTRY_SECTOR_PAIRS,
joins fact_macro_indicator (pivoted wide by indicator) with fact_commodity_price and
fact_equity_valuation on date_key, then runs Pingouin correlation, partial correlation,
and multiple regression — for both rate bases (nominal/real) and both targets
(equity_valuation/commodity_price), sweeping lags t, t+1, t+2, t+3 and keeping the
strongest-fitting lag per combination. Results are appended to fact_correlation_result
(never overwritten — see README's "Historical Data & Date Filtering" section).

Run directly: `python -m analysis.correlation_regression`
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pingouin as pg

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
MIN_OBS = 8  # below this, Pingouin's correlation/regression output is not meaningful


def build_country_panel(store, country_code: str, sector_id: str) -> pd.DataFrame:
    macro = store.query_df(
        f"SELECT date_key, indicator_id, value_change FROM fact_macro_indicator WHERE country_code = '{country_code}'"
    )
    if macro.empty:
        return pd.DataFrame()
    wide = macro.pivot_table(index="date_key", columns="indicator_id", values="value_change", aggfunc="first").reset_index()

    price = store.query_df(f"SELECT date_key, price_change FROM fact_commodity_price WHERE sector_id = '{sector_id}'")
    price = price.rename(columns={"price_change": "commodity_price_change"})

    valuation = store.query_df(f"SELECT date_key, valuation_change FROM fact_equity_valuation WHERE sector_id = '{sector_id}'")
    valuation = valuation.rename(columns={"valuation_change": "equity_valuation_change"})

    panel = wide.merge(price, on="date_key", how="left").merge(valuation, on="date_key", how="left")
    panel = panel.sort_values("date_key").reset_index(drop=True)
    panel["time_index"] = range(len(panel))
    return panel


def apply_lag(panel: pd.DataFrame, target_col: str, lag: int) -> pd.DataFrame:
    """rate/control changes at month t vs. target change at month t+lag."""
    df = panel.copy()
    df["target_shifted"] = df[target_col].shift(-lag)
    return df


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
        needed = [rate_col, control, "target_shifted"]
        candidate_sub = sub.dropna(subset=needed)
        if len(candidate_sub) < MIN_OBS:
            continue
        try:
            partial = pg.partial_corr(data=candidate_sub, x=rate_col, y="target_shifted", covar=[control])
        except (ValueError, AssertionError):
            continue
        result = {"r": float(partial["r"].iloc[0]), "p": float(partial["p-val"].iloc[0]), "n": len(candidate_sub)}
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

        corr = pg.corr(sub[rate_col], sub["target_shifted"], method="pearson")
        pearson_r = float(corr["r"].iloc[0])
        p_value = float(corr["p-val"].iloc[0])

        if controls is not None:
            chosen_control = controls[0] if controls else None
        else:
            chosen_control, _ = select_best_control(sub, rate_col, CANDIDATE_CONTROLS)

        if chosen_control and chosen_control in sub.columns:
            control_sub = sub.dropna(subset=[chosen_control])
            covar = [chosen_control] + (["time_index"] if "time_index" in control_sub.columns else [])
            try:
                partial = pg.partial_corr(data=control_sub, x=rate_col, y="target_shifted", covar=covar)
                partial_r = float(partial["r"].iloc[0])
            except (ValueError, AssertionError):
                partial_r = pearson_r
            try:
                reg = pg.linear_regression(control_sub[[rate_col, chosen_control]], control_sub["target_shifted"])
                r_squared = float(reg["r2"].iloc[0])
            except (ValueError, AssertionError):
                r_squared = float(corr["r2"].iloc[0]) if "r2" in corr.columns else pearson_r ** 2
        else:
            chosen_control = None
            partial_r = pearson_r
            r_squared = float(corr["r2"].iloc[0]) if "r2" in corr.columns else pearson_r ** 2

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

    for country_code, sector_id in COUNTRY_SECTOR_PAIRS:
        panel = build_country_panel(store, country_code, sector_id)
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
        store.upsert_df("fact_correlation_result", df, ["result_id"])

    log.info("Wrote %d correlation results across %d country/sector pairs", len(results), len(COUNTRY_SECTOR_PAIRS))
    return {"results_written": len(results)}


if __name__ == "__main__":
    run_analysis()
