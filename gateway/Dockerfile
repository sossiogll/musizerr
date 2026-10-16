# The provider gateway. Build context: the root of the repository.
#   docker build -f gateway/Dockerfile --target production -t musizerr-gateway .
#
# bubblewrap makes the sandboxes; util-linux (prlimit) sets their resource limits. The
# container runs as an unprivileged user: it needs the seccomp profile and the
# unconfined /proc described in docs/gateway.md, and no capability.
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap util-linux \
    && rm -rf /var/lib/apt/lists/*

# Same uid as the application's user, so the sockets and workspaces they share need
# no widening of permissions.
RUN useradd -u 1000 -m musizerr \
    && mkdir -p /var/lib/musizerr-gateway /run/musizerr /downloads \
    && chown 1000:1000 /var/lib/musizerr-gateway /run/musizerr /downloads

ENV PYTHONUNBUFFERED=1


FROM base AS development
# The code is bind-mounted (./sdk, ./gateway): installed editable once, edited live.
COPY sdk /sdk
COPY gateway /gateway
RUN pip install --no-cache-dir -e /sdk -e /gateway
USER musizerr
CMD ["python", "-m", "musizerr_gateway"]


FROM base AS production
# The version this image is (ADR-0007 §2), shown in the gateway's health.
ARG MUSIZERR_VERSION=dev
ARG MUSIZERR_COMMIT=
ENV MUSIZERR_VERSION=$MUSIZERR_VERSION MUSIZERR_COMMIT=$MUSIZERR_COMMIT
COPY sdk /tmp/sdk
COPY gateway /tmp/gateway
RUN pip install --no-cache-dir /tmp/sdk /tmp/gateway && rm -rf /tmp/sdk /tmp/gateway
USER musizerr
CMD ["python", "-m", "musizerr_gateway"]
