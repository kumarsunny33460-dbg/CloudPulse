# ------------------------------------------------------------------
# ECS / Fargate path
#
# IMPORTANT: kubernetes.tf and this file are ALTERNATIVES, not layers.
# kubernetes.tf provisions a Deployment, a ClusterIP Service and an HPA.
# This file provisions an ECS cluster, a task definition, a service behind the
# ALB in alb.tf, and a target-tracking autoscaling policy. They solve the same
# problem, so running both would put two independent schedulers on the same
# database and produce duplicate incidents.
#
# Enable exactly one by setting var.deployment_target:
#
#   -Ddeployment_target="kubernetes"   (default) -> only kubernetes.tf runs
#   -Ddeployment_target="ecs"                   -> only the resources in this
#                                                 file are created
#
# Every resource below is guarded with `count`, so switching the variable
# destroys or creates the whole path in a single plan. The network, database,
# ECR repository and both IAM roles in iam.tf are shared by either path and are
# intentionally left unguarded.
#
# Deliberately not created here:
#   - An ECS task definition container health check. Fargate does not support
#     the Docker HEALTHCHECK through the ECS API; readiness is covered by the
#     ALB target group health check in alb.tf instead.
#   - Service Connect, App Mesh or a service discovery registry. CloudPulse
#     reaches Postgres directly and publishes to Prometheus, so a service mesh
#     would add cost and failure modes without changing behaviour.
# ------------------------------------------------------------------

resource "aws_ecs_cluster" "cloudpulse" {
  count = var.deployment_target == "ecs" ? 1 : 0

  name = "${var.project_name}-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${var.project_name}-ecs-cluster" }
}

resource "aws_ecs_task_definition" "cloudpulse" {
  count = var.deployment_target == "ecs" ? 1 : 0

  family                   = var.ecs_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ecs_task_cpu_units
  memory                   = var.ecs_task_memory_mib
  execution_role_arn       = aws_iam_role.cloudpulse_app.arn
  task_role_arn            = aws_iam_role.cloudpulse_task.arn

  container_definitions = jsonencode([
    {
      name      = "cloudpulse"
      image     = "${aws_ecr_repository.cloudpulse.repository_url}:latest"
      essential = true

      portMappings = [
        {
          name          = "http"
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      # Secrets are injected by the agent, never as plain `environment`
      # entries, so DATABASE_URL and SECRET_KEY stay out of the task
      # definition and out of the state file. Both keys live in the single
      # JSON secret named by var.secrets_manager_secret_arn.
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${var.secrets_manager_secret_arn}:DATABASE_URL::"
        },
        {
          name      = "SECRET_KEY"
          valueFrom = "${var.secrets_manager_secret_arn}:SECRET_KEY::"
        },
      ]

      # Non-sensitive configuration is still passed as plain environment
      # variables; only credential material is listed above.
      environment = [
        {
          name  = "PORT"
          value = tostring(var.container_port)
        },
        {
          name  = "APP_ENV"
          value = var.environment
        },
        {
          name  = "LOG_LEVEL"
          value = "INFO"
        },
        {
          name  = "ENABLE_SCHEDULER"
          value = "true"
        },
        {
          name  = "MONITOR_INTERVAL_SECONDS"
          value = "60"
        },
        {
          name  = "HEALTH_CHECK_RETENTION_DAYS"
          value = "30"
        },
        {
          name  = "AUTO_CREATE_INCIDENTS"
          value = "true"
        },
        {
          name  = "AUTO_RESOLVE_INCIDENTS"
          value = "true"
        },
        {
          name  = "SESSION_COOKIE_SECURE"
          value = "true"
        },
        {
          name  = "CSRF_PROTECTION_ENABLED"
          value = "true"
        },
        {
          name  = "STRICT_ORIGIN_CHECK"
          value = "true"
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.cloudpulse.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
        }
      ]

      # Not an ECS health check: Fargate ignores the Docker HEALTHCHECK
      # through the ECS API. It is declared so the same definition stays
      # portable to an EC2 launch type. On Fargate, readiness is enforced by
      # the target group health check in alb.tf.
      healthCheck = {
        command     = ["CMD-SHELL", "exit 0"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
    }
  ])

  # No `volume` block is declared. Fargate gives every task its own scratch
  # volume and /tmp is already writable on it, and the `ephemeral_volume`
  # block used to mirror that is not accepted by the aws provider on Fargate.
  # On the EC2 launch type a host volume would be required here instead.

  tags = { Name = "${var.project_name}-task" }
}

resource "aws_ecs_service" "cloudpulse" {
  count = var.deployment_target == "ecs" ? 1 : 0

  name            = "${var.project_name}-${var.environment}"
  cluster         = aws_ecs_cluster.cloudpulse[0].id
  task_definition = aws_ecs_task_definition.cloudpulse[0].arn
  desired_count   = var.ecs_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = var.ecs_deployment_min_healthy_percent
  deployment_maximum_percent         = var.ecs_deployment_max_healthy_percent

  # Tasks land across the private subnets through the target group. The
  # classic "distinctInstance" placement constraint is deliberately not used:
  # AWS does not support it for the FARGATE launch type, and tasks run in the
  # awsvpc network mode where each one already gets its own ENI.
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.cloudpulse.arn
    container_name   = "cloudpulse"
    container_port   = var.container_port
  }

  # CloudPulse is a web application with sessions; a rolling update would drop
  # the in-flight login state of anyone mid-request.
  health_check_grace_period_seconds = 60

  depends_on = [aws_iam_role_policy.cloudpulse_task]

  tags = { Name = "${var.project_name}-ecs-service" }
}

resource "aws_appautoscaling_target" "cloudpulse" {
  count = var.deployment_target == "ecs" ? 1 : 0

  max_capacity       = var.ecs_max_capacity
  min_capacity       = var.ecs_min_capacity
  resource_id        = "service/${aws_ecs_cluster.cloudpulse[0].name}/${aws_ecs_service.cloudpulse[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cloudpulse_cpu" {
  count = var.deployment_target == "ecs" ? 1 : 0

  name               = "${var.project_name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.cloudpulse[0].service_namespace
  resource_id        = aws_appautoscaling_target.cloudpulse[0].resource_id
  scalable_dimension = aws_appautoscaling_target.cloudpulse[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    target_value       = var.ecs_target_cpu_utilization
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}
