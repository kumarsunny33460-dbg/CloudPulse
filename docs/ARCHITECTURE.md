# CloudPulse Architecture

## 1. Goals

CloudPulse answers three questions continuously:

1. **Is my application available?** — scheduled probing, latency and uptime.
2. **What broke, and who owns it?** — incident lifecycle with SLA tracking.
3. **What did we ship, and did it cause the outage?** — deployment timeline.

Everything else (dashboard, exports, Prometheus) exists to make those answers
actionable.

## 2. Layering

```
HTTP          app/api/*            routing, validation, serialisation
Domain        app/services/*       probing, incident rules, alerting, metrics
Persistence   app/models.py        SQLAlchemy models and constraints
Adapters      config.py, serializers.py, extensions.py
```

The dependency direction is strictly downwards. `services` never imports from
`api`; `api` never reaches into the database for domain rules. This is what
makes the rules testable without HTTP and reusable from the background
scheduler.

### Why a single `app.py` entrypoint

The `app/` directory is the import root, matching `gunicorn app:app`. That keeps
the container command and the local `python app/app.py` identical. `app.py`
holds the application factory, the request lifecycle and the authentication
routes; everything else lives in modules.

## 3. Data model

```
users ──────────────┐
                    │ (actor_id, SET NULL)
audit_logs          │
                    │
applications ──┬── incidents ──┬── notifications
               ├── deployments   │
               └── health_checks ┘
                        │
                        └── triggered_incident_id (SET NULL)
```

| Table | Purpose | Notable columns |
| --- | --- | --- |
| `users` | Accounts and roles | `role` (`Admin` / `Editor` / `Viewer`) |
| `applications` | Monitored services | monitoring config, last known state, uptime |
| `health_checks` | One row per probe | status, http status, latency, error, linked incident |
| `incidents` | Incident lifecycle | severity, status, assignee, root cause, SLA anchors |
| `deployments` | Release history | version, commit, changelog, rollback timestamp |
| `audit_logs` | Append-only change log | actor, action, entity, before/after detail |
| `notifications` | Alert outbox | channel, event, delivery status, attempts |

### Design decisions

**Timezone aware timestamps everywhere.** Every column is
`DateTime(timezone=True)` and is populated with `datetime.now(UTC)`. The
original code mixed `datetime.utcnow` with `datetime.now(UTC)` and used naive
columns, which silently produced incomparable timestamps.

**Enums enforced by the database.** Severity, status, environment, HTTP method
and channel all have `CHECK` constraints. Validation in Python gives good error
messages; the constraint guarantees no other writer can corrupt the data.

**Cascading deletes on the model.** `Application` owns incidents, deployments
and health checks with `cascade="all, delete-orphan"`. Deleting a service is a
single statement and cannot orphan rows.

**Indexes match the query patterns.** `(application_id, checked_at)` on
`health_checks` serves the history query; `(application_id, status)` on
`incidents` serves the open-incident lookup that runs on every monitoring
cycle; `(entity_type, entity_id)` on `audit_logs` serves the incident timeline.

**`health_checks` grows, then shrinks.** One row per probe is the source of
truth for availability reporting. It is therefore the only unbounded table,
and the retention job deletes rows older than
`HEALTH_CHECK_RETENTION_DAYS` every six hours.

**`audit_logs` commits immediately.** Audit calls happen after the business
transaction has committed, so the entry is written in its own transaction. If
it shared the business transaction it would be discarded when the request
scoped session is torn down.

## 4. Monitoring cycle

```
scheduler tick
   └─▶ select applications where not paused, status = Operational, due
         └─▶ probe(url, method, headers, timeout, expected status, keyword)
               ├─▶ 2xx/3xx and fast   → UP
               ├─▶ 2xx/3xx and slow   → DEGRADED
               └─▶ anything else       → DOWN
         └─▶ persist HealthCheck, update last known state
         └─▶ if DOWN
               ├─▶ debounce: fewer than MONITOR_DEGRADED_AFTER_CHECKS failures
               │   → record the check but do not open an incident
               └─▶ create_incident_if_needed → de-duplicated open incident
         └─▶ if UP or DEGRADED and AUTO_RESOLVE_INCIDENTS
               └─▶ resolve monitoring-created incidents
```

