# Render looks for a Dockerfile at the repository root before it looks anywhere
# else. Without this one, creating a Web Service from this repository either
# fails to find a Dockerfile or falls back to the native Python builder, which
# has no Procfile or start command and produces a container that never serves.
#
# The real Dockerfile lives at app/Dockerfile next to the code. This forwards to
# it so that a default Render setup, a plain `docker build .`, and the blueprint
# in render.yaml all produce the same image.
#
# The build context has to be ./app because that is where requirements.txt and
# the application package are; everything the image needs is inside it.

# The stage must not be called "build": in a multi-stage build Docker resolves
# `--from=<name>` against a previous stage first and then against an image, and a
# stage called "build" made it try to pull docker.io/library/build:latest.
FROM docker.io/library/python:3.12-slim AS builder
COPY app/requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    PORT=5000 \
    INSTANCE_DIR=/home/cloudpulse/instance \
    AUTO_CREATE_SCHEMA=true \
    AUTO_SCHEMA_SYNC=true

RUN groupadd --system cloudpulse \
    && useradd --system --gid cloudpulse --create-home --home-dir /home/cloudpulse cloudpulse

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=cloudpulse:cloudpulse app/ .
RUN mkdir -p /home/cloudpulse/instance \
    && chown -R cloudpulse:cloudpulse /home/cloudpulse

VOLUME ["/home/cloudpulse/instance"]

USER cloudpulse

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;port=os.getenv('PORT','5000');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=4).status==200 else 1)"]

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