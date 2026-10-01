# CloudPulse Runbook

Day-two operations for the CloudPulse platform.

---

## 1. Health checks

### Liveness vs readiness

| Probe | Path | Meaning | Restart on failure |
| --- | --- | --- | --- |
| Liveness | `/health/live` | The process is running | Yes |
| Readiness | `/health/ready` | The process can serve traffic | No — remove from the load balancer |
| Docker | `/health` | Alias of liveness | Yes |

```bash
curl -fsS https://cloudpulse.example.com/health/ready | jq
```

`503` from `/health/ready` means the database is unreachable. Do not restart
the pod — the database is the problem.

### CloudPulse reports an application as DOWN

1. Open the **Applications** view and filter by `DOWN`.
2. Use **Check** on the row to probe it on demand. The toast shows the HTTP
   status, latency and the exact failure message.
3. Open the **Health history** modal to see whether it is a blip or a
   sustained outage.

Interpretation guide:

| Message | Cause | Action |
| --- | --- | --- |
| `Application is unreachable: ...` | DNS, TCP or TLS failure | Check the target from inside the cluster |
| `Application returned HTTP 5xx` | The service is erroring | The application is up, its code path is not |
| `Unexpected HTTP status 404` | Wrong path | Fix `expected_status_codes` or the URL |
| `Response body did not contain '...'` | Content assertion failed | Update the keyword or the health endpoint |
| `TLS certificate verification failed` | A TLS-intercepting proxy is in the path | Trust its root CA, or set `HEALTH_CHECK_VERIFY_TLS=false` |
| `DNS lookup failed` | The hostname does not resolve | Check the URL and the container DNS settings |
| `Connection refused` | Nothing is listening on that port | The target is down or on the wrong port |
| `Invalid application URL` | Bad scheme or missing host | Fix the URL in the registry |

### Opening incidents without an outage

Set `MONITOR_DEGRADED_AFTER_CHECKS=1` in the ConfigMap and restart, or raise
`HEALTH_CHECK_RETENTION_DAYS` if history is also filling too fast.

### Incident flood from a flapping service

`create_incident_if_needed` de-duplicates against any open incident for that
application, so a flapping service produces one ticket, not hundreds. If you
still see many, they are distinct applications — check
`GET /api/incidents?source=monitoring`.

### Incidents never auto-resolve

Auto-resolution only touches incidents with `source = "monitoring"` and
requires `AUTO_RESOLVE_INCIDENTS=true`. Incidents raised by a person stay open
by design. Use **Resolve** on the incident, or flip the setting:

```bash
kubectl -n cloudpulse set env deployment/cloudpulse AUTO_RESOLVE_INCIDENTS=true
```

---

## 2. Database

### Backups

Terraform configures RDS with 7-day retention, a 03:00–04:00 backup window,
encryption at rest and Performance Insights. Verify:

```bash
aws rds describe-db-instances --db-instance-identifier cloudpulse-postgres \
  --query 'DBInstances[0].EarliestRestorableTime'
```

### Schema migrations

Local development creates the schema automatically
(`AUTO_CREATE_SCHEMA=true`). For PostgreSQL, own the schema with migrations:

```bash
cd app
flask --app app db init      # first time only
flask --app app db migrate -m "describe the change"
flask --app app db upgrade
```

Set `AUTO_CREATE_SCHEMA=false` in production so tables are never created
implicitly.

### The database is growing fast

Every table that grows without bound is pruned by the scheduled retention job,
which runs every `RETENTION_JOB_HOURS` (default 6). Check what is stored and
which windows are in force:

```bash
curl "https://cloudpulse.example.com/api/monitor/retention" \
    -H "X-Requested-With: XMLHttpRequest" -b cookies.txt
```

To prune now, either use the configured policies or force a window:

```bash
# Configured policies
curl -X POST "https://cloudpulse.example.com/api/monitor/retention" \
    -H "X-Requested-With: XMLHttpRequest" -b cookies.txt

# Everything older than 14 days
curl -X POST "https://cloudpulse.example.com/api/monitor/retention?days=14" \
    -H "X-Requested-With: XMLHttpRequest" -b cookies.txt
```

Then lower the relevant window — `HEALTH_CHECK_RETENTION_DAYS`,
`AUDIT_LOG_RETENTION_DAYS` or `NOTIFICATION_RETENTION_DAYS` — in the ConfigMap
and restart. A window of `0` keeps that table forever.

`/health/ready` reports the scheduler state; `running: false` with
`ENABLE_SCHEDULER=true` means the background job is not executing and nothing
will be pruned on a schedule.

### Long queries after an incident

```sql
SELECT application_id, status, count(*), min(checked_at), max(checked_at)
FROM health_checks
GROUP BY application_id, status
ORDER BY count(*) DESC
LIMIT 20;

SELECT count(*) FROM audit_logs WHERE created_at < now() - interval '90 days';
```

