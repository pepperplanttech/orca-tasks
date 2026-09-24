data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # "Klaviyo Campaign" -> "KLAVIYO_CAMPAIGN" (env var suffix)
  #                    -> "klaviyo-campaign" (secret name)
  routine_slugs = {
    for name, cfg in var.routines :
    name => upper(replace(name, "/[^A-Za-z0-9]+/", "_"))
  }
  routine_secret_names = {
    for name, cfg in var.routines :
    name => "routine-token-${lower(replace(name, "/[^A-Za-z0-9]+/", "-"))}"
  }

  services = [
    "iamcredentials.googleapis.com", # keyless signJwt -- the whole auth model
    "tasks.googleapis.com",
    "drive.googleapis.com",
    "secretmanager.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each = toset(local.services)
  service  = each.value

  # Disabling an API on destroy can break unrelated things in a shared
  # project, and is rarely what anyone wants.
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Identities
#
# Two service accounts, on purpose. The orchestrator impersonates a Workspace
# user via domain-wide delegation and can therefore act broadly as that user.
# The Drive reader has NO delegation -- it is a principal in its own right,
# and sees only what has been shared with its email address. Merging them
# would hand the campaign agent the orchestrator's reach over Drive.
# ---------------------------------------------------------------------------

resource "google_service_account" "orchestrator" {
  account_id   = "agent-tasks-orchestrator"
  display_name = "Agent Tasks Orchestrator"
  description  = "Reads the task list via domain-wide delegation and fires routines."
  depends_on   = [google_project_service.enabled]
}

resource "google_service_account" "drive_reader" {
  account_id   = "drive-asset-reader"
  display_name = "Drive Asset Reader"
  description  = "Read-only access to the shared brand folder. No domain-wide delegation."
  depends_on   = [google_project_service.enabled]
}

# The orchestrator signs its own delegation JWT, so it must be able to act as
# itself. Locally this role sits on the human; in Cloud Run the SA is the caller.
resource "google_service_account_iam_member" "orchestrator_self_sign" {
  service_account_id = google_service_account.orchestrator.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_service_account_iam_member" "orchestrator_impersonates_drive_reader" {
  service_account_id = google_service_account.drive_reader.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_service_account_iam_member" "deployer_acts_as_orchestrator" {
  service_account_id = google_service_account.orchestrator.name
  role               = "roles/iam.serviceAccountUser"
  member             = "user:${var.deployer_email}"
}

# Cloud Scheduler invokes the job as the orchestrator SA.
resource "google_project_iam_member" "orchestrator_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

# Newly created projects no longer grant the Compute Engine default SA broad
# rights, but Cloud Build still stages source through it. Without this, the
# first `gcloud builds submit` fails with a storage.objects.get denial.
resource "google_project_iam_member" "cloudbuild_default_sa" {
  project    = var.project_id
  role       = "roles/cloudbuild.builds.builder"
  member     = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  depends_on = [google_project_service.enabled]
}

# ---------------------------------------------------------------------------
# Routine tokens
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "routine_token" {
  for_each  = var.routines
  secret_id = local.routine_secret_names[each.key]

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "routine_token" {
  for_each    = var.routines
  secret      = google_secret_manager_secret.routine_token[each.key].id
  secret_data = var.routine_tokens[each.key]
}

resource "google_secret_manager_secret_iam_member" "orchestrator_reads_token" {
  for_each  = var.routines
  secret_id = google_secret_manager_secret.routine_token[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator.email}"
}

# ---------------------------------------------------------------------------
# The orchestrator tick
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "orchestrator" {
  name     = "orca-orchestrator"
  location = var.region

  template {
    # No task-level retries. A tick that patches "running" and fires, then
    # dies before finishing, must NOT be retried: /fire has no idempotency
    # key, so a retry means a second real campaign from one task. A stranded
    # task is recoverable; a duplicate campaign is not.
    task_count  = 1
    parallelism = 1

    template {
      max_retries     = 0
      timeout         = "600s"
      service_account = google_service_account.orchestrator.email

      containers {
        image = var.image

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }

        env {
          name  = "ORCHESTRATOR_SA_EMAIL"
          value = google_service_account.orchestrator.email
        }
        env {
          name  = "WORKSPACE_USER_EMAIL"
          value = var.workspace_user_email
        }
        env {
          name  = "AGENT_TASKLIST_TITLE"
          value = var.tasklist_title
        }
        env {
          name  = "DRIVE_READER_SA_EMAIL"
          value = google_service_account.drive_reader.email
        }
        env {
          name  = "BRAND_ASSETS_FOLDER_ID"
          value = var.brand_assets_folder_id
        }
        env {
          name  = "REAPER_TIMEOUT_SECONDS"
          value = tostring(var.reaper_timeout_seconds)
        }

        dynamic "env" {
          for_each = var.routines
          content {
            name  = "ROUTINE_ID_${local.routine_slugs[env.key]}"
            value = env.value.trigger_id
          }
        }

        dynamic "env" {
          for_each = var.routines
          content {
            name = "ROUTINE_TOKEN_${local.routine_slugs[env.key]}"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.routine_token[env.key].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.orchestrator_reads_token,
    google_project_service.enabled,
  ]
}

resource "google_cloud_scheduler_job" "tick" {
  name             = "orca-tick"
  region           = var.region
  schedule         = var.schedule
  time_zone        = var.time_zone
  attempt_deadline = "600s"

  # Scheduler-level retries would create a SECOND execution and defeat the
  # job's own max_retries = 0. Both layers must say zero.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.orchestrator.name}:run"

    oauth_token {
      service_account_email = google_service_account.orchestrator.email
    }
  }

  depends_on = [google_project_iam_member.orchestrator_run_invoker]
}
