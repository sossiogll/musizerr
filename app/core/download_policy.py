"""
The approval policy of download requests: pure rules, no I/O.

A request to download something is one of five ``KINDS``. For each kind the
admin picks a ``mode``:

- ``auto``: approved at once;
- ``approval``: always waits for an admin;
- ``auto_up_to``: approved at once when it needs at most ``threshold`` tracks
  downloaded, otherwise it waits.

On top of the modes a user has a ``quota``: the tracks their approved requests
may queue in a rolling window of ``quota_days``. A request that would pass it
waits, whatever its mode. The count a rule looks at is always the tracks that
NEED a download (the host excludes what is in the library and what already
has an open download: ``services/download_requests.py``).

Two levels: the general ``Policy`` (the admin's settings) and a per-user
``PolicyOverride`` (a mode, a threshold or the quota for one user, set by an
admin; whatever it does not say is inherited). ``effective_policy`` merges
them. Admins, and an instance with authentication off, are never subject to
the policy: that is decided by the host, not here.

Provider-agnostic: nothing here knows a provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

KIND_TRACK = "track"
KIND_ALBUM = "album"
KIND_PLAYLIST = "playlist"
KIND_ARTIST = "artist"
# The user-initiated "download the ones liked elsewhere" library job.
KIND_LIKED_ELSEWHERE = "liked_elsewhere"
KINDS = (KIND_TRACK, KIND_ALBUM, KIND_PLAYLIST, KIND_ARTIST, KIND_LIKED_ELSEWHERE)

MODE_AUTO = "auto"
MODE_APPROVAL = "approval"
MODE_AUTO_UP_TO = "auto_up_to"
MODES = (MODE_AUTO, MODE_APPROVAL, MODE_AUTO_UP_TO)

# Why a request was approved at once...
REASON_AUTO = "auto"
REASON_WITHIN_THRESHOLD = "within_threshold"
# ...and why it waits.
REASON_KIND_REQUIRES_APPROVAL = "kind_requires_approval"
REASON_OVER_THRESHOLD = "over_threshold"
REASON_OVER_QUOTA = "over_quota"
REASONS = (
    REASON_AUTO, REASON_WITHIN_THRESHOLD,
    REASON_KIND_REQUIRES_APPROVAL, REASON_OVER_THRESHOLD, REASON_OVER_QUOTA,
)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED, STATUS_CANCELLED)


@dataclass(frozen=True)
class KindRule:
    mode: str
    # Only read by ``auto_up_to``: the most tracks that are approved at once.
    threshold: int


# The defaults of the catalogue (``core/settings_catalog.py`` seeds its
# settings from them). A single track is cheap: automatic. An album is one
# release: automatic up to a long one (30 tracks covers every normal album and
# stops a box set). A playlist or a whole discography is the request that
# fills a disk: it waits. "The ones liked elsewhere" is a batch the user
# built over time: automatic up to a medium one (50).
DEFAULT_RULES: dict[str, KindRule] = {
    KIND_TRACK: KindRule(MODE_AUTO, 1),
    KIND_ALBUM: KindRule(MODE_AUTO_UP_TO, 30),
    KIND_PLAYLIST: KindRule(MODE_APPROVAL, 30),
    KIND_ARTIST: KindRule(MODE_APPROVAL, 30),
    KIND_LIKED_ELSEWHERE: KindRule(MODE_AUTO_UP_TO, 50),
}
# Tracks a user's approved requests may queue per window (0 = no quota): about
# twenty albums a week, beyond which the owner looks before the disk fills.
DEFAULT_QUOTA_TRACKS = 200
DEFAULT_QUOTA_DAYS = 7


def mode_key(kind: str) -> str:
    """The key of the setting that holds the mode of ``kind``."""
    return f"download_request_{kind}_mode"


def threshold_key(kind: str) -> str:
    """The key of the setting that holds the threshold of ``kind``."""
    return f"download_request_{kind}_threshold"


QUOTA_TRACKS_KEY = "download_request_quota_tracks"
QUOTA_DAYS_KEY = "download_request_quota_days"


@dataclass(frozen=True)
class Policy:
    """The rules in force for one requester."""

    rules: Mapping[str, KindRule]
    # Tracks per window; 0 = no quota.
    quota_tracks: int
    quota_days: int

    @classmethod
    def from_settings(cls, get: Any) -> "Policy":
        """The general policy from a settings reader (``ConfigManager.get``)."""
        return cls(
            rules={
                kind: KindRule(mode=str(get(mode_key(kind))), threshold=int(get(threshold_key(kind))))
                for kind in KINDS
            },
            quota_tracks=max(0, int(get(QUOTA_TRACKS_KEY) or 0)),
            quota_days=max(1, int(get(QUOTA_DAYS_KEY) or DEFAULT_QUOTA_DAYS)),
        )


@dataclass(frozen=True)
class RuleOverride:
    """What an admin changed for one user on one kind: ``None`` inherits."""

    mode: str | None = None
    threshold: int | None = None


@dataclass(frozen=True)
class PolicyOverride:
    """What an admin changed for one user. ``quota_tracks`` ``None`` inherits
    the general quota; ``0`` is "no quota for this user"."""

    rules: Mapping[str, RuleOverride] = field(default_factory=dict)
    quota_tracks: int | None = None

    @property
    def is_empty(self) -> bool:
        return self.quota_tracks is None and not self.rules

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "PolicyOverride":
        """Validates and builds an override from its JSON form
        (``{"rules": {kind: {"mode", "threshold"}}, "quota_tracks"}``); every
        field is optional and a ``null`` inherits. ``ValueError`` (with a
        readable message) for an unknown kind or mode, or a number below its
        floor."""
        data = data or {}
        unknown = sorted(set(data) - {"rules", "quota_tracks"})
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(unknown)}")
        rules: dict[str, RuleOverride] = {}
        for kind, raw in (data.get("rules") or {}).items():
            if kind not in KINDS:
                raise ValueError(f"unknown kind '{kind}'")
            raw = raw or {}
            extra = sorted(set(raw) - {"mode", "threshold"})
            if extra:
                raise ValueError(f"{kind}: unknown field(s): {', '.join(extra)}")
            mode, threshold = raw.get("mode"), raw.get("threshold")
            if mode is not None and mode not in MODES:
                raise ValueError(f"{kind}: mode must be one of {', '.join(MODES)}")
            if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1):
                raise ValueError(f"{kind}: threshold must be a whole number of at least 1")
            if mode is not None or threshold is not None:
                rules[kind] = RuleOverride(mode=mode, threshold=threshold)
        quota = data.get("quota_tracks")
        if quota is not None and (isinstance(quota, bool) or not isinstance(quota, int) or quota < 0):
            raise ValueError("quota_tracks must be a whole number, 0 or more")
        return cls(rules=rules, quota_tracks=quota)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules": {
                kind: {"mode": rule.mode, "threshold": rule.threshold} for kind, rule in self.rules.items()
            },
            "quota_tracks": self.quota_tracks,
        }


def effective_policy(general: Policy, override: PolicyOverride | None) -> Policy:
    """``general`` with the user's override on top: a mode or threshold the
    override sets replaces the general one of that kind, the rest is
    inherited."""
    if override is None or override.is_empty:
        return general
    rules = dict(general.rules)
    for kind, change in override.rules.items():
        base = rules[kind]
        rules[kind] = KindRule(
            mode=change.mode if change.mode is not None else base.mode,
            threshold=change.threshold if change.threshold is not None else base.threshold,
        )
    return Policy(
        rules=rules,
        quota_tracks=general.quota_tracks if override.quota_tracks is None else override.quota_tracks,
        quota_days=general.quota_days,
    )


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str


def decide(policy: Policy, kind: str, needed: int, used: int) -> Decision:
    """Whether a request that needs ``needed`` tracks downloaded (never 0:
    a request that needs nothing is not subject to the policy) is approved at
    once, given the ``used`` tracks of the user's approved requests in the
    window. The reason of a request that waits is the first rule it breaks:
    the kind asks for approval, then the threshold, then the quota."""
    rule = policy.rules[kind]
    if rule.mode == MODE_APPROVAL:
        return Decision(False, REASON_KIND_REQUIRES_APPROVAL)
    if rule.mode == MODE_AUTO_UP_TO and needed > rule.threshold:
        return Decision(False, REASON_OVER_THRESHOLD)
    if policy.quota_tracks > 0 and used + needed > policy.quota_tracks:
        return Decision(False, REASON_OVER_QUOTA)
    return Decision(True, REASON_AUTO if rule.mode == MODE_AUTO else REASON_WITHIN_THRESHOLD)


def auto_limit(policy: Policy, kind: str, used: int) -> int | None:
    """The most tracks a request of ``kind`` can need and still be approved
    at once right now (``decide`` approves exactly the requests at or under
    it): ``None`` = no limit, ``0`` = it always waits. What the UI shows to
    label a download button honestly."""
    rule = policy.rules[kind]
    if rule.mode == MODE_APPROVAL:
        return 0
    limit: int | None = rule.threshold if rule.mode == MODE_AUTO_UP_TO else None
    if policy.quota_tracks > 0:
        remaining = max(0, policy.quota_tracks - used)
        limit = remaining if limit is None else min(limit, remaining)
    return limit
