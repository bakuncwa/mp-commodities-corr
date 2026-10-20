"""
Next-month policy-rate hike probability per country — the one ML model from
notebooks/03_ml_forecasting.ipynb with real out-of-sample skill (walk-forward
ROC-AUC ~0.83 vs 0.63 for persistence), run monthly after the correlation batch.

Model: RandomForestClassifier (Breiman, 2001), balanced class weights, on six
monthly lags of each indicator's change (policy rate, CPI, unemployment, REER,
FED funds, USD index, 10-year Treasury). Euro members share the ECB rate, so
the euro area trains once (Germany's series) and its forecast is copied to
every member that has adopted the euro.

Each run stores, per country: the probability of a hike in the month after the
latest observed month, plus run-level walk-forward ROC-AUC, the hike base rate
and permutation-importance drivers, so the UI can show how much to trust it.

Run directly: `python -m analysis.forecast` (also invoked by the Cloud Run job
after analysis.correlation_regression).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from common.countries import EURO_ADOPTION
from common.db import get_store, new_id

log = logging.getLogger("forecast")

FEATURES = ["policy_rate", "cpi_inflation", "unemployment_rate", "reer",
            "fed_funds_rate_global", "usd_index", "treasury_yield_10y"]
N_LAGS = 6
MIN_RATE_MONTHS = 60
START = "1995-01"
MODEL = "random_forest_v1"
EURO_ANCHOR = "DE"
MAX_STALENESS_MONTHS = 3
# Months an indicator is published after the others (BIS effective exchange
# rates trail policy rates by one month). Shifting it keeps every feature
# limited to data actually available at forecast time, so the latest month
# with a policy rate can be forecast from.
PUBLICATION_LAG = {"reer": 1}


def _model():
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(n_estimators=300, min_samples_leaf=20, max_features=0.3,
                                  class_weight="balanced", n_jobs=-1, random_state=0)


def build_frame(store) -> pd.DataFrame:
    """One row per country-month: lagged feature changes, plus `hike` (next
    month's policy-rate change > 0), which is NaN for the latest month."""
    macro = store.query_df(
        f"SELECT country_code, date_key, indicator_id, value_change FROM fact_macro_indicator WHERE date_key >= '{START}'"
    )
    glob = macro[macro.country_code.isna()].pivot_table(index="date_key", columns="indicator_id", values="value_change", aggfunc="first")
    ctry = macro[macro.country_code.notna()].pivot_table(index=["country_code", "date_key"], columns="indicator_id",
                                                         values="value_change", aggfunc="first").reset_index()
    wide = ctry.merge(glob.reset_index(), on="date_key", how="left")
    since = wide.country_code.map(EURO_ADOPTION)
    wide = wide[~(since.notna() & (wide.country_code != EURO_ANCHOR) & (wide.date_key >= since))]

    feats = [f for f in FEATURES if f in wide]
    frames = []
    for cc, g in wide.sort_values("date_key").groupby("country_code"):
        if g["policy_rate"].notna().sum() < MIN_RATE_MONTHS:
            continue
        g = g.set_index("date_key")
        X = pd.concat({f"{f}_l{k}": g[f].shift(k + PUBLICATION_LAG.get(f, 0)) for f in feats for k in range(N_LAGS)}, axis=1)
        nxt = g["policy_rate"].shift(-1)
        X["hike"] = np.where(nxt.notna(), (nxt > 0).astype(float), np.nan)
        X["country_code"] = cc
        frames.append(X.reset_index())
    frame = pd.concat(frames, ignore_index=True)
    return frame.dropna(subset=[c for c in frame if c not in ("hike", "country_code", "date_key")])


def walk_forward_auc(train: pd.DataFrame, cols: list[str], n_splits: int = 5) -> tuple[float, object, pd.DataFrame]:
    """Mean ROC-AUC over expanding-window folds on calendar months; also
    returns the last fold's model and test set for permutation importance."""
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit
    months = np.sort(train.date_key.unique())
    aucs, model, test = [], None, None
    for tr_idx, te_idx in TimeSeriesSplit(n_splits=n_splits).split(months):
        tr, te = train[train.date_key.isin(months[tr_idx])], train[train.date_key.isin(months[te_idx])]
        if te.hike.nunique() < 2:
            continue
        model = _model().fit(tr[cols], tr.hike)
        aucs.append(roc_auc_score(te.hike, model.predict_proba(te[cols])[:, 1]))
        test = te
    return float(np.mean(aucs)), model, test


def drivers(model, test: pd.DataFrame, cols: list[str], top: int = 5) -> list[dict]:
    """Permutation importance (drop in ROC-AUC), summed over each indicator's lags."""
    from sklearn.inspection import permutation_importance
    imp = permutation_importance(model, test[cols], test.hike, scoring="roc_auc", n_repeats=5, random_state=0, n_jobs=-1)
    by_ind = pd.Series(imp.importances_mean, index=cols).groupby(lambda c: c.rsplit("_l", 1)[0]).sum()
    by_ind = by_ind[by_ind > 0].sort_values(ascending=False).head(top)
    return [{"indicator": k, "importance": round(float(v), 4)} for k, v in by_ind.items()]


def run_forecast(write: bool = True) -> pd.DataFrame:
    store = get_store()
    frame = build_frame(store)
    cols = [c for c in frame if c not in ("date_key", "country_code", "hike")]
    train = frame[frame.hike.notna()].sort_values("date_key")
    auc, fold_model, fold_test = walk_forward_auc(train, cols)
    top = drivers(fold_model, fold_test, cols)

    model = _model().fit(train[cols], train.hike)
    latest = frame.sort_values("date_key").groupby("country_code").tail(1)
    # A series that stopped long ago would yield a "forecast" for a past month.
    freshest = pd.Period(latest.date_key.max(), "M")
    latest = latest[latest.date_key.map(lambda d: (freshest - pd.Period(d, "M")).n <= MAX_STALENESS_MONTHS)
                    | (latest.country_code == EURO_ANCHOR)]
    prob = dict(zip(latest.country_code, model.predict_proba(latest[cols])[:, 1]))
    as_of = dict(zip(latest.country_code, latest.date_key))

    if EURO_ANCHOR in prob:  # members share the ECB decision
        for cc in EURO_ADOPTION:
            if cc != EURO_ANCHOR and as_of[EURO_ANCHOR] >= EURO_ADOPTION[cc]:
                prob[cc], as_of[cc] = prob[EURO_ANCHOR], as_of[EURO_ANCHOR]

    run_id, now = new_id(), datetime.now(timezone.utc)
    rows = pd.DataFrame([{
        "forecast_id": new_id(), "run_id": run_id, "country_code": cc,
        "as_of_month": as_of[cc],
        "target_month": (pd.Period(as_of[cc], "M") + 1).strftime("%Y-%m"),
        "hike_probability": round(float(p), 4),
        "model": MODEL, "cv_roc_auc": round(auc, 4), "base_rate": round(float(train.hike.mean()), 4),
        "drivers": json.dumps(top), "computed_at": now,
    } for cc, p in sorted(prob.items())])
    if write:
        store.upsert_df("fact_hike_forecast", rows, ["forecast_id"])
    log.info("Hike forecast %s: %d countries, walk-forward ROC-AUC %.3f, base rate %.1f%%, drivers %s",
             run_id[:8], len(rows), auc, 100 * train.hike.mean(), [d["indicator"] for d in top])
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forecast()
