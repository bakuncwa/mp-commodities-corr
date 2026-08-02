terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

# Firebase resources (firebase.tf) are google-beta only as of provider v6.
provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

# --- APIs this project actually touches. Enabling explicitly rather than relying on
# --- implicit enablement keeps `terraform plan` a true preview of what changes. ---
locals {
  required_apis = [
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firebase.googleapis.com",
    "firebasehosting.googleapis.com",
    "eventarc.googleapis.com",
    "pubsub.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each                   = toset(local.required_apis)
  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# --- Service account shared by the Functions/Cloud Run services. One identity,
# --- scoped IAM bindings below, rather than the default compute SA with broad rights. ---
resource "google_service_account" "app" {
  account_id   = "mp-commodities-corr"
  display_name = "mp-commodities-corr ETL/API service account"
  project      = var.project_id
}

resource "google_project_iam_member" "app_bigquery" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# --- Secrets: Gemini/FRED keys never sit in plain env vars on the deployed functions ---
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"
  project   = var.project_id
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "gemini_api_key" {
  secret      = google_secret_manager_secret.gemini_api_key.id
  secret_data = var.gemini_api_key
}

resource "google_secret_manager_secret" "fred_api_key" {
  secret_id = "fred-api-key"
  project   = var.project_id
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "fred_api_key" {
  secret      = google_secret_manager_secret.fred_api_key.id
  secret_data = var.fred_api_key
}

# --- Cloud Storage: raw article cache (staging buffer, not the analytical store) ---
resource "google_storage_bucket" "raw_cache" {
  name                        = "${var.project_id}-mp-commodities-raw"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition { age = var.raw_cache_retention_days }
    action { type = "Delete" }
  }
}

# --- Artifact Registry: slim multi-stage images, pruned on a schedule (see README's
# --- Deployment & Container Hosting Cost Notes on the 0.5GB free-tier ceiling) ---
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "mp-commodities-corr"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]

  cleanup_policies {
    id     = "keep-last-5-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "86400s" # 1 day — a 7-day grace period let 21 old versions (6.6GB) pile up during a single day of active redeploys before this ever triggered
    }
  }
}
