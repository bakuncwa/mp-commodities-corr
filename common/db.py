"""
Storage backend switch: DuckDB for local development (default), BigQuery for the
deployed Cloud Run / Cloud Functions services. Same table names and SQL both ways
(see schema.py) — ingestion and analysis code never imports duckdb or bigquery
directly, only this module.

Select the backend with DB_BACKEND=duckdb|bigquery (defaults to duckdb so the
project runs with zero GCP setup). BigQuery mode requires GOOGLE_CLOUD_PROJECT
and BQ_DATASET to be set, and application-default credentials to already be
configured (gcloud auth application-default login) — this module does not
provision the dataset or tables itself; infra/main.tf does that at deploy time.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterable

import pandas as pd
from dotenv import load_dotenv

from common.schema import DDL, TABLE_ORDER

load_dotenv()

DB_BACKEND = os.environ.get("DB_BACKEND", "duckdb").lower()
REPO_ROOT = Path(__file__).resolve().parent.parent
# A relative LOCAL_DB_PATH (the .env.example default) must resolve against the
# repo root, not the process cwd — otherwise running a script from notebooks/
# (nbconvert, Jupyter's default cwd) silently opens a second, empty DuckDB file
# instead of the one every other entry point uses.
_local_db_path = os.environ.get("LOCAL_DB_PATH", "data/warehouse.duckdb")
LOCAL_DB_PATH = str(REPO_ROOT / _local_db_path) if not os.path.isabs(_local_db_path) else _local_db_path
GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
BQ_DATASET = os.environ.get("BQ_DATASET", "mp_commodities_corr")


def new_id() -> str:
    return uuid.uuid4().hex


class DuckDBStore:
    def __init__(self, path: str):
        import duckdb
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(path)
        for table in TABLE_ORDER:
            self.con.execute(DDL[table])

    def query_df(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or {}).fetchdf()

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.con.execute(sql, params or {})

    def upsert_df(self, table: str, df: pd.DataFrame, pk_cols: Iterable[str]) -> int:
        """Delete-then-insert on the given key columns. Small tables (monthly
        macro/price/valuation series, per-run correlation results) so this is
        cheap; BigQueryStore below uses a real MERGE for the same call shape."""
        if df.empty:
            return 0
        pk_cols = list(pk_cols)
        self.con.register("_incoming", df)
        keys = ", ".join(pk_cols)
        self.con.execute(f"DELETE FROM {table} WHERE ({keys}) IN (SELECT {keys} FROM _incoming)")
        self.con.execute(f"INSERT INTO {table} SELECT * FROM _incoming")
        self.con.unregister("_incoming")
        return len(df)

    def table_exists_nonempty(self, table: str) -> bool:
        return self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0


class BigQueryStore:
    """Production backend. Assumes infra/main.tf already created the dataset and
    tables with a schema matching schema.py — this class only reads/writes rows."""

    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery
        self.bigquery = bigquery
        self.client = bigquery.Client(project=project)
        self.project = project
        self.dataset = dataset

    def _t(self, table: str) -> str:
        return f"`{self.project}.{self.dataset}.{table}`"

    def query_df(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        # default_dataset lets the same unqualified table names (fact_article,
        # not project.dataset.fact_article) work against both DuckDB and
        # BigQuery — without it, BigQuery rejects any bare table reference.
        kwargs = {"default_dataset": self.bigquery.DatasetReference(self.project, self.dataset)}
        if params:
            kwargs["query_parameters"] = [self.bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()]
        job_config = self.bigquery.QueryJobConfig(**kwargs)
        return self.client.query(sql, job_config=job_config).to_dataframe()

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.query_df(sql, params)

    def upsert_df(self, table: str, df: pd.DataFrame, pk_cols: Iterable[str]) -> int:
        if df.empty:
            return 0
        pk_cols = list(pk_cols)
        staging = f"{table}_staging_{new_id()[:8]}"
        # Load against the real target table's schema rather than letting
        # BigQuery auto-infer types from the DataFrame — a column that's all
        # None in this batch (e.g. dim_source.home_country_code, never
        # populated by any caller) can get inferred as INT64, which then
        # fails to MERGE into the real STRING column with a type mismatch.
        target_schema = self.client.get_table(f"{self.project}.{self.dataset}.{table}").schema
        load_config = self.bigquery.LoadJobConfig(schema=target_schema)
        self.client.load_table_from_dataframe(df, f"{self.project}.{self.dataset}.{staging}", job_config=load_config).result()
        on_clause = " AND ".join(f"T.{c} = S.{c}" for c in pk_cols)
        set_clause = ", ".join(f"{c} = S.{c}" for c in df.columns if c not in pk_cols)
        cols = ", ".join(df.columns)
        merge_sql = f"""
            MERGE {self._t(table)} T
            USING `{self.project}.{self.dataset}.{staging}` S
            ON {on_clause}
            WHEN MATCHED THEN UPDATE SET {set_clause}
            WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({cols})
        """
        self.client.query(merge_sql).result()
        self.client.delete_table(f"{self.project}.{self.dataset}.{staging}", not_found_ok=True)
        return len(df)


_store = None


def get_store():
    global _store
    if _store is not None:
        return _store
    if DB_BACKEND == "bigquery":
        if not GOOGLE_CLOUD_PROJECT:
            raise RuntimeError("DB_BACKEND=bigquery requires GOOGLE_CLOUD_PROJECT to be set")
        _store = BigQueryStore(GOOGLE_CLOUD_PROJECT, BQ_DATASET)
    else:
        _store = DuckDBStore(LOCAL_DB_PATH)
    return _store
