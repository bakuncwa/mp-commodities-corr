"""
Seeds the dimension tables and the country/sector analysis pairs this project
tracks: every economy × every sector, with a featured subset of producer pairs.

Run directly: `python -m common.seed`
"""
from __future__ import annotations

import pandas as pd

from common.db import get_store
from common.countries import ALL_COUNTRIES
from common.schema import SECTORS, INDICATORS

# Every economy (see common/countries.py) — the article tagger, the macro
# ingest and the correlation batch all run over this full list.
COUNTRIES = [(cc, name, region) for cc, name, region, _lat, _lon in ALL_COUNTRIES]

# Producer/processor pairs (e.g. Chile is the world's largest copper producer,
# DR Congo mines ~70% of global cobalt, Taiwan hosts the dominant advanced-node
# foundry). Shown first in the UI; the batch itself runs every pair below.
FEATURED_PAIRS = [
    ("CL", "copper"),
    ("PE", "copper"),
    ("ZM", "copper"),
    ("CD", "cobalt"),
    ("AU", "lithium"),
    ("AU", "nickel"),
    ("ID", "nickel"),
    ("CN", "rare_earths"),
    ("CN", "semiconductors"),
    ("TW", "semiconductors"),
    ("KR", "semiconductors"),
    ("US", "semiconductors"),
    ("JP", "semiconductors"),
]

# Every country × every sector. Commodity prices and sector ETFs are global
# series, so each pair asks whether that country's rate changes co-move with the
# global sector; pairs without a policy-rate series (no BIS coverage) produce no
# panel and are skipped by the batch at no cost.
COUNTRY_SECTOR_PAIRS = [(cc, sector_id) for cc, *_ in COUNTRIES for sector_id, *_ in SECTORS]


def seed():
    store = get_store()

    countries_df = pd.DataFrame(COUNTRIES, columns=["country_code", "country_name", "region"])
    store.upsert_df("dim_country", countries_df, ["country_code"])

    sectors_df = pd.DataFrame(SECTORS, columns=["sector_id", "sector_name", "sector_category"])
    store.upsert_df("dim_sector", sectors_df, ["sector_id"])

    indicators_df = pd.DataFrame(INDICATORS, columns=["indicator_id", "indicator_name", "unit", "source_name", "is_derived"])
    store.upsert_df("dim_indicator", indicators_df, ["indicator_id"])

    print(f"Seeded {len(countries_df)} countries, {len(sectors_df)} sectors, {len(indicators_df)} indicators.")
    print(f"{len(COUNTRY_SECTOR_PAIRS)} country/sector analysis pairs configured ({len(FEATURED_PAIRS)} featured).")


if __name__ == "__main__":
    seed()
