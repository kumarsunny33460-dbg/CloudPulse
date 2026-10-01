# CloudPulse API Reference

Base URL: `/`. All requests are JSON. Authentication uses the session cookie
issued by `POST /login`.

## Conventions

### Authentication

| Guard | Applies to | Failure |
| --- | --- | --- |
| none | `/health*`, `/metrics`, `/api/cicd` | — |
| `login_required` | every other read endpoint | `401` JSON, or a redirect to `/login` for pages |
| `write_required` | every mutating endpoint | `401` unauthenticated, `403` for `Viewer` |
| `admin_required` | reserved for admin-only operations | `403` for non-admins |

Mutating `/api/` requests must send `X-Requested-With: XMLHttpRequest` or a
same-origin `Origin` / `Referer` header. Non-browser clients should set
`X-Requested-With: XMLHttpRequest`; set `CSRF_PROTECTION_ENABLED=false` to
disable the check entirely.

The browser-facing form routes (`/login`, `/register`, `/forgot-password`,
`/reset-password/<token>`) additionally carry a Flask-WTF CSRF token in a
`csrf_token` hidden field. The JSON API is exempt from that check because it
already requires a header a cross-origin page cannot set without a pre-flight
that this application never approves.

### Rate limiting

`POST /login` and `POST /register` return **`429`** with an HTML page once the
attempt limit is reached. The response body states how many seconds remain.

Counters are tracked per `(email, client IP)` pair over a sliding window
(`LOGIN_ATTEMPT_WINDOW_SECONDS`, default 300s). Reaching
`LOGIN_MAX_ATTEMPTS` (default 5) locks the pair out for
`LOGIN_LOCKOUT_SECONDS` (default 900s). A successful login clears the counter.
Setting a limit to `0` disables throttling for that scope.

### Request correlation

Every response carries an `X-Request-ID` header. An inbound `X-Request-ID` is
passed through unchanged so a gateway-provided id reaches the application logs;
otherwise a new id is generated per request. It is also emitted as a field on
every JSON log record.

### Account recovery

