#!/usr/bin/env bash
# Assembles one deployable directory per Cloud Function under infra/dist/ —
# each needs the job module(s), the shared `common/` package, a
# Functions-Framework entry point (main.py), and requirements.txt at the zip
# root. Terraform's archive_file data source (see cloud_functions.tf) just
# zips whatever's here; it doesn't assemble a Python package tree across
# directories, so that step happens here instead. Run this before
# `terraform plan/apply`.
set -euo pipefail
cd "$(dirname "$0")/.."

DIST=infra/dist
WRAPPERS=infra/function_wrappers
rm -rf "$DIST"

build_one() {
  local fn=$1 wrapper=$2 entrypoint=$3
  mkdir -p "$DIST/$fn"
  cp "etl/${fn}.py" "$DIST/$fn/job.py"
  cp -r common "$DIST/$fn/common"
  [ -f etl/feeds.yaml ] && cp etl/feeds.yaml "$DIST/$fn/feeds.yaml"
  cp requirements.txt "$DIST/$fn/requirements.txt"
  echo "functions-framework>=3.5,<4" >> "$DIST/$fn/requirements.txt"
  sed "s/{ENTRYPOINT_FN}/${entrypoint}/g" "$WRAPPERS/$wrapper" > "$DIST/$fn/main.py"
  echo "Built $DIST/$fn (entry point: main, calling job.${entrypoint})"
}

build_one ingest_feeds    http_wrapper.py.tmpl    run_ingest
build_one extract_and_tag storage_wrapper.py.tmpl run_extraction

# ingest_macro_indicators + ingest_market_data are combined into one deployed
# function/schedule — purely to keep Cloud Scheduler at 3 jobs total (its free
# tier), not because the two ETL scripts are related in any other way. Both
# stay independently runnable locally via `make ingest-macro` / `make ingest-market`.
mkdir -p "$DIST/ingest_monthly"
cp etl/ingest_macro_indicators.py "$DIST/ingest_monthly/job_macro.py"
cp etl/ingest_market_data.py "$DIST/ingest_monthly/job_market.py"
cp -r common "$DIST/ingest_monthly/common"
cp requirements.txt "$DIST/ingest_monthly/requirements.txt"
echo "functions-framework>=3.5,<4" >> "$DIST/ingest_monthly/requirements.txt"
cp "$WRAPPERS/monthly_ingest_wrapper.py.tmpl" "$DIST/ingest_monthly/main.py"
echo "Built $DIST/ingest_monthly (entry point: main, calling job_macro.run_ingest + job_market.run_ingest)"

echo "Done. cloud_functions.tf zips infra/dist/<name> via archive_file."
