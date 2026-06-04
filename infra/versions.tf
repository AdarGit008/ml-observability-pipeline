# Provider + backend pins for the AWS hot path (IaC session #1, 2026-06-04).
#
# Backend: local. Single-PC project (hard constraint #2) — an S3 state
# bucket would add a standing cost surface and a chicken-and-egg
# bootstrap for a stack whose entire lifetime is apply → 30-min demo →
# destroy. State lives at infra/terraform.tfstate (gitignored).

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Floor 5.30 (python3.12 runtime support); ceiling open through
      # 6.x — no 6.x-only syntax is used below.
      version = ">= 5.30, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  backend "local" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_tag
      ManagedBy = "terraform"
    }
  }
}
