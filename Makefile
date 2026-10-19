.PHONY: setup seed ingest-feeds extract ingest-macro ingest-market analyze api export-api frontend all-local mock-rss ingest-feeds-local analyze-spark

VENV := .venv/bin

setup:
	python3.11 -m venv .venv
	$(VENV)/pip install --upgrade pip
	$(VENV)/pip install -r requirements-notebooks.txt

seed:
	$(VENV)/python -m common.seed

ingest-feeds:
	$(VENV)/python -m etl.ingest_feeds

# Simulates more RSS volume than the real active feeds provide — start this in
# one terminal, then `make ingest-feeds-local` in another (repeatable; each
# run generates a fresh batch, like a live feed that keeps publishing).
mock-rss:
	$(VENV)/python -m etl.mock_rss_server

ingest-feeds-local:
	USE_LOCAL_FEEDS=true $(VENV)/python -m etl.ingest_feeds

extract:
	$(VENV)/python -m etl.extract_and_tag

ingest-macro:
	$(VENV)/python -m etl.ingest_macro_indicators

ingest-market:
	$(VENV)/python -m etl.ingest_market_data

analyze:
	$(VENV)/python -m analysis.correlation_regression

# Same batch on Apache Spark (local[*]); `pip install -r requirements-spark.txt` first.
analyze-spark:
	$(VENV)/python -m analysis.spark_job

api:
	$(VENV)/uvicorn api.main:app --reload --port 8000

export-api:
	$(VENV)/uvicorn api.export:app --reload --port 8001

frontend:
	python3 -m http.server 5500 --directory frontend

# Full local pipeline, in dependency order. Requires GEMINI_API_KEY (extract)
# and FRED_API_KEY (macro indicators) in .env for those two steps to do
# anything beyond logging a skip.
all-local: seed ingest-feeds extract ingest-macro ingest-market analyze
