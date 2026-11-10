# Terraform Drift Agent

An **observe-first** Terraform drift agent for a large Azure estate. It runs
Terraform itself in an isolated worker, reduces the plan to metadata, applies a
deterministic risk policy, asks an existing Azure AI Foundry model to explain the
drift, and — only for low and medium risk — proposes a validated patch as a
draft pull request.

Terraform detects facts. Policy controls safety. AI reasons about context and
proposes code. GitHub provides review and audit. **Nothing is ever applied.**

This repository implements phases 0–3 of the design document plus most of phase
4: registry, policy, audit contracts, detection, Azure Activity correlation,
Foundry classification, repository-aware remediation, validation, draft PRs,
notifications and structured observability. See [Not implemented](#not-implemented)
for the deliberate gaps.

---

## How a scan works

```
prepare ─ copy checkout to a temp dir (source tree is never touched)
   │
   ▼
init ─── terraform init -input=false [+ backend config]
   │
   ▼
detect ─ terraform plan -refresh-only -detailed-exitcode -out=drift.tfplan
   │            exit 0 → clean: audit, stop. No model call, no notification.
   │            exit 2 → drift, continue.
   ▼
parse ── terraform show -json → addresses, resource ids, actions, attribute names
   │      (never values, never outputs' values, never secrets)
   ▼
enrich ─ Azure Activity Log for the affected resource ids
   │      (evidence is only attributed when the resource id matches)
   ▼
classify ─ deterministic risk policy; hard rules cannot be relaxed by the model
   │
   ▼
explain ─ Foundry model (JSON output): summary, cause, risk, proposed diff
   │       the model may escalate risk; it can never lower it
   ▼
remediate ─ guarded patch: protected paths, module preservation, scope limits
   │         high/blocked risk never receives a patch (NEEDS_MANUAL_REVIEW)
   ▼
validate ─ terraform fmt -check → validate → fresh plan → compare addresses
   │        an unrelated or destructive post-change plan discards the patch
   ▼
publish ─ branch → single commit → draft PR (only in pr/auto mode, never dry run)
   │
   ▼
notify ── Teams/email with reduced facts only
   │
   ▼
audit ─── one JSON record per scan: scan_id, commit, hashes, risk, outcomes
```

## Operating modes

| Mode | Behaviour | Notes |
| --- | --- | --- |
| `observe` | Detect, classify, explain, audit, notify. No code change. | Default in the registry and the container. |
| `suggest` | Everything above plus a validated patch, held in the run record only. | Use to evaluate model quality. |
| `pr` | Validated patch → branch → one commit → **draft** pull request. | Recommended production mode. |
| `auto` | Only when the matched policy rule sets `auto_allowed: true` **and** the repository allowlists that rule id. | Downgrades to `pr` otherwise; never for high/blocked risk. |

`--dry-run` is stronger than any mode: no file is edited, no branch, commit, PR
or notification is created. The record lists the actions it *would* have taken.

## Safety controls

These are deterministic and enforced in code, not by prompt text:

- **Command allowlist.** Only `init`, `plan`, `show`, `fmt`, `validate` exist.
  `apply`, `destroy`, `import` and `state` raise `ValueError` before execution.
- **Environment hardening.** `TF_CLI_ARGS*` and `TF_LOG*` are stripped from the
  subprocess environment; `TF_DATA_DIR` is isolated; `TF_INPUT=0`,
  `TF_IN_AUTOMATION=1`; `shell=False`; stdin is closed; stderr is discarded.
- **Value-free by construction.** The parser emits addresses, resource ids,
  actions and changed attribute *names*. Whole-object sensitivity degrades to
  `[sensitive]`; per-attribute sensitivity is flagged, never expanded.
- **Blocked by policy.** Delete/replace, RBAC/identity, encryption, network
  exposure, SKU/availability and anything unclassified are high or blocked and
  receive no patch.
- **Patch guards.** Protected paths (`.github/workflows/**`, `backend.tf`,
  `providers.tf`, `*.tfbackend`, lock files, key material) are refused; a
  standalone `azurerm_*` resource is refused when the repository uses modules;
  provider/backend edits are refused; patches are limited to 3 files / 60 lines;
  patches apply all-or-nothing and cannot escape the checkout.
- **Post-change proof.** A patch survives only if `fmt -check`, `validate` and a
  fresh `plan` pass and that plan contains no address outside the approved drift
  set and no destructive action.
- **Evidence discipline.** Activity Log records are attached with
  `resource_match=false` unless the returned resource id equals the queried one,
  and only matched evidence is reported as an actor.
- **Degrade, never fail silent.** A missing Foundry, Azure, GitHub or Teams
  configuration is recorded as a reason; the drift finding still reaches the
  audit record and the report.

## Quick start

Requires Python 3.12+ and Terraform on `PATH`.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
drift-agent validate-config --config config/repos.example.yaml
drift-agent scan --repo tests/fixtures/clean --dry-run
drift-agent scan-all --config config/repos.yaml --mode observe --dry-run --max-concurrency 5

# Fleet rotation: the 10 least-recently-scanned repositories, then the rollup
drift-agent scan-all --config config/repos.yaml --limit 10 --dry-run
drift-agent fleet-report --config config/repos.yaml --format markdown
```

`--repo` accepts either a registry id or a local checkout path, which is handy
for a single run without any registry.

Reports are a single JSON object on **stdout**; structured JSON logs go to
**stderr**. That split is deliberate: a Container Apps Job keeps the report and
streams the logs.

### Exit codes

| Result | Report status | Exit |
| --- | --- | --- |
| Terraform plan exit 0 | `clean` | 0 |
| Terraform plan exit 2 | `drift` | 0 (or 2 with `--fail-on-drift`) |
| Any init/plan/show/parse/input failure | `error` | 1 |

Drift is a finding, not a job failure: exit 0 keeps the scheduled job green while
`--fail-on-drift` gives CI the strict behaviour.

## Fleet rotation

Scanning 100+ repositories in one sweep is slow and mostly wasted attention. The
agent instead keeps a small ledger — `reports/fleet/state.json`, override with
`DRIFT_FLEET_STATE` or `--fleet-state` — and scans the next slice on each run:

```bash
drift-agent scan-all --limit 10              # 10 least-recently-scanned repositories
drift-agent fleet-report --format markdown   # coverage, drift age, next batch
```

- Order is **never scanned first, then least recently scanned**, so no repository
  starves and full coverage arrives after `repositories / limit` runs.
- A repository scanned within `DRIFT_COOLDOWN_HOURS` (default 20) is skipped, so a
  second run on the same day continues the rotation instead of repeating it.
  `--force` ignores the cooldown.
- The ledger is rewritten after **every** repository, so a job stopped halfway
  through a slice still advances the rotation. A missing or corrupt ledger starts
  empty rather than blocking a scan.
- Drift age is tracked per repository: `drifting_since` is set on the first drift
  scan and cleared only by a clean scan. An errored scan is not evidence of
  recovery, so it leaves the streak intact.
- `fleet-report` rolls coverage up by subscription and owner, lists drifting
  repositories with their age and PR link, and reports how many runs remain until
  full coverage (`--batch`, `--stale-days`, `--next`, `--output`).
- Without `--limit`, `scan-all` keeps its original behaviour: every enabled
  repository, bounded by `--max-concurrency`.

The ledger is derived bookkeeping, not evidence — deleting it only resets the
rotation. Per-scan audit records remain the record of what was found.

## Configuration

### Registry — `config/repos.yaml`

```yaml
version: 1
repositories:
  - id: storage-prod
    url: https://github.com/example-org/storage-platform.git
    path: /workspace                 # mounted checkout for the worker
    default_branch: main
    terraform_root: environments/prod
    environment: prod
    owner_email: platform-storage@example.com
    teams_target: https://example.webhook.office.com/...
    azure_subscription_id: "00000000-0000-0000-0000-000000000000"
    mode: pr
    schedule: daily
    var_files: [prod.tfvars]
    backend_configs: [backend.hcl]
    auto_allowlist: [tags-metadata-only]
    protected_paths: [".github/workflows/**", backend.tf, providers.tf]
```

`terraform_root` and every `var_files` / `backend_configs` entry must resolve
inside the checkout; absolute paths and `..` are rejected.

### Risk policy — `config/risk-policy.yaml`

Rules are evaluated in file order, first match wins, and each rule must constrain
`actions_*` and/or `attributes_*` (globs, case-insensitive). `auto_allowed` is
only legal on low-risk rules, and `auto` mode additionally requires the
repository's `auto_allowlist` to contain that rule id. Unknown drift falls to the
`default`, which is high and requires review.

The shipped rules mirror the design document: destructive → blocked;
identity/RBAC, encryption, network exposure, SKU/availability → high; diagnostics
→ medium; tag-only → low and auto-eligible; a short allowlist of benign module
inputs → medium. Everything else is high.

### Environment — `.env.example`

| Group | Variables |
| --- | --- |
| Agent | `DRIFT_REPOS_CONFIG`, `DRIFT_RISK_POLICY`, `DRIFT_SKILL_PATH`, `DRIFT_AUDIT_DIR`, `DRIFT_FLEET_STATE`, `DRIFT_BATCH_SIZE`, `DRIFT_COOLDOWN_HOURS`, `DRIFT_TERRAFORM_TIMEOUT`, `DRIFT_MAX_CONCURRENCY`, `DRIFT_TFLINT_ENABLED`, `DRIFT_DRY_RUN`, `DRIFT_LOG_LEVEL` |
| Foundry | `FOUNDRY_ENDPOINT`, `FOUNDRY_DEPLOYMENT`, `FOUNDRY_API_VERSION`, `FOUNDRY_API_KEY` (prefer managed identity), `FOUNDRY_TIMEOUT` |
| Azure | `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_ACTIVITY_LOOKBACK_MINUTES`, `AZURE_ACTIVITY_MAX_RECORDS` |
| GitHub App | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_API_URL` |
| Notifications | `TEAMS_WEBHOOK_URL`, `GRAPH_SENDER` |

No secret values are read from the repository. The GitHub private key is a
**path** to a mounted secret file; Foundry and Graph prefer managed identity.

### Finding model context

`skills/terraform-drift/SKILL.md` is loaded as model context on every drift. Only
the Terraform root's `*.tf` files (redacted, size-capped) are included — never
`.tfvars`, state or plan files.

## Report shape

```json
{
  "scan_id": "…", "correlation_id": "…", "repository": "storage-prod",
  "commit_sha": "…", "terraform_root": "environments/prod", "mode": "pr",
  "dry_run": false, "status": "drift", "terraform_exit_code": 2,
  "resources": [{
    "address": "module.storage.azurerm_storage_account.this",
    "resource_id": "/subscriptions/…/storageAccounts/stpilot",
    "actions": ["update"],
    "changed_attributes": [{"name": "tags", "sensitive": false}],
    "risk": {"level": "low", "policy_rule": "tags-metadata-only",
             "requires_review": false, "auto_allowed": true}
  }],
  "outputs": [],
  "azure_activity": [{"operation": "…/write", "caller": "…", "resource_match": true}],
  "risk": {"level": "low", "policy_rule": "tags-metadata-only", "requires_review": false},
  "remediation": {"status": "pr_created", "patch_hash": "…", "pr_url": "…",
                  "validation": {"passed": true, "steps": []}},
  "notification": {"sent": ["teams"], "failed": []},
  "durations_ms": {"total": 41230}
}
```

## Permissions

| Area | Runtime permission |
| --- | --- |
| Azure reads | `Reader` on the pilot scope (`reader_scope_id`) |
| Azure Activity Log | Read access to the Activity Log for the same scope |
| Terraform backend | State/blob read plus only the operations `init`/refresh needs |
| GitHub | GitHub App: Metadata read, Contents read/write, Pull requests read/write |
| Foundry | Ability to invoke the existing model deployment (managed identity preferred) |
| Key Vault | Secrets User, only for the GitHub key and optional webhook |
| Notifications | Teams webhook or Graph `sendMail` for the configured sender |

Never start with Contributor. Remediation is code/PR based, so no Azure write
permission is required.

## Container

```bash
docker build -t terraform-drift-agent:0.2.0 .
docker run --rm \
  --mount "type=bind,source=$PWD/tests/fixtures/clean,target=/workspace,readonly" \
  terraform-drift-agent:0.2.0 scan --repo /workspace --dry-run
```

Python 3.12 slim, Terraform pinned via `--build-arg TERRAFORM_VERSION=…`, `git`
and CA certificates installed, runs as UID 10001, no Docker socket, no
credentials, `DRIFT_DRY_RUN=true` by default.

## Azure deployment

`deploy/terraform/` creates **only** what does not exist yet: a user-assigned
identity, a Reader assignment, an Azure Files environment storage registration
for the pre-staged checkout, and the Container Apps Job (with Key Vault-backed
secrets and a Secret volume for the GitHub key). The Foundry endpoint, model
deployment, Key Vault, environment and image are inputs — the module never
creates a Foundry resource.

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in, keep untracked
terraform init
terraform plan -out job.tfplan
terraform apply job.tfplan
```

Assumptions encoded as variables: subscription, resource group, region,
environment id, workload profile name, image reference, the existing Foundry
endpoint and deployment name, the Key Vault id and secret names, the checkout
storage account/share, reader scope, schedule cron, mode and `dry_run`.

The checkout share must be **pre-staged** outside this module (for example an
Azure Files sync of a trusted checkout). Nothing here clones repositories, and
the worker never persists refreshed state. The image ships only
`config/repos.example.yaml`, so the fleet registry must live on that share and is
pointed at by `repos_config_path` (default `/workspace/fleet/repos.yaml`).

### Cost profile

Container Apps **Jobs** on the Consumption plan bill only while an execution
runs — nothing when the job is idle — and every subscription gets 180,000 free
vCPU-seconds and 360,000 free GiB-seconds per month. A small rotation therefore
costs effectively nothing:

- Defaults are one execution at a time (`parallelism = 1`,
  `replica_retry_limit = 0`, one completed replica per execution), sized at
  `container_cpu = 0.5` / `container_memory = "1Gi"`, scanning
  `batch_size = 2` repositories via `scan-all --limit`.
- Five minutes of work at that size is ~150 vCPU-s and ~300 GiB-s. Two
  repositories a day for a month is roughly 4,500 vCPU-s — about 2.5% of the
  free grant, so **no compute charge**. Even 100 repositories a month in batches
  of 10 stays inside it.
- `replica_timeout_seconds` (default 1800) caps a stuck `terraform plan`.
- Keep `workload_profile_name = "Consumption"`; a dedicated workload profile
  bills continuously and defeats the purpose.
- The costs that do appear are around the job, not the job: the image registry
  (ACR Basic is a fixed monthly fee — reuse an existing one), the Azure Files
  shares (cents per GiB) and Log Analytics ingestion if the environment forwards
  container logs to a workspace (a batch of 1–2 is kilobytes per run).

### State that must survive an execution

Two things are written at runtime and both must outlive the container:

| Variable | Default | Why it matters |
| --- | --- | --- |
| `DRIFT_FLEET_STATE` | `/state/fleet/state.json` | Without it the rotation restarts at the first repository on every run |
| `DRIFT_AUDIT_DIR` | `/state/audit` | Per-scan evidence, kept per day |

The module mounts the checkout share **read-only** and registers a second,
writable Azure Files share (`state_share_name`, default `drift-state`) mounted at
`/state` for exactly these two paths. Create that share before `terraform apply`;
the worker never gets write access to the checkout it inspects.

## Tests

```bash
python -m pytest        # unit + contract tests
ruff check src tests    # lint
```

The suite covers the document's required cases: Terraform exit codes 0/1/2, plan
JSON parsing, sensitive-value redaction, risk rules, protected-path blocking,
module-preserving remediation, high risk → `NEEDS_MANUAL_REVIEW`, an unrelated
post-change plan rejecting the patch, GitHub/notification clients untouched in
dry runs, and a clean repository causing **zero** Foundry calls. Real-Terraform
fixtures under `tests/fixtures/` cover clean, output-only drift and a missing
variable; that test skips when `terraform` is not on `PATH`.

## Disable and roll back

1. Set `DRIFT_DRY_RUN=true` (container default) — external side effects stop.
2. Drop the repository `mode` to `observe`, or set `enabled: false`.
3. Set `schedule_cron = ""` and redeploy to return the job to manual trigger.
4. To stop everything, delete or disable the Container Apps Job; no Azure state
   is managed by the agent, so there is nothing to unwind.
5. Draft PRs are never merged automatically: closing them is a complete rollback
   of any proposed remediation.

## Not implemented

Deliberately out of scope for this slice, and therefore honest gaps:

- `auto` merge/remediation execution. AUTO only ever opens a draft PR; it never
  applies or merges.
- MCP server exposure of the tools (the design defers it until a second consumer
  exists).
- Repository cloning / GitHub App checkout inside the worker. The job mounts a
  pre-staged checkout.
- A dashboard or triage UI. `fleet-report` produces the consolidated JSON/Markdown
  over many runs, but there is no hosted view, no waiver-with-expiry and no
  assignment workflow beyond the registry's `owner_email`.
- Live Azure drift seeding in CI: the suite proves detection mechanics, not a
  real out-of-band change against a subscription. Run `observe` against a pilot
  repository before trusting `pr` mode.
