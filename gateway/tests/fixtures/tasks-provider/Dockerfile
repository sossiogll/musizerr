# TEST FIXTURE (scripts/test-gateway-tasks-integration.sh): starts as many threads as it is told, to see what bounds the
# processes and threads of a sandbox, and reports how many it got.
FROM ghcr.io/sossiogll/musizeer-provider-base:5

LABEL org.opencontainers.image.description="Test fixture: starts threads on request."

COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider

ENTRYPOINT ["python", "-m", "musizeer_sdk.runner", "--provider", "tasks_provider:TasksLyricsProvider"]
