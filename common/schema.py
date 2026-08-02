"""
Star schema shared by every backend (local DuckDB for development, BigQuery in
production). Table and column names match the README's data-dictionary exactly
so the same SQL runs unmodified against either backend — only common.db picks
the engine.
"""

DDL = {
    "dim_source": """
        CREATE TABLE IF NOT EXISTS dim_source (
            source_id VARCHAR PRIMARY KEY,
            source_name VARCHAR,
            source_type VARCHAR,   -- central_bank / world_bank_group / mnc_bank / commodity_press / semiconductor_press
            home_country_code VARCHAR
        )
    """,
    "dim_country": """
        CREATE TABLE IF NOT EXISTS dim_country (
            country_code VARCHAR PRIMARY KEY,
            country_name VARCHAR,
            region VARCHAR
        )
    """,
    "dim_sector": """
        CREATE TABLE IF NOT EXISTS dim_sector (
            sector_id VARCHAR PRIMARY KEY,
            sector_name VARCHAR,
            sector_category VARCHAR  -- metal / semiconductor
        )
    """,
    "dim_indicator": """
        CREATE TABLE IF NOT EXISTS dim_indicator (
            indicator_id VARCHAR PRIMARY KEY,
            indicator_name VARCHAR,
            unit VARCHAR,
            source_name VARCHAR,
            is_derived BOOLEAN
        )
    """,
    "fact_article": """
        CREATE TABLE IF NOT EXISTS fact_article (
            article_id VARCHAR PRIMARY KEY,
            date_key VARCHAR,
            source_id VARCHAR,
            article_category VARCHAR,   -- monetary_policy / commodity_stocks
            policy_subtype VARCHAR,     -- comma-joined tags; NULL when not monetary_policy
            sentiment_score DOUBLE,
            sentiment_target VARCHAR,
            source_url VARCHAR,
            title VARCHAR,
            published TIMESTAMP,
            mech VARCHAR,               -- comma-joined transmission-mechanism tags, LLM-inferred
            policy_stance VARCHAR       -- contractionary / expansionary / neutral, LLM-inferred (monetary_policy articles only)
        )
    """,
    "fact_macro_indicator": """
        CREATE TABLE IF NOT EXISTS fact_macro_indicator (
            record_id VARCHAR PRIMARY KEY,
            date_key VARCHAR,
            country_code VARCHAR,       -- NULL for the global rate-spillover benchmark
            indicator_id VARCHAR,
            value DOUBLE,
            value_change DOUBLE,
            is_interpolated BOOLEAN DEFAULT FALSE
        )
    """,
    "fact_commodity_price": """
        CREATE TABLE IF NOT EXISTS fact_commodity_price (
            price_id VARCHAR PRIMARY KEY,
            date_key VARCHAR,
            sector_id VARCHAR,
            price_index DOUBLE,
            price_change DOUBLE,
            production_volume_change DOUBLE
        )
    """,
    "fact_equity_valuation": """
        CREATE TABLE IF NOT EXISTS fact_equity_valuation (
            valuation_id VARCHAR PRIMARY KEY,
            date_key VARCHAR,
            sector_id VARCHAR,
            valuation_index DOUBLE,
            valuation_change DOUBLE,
            instrument_type VARCHAR
        )
    """,
    "fact_correlation_result": """
        CREATE TABLE IF NOT EXISTS fact_correlation_result (
            result_id VARCHAR PRIMARY KEY,
            country_code VARCHAR,
            sector_id VARCHAR,
            rate_basis VARCHAR,
            target_variable VARCHAR,
            control_set VARCHAR,
            date_range_start DATE,
            date_range_end DATE,
            lag INTEGER,
            pearson_r DOUBLE,
            partial_r DOUBLE,
            p_value DOUBLE,
            r_squared DOUBLE,
            computed_at TIMESTAMP
        )
    """,
    "bridge_article_country": """
        CREATE TABLE IF NOT EXISTS bridge_article_country (
            article_id VARCHAR,
            country_code VARCHAR
        )
    """,
    "bridge_article_sector": """
        CREATE TABLE IF NOT EXISTS bridge_article_sector (
            article_id VARCHAR,
            sector_id VARCHAR
        )
    """,
    "fact_interpretation": """
        CREATE TABLE IF NOT EXISTS fact_interpretation (
            cache_key VARCHAR PRIMARY KEY,
            country_code VARCHAR,
            sector_id VARCHAR,
            rate_basis VARCHAR,
            target_variable VARCHAR,
            control_used VARCHAR,
            interpretation_text VARCHAR,
            model VARCHAR,
            source VARCHAR,
            created_at TIMESTAMP
        )
    """,
    # Articles that arrived faster than the Gemini free tier's daily extraction
    # budget could absorb (see etl/extract_and_tag.py) — carried over to a
    # later run rather than force-failed with a 429 the moment quota runs out.
    "fact_extraction_queue": """
        CREATE TABLE IF NOT EXISTS fact_extraction_queue (
            article_id VARCHAR PRIMARY KEY,
            payload VARCHAR,
            queued_at TIMESTAMP
        )
    """,
    # Tracks Gemini extraction calls actually attempted per UTC day, so
    # extract_and_tag can stop before hitting the free tier's real 20/day
    # ceiling instead of discovering it via repeated 429s.
    "dim_extraction_budget": """
        CREATE TABLE IF NOT EXISTS dim_extraction_budget (
            date_key VARCHAR PRIMARY KEY,
            calls_used INTEGER
        )
    """,
}

# Load order respects FK-ish dependencies even though DuckDB/BigQuery don't enforce them here.
TABLE_ORDER = [
    "dim_source", "dim_country", "dim_sector", "dim_indicator",
    "fact_article", "fact_macro_indicator", "fact_commodity_price",
    "fact_equity_valuation", "fact_correlation_result",
    "bridge_article_country", "bridge_article_sector",
    "fact_interpretation", "fact_extraction_queue", "dim_extraction_budget",
]

SECTORS = [
    ("copper", "Copper", "metal"),
    ("lithium", "Lithium", "metal"),
    ("nickel", "Nickel", "metal"),
    ("rare_earths", "Rare earths", "metal"),
    ("cobalt", "Cobalt", "metal"),
    ("semiconductors", "Semiconductors", "semiconductor"),
]

INDICATORS = [
    ("policy_rate", "Policy interest rate", "%", "BIS", False),
    ("real_interest_rate", "Real interest rate (policy_rate - cpi_inflation)", "%", "derived", True),
    ("fdi_net_inflow", "FDI net inflows", "% of GDP", "World Bank", False),
    ("cpi_inflation", "CPI inflation", "%", "World Bank WDI", False),
    ("gdp_growth", "GDP growth", "%", "World Bank WDI", False),
    ("unemployment_rate", "Unemployment rate", "%", "World Bank WDI", False),
    ("trade_balance", "Trade balance (net exports)", "% of GDP", "World Bank WDI", False),
    ("reer", "Real effective exchange rate", "index", "BIS", False),
    ("gov_debt_pct_gdp", "Government debt", "% of GDP", "World Bank WDI / IMF GFS", False),
    ("fed_funds_rate_global", "US Federal Funds Rate (global benchmark)", "%", "FRED", False),
    ("usd_index", "Trade-weighted US Dollar Index", "index", "FRED", False),
    ("treasury_yield_10y", "US 10-year Treasury yield", "%", "FRED", False),
]
