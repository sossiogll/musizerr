# ------------------------------------------------------------------
# Base — dipendenze Python condivise tra dev e prod
# ------------------------------------------------------------------
FROM python:3.12-slim AS base

# ffmpeg/ffprobe: integrity check, quality probe and tag writing of the host (core/).
# postgresql-client-17: pg_dump/pg_restore, the backup `init` takes before a migration (ADR-0007 §4); the
# client must be of the server's major (the compose files run postgres:17) or pg_dump refuses.
# Nothing a provider needs lives here: providers are separate images run by the gateway.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The provider contract (musizeer-sdk): installed before the application code,
# so development (bind mount of ./sdk, editable) and production both have it.
COPY sdk/ /sdk/
RUN pip install --no-cache-dir -e /sdk

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime user non-root (uid 1000 per compatibilità con volumi host)
RUN useradd -u 1000 -m musizeer

# Where the sockets of the providers the gateway runs are shared (docs/gateway.md): a volume
# mounted here is initialised owned by the application's user, whichever service starts first.
RUN mkdir -p /run/musizeer && chown musizeer:musizeer /run/musizeer


# ------------------------------------------------------------------
# Development — codice montato via bind mount, no COPY
# ------------------------------------------------------------------
FROM base AS development

# Dipendenze di test nello stage di sviluppo, cosi' sopravvivono a ogni
# ricostruzione (prima andavano installate a mano dopo ogni avvio). Mai in
# produzione: lo stage production parte da base, non da qui.
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# In dev si gira come root per evitare problemi di permessi sui volumi Docker
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app", "--reload-dir", "/sdk", "--timeout-graceful-shutdown", "5"]


# ------------------------------------------------------------------
# Production — codice copiato nell'immagine
# ------------------------------------------------------------------
FROM base AS production

# The version this image is (ADR-0007 §2): the images workflow passes the release tag or the build of
# master; a local build is "dev".
ARG MUSIZEER_VERSION=dev
ARG MUSIZEER_COMMIT=
ENV MUSIZEER_VERSION=$MUSIZEER_VERSION MUSIZEER_COMMIT=$MUSIZEER_COMMIT

COPY app/ .

RUN mkdir -p /data && chown musizeer:musizeer /data

USER musizeer

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