These render HTML rather than JSON and are protected by a CSRF token.

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` / `POST` | `/forgot-password` | Always returns the same message whether or not the address is registered, so the form cannot enumerate accounts. Throttled per address by `PASSWORD_RESET_MAX_ATTEMPTS`. |
| `GET` / `POST` | `/reset-password/<token>` | Single use. Returns `400` for an invalid, expired or already-spent token, or for a password that fails the policy. |
| `GET` | `/verify-email/<token>` | Single use. Returns `400` for an invalid or spent token. |

Tokens are stored only as a SHA-256 digest, expire after
`PASSWORD_RESET_TOKEN_TTL_SECONDS` / `EMAIL_VERIFICATION_TOKEN_TTL_SECONDS`, and
issuing a new token for the same purpose invalidates the previous one. Links
use `PUBLIC_BASE_URL` when set, otherwise the inbound request host.

With `REQUIRE_EMAIL_VERIFICATION=true`, `POST /login` returns `403` for an
unverified account.

### Pagination

Collections return a bare JSON array by default. Add `?page=`, `?per_page=`
(or `?limit=`) or `?paginated=true` to receive the envelope:

```json
{
  "data": [],
  "pagination": {
    "page": 1,
    "per_page": 25,
    "total": 0,
    "total_pages": 1,
    "has_next": false,
    "has_previous": false
  }
}
```

`per_page` is clamped to `MAX_PAGE_SIZE` (default 200).

### Errors

```json
{ "error": "Validation failed", "fields": { "url": "URL scheme 'ftp' is not allowed" } }
```

Validation failures use `422` and enumerate every offending field. Malformed
bodies use `400`. State conflicts use `409`. Throttled authentication attempts
use `429`; an account gated by `REQUIRE_EMAIL_VERIFICATION` uses `403`.

---

## System

### `GET /health`
```json
{ "status": "healthy" }
```

### `GET /health/ready`
Runs `SELECT 1` against the configured database. Returns `503` when the
database is unreachable.

```json
{
  "status": "ready",
  "database": { "status": "connected", "detail": "ok" },
  "scheduler": { "enabled": true, "running": true, "interval_seconds": 60 },
  "uptime_seconds": 128.4,
  "checked_at": "2026-09-30T09:12:44.120+00:00"
}
```

### `GET /health/live`
```json
{ "status": "alive" }
```

### `GET /metrics`
Prometheus text exposition format. See the metric table in the
[README](../README.md#observability).

### `GET /api/cicd`
```json
{
  "status": "success",
  "message": "CloudPulse CI/CD pipeline is working",
  "version": "1.0.0",
  "commit": "9f2c1ab",
  "environment": "production"
}
```

### `GET /api/me`
```json
{
  "user": {
    "id": 1,
    "username": "admin",
    "email": "admin@example.com",
    "role": "Admin",
    "created_at": "2026-09-30T09:00:00+00:00"
  }
}
```

---

## Applications

### `GET /api/applications`

Query parameters: `search`, `environment`, `status` (lifecycle state),
`health` (`UP` / `DEGRADED` / `DOWN` / `UNKNOWN`), `is_paused`, `sort`
(`id`, `name`, `environment`, `status`, `created_at`, `last_checked_at`,
`last_health_status`, `uptime_percentage`), `order` (`asc` / `desc`).

```json
[
  {
    "id": 1,
    "name": "payments-api",
    "url": "https://payments.example.com/health",
    "environment": "Production",
    "status": "Operational",
    "description": "Card payment service",
    "created_at": "2026-09-30T09:00:00+00:00",
    "last_checked_at": "2026-09-30T09:20:11.482+00:00",
    "last_health_status": "UP",
    "last_response_time": 42.31,
    "last_http_status": 200,
    "uptime_percentage": 99.98,
    "consecutive_failures": 0,
    "is_paused": false,
    "open_incident_count": 0,
    "open_incident_ids": [],
    "last_deployment": {
      "id": 7,
      "version": "v2.4.0",
      "status": "Successful",
      "deployed_at": "2026-09-29T18:02:00+00:00"
    },
    "monitoring": {
      "http_method": "GET",
      "timeout_seconds": 5,
      "check_interval_seconds": 60,
      "expected_status_codes": "200-399",
      "expected_body_keyword": null,
      "is_paused": false
    }
  }
]
```

### `POST /api/applications` — write

| Field | Required | Rules |
| --- | --- | --- |
| `name` | yes | 1–100 characters, unique |
| `url` | yes | `http` or `https`, must include a host |
| `environment` | yes | `Development`, `Staging`, `Production` |
| `status` | no | `Operational`, `Maintenance`, `Decommissioned` (default `Operational`) |
| `description` | no | free text |
| `http_method` | no | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` |
| `timeout_seconds` | no | 1–60, default 5 |
| `check_interval_seconds` | no | 15–86400, default 60 |
| `expected_status_codes` | no | `200-399`, `404`, `200-399,404`; default `200-399` |
| `expected_body_keyword` | no | check fails when absent from the response body |
| `request_headers` | no | JSON object of extra request headers |
| `is_paused` | no | boolean |

```bash
curl -X POST http://localhost:5000/api/applications \
  -H 'Content-Type: application/json' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -b cookies.txt \
  -d '{"name":"payments-api","url":"https://payments.example.com/health","environment":"Production"}'
```

### `PUT` / `PATCH /api/applications/{id}` — write
Partial updates. Responds `200` with `{"message": "No changes were supplied"}`
when the payload matches the current state.

### `DELETE /api/applications/{id}` — write
Removes the application and cascades to incidents, deployments and health
checks.

### `POST /api/applications/{id}/check` — write
```json
{
  "application_id": 1,
  "name": "payments-api",
  "url": "https://payments.example.com/health",
  "environment": "Production",
  "health": "UP",
  "response_time_ms": 42.31,
  "http_status": 200,
  "message": "Application is reachable",
  "checked_at": "2026-09-30T09:20:11.482+00:00"
}
```

