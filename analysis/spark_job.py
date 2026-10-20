"""
Opt-in Apache Spark runner for the same correlation/regression batch as
analysis/correlation_regression.run_analysis() — not deployed, not in
requirements.txt (see requirements-spark.txt).

Each (country, sector) pair is independent, so the job is embarrassingly
parallel: panels are built once on the driver via load_panels(), unioned into
one Spark DataFrame, and groupBy(country_code, sector_id).applyInPandas() runs
the unchanged run_one() per pair on the executors. Output rows are identical
to the pandas path.

At the current scale (13 pairs × a few hundred monthly rows) the pandas job is
faster: JVM start-up and Arrow serialisation dominate. This exists for
scale-out — thousands of pairs, daily/intraday granularity, or a bootstrap /
rolling-window sweep — where per-pair work outgrows a single Cloud Run job.

Run: `make analyze-spark` (local[*]) or spark-submit with --master <cluster>.
"""
from __future__ import annotations

import logging
from datetime import timezone

import pandas as pd

from analysis.correlation_regression import MIN_OBS, bh_qvalues, load_panels, run_one
from common.db import get_store
from common.seed import COUNTRY_SECTOR_PAIRS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spark_job")

KEYS = ["country_code", "sector_id"]
RESULT_SCHEMA = (
    "result_id string, country_code string, sector_id string, rate_basis string, "
    "target_variable string, control_set string, date_range_start date, date_range_end date, "
    "lag int, pearson_r double, partial_r double, p_value double, r_squared double, "
    "computed_at timestamp"
)
RESULT_COLUMNS = [f.split()[0] for f in RESULT_SCHEMA.split(", ")]


def _analyse_pair(pdf: pd.DataFrame) -> pd.DataFrame:
    """Executor-side: one pair's panel in, its result rows out."""
    country_code, sector_id = pdf["country_code"].iloc[0], pdf["sector_id"].iloc[0]
    # The union gives every pair every indicator column; restore the per-pair
    # column set run_one() expects (absent series → absent column, not all-NaN).
    panel = pdf.drop(columns=KEYS).dropna(axis=1, how="all")
    panel = panel.sort_values("date_key").reset_index(drop=True)
    panel["time_index"] = panel["time_index"].astype(int)
    rows = []
    if len(panel) >= MIN_OBS:
        for rate_basis in ("nominal", "real"):
            for target_variable in ("equity_valuation", "commodity_price"):
                res = run_one(None, country_code, sector_id, rate_basis, target_variable, panel)
                if res:
                    rows.append(res)
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def run_spark_analysis(master: str = "local[*]", write: bool = True) -> pd.DataFrame:
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.master(master).appName("mp-commodities-corr")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .config("spark.sql.shuffle.partitions", str(len(COUNTRY_SECTOR_PAIRS)))
        .getOrCreate()
    )
    try:
        store = get_store()
        panels = load_panels(store, COUNTRY_SECTOR_PAIRS)
        long = pd.concat(
            [p.assign(country_code=c, sector_id=s) for (c, s), p in panels.items() if not p.empty],
            ignore_index=True,
        )
        out = (
            spark.createDataFrame(long)
            .groupBy(*KEYS)
            .applyInPandas(_analyse_pair, schema=RESULT_SCHEMA)
            .toPandas()
        )
    finally:
        spark.stop()

    out["computed_at"] = pd.to_datetime(out["computed_at"]).dt.tz_localize(timezone.utc)
    out = out[RESULT_COLUMNS]
    out["q_value_bh"] = bh_qvalues(out["p_value"])
    if write and not out.empty:
        store.upsert_df("fact_correlation_result", out, ["result_id"])
    log.info("Spark job computed %d correlation results across %d pairs (written: %s)", len(out), len(panels), write)
    return out


if __name__ == "__main__":
    run_spark_analysis()
