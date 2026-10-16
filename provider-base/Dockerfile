# The base image of every provider image: Python, the SDK and nothing else.
# The build context is the SDK itself:
#   docker build -f provider-base/Dockerfile -t ghcr.io/sossiogll/musizerr-provider-base:5 sdk
# (scripts/build-provider-image.sh does it for images under test; scripts/publish-provider-base.sh
# publishes it for linux/amd64 and linux/arm64). The tag is the provider API version the SDK implements.
FROM python:3.12-slim

LABEL org.musizerr.provider-base.api-version="5" \
      org.opencontainers.image.description="The base of every Musizerr provider image: Python and the musizerr-sdk." \
      org.opencontainers.image.source="https://github.com/sossiogll/musizerr" \
      org.opencontainers.image.licenses="LGPL-3.0-or-later"

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

COPY . /tmp/sdk
RUN pip install /tmp/sdk && rm -rf /tmp/sdk
