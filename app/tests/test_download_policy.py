"""
The rules of the download approval policy (``core/download_policy.py``): pure,
no database. Every mode of every kind, the boundary of the threshold and of
the quota, the per-user override on top of the general policy, and the
``auto_limit`` the UI reads agreeing with ``decide`` everywhere.
"""
from __future__ import annotations

import pytest

from core import download_policy as dp
from core.settings_catalog import CATALOG, get_def


def _policy(mode: str, *, threshold: int = 10, quota: int = 0, kind: str = dp.KIND_ALBUM) -> dp.Policy:
    rules = {k: dp.KindRule(dp.MODE_AUTO, 1) for k in dp.KINDS}
    rules[kind] = dp.KindRule(mode, threshold)
    return dp.Policy(rules=rules, quota_tracks=quota, quota_days=7)


# ---------------------------------------------------------------------------
# Every mode, every kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", dp.KINDS)
def test_auto_approves_any_size(kind):
    decision = dp.decide(_policy(dp.MODE_AUTO, kind=kind), kind, needed=100_000, used=0)

    assert decision == dp.Decision(True, dp.REASON_AUTO)


@pytest.mark.parametrize("kind", dp.KINDS)
def test_approval_always_waits_even_for_one_track(kind):
    decision = dp.decide(_policy(dp.MODE_APPROVAL, kind=kind), kind, needed=1, used=0)

    assert decision == dp.Decision(False, dp.REASON_KIND_REQUIRES_APPROVAL)


@pytest.mark.parametrize("kind", dp.KINDS)
def test_auto_up_to_approves_at_the_threshold_and_waits_one_past_it(kind):
    policy = _policy(dp.MODE_AUTO_UP_TO, threshold=10, kind=kind)

    assert dp.decide(policy, kind, needed=10, used=0) == dp.Decision(True, dp.REASON_WITHIN_THRESHOLD)
    assert dp.decide(policy, kind, needed=11, used=0) == dp.Decision(False, dp.REASON_OVER_THRESHOLD)
    assert dp.decide(policy, kind, needed=1, used=0).approved


def test_the_kinds_are_judged_by_their_own_rule():
    rules = {
        dp.KIND_TRACK: dp.KindRule(dp.MODE_AUTO, 1),
        dp.KIND_ALBUM: dp.KindRule(dp.MODE_AUTO_UP_TO, 30),
        dp.KIND_PLAYLIST: dp.KindRule(dp.MODE_APPROVAL, 30),
        dp.KIND_ARTIST: dp.KindRule(dp.MODE_APPROVAL, 30),
        dp.KIND_LIKED_ELSEWHERE: dp.KindRule(dp.MODE_AUTO_UP_TO, 50),
    }
    policy = dp.Policy(rules=rules, quota_tracks=0, quota_days=7)

    assert [dp.decide(policy, kind, needed=30, used=0).approved for kind in dp.KINDS] == [True, True, False, False, True]
    assert [dp.decide(policy, kind, needed=31, used=0).approved for kind in dp.KINDS] == [True, False, False, False, True]
    assert [dp.decide(policy, kind, needed=51, used=0).approved for kind in dp.KINDS] == [True, False, False, False, False]


# ---------------------------------------------------------------------------
# The quota
# ---------------------------------------------------------------------------


def test_the_quota_boundary_exactly_fits_and_one_more_waits():
    policy = _policy(dp.MODE_AUTO, quota=100)

    assert dp.decide(policy, dp.KIND_ALBUM, needed=40, used=60).approved
    assert dp.decide(policy, dp.KIND_ALBUM, needed=41, used=60) == dp.Decision(False, dp.REASON_OVER_QUOTA)


def test_the_quota_applies_to_every_automatic_mode():
    for mode in (dp.MODE_AUTO, dp.MODE_AUTO_UP_TO):
        policy = _policy(mode, threshold=1000, quota=10)
        assert dp.decide(policy, dp.KIND_ALBUM, needed=5, used=5).approved
        assert dp.decide(policy, dp.KIND_ALBUM, needed=6, used=5).reason == dp.REASON_OVER_QUOTA


def test_no_quota_means_no_limit():
    assert dp.decide(_policy(dp.MODE_AUTO, quota=0), dp.KIND_ALBUM, needed=10_000, used=10_000).approved


def test_a_waiting_request_names_the_first_rule_it_breaks():
    over_both = _policy(dp.MODE_AUTO_UP_TO, threshold=5, quota=10)
    assert dp.decide(over_both, dp.KIND_ALBUM, needed=20, used=0).reason == dp.REASON_OVER_THRESHOLD
    over_quota_only = _policy(dp.MODE_AUTO_UP_TO, threshold=50, quota=10)
    assert dp.decide(over_quota_only, dp.KIND_ALBUM, needed=20, used=0).reason == dp.REASON_OVER_QUOTA
    approval = _policy(dp.MODE_APPROVAL, quota=10)
    assert dp.decide(approval, dp.KIND_ALBUM, needed=20, used=0).reason == dp.REASON_KIND_REQUIRES_APPROVAL


# ---------------------------------------------------------------------------
# auto_limit: what the UI reads is what decide does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", dp.MODES)
@pytest.mark.parametrize("quota", [0, 12])
@pytest.mark.parametrize("used", [0, 7, 12, 30])
def test_auto_limit_is_exactly_the_largest_request_decide_approves(mode, quota, used):
    policy = _policy(mode, threshold=9, quota=quota)
    limit = dp.auto_limit(policy, dp.KIND_ALBUM, used)

    for needed in range(1, 60):
        approved = dp.decide(policy, dp.KIND_ALBUM, needed, used).approved
        assert approved == (limit is None or needed <= limit), (needed, limit)