### `GET /api/applications/{id}/health` — session
Legacy compatibility endpoint. Same probe as `/check`, plus the original
`incident_created` and `incident_id` fields.

### `GET /api/applications/{id}/health-history` — session
Query parameters: `limit` (default 50, max 1000), `hours`, `page`, `per_page`.

```json
{
  "application_id": 1,
  "application_name": "payments-api",
  "summary": {
    "total_checks": 42,
    "up_checks": 41,
    "degraded_checks": 0,
    "down_checks": 1,
    "uptime_percentage": 97.62,
    "avg_response_time": 44.8,
    "min_response_time": 31.2,
    "max_response_time": 402.6
  },
  "latest_status": "UP",
  "has_more": false,
  "history": [
    {
      "id": 918,
      "status": "UP",
      "http_status": 200,
      "response_time": 42.31,
      "checked_at": "2026-09-30T09:20:11.482+00:00",
      "error_message": null,
      "triggered_incident_id": null
    }
  ]
}
```

### `GET /api/applications/{id}/uptime?days=7` — session
```json
{
  "application_id": 1,
  "application_name": "payments-api",
  "days": 7,
  "series": [
    {
      "date": "2026-09-24",
      "total_checks": 1440,
      "uptime_percentage": 100.0,
      "avg_response_time": 43.1
    }
  ]
}
```

### `POST /api/applications/{id}/pause` and `/resume` — write

### `GET /api/applications/metadata` — session
```json
{
  "environments": ["Development", "Staging", "Production"],
  "statuses": ["Operational", "Maintenance", "Decommissioned"],
  "http_methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  "sort_columns": ["created_at", "environment", "id", "name", "status", "uptime_percentage"]
}
```

---

## Incidents

### `GET /api/incidents`
Query parameters: `search`, `status`, `severity`, `environment`, `application_id`,
`source` (`manual` / `monitoring`), `open=true`, `breached=true`, `sort`
(`id`, `title`, `severity`, `status`, `detected_at`, `created_at`, `resolved_at`),
`order`.

```json
[
  {
    "id": 12,
    "application_id": 1,
    "application_name": "payments-api",
    "application_url": "https://payments.example.com/health",
    "title": "payments-api is Down",
    "description": "CloudPulse detected that payments-api is currently unreachable.",
    "root_cause": "Automatically resolved: payments-api responded with 200 after a failed check.",
    "assignee": "on-call engineer",
    "severity": "High",
    "severity_rank": 3,
    "status": "Open",
    "source": "monitoring",
    "created_at": "2026-09-30T09:20:12.004+00:00",
    "detected_at": "2026-09-30T09:20:11.482+00:00",
    "acknowledged_at": null,
    "resolved_at": null,
    "duration_minutes": null,
    "sla_minutes": 120,
    "sla_elapsed_minutes": 4.2,
    "sla_remaining_minutes": 115.8,
    "sla_breached": false
  }
]
```

### `POST /api/incidents` — write
Requires `application_id` and `title`. Optional: `description`, `severity`,
`status`, `assignee`, `root_cause`, `source`. Creating with
`status = "Resolved"` stamps `resolved_at`.

### `PUT /api/incidents/{id}/resolve` — write
Body may contain `root_cause` or `note`, which is stored on the incident.

### `POST /api/incidents/{id}/acknowledge` — write
Moves `Open` to `Investigating` and stamps `acknowledged_at`.

### `POST /api/incidents/{id}/escalate` — write
Raises severity one level. Returns `409` when already `Critical`.

### `POST /api/incidents/bulk` — write
```json
{ "action": "resolve", "ids": [12, 13, 14] }
```
`action` is `resolve` or `delete`. Unknown ids are reported in `not_found`.

