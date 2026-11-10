# Deployment covers only the runtime components that do not exist yet.
# The Foundry project, Key Vault, registry and the mounted checkout share are
# referenced by variable; nothing here creates or replaces them.

variable "subscription_id" {
  description = "Subscription that hosts the Container Apps Job."
  type        = string
}

variable "resource_group_name" {
  description = "Existing resource group for the job and its identity."
  type        = string
}

variable "location" {
  description = "Azure region for the job and its identity."
  type        = string
}

variable "container_app_environment_id" {
  description = "Existing Container Apps environment (workload profile aware)."
  type        = string
}

variable "container_image" {
  description = "Fully qualified image reference of this repository's build."
  type        = string
}

variable "job_name" {
  description = "Container Apps Job name."
  type        = string
  default     = "terraform-drift-agent"
}

variable "workload_profile_name" {
  description = "Workload profile to run on; Consumption keeps the pilot cheap."
  type        = string
  default     = "Consumption"
}

variable "default_mode" {
  description = "Mode used by the scheduled scan: observe, suggest or pr."
  type        = string
  default     = "observe"
}

variable "max_concurrency" {
  description = "Repository scans per worker invocation."
  type        = number
  default     = 5
}

variable "dry_run" {
  description = "Keep true until the pilot has been reviewed end to end."
  type        = bool
  default     = true
}

variable "schedule_cron" {
  description = "Optional cron expression; empty means manual trigger only."
  type        = string
  default     = ""
}

variable "reader_scope_id" {
  description = "Scope granted Reader to the job identity (subscription or resource groups)."
  type        = string
}

# --- Existing resources ----------------------------------------------------
variable "key_vault_id" {
  description = "Existing Key Vault holding the GitHub App key and webhook secret."
  type        = string
}

variable "foundry_endpoint" {
  description = "Existing Azure AI Foundry deployment endpoint. Not created here."
  type        = string
}

variable "foundry_deployment" {
  description = "Existing model deployment name in that Foundry project."
  type        = string
}

variable "foundry_api_key_secret_name" {
  description = "Optional Key Vault secret name holding a Foundry key; prefer managed identity."
  type        = string
  default     = ""
}

variable "github_app_id" {
  description = "GitHub App id."
  type        = string
}

variable "github_app_installation_id" {
  description = "GitHub App installation id for the scanned organisation."
  type        = string
}

variable "github_private_key_secret_name" {
  description = "Key Vault secret name holding the GitHub App private key (PEM)."
  type        = string
}

variable "teams_webhook_secret_name" {
  description = "Optional Key Vault secret name holding the Teams webhook URL."
  type        = string
  default     = ""
}

# --- Mounted checkout ------------------------------------------------------
variable "checkout_storage_account_name" {
  description = "Storage account holding the pre-staged repository share."
  type        = string
}

variable "checkout_storage_account_key" {
  description = "Access key for the environment storage registration (kept in Key Vault)."
  type        = string
  sensitive   = true
}

variable "checkout_share_name" {
  description = "Azure Files share that contains the trusted, pre-staged checkout."
  type        = string
}

variable "checkout_mount_path" {
  description = "Container path where the checkout share is mounted."
  type        = string
  default     = "/workspace"
}

variable "repos_config_path" {
  description = "Registry YAML path inside the mounted share; the image ships only an example."
  type        = string
  default     = "/workspace/fleet/repos.yaml"
}
