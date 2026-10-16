"""
The exceptions a provider raises to tell the host WHY a call failed, in terms
the host can act on without knowing the provider (the host classifies them:
the routes answer 503 + ``Retry-After``, the workers report a code, the
download resolver moves on or waits).
"""
from __future__ import annotations


class ProviderRateLimited(Exception):
    """The upstream service refused the call for now: a rate limit or an
    exhausted quota, answered with whatever status it uses for it (a quota that
    comes as a 403 must not look like a refused credential, nor an empty
    result). Retrying later can succeed; the host classifies it as
    ``rate_limited``.

    ``retry_after`` (seconds), when the upstream says how long to wait, is the
    ``Retry-After`` the host answers with."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RemotePlaylistGone(Exception):
    """A sync provider raises it from ``update_playlist`` / ``delete_playlist``
    when the playlist it was asked about no longer exists on the server (the
    user deleted it there): the host forgets its id and, for an update,
    creates the playlist again."""


class DownloadDelegated(Exception):
    """
    Sollevata da un DownloadProvider che ha delegato il download a un sistema esterno
    (es. Lidarr) invece di restituire un file direttamente.
    Il resolver la cattura e imposta delegated=True nel risultato.
    """

    def __init__(self, provider: str, details: list | None = None, handle: str | None = None) -> None:
        self.provider = provider
        self.details = details or []
        # Handle opaco per la successiva riconciliazione (interpretato solo dal
        # provider che lo emette, es. id album del sistema esterno).
        self.handle = handle
        super().__init__(f"Download delegated to external system via {provider}")


class DownloadDeclined(Exception):
    """
    Raised by a DownloadProvider that answered, definitively, that it cannot
    provide this track, for a reason worth telling apart from "not found"
    (e.g. the external system already has the release, complete, without
    this track). Not transient: the resolver moves on and, if nobody else
    downloads the track, the failure carries ``code``.
    """

    def __init__(self, provider: str, code: str, message: str) -> None:
        self.provider = provider
        self.code = code
        self.message = message
        super().__init__(f"{provider} declined ({code}): {message}")



class DelegateHoldsRelease(Exception):
    """Raised by ``DownloadProvider.download_fuzzy`` when the external system it
    delegates to ALREADY holds the release the requested track belongs to
    (e.g. the album is already in its library): the provider reports what it
    holds, as data, and does NOT decide what that means for the track.

    The host decides: the requested track is among ``tracks`` (it has a file
    already: the download is delegated, nothing to search), or the release is
    ``complete`` without it (nothing a search would bring: declined), or
    neither (``DownloadProvider.search_delegated`` asks the system to look for
    the release).

    - ``handle``: the handle of the delegation (as in ``DownloadDelegated``).
    - ``tracks``: the tracks of the release that already have a file, as in
      ``DownloadProvider.list_delegated_tracks`` (``album`` empty = the
      delegated release).
    - ``complete``: the release has a file for every one of its tracks.
    - ``release``: the release's title, for the host's messages.
    - ``details``: what ``DownloadDelegated.details`` carries if the host
      delegates (provider vocabulary, opaque to the host).
    - ``state``: JSON-serializable data the provider wants back in
      ``search_delegated`` (opaque to the host).
    """

    def __init__(
        self,
        provider: str,
        handle: str,
        tracks: list,
        *,
        complete: bool,
        release: str,
        details: list | None = None,
        state: dict | None = None,
    ) -> None:
        self.provider = provider
        self.handle = handle
        self.tracks = tracks
        self.complete = complete
        self.release = release
        self.details = details or []
        self.state = state or {}
        super().__init__(f"{provider} already holds '{release}'")
