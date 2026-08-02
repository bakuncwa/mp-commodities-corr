"""
Seeds the dimension tables and the country/sector analysis pairs this project
tracks. Global on purpose — not scoped to a single country — chosen by actual
production/processing share of each commodity, plus the central banks whose
RSS feeds are configured in etl/feeds.yaml.

Run directly: `python -m common.seed`
"""
from __future__ import annotations

import pandas as pd

from common.db import get_store
from common.schema import SECTORS, INDICATORS

COUNTRIES = [
    ("CL", "Chile", "Latin America & Caribbean"),
    ("PE", "Peru", "Latin America & Caribbean"),
    ("ZM", "Zambia", "Sub-Saharan Africa"),
    ("CD", "DR Congo", "Sub-Saharan Africa"),
    ("AU", "Australia", "East Asia & Pacific"),
    ("ID", "Indonesia", "East Asia & Pacific"),
    ("CN", "China", "East Asia & Pacific"),
    ("TW", "Taiwan", "East Asia & Pacific"),
    ("KR", "South Korea", "East Asia & Pacific"),
    ("JP", "Japan", "East Asia & Pacific"),
    ("US", "United States", "North America"),
    ("GB", "United Kingdom", "Europe & Central Asia"),
    ("DE", "Germany", "Europe & Central Asia"),
    ("IN", "India", "South Asia"),
]

# Which country/sector pairs the correlation job actually runs, chosen by real
# production/processing share so the rate-vs-valuation relationship being
# tested is economically plausible (e.g. Chile is the world's largest copper
# producer, DR Congo mines ~70% of global cobalt, Taiwan hosts the dominant
# advanced-node foundry).
COUNTRY_SECTOR_PAIRS = [
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


def seed():
    store = get_store()

    countries_df = pd.DataFrame(COUNTRIES, columns=["country_code", "country_name", "region"])
    store.upsert_df("dim_country", countries_df, ["country_code"])

    sectors_df = pd.DataFrame(SECTORS, columns=["sector_id", "sector_name", "sector_category"])
    store.upsert_df("dim_sector", sectors_df, ["sector_id"])

    indicators_df = pd.DataFrame(INDICATORS, columns=["indicator_id", "indicator_name", "unit", "source_name", "is_derived"])
    store.upsert_df("dim_indicator", indicators_df, ["indicator_id"])

    print(f"Seeded {len(countries_df)} countries, {len(sectors_df)} sectors, {len(indicators_df)} indicators.")
    print(f"{len(COUNTRY_SECTOR_PAIRS)} country/sector analysis pairs configured: {COUNTRY_SECTOR_PAIRS}")


if __name__ == "__main__":
    seed()