The composite index on `(application_id, checked_at)` serves the history query.
If the dashboard feels slow, check `EXPLAIN ANALYZE` on the incident list
before adding indexes.

---

## 3. Deployments

### Rolling out a new version

```bash
cd k8s/overlays/production
kustomize edit set image cloudpulse=ghcr.io/<owner>/cloudpulse:<sha>
kubectl apply -k .
kubectl -n cloudpulse rollout status deployment/cloudpulse --timeout=300s
kubectl -n cloudpulse rollout undo deployment/cloudpulse     # if it misbehaves
```

### Rollback without redeploying the code

Open **Deployments**, find the release and choose **Rollback**. CloudPulse
records `rolled_back_at`, writes an audit entry and keeps the incident timeline
correlated, but it does **not** redeploy the workload — use `rollout undo` for
that.

### The image will not pull

Verify the image is public in GHCR, or attach a pull secret to the
`cloudpulse` ServiceAccount:

```yaml
imagePullSecrets:
  - name: ghcr-pull
```

---

## 4. Alerts

### Webhook

```bash
NOTIFY_WEBHOOK_URL=<your-slack-incoming-webhook-url>
```

CloudPulse posts:

```json
{
  "event": "incident.opened",
  "source": "CloudPulse",
  "timestamp": "2026-09-30T09:20:12.004+00:00",
  "incident": {
    "id": 12,
    "title": "payments-api is Down",
    "severity": "High",
    "status": "Open",
    "description": "...",
    "detected_at": "2026-09-30T09:20:11.482+00:00",
    "resolved_at": null
  },
  "application": {
    "id": 1,
    "name": "payments-api",
    "url": "https://payments.example.com/health",
    "environment": "Production"
  }
}
```

Set `NOTIFY_WEBHOOK_SECRET` to send an `X-CloudPulse-Signature` header.

### Email

```bash
NOTIFY_SMTP_HOST=smtp.example.com
NOTIFY_SMTP_PORT=587
NOTIFY_SMTP_USER=cloudpulse@example.com
NOTIFY_SMTP_PASSWORD=...
NOTIFY_SMTP_TO=oncall@example.com,sre@example.com
```

### Checking delivery

Every attempt writes a `notifications` row with `channel`, `event`, `status`
(`sent` / `failed` / `skipped`), `attempts` and `last_error`.

```sql
SELECT created_at, channel, event, status, last_error
FROM notifications
ORDER BY created_at DESC
LIMIT 20;
```

Delivery failures never block the request path: a broken webhook is logged and
recorded, and monitoring continues.

---

## 5. Prometheus and Grafana

### CloudPulse is not scrapeable

```bash
curl -i http://cloudpulse:5000/metrics | head
```

Check `cloudpulse_build_info` is present. In Kubernetes, confirm the pod
annotations:

```bash
kubectl -n cloudpulse get pod -l app=cloudpulse \
  -o jsonpath='{.items[0].metadata.annotations}'
```

### The monitoring loop stalled

```bash
curl -s https://cloudpulse.example.com/api/monitor/scheduler | jq
```

`running: false` means `ENABLE_SCHEDULER` is off or APScheduler failed to
start. The alert `CloudPulseMonitoringStalled` fires when
`cloudpulse_monitoring_last_run_timestamp_seconds` is more than 300 seconds
old.

The scheduler also falls back to a standard library thread when APScheduler is
not installed, so a missing dependency degrades rather than breaks.

### Multiple schedulers

CloudPulse runs one Gunicorn worker with threads. If you scale by increasing
`--workers`, every worker starts a scheduler. Scale with the HPA (more pods)
instead, never with more workers per pod.

### Reload alert rules

```bash
curl -X POST http://localhost:9090/-/reload
```

Validate before reloading:

```bash
promtool check config monitoring/prometheus.yml
promtool check rules monitoring/alert-rules.yml
```

---

## 6. Security

### The app will not start: `SECRET_KEY must be set when APP_ENV=production`

This is intentional. The previous fallback was a literal committed to the
repository, so every deployment that forgot to set the variable shared the same
signing key. Generate one and inject it:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set `ALLOW_INSECURE_SECRET_KEY=true` only for a throwaway local run. It has no
effect in production, which still refuses to boot.

### Rotate `SECRET_KEY`

```bash
kubectl -n cloudpulse create secret generic cloudpulse-secrets \
    --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
    --from-literal=DATABASE_URL="$DATABASE_URL" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl -n cloudpulse rollout restart deployment/cloudpulse
```

Rotating invalidates every active session, so all users must sign in again.

### Users are locked out after too many failed attempts

`429 Too many failed attempts` means `LOGIN_MAX_ATTEMPTS` failures were recorded
for that `(email, client IP)` pair inside `LOGIN_ATTEMPT_WINDOW_SECONDS`. The
lock lasts `LOGIN_LOCKOUT_SECONDS`.

