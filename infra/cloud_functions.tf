# Run infra/build_function_packages.sh before `terraform plan/apply` — it
# assembles infra/dist/<name>/ (job.py + common/ + main.py entry point +
# requirements.txt) for each function; archive_file below just zips it.

data "google_project" "current" {
  project_id = var.project_id
}

# Cloud Storage's own service agent needs to publish to Pub/Sub for Eventarc to
# see object-finalize events at all; the function's runtime SA needs to be able
# to receive them. Both are required for the extract_and_tag storage trigger
# below — without them Eventarc trigger creation fails with a permission error
# that looks unrelated to either of these roles.
resource "google_project_iam_member" "gcs_pubsub_publisher" {
  project    = var.project_id
  role       = "roles/pubsub.publisher"
  member     = "serviceAccount:service-${data.google_project.current.number}@gs-project-accounts.iam.gserviceaccount.com"
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "app_eventarc_receiver" {
  project    = var.project_id
  role       = "roles/eventarc.eventReceiver"
  member     = "serviceAccount:${google_service_account.app.email}"
  depends_on = [google_project_service.apis]
}

data "archive_file" "ingest_feeds_zip" {
  type        = "zip"
  source_dir  = "${path.module}/dist/ingest_feeds"
  output_path = "${path.module}/dist/ingest_feeds.zip"
}

data "archive_file" "extract_and_tag_zip" {
  type        = "zip"
  source_dir  = "${path.module}/dist/extract_and_tag"
  output_path = "${path.module}/dist/extract_and_tag.zip"
}

data "archive_file" "ingest_monthly_zip" {
  type        = "zip"
  source_dir  = "${path.module}/dist/ingest_monthly"
  output_path = "${path.module}/dist/ingest_monthly.zip"
}

resource "google_storage_bucket" "function_source" {
  name                        = "${var.project_id}-mp-commodities-fn-source"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
}

resource "google_storage_bucket_object" "ingest_feeds_src" {
  name   = "ingest_feeds-${data.archive_file.ingest_feeds_zip.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.ingest_feeds_zip.output_path
}

resource "google_storage_bucket_object" "extract_and_tag_src" {
  name   = "extract_and_tag-${data.archive_file.extract_and_tag_zip.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.extract_and_tag_zip.output_path
}

resource "google_storage_bucket_object" "ingest_monthly_src" {
  name   = "ingest_monthly-${data.archive_file.ingest_monthly_zip.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.ingest_monthly_zip.output_path
}

# --- RSS ingestion: hourly via Cloud Scheduler HTTP trigger (see scheduler.tf) ---
resource "google_cloudfunctions2_function" "ingest_feeds" {
  name     = "ingest-feeds"
  project  = var.project_id
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "main"
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.ingest_feeds_src.name
      }
    }
  }

  service_config {
    available_memory      = "512Mi"
    timeout_seconds       = 300
    service_account_email = google_service_account.app.email
    environment_variables = {
      DB_BACKEND           = "bigquery"
      GOOGLE_CLOUD_PROJECT = var.project_id
      BQ_DATASET           = var.bq_dataset
      GCS_RAW_BUCKET       = google_storage_bucket.raw_cache.name
    }
  }
}

# --- Structured extraction: triggered by a new object landing in the raw cache bucket ---
resource "google_cloudfunctions2_function" "extract_and_tag" {
  name     = "extract-and-tag"
  project  = var.project_id
  location = var.region
  depends_on = [
    google_project_iam_member.gcs_pubsub_publisher,
    google_project_iam_member.app_eventarc_receiver,
  ]

  build_config {
    runtime     = "python311"
    entry_point = "main"
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.extract_and_tag_src.name
      }
    }
  }

  service_config {
    available_memory      = "512Mi"
    timeout_seconds       = 540
    service_account_email = google_service_account.app.email
    environment_variables = {
      DB_BACKEND           = "bigquery"
      GOOGLE_CLOUD_PROJECT = var.project_id
      BQ_DATASET           = var.bq_dataset
    }
    secret_environment_variables {
      key        = "GEMINI_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.gemini_api_key.secret_id
      version    = "latest"
    }
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.storage.object.v1.finalized"
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.app.email
    event_filters {
      attribute = "bucket"
      value     = google_storage_bucket.raw_cache.name
    }
  }
}

# Cloud Functions gen2 runs on an auto-created Cloud Run service of the same
# name — the Eventarc trigger's SA needs roles/run.invoker on *that* service
# specifically, separate from the project-level eventarc.eventReceiver role
# above. Without this, Eventarc retries the delivery and logs "not
# authenticated ... lacks run.routes.invoke" indefinitely.
resource "google_cloud_run_v2_service_iam_member" "extract_and_tag_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.extract_and_tag.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app.email}"
}

# --- Macro-indicator + commodity/equity ingestion, combined into one function
# --- behind one Cloud Scheduler job: Cloud Scheduler's free tier is 3 jobs per
# --- billing account, and this project already uses one for ingest_feeds and
# --- one for the correlation batch job — a 4th (one per ingestion script)
# --- would cost ~$0.10/month. The two ETL scripts stay independent; this
# --- combination is a deploy-only wrapper (see build_function_packages.sh),
# --- not a code-level merge. ---
resource "google_cloudfunctions2_function" "ingest_monthly" {
  name     = "ingest-monthly"
  project  = var.project_id
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "main"
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.ingest_monthly_src.name
      }
    }
  }

  service_config {
    available_memory      = "512Mi"
    timeout_seconds       = 540
    service_account_email = google_service_account.app.email
    environment_variables = {
      DB_BACKEND           = "bigquery"
      GOOGLE_CLOUD_PROJECT = var.project_id
      BQ_DATASET           = var.bq_dataset
    }
    secret_environment_variables {
      key        = "FRED_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.fred_api_key.secret_id
      version    = "latest"
    }
  }
}
