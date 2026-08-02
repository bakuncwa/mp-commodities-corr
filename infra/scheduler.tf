# Cloud Scheduler triggers everything — no persistent orchestrator (README's
# stated reason for Scheduler+Functions over something like Airflow).

resource "google_service_account" "scheduler_invoker" {
  account_id   = "mp-commodities-scheduler"
  display_name = "Invokes ETL Cloud Functions and the correlation Cloud Run Job on a schedule"
  project      = var.project_id
}

resource "google_cloudfunctions2_function_iam_member" "scheduler_can_invoke_ingest_feeds" {
  project        = var.project_id
  location       = var.region
  cloud_function = google_cloudfunctions2_function.ingest_feeds.name
  role           = "roles/cloudfunctions.invoker"
  member         = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloudfunctions2_function_iam_member" "scheduler_can_invoke_monthly" {
  project        = var.project_id
  location       = var.region
  cloud_function = google_cloudfunctions2_function.ingest_monthly.name
  role           = "roles/cloudfunctions.invoker"
  member         = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_project_iam_member" "scheduler_can_run_jobs" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# --- RSS ingestion: 3x/day, not hourly. The Gemini free tier is 20
# --- extraction calls/day total across every article regardless of how often
# --- ingest_feeds itself runs, so polling hourly just meant most extraction
# --- attempts failed with 429 RESOURCE_EXHAUSTED; three evenly-spaced runs
# --- give extract_and_tag's daily budget tracker (see etl/extract_and_tag.py)
# --- three natural checkpoints to work through the queue instead of one. ---
resource "google_cloud_scheduler_job" "ingest_feeds" {
  name      = "ingest-feeds-3x-daily"
  project   = var.project_id
  region    = var.region
  schedule  = "0 6,14,22 * * *"
  time_zone = "Etc/UTC"

  http_target {
    uri         = google_cloudfunctions2_function.ingest_feeds.url
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }
}

# --- Macro-indicator + commodity/equity ingestion: monthly, combined into one
# --- job (see the ingest_monthly function above) to stay at 3 total Cloud
# --- Scheduler jobs — its free-tier allowance per billing account. ---
resource "google_cloud_scheduler_job" "ingest_monthly" {
  name      = "ingest-monthly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 6 3 * *" # 3rd of the month — gives upstream sources a couple days to publish the new month
  time_zone = "Etc/UTC"

  http_target {
    uri         = google_cloudfunctions2_function.ingest_monthly.url
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }
}

# --- Correlation/regression batch job: monthly, after the macro/market pulls above ---
resource "google_cloud_scheduler_job" "correlation_regression" {
  name      = "correlation-regression-monthly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 8 3 * *" # a couple hours after the two ingestion jobs on the same day
  time_zone = "Etc/UTC"

  http_target {
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.correlation_regression.name}:run"
    http_method = "POST"
    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }
}
