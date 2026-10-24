# Push callback of a delegated download

A download provider that hands a download to an external system (for example a library manager that fetches albums)
can ask that system to tell Musizeer when it is done, instead of waiting for the next poll. The pieces:

- the inbound route `POST /api/delegation/webhook/{provider}?token=...` (`routes/delegation_webhook.py`), open to the
  network but guarded by the shared token (`delegation_webhook_token`, a secret setting). The payload is untrusted
  data: the provider reads only the fields it expects (`DownloadProvider.delegation_event_handle`);
- the registration, `POST /api/delegation/register-webhook` (Settings › Reconciliation › "Register push callback",
  admin only), which asks every download provider to register that route on its external system
  (`DownloadProvider.register_push_endpoint`).

## The callback address must be reachable from the external system

The address Musizeer gives to the external system is `public_base_url` + `/api/delegation/webhook/{provider}?token=...`.
It is the external system that calls it, so it must resolve and answer **from where the external system runs**, not
from your browser. A name that works on your laptop (`localhost`, a LAN name only your machine resolves) usually does
not work for a container.

When both run in the same Docker network, use an internal URL: the service name of the web container of this stack
(`frontend` in `docker-compose.yaml`, which proxies `/api/` to the app), for example `http://frontend` (port 80 in the
network). Put it in `public_base_url`. If the external system runs elsewhere, use an address it can reach (a VPN
address, a reverse proxy's hostname) and make sure `/api/delegation/webhook/` is open to it.

## What the registration reports

One result per download provider:

| `state` | Meaning |
| --- | --- |
| `registered` | The external system accepted the callback (`detail` is the provider's outcome word: `created`, `updated`...). |
| `not_supported` | The provider does not implement `register_push_endpoint` (its manifest does not list it). It is the only case in which Musizeer says so. |
| `failed` | The provider implements it but it did not work. `error_code` classifies it (`download_provider_unreachable`, `_timeout`, `_auth_failed`, `_upstream_error`, `_failed`, or `registration_not_confirmed` when the provider answered with neither an outcome nor a reason) and `error_detail` has the provider's own reason, as the external system gave it. |

The usual `failed` is the external system refusing a callback address it cannot reach: it tests the address when it
saves it and answers with a validation message (for example "Unable to send test message"). Fix `public_base_url` as
described above and register again.

The callback token is never in a result, in a log or in a message: it is hidden wherever a provider quotes the URL.

## For provider authors

`register_push_endpoint(callback_url, session)` (`musizeer_sdk.interfaces.DownloadProvider`):

- do not override it if the external system has no push registration: that is what "not supported" is;
- return a short outcome word (`"created"`, `"updated"`) when the external system accepted the callback;
- **raise** when it refused or could not be reached, with the external system's own reason in the message (an
  exception that carries `response.status_code` is classified by the status; a network error as unreachable). Never
  return `None` to swallow a refusal: the administrator would not learn why;
- never put the token (or any secret) in the message: `callback_url` carries it, so hide it in anything you quote.