**Debouncing.** A single failed probe on a flaky network should not page
anyone. `MONITOR_DEGRADED_AFTER_CHECKS` (default 2) requires consecutive
failures before an incident is opened. The failing check is still recorded, so
the history stays honest.

**Auto-resolution only touches machine-created incidents.** A `Viewer` who
raised "release freeze in progress" should not have it silently closed by a
green health check, so resolution filters on `source == "monitoring"`.

**Degradation threshold.** Responses slower than 1000 ms are recorded as
`DEGRADED` rather than `UP`: the service is answering, but not usefully.

## 5. Request lifecycle

| Phase | What happens |
| --- | --- |
| `before_request` | Request id assigned (inbound `X-Request-ID` passed through) |
| `before_request` | Session identity copied onto `flask.g` for the audit trail |
| `before_request` | Cross-origin guard on state-changing `/api/` calls |
| View | `login_required` for reads, `write_required` for mutations |
| View | Payload validation, then a service call |
| Service | Domain rules, then a single `commit()` |
| Service | Audit entry and notification outbox rows |
| `after_request` | Security headers, request id echo, no-store on API responses, metrics |
| `teardown` | Rollback on unhandled exceptions |

### Cross-origin guard

Two different mechanisms cover two different surfaces:

- **HTML form routes** (`/login`, `/register`, `/forgot-password`,
  `/reset-password/<token>`) use a Flask-WTF CSRF token in a hidden
  `csrf_token` field.
- **JSON API routes** require either `X-Requested-With: XMLHttpRequest` (which
  the dashboard always sends) or a same-origin `Origin` / `Referer`. Cross-site
  form posts and simple requests cannot satisfy either condition, which is
  exactly the CSRF threat model.

The API blueprints are exempt from the form-token check on purpose. A `fetch`
client would need a token round trip per request, and the custom-header
requirement already blocks the attack; adding the form token would be cost
without additional protection. When `CSRF_PROTECTION_ENABLED=false` the
`csrf_token` template global still exists and renders empty, so templates need
no conditional.

### Authorisation

Authentication stays exactly as it was. On top of it, `write_required` admits
`Admin` and `Editor` and rejects `Viewer` with `403`. Admin-only operations
continue to use `admin_required`.

### Brute force protection

`ratelimit.py` keeps a sliding window of failures per `(identifier, client IP)`
pair in process memory. Both halves of the key matter: keying on the email
alone lets an attacker deliberately lock a victim out, and keying on the IP
alone lets one attacker sweep many accounts without ever tripping the limit.

The store is a plain dictionary guarded by a lock rather than Redis, so the
limiter adds no infrastructure dependency and cannot fail the request path. The
consequence is documented rather than hidden: with `N` gunicorn workers the
effective limit is `LOGIN_MAX_ATTEMPTS * N`, and locks are released on restart.
The interface is small enough to swap for a shared store when that matters.

### Account recovery tokens

Reset and verification links carry a token whose SHA-256 digest is the only
thing persisted, so a database snapshot cannot be replayed. Issuing a new token
for the same purpose deletes the previous one, which makes the most recent link
the only valid one; `used_at` makes each single use.

`/forgot-password` returns the same message and does comparable work for a
known and an unknown address, so it is not an account enumeration oracle.

### Secret key

`resolve_secret_key()` prefers the environment. Outside production a missing
value produces a random ephemeral key rather than a literal committed to the
repository, and a warning is logged. `ProductionConfig` is validated by
`validate_config()` in `create_app` and raises when the variable is absent.
`app.config.from_object` never instantiates a config class, so the check cannot
live in a constructor.

## 6. Observability design

`GET /metrics` implements the Prometheus text exposition format directly. No
client library is required, and the payload is easy to reason about.

Two kinds of data are exposed:

- **Database derived**, computed at scrape time: application state, incident
  counts, deployment counts, aggregate latency. These are always accurate
  because they are never cached.
