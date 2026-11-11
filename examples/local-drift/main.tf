# Throwaway sandbox for the local end-to-end test (docs/local-setup-guide.md).
#
# It uses only the `local` provider, so it never talks to Azure. Apply it once
# yourself inside the container, then change marker.txt out of band: the next
# refresh-only plan reports drift and the agent classifies it. The agent itself
# only ever runs init, plan, show, fmt and validate — never apply.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

resource "local_file" "marker" {
  filename = "${path.module}/marker.txt"
  content  = "expected content\n"
}
