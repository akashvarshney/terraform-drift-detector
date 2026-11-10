locals {
  github_key_path = "/var/run/secrets/github/github-private-key"
  teams_enabled   = var.teams_webhook_secret_name != ""
  foundry_key     = var.foundry_api_key_secret_name != ""
  scheduled       = var.schedule_cron != ""
}

resource "azurerm_user_assigned_identity" "job" {
  name                = "${var.job_name}-identity"
  resource_group_name = var.resource_group_name
  location            = var.location
}

# Read-oriented pilot identity: Reader on the given scope only.
resource "azurerm_role_assignment" "reader" {
  scope                = var.reader_scope_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

# Required so the job identity can resolve the Key Vault-backed secrets at
# runtime (GitHub App key and the optional webhook). Scope is the vault only.
resource "azurerm_role_assignment" "key_vault_secrets" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

resource "azurerm_container_app_environment_storage" "checkout" {
  name                         = "checkout"
  container_app_environment_id = var.container_app_environment_id
  account_name                 = var.checkout_storage_account_name
  share_name                   = var.checkout_share_name
  access_key                   = var.checkout_storage_account_key
  access_mode                  = "ReadOnly"
}

# Writable share for the two runtime artefacts that must outlive an execution:
# the rotation ledger and the audit records. The checkout share stays read-only,
# so the worker can never edit the repositories it inspects.
resource "azurerm_container_app_environment_storage" "state" {
  name                         = "state"
  container_app_environment_id = var.container_app_environment_id
  account_name                 = var.checkout_storage_account_name
  share_name                   = var.state_share_name
  access_key                   = var.checkout_storage_account_key
  access_mode                  = "ReadWrite"
}

# Private registries deny anonymous pulls: the job identity needs AcrPull on the
# registry, otherwise every execution fails before the agent starts.
resource "azurerm_role_assignment" "acr_pull" {
  count                = var.container_registry_id == "" ? 0 : 1
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

resource "azurerm_container_app_job" "drift" {
  name                         = var.job_name
  resource_group_name          = var.resource_group_name
  location                     = var.location
  container_app_environment_id = var.container_app_environment_id
  workload_profile_name        = var.workload_profile_name

  replica_timeout_in_seconds = var.replica_timeout_seconds
  replica_retry_limit        = 0

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.job.id]
  }

  dynamic "manual_trigger_config" {
    for_each = local.scheduled ? [] : [1]
    content {
      parallelism = 1
    }
  }

  dynamic "schedule_trigger_config" {
    for_each = local.scheduled ? [var.schedule_cron] : []
    content {
      cron_expression = schedule_trigger_config.value
      parallelism     = 1
    }
  }

  secret {
    name                = "github-private-key"
    key_vault_secret_id = "${var.key_vault_id}/secrets/${var.github_private_key_secret_name}"
    identity            = azurerm_user_assigned_identity.job.id
  }

  dynamic "secret" {
    for_each = local.teams_enabled ? [var.teams_webhook_secret_name] : []
    content {
      name                = "teams-webhook"
      key_vault_secret_id = "${var.key_vault_id}/secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.job.id
    }
  }

  dynamic "secret" {
    for_each = local.foundry_key ? [var.foundry_api_key_secret_name] : []
    content {
      name                = "foundry-api-key"
      key_vault_secret_id = "${var.key_vault_id}/secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.job.id
    }
  }

  template {
    container {
      name   = "drift-agent"
      image  = var.container_image
      cpu    = var.container_cpu
      memory = var.container_memory

      # Rotation mode: one small slice per execution. The ledger on the state
      # share remembers what was already covered, so a large fleet is covered
      # over successive cheap runs instead of one expensive sweep.
      args = [
        "scan-all",
        "--mode", var.default_mode,
        "--limit", tostring(var.batch_size),
        "--max-concurrency", tostring(var.max_concurrency),
      ]

      env {
        name  = "DRIFT_DRY_RUN"
        value = tostring(var.dry_run)
      }

      env {
        name  = "DRIFT_REPOS_CONFIG"
        value = var.repos_config_path
      }

      env {
        name  = "DRIFT_FLEET_STATE"
        value = var.fleet_state_path
      }

      env {
        name  = "DRIFT_AUDIT_DIR"
        value = var.audit_dir
      }

      # DefaultAzureCredential needs the tenant to authenticate and the client id
      # to select this user-assigned identity when the environment has more than one.
      env {
        name  = "AZURE_TENANT_ID"
        value = var.tenant_id
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.job.client_id
      }

      env {
        name  = "DRIFT_RISK_POLICY"
        value = "/app/config/risk-policy.yaml"
      }

      env {
        name  = "FOUNDRY_ENDPOINT"
        value = var.foundry_endpoint
      }

      env {
        name  = "FOUNDRY_DEPLOYMENT"
        value = var.foundry_deployment
      }

      env {
        name  = "GITHUB_APP_ID"
        value = var.github_app_id
      }

      env {
        name  = "GITHUB_APP_INSTALLATION_ID"
        value = var.github_app_installation_id
      }

      env {
        name  = "GITHUB_APP_PRIVATE_KEY_PATH"
        value = local.github_key_path
      }

      dynamic "env" {
        for_each = local.teams_enabled ? [1] : []
        content {
          name        = "TEAMS_WEBHOOK_URL"
          secret_name = "teams-webhook"
        }
      }

      dynamic "env" {
        for_each = local.foundry_key ? [1] : []
        content {
          name        = "FOUNDRY_API_KEY"
          secret_name = "foundry-api-key"
        }
      }

      volume_mounts {
        name = "checkout"
        path = var.checkout_mount_path
      }

      volume_mounts {
        name = "state"
        path = var.state_mount_path
      }

      volume_mounts {
        name = "github-key"
        path = "/var/run/secrets/github"
      }
    }

    volume {
      name         = "checkout"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.checkout.name
    }

    volume {
      name         = "state"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.state.name
    }

    volume {
      name         = "github-key"
      storage_type = "Secret"
    }
  }

  lifecycle {
    # The image tag is rolled forward outside Terraform by the release pipeline.
    ignore_changes = [template[0].container[0].image]
  }
}
