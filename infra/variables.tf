variable "project_id" {
  description = "GCP project to deploy into. No default on purpose — this must be explicitly chosen, not accidentally applied against whichever project gcloud happens to have active."
  type        = string
}

variable "region" {
  description = "Primary region for Cloud Run, Cloud Functions, and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "bq_dataset" {
  description = "BigQuery dataset name for the star schema."
  type        = string
  default     = "mp_commodities_corr"
}

variable "gemini_api_key" {
  description = "Gemini API key for etl/extract_and_tag.py. Stored in Secret Manager, not as a plain env var on the function."
  type        = string
  sensitive   = true
}

variable "fred_api_key" {
  description = "FRED API key for etl/ingest_macro_indicators.py."
  type        = string
  sensitive   = true
  default     = ""
}

variable "raw_cache_retention_days" {
  description = "Cloud Storage lifecycle rule for the raw article JSON cache (README: 30-day rolling window, the extracted fact_article row is the durable copy)."
  type        = number
  default     = 30
}

variable "billing_account_id" {
  description = "Billing account linked to project_id — used only to attach a low-threshold budget alert as a safety net, not to provision anything billable by itself."
  type        = string
}

