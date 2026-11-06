# Terraform Drift Agent: Phase 1

Minimal, observe-only detector for a trusted Terraform checkout. Designed to run
once and exit in an Azure Container Apps Job. No Azure resources are provisioned
by this project.

## Included

- Copy a supplied checkout to a fresh temporary directory, preserving sibling modules.
- Run only `terraform init`, `terraform plan -refresh-only -detailed-exitcode`, and
  `terraform show -json` when changes are found.
- Emit one JSON report with a scan ID, timestamp, clean/drift/error status,
  Terraform exit code, resource addresses, actions, changed top-level attribute
  names, changed output names, and a hash of the plan JSON when available.
- Remove temporary checkout and plan files after the scan.

Not included: repository cloning or GitHub App authentication, registry,
multi-repository scheduling, AI, Azure activity correlation, notifications,
remediation, durable audit storage, or Azure infrastructure deployment.
Supply an existing checkout for this first slice of Phase 1.

## Local setup

Requires Python 3.12+ and Terraform on PATH. The container pins Terraform 1.14.1;
choose a version compatible with the pilot repository and its state.

PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\drift-agent.exe --repo tests/fixtures/clean
```

Use `py -3.12` instead when Python 3.12 is installed. Smoke fixtures use no cloud
resources, external providers, credentials, or `apply` commands. The integration
test skips when Terraform is absent.

Scan a real checkout only after configuring its backend, credentials, and inputs:

```powershell
.\.venv\Scripts\drift-agent.exe --repo C:/repos/pilot --terraform-root environments/dev --workspace default --var-file dev.tfvars --backend-config backend.hcl
```

`--terraform-root` is relative to the checkout. Input file paths are relative to
the Terraform root and must remain inside the checkout. Both file options are
optional and repeatable. Automatic `.tfvars` loading still works. The default
workspace is explicitly `default`; use `--workspace` for any other existing
workspace. The worker never creates a workspace. Initialization starts fresh,
so backend settings from an existing `.terraform` directory are not reused.

The default timeout is 600 seconds per Terraform command, configurable using
`--timeout`. State locking remains enabled with a 60-second lock wait.

## Reports and exit codes

Example report fields (IDs, timestamp, and hash omitted here):

```json
{
  "mode": "observe",
  "status": "drift",
  "terraform_exit_code": 2,
  "resources": [{
    "address": "module.storage.azurerm_storage_account.this",
    "actions": ["update"],
    "changed_attributes": ["tags"]
  }],
  "outputs": []
}
```

| Result | Report status | Process exit code |
| --- | --- | --- |
| Terraform plan exit 0 | `clean` | 0 |
| Terraform plan exit 2 | `drift` | 0 |
| Initialization, plan, show, parsing, or input failure | `error` | 1 |

Terraform exit code 2 means a successful scan with changes, not a failed job.
`--fail-on-drift` changes the process exit code to 2 for local/CI use. Do not use
that flag in Container Apps Jobs unless drift should be treated as job failure.

Refresh-only compares observed infrastructure with saved state. It does not
detect every configuration-versus-infrastructure difference, discover unmanaged
resources, or generate a remediation plan. Output-only changes also produce
`drift`, with an empty resource list and a populated output list.

## Container

Start Docker Desktop with Linux containers enabled, then:

```powershell
docker build --tag terraform-drift-agent:phase1 .
docker run --rm --mount "type=bind,source=$((Resolve-Path tests/fixtures/clean).Path),target=/workspace,readonly" terraform-drift-agent:phase1 --repo /workspace
```

The image uses Python 3.12, includes Git for Terraform module downloads, runs as
non-root UID 10001, and contains no repository checkout, state, or credentials.
Override the Terraform version at build time with
`--build-arg TERRAFORM_VERSION=<approved-version>` when required.

For a real scan, mount the full checkout read-only and provide provider/backend
authentication separately. Do not bake credentials, state, or a pilot repository
into the image. The source mount must be readable by UID 10001.

## Azure Container Apps Jobs target

Deployment is a separate step. The minimum configuration is:

| Setting | Initial value |
| --- | --- |
| Workload profile | Consumption |
| Image | This image pushed to an approved registry |
| Trigger | Manual for the pilot; Schedule after verification |
| CPU / memory | Start with 1 vCPU / 2 GiB; measure pilot usage |
| Arguments | `--repo /workspace --terraform-root environments/dev` plus required file/workspace options |
| Parallelism / completion count | 1 / 1 |
| Replica timeout | 1900 seconds with the default per-command timeout |
| Retry limit | 0 during initial validation |
| Checkout | Full trusted checkout staged in a mounted volume; not included here |
| Identity | Managed identity with narrowly scoped provider/backend access |
| Logs | Retain the single reduced JSON report per scan |

A scheduled job has its own cron scheduler; a Function App is not required.
Container Apps Jobs cannot use a bind mount from your laptop. Before deploying,
provide a mounted checkout (for example, a pre-staged Azure Files share) or add a
reviewed GitHub App checkout step. The image alone does not fetch repositories.
No subscription, registry, identity, mount, or credentials have been configured.

For Azure, use managed identity and provider/backend-supported environment
configuration. Resource Reader access alone may not satisfy Terraform backend
blob lease/locking operations. Scope backend permissions independently. Avoid
Contributor access to managed infrastructure for a read-oriented scan.

## Safety and limitations

- No `apply`, destroy, state mutation commands, HCL edits, or pull requests.
- Never print raw Terraform stdout/stderr, plan values, output values, or resource
  IDs. Failure reports contain a stage, safe reason, and exit code only. Diagnose
  detailed errors in a separately authorized local session; do not publish raw
  plan JSON or provider logs.
- Report addresses and output/attribute names are metadata, not anonymized data.
  Keep secrets out of names and `for_each` keys; protect access to report logs.
- `TF_CLI_ARGS*` and `TF_LOG*` are removed from subprocess environments to avoid
  hidden targeting flags or sensitive debug logs. `TF_DATA_DIR` is isolated.
  Provider credentials and Terraform input environment variables remain usable.
- Run only trusted repositories and providers. Terraform providers and external
  data sources can execute code even during planning. A command allowlist is not
  a sandbox for untrusted HCL. Use references within the checkout; absolute local
  module/state paths are not isolated by copying the checkout.
- The mounted checkout must be a stable snapshot while copying. Local state, if
  used, is copied with it; prefer an appropriately secured remote backend for
  shared environments. The worker does not persist refreshed state.
- Temporary files can contain secrets. Normal completion removes them, but a
  forcibly terminated host process may leave files until its container is removed.
- Live Azure resource drift is not seeded by the test suite. Before relying on
  the pilot, scan representative repositories with real backend access and
  confirm a harmless out-of-band change appears in `resource_drift`.

## References

- [Terraform JSON format](https://developer.hashicorp.com/terraform/internals/json-format)
- [Terraform plan options](https://developer.hashicorp.com/terraform/cli/commands/plan)
- [Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs)
- [Container Apps billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)