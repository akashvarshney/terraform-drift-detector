# Local setup and end-to-end test (Docker)

This guide runs the agent on your machine with Docker, against real Terraform,
without deploying anything to Azure. It is the fastest way to see the safety
behaviour before the landing-zone work in
[azure-setup-guide.md](azure-setup-guide.md).

Two rules hold throughout:

- The **agent** only runs `init`, `plan`, `show`, `fmt` and `validate`. It never
  applies, destroys or imports.
- The only `terraform apply` below is a **throwaway sandbox** you run yourself,
  with the `local` provider, so no cloud resource exists to change. It is what
  creates something for the agent to detect drift against.

Everything is parameterised with placeholders. Never put real identifiers or
secrets in a tracked file; `local/` is git-ignored.

Order of work:

| Step | What | Needs |
| --- | --- | --- |
| [1](#1-what-you-can-test-locally) | What is and is not testable locally | — |
| [2](#2-prerequisites) | Docker and disk | — |
| [3](#3-create-the-local-workspace) | Registry, directories, env file | 5 min |
| [4](#4-build-the-image) | Build the image | 5 min |
| [5](#5-create-real-drift-in-the-sandbox) | Apply the throwaway sandbox once | 2 min |
| [6](#6-run-the-agent) | Detect, classify, rotate, report | 5 min |
| [7](#7-scan-a-real-repository) | A clone of one of your repositories | 15 min |
| [8](#8-add-foundry-explanations) | Foundry endpoint + key | 10 min |
| [9](#9-add-azure-activity-correlation) | Read-only service principal | 10 min |
| [10](#10-open-a-draft-pr) | GitHub App + Foundry + a remediable finding | 20 min |
| [11](#11-plain-docker-without-compose) | `docker run` equivalents | — |
| [12](#12-exit-codes-and-artefacts) | Exit codes and artefacts | — |
| [13](#13-troubleshooting) | Symptom → cause → fix | — |
| [14](#14-clean-up) | Clean up | — |
| [15](#15-what-still-needs-azure) | What still needs Azure | — |

---

## 1. What you can test locally

| Capability | Locally, no credentials | Needs |
| --- | --- | --- |
| Real `terraform init` / refresh-only plan | yes | network for providers |
| Drift parsing, risk policy, guardrails | yes | — |
| Audit JSON and rotation ledger | yes | — |
| Foundry explanation and proposed diff | no | `FOUNDRY_*` |
| Azure Activity Log correlation | no | read-only service principal |
| Draft pull request | no | GitHub App, Foundry, a non-blocked finding |
| Managed identity, Key Vault secret volume | no | Azure only |
| Container Apps Job, schedule, private networking | no | Azure only |

The local path proves detection, classification and the "never remediate
destructive drift" rule end to end. The Azure path proves identity, networking
and the job runtime.

---

## 2. Prerequisites

- **Docker Engine 24+** or Docker Desktop 4.27+ (Compose v2.24+ for the optional
  `env_file.required` syntax in `compose.yaml`).
- ~2 GB free disk for the image, plus provider downloads per scan (~50 MB each
  on first use).
- Internet access for the build (`python:3.12-slim-bookworm`,
  `hashicorp/terraform`) and for `terraform init`.
- `git` on the host to clone a repository you want to scan.
- No host Terraform and no Azure CLI are required: Terraform is inside the
  image, and the container has no `az` CLI.

On Windows, use the WSL2 backend and keep the repository under the Linux
filesystem (for example `~/src/terraform-drift-detector`); bind mounts across
`/mnt/c` are dramatically slower. Run the commands in a bash shell (WSL or Git
Bash), where `$PWD` expands as written.

The container runs as **UID 10001** (not root) and has no Docker socket, no
cloud credentials and `DRIFT_DRY_RUN=true` by default.

---

## 3. Create the local workspace

From the repository root:

```bash
mkdir -p local/checkout local/sandbox local/reports
cp -r examples/local-drift/. local/sandbox/

# UID 10001 writes the audit records and the sandbox state.
chmod -R a+rwX local/sandbox local/reports
```

Registry — this is the same file format as production, but `path` is the
**container** path:

```bash
cat > local/repos.yaml <<'EOF'
version: 1

repositories:
  - id: local-sandbox
    path: /sandbox
    terraform_root: .
    environment: sandbox
    mode: observe

  - id: fixture-clean
    path: /fixtures/clean
    terraform_root: .
    environment: sandbox
    mode: observe
EOF
```

Why two entries: one has drift, one is clean, and together they demonstrate the
rotation. Add your real repositories later (§7).

Local environment — start with **no integrations**, so nothing is called and
nothing can fail:

```bash
cat > local/.env <<'EOF'
DRIFT_DRY_RUN=true
DRIFT_BATCH_SIZE=2
DRIFT_COOLDOWN_HOURS=20
DRIFT_LOG_LEVEL=INFO
EOF
chmod 600 local/.env
```

Paths do not need to be set: the image already points `DRIFT_REPOS_CONFIG` at
`/app/config/repos.yaml`, the audit directory at `/app/reports/audit`, and the
rotation ledger defaults to `reports/fleet/state.json` under `/app`. `local/` is
git-ignored, including `local/.env`.

Resulting layout:

```
compose.yaml
examples/local-drift/main.tf   # copied into local/sandbox
local/
  .env                         # local secrets, never committed
  repos.yaml                   # registry, container paths
  checkout/                    # clones of repositories under test
  sandbox/                     # throwaway Terraform config
  reports/audit/               # one JSON record per scan
  reports/fleet/state.json     # rotation ledger
```

---

## 4. Build the image

```bash
docker compose build

# Smoke test: the entrypoint is drift-agent.
docker run --rm terraform-drift-agent:0.2.0 --help
```

The build takes a few minutes the first time and produces a ~500 MB image
(Python 3.12 slim + Terraform 1.14.1 + `git`). The Terraform version is pinned in
`compose.yaml`; override it with a direct build if needed:

```bash
docker build --build-arg TERRAFORM_VERSION=1.14.1 \
  -t terraform-drift-agent:0.2.0 .
```

On Apple silicon the base images are usually multi-arch; if a pinned tag has no
`arm64` variant, add `--platform linux/amd64` (slower, but functional).

---

## 5. Create real drift in the sandbox

`examples/local-drift/main.tf` uses only the `hashicorp/local` provider: it
writes one file. You apply it **once, yourself**, then edit that file behind
Terraform's back. This is a genuine out-of-band change that a refresh-only plan
can see, with no Azure subscription involved.

```bash
docker compose run --rm sandbox init
docker compose run --rm sandbox apply -auto-approve

# Change the file out of band, inside the container (same UID as the agent).
docker compose run --rm --entrypoint sh sandbox -c \
  'echo "changed out of band" > marker.txt'
```

The `local` provider treats content that no longer matches as the object having
disappeared, so the next refresh-only plan reports the resource as `delete`
rather than `update`. That is exactly the destructive case the policy must
block, which makes it a useful first finding: the record should stop at
`needs_manual_review`, never a patch.

> Do not run `terraform apply` through the agent image's default entrypoint, and
> do not add `apply` anywhere in a scan path — the agent has no such command.

---

## 6. Run the agent

All commands append to the image entrypoint (`drift-agent`). Compose forwards
`local/.env`, mounts `local/reports`, and keeps stdout (the JSON report) separate
from stderr (structured logs).

### 6.1 Configuration check

```bash
docker compose run --rm agent validate-config
```

Expected: `"ok": true`, `"repositories": 2`, `"policy_rules": 9`, `"problems": []`,
`"dry_run": true`, and all four `integrations` false with a note for each.

### 6.2 A clean scan

```bash
docker compose run --rm agent scan --repo /fixtures/clean --dry-run
```

Expected: `"status": "clean"`, `"terraform_exit_code": 0`, empty `resources`, exit
code 0. A clean scan never calls Foundry and never notifies.

### 6.3 The drifted sandbox

```bash
docker compose run --rm agent scan --repo /sandbox --dry-run
```

Expected, abridged — the `delete` action lists every attribute of the resource,
and this sample keeps two of them:

```json
{
  "repository": "sandbox",
  "mode": "observe",
  "dry_run": true,
  "status": "drift",
  "terraform_exit_code": 2,
  "resources": [
    {
      "address": "local_file.marker",
      "actions": ["delete"],
      "changed_attributes": [
        {"name": "content", "sensitive": false},
        {"name": "filename", "sensitive": false}
      ],
      "risk": {
        "level": "blocked",
        "policy_rule": "state-destruction",
        "requires_review": true,
        "auto_allowed": false
      }
    }
  ],
  "risk": {
    "level": "blocked",
    "policy_rule": "state-destruction",
    "requires_review": true
  },
  "remediation": {
    "status": "needs_manual_review",
    "reject_reason": "risk_requires_manual_review",
    "patch": null,
    "analysis": null
  }
}
```

What this proves: the agent ran Terraform itself, parsed only addresses, actions
and attribute **names**, applied the deterministic policy, and refused to patch a
destructive change. Foundry is not configured, so the analysis is `null` — and
the drift is still recorded, which is the intended degradation.

Drift is a finding, not a failure. To get a failing exit code for CI:

```bash
docker compose run --rm agent scan --repo /sandbox --dry-run --fail-on-drift
echo $?    # 2
```

### 6.4 Rotation

```bash
docker compose run --rm agent scan-all --limit 1
docker compose run --rm agent scan-all --limit 1
docker compose run --rm agent scan-all --limit 1
```

Expected: the first run scans one repository and reports `fleet.batch: 1`,
`fleet.due: 2`, `fleet.deferred: 1`; the second scans the other; the third skips
everything still inside the 20-hour cooldown:

```json
{"status":"skipped","reason":"no_repository_due","repositories":2,"cooldown_hours":20.0,"scans":[],"counts":{"clean":0,"drift":0,"error":0}}
```

The ledger is written after **each** repository, so stopping a run halfway still
advances the rotation. `--force` ignores the cooldown.

### 6.5 Rollup and audit

```bash
docker compose run --rm agent fleet-report --format markdown
ls -R local/reports
cat local/reports/fleet/state.json
```

`local/reports/audit/YYYY/MM/DD/<scan-id>-<repository>.json` is the permanent
record of each scan; the ledger is derived bookkeeping and can be deleted to
reset the rotation.

---

## 7. Scan a real repository

Clone one repository (shallow is enough) into the mounted checkout directory.
The agent copies it to a temporary directory, so the mount is read-only:

```bash
git clone --depth 1 <repo-url> local/checkout/platform
```

Add it to `local/repos.yaml` under the container path:

```yaml
  - id: platform-prod
    path: /workspace/platform
    terraform_root: environments/prod
    environment: prod
    mode: observe
    # Only when the root needs them; paths are relative to terraform_root.
    var_files: [prod.tfvars]
    backend_configs: [backend.hcl]
    # Only for Activity Log correlation; not used by Terraform itself.
    azure_subscription_id: "<subscription-id>"
```

Then:

```bash
docker compose run --rm agent validate-config
docker compose run --rm agent scan --repo platform-prod --dry-run
```

`terraform_root` must contain `.tf` files; `var_files` and `backend_configs`
must exist relative to it. Registry `path` values are **container** paths —
using a host path gives `checkout_not_found`.

### Authentication for real repositories

The agent does not authenticate Terraform for you; it runs the repository's own
configuration. That means:

- `terraform init` downloads providers, and a remote backend (for example
  `azurerm`) needs credentials. The container has **no `az` CLI**, so `use_cli`
  cannot work, and `use_oidc` cannot work without a GitHub OIDC token.
- Supply a read-only service principal through `local/.env`:

  ```bash
  ARM_TENANT_ID=<tenant-id>
  ARM_CLIENT_ID=<client-id>
  ARM_CLIENT_SECRET=<client-secret>
  ARM_SUBSCRIPTION_ID=<subscription-id>
  ```

  The runner strips only `TF_CLI_ARGS*` and `TF_LOG*`; `ARM_*` passes through to
  Terraform. For an AAD-backed state account the principal also needs
  `Storage Blob Data Reader` on the state container.
- If the repository's provider block hard-codes `use_oidc = true`, a local run
  cannot authenticate. Use a **scratch copy** with service-principal or local
  backend configuration; never edit the real landing-zone repository for a
  local test.
- Required variables are not supplied automatically. List them in `var_files`
  (relative to the Terraform root) or pass `TF_VAR_*` in `local/.env`; a missing
  variable fails at `plan`.

To debug an `init` or `plan` failure, reproduce it with stderr visible. The agent
deliberately discards Terraform stderr, so the record only carries
`command_failed`:

```bash
docker compose run --rm --entrypoint sh agent -c '
  cp -r /workspace/platform /tmp/repo &&
  cd /tmp/repo/environments/prod &&
  terraform init -input=false &&
  terraform plan -refresh-only -input=false'
```

---

## 8. Add Foundry explanations

Docker has no managed identity, so local runs use the API key. Append to
`local/.env` (the key is a secret; the file stays untracked):

```bash
FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com
FOUNDRY_DEPLOYMENT=<existing-deployment-name>
FOUNDRY_API_VERSION=2024-10-21
FOUNDRY_API_KEY=<key>
```

Use the existing Foundry deployment — the agent never provisions one. Confirm
with `docker compose run --rm agent validate-config` (`foundry: true`, one note
fewer), then rescan the sandbox:

```bash
docker compose run --rm agent scan --repo /sandbox --dry-run
```

Expected: `remediation.analysis` now contains a summary, cause and confidence;
the aggregated risk stays **blocked** because the model may only escalate. On a
clean scan there is still no model call.

If the call fails, the scan degrades: `model_explanation_skipped` appears on
stderr with a reason, `analysis` stays `null`, and the drift finding is intact.

---

## 9. Add Azure Activity correlation

This is the agent's own credential (the Azure SDK), separate from the
`ARM_*` variables Terraform uses. A read-only service principal with `Reader` at
the subscription scope is enough:

```bash
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<client-id>
AZURE_CLIENT_SECRET=<client-secret>
AZURE_SUBSCRIPTION_ID=<subscription-id>
```

`DefaultAzureCredential` picks these up as `EnvironmentCredential`; no `az login`
is involved. Verify with `validate-config` (`azure_activity: true`), then scan a
repository whose plan contains real `azurerm` resources. Records are attributed
only when the Activity Log returns the same resource id; `resource_match` is
`false` otherwise. The `local_file` sandbox has no resource ids, so it produces
no Activity lookups by design.

Use a short-lived secret for a sandbox principal, never a production principal
with write access — the agent only needs reads.

---

## 10. Open a draft PR

The PR path needs all of the following; miss one and the record stops with a
`reject_reason` instead:

1. A **GitHub App** installed on the target repository (create it once via
   [azure-setup-guide.md §1](azure-setup-guide.md#1-create-the-github-app-once));
   Metadata read, Contents read/write, Pull requests read/write. No workflow,
   secrets or administration permissions.
2. A **remediable finding** — low or medium risk, not blocked, in `pr` mode.
   A tag-only change on a sandbox resource is the usual choice. Blocked
   destructive drift never receives a patch.
3. **Foundry configured**, because the patch comes from the model's proposed
   diff and is then validated by `fmt`, `validate` and a fresh plan.
4. A registry entry (not a path), because publishing needs `url`.

Put the key outside the repository or in `local/` (ignored by git):

```bash
cp /secure/path/drift-agent.private.pem local/github-app.pem
chmod 644 local/github-app.pem     # UID 10001 must read it; it is local-only
```

Append to `local/.env`:

```bash
GITHUB_APP_ID=<app-id>
GITHUB_APP_INSTALLATION_ID=<installation-id>
GITHUB_APP_PRIVATE_KEY_PATH=/var/run/secrets/github/app-private-key.pem
```

Add a scratch repository to `local/repos.yaml`:

```yaml
  - id: scratch-prod
    url: https://github.com/<org>/<scratch-repo>.git
    path: /workspace/scratch
    default_branch: main
    terraform_root: .
    environment: sandbox
    mode: pr
    protected_paths:
      - ".github/workflows/**"
      - backend.tf
      - providers.tf
```

Mount the key only for this run, and turn the dry run off explicitly:

```bash
docker compose run --rm \
  -v "$PWD/local/github-app.pem:/var/run/secrets/github/app-private-key.pem:ro" \
  -e DRIFT_DRY_RUN=false \
  agent scan --repo scratch-prod --mode pr
```

Expected: `remediation.status: "pr_created"` and a `pr_url`; a branch
`drift-fix/<scan-id>`, exactly one commit, and a **draft** pull request whose
body lists the risk decision and validation steps. The agent never merges, never
pushes to the default branch and never edits the workflow. Close the PR to roll
back.

The most common non-obvious failures:

| Record | Meaning |
| --- | --- |
| `github_url_not_configured` | You scanned a path; use the registry id |
| `publish_requires_pr_or_auto_mode` | Effective mode fell back to observe |
| `model_unavailable` | Foundry is not configured or unreachable |
| `risk_requires_manual_review` | Blocked/high risk — working as intended |
| `github_publish_failed: ...` | App not installed on the repo, or permissions |

---

## 11. Plain Docker (without Compose)

Compose is convenience; every command works with `docker run` if you prefer:

```bash
docker build -t terraform-drift-agent:0.2.0 .

docker run --rm \
  --env-file local/.env \
  -v "$PWD/local/repos.yaml:/app/config/repos.yaml:ro" \
  -v "$PWD/local/checkout:/workspace:ro" \
  -v "$PWD/local/sandbox:/sandbox:ro" \
  -v "$PWD/tests/fixtures:/fixtures:ro" \
  -v "$PWD/local/reports:/app/reports" \
  terraform-drift-agent:0.2.0 scan --repo /sandbox --dry-run
```

For the sandbox Terraform commands, override the entrypoint:

```bash
docker run --rm --entrypoint terraform \
  -v "$PWD/local/sandbox:/sandbox" -w /sandbox \
  terraform-drift-agent:0.2.0 init
```

Remember that `local/reports` and `local/sandbox` must be writable by UID 10001,
as in §3.

---

## 12. Exit codes and artefacts

| Result | Report `status` | Exit |
| --- | --- | --- |
| Plan exit 0 | `clean` | 0 |
| Plan exit 2 | `drift` | 0, or 2 with `--fail-on-drift` |
| `init` / `plan` / `show` / parse failure | `error` | 1 |

| Artefact | Location | Notes |
| --- | --- | --- |
| Scan record | `local/reports/audit/YYYY/MM/DD/` | reduced JSON, one per scan |
| Rotation ledger | `local/reports/fleet/state.json` | delete to reset rotation |
| Report | stdout | single JSON object |
| Logs | stderr | structured JSON, `scan_id` on every line |

The report never contains attribute values, tfvars, state or secrets.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `permission denied` writing `reports/` | Bind mount owned by your user, container is UID 10001 | `chmod -R a+rwX local/reports` or `sudo chown -R 10001 local/reports` |
| `bind source path does not exist` | `local/repos.yaml`, `local/checkout` or `local/sandbox` missing | Create them as in §3 |
| `pull access denied for terraform-drift-agent` | The sandbox service has no `build:` and the image is absent | `docker compose build` first |
| `env_file` / `required` rejected | Compose older than 2.24 | Upgrade Compose, or remove `env_file` and use `docker run --env-file` |
| `checkout_not_found` | Registry uses a host path, or clone missing | Use the container path (`/workspace/<name>`) and check the mount |
| `no_terraform_configuration_found` | Wrong `terraform_root`, or no `.tf` at that level | Fix the relative root |
| `command_failed` at `init` | Provider download blocked, or backend authentication | Network/proxy; provide `ARM_*`; check backend config |
| `command_failed` at `plan` | Missing required variable or `var_files` entry | Add the variable file or `TF_VAR_*`; reproduce with stderr (§7) |
| `input_file_missing` | `var_files` / `backend_configs` path wrong | Paths are relative to `terraform_root` |
| `unknown_repository` | `--repo` is neither an id nor an existing directory | Fix the id/path |
| `model_explanation_skipped: foundry_not_configured` | `local/.env` not loaded | `docker compose run --rm agent validate-config`; check `foundry` |
| Foundry `model_explanation_failed` | Wrong endpoint, deployment, API version or key | Verify the three values against the Foundry deployment |
| Activity results always empty | No resource ids, or principal lacks Reader/Activity read | Use a real `azurerm` resource; grant Reader |
| PR not created, `github_url_not_configured` | Scanned a path instead of a registry id | Add a `url` entry and scan by id |
| PR `403` / `github_publish_failed` | App not installed on that repository, or permissions missing | Install it on selected repos; check Contents/PR scopes |
| Draft PR body empty / 422 | Patch outside the diff hunks or protected path | Read `remediation.reject_reason` |
| Terraform provider downloads repeat every scan | Each run uses a fresh temporary checkout | Add `-v tf-plugin-cache:/plugin-cache -e TF_PLUGIN_CACHE_DIR=/plugin-cache` to `docker compose run` |
| Very slow on Windows | Bind mount over `/mnt/c` | Move the repository into the WSL filesystem |

---

## 14. Clean up

One-off containers already use `--rm`. To remove the image and all local state:

```bash
docker compose down --rmi local
rm -rf local
```

If `rm` is refused because the sandbox wrote as UID 10001, use
`sudo rm -rf local`. Nothing in this guide created a cloud resource; the only
state was `local/sandbox/terraform.tfstate`.

---

## 15. What still needs Azure

Local runs cannot prove managed identity, Key Vault secret mounting, the
Container Apps Job, private egress or the fleet-wide rotation in production.
Once the checks above pass, continue with
[azure-setup-guide.md](azure-setup-guide.md) step 3 (landing-zone
prerequisites) and deploy the same image there.
