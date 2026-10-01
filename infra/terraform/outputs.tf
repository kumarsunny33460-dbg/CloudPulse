output "vpc_id" {
  description = "Identifier of the CloudPulse VPC."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnets suitable for a load balancer."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnets used by RDS and the node groups."
  value       = aws_subnet.private[*].id
}

output "ecr_repository_url" {
  description = "Push the CloudPulse image here."
  value       = aws_ecr_repository.cloudpulse.repository_url
}

output "db_endpoint" {
  description = "RDS connection endpoint."
  value       = aws_db_instance.cloudpulse.address
}

output "db_name" {
  description = "Name of the CloudPulse database."
  # aws_db_instance exports db_name. The previous value read
  # .database_name, which is not an attribute of the resource and made
  # `terraform validate` fail for the whole module.
  value = aws_db_instance.cloudpulse.db_name
}

output "db_connection_string" {
  description = "Full DATABASE_URL for the application."
  sensitive   = true
  value = format(
    "postgresql://%s:%s@%s:5432/%s",
    var.db_username,
    var.db_password != "" ? var.db_password : random_password.db.result,
    aws_db_instance.cloudpulse.address,
    var.db_name
  )
}

output "generated_db_password" {
  description = "Password Terraform generated when db_password was not supplied."
  sensitive   = true
  value       = random_password.db.result
}

output "kubernetes_namespace" {
  description = "Namespace the CloudPulse workloads run in."
  value       = kubernetes_namespace.cloudpulse.metadata[0].name
}

output "cloudpulse_service_cluster_ip" {
  description = "In-cluster address of the CloudPulse service."
  value       = kubernetes_service.cloudpulse.spec[0].cluster_ip
}

# ------------------------------------------------------------------
# ECS / load balancer
# ------------------------------------------------------------------

output "alb_dns_name" {
  description = "Public DNS name of the CloudPulse application load balancer."
  value       = aws_lb.cloudpulse.dns_name
}

output "alb_url" {
  description = "URL to reach CloudPulse through the load balancer."
  value       = var.alb_certificate_arn == "" ? "http://${aws_lb.cloudpulse.dns_name}" : "https://${var.domain_name}"
}

output "alb_target_group_arn" {
  description = "Target group the ECS service registers into."
  value       = aws_lb_target_group.cloudpulse.arn
}

output "alb_log_bucket" {
  description = "S3 bucket receiving application load balancer access logs."
  value       = aws_s3_bucket.alb_logs.bucket
}

# ------------------------------------------------------------------
# Container runtime
# ------------------------------------------------------------------

output "cloudwatch_log_group" {
  description = "CloudWatch log group the ECS tasks stream to."
  value       = aws_cloudwatch_log_group.cloudpulse.name
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster, or null when deployment_target is not ecs."
  value       = var.deployment_target == "ecs" ? aws_ecs_cluster.cloudpulse[0].name : null
}

output "ecs_service_name" {
  description = "Name of the ECS service, or null when deployment_target is not ecs."
  value       = var.deployment_target == "ecs" ? aws_ecs_service.cloudpulse[0].name : null
}

output "ecs_task_definition_arn" {
  description = "ARN of the current ECS task definition revision."
  value       = var.deployment_target == "ecs" ? aws_ecs_task_definition.cloudpulse[0].arn : null
}

# ------------------------------------------------------------------
# Object storage and identity
# ------------------------------------------------------------------

output "artifact_bucket" {
  description = "S3 bucket holding incident attachments, reports and deployment artefacts."
  value       = aws_s3_bucket.this.bucket
}

output "artifact_bucket_arn" {
  description = "ARN of the CloudPulse artifact bucket."
  value       = aws_s3_bucket.this.arn
}

output "ecs_execution_role_arn" {
  description = "Execution role ECS uses to pull the image, read secrets and write logs."
  value       = aws_iam_role.cloudpulse_app.arn
}

output "ecs_task_role_arn" {
  description = "AWS identity the running container assumes."
  value       = aws_iam_role.cloudpulse_task.arn
}

output "aws_account_id" {
  description = "Account the CloudPulse stack was applied to."
  value       = data.aws_caller_identity.current.account_id
}
