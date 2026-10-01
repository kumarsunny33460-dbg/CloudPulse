# CloudPulse Helm chart

Deploys CloudPulse, a self-hosted uptime monitoring, health-check and
incident-management platform that exposes Prometheus metrics on `/metrics`.

The chart is functionally equivalent to `k8s/base/cloudpulse.yaml`, plus the
production resources that base file leaves out: a PodDisruptionBudget, a
headless Service, NetworkPolicies, a ServiceMonitor and a PrometheusRule.

- Chart version: `1.0.0`
- App version: `1.0.0`
- Requires Kubernetes 1.23 or newer (`autoscaling/v2`, `policy/v1`,
  `networking.k8s.io/v1`).
- The ServiceMonitor and PrometheusRule templates require the Prometheus
  Operator CRDs. Set `monitoring.serviceMonitor.enabled=false` and
  `monitoring.prometheusRule.enabled=false` on a cluster without them.

## Install

The chart does not create the Secret by default. Create it first, so the
plaintext credentials never pass through a values file:

```bash
kubectl create namespace cloudpulse

kubectl -n cloudpulse create secret generic cloudpulse-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql://cloudpulse:<password>@<db-host>:5432/cloudpulse"

helm upgrade --install cloudpulse ./chart/cloudpulse \
  --namespace cloudpulse \
  --create-namespace \
  --set config.existingSecret=cloudpulse-secrets \
  --set ingress.hosts[0].host=cloudpulse.example.com \
  --set ingress.tls[0].secretName=cloudpulse-tls \
  --set ingress.tls[0].hosts[0]=cloudpulse.example.com \
  --wait --timeout 5m
```

For a local or demo cluster only, let the chart build the Secret:

```bash
helm upgrade --install cloudpulse ./chart/cloudpulse \
  --namespace cloudpulse --create-namespace \
  --set config.createSecret=true \
  --set config.SECRET_KEY="$(openssl rand -hex 32)" \
  --set config.DATABASE_URL="postgresql://cloudpulse:cloudpulse@postgres:5432/cloudpulse" \
  --set ingress.enabled=false
```

## Upgrade

```bash
# See what would change.
helm upgrade cloudpulse ./chart/cloudpulse --namespace cloudpulse --dry-run

# Apply.
helm upgrade cloudpulse ./chart/cloudpulse \
  --namespace cloudpulse \
  --reuse-values \
  --set image.tag=1.1.0 \
  --wait --timeout 5m

# Roll back if the new revision misbehaves.
helm history cloudpulse --namespace cloudpulse
helm rollback cloudpulse <REVISION> --namespace cloudpulse --wait
```

`replicaCount` is ignored while `autoscaling.enabled=true`; the HPA owns the
replica count in that case.

## Uninstall

```bash
helm uninstall cloudpulse --namespace cloudpulse
```

The chart-created Secret is annotated `helm.sh/resource-policy: keep`, so it
survives uninstall. Delete it explicitly when you are finished with it:

```bash
kubectl -n cloudpulse delete secret cloudpulse-secrets
```

## Values

