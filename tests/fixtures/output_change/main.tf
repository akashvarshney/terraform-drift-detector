terraform {
  required_version = ">= 1.5.0"
}

output "sample" {
  value     = "fixture-only-sensitive-value"
  sensitive = true
}
