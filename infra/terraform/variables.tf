variable "project_name" {
  description = "Name prefix applied to every resource."
  type        = string
  default     = "cloudpulse"
}

variable "environment" {
  description = "Deployment environment (development, staging, production)."
  type        = string
  default     = "production"
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-south-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the CloudPulse VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones used for subnets."
  type        = list(string)
  default     = ["ap-south-1a", "ap-south-1b"]
}

variable "db_instance_class" {
  description = "RDS instance size."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS storage in GiB."
  type        = number
  default     = 20
}

variable "db_username" {
  description = "Master username for the CloudPulse database."
  type        = string
  default     = "cloudpulse"
}

variable "db_name" {
  description = "Name of the CloudPulse database."
  type        = string
  default     = "cloudpulse"
}

variable "kubernetes_namespace" {
  description = "Namespace the CloudPulse workloads run in."
  type        = string
  default     = "cloudpulse"
}

variable "image" {
  description = "Container image deployed to Kubernetes."
  type        = string
  default     = "ghcr.io/your-github-username/cloudpulse:latest"
}

# ------------------------------------------------------------------
# Kubernetes provider
# ------------------------------------------------------------------

variable "k8s_endpoint" {
  description = "Kubernetes API server endpoint."
  type        = string
  default     = ""
}

variable "k8s_ca_certificate" {
  description = "Base64 encoded cluster CA certificate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "k8s_token" {
  description = "Bearer token used by the Terraform Kubernetes provider."
  type        = string
  default     = ""
  sensitive   = true
}

# ------------------------------------------------------------------
# Secret material
# ------------------------------------------------------------------

variable "db_password" {
  description = "Password for the CloudPulse database user. Prefer TF_VAR_db_password or a secrets manager."
  type        = string
  default     = ""
  sensitive   = true
}

variable "secret_key" {
  description = "Flask session signing key."
  type        = string
  default     = ""
  sensitive   = true
}

# ------------------------------------------------------------------
# Deployment target
# ------------------------------------------------------------------

variable "deployment_target" {
  description = <<-EOT
    Which runtime path to build. "kubernetes" provisions the namespace, config
    map, secret, deployment, service and HPA in kubernetes.tf. "ecs" provisions
    the ALB, ECS cluster, task definition, service and autoscaling resources in
    ecs.tf. The two paths are alternatives: only one should ever be enabled for
    a given state.
  EOT
  type        = string
  default     = "kubernetes"

  validation {
    condition     = contains(["kubernetes", "ecs"], var.deployment_target)
    error_message = "deployment_target must be either \"kubernetes\" or \"ecs\"."
  }
}

# ------------------------------------------------------------------
# Secrets Manager
# ------------------------------------------------------------------

# SECURITY: this must be the ARN of a Secrets Manager secret, for example
# "arn:aws:secretsmanager:ap-south-1:123456789012:secret:cloudpulse/prod-abc123".
# Never point it at a plaintext value, a Terraform variable holding one, or a
# secret that lives in Parameter Store. The task role is granted
# secretsmanager:GetSecretValue and secretsmanager:DescribeSecret on exactly
# this ARN and nothing else, so the value never lands in the task definition,
# in the ECS service, or in this state file. Keep it out of any tfvars that get
# committed; pass it through TF_VAR_secrets_manager_secret_arn or a
# *.auto.tfvars that is git-ignored.
variable "secrets_manager_secret_arn" {
  description = "ARN of the Secrets Manager secret holding DATABASE_URL and SECRET_KEY. Must be a Secrets Manager secret ARN, never a plaintext value."
  type        = string
  default     = ""
  sensitive   = true
}

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the CloudPulse log group."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention periods CloudWatch Logs accepts."
  }
}

# ------------------------------------------------------------------
# Load balancer
# ------------------------------------------------------------------

variable "container_port" {
  description = "TCP port the CloudPulse container listens on."
  type        = number
  default     = 5000
}

variable "alb_certificate_arn" {
  description = "ARN of an ACM certificate for the HTTPS listener. Leave empty to skip the HTTPS listener and keep the HTTP redirect only."
  type        = string
  default     = ""
}

variable "alb_ssl_policy" {
  description = "TLS policy applied to the HTTPS listener."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

variable "alb_health_check_path" {
  description = "Path the ALB target group uses for health checks."
  type        = string
  default     = "/health/ready"
}

variable "domain_name" {
  description = "Fully qualified domain name routed to the ALB, used for tags and outputs."
  type        = string
  default     = "cloudpulse.example.com"
}

# ------------------------------------------------------------------
# ECS
# ------------------------------------------------------------------

variable "ecs_task_family" {
  description = "Family name of the CloudPulse ECS task definition."
  type        = string
  default     = "cloudpulse"
}

variable "ecs_desired_count" {
  description = "Number of CloudPulse tasks the ECS service keeps running."
  type        = number
  default     = 2
}

variable "ecs_task_cpu_units" {
  description = "CPU units reserved for the task (1024 = one vCPU)."
  type        = number
  default     = 1024
}

variable "ecs_task_memory_mib" {
  description = "Memory reserved for the task in MiB."
  type        = number
  default     = 2048
}

variable "ecs_min_capacity" {
  description = "Minimum task count for CPU target tracking autoscaling."
  type        = number
  default     = 2
}

variable "ecs_max_capacity" {
  description = "Maximum task count for CPU target tracking autoscaling."
  type        = number
  default     = 8
}

variable "ecs_target_cpu_utilization" {
  description = "Average CPU utilisation percentage that target tracking aims for."
  type        = number
  default     = 70
}

variable "ecs_deployment_min_healthy_percent" {
  description = "Minimum healthy task percentage during a deployment."
  type        = number
  default     = 100
}

variable "ecs_deployment_max_healthy_percent" {
  description = "Maximum healthy task percentage during a deployment."
  type        = number
  default     = 200
}

# ------------------------------------------------------------------
# Object storage
# ------------------------------------------------------------------

variable "s3_ia_transition_days" {
  description = "Days before noncurrent versions and objects transition to S3 Standard-IA."
  type        = number
  default     = 30
}

variable "s3_glacier_transition_days" {
  description = "Days before objects transition to S3 Glacier Deep Archive."
  type        = number
  default     = 90
}

variable "s3_force_destroy" {
  description = "Allow Terraform to delete a non-empty bucket. Keep false outside throwaway environments."
  type        = bool
  default     = false
}
