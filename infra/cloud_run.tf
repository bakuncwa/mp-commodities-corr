# Images referenced here are built and pushed separately (Cloud Build / CI —
# see .github/workflows), not by Terraform. `terraform apply` will fail on first
# run until each image tag below has been pushed at least once; that's expected
# for a Cloud Run resource, not a bug in this config.

locals {
  api_main_image   = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/api-main:latest"
  api_export_image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/api-export:latest"
  analysis_image   = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/analysis:latest"
}

resource "google_cloud_run_v2_service" "api_main" {
  name                = "mp-commodities-api"
  project             = var.project_id
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.app.email
    containers {
      image = local.api_main_image
      env {
        name  = "DB_BACKEND"
        value = "bigquery"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "BQ_DATASET"
        value = var.bq_dataset
      }
      # GET /api/interpret calls Gemini directly (see analysis/interpret.py) —
      # the same secret extract_and_tag's Cloud Function already reads, bound
      # here too rather than a plaintext env var.
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
    }
    scaling {
      min_instance_count = 0 # scale-to-zero — this is the request-serving API, not the heavier export path
      max_instance_count = 5
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "api_main_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api_main.name
  role     = "roles/run.invoker"
  member   = "allUsers" # read-only public API; the frontend calls it directly from the browser
}

# --- Export service: isolated container (openpyxl/reportlab), separate from api_main ---
resource "google_cloud_run_v2_service" "api_export" {
  name                = "mp-commodities-export"
  project             = var.project_id
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.app.email
    containers {
      image = local.api_export_image
      env {
        name  = "DB_BACKEND"
        value = "bigquery"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "BQ_DATASET"
        value = var.bq_dataset
      }
      resources {
        limits = { cpu = "1", memory = "1Gi" } # openpyxl/reportlab need more headroom than the main API
      }
    }
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "api_export_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api_export.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Correlation/regression batch job: Cloud Run Job, not a Service — runs to
# --- completion on the schedule in scheduler.tf, matching the README's "scheduled
# --- batch job, not per-request" design. ---
resource "google_cloud_run_v2_job" "correlation_regression" {
  name                = "correlation-regression"
  project             = var.project_id
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.app.email
      timeout         = "1800s" # full lag sweep across every country/sector pair
      containers {
        image = local.analysis_image
        env {
          name  = "DB_BACKEND"
          value = "bigquery"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "BQ_DATASET"
          value = var.bq_dataset
        }
        resources {
          limits = { cpu = "2", memory = "2Gi" }
        }
      }
    }
  }
}
