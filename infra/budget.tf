# Safety net for the "stay in free tier" requirement — this doesn't prevent
# spend by itself (a hard cap would also risk killing free-tier services), but
# alerts billing account admins by email at 50%/90%/100% of a deliberately
# tiny $1 monthly amount, so any unexpected charge (e.g. Artifact Registry
# crossing 0.5GB, an egress spike) surfaces immediately instead of silently
# accumulating.
resource "google_billing_budget" "guardrail" {
  billing_account = var.billing_account_id
  display_name    = "mp-commodities-corr free-tier guardrail"

  budget_filter {
    # The Billing API stores and returns the project *number*; using the ID
    # here produced a perpetual in-place diff on every plan.
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "1"
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
