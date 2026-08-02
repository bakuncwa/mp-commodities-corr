# Static frontend hosting — no container, no request-quota overlap with the API
# (see README's rationale for the Firebase/Cloud Run split).

resource "google_firebase_project" "default" {
  provider   = google-beta
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

resource "google_firebase_hosting_site" "frontend" {
  provider   = google-beta
  project    = var.project_id
  site_id    = var.project_id
  depends_on = [google_firebase_project.default]
}

# Deploying actual content is a `firebase deploy --only hosting` step (see
# frontend/README or the GitHub Actions workflow), not something Terraform
# does — Terraform just provisions the site itself.
