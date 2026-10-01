terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "s3" {
    # Configure with: terraform init -backend-config=backend.hcl
    key = "cloudpulse/terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# An empty CA certificate is valid for an in-cluster or already-trusted
# endpoint, and `base64decode("")` would otherwise abort every plan with an
# opaque base64 error before the user can read the missing-variable hint.
locals {
  k8s_ca_decoded = var.k8s_ca_certificate == "" ? null : base64decode(var.k8s_ca_certificate)
}

provider "kubernetes" {
  host                   = var.k8s_endpoint
  cluster_ca_certificate = local.k8s_ca_decoded
  token                  = var.k8s_token
}
