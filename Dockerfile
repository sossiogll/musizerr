# ------------------------------------------------------------------
# Base — dipendenze Python condivise tra dev e prod
# ------------------------------------------------------------------
FROM python:3.12-slim AS base

# ffmpeg/ffprobe: integrity check, quality probe and tag writing of the host (core/).
# Nothing a provider needs lives here: providers are separate images run by the gateway.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The provider contract (musizerr-sdk): installed before the application code,
# so development (bind mount of ./sdk, editable) and production both have it.
COPY sdk/ /sdk/
RUN pip install --no-cache-dir -e /sdk

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime user non-root (uid 1000 per compatibilità con volumi host)
RUN useradd -u 1000 -m musizerr

# Where the sockets of the providers the gateway runs are shared (docs/gateway.md): a volume
# mounted here is initialised owned by the application's user, whichever service starts first.
RUN mkdir -p /run/musizerr && chown musizerr:musizerr /run/musizerr


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
ARG MUSIZERR_VERSION=dev
ARG MUSIZERR_COMMIT=
ENV MUSIZERR_VERSION=$MUSIZERR_VERSION MUSIZERR_COMMIT=$MUSIZERR_COMMIT

COPY app/ .

RUN mkdir -p /data && chown musizerr:musizerr /data

USER musizerr

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
