# TEST FIXTURE (gateway/tests/integration): a provider that tries to read what it must not,
# write where it must not and reach what it did not declare, and reports what happened.
FROM ghcr.io/sossiogll/musizeer-provider-base:6

COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider

ENTRYPOINT ["python", "-m", "musizeer_sdk.runner", "--provider", "hostile_provider:HostileLyricsProvider"]
