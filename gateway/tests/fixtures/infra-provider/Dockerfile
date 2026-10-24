# TEST FIXTURE (scripts/test-provider-infra-integration.sh): uses what the gateway grants a provider besides
# fixed hosts (wildcards, raw TCP to the host of a setting, folders named by settings) and reports what it could do.
# Also what scripts/dev-catalog.sh is tried on: a provider directory that is not in the providers repository.
FROM ghcr.io/sossiogll/musizeer-provider-base:5

LABEL org.opencontainers.image.description="Test fixture: wildcard egress, raw TCP and folders from settings."

COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider

ENTRYPOINT ["python", "-m", "musizeer_sdk.runner", "--provider", "infra_provider:InfraLyricsProvider"]
