output "job_id" {
  description = "Container Apps Job resource id."
  value       = azurerm_container_app_job.drift.id
}

output "job_name" {
  description = "Container Apps Job name, used by the manual trigger."
  value       = azurerm_container_app_job.drift.name
}

output "identity_principal_id" {
  description = "Principal id to reuse in additional narrow role assignments."
  value       = azurerm_user_assigned_identity.job.principal_id
}

output "identity_client_id" {
  description = "Client id for workload identity federation, when used."
  value       = azurerm_user_assigned_identity.job.client_id
}

output "checkout_mount_path" {
  description = "Container path of the mounted checkout share."
  value       = var.checkout_mount_path
}
