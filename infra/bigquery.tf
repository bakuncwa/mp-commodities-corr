# Star schema — table/column names must stay in lockstep with common/schema.py.
# There is no FK enforcement in BigQuery; the pairing discipline (fact tables keyed
# by date_key + country_code/sector_id) is application-level, same as the README says.

resource "google_bigquery_dataset" "main" {
  project    = var.project_id
  dataset_id = var.bq_dataset
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "dim_source" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "dim_source"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "source_id", type = "STRING", mode = "REQUIRED" },
    { name = "source_name", type = "STRING" },
    { name = "source_type", type = "STRING" },
    { name = "home_country_code", type = "STRING" },
  ])
}

resource "google_bigquery_table" "dim_country" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "dim_country"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "country_code", type = "STRING", mode = "REQUIRED" },
    { name = "country_name", type = "STRING" },
    { name = "region", type = "STRING" },
  ])
}

resource "google_bigquery_table" "dim_sector" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "dim_sector"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "sector_id", type = "STRING", mode = "REQUIRED" },
    { name = "sector_name", type = "STRING" },
    { name = "sector_category", type = "STRING" },
  ])
}

resource "google_bigquery_table" "dim_indicator" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "dim_indicator"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "indicator_id", type = "STRING", mode = "REQUIRED" },
    { name = "indicator_name", type = "STRING" },
    { name = "unit", type = "STRING" },
    { name = "source_name", type = "STRING" },
    { name = "is_derived", type = "BOOLEAN" },
  ])
}

resource "google_bigquery_table" "fact_article" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_article"
  project             = var.project_id
  deletion_protection = false
  time_partitioning {
    type  = "MONTH"
    field = "published"
  }
  schema = jsonencode([
    { name = "article_id", type = "STRING", mode = "REQUIRED" },
    { name = "date_key", type = "STRING" },
    { name = "source_id", type = "STRING" },
    { name = "article_category", type = "STRING" },
    { name = "policy_subtype", type = "STRING" },
    { name = "sentiment_score", type = "FLOAT64" },
    { name = "sentiment_target", type = "STRING" },
    { name = "source_url", type = "STRING" },
    { name = "title", type = "STRING" },
    { name = "published", type = "TIMESTAMP" },
    { name = "mech", type = "STRING" },
    { name = "policy_stance", type = "STRING" },
  ])
}

resource "google_bigquery_table" "fact_macro_indicator" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_macro_indicator"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "record_id", type = "STRING", mode = "REQUIRED" },
    { name = "date_key", type = "STRING" },
    { name = "country_code", type = "STRING" },
    { name = "indicator_id", type = "STRING" },
    { name = "value", type = "FLOAT64" },
    { name = "value_change", type = "FLOAT64" },
    { name = "is_interpolated", type = "BOOLEAN" },
  ])
}

resource "google_bigquery_table" "fact_commodity_price" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_commodity_price"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "price_id", type = "STRING", mode = "REQUIRED" },
    { name = "date_key", type = "STRING" },
    { name = "sector_id", type = "STRING" },
    { name = "price_index", type = "FLOAT64" },
    { name = "price_change", type = "FLOAT64" },
    { name = "production_volume_change", type = "FLOAT64" },
  ])
}

resource "google_bigquery_table" "fact_equity_valuation" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_equity_valuation"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "valuation_id", type = "STRING", mode = "REQUIRED" },
    { name = "date_key", type = "STRING" },
    { name = "sector_id", type = "STRING" },
    { name = "valuation_index", type = "FLOAT64" },
    { name = "valuation_change", type = "FLOAT64" },
    { name = "instrument_type", type = "STRING" },
  ])
}

resource "google_bigquery_table" "fact_correlation_result" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_correlation_result"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "result_id", type = "STRING", mode = "REQUIRED" },
    { name = "country_code", type = "STRING" },
    { name = "sector_id", type = "STRING" },
    { name = "rate_basis", type = "STRING" },
    { name = "target_variable", type = "STRING" },
    { name = "control_set", type = "STRING" },
    { name = "date_range_start", type = "DATE" },
    { name = "date_range_end", type = "DATE" },
    { name = "lag", type = "INTEGER" },
    { name = "pearson_r", type = "FLOAT64" },
    { name = "partial_r", type = "FLOAT64" },
    { name = "p_value", type = "FLOAT64" },
    { name = "r_squared", type = "FLOAT64" },
    { name = "computed_at", type = "TIMESTAMP" },
  ])
}

resource "google_bigquery_table" "bridge_article_country" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "bridge_article_country"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "article_id", type = "STRING" },
    { name = "country_code", type = "STRING" },
  ])
}

resource "google_bigquery_table" "bridge_article_sector" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "bridge_article_sector"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "article_id", type = "STRING" },
    { name = "sector_id", type = "STRING" },
  ])
}

resource "google_bigquery_table" "fact_interpretation" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_interpretation"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "cache_key", type = "STRING", mode = "REQUIRED" },
    { name = "country_code", type = "STRING" },
    { name = "sector_id", type = "STRING" },
    { name = "rate_basis", type = "STRING" },
    { name = "target_variable", type = "STRING" },
    { name = "control_used", type = "STRING" },
    { name = "interpretation_text", type = "STRING" },
    { name = "model", type = "STRING" },
    { name = "source", type = "STRING" },
    { name = "created_at", type = "TIMESTAMP" },
  ])
}

resource "google_bigquery_table" "fact_extraction_queue" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "fact_extraction_queue"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "article_id", type = "STRING", mode = "REQUIRED" },
    { name = "payload", type = "STRING" },
    { name = "queued_at", type = "TIMESTAMP" },
  ])
}

resource "google_bigquery_table" "dim_extraction_budget" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "dim_extraction_budget"
  project             = var.project_id
  deletion_protection = false
  schema = jsonencode([
    { name = "date_key", type = "STRING", mode = "REQUIRED" },
    { name = "calls_used", type = "INTEGER" },
  ])
}