def test_auto_limit_none_means_no_limit_and_zero_means_always_waits():
    assert dp.auto_limit(_policy(dp.MODE_AUTO), dp.KIND_ALBUM, used=0) is None
    assert dp.auto_limit(_policy(dp.MODE_APPROVAL), dp.KIND_ALBUM, used=0) == 0
    assert dp.auto_limit(_policy(dp.MODE_AUTO, quota=10), dp.KIND_ALBUM, used=10) == 0


# ---------------------------------------------------------------------------
# The two levels
# ---------------------------------------------------------------------------


def _general() -> dp.Policy:
    return dp.Policy(
        rules={**{k: dp.KindRule(dp.MODE_AUTO, 1) for k in dp.KINDS}, dp.KIND_ALBUM: dp.KindRule(dp.MODE_AUTO_UP_TO, 30)},
        quota_tracks=200, quota_days=7,
    )


def test_no_override_is_the_general_policy():
    assert dp.effective_policy(_general(), None) == _general()
    assert dp.effective_policy(_general(), dp.PolicyOverride()) == _general()


def test_an_override_replaces_what_it_says_and_inherits_the_rest():
    override = dp.PolicyOverride.from_dict({
        "rules": {"album": {"threshold": 5}, "playlist": {"mode": "auto_up_to"}},
        "quota_tracks": 20,
    })

    effective = dp.effective_policy(_general(), override)

    assert effective.rules[dp.KIND_ALBUM] == dp.KindRule(dp.MODE_AUTO_UP_TO, 5)          # threshold only: the mode is inherited
    assert effective.rules[dp.KIND_PLAYLIST] == dp.KindRule(dp.MODE_AUTO_UP_TO, 1)       # mode only: the threshold is inherited
    assert effective.rules[dp.KIND_TRACK] == _general().rules[dp.KIND_TRACK]              # untouched kinds follow the general one
    assert (effective.quota_tracks, effective.quota_days) == (20, 7)


def test_a_quota_of_zero_in_an_override_is_no_quota_for_that_user_and_none_inherits():
    assert dp.effective_policy(_general(), dp.PolicyOverride.from_dict({"quota_tracks": 0})).quota_tracks == 0
    assert dp.effective_policy(_general(), dp.PolicyOverride.from_dict({"quota_tracks": None})).quota_tracks == 200


@pytest.mark.parametrize("data, message", [
    ({"nope": 1}, "unknown field"),
    ({"rules": {"movie": {"mode": "auto"}}}, "unknown kind"),
    ({"rules": {"album": {"mode": "always"}}}, "mode must be one of"),
    ({"rules": {"album": {"threshold": 0}}}, "threshold must be"),
    ({"rules": {"album": {"threshold": True}}}, "threshold must be"),
    ({"rules": {"album": {"cap": 1}}}, "unknown field"),
    ({"quota_tracks": -1}, "quota_tracks must be"),
    ({"quota_tracks": "ten"}, "quota_tracks must be"),
])
def test_an_invalid_override_is_refused_with_a_message(data, message):
    with pytest.raises(ValueError, match=message):
        dp.PolicyOverride.from_dict(data)


def test_an_override_round_trips_and_empty_ones_are_empty():
    override = dp.PolicyOverride.from_dict({"rules": {"album": {"mode": "approval"}}, "quota_tracks": 3})

    assert dp.PolicyOverride.from_dict(override.as_dict()) == override
    assert dp.PolicyOverride.from_dict({}).is_empty
    assert dp.PolicyOverride.from_dict({"rules": {"album": {}}, "quota_tracks": None}).is_empty


# ---------------------------------------------------------------------------
# The catalogue: the general policy is settings
# ---------------------------------------------------------------------------


def test_the_catalogue_declares_one_mode_and_threshold_per_kind_and_the_quota():
    keys = {d.key for d in CATALOG}

    for kind in dp.KINDS:
        assert dp.mode_key(kind) in keys and dp.threshold_key(kind) in keys
    assert {dp.QUOTA_TRACKS_KEY, dp.QUOTA_DAYS_KEY} <= keys


def test_the_defaults_are_the_documented_ones():
    policy = dp.Policy.from_settings(lambda key: get_def(key).default)

    assert {kind: (rule.mode, rule.threshold) for kind, rule in policy.rules.items() if rule.mode != dp.MODE_APPROVAL} == {
        dp.KIND_TRACK: (dp.MODE_AUTO, 1),
        dp.KIND_ALBUM: (dp.MODE_AUTO_UP_TO, 30),
        dp.KIND_LIKED_ELSEWHERE: (dp.MODE_AUTO_UP_TO, 50),
    }
    assert policy.rules[dp.KIND_PLAYLIST].mode == policy.rules[dp.KIND_ARTIST].mode == dp.MODE_APPROVAL
    assert (policy.quota_tracks, policy.quota_days) == (200, 7)


def test_the_settings_are_admin_only_and_translatable():
    for definition in CATALOG:
        if definition.group != "download_requests":
            continue
        public = definition.to_public()
        assert definition.scope == "system" and public["admin_only"] is True
        assert public["label_key"] == f"settings.labels.{definition.key}"
        assert public["description_key"] == f"settings.descriptions.{definition.key}"
        assert definition.label and definition.description


def test_the_mode_settings_are_enums_of_the_modes():
    for kind in dp.KINDS:
        assert get_def(dp.mode_key(kind)).options == dp.MODES
