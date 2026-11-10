# Setup and end-to-end test guide

Two goals: get the agent running **locally** against a real repository, then
deploy it into your landing zone and prove the whole path with a manual job
execution before you turn on a schedule.

Read [azure-architecture.md](azure-architecture.md) first for the design and the
identity model. Everything below uses placeholders — replace `<...>` with your
own values and never commit them.

Order of work:

| Step | What | Time |
| --- | --- | --- |
| [0](#0-prerequisites) | Prerequisites and access | — |
| [1](#1-create-the-github-app-once) | GitHub App (once, by an org admin) | 15 min |
| [2](#2-deploy-identity-with-oidc) | Deploy principal with GitHub OIDC | 15 min |
| [3](#3-landing-zone-prerequisites) | Subscription, network, environment, ACR, Key Vault, storage | platform team |
| [4](#4-build-and-push-the-image) | Build and push the image from a private runner | 10 min |
| [5](#5-stage-the-registry-and-checkout) | Seed the shares | 15 min |
| [6](#6-deploy-the-job) | `terraform apply` (the only apply in the project) | 10 min |
| [7](#7-local-end-to-end-test) | Local end-to-end test, including a real draft PR | 30 min |
| [8](#8-azure-end-to-end-test) | Azure end-to-end test with a manual execution | 30 min |
| [9](#9-turn-on-the-schedule) | Schedule, then widen the batch | 5 min |

---

## 0. Prerequisites

**Tools (local and on the runner):** Python 3.12+, Terraform 1.9+ (image ships
1.14.1), Docker, Azure CLI, and `git`.

**Access you need:**

| Purpose | Permission |
| --- | --- |
| Create the job, identity, role assignments | `Contributor` + `User Access Administrator` on the job resource group (or pre-created assignments) |
| Read the storage key during deploy | `Key Vault Secrets User` on the secrets vault |
| Push the image | `AcrPush` on the registry |
| Create the GitHub App | GitHub org owner |

**Values to collect:** tenant id, workload subscription id, job resource group,
Container Apps environment id, ACR login server and resource id, Key Vault id,
Foundry endpoint + deployment name, storage account name + key, GitHub org/repo,
and the email/webhook for notifications.

---

## 1. Create the GitHub App (once)

This is the identity the **runtime** uses to open draft PRs. It is unrelated to
the OIDC login your pipeline uses for Azure.

1. GitHub → **Organization settings → Developer settings → GitHub Apps → New GitHub App**.
2. Fill in:
   - **Name:** `drift-agent-<org>`
   - **Homepage URL:** your internal wiki page
   - **Webhook:** uncheck *Active* (the agent is pull-based; it needs no webhooks)
   - **Repository permissions:**
     - Metadata: Read-only (mandatory)
     - Contents: Read and write
     - Pull requests: Read and write
   - **Where can this GitHub App be installed?** Only on this account
3. **Create GitHub App** → note the **App ID**.
4. **Generate a private key** → a `.pem` downloads once. Store it immediately:

```bash
az keyvault secret set \
  --vault-name <kv-name> \
  --name github-app-private-key \
  --file ./drift-agent.private.pem
rm ./drift-agent.private.pem
```

5. **Install App** → choose **Only select repositories** → pick the pilot repos
   (never "All repositories" for a first run).
6. Note the **installation id** from the installation settings URL
   (`.../settings/installations/<installation-id>`).

Record `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID`. The private key never
leaves Key Vault; the job mounts it at `/var/run/secrets/github`.

> The App must not be given Administration, Workflows, Actions or Secrets
> permissions. The agent never edits workflows, never merges and never pushes to
> a default branch.

---

## 2. Deploy identity with OIDC

No client secret is created — the pipeline exchanges a GitHub OIDC token for an
Azure token.

```bash
# 1. App registration + service principal
az ad app create --display-name sp-drift-deploy
APP_ID=$(az ad app list --display-name sp-drift-deploy --query "[0].appId" -o tsv)
az ad sp create --id "$APP_ID"

# 2. Federated credential for the deploy workflow (protected environment)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-prod",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<org>/<repo>:environment:prod",
  "audiences": ["api://AzureADTokenExchange"]
}'

# 3. Least-privilege role assignments
az role assignment create --assignee "$APP_ID" --role Contributor \
  --scope /subscriptions/<sub>/resourceGroups/<rg-drift-agent>
az role assignment create --assignee "$APP_ID" --role AcrPush \
  --scope /subscriptions/<sub>/resourceGroups/<rg-platform>/providers/Microsoft.ContainerRegistry/registries/<acr>
az role assignment create --assignee "$APP_ID" --role "Key Vault Secrets User" \
  --scope /subscriptions/<sub>/resourceGroups/<rg-platform>/providers/Microsoft.KeyVault/vaults/<kv>
```

Pipeline login (GitHub Actions):

```yaml
permissions:
  id-token: write
  contents: read
jobs:
  deploy:
    runs-on: [self-hosted, linux, private-vnet]
    environment: prod
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      - run: terraform -chdir=deploy/terraform init -input=false
      - run: terraform -chdir=deploy/terraform plan -input=false -out=job.tfplan
      - run: terraform -chdir=deploy/terraform apply -input=false -auto-approve job.tfplan
```

`terraform apply` here provisions **the agent's own hosting**. The agent itself
never applies anything: its Terraform allowlist is `init`, `plan`, `show`, `fmt`,
`validate`, and its Azure role is `Reader`.

---

## 3. Landing zone prerequisites

Platform team tasks, in this order:

1. **Vend the subscription** into `landing-zones` with the Corp archetype
   (`Azure/avm-ptn-alz-sub-vending/azure` does subscription + VNet + peering +
   private endpoints in one apply).
2. **Subnets** — `/23` for the Container Apps environment, a private-endpoint
   subnet, and a runner subnet. See
   [architecture §5](azure-architecture.md#5-network); the environment subnet
   cannot be resized later.
3. **Register resource providers**: `Microsoft.App`, `Microsoft.OperationalInsights`.
4. **Container Apps environment** (VNet-injected). This is an input to the repo's
   module, not something it creates:

```hcl
resource "azurerm_container_app_environment" "drift" {
  name                       = "cae-drift-prod"
  location                   = var.location
  resource_group_name        = "rg-drift-agent-prod"
  infrastructure_subnet_id   = var.aca_subnet_id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  # Only for a workload-profile environment; omit for Consumption-only.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
    minimum_count         = 0
    maximum_count         = 1
  }
}
```

5. **Private endpoints + DNS** for ACR, Key Vault, storage and Foundry, linked to
   the workload VNet.
6. **Firewall allowlist** for `registry.terraform.io`, `releases.hashicorp.com`,
   `objects.githubusercontent.com`, `github.com`, `api.github.com`,
   `management.azure.com`, `login.microsoftonline.com` — or set up a provider
   mirror and skip the first two.
7. **Storage account** (private endpoint) with two shares:

```bash
az storage share create --account-name <sa> --name pilot-checkout
az storage share create --account-name <sa> --name drift-state
```

8. **Key Vault** holding the GitHub App key and the Teams webhook, with
   "Allow trusted Microsoft services" enabled if public access is disabled
   (Container Apps resolves secrets on the platform side).

---

## 4. Build and push the image

Build on the private runner so the build reaches the private ACR:

```bash
docker build -t <acr>.azurecr.io/terraform-drift-agent:0.2.0 .
az acr login --name <acr>
docker push <acr>.azurecr.io/terraform-drift-agent:0.2.0
```

The image is ~500 MB including Terraform, runs as UID 10001, and sets
`DRIFT_DRY_RUN=true` — the module overrides it with `dry_run`, so the deployed
default is whatever your tfvars say. Keep it `true` until step 8 passes.

`az acr build` also works, but the ACR task needs egress to the base images; many
private landing zones cannot do that, which is why the runner build is the
recommended path.

---

## 5. Stage the registry and checkout

The job mounts the share read-only at `/workspace`; the registry lives on it.

```bash
# Pilot repository checkout (no .terraform, no state files)
az storage file upload-batch --account-name <sa> --destination pilot-checkout \
  --source ./checkout --pattern "*"

# Registry, pointed at container-side paths
az storage file upload --account-name <sa> --share-name pilot-checkout \
  --source ./repos.yaml --path fleet/repos.yaml
```

Registry rules that bite people:

- `path` must be the **container** path (`/workspace/<repo>`), never the local
  host path — otherwise you get `checkout_not_found`.
- `terraform_root` is relative to that path and must exist — otherwise
  `no_terraform_configuration_found`.
- `backend_configs` / `var_files` are relative to the Terraform root.
- `azure_subscription_id` per entry drives Activity Log correlation.
- Every id must be unique and lowercase.

Validate it locally before uploading:

```bash
drift-agent validate-config --config ./repos.yaml
```

---

## 6. Deploy the job

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars
# fill in: tenant_id, subscription_id, resource_group_name, environment id,
#          image, acr id, key vault id, foundry, storage account + key,
#          state_share_name, batch_size, dry_run
terraform init
terraform plan -out job.tfplan
terraform apply job.tfplan
```

What this creates, and nothing else: user-assigned identity, `Reader` on
`reader_scope_id`, `Key Vault Secrets User` on the vault, `AcrPull` on the
registry, the two storage registrations, and the Container Apps Job.

Keep `batch_size = 2` and `dry_run = true` for the first pass.

---

## 7. Local end-to-end test

Do this before the Azure run — it isolates agent bugs from infrastructure bugs.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,azure,github]"

# 7.1  Configuration and offline fixtures (no credentials needed)
drift-agent validate-config --config config/repos.example.yaml
drift-agent scan --repo tests/fixtures/clean --dry-run      # status: clean, exit 0
drift-agent scan-all --config config/repos.example.yaml --dry-run --limit 2
drift-agent fleet-report --format markdown

# 7.2  A real checkout, observe mode, no model and no writes
az login
export AZURE_TENANT_ID=<tenant-id> AZURE_SUBSCRIPTION_ID=<sub-id>
drift-agent scan --repo ./my-repo --mode observe --dry-run
```

Expected: `status: clean` or `status: drift` with `resources[].address` and
`changed_attributes[].name` — never values. Check `reports/audit/YYYY/MM/DD/`.

To force a real drift finding, change one tag out of band in a **sandbox**
subscription, then rescan: the low-risk tag rule should match and the remediator
should produce a patch in `suggest` mode:

```bash
drift-agent scan --repo ./my-repo --mode suggest --dry-run
```

Then the only external write in the whole system — a draft PR — against a
**scratch repository**:

```bash
export GITHUB_APP_ID=<app-id>
export GITHUB_APP_INSTALLATION_ID=<installation-id>
export GITHUB_APP_PRIVATE_KEY_PATH=/secure/path/drift-agent.private.pem
drift-agent scan --repo ./scratch-repo --mode pr
```

Expected: branch `drift-agent/...`, exactly one commit, a **draft** PR whose body
lists the risk decision and validation steps. Nothing is merged.

---

## 8. Azure end-to-end test

```bash
# Trigger one execution
az containerapp job start -g <rg> -n terraform-drift-agent

# Execution status
az containerapp job execution list -g <rg> -n terraform-drift-agent -o table
```

Console logs (either the portal: Job → Execution history → Console logs, or Log
Analytics if the environment forwards logs):

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "terraform-drift-agent"
| where Log_s has "scan_finished" or Log_s has "fleet_state_not_saved"
| order by TimeGenerated desc
| take 50
```

The report itself goes to **stdout** as one JSON object (exit 0 clean/drift, 1
error); structured logs go to **stderr**. Fetch the ledger to prove persistence:

```bash
az storage file download --account-name <sa> --share-name drift-state \
  --path fleet/state.json --dest ./state.json
cat state.json   # entries for the 2 repositories just scanned
```

Then prove the rotation: trigger a **second** execution and confirm it picks the
*next* two repositories, and a same-day third execution reports
`{"status":"skipped","reason":"no_repository_due"}`.

---

## 9. Turn on the schedule

1. Confirm the pilot findings look right (audit JSON + Teams message).
2. Set `dry_run = false` and re-apply.
3. Set `schedule_cron = "0 6 * * *"` and re-apply, or trigger manually each day
   (`schedule_cron = ""`).
4. Raise `batch_size` once the per-run cost and duration are understood — 5/day
   covers 100 repositories in 20 runs.

---

## Verification checklist

| # | Check | Expected |
| --- | --- | --- |
| 1 | `validate-config` on the real registry | `ok: true`, expected repository and rule counts |
| 2 | Clean fixture scan | `status: clean`, exit 0 |
| 3 | Drift fixture with `--fail-on-drift` | `status: drift`, exit 2 |
| 4 | First rotation run | `fleet.batch = 2`, ledger created |
| 5 | Second rotation run | Next 2 repositories, previous ones not repeated |
| 6 | Third run same day | `status: skipped`, `reason: no_repository_due` |
| 7 | `fleet-report` | Coverage counts and next batch match reality |
| 8 | Azure job execution | Succeeded; ledger `last_scanned_at` advanced on the share |
| 9 | Dry run (`dry_run = true`) | No branch, commit or PR created anywhere |
| 10 | PR mode on a scratch repo | Draft PR, one commit, never merged |
| 11 | Forced high-risk drift (e.g. delete/replace) | `needs_manual_review`, no patch, no PR |
| 12 | Foundry unreachable | Drift still recorded; analysis source `fallback` |
| 13 | Key Vault unreachable | Job fails visibly — it must not report clean |
| 14 | Unrelated post-change plan | Patch rejected, drift still reported |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Job fails immediately, image pull `401` | `AcrPull` missing, `container_registry_id` unset, or `privatelink.azurecr.io` DNS not linked | Set `container_registry_id`; check the role assignment and DNS zone link |
| Secret volume mount fails / `403` on Key Vault | No `Key Vault Secrets User`, or vault public access disabled without the trusted-services exception | Fix the role assignment; enable the exception |
| `terraform init` hangs, then times out | Firewall blocks `registry.terraform.io` / `releases.hashicorp.com` | Allowlist, or use a provider mirror on the state share |
| Azure Activity correlation always empty | `AZURE_TENANT_ID` not set, or `azure-mgmt-monitor` missing from the image | Module sets it now; verify `validate-config` reports `azure_activity: true` |
| `fleet_state_not_saved` in logs, rotation repeats | `/state` not writable (share missing, SMB permissions, wrong mount) | Check the state registration and `access_mode = "ReadWrite"` |
| `registry_unreadable: /workspace/fleet/repos.yaml` | Registry not uploaded, or `repos_config_path` wrong | Upload it and fix the variable |
| `checkout_not_found` for every repo | Registry `path` uses host paths | Use container paths (`/workspace/<repo>`) |
| `no_terraform_configuration_found` | `terraform_root` wrong or empty directory | Fix the relative root in the registry |
| Job succeeds but no PR appears | Mode is `observe`, `dry_run = true`, risk high, or GitHub App not installed on that repo | Check `remediation.status` and `reject_reason` in the audit JSON |
| Draft PR body empty / 422 from GitHub | Patched file outside the diff hunks, or protected path | Inspect `remediation.reject_reason`; confirm `protected_paths` |
| Costs higher than expected | Dedicated workload profile, or a new Premium ACR | Use Consumption and the shared registry |

---

## Rollback

1. `dry_run = true` and re-apply — all external side effects stop immediately.
2. Set `schedule_cron = ""` to return the job to manual triggering.
3. Drop suspicious repositories to `mode: observe` or `enabled: false`.
4. Draft PRs are the only artefacts in GitHub: closing them is a complete
   rollback of proposed remediation.
5. To remove everything, delete the Container Apps Job and its resource group;
   no target-subscription state is managed by this agent.
