"""
Cloud Function equivalent: pulls the policy rate + every control-variable series
from BIS, World Bank WDI, and FRED, computes real_interest_rate, and writes to
fact_macro_indicator. Real HTTP calls against real public APIs — verified
endpoints/dimension keys below (BIS in particular uses SDMX dimension codes that
aren't documented anywhere obvious, so these were found by trial against the
live API, not guessed).

Coverage is uneven by design, not by omission: BIS's WS_CBPOL policy-rate
dataflow doesn't include Taiwan (not a BIS member), Zambia, or DR Congo — those
countries simply won't get a policy_rate/real_interest_rate row, logged as a
skip rather than silently absent. World Bank WDI still covers them for the
other control variables.

FRED requires FRED_API_KEY (free, instant signup). Without it, the three global
FRED-sourced series (fed_funds_rate_global, usd_index, treasury_yield_10y) are
skipped with a warning rather than failing the whole run.

Run directly: `python -m etl.ingest_macro_indicators`
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests
from dotenv import load_dotenv

from common.db import get_store
from common.seed import COUNTRIES

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_macro_indicators")

FRED_API_KEY = os.environ.get("FRED_API_KEY")
COUNTRY_CODES = [c[0] for c in COUNTRIES]


def _get_with_retry(url: str, params: dict | None = None, timeout: int = 20, retries: int = 3) -> requests.Response | None:
    for attempt in range(1, retries + 1):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt == retries:
                log.warning("Giving up on %s after %d attempts: %s", url, retries, e)
                return None
            time.sleep(1.5 * attempt)
    return None

# World Bank WDI indicator codes for each control variable this project tracks.
WDI_INDICATORS = {
    "cpi_inflation": "FP.CPI.TOTL.ZG",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "unemployment_rate": "SL.UEM.TOTL.ZS",
    "trade_balance": "NE.RSB.GNFS.ZS",
    "gov_debt_pct_gdp": "GC.DOD.TOTL.GD.ZS",
    "fdi_net_inflow": "BX.KLT.DINV.WD.GD.ZS",
}

FRED_SERIES = {
    "fed_funds_rate_global": "FEDFUNDS",
    "usd_index": "DTWEXBGS",
    "treasury_yield_10y": "DGS10",
}


def _record_id(date_key: str, country_code: str | None, indicator_id: str) -> str:
    return f"{indicator_id}|{country_code or 'GLOBAL'}|{date_key}"


def fetch_bis_series(dataflow: str, dim_key: str, country_code: str) -> pd.DataFrame:
    """dim_key already has the country substituted, e.g. 'M.{cc}' or 'M.R.B.{cc}'."""
    url = f"https://stats.bis.org/api/v1/data/{dataflow}/{dim_key}"
    r = _get_with_retry(url, params={"format": "csv"})
    if r is None or r.status_code != 200 or len(r.text) < 100:
        return pd.DataFrame()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df = df[["TIME_PERIOD", "OBS_VALUE"]].rename(columns={"TIME_PERIOD": "date_key", "OBS_VALUE": "value"})
    df["country_code"] = country_code
    return df.dropna(subset=["value"])


def fetch_wdi_series(indicator_code: str, country_code: str) -> pd.DataFrame:
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_code}"
    r = _get_with_retry(url, params={"format": "json", "per_page": 100, "date": "1990:2030"})
    if r is None or r.status_code != 200:
        return pd.DataFrame()
    payload = r.json()
    if len(payload) < 2 or not payload[1]:
        return pd.DataFrame()
    rows = [{"year": row["date"], "value": row["value"]} for row in payload[1] if row["value"] is not None]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["country_code"] = country_code
    return df


def expand_annual_to_monthly(annual_df: pd.DataFrame, through: str) -> pd.DataFrame:
    """WDI is annual-only. Forward-fills each year's value across its 12 months
    (and beyond, until the next year's observation) so it can join against
    monthly BIS/FRED series — every row produced this way is is_interpolated=True,
    since no sub-year granularity actually exists in the source."""
    if annual_df.empty:
        return annual_df
    annual_df = annual_df.sort_values("year")
    through_year, through_month = int(through[:4]), int(through[5:7])
    rows = []
    for i, row in annual_df.iterrows():
        year = int(row["year"])
        rows.append({"date_key": f"{year}-01", "value": row["value"], "country_code": row["country_code"]})
    monthly_index = pd.date_range(f"{annual_df['year'].min()}-01-01", f"{through_year}-{through_month:02d}-01", freq="MS")
    monthly_df = pd.DataFrame({"date_key": monthly_index.strftime("%Y-%m")})
    monthly_df["country_code"] = annual_df["country_code"].iloc[0]
    yearly_lookup = {f"{int(r['year'])}-01": r["value"] for _, r in annual_df.iterrows()}
    # forward-fill: for each month, use the latest year <= that month
    sorted_years = sorted(int(y) for y in annual_df["year"])
    def value_for(date_key: str):
        y = int(date_key[:4])
        candidates = [yr for yr in sorted_years if yr <= y]
        if not candidates:
            return None
        return yearly_lookup[f"{max(candidates)}-01"]
    monthly_df["value"] = monthly_df["date_key"].apply(value_for)
    return monthly_df.dropna(subset=["value"])


def fetch_fred_series(series_id: str) -> pd.DataFrame:
    if not FRED_API_KEY:
        return pd.DataFrame()
    r = _get_with_retry(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
                "observation_start": "1990-01-01"},
    )
    if r is None or r.status_code != 200:
        log.warning("FRED fetch failed for %s: HTTP %s", series_id, r.status_code)
        return pd.DataFrame()
    obs = r.json().get("observations", [])
    rows = [{"date_key": o["date"][:7], "value": float(o["value"])} for o in obs if o["value"] != "."]
    return pd.DataFrame(rows)


def with_change(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    df = df.sort_values(group_cols + ["date_key"]) if group_cols else df.sort_values("date_key")
    if group_cols:
        df["value_change"] = df.groupby(group_cols)["value"].diff()
    else:
        df["value_change"] = df["value"].diff()
    return df


def run_ingest() -> dict:
    store = get_store()
    now_month = datetime.utcnow().strftime("%Y-%m")
    all_rows = []
    skipped = []

    # --- BIS: policy_rate, reer (per-country coverage varies) ---
    for cc in COUNTRY_CODES:
        rate_df = fetch_bis_series("WS_CBPOL", f"M.{cc}", cc)
        if rate_df.empty:
            skipped.append(("policy_rate", cc))
        else:
            rate_df["indicator_id"] = "policy_rate"
            rate_df = with_change(rate_df, ["country_code"])
            all_rows.append(rate_df)
        time.sleep(0.2)

        reer_df = fetch_bis_series("WS_EER", f"M.R.B.{cc}", cc)
        if reer_df.empty:
            skipped.append(("reer", cc))
        else:
            reer_df["indicator_id"] = "reer"
            reer_df = with_change(reer_df, ["country_code"])
            all_rows.append(reer_df)
        time.sleep(0.2)
        log.info("BIS %-4s policy_rate=%s reer=%s", cc, not rate_df.empty, not reer_df.empty)

    # --- World Bank WDI (annual, forward-filled to monthly) ---
    for indicator_id, wdi_code in WDI_INDICATORS.items():
        for cc in COUNTRY_CODES:
            annual_df = fetch_wdi_series(wdi_code, cc)
            if annual_df.empty:
                skipped.append((indicator_id, cc))
                continue
            monthly_df = expand_annual_to_monthly(annual_df, now_month)
            monthly_df["indicator_id"] = indicator_id
            monthly_df["is_interpolated"] = True
            monthly_df = with_change(monthly_df, ["country_code"])
            all_rows.append(monthly_df)
            time.sleep(0.15)
        log.info("WDI %-20s done", indicator_id)

    # --- FRED (global, country_code=NULL) ---
    for indicator_id, series_id in FRED_SERIES.items():
        fred_df = fetch_fred_series(series_id)
        if fred_df.empty:
            skipped.append((indicator_id, "GLOBAL"))
            continue
        fred_df["indicator_id"] = indicator_id
        fred_df["country_code"] = None
        fred_df = with_change(fred_df, [])
        all_rows.append(fred_df)
        log.info("FRED %-20s %d observations", indicator_id, len(fred_df))

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not combined.empty:
        combined["is_interpolated"] = combined.get("is_interpolated", False).fillna(False)
        combined["record_id"] = combined.apply(
            lambda r: _record_id(r["date_key"], r["country_code"], r["indicator_id"]), axis=1
        )
        cols = ["record_id", "date_key", "country_code", "indicator_id", "value", "value_change", "is_interpolated"]
        store.upsert_df("fact_macro_indicator", combined[cols], ["record_id"])

        # real_interest_rate = policy_rate - cpi_inflation, per country/month where both exist
        pivot = combined.pivot_table(index=["date_key", "country_code"], columns="indicator_id", values="value", aggfunc="first").reset_index()
        if "policy_rate" in pivot.columns and "cpi_inflation" in pivot.columns:
            real_df = pivot.dropna(subset=["policy_rate", "cpi_inflation"]).copy()
            real_df["value"] = real_df["policy_rate"] - real_df["cpi_inflation"]
            real_df["indicator_id"] = "real_interest_rate"
            real_df["is_interpolated"] = False
            real_df = with_change(real_df, ["country_code"])
            real_df["record_id"] = real_df.apply(lambda r: _record_id(r["date_key"], r["country_code"], "real_interest_rate"), axis=1)
            cols2 = ["record_id", "date_key", "country_code", "indicator_id", "value", "value_change", "is_interpolated"]
            store.upsert_df("fact_macro_indicator", real_df[cols2], ["record_id"])
            log.info("Derived real_interest_rate for %d country/month rows", len(real_df))

    log.info("Ingested %d indicator rows. Skipped (no data): %s", len(combined), skipped)
    return {"rows": len(combined), "skipped": skipped}


if __name__ == "__main__":
    run_ingest()
