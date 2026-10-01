# CloudPulse

Cloud-native **application monitoring and incident management** platform.

CloudPulse registers your services, probes them on a schedule, records every
health check, opens and auto-resolves incidents when availability drops, tracks
releases, and exposes the whole picture through a dashboard, a REST API, CSV
exports and a Prometheus endpoint.

> DevOps major project — built with Flask, PostgreSQL, Docker, GitHub Actions,
> AWS (Terraform), Kubernetes, Prometheus and Grafana.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Testing](#testing)
- [Docker](#docker)
- [CI/CD](#cicd)
- [Infrastructure](#infrastructure)
- [Observability](#observability)
- [Documentation](#documentation)

---

## Features

### Monitoring
- Scheduled health checks per application with a configurable interval, timeout
  and HTTP method
- Accepted status code specification (`200-399, 404`) and optional response
  body keyword matching
- Custom request headers per application (stored as JSON, never logged)
- `UP` / `DEGRADED` / `DOWN` states, latency tracking, consecutive failure
  counters and rolling uptime
- Automatic incident creation with de-duplication and a configurable failure
  debounce, plus automatic resolution when the service recovers
- Retention job that prunes old health checks (`HEALTH_CHECK_RETENTION_DAYS`)
- Pause and resume monitoring per application
- Daily availability reporting per application

### Incident management
- Severity (`Low` / `Medium` / `High` / `Critical`) and status
  (`Open` / `Investigating` / `Resolved`) lifecycle
- Acknowledge, escalate, resolve, delete and bulk actions
- Root cause capture, assignee tracking and auto-filled cause on recovery
- SLA targets per severity with live countdown, breach detection and
  MTTR / p50 / p90 reliability statistics
- Full audit timeline per incident

### Delivery
- Deployment records with version, environment, commit hash, changelog,
  duration, author and status
- One-click rollback with a timestamped audit entry
- Deployment-to-incident correlation view
- Webhook and SMTP notifications for incident open/resolve and deployments

### Dashboard
- Fleet overview with KPI cards, availability/latency chart, health mix donut,
  incident severity breakdown and check volume chart
- Applications table with search, filters, sorting, pagination, bulk
  pause/delete, per-row health checks and a health history modal with sparkline
- Incidents table with SLA countdown, severity filters and a detail drawer
- Deployments table with rollback and release metadata
- Activity feed backed by the audit log, an alert delivery outbox with a manual
  retry action, and a stored history panel showing retention windows
- Dark and light themes, toasts, confirm dialogs, skeleton loaders, empty
  states, keyboard shortcuts (`1`–`5` to switch views, `R` to refresh)
- Zero runtime dependencies in the browser: charts are rendered as inline SVG

### Security
- CSRF tokens on the browser facing forms (Flask-WTF) plus an origin guard that
  requires `X-Requested-With` on state-changing API calls
- Brute force protection with a per `(email, client IP)` sliding window and a
  lockout, so one attacker cannot lock a victim's account out
- Self service password reset and email verification using single use,
  time limited tokens stored only as a SHA-256 digest
- Forgot-password responses never reveal whether an address is registered
- `APP_ENV=production` refuses to boot without an explicit `SECRET_KEY`; a
  development run without one gets a random ephemeral key instead of a
  published default
- Password policy: minimum length plus a character class requirement

### Platform
- Prometheus `/metrics` endpoint, `/health`, `/health/live`, `/health/ready`,
  `/api/cicd` and `/api/system`
- CSV exports for applications, incidents, deployments, health checks and
  activity
- Search, filter, sort and pagination across every collection
- Role based access control: `Admin` and `Editor` may write, `Viewer` is
  read-only
- Cross-origin mutation guard, security headers, `X-Request-ID` correlation
- Structured logging in either `text` or one-object-per-line `json` format
- Scheduled retention across health checks, audit entries, notifications and
  spent auth tokens

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
   Browser  ───────▶│  Flask (Gunicorn, 1 worker + threads)    │
                    │                                          │
                    │  api/          REST blueprints            │
                    │  services/     domain logic               │
                    │  models/       SQLAlchemy                 │
                    │  serializers/  JSON contracts             │
                    └───────┬───────────────┬──────────────────┘
                            │               │
                    ┌───────▼──────┐  ┌─────▼─────────────────────┐
                    │ PostgreSQL   │  │ APScheduler (background)    │
                    │ or SQLite    │  │ monitoring + retention     │
                    └──────────────┘  └────────────────────────────┘
                                                │
                    ┌───────────────────────────▼──────────────────┐
                    │ Prometheus scrapes /metrics                   │
                    │ Alertmanager routes and inhibits alerts       │
                    │ Grafana renders three provisioned dashboards  │
                    │ Promtail ships logs to Loki                   │
                    │ Webhooks / SMTP receive incident alerts       │
                    └──────────────────────────────────────────────┘
```

**Request lifecycle**

```
request
  → request id assigned (inbound X-Request-ID passed through)
  → CSRF token (forms) / origin guard (state-changing /api calls)
  → session identity loaded into flask.g
  → login_required (read) or write_required (Admin/Editor)
  → blueprint handler
  → service layer (health / incidents / notifications / accounts)
  → ORM models
  → audit log entry
  → response + security headers + Prometheus counters
```

---

## Project layout

```
CloudPulse/
├── app/
│   ├── app.py                 # entrypoint, app factory, auth routes
│   ├── config.py              # environment driven configuration objects
│   ├── extensions.py          # shared Flask extension singletons
│   ├── models.py              # SQLAlchemy models
│   ├── security.py            # login_required / admin_required guards
│   ├── serializers.py         # JSON payload builders
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── api/
│   │   ├── __init__.py        # blueprint registry
│   │   ├── helpers.py         # pagination, validation, write_required
│   │   ├── applications.py
│   │   ├── incidents.py
│   │   ├── deployments.py
│   │   ├── monitoring.py
│   │   ├── overview.py
│   │   ├── activity.py
│   │   ├── exports.py
│   │   └── system.py          # health probes and /metrics
│   ├── services/
│   │   ├── health.py          # probing, monitoring cycle
│   │   ├── incidents.py       # lifecycle rules, SLA, statistics
│   │   ├── notifications.py   # webhook and email delivery
│   │   ├── accounts.py        # password reset and email verification
│   │   ├── ratelimit.py       # per identifier/IP brute force window
│   │   ├── retention.py       # scheduled pruning of unbounded tables
│   │   ├── logging_config.py  # text and JSON log formatters
│   │   ├── metrics.py         # Prometheus exposition
│   │   ├── audit.py           # audit trail writer
│   │   ├── schema.py          # idempotent column/index sync
│   │   └── scheduler.py       # APScheduler wiring
│   ├── static/
│   │   ├── style.css
│   │   └── js/                # core, charts and one module per view
│   ├── seed_demo_data.py      # realistic local demo dataset
│   └── templates/
│       ├── index.html         # dashboard
│       ├── login.html
│       ├── register.html
│       ├── forgot_password.html
│       ├── reset_password.html
│       └── verify_email.html
├── tools/
│   └── demo_targets.py        # local endpoints for the demo dataset
├── tests/                     # pytest suite
├── chart/cloudpulse/          # Helm chart (deployment, ingress, HPA, monitoring)
├── k8s/                       # base manifests and kustomize overlays
├── infra/terraform/           # AWS VPC, ECR, RDS, ALB, ECS/IAM/S3
├── monitoring/                # Prometheus, Alertmanager, Loki, Grafana
├── docs/                      # architecture, API and runbook
├── .github/workflows/         # CI and Kubernetes deployment
├── docker-compose.yml
├── pyproject.toml             # ruff and pytest configuration
└── .env.example
```

---

## Quick start

### Local development

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/Scripts/activate      # Windows
source .venv/bin/activate          # macOS / Linux

# 2. Install dependencies
pip install -r app/requirements.txt

# 3. Configure the environment
copy .env.example .env             # Windows
cp .env.example .env               # macOS / Linux

# 4. Run the app
python app/app.py
```

Open <http://127.0.0.1:5000/register>, create the **first account** — that
account automatically becomes the `Admin`. Every later account is a `Viewer`.

### Demo data

To explore the dashboard with realistic content — six applications with seven
days of availability history, open and resolved incidents, and a release
timeline:

```bash
python app/seed_demo_data.py            # add demo data
python app/seed_demo_data.py --reset    # clear existing data first
```

The demo applications point at `tools/demo_targets.py`, a tiny local service
with healthy, slow, keyword-checked and error-returning endpoints. Start it in
a second terminal so every check is genuinely live and completely offline:

```bash
python tools/demo_targets.py            # serves on 127.0.0.1:5099
```

Then press **Run all checks** in the Applications view. You will see
`payments-api` and `notification-gateway` UP, `batch-worker` DEGRADED (it
answers in ~2.5 s), and `legacy-report-service` UP because its configuration
expects HTTP 404.

Set `DEMO_TARGET_BASE` to seed the applications at a different host, or just
edit the URLs in the Applications view to monitor your own services.

### Background monitoring

Set `ENABLE_SCHEDULER=true` to let CloudPulse check every application on its own
schedule instead of only when a user opens the dashboard. The Dockerfile ships
with a single Gunicorn worker and threads so exactly one scheduler runs.

### Upgrading an existing database

`create_all()` never alters an existing table, so an older database would not
pick up new columns. `AUTO_SCHEMA_SYNC=true` (the default) adds missing columns
and indexes in place, preserving existing rows. For production PostgreSQL, run
`flask --app app db upgrade` instead and set `AUTO_SCHEMA_SYNC=false`.

### Recovering an account

`SECRET_KEY` is not required locally, but set it in `.env` if you want sessions
to survive a restart. If you forget a password, `/login` links to
`/forgot-password`; without an SMTP host the reset link is written to the
terminal running the app instead of being emailed.

---

## Configuration

Every setting is read from the environment. See [`.env.example`](.env.example)
for the full annotated list. The essentials:

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`, `testing` or `production` |
| `SECRET_KEY` | random per boot | Session signing key — **required in production** |
| `PUBLIC_BASE_URL` | inbound host | Public address used in reset and verification links |
| `DATABASE_URL` | SQLite in `instance/` | PostgreSQL connection string |
| `ENABLE_SCHEDULER` | `false` | Run the background monitoring loop |
| `MONITOR_INTERVAL_SECONDS` | `60` | How often due applications are probed |
| `HEALTH_CHECK_TIMEOUT` | `5` | Per request timeout in seconds |
| `HEALTH_CHECK_VERIFY_TLS` | `true` | Verify TLS certificates — see below |
| `HEALTH_CHECK_RETENTION_DAYS` | `30` | How long health checks are kept |
| `MONITOR_DEGRADED_AFTER_CHECKS` | `2` | Failures needed before opening an incident |
| `AUTO_CREATE_INCIDENTS` | `true` | Open incidents on outages |
| `AUTO_RESOLVE_INCIDENTS` | `true` | Resolve incidents on recovery |
| `SLA_CRITICAL_MINUTES` | `30` | SLA target per severity |
| `NOTIFY_WEBHOOK_URL` | unset | Slack / generic webhook for alerts |
| `NOTIFY_SMTP_HOST` | unset | SMTP server for email alerts |
| `AUTO_CREATE_SCHEMA` | `true` | Create missing tables on start-up |
| `AUTO_SCHEMA_SYNC` | `true` | Add missing columns/indexes to an existing database |
| `CSRF_PROTECTION_ENABLED` | `true` | Form tokens plus the API origin check |
| `STRICT_ORIGIN_CHECK` | `true` | Reject cross-origin state changes |
| `TRUSTED_PROXY_NETWORKS` | unset | Ranges whose `X-Forwarded-For` is believed — see below |
| `CSP_ENABLED` | `false` | Also send a Content-Security-Policy header |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed logins allowed per window, `0` disables |
| `LOGIN_LOCKOUT_SECONDS` | `900` | Lockout applied once the limit is hit |
| `LOGIN_ATTEMPT_WINDOW_SECONDS` | `300` | Sliding window for failed attempts |
| `PASSWORD_RESET_TOKEN_TTL_SECONDS` | `3600` | Lifetime of a reset link |
| `REQUIRE_EMAIL_VERIFICATION` | `false` | Refuse sign-in until the address is verified |
| `MIN_PASSWORD_LENGTH` | `8` | Minimum password length |
| `AUDIT_LOG_RETENTION_DAYS` | `180` | How long audit entries are kept |
| `NOTIFICATION_RETENTION_DAYS` | `30` | How long outbox rows are kept |
| `NOTIFICATION_RETRY_MAX_AGE_DAYS` | `7` | How far back the manual retry may reach, `0` disables |
| `RETENTION_JOB_HOURS` | `6` | How often the retention job runs |
| `LOG_FORMAT` | `text` | `text` or `json` for Loki/Elasticsearch ingestion |
| `LOG_LEVEL` | `INFO` | Root log level |

### Secret key handling

The original fallback was a literal string committed to the repository, which
means every deployment that forgot to set `SECRET_KEY` shared the same signing
key. That is now a startup error:

- `APP_ENV=production` raises unless `SECRET_KEY` is set.
- Development and testing generate a random ephemeral key and log a warning.
  Sessions therefore do not survive a restart, which is the correct trade-off
  for a local run.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Production also refuses two things a check for "is it set" would have passed:

- **A placeholder.** `replace-me-with-a-long-random-value`, `changeme` and
  similar are rejected. These values ship in the Helm chart and the kustomize
  overlays, so this repository is public and anyone can read them. A signing key
  that is publicly known is worse than an absent one, because it looks
  configured: sessions can be forged for the deployment. Set
  `ALLOW_INSECURE_SECRET_KEY=true` to bypass deliberately.
- **A key under 32 characters.**

```bash
# What the check does
SECRET_KEY=replace-me-with-a-long-random-value APP_ENV=production python app/app.py
# RuntimeError: SECRET_KEY is set to 'replace-me-with-a-long-random-value', which
# is a placeholder published in this repository. Anyone who can read it can forge
# session cookies for this deployment.
```

### Account security

**Brute force protection.** Failed logins are counted per `(email, client IP)`
pair over a sliding window. Tracking only the email would let an attacker lock a
victim out deliberately; tracking only the IP would let one attacker brute force
many accounts. A successful login clears the counter, and a limit of `0`
disables throttling entirely. The counters are held in process memory, so a
multi-worker gunicorn deployment applies the limit per worker.

Two details make the limit meaningful rather than decorative:

- The attempt slot is **reserved before** the password hash runs. Checking the
  limit and recording the failure as two separate steps leaves an ~80 ms window
  in which a burst of parallel requests all pass the check before any of them
  records anything, so the limiter never engages at all.
- The client address is only read from `X-Forwarded-For` when the immediate
  peer is listed in `TRUSTED_PROXY_NETWORKS`. That header is supplied by the
  client and can contain anything, so trusting it by default would let an
  attacker mint a fresh bucket per request. See
  [Client addresses behind a proxy](#client-addresses-behind-a-proxy).

### Client addresses behind a proxy

The login rate limiter and the audit trail both key on the client address, so
that value has to be one an attacker cannot choose. `request.remote_addr` is
the only such value, because it comes from the socket.

`X-Forwarded-For` is added by proxies, not by the client, but the client can
send it too. If the app trusts it unconditionally, anyone can put a new address
in every request and get a fresh rate limit bucket each time.

`TRUSTED_PROXY_NETWORKS` lists the ranges that are genuinely your own
infrastructure:

```bash
# Directly reachable, no proxy in front: leave empty.
TRUSTED_PROXY_NETWORKS=

# Behind an internal load balancer or ingress controller.
TRUSTED_PROXY_NETWORKS=10.0.0.0/8,172.16.0.0/12
```

When set, the header is read **only** if the immediate peer is in the list. The
chain is then walked right to left and the first address that is not itself a
trusted proxy is used. If every entry is a trusted proxy, the header identifies
no client and the socket peer is used instead.

Leaving it empty is the safe default. Setting it too broadly reintroduces the
problem, so list specific ranges rather than `0.0.0.0/0`.

### Account recovery

**Password reset.** `/forgot-password` accepts an address and always returns the
same message whether or not the account exists, so the form cannot be used to
enumerate users. Tokens are single use, expire after
`PASSWORD_RESET_TOKEN_TTL_SECONDS`, are invalidated when a newer one is
issued, and only their SHA-256 digest is stored. Requests are rate limited for
unknown addresses as well, since that is the set an attacker actually uses.

With no SMTP host configured the link is written to the log — but **only
outside production**. In production it is withheld and an error is logged
instead, because the link is a live credential for the whole token lifetime and
anything with log access could complete the reset.

**Email verification.** `REQUIRE_EMAIL_VERIFICATION=true` blocks sign-in until
the address is confirmed. Tokens follow the same rules as reset tokens.

Set `PUBLIC_BASE_URL` when running behind a proxy or load balancer, otherwise
emails will contain the internal host name.

### Structured logging

`LOG_FORMAT=json` writes one JSON object per line for Loki or Elasticsearch.
Every record carries `timestamp`, `level`, `logger` and `message`; the request id
is promoted to its own field; and anything else is nested under `context`.
`monitoring/loki/promtail-config.yml` parses both formats.

Every response carries an `X-Request-ID` header. An inbound `X-Request-ID` is
passed through unchanged so a gateway correlation id survives into the logs.

### Corporate proxies and TLS interception

Many office and campus networks intercept HTTPS with a proxy whose root
certificate is only installed in the system trust store. CloudPulse reports
this precisely rather than as a generic failure:

```
TLS certificate verification failed. Install the issuing CA in the system trust
store, or set HEALTH_CHECK_VERIFY_TLS=false when monitoring through a
TLS-intercepting proxy.
```

Certificate problems, DNS failures, refused connections and timeouts each get
their own message so a red dashboard tells you *why*. The fix is to trust the
intercepting CA; `HEALTH_CHECK_VERIFY_TLS=false` is an escape hatch that
disables verification entirely and should only be used deliberately.

---

## API reference

All `/api/` endpoints except the system probes require an authenticated
session. Writes additionally require the `Admin` or `Editor` role.

<details>
<summary><strong>System</strong></summary>

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/health` | public | Liveness probe |
| GET | `/health/live` | public | Liveness probe (Kubernetes) |
| GET | `/health/ready` | public | Readiness probe including a database round trip |
| GET | `/metrics` | public | Prometheus exposition |
| GET | `/api/cicd` | public | Deployment verification used by pipelines |
| GET | `/api/system` | session | Runtime and platform information |
| GET | `/api/me` | session | Current user profile |

</details>

<details>
<summary><strong>Applications</strong></summary>

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/api/applications` | session | List, filter, sort, paginate |
| POST | `/api/applications` | write | Register an application |
| GET | `/api/applications/{id}` | session | Detail with recent checks |
| PUT/PATCH | `/api/applications/{id}` | write | Update |
| DELETE | `/api/applications/{id}` | write | Delete with cascade |
| POST | `/api/applications/{id}/check` | write | Run one health check now |
| GET | `/api/applications/{id}/health` | session | Legacy check endpoint |
| GET | `/api/applications/{id}/health-history` | session | Check history and availability |
| GET | `/api/applications/{id}/uptime` | session | Daily availability report |
| POST | `/api/applications/{id}/pause` | write | Pause monitoring |
| POST | `/api/applications/{id}/resume` | write | Resume monitoring |
| GET | `/api/applications/metadata` | session | Enumerations and sort columns |

</details>

<details>
<summary><strong>Incidents</strong></summary>

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/api/incidents` | session | List, filter, sort, paginate |
| POST | `/api/incidents` | write | Create |
| GET | `/api/incidents/{id}` | session | Detail with audit timeline |
| PUT/PATCH | `/api/incidents/{id}` | write | Update |
| PUT/POST | `/api/incidents/{id}/resolve` | write | Resolve |
| POST | `/api/incidents/{id}/acknowledge` | write | Move to Investigating |
| POST | `/api/incidents/{id}/escalate` | write | Raise severity one level |
| DELETE | `/api/incidents/{id}` | write | Delete |
| POST | `/api/incidents/bulk` | write | Bulk `resolve` or `delete` |
| GET | `/api/incidents/stats` | session | Reliability KPIs |
| GET | `/api/incidents/metadata` | session | Enumerations and SLA targets |

</details>

<details>
<summary><strong>Deployments, monitoring, overview, activity, exports</strong></summary>

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET/POST | `/api/deployments` | session / write | List and record |
| GET/PUT/DELETE | `/api/deployments/{id}` | session / write | Detail, update, delete |
| POST | `/api/deployments/{id}/rollback` | write | Mark as rolled back |
| GET | `/api/deployments/{id}/incidents` | session | Incidents for that application |
| GET | `/api/deployments/latest` | session | Most recent releases |
| GET | `/api/monitor` | session | Run a monitoring pass (legacy path) |
| POST | `/api/monitor/run` | write | Run a monitoring pass |
| GET | `/api/monitor/summary` | session | Side-effect free current state |
| GET | `/api/monitor/history` | session | Raw check history |
| GET | `/api/monitor/uptime` | session | Fleet availability report |
| GET | `/api/monitor/retention` | session | Stored row counts and retention windows |
| POST | `/api/monitor/retention` | write | Prune history (`?days=` overrides the policy) |
| GET | `/api/monitor/scheduler` | session | Scheduler state |
| GET | `/api/overview` | session | Aggregated dashboard payload |
| GET | `/api/overview/timeseries` | session | Checks, latency and failures over time |
| GET | `/api/overview/leaderboard` | session | Lowest availability first |
| GET | `/api/activity` | session | Audit trail |
| GET | `/api/activity/notifications` | session | Alert delivery outbox |
| GET | `/api/activity/notifications/summary` | session | Delivery rate and alerting configuration |
| POST | `/api/activity/notifications/{id}/retry` | write | Re-attempt a failed delivery |
| GET | `/api/export/{resource}` | session | CSV export |

Supported export resources: `applications`, `incidents`, `deployments`,
`health-checks`, `activity`.

</details>

<details>
<summary><strong>Account recovery (HTML forms)</strong></summary>

These render pages rather than JSON and are the only routes subject to the
Flask-WTF CSRF token.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET/POST | `/forgot-password` | public | Request a reset link |
| GET/POST | `/reset-password/{token}` | public | Choose a new password |
| GET | `/verify-email/{token}` | public | Confirm an address |

</details>

### Pagination

Collections return a plain JSON array by default, preserving the original
API contract. Pass `?page=1&per_page=25` (or `?paginated=true`) to receive the
envelope instead:

```json
{
  "data": [ ... ],
  "pagination": {
    "page": 1, "per_page": 25, "total": 87,
    "total_pages": 4, "has_next": true, "has_previous": false
  }
}
```

### Errors

```json
{ "error": "Validation failed", "fields": { "url": "URL scheme 'ftp' is not allowed" } }
```

| Status | Meaning |
| --- | --- |
| `400` | Malformed request, or an invalid/expired recovery token |
| `401` | Not authenticated |
| `403` | Missing role, a blocked cross-origin mutation, or an unverified email |
| `404` | Resource not found |
| `409` | Conflicting state change |
| `422` | Field validation failed |
| `429` | Rate limited: too many failed logins or registrations |
| `500` | Unexpected server error |

---

## Testing

```bash
pytest tests -v            # full suite
pytest tests -m smoke      # fast page and boot checks
ruff check app tools tests # lint
```

The suite uses an isolated in-memory SQLite database created through the
application factory, patches `urlopen` so no test touches the network, and
covers the auth flow, role enforcement, application and incident CRUD,
deployments and rollback, health probing and incident reconciliation,
monitoring cycles, retention, Prometheus output, notifications, exports and
the audit trail.

Security behaviour is tested directly rather than assumed: rate limit scoping
per identifier and per client IP, lockout expiry, token single-use and
expiry, password policy, the no-account-enumeration guarantee on
`/forgot-password`, email verification gating, the production `SECRET_KEY`
refusal, and both log formatters.

---

## Docker

```bash
# Build
docker build -t cloudpulse:local -f app/Dockerfile app

# Run standalone
docker run -p 5000:5000 -e SECRET_KEY=dev-secret cloudpulse:local

# Full stack: app, PostgreSQL, Prometheus, Alertmanager, Grafana, Loki, Promtail
SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") docker compose up --build
```

| Service | URL |
| --- | --- |
| CloudPulse | <http://localhost:5000> |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3000> (`admin` / `admin`) |
| Alertmanager | <http://localhost:9093> |
| Loki | <http://localhost:3100> |

The compose file sets `APP_ENV=production`, so it requires `SECRET_KEY` in the
environment or `.env` rather than falling back to a default. The image is
multi-stage, runs as an unprivileged user (`uid=999`), and ships a `HEALTHCHECK`
that hits `/health` using `urllib` from the standard library rather than curl —
installing curl needed an `apt-get` round trip that fails on any host which
cannot reach the Debian mirrors.

Two variables matter inside the image:

| Variable | Why |
| --- | --- |
| `INSTANCE_DIR` | The SQLite fallback defaults to `<repo>/instance`, which assumes the modules live in `<repo>/app/`. The image mounts them at the root, so the same calculation yields `/instance` — not writable. The image sets `/home/cloudpulse/instance`, the only path the unprivileged user owns. |
| `AUTO_CREATE_SCHEMA` | `ProductionConfig` defaults it to `false`, because with a real `DATABASE_URL` the schema is owned by migrations. The SQLite fallback has no migration path, so the image sets it to `true`; otherwise the container boots, answers `/health`, and returns `500` from every endpoint that touches a table. |

Point `DATABASE_URL` at PostgreSQL, run `flask --app app db upgrade`, and set
`AUTO_CREATE_SCHEMA=false` and `AUTO_SCHEMA_SYNC=false` in production.

`tests/test_container.py` builds the image, runs it, and asserts every public
endpoint answers and that the process is not root.

---

## CI/CD

`.github/workflows/docker-ci.yml` runs five jobs on every push and pull
request to `main`:

1. **lint** — `ruff check app tools tests`
2. **infra** — `terraform fmt -check` and `terraform validate`, `helm lint` plus
   `helm template` (including the ingress, autoscaling and monitoring paths),
   `kubectl kustomize` on both overlays, a YAML parse of every config file, and
   `promtool` / `amtool` validation of the Prometheus and Alertmanager configs
3. **test** — verifies the app imports, asserts that production refuses to boot
   without a `SECRET_KEY`, then `pytest tests -v`
4. **security** — `pip-audit` against the pinned requirements, and a
   `gitleaks` scan for committed secrets
5. **docker-build** — buildx build and push to `ghcr.io`, then a live smoke
   test of the published image

Infra lives in its own job because a syntax error in a Terraform or Helm file
would otherwise only surface at deploy time, long after review.

`.github/workflows/kubernetes-deploy.yml` validates the kustomize overlays and,
on `main`, applies them to the configured cluster and waits for a healthy rollout.

---

## Infrastructure

### Terraform

```bash
cd infra/terraform
terraform init
terraform plan -var="image=ghcr.io/<owner>/cloudpulse:latest"
terraform apply
```

Provisions a VPC with public and private subnets, a NAT gateway, an ECR
repository with lifecycle policies and image scanning, an encrypted PostgreSQL
instance with backups and deletion protection, and the Kubernetes namespace,
config, deployment, service, HPA and service account.

Additional files cover the production path:

| File | Contents |
| --- | --- |
| `iam.tf` | ECS execution and task roles, scoped Secrets Manager read, CloudWatch log group |
| `alb.tf` | Public ALB, target group on `/health/ready`, HTTP→HTTPS redirect, TLS listener, S3 access logs, TLS-only bucket policy |
| `ecs.tf` | ECS cluster, task definition (secrets via `secrets`, not `environment`), service on the target group, CPU autoscaling |
| `storage.tf` | Versioned, encrypted, private artifact bucket with lifecycle tiers and a TLS-only policy |

Set `deployment_target` to `kubernetes` or `ecs`. The ECS resources are guarded
with `count` so only the chosen path is created; the two are alternatives, not
a sequence.

> `terraform` and `helm` are not installed on the development machine, so
> that configuration was checked structurally (HCL balance, YAML/JSON parse,
> and the chart's ConfigMap/Secret split) rather than by running the tools. The
> Kubernetes overlays *are* validated for real, via `kubectl kustomize` in
> `tests/test_kustomize.py`. CI runs `terraform validate`, `helm lint`,
> `helm template` and `promtool` on every pull request.

> The overlays patch the base ConfigMap with a strategic merge rather than
> merging via `configMapGenerator`. A generator only merges into maps it
> generated itself, so it cannot reach the static ConfigMap the base declares,
> and `kubectl kustomize` failed with *"does not exist; cannot merge or
> replace"*.

### Helm

```bash
helm lint chart/cloudpulse
helm template cloudpulse chart/cloudpulse
helm upgrade --install cloudpulse chart/cloudpulse \
  --set image.tag=1.0.0 \
  --set config.existingSecret=cloudpulse-secrets \
  --set config.PUBLIC_BASE_URL=https://cloudpulse.example.com \
  --set ingress.enabled=true
```

The chart renders the deployment, service, headless service, service account,
config map, optional secret, ingress, HPA, default-deny NetworkPolicy,
ServiceMonitor and PrometheusRule. `SECRET_KEY` must come from
`config.existingSecret` or be supplied when `config.createSecret=true`; the
chart fails to render rather than deploying a default key.

Only six keys are treated as credentials and routed to the Secret: `SECRET_KEY`,
`DATABASE_URL`, `NOTIFY_WEBHOOK_URL`, `NOTIFY_WEBHOOK_SECRET`,
`NOTIFY_SMTP_USER` and `NOTIFY_SMTP_PASSWORD`. Everything else in `config`,
including the SMTP host and the `NOTIFY_ON_*` switches, goes to the ConfigMap.
That split is an allow-list in the `cloudpulse.secretKeys` helper, so a setting
added in a later version defaults to the ConfigMap instead of silently landing
in a Secret or being dropped. `tests/test_chart_config.py` pins it.

Set `config.PUBLIC_BASE_URL` to the address users actually reach, otherwise
password reset emails will contain the in-cluster host name from behind the
Ingress.

### Kubernetes

```bash
kubectl apply -k k8s/overlays/production
kubectl -n cloudpulse rollout status deployment/cloudpulse
```

The base manifests include a namespace with the restricted Pod Security
Standard, probes wired to `/health/live` and `/health/ready`, a PodDisruption
Budget, a HorizontalPodAutoscaler, a default-deny plus allow NetworkPolicy pair,
a non-root read-only container with an in-memory `/tmp`, topology spread
constraints and Prometheus scrape annotations. Staging and production overlays
differ in replica count, check interval and cookie security. The ConfigMap
carries the same security and retention settings as `.env.example`.

> The Secret in `k8s/base/cloudpulse.yaml` ships placeholder values, including
> a `DATABASE_URL` pointing at a `cloudpulse-postgres` host that this manifest
> does not define. Replace them before applying, or use the Helm chart with an
> existing secret.

---

## Observability

Prometheus scrapes `/metrics` every 15 seconds using the configuration in
`monitoring/prometheus.yml`. Exposed series include:

| Metric | Type | Meaning |
| --- | --- | --- |
| `cloudpulse_application_up` | gauge | 1 when the last check succeeded |
| `cloudpulse_application_response_time_ms` | gauge | Latency of the last check |
| `cloudpulse_application_uptime_ratio` | gauge | Availability ratio, 0 to 1 |
| `cloudpulse_application_consecutive_failures` | gauge | Failure streak |
| `cloudpulse_application_info` | gauge | Static labels including the URL |
| `cloudpulse_health_checks_recorded` | gauge | Stored checks per status |
| `cloudpulse_health_check_response_time_ms` | gauge | Average and maximum latency |
| `cloudpulse_incidents` | gauge | Incidents by severity and status |
| `cloudpulse_incidents_open` | gauge | Unresolved incident count |
| `cloudpulse_deployments` | gauge | Deployments by environment and status |
| `cloudpulse_http_requests_total` | counter | Requests by route, method and status |
| `cloudpulse_http_request_duration_seconds_*` | histogram | Request latency distribution |
| `cloudpulse_monitoring_runs_total` | counter | Completed monitoring cycles |
| `cloudpulse_monitoring_last_run_timestamp_seconds` | gauge | Last cycle timestamp |
| `cloudpulse_process_uptime_seconds` | gauge | Process uptime |
| `cloudpulse_build_info` | gauge | Version, commit and environment |

Alert rules in `monitoring/alert-rules.yml` cover application outages, slow
responses, incident spikes, a stalled monitoring loop and an unscrapeable
target. `monitoring/alertmanager.yml` routes them to Slack and PagerDuty, and
inhibits `warning` alerts when a matching `critical` is already firing.

> `cloudpulse_incidents` is a gauge recomputed from the database at every
> scrape, not a counter of incidents ever created, so the spike rule uses
> `delta()` rather than `increase()`. A true creation rate would need a counter
> incremented on insert.

### Grafana

Three provisioned dashboards ship in `monitoring/grafana/dashboards/`:

| Dashboard | Focus |
| --- | --- |
| `cloudpulse-overview.json` | Fleet health, availability, latency, severity mix |
| `cloudpulse-incidents.json` | Incident volume, MTTR, SLA breaches, worst offenders |
| `cloudpulse-endpoints.json` | Per-application availability, latency percentiles, status codes |

### Logs

`monitoring/loki/` contains a single-binary Loki configuration and a Promtail
pipeline that parses both the `text` and `json` log formats, extracting the
timestamp, level and logger into labels.

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, data model, design decisions |
| [docs/API.md](docs/API.md) | Endpoint contracts and payloads |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Day-two operations and troubleshooting |
