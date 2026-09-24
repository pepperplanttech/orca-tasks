output "orchestrator_sa_email" {
  value       = google_service_account.orchestrator.email
  description = "Authorize this SA's NUMERIC client ID for domain-wide delegation."
}

output "orchestrator_sa_client_id" {
  value       = google_service_account.orchestrator.unique_id
  description = <<-EOT
    The numeric client ID to paste into the Workspace Admin console under
    Domain-wide delegation. NOT the email -- the console needs this number.
  EOT
}

output "drive_reader_sa_email" {
  value       = google_service_account.drive_reader.email
  description = "Share the brand assets folder with this address as Viewer."
}

output "job_name" {
  value = google_cloud_run_v2_job.orchestrator.name
}

output "schedule" {
  value = "${google_cloud_scheduler_job.tick.schedule} (${google_cloud_scheduler_job.tick.time_zone})"
}

output "manual_steps" {
  description = "Things Terraform cannot do. The system will not work until these are done."
  value       = <<-EOT

    1. DOMAIN-WIDE DELEGATION (Workspace super admin required)
       admin.google.com -> Security -> Access and data control ->
       API controls -> Domain-wide delegation -> Add new

         Client ID: ${google_service_account.orchestrator.unique_id}
         Scope:     https://www.googleapis.com/auth/tasks

       Until this is done every call succeeds up to the final token
       exchange, which fails with "unauthorized_client".

    2. SHARE THE BRAND FOLDER (if using brand assets)
       Share it with ${google_service_account.drive_reader.email} as Viewer.
       On a shared drive this may attach the account to the whole drive
       rather than the one folder -- verify the scope afterwards.

    3. CREATE THE TASK LIST
       A Google Tasks list named "${var.tasklist_title}" owned by
       ${var.workspace_user_email}.

    4. CHECK THE ROUTINES in Claude Code (created before deploying, since
       their trigger IDs and tokens were needed above). Confirm each prompt
       opts in to the <routine-fire-payload> block, or fired tasks arrive
       as inert context and the run does nothing.
  EOT
}