| Key | Default | Description |
| --- | --- | --- |
| `replicaCount` | `2` | Pods to run. Ignored when `autoscaling.enabled=true`. |
| `revisionHistoryLimit` | `3` | ReplicaSets kept for rollback. |
| `image.repository` | `ghcr.io/your-github-username/cloudpulse` | Container image repository. |
| `image.tag` | `""` | Image tag. Empty falls back to the chart `appVersion`. |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `imagePullSecrets` | `[]` | Secrets for a private registry. |
| `nameOverride` / `fullnameOverride` | `""` | Override the generated resource name. |
| `commonLabels` / `commonAnnotations` | `{}` | Added to every object. |
| `serviceAccount.create` | `true` | Create a dedicated ServiceAccount. |
| `serviceAccount.name` | `""` | ServiceAccount name. Empty uses the release fullname. |
| `serviceAccount.automount` | `false` | Mount the API token. Off: CloudPulse never calls the API server. |
| `serviceAccount.annotations` | `{}` | ServiceAccount annotations. |
| `service.type` | `ClusterIP` | Service type. |
| `service.port` | `80` | Service port. |
| `service.targetPort` | `5000` | Container port. |
| `service.appProtocol` | `http` | Port name and `appProtocol`. |
| `service.annotations` / `service.labels` | `{}` | Extra Service metadata. |
| `headlessService.enabled` | `true` | Headless Service for stable per-pod DNS. |
| `headlessService.annotations` | `{}` | Headless Service annotations. |
| `ingress.enabled` | `true` | Render the Ingress. |
| `ingress.className` | `nginx` | `ingressClassName`. |
| `ingress.annotations` | `nginx.ingress.kubernetes.io/proxy-body-size: 2m`, `nginx.ingress.kubernetes.io/ssl-redirect: "true"` | Ingress annotations. |
| `ingress.hosts` | one host `cloudpulse.example.com`, path `/` | Host rules. |
| `ingress.tls` | secret `cloudpulse-tls` for that host | TLS blocks. |
| `networkPolicy.enabled` | `true` | Render default-deny plus allow policies. |
| `networkPolicy.ingressNamespaceSelector` | `{}` | Namespace label selector for the ingress controller. **Set this to your ingress namespace.** |
| `networkPolicy.additionalIngressFrom` | `[]` | Extra pod selectors allowed to reach port 5000. |
| `networkPolicy.databasePort` | `5432` | PostgreSQL port allowed on egress. |
| `networkPolicy.egressToSelectors` | `[]` | Pod selectors for the database. |
| `networkPolicy.egressCIDRs` | `[]` | Extra egress CIDRs, for example an SMTP relay. |
| `resources.requests` | `100m` CPU, `192Mi` memory | Scheduling requests. |
| `resources.limits` | `500m` CPU, `512Mi` memory | Container limits. |
| `tmpVolume.sizeLimit` | `256Mi` | `emptyDir` size at `/tmp`. |
| `tmpVolume.medium` / `tmpVolume.storageClass` | `""` | `emptyDir` medium and storage class. |
| `startupProbe.*` | `/health/live`, 30 x 2s | Protects against restart loops on a slow first connection. |
| `livenessProbe.*` | `/health/live`, 20s period | Restart probe. |
| `readinessProbe.*` | `/health/ready`, 10s period | Removes the pod from the Service when the database is unreachable. |
| `terminationGracePeriodSeconds` | `45` | Must exceed the `preStop` sleep plus request duration. |
| `lifecycle` | `preStop: sleep 10` | Drains the endpoint before SIGTERM. |
| `config.createSecret` | `false` | Create the Secret from values. Keep false outside demo clusters. |
| `config.existingSecret` | `cloudpulse-secrets` | Pre-created Secret name. Required when `createSecret=false`. |
| `config.APP_ENV` … `config.MAX_PAGE_SIZE` | see `values.yaml` | Non-sensitive settings rendered into the ConfigMap. |
| `config.PUBLIC_BASE_URL` | `""` | Public address used in reset and verification links. **Set this behind an Ingress**, or emails will contain the in-cluster host name. |
| `config.LOGIN_MAX_ATTEMPTS` | `5` | Failed logins allowed per window. `0` disables. |
| `config.LOGIN_ATTEMPT_WINDOW_SECONDS` | `300` | Sliding window for failed attempts. |
| `config.LOGIN_LOCKOUT_SECONDS` | `900` | Lockout applied once the limit is reached. |
| `config.REGISTRATION_MAX_ATTEMPTS` | `5` | Failed registration attempts allowed. |
| `config.PASSWORD_RESET_MAX_ATTEMPTS` | `5` | Reset requests allowed per address. |
| `config.PASSWORD_RESET_TOKEN_TTL_SECONDS` | `3600` | Lifetime of a reset link. |
| `config.EMAIL_VERIFICATION_TOKEN_TTL_SECONDS` | `86400` | Lifetime of a verification link. |
| `config.REQUIRE_EMAIL_VERIFICATION` | `false` | Block sign-in until the address is confirmed. |
| `config.MIN_PASSWORD_LENGTH` | `8` | Minimum password length. |
| `config.CSP_ENABLED` | `false` | Also send a Content-Security-Policy header. |
| `config.HEALTH_CHECK_RETENTION_DAYS` | `30` | Health check history window. |
| `config.AUDIT_LOG_RETENTION_DAYS` | `180` | Audit log window. |
| `config.NOTIFICATION_RETENTION_DAYS` | `30` | Outbox window. |
| `config.RETENTION_JOB_HOURS` | `6` | How often the retention job runs. |
| `config.LOG_FORMAT` | `json` | `json` emits one structured object per line for Loki or Elasticsearch. |
| `config.NOTIFY_ON_*` | `true` | Which events raise a notification. ConfigMap, not Secret. |
| `config.NOTIFY_SMTP_HOST` / `_PORT` / `_FROM` / `_TO` / `_USE_TLS` | see `values.yaml` | SMTP connection details. ConfigMap, not Secret. |
| `config.SECRET_KEY` / `config.DATABASE_URL` | `""` | Required when `createSecret=true`; the render fails if empty. |
| `config.NOTIFY_WEBHOOK_URL` / `_SECRET` / `config.NOTIFY_SMTP_USER` / `_PASSWORD` | `""` | The only other keys routed to the Secret. |
| `config.extra` | `{}` | Extra ConfigMap keys. |
| `config.extraConfigMap` / `config.extraSecret` | `{}` | Raw overrides; merged last. |
| `podSecurityContext` | non-root 10001, `RuntimeDefault` seccomp | Pod security context. |
| `securityContext` | no privilege escalation, read-only root, drop `ALL` | Container security context. |
| `podAnnotations` / `podLabels` | `{}` | Added to the pod template. |
| `nodeSelector` / `tolerations` / `affinity` | see `values.yaml` | Scheduling constraints. |
| `topologySpreadConstraints` | two constraints, zone and hostname | Spread pods across zones and hosts. Set to `[]` to disable. |
| `extraVolumes` / `extraVolumeMounts` | `[]` | Additional volumes. |
| `extraEnv` / `extraEnvFrom` | `[]` | Additional environment variables. |
| `command` / `args` / `extraArgs` | `[]` | Entrypoint overrides. |
| `autoscaling.enabled` | `true` | Render the HPA. |
| `autoscaling.minReplicas` / `maxReplicas` | `2` / `8` | HPA bounds. |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | HPA CPU target. |
| `autoscaling.extraMetrics` / `behavior` | `[]` / `{}` | Extra HPA metrics and scaling behaviour. |
| `podDisruptionBudget.enabled` | `true` | Render the PDB. |
| `podDisruptionBudget.minAvailable` | `1` | PDB minimum. Ignored when `maxUnavailable` is set. |
| `podDisruptionBudget.maxUnavailable` | `""` | PDB maximum. Mutually exclusive with `minAvailable`. |
| `monitoring.serviceMonitor.enabled` | `true` | Render the ServiceMonitor. |
| `monitoring.serviceMonitor.interval` | `15s` | Scrape interval. |
| `monitoring.serviceMonitor.scrapeTimeout` | `10s` | Scrape timeout. |
| `monitoring.serviceMonitor.additionalLabels` | `{}` | Must match your Prometheus `serviceMonitorSelector`. |
| `monitoring.serviceMonitor.relabelings` | stamps `pod`, `namespace`, `service` | Relabelings applied before the scrape. |
| `monitoring.serviceMonitor.metricRelabelings` | `[]` | Relabelings applied to samples. |
| `monitoring.serviceMonitor.path` / `scheme` | `/metrics` / `http` | Endpoint path and scheme. |
| `monitoring.prometheusRule.enabled` | `true` | Render the PrometheusRule. |
| `monitoring.prometheusRule.additionalLabels` | `{}` | Must match your Prometheus `ruleSelector`. |
| `monitoring.prometheusRule.namespace` | `""` | Namespace for the rules. Empty means the release namespace. |

## Alert rules

`templates/prometheusrule.yaml` reproduces `monitoring/alert-rules.yml` in the
PrometheusRule CRD shape: the same three groups (`cloudpulse-availability`,
`cloudpulse-incidents`, `cloudpulse-platform`), the same seven alerts, the
same expressions, `for` durations, severities and annotations. The two were
compared programmatically and the group lists are identical. Change one file
and change the other.
