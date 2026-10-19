"""
Cloud Function equivalent: pulls the policy rate + every control-variable series
from BIS, World Bank WDI, and FRED, computes real_interest_rate, and writes to
fact_macro_indicator. Real HTTP calls against real public APIs — verified
endpoints/dimension keys below (BIS in particular uses SDMX dimension codes that
aren't documented anywhere obvious, so these were found by trial against the
live API, not guessed).

Covers every economy in common/countries.py with one bulk request per
dataflow/indicator (BIS: 2, WDI: 6, FRED: 3) rather than one per country.
Coverage is uneven by source, not by omission: BIS WS_CBPOL reports ~39 policy
rates (the euro area once, as XM — mapped to each member from its euro adoption
month), so countries outside it get no policy_rate/real_interest_rate row.
World Bank WDI covers nearly every economy for the other controls.

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
from common.countries import EURO_ADOPTION
from common.seed import COUNTRIES

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_macro_indicators")

FRED_API_KEY = os.environ.get("FRED_API_KEY")
COUNTRY_CODES = {c[0] for c in COUNTRIES}


def _get_with_retry(url: str, params: dict | None = None, timeout: int = 60, retries: int = 3) -> requests.Response | None:
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


def _record_ids(df: pd.DataFrame) -> pd.Series:
    """Vectorized _record_id over a frame (row-wise apply is slow at ~10^6 rows)."""
    return df["indicator_id"] + "|" + df["country_code"].fillna("GLOBAL") + "|" + df["date_key"]


def fetch_bis_all(dataflow: str, dim_key: str) -> pd.DataFrame:
    """Every reporting area in one request; dim_key leaves REF_AREA open,
    e.g. 'M.' (WS_CBPOL) or 'M.R.B.' (WS_EER)."""
    r = _get_with_retry(f"https://stats.bis.org/api/v1/data/{dataflow}/{dim_key}", params={"format": "csv"})
    if r is None or r.status_code != 200 or len(r.text) < 100:
        return pd.DataFrame()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text), usecols=["REF_AREA", "TIME_PERIOD", "OBS_VALUE"])
    df = df.rename(columns={"REF_AREA": "country_code", "TIME_PERIOD": "date_key", "OBS_VALUE": "value"})
    return df.dropna(subset=["value"])


def expand_euro_area(rates: pd.DataFrame) -> pd.DataFrame:
    """Replace the XM (euro area) series with one series per member: its
    national central bank's rate (where BIS has one, e.g. FR/DE pre-1999)
    before its euro adoption month, the ECB rate from that month on."""
    euro = rates[rates["country_code"] == "XM"]
    rest = rates[rates["country_code"] != "XM"]
    is_member = rest["country_code"].isin(EURO_ADOPTION.keys())
    spliced = []
    for cc, since in EURO_ADOPTION.items():
        national = rest[(rest["country_code"] == cc) & (rest["date_key"] < since)]
        spliced += [national, euro[euro["date_key"] >= since].assign(country_code=cc)]
    return pd.concat([rest[~is_member], *spliced], ignore_index=True)


def fetch_wdi_all(indicator_code: str) -> pd.DataFrame:
    """Every economy for one indicator (paged; ~11k rows fit in one page)."""
    rows, page, pages = [], 1, 1
    while page <= pages:
        r = _get_with_retry(
            f"https://api.worldbank.org/v2/country/all/indicator/{indicator_code}",
            params={"format": "json", "per_page": 20000, "date": "1990:2030", "page": page},
        )
        if r is None or r.status_code != 200:
            break
        payload = r.json()
        if len(payload) < 2 or not payload[1]:
            break
        pages = payload[0].get("pages", 1)
        rows += [
            {"country_code": row["country"]["id"], "year": int(row["date"]), "value": row["value"]}
            for row in payload[1] if row["value"] is not None
        ]
        page += 1
    df = pd.DataFrame(rows, columns=["country_code", "year", "value"])
    return df[df["country_code"].isin(COUNTRY_CODES)]


def expand_annual_to_monthly(annual_df: pd.DataFrame, through: str) -> pd.DataFrame:
    """WDI is annual-only. Each month from a country's first observed year
    through `through` takes the latest annual value at or before its year
    (forward fill), so it can join monthly BIS/FRED series — every row produced
    this way is is_interpolated=True, since no sub-year granularity exists in
    the source. Vectorized over all countries at once."""
    if annual_df.empty:
        return annual_df.assign(date_key=pd.Series(dtype=str))[["date_key", "value", "country_code"]]
    months = pd.date_range(f"{annual_df['year'].min()}-01-01", f"{through}-01", freq="MS")
    grid = pd.DataFrame({"date_key": months.strftime("%Y-%m"), "year": months.year.astype("int64")})
    first_year = annual_df.groupby("country_code")["year"].min().rename("first_year")
    grid = grid.merge(first_year.reset_index(), how="cross")
    grid = grid[grid["year"] >= grid["first_year"]].sort_values("year")
    out = pd.merge_asof(
        grid, annual_df.astype({"year": "int64"}).sort_values("year")[["country_code", "year", "value"]],
        on="year", by="country_code", direction="backward",
    )
    return out.dropna(subset=["value"]).sort_values(["country_code", "date_key"])[["date_key", "value", "country_code"]].reset_index(drop=True)


def fetch_fred_series(series_id: str) -> pd.DataFrame:
    if not FRED_API_KEY:
        return pd.DataFrame()
    r = _get_with_retry(
        "https://api.stlouisfed.org/fred/series/observations",
        # frequency=m: DGS10 and DTWEXBGS are daily; FRED's server-side monthly
        # average gives one row per YYYY-MM instead of ~21 colliding record_ids.
        params={"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
                "observation_start": "1990-01-01", "frequency": "m", "aggregation_method": "avg"},
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

    # --- BIS: policy_rate, reer (one request each, all reporting areas) ---
    rates = fetch_bis_all("WS_CBPOL", "M.")
    rates = expand_euro_area(rates) if not rates.empty else rates
    for indicator_id, df in (("policy_rate", rates), ("reer", fetch_bis_all("WS_EER", "M.R.B."))):
        df = df[df["country_code"].isin(COUNTRY_CODES)] if not df.empty else df
        if df.empty:
            skipped.append((indicator_id, "ALL"))
            continue
        df = with_change(df.assign(indicator_id=indicator_id), ["country_code"])
        all_rows.append(df)
        log.info("BIS %-12s %d countries, %d observations", indicator_id, df["country_code"].nunique(), len(df))
    covered = set(rates["country_code"]) if not rates.empty else set()
    skipped.append(("policy_rate", f"{len(COUNTRY_CODES - covered)} countries without a BIS series"))

    # --- World Bank WDI (annual, forward-filled to monthly; one request per indicator) ---
    for indicator_id, wdi_code in WDI_INDICATORS.items():
        annual_df = fetch_wdi_all(wdi_code)
        if annual_df.empty:
            skipped.append((indicator_id, "ALL"))
            continue
        monthly_df = expand_annual_to_monthly(annual_df, now_month)
        monthly_df["indicator_id"] = indicator_id
        monthly_df["is_interpolated"] = True
        all_rows.append(with_change(monthly_df, ["country_code"]))
        log.info("WDI %-20s %d countries", indicator_id, annual_df["country_code"].nunique())

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
        combined["is_interpolated"] = combined["is_interpolated"].astype("boolean").fillna(False).astype(bool)
        combined["record_id"] = _record_ids(combined)
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
            real_df["record_id"] = _record_ids(real_df)
            cols2 = ["record_id", "date_key", "country_code", "indicator_id", "value", "value_change", "is_interpolated"]
            store.upsert_df("fact_macro_indicator", real_df[cols2], ["record_id"])
            log.info("Derived real_interest_rate for %d country/month rows", len(real_df))

    log.info("Ingested %d indicator rows across %d countries. Skipped: %s",
             len(combined), combined["country_code"].nunique() if not combined.empty else 0, skipped)
    return {"rows": len(combined), "skipped": skipped}


if __name__ == "__main__":
    run_ingest()
