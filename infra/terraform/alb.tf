# ------------------------------------------------------------------
# Application load balancer
#
# Public subnets come from aws_subnet.public in main.tf, which is the only
# subnet list in the state. No new subnet variables are introduced.
# ------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb"
  description = "Public entry point for CloudPulse"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from the internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Deliberately not scoped to aws_security_group.app: that group already
  # allows ingress from this one, and a mutual security_group reference
  # between the two resources is a dependency cycle that Terraform rejects.
  egress {
    description = "Forward to the application tasks"
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-alb" }
}

# Ingress is expressed as a security group reference rather than a CIDR so
# that the rule follows the load balancer even when the VPC layout changes.
resource "aws_security_group" "app" {
  name        = "${var.project_name}-app"
  description = "CloudPulse application tasks, reachable only from the ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Application traffic from the load balancer only"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Database and outbound HTTPS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-app" }
}

resource "aws_lb" "cloudpulse" {
  name               = substr("${var.project_name}-${var.environment}", 0, 32)
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  enable_deletion_protection = var.environment == "production"
  drop_invalid_header_fields = false

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.bucket
    prefix  = "alb/${var.project_name}"
    enabled = true
  }

  tags = { Name = "${var.project_name}-alb" }
}

resource "aws_lb_target_group" "cloudpulse" {
  name        = substr("${var.project_name}-tg", 0, 32)
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = var.alb_health_check_path
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${var.project_name}-tg" }
}

# Plain HTTP exists only to redirect. Nothing is served over it.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.cloudpulse.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# The HTTPS listener is skipped when no certificate ARN is supplied so that a
# first `terraform plan` does not fail before ACM has been requested.
resource "aws_lb_listener" "https" {
  count = var.alb_certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.cloudpulse.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.alb_ssl_policy
  certificate_arn   = var.alb_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.cloudpulse.arn
  }
}

# ------------------------------------------------------------------
# ALB access logging
#
# The log-delivery bucket is a separate, dedicated bucket so that the
# CloudPulse artifact bucket below (storage.tf) keeps a simple, auditable
# lifecycle policy. The bucket policy is the modern replacement for the old
# "enable ACLs and grant log-delivery" dance.
# ------------------------------------------------------------------

resource "aws_s3_bucket" "alb_logs" {
  bucket = "${var.project_name}-${var.environment}-alb-logs-${data.aws_caller_identity.current.account_id}"

  tags = { Name = "${var.project_name}-alb-logs" }
}

# Log delivery writes with the service principal's ACL, so the bucket keeps
# BucketOwnerPreferred rather than forcing the account to own every object.
resource "aws_s3_bucket_ownership_controls" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "alb_logs" {
  statement {
    sid     = "AllowALBLogDelivery"
    actions = ["s3:PutObject"]

    resources = ["${aws_s3_bucket.alb_logs.arn}/alb/${var.project_name}/*"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
  }

  statement {
    sid     = "DenyUnencryptedTransport"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.alb_logs.arn,
      "${aws_s3_bucket.alb_logs.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = data.aws_iam_policy_document.alb_logs.json
}