Counters are held in process memory and clear on restart, so rolling the pods
releases every lock. To recover one user immediately, restart the deployment:

```bash
kubectl -n cloudpulse rollout restart deployment/cloudpulse
```

If you are running several gunicorn workers, note that the limit applies per
worker. Set `LOGIN_MAX_ATTEMPTS=0` to disable throttling, which is only
appropriate for a development instance.

### A user cannot remember their password

`/login` links to `/forgot-password`. With `NOTIFY_SMTP_HOST` configured the
link is emailed; without it the link is written to the application log:

```bash
kubectl -n cloudpulse logs deployment/cloudpulse | grep "password reset link"
```

The response is identical for a known and an unknown address, so you cannot tell
from the HTTP response whether an account exists.

If emails contain the internal host name, set `PUBLIC_BASE_URL` to the address
users actually reach.

### A user is asked to verify their email

`REQUIRE_EMAIL_VERIFICATION=true` blocks sign-in until the address is confirmed.
Resend by issuing a fresh token, or mark the account directly:

```sql
UPDATE users SET is_verified = 1 WHERE email = 'user@example.com';
```

### Role changes

```sql
UPDATE users SET role = 'Editor' WHERE email = 'engineer@example.com';
```

The session stores the role, so the user must sign out and back in for the
change to take effect.

### Cross-origin requests are blocked

`403 Cross-origin request blocked` on a write means the request carried neither
`X-Requested-With: XMLHttpRequest` nor a same-origin `Origin`. For a machine
client either set the header or run with `CSRF_PROTECTION_ENABLED=false`.

The HTML form routes additionally require a `csrf_token` field. A script posting
to `/login` must scrape the token from the rendered page.

### Verifying the alerting outbox

`GET /api/activity/notifications` lists every delivery attempt, including
failures and their error text. `GET /api/activity/notifications/summary` reports
the success rate and whether a webhook or SMTP host is configured at all. If the
summary shows `webhook_configured: false`, alerts were never attempted rather
than silently dropped.

### Every HTTPS target reports DOWN on this machine

A TLS-intercepting proxy (typical of corporate and campus networks) is
presenting a self-signed certificate. CloudPulse reports this explicitly:

```
TLS certificate verification failed. Install the issuing CA in the system
trust store, or set HEALTH_CHECK_VERIFY_TLS=false ...
```

The correct fix is to install the proxy's root CA into the system trust store.
`HEALTH_CHECK_VERIFY_TLS=false` disables certificate verification entirely, so
use it only for a temporary local demo and never in production.

### The dashboard will not load, or loads unstyled

The browser is showing a cached page from an older process. Stop the running
server, start the new one, then hard refresh the browser with `Ctrl+Shift+R`.
Jinja templates are cached per process, so a server started before a template
change keeps serving the old markup until it is restarted.

### 401 on everything after a deploy

The session cookie is `Secure` when `SESSION_COOKIE_SECURE=true`. If the
cluster is served over plain HTTP, sign-ins appear to succeed but the session
is discarded. Either terminate TLS or set the flag to `false` for local work.

---

## 7. Disaster recovery

### Restore PostgreSQL

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier cloudpulse-postgres-restored \
  --db-snapshot-identifier cloudpulse-postgres:2026-09-30T03-15-00 \
  --db-instance-class db.t4g.micro

aws rds modify-db-instance \
  --db-instance-identifier cloudpulse-postgres \
  --apply-immediately \
  --no-multi-az
```

Then update `DATABASE_URL` in the Secret and restart the deployment.

### Rebuilding from the image

CloudPulse holds no state outside the database except the scheduler, which
rebuilds itself on start. Restoring the database and pointing the deployment at
it is a complete recovery.

### What is safe to delete

| Artefact | Safe to delete | Effect |
| --- | --- | --- |
| `health_checks` older than the retention window | yes | Only historical reporting is lost |
| `audit_logs` | yes | Timeline history is lost |
| `notifications` | yes | Delivery history is lost |
| `incidents` | carefully | Open incidents stop being tracked |
| `applications` | no | Everything cascades from it |

---

## 8. Routine tasks

| Cadence | Task | Command |
| --- | --- | --- |
| Daily | Review open incidents and SLA breaches | `GET /api/incidents/stats` |
| Weekly | Export the incident report | `GET /api/export/incidents?days=7` |
| Weekly | Check the delivery timeline for failed releases | `GET /api/export/deployments` |
| Monthly | Review `CLOUDPULSE` image tags and prune | `aws ecr describe-images` |
| Monthly | Rotate database credentials | `ALTER ROLE cloudpulse PASSWORD ...` |
| Quarterly | Rotate `SECRET_KEY` and webhook secrets | Section 6 |
| Quarterly | `terraform plan` and review drift | `cd infra/terraform && terraform plan` |
