# syntax=docker/dockerfile:1

# ==================================================================
# Stage 1 - build the dependency layer
# ==================================================================
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ==================================================================
# Stage 2 - runtime
# ==================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    PORT=5000 \
    INSTANCE_DIR=/home/cloudpulse/instance \
    AUTO_CREATE_SCHEMA=true \
    AUTO_SCHEMA_SYNC=true

# ProductionConfig deliberately defaults both schema settings to false: with a
# real DATABASE_URL the schema is owned by migrations, and creating it from
# application code would let a replica race to define it. The SQLite fallback
# has no migration path, so without these the container boots into a usable
# /health and then 500s on every endpoint that touches a table, with no clear
# clue why. Point DATABASE_URL at PostgreSQL, run `flask db upgrade`, and set
# these back to false.

# The healthcheck uses Python's standard library rather than curl. Installing
# curl needed an apt-get round trip, which adds a network dependency to every
# build, pulls an extra package into the image, and fails outright on hosts
# that cannot reach the Debian mirrors. urllib is always present.
#
# Run as an unprivileged user
RUN groupadd --system cloudpulse \
    && useradd --system --gid cloudpulse --create-home --home-dir /home/cloudpulse cloudpulse

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY --chown=cloudpulse:cloudpulse . .

# The application is mounted at the image root, so the SQLite fallback must
# not be placed at the project root as it is in a local checkout. It goes under
# the unprivileged user's home, which is the only guaranteed writable path.
# Mount a volume here in production, or supply DATABASE_URL and skip it.
RUN mkdir -p /home/cloudpulse/instance \
    && chown -R cloudpulse:cloudpulse /home/cloudpulse

VOLUME ["/home/cloudpulse/instance"]

USER cloudpulse

# Render, Heroku, Railway and Cloud Run all assign the listen port at run time
# through PORT and route traffic to whatever it contains. A hardcoded 5000 gets
# the container started and then unreachable, so the default below is only a
# fallback for a plain `docker run -p 5000:5000`.
ENV PORT=5000

# Documentation only; the runtime port comes from $PORT.
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;port=os.getenv('PORT','5000');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=4).status==200 else 1)"]

# Gunicorn runs a single worker with threads so the in-process scheduler
# and the Prometheus counters stay consistent per process.
#
# Shell form rather than exec form, because only the shell expands $PORT.
# Every argument is unquoted for the same reason; none of them contain spaces.
CMD gunicorn \
     --bind 0.0.0.0:${PORT:-5000} \
     --workers 1 \
     --threads 8 \
     --worker-class gthread \
     --timeout 60 \
     --graceful-timeout 30 \
     --keep-alive 5 \
     --access-logfile - \
     --error-logfile - \
     --capture-output \
     app:app