- **Process derived**, accumulated in memory: HTTP request counters, the
    latency histogram and monitoring cycle counters. These reset when the
    process restarts, which is normal for process-level instrumentation.

Anything derived from the database is recomputed on every scrape, so it is a
gauge rather than a counter. That has a consequence worth stating: `increase()`
on `cloudpulse_incidents` would extrapolate a reset on each scrape and only ever
grow, so the spike alert uses `delta()` and measures the real change. A true
creation rate would need a counter incremented on insert.

### Structured logging

`logging_config.py` emits one of two formats. `text` is the human readable pipe
separated line used during local development. `json` writes one object per line
with a stable key set — `timestamp`, `level`, `logger`, `message`, `request_id`,
plus any promoted request fields, with everything else nested under `context`.
A stable schema is what lets a log query avoid parsing the message text.

The request id is attached by a logging filter reading `flask.g`, so it reaches
records emitted by any library, not just the ones that pass it explicitly.

### Retention

Every table that grows without bound has a retention window: health checks,
audit entries, notification outbox rows and spent auth tokens. `retention.py`
prunes them together and reports a per-table total; the scheduler runs it every
`RETENTION_JOB_HOURS`. Windows are configurable and a value of `0` disables
pruning for that table.

The manual endpoint accepts an explicit window, which is what you want after
an incident that generated far more history than usual.

The monitoring loop and the HTTP layer share the process, so
`cloudpulse_monitoring_runs_total` and
`cloudpulse_monitoring_last_run_timestamp_seconds` give a direct signal for a
stalled scheduler. The matching alert rule fires when no cycle completes for
five minutes.

## 7. Frontend architecture

No build step, no package manager, no CDN dependency.

| File | Responsibility |
| --- | --- |
| `core.js` | DOM helpers, fetch wrapper, toasts, modals, formatters, store |
| `charts.js` | Dependency-free SVG line, bar, donut and sparkline charts |
| `overview.js` | KPI cards, charts, attention list, activity preview |
| `applications.js` | Registry table, filters, form, health history modal |
| `incidents.js` | Triage table, form, detail drawer, bulk actions |
| `deployments.js` | Release table, form, rollback |
| `activity.js` | Audit trail timeline |
| `main.js` | Shell: identity, navigation, theme, auto refresh |

Charts are inline SVG computed from the current theme's CSS custom properties,
so they follow the dark and light themes without re-rendering and without
shipping a charting library. The theme is stored in `localStorage` and defaults
to the operating system preference.

The original single 94 KB `index.html` was split into a template plus eight
focused modules, which is what made the views independently maintainable.

## 8. Deployment topology

```
GitHub (main)
   └─▶ CI: lint → test → security → build & push to GHCR
          └─▶ Kubernetes deploy workflow → rolling update
                 └─▶ 2 replicas, HPA 2-8, PDB minAvailable 1
                        └─▶ Prometheus scrapes /metrics
                               └─▶ Grafana renders the provisioned dashboard
```

Terraform owns the cloud foundation: VPC, subnets, NAT, ECR and PostgreSQL,
plus the Kubernetes namespace and workload. Kubernetes manifests are managed
with kustomize so the same base serves staging and production.

A single Gunicorn worker with eight threads is deliberate. Multiple workers
would start multiple background schedulers and split the in-process Prometheus
counters. Horizontal scaling is handled by the HPA adding replicas, each with
its own scheduler; the debounce window and the open-incident de-duplication
make that safe.

## 9. Known trade-offs

| Decision | Trade-off |
| --- | --- |
| One row per health check | Precise reporting at the cost of a table that needs pruning |
| Schema via `create_all` by default | Zero friction locally; production should set `AUTO_CREATE_SCHEMA=false` and run migrations |
| Text-format Prometheus exporter | No dependency, slightly more code than a client library |
| Session cookie authentication | Simple to operate; a token-based API for machine clients would be the next step |
| SQLite fallback | Ideal for a local demo; PostgreSQL is required for concurrent production traffic |
