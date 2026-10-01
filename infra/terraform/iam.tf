# ------------------------------------------------------------------
# IAM and logging
#
# Two roles are used, mirroring the split AWS documents for Fargate:
#
#   cloudpulse_app   - the *execution* role. ECS uses it to pull the image
#                      from ECR, resolve the secrets, and ship logs. It never
#                      grants the application itself any access to data.
#   cloudpulse_task  - the *task* role. It becomes the container's own AWS
#                      identity and is what the inline policy below scopes.
#
# Secrets are injected through the ECS `secrets` block, which means the
# plaintext values never appear in the task definition, the service, or this
# state file. See var.secrets_manager_secret_arn in variables.tf for the
# ARN contract that must be honoured.
# ------------------------------------------------------------------

data "aws_caller_identity" "current" {}

# The execution role assumes the trust policy documented for Fargate tasks.
data "aws_iam_policy_document" "cloudpulse_ecs_assume_role" {
  statement {
    sid     = "AllowECSAssumeRole"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cloudpulse_app" {
  name               = "${var.project_name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.cloudpulse_ecs_assume_role.json

  tags = { Name = "${var.project_name}-ecs-execution" }
}

# Minimal and deliberate: an image can only be pulled from the CloudPulse ECR
# repository created in main.tf, and secrets/logs can only be read from the
# log group and secret named in the variables. AmazonSSMParameterReadOnly is
# deliberately not attached - CloudPulse pulls configuration from a ConfigMap
# equivalent (the ECS task definition) rather than from Parameter Store, so
# granting it would be unused privilege.
resource "aws_iam_role_policy_attachment" "cloudpulse_app_ecs" {
  role       = aws_iam_role.cloudpulse_app.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "cloudpulse_task" {
  name               = "${var.project_name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.cloudpulse_ecs_assume_role.json

  tags = { Name = "${var.project_name}-ecs-task" }
}

data "aws_iam_policy_document" "cloudpulse_task" {
  statement {
    sid    = "ReadApplicationSecrets"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]

    resources = [var.secrets_manager_secret_arn]
  }

  statement {
    sid    = "WriteApplicationLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.cloudpulse.arn}:*"]
  }
}

resource "aws_iam_role_policy" "cloudpulse_task" {
  name   = "${var.project_name}-ecs-task"
  role   = aws_iam_role.cloudpulse_task.id
  policy = data.aws_iam_policy_document.cloudpulse_task.json
}

resource "aws_cloudwatch_log_group" "cloudpulse" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.project_name}-ecs" }
}