### `GET /api/incidents/stats?days=30` — session
```json
{
  "reliability": {
    "window_days": 30,
    "total_incidents": 48,
    "open_incidents": 3,
    "resolved_incidents": 45,
    "mttr_minutes": 96.4,
    "p50_resolution_minutes": 61.0,
    "p90_resolution_minutes": 240.5,
    "fastest_resolution_minutes": 4.2,
    "sla_breaches": 5,
    "sla_compliance_percentage": 88.89
  },
  "severity_breakdown": [
    { "severity": "Critical", "count": 4, "open": 1, "rank": 4 }
  ],
  "status_breakdown": [
    { "status": "Resolved", "count": 45 }
  ],
  "open_incidents": [
    { "id": 12, "title": "payments-api is Down", "severity": "High", "application": "payments-api" }
  ]
}
```

### `GET /api/incidents/{id}` — session
Adds a `timeline` array built from the audit log for that incident.

---

## Deployments

### `GET /api/deployments`
Query parameters: `search`, `environment`, `status`, `application_id`, `since`
(ISO-8601), `sort`, `order`, `page`, `per_page`.

```json
[
  {
    "id": 7,
    "application_id": 1,
    "application_name": "payments-api",
    "version": "v2.4.0",
    "environment": "Production",
    "status": "Successful",
    "commit_hash": "9f2c1ab4d5e6",
    "changelog": "Adds idempotency keys to the charge endpoint",
    "duration_seconds": 142,
    "deployed_by": "admin",
    "created_at": "2026-09-29T18:00:00+00:00",
    "deployed_at": "2026-09-29T18:02:00+00:00",
    "rolled_back_at": null
  }
]
```

### `POST /api/deployments` — write
Requires `application_id`, `version`, `environment`. Optional: `status`
(`Successful`, `Failed`, `In Progress`, `Rolled Back`), `commit_hash`,
`changelog`, `duration_seconds`, `deployed_by` (defaults to the session user).

### `POST /api/deployments/{id}/rollback` — write
Sets `status` to `Rolled Back` and stamps `rolled_back_at`. Returns `409` when
already rolled back.

### `GET /api/deployments/{id}/incidents` — session
Incidents recorded for the same application, most recent first.

---

## Monitoring

### `POST /api/monitor/run` — write
Runs one monitoring pass. `?force=false` only checks applications that are due.

```json
{
  "summary": {
    "total": 12,
    "healthy": 10,
    "degraded": 1,
    "unhealthy": 1,
    "open_incidents": 2,
    "checked_at": "2026-09-30T09:25:00+00:00",
    "duration_ms": 1840.22,
    "triggered_incidents": [],
    "resolved_incidents": [11]
  },
  "applications": [ /* per-application health results */ ]
}
```

### `GET /api/monitor/summary` — session
Side-effect free snapshot of the current monitoring state. Safe for high
frequency polling.

### `GET /api/monitor/history?hours=24` — session
### `GET /api/monitor/uptime?days=7` — session
### `POST /api/monitor/retention?days=30` — write
Prunes every unbounded table. With `?days=N` all tables use that window;
without it each table uses its own configured policy. A window of `0` keeps
that table forever.

```json
{
  "message": "Pruned 412 records",
  "removed": 412,
  "removed_by_table": {
    "health_checks": 400,
    "audit_logs": 8,
    "notifications": 4
  },
  "retention_days": 30
}
```

### `GET /api/monitor/retention` — session
Stored row counts and the windows the scheduled job enforces. Read-only.

```json
{
  "counts": {
    "health_checks": 2841,
    "audit_logs": 96,
    "notifications": 21,
    "auth_tokens": 2
  },
  "oldest_record": {
    "health_checks": "2026-09-01T04:12:00+00:00",
    "audit_logs": "2026-09-24T11:03:19+00:00"
  },
  "policies": {
    "health_checks_days": 30,
    "audit_logs_days": 180,
    "notifications_days": 30,
    "job_interval_hours": 6
  },
  "checked_at": "2026-09-30T18:04:11.900+00:00"
}
```

