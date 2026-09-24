variable "project_id" {
  type        = string
  description = "GCP project to deploy into. Must already exist and have billing linked."
}

variable "region" {
  type        = string
  default     = "us-east4"
  description = "Region for the Cloud Run job and scheduler."
}

variable "workspace_user_email" {
  type        = string
  description = <<-EOT
    The Google Workspace user whose task list is read. The orchestrator
    impersonates this user via domain-wide delegation, so the tasks, and the
    Klaviyo/Shopify accounts behind the routine, are all seen as this person.
  EOT
}

variable "tasklist_title" {
  type        = string
  default     = "Agent Tasks"
  description = "Title of the Google Tasks list to read."
}

variable "brand_assets_folder_id" {
  type        = string
  default     = ""
  description = <<-EOT
    Drive folder ID holding brand assets, shared read-only with the Drive
    reader service account. Leave empty to run without brand assets.
  EOT
}

variable "routines" {
  type = map(object({
    trigger_id = string
  }))
  description = <<-EOT
    Claude Code routines, keyed by the exact name used in a task's
    "Routine:" line. The key becomes an env var suffix: "Klaviyo Campaign"
    -> ROUTINE_ID_KLAVIYO_CAMPAIGN / ROUTINE_TOKEN_KLAVIYO_CAMPAIGN.

    Tokens live in var.routine_tokens, keyed identically. They are separate
    because Terraform forbids for_each over a sensitive value -- the map
    keys would end up in resource addresses.
  EOT
}

variable "routine_tokens" {
  type        = map(string)
  sensitive   = true
  description = "Routine API bearer tokens, keyed exactly as var.routines."

  validation {
    condition     = alltrue([for t in values(var.routine_tokens) : startswith(t, "sk-ant-oat")])
    error_message = "Routine tokens should start with sk-ant-oat -- check you pasted the routine's API token and not an API key."
  }
}

variable "image" {
  type        = string
  description = "Fully qualified container image, built and pushed by deploy.sh."
}

variable "schedule" {
  type        = string
  default     = "*/30 * * * *"
  description = "Cron schedule for the orchestrator tick."
}

variable "time_zone" {
  type        = string
  default     = "America/New_York"
  description = "Time zone for the schedule."
}

variable "reaper_timeout_seconds" {
  type        = number
  default     = 86400
  description = <<-EOT
    How long a task may sit at "running" before it is marked "stalled".
    This covers HUMAN REVIEW latency, not run time -- a task leaves
    "running" when someone ticks it off after reviewing the draft. Reaping
    too early releases the sequential gate and fires the next task before
    anyone has looked at the last one.
  EOT
}

variable "deployer_email" {
  type        = string
  description = "User running the deploy; granted serviceAccountUser so Cloud Run can deploy as the orchestrator SA."
}
