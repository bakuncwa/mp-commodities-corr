output "api_main_url" {
  value = google_cloud_run_v2_service.api_main.uri
}

output "api_export_url" {
  value = google_cloud_run_v2_service.api_export.uri
}

output "raw_cache_bucket" {
  value = google_storage_bucket.raw_cache.name
}

output "bigquery_dataset" {
  value = google_bigquery_dataset.main.dataset_id
}

output "firebase_hosting_url" {
  value = "https://${var.project_id}.web.app"
}

output "service_account_email" {
  value = google_service_account.app.email
}
