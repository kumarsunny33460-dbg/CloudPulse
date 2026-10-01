resource "kubernetes_namespace" "cloudpulse" {
  metadata {
    name = var.kubernetes_namespace
    labels = {
      "app.kubernetes.io/part-of"          = var.project_name
      "pod-security.kubernetes.io/enforce" = "restricted"
    }
  }
}

resource "kubernetes_config_map" "cloudpulse" {
  metadata {
    name      = "cloudpulse-config"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
  }

  data = {
    APP_ENV                     = var.environment
    ENABLE_SCHEDULER            = "true"
    MONITOR_INTERVAL_SECONDS    = "60"
    HEALTH_CHECK_RETENTION_DAYS = "30"
    HEALTH_CHECK_TIMEOUT        = "5"
    AUTO_CREATE_INCIDENTS       = "true"
    AUTO_RESOLVE_INCIDENTS      = "true"
    CSRF_PROTECTION_ENABLED     = "true"
    SESSION_COOKIE_SECURE       = "true"
    STRICT_ORIGIN_CHECK         = "true"
    DATABASE_URL = format(
      "postgresql://%s:%s@%s:5432/%s",
      var.db_username,
      var.db_password != "" ? var.db_password : random_password.db.result,
      aws_db_instance.cloudpulse.address,
      var.db_name
    )
  }
}

resource "kubernetes_secret" "cloudpulse" {
  metadata {
    name      = "cloudpulse-secrets"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
  }

  type = "Opaque"

  data = {
    SECRET_KEY  = var.secret_key != "" ? var.secret_key : random_password.db.result
    DB_PASSWORD = var.db_password != "" ? var.db_password : random_password.db.result
  }
}

resource "kubernetes_service_account" "cloudpulse" {
  metadata {
    name      = "cloudpulse"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
  }
}

resource "kubernetes_deployment" "cloudpulse" {
  metadata {
    name      = "cloudpulse"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
    labels = {
      app     = "cloudpulse"
      version = "latest"
    }
  }

  spec {
    replicas = 2

    strategy {
      type = "RollingUpdate"
      rolling_update {
        max_surge       = 1
        max_unavailable = 0
      }
    }

    selector {
      match_labels = { app = "cloudpulse" }
    }

    template {
      metadata {
        labels = { app = "cloudpulse" }
        annotations = {
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "5000"
          "prometheus.io/path"   = "/metrics"
        }
      }

      spec {
        service_account_name = kubernetes_service_account.cloudpulse.metadata[0].name

        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          fs_group        = 10001
        }

        container {
          name  = "cloudpulse"
          image = var.image

          port {
            container_port = 5000
            name           = "http"
          }

          env_from {
            config_map_ref {
              name = kubernetes_config_map.cloudpulse.metadata[0].name
            }
          }

          env {
            name  = "GIT_COMMIT_SHA"
            value = "terraform"
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "192Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
          }

          # read_only_root_filesystem is set above, so the writable scratch
          # paths the interpreter needs are backed by an in-memory volume.
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }

          # read_only_root_filesystem is set above, so the writable scratch
          # paths the interpreter and any temp file handling need are mounted
          # from an in-memory emptyDir.
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }

          readiness_probe {
            http_get {
              path = "/health/ready"
              port = 5000
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 5
            failure_threshold     = 3
          }

          liveness_probe {
            http_get {
              path = "/health/live"
              port = 5000
            }
            initial_delay_seconds = 20
            period_seconds        = 20
            timeout_seconds       = 5
            failure_threshold     = 3
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true

            capabilities {
              drop = ["ALL"]
            }
          }
        }

        # A pod volume, so it belongs inside the pod spec (spec.template.spec),
        # not on the deployment's own spec. The container above is read-only at
        # the root, and this is the writable scratch path it needs.
        volume {
          name = "tmp"

          empty_dir {
            medium = "Memory"
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "cloudpulse" {
  metadata {
    name      = "cloudpulse"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
  }

  spec {
    selector = { app = "cloudpulse" }

    port {
      port        = 80
      target_port = 5000
      name        = "http"
    }

    type = "ClusterIP"
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "cloudpulse" {
  metadata {
    name      = "cloudpulse"
    namespace = kubernetes_namespace.cloudpulse.metadata[0].name
  }

  spec {
    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = kubernetes_deployment.cloudpulse.metadata[0].name
    }

    min_replicas = 2
    max_replicas = 8

    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = 70
        }
      }
    }
  }
}
