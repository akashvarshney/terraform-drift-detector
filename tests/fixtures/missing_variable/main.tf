terraform {
  required_version = ">= 1.5.0"
}

variable "required_value" {
  type      = string
  sensitive = true
}

output "sample" {
  value     = var.required_value
  sensitive = true
}
