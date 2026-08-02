"""
Cloud Function equivalent: pulls commodity spot prices (World Bank Pink Sheet)
and sector equity valuations (Yahoo Finance via yfinance) into
fact_commodity_price and fact_equity_valuation.

Known real-world gap, stated rather than papered over: the World Bank Pink
Sheet only covers copper and nickel among this project's six sectors — lithium,
cobalt, and rare earths have no equivalent free public spot-price series that
was findable during development. Their fact_commodity_price rows are simply
absent (not backfilled with a fabricated number); the correlation job's
"commodity price" target variable will just have no data for those three
sectors until a real source is found, while "equity valuation" (below) still
works for all six via sector ETF proxies.

Sector equity valuation uses one ETF per sector as a public, free, no-key proxy
via yfinance. Two of the six don't have a pure-play ETF and use a broader proxy,
noted below rather than presented as sector-specific:
  - nickel -> PICK (broad metals/mining producers ETF; no pure-nickel ETF exists)
  - cobalt -> BATT (battery-metals ETF with cobalt exposure; no pure-cobalt ETF exists)
The other four (copper/COPX, lithium/LIT, rare_earths/REMX, semiconductors/SOXX)
are reasonably close pure-play proxies.

Run directly: `python -m etl.ingest_market_data`
"""
from __future__ import annotations

import io
import logging
import time

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

from common.db import get_store

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_market_data")

# World Bank "Pink Sheet" — the docs.worldbank.org hash in this URL is refreshed
# periodically by the World Bank; if this 404s, get the current link from
# https://www.worldbank.org/en/research/commodity-markets ("Monthly Prices" download).
PINK_SHEET_URL = "https://thedocs.worldbank.org/en/doc/5d903e848db1d1b83e0ec8f744e55570-0350012021/related/CMO-Historical-Data-Monthly.xlsx"

PINK_SHEET_COLUMNS = {
    "copper": "Copper",
    "nickel": "Nickel",
}

SECTOR_ETF = {
    "copper": "COPX",
    "lithium": "LIT",
    "nickel": "PICK",
    "rare_earths": "REMX",
    "cobalt": "BATT",
    "semiconductors": "SOXX",
}


def fetch_pink_sheet() -> pd.DataFrame:
    r = requests.get(PINK_SHEET_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), sheet_name="Monthly Prices", header=4)
    df = df.rename(columns={df.columns[0]: "period"})
    df = df.dropna(subset=["period"])
    df["date_key"] = df["period"].astype(str).str.strip().str.replace("M", "-")
    return df


def ingest_commodity_prices() -> int:
    store = get_store()
    sheet = fetch_pink_sheet()
    rows = []
    for sector_id, col_name in PINK_SHEET_COLUMNS.items():
        if col_name not in sheet.columns:
            log.warning("Pink Sheet column %r not found for sector %s", col_name, sector_id)
            continue
        sub = sheet[["date_key", col_name]].rename(columns={col_name: "price_index"}).dropna()
        sub["sector_id"] = sector_id
        sub = sub.sort_values("date_key")
        sub["price_change"] = sub.groupby("sector_id")["price_index"].pct_change() * 100
        sub["production_volume_change"] = None  # populated separately if/when a USGS source is wired in
        sub["price_id"] = sub["sector_id"] + "|" + sub["date_key"]
        rows.append(sub[["price_id", "date_key", "sector_id", "price_index", "price_change", "production_volume_change"]])

    if not rows:
        log.warning("No commodity price rows ingested.")
        return 0
    combined = pd.concat(rows, ignore_index=True)
    store.upsert_df("fact_commodity_price", combined, ["price_id"])
    log.info("Ingested %d commodity price rows (sectors: %s)", len(combined), list(PINK_SHEET_COLUMNS))
    return len(combined)


def ingest_equity_valuations() -> int:
    store = get_store()
    rows = []
    for sector_id, ticker in SECTOR_ETF.items():
        try:
            hist = yf.Ticker(ticker).history(period="10y", interval="1mo")
        except Exception as e:
            log.warning("yfinance fetch failed for %s (%s): %s", sector_id, ticker, e)
            continue
        if hist.empty:
            log.warning("No history returned for %s (%s)", sector_id, ticker)
            continue
        hist = hist.reset_index()
        hist["date_key"] = hist["Date"].dt.strftime("%Y-%m")
        sub = hist[["date_key", "Close"]].rename(columns={"Close": "valuation_index"})
        sub = sub.groupby("date_key", as_index=False).last()  # collapse any same-month duplicates
        sub["sector_id"] = sector_id
        sub = sub.sort_values("date_key")
        sub["valuation_change"] = sub["valuation_index"].pct_change() * 100
        sub["instrument_type"] = "sector_etf"
        sub["valuation_id"] = sub["sector_id"] + "|" + sub["date_key"]
        rows.append(sub[["valuation_id", "date_key", "sector_id", "valuation_index", "valuation_change", "instrument_type"]])
        log.info("%-15s (%s) %d monthly observations", sector_id, ticker, len(sub))
        time.sleep(0.3)

    if not rows:
        return 0
    combined = pd.concat(rows, ignore_index=True)
    store.upsert_df("fact_equity_valuation", combined, ["valuation_id"])
    log.info("Ingested %d equity valuation rows across %d sectors", len(combined), len(rows))
    return len(combined)


def run_ingest() -> dict:
    price_rows = ingest_commodity_prices()
    valuation_rows = ingest_equity_valuations()
    return {"price_rows": price_rows, "valuation_rows": valuation_rows}


if __name__ == "__main__":
    run_ingest()