### `GET /api/monitor/scheduler` — session

---

## Overview

### `GET /api/overview`
Single round trip that fills the dashboard header, KPI cards and charts.

```json
{
  "generated_at": "2026-09-30T09:25:04.221+00:00",
  "applications": {
    "total": 12, "active": 11, "paused": 1,
    "healthy": 10, "degraded": 1, "unhealthy": 1,
    "unknown": 0, "never_checked": 0,
    "by_environment": [{ "environment": "Production", "count": 8 }]
  },
  "incidents": {
    "open": 2, "total": 48,
    "by_severity": [{ "severity": "Critical", "count": 4, "open": 1, "rank": 4 }],
    "by_status": [{ "status": "Resolved", "count": 45 }]
  },
  "deployments": {
    "total": 87,
    "by_status": [{ "status": "Successful", "count": 80 }]
  },
  "reliability": { "...": "see /api/incidents/stats" }
}
```

### `GET /api/overview/timeseries?hours=24`
```json
{
  "hours": 24,
  "labels": ["2026-09-30T00:00:00+00:00"],
  "checks": [1440],
  "avg_response_time_ms": [43.1],
  "failures": [0]
}
```

### `GET /api/overview/leaderboard?limit=10`
Applications ordered by ascending uptime.

---

## Activity

### `GET /api/activity`
Query parameters: `search`, `entity_type`, `entity_id`, `actor_id`, `sort`, `order`.

```json
[
  {
    "id": 501,
    "actor_id": 1,
    "actor_name": "admin",
    "action": "incident.resolved",
    "entity_type": "incident",
    "entity_id": 12,
    "summary": "Resolved incident 'payments-api is Down'",
    "detail": "{\"already_resolved\": false}",
"ip_address": "127.0.0.1",
      "created_at": "2026-09-30T09:31:02.774+00:00"
    }
  ]
```

### `GET /api/activity/notifications` — session
The alert delivery outbox. Every attempt is recorded, including failures, so a
silent notification gap is diagnosable without reading the application log.

Query parameters: `status` (`pending` / `sent` / `failed` / `skipped`),
`channel` (`webhook` / `email` / `log`), `event`, `incident_id`.

```json
[
  {
    "id": 88,
    "incident_id": 12,
    "application_id": 3,
    "channel": "webhook",
    "target": "https://hooks.example.com/cloudpulse",
    "event": "incident.opened",
    "status": "failed",
    "attempts": 1,
    "last_error": "<urlopen error [Errno 111] Connection refused>",
    "sent_at": null,
    "created_at": "2026-09-30T09:31:02.901+00:00"
  }
]
```

### `GET /api/activity/notifications/summary` — session

```json
{
  "by_status": { "sent": 14, "failed": 2, "skipped": 5 },
  "success_rate": 87.5,
  "configuration": {
    "webhook_configured": true,
    "smtp_configured": false,
    "on_incident_open": true,
    "on_incident_resolved": true,
    "on_deployment": true
  },
  "retention_days": 30
}
```

`success_rate` is `null` when nothing has been attempted yet, rather than a
misleading `100`.

### `POST /api/activity/notifications/{id}/retry` — write
Re-attempts delivery of one notification. The retry appends a **new** outbox
row rather than flipping the original in place, so the attempt is visible in
the delivery history.

- `409` when the notification was already delivered, or its incident no longer
  exists
- `404` when the id is unknown

---

## Exports

| Path | Contents |
| --- | --- |
| `GET /api/export/applications` | Registry with uptime and open incident counts |
| `GET /api/export/incidents?days=90&status=Resolved` | Incidents with duration |
| `GET /api/export/deployments` | Release history |
| `GET /api/export/health-checks?application_id=1&limit=1000` | Raw probe results |
| `GET /api/export/activity?limit=1000` | Audit trail |

All return `text/csv` with a `Content-Disposition: attachment` header.
