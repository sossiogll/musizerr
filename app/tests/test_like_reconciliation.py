"""Regole di convergenza del reconciler auto-like (pure, provider-agnostiche)."""

from __future__ import annotations

from core import like_reconciliation as autolike
from core.backoff import RETRY_JITTER, retry_delay


def test_no_account_always_skips():
    # Anche se la traccia è disponibile: senza account personale niente like.
    action = autolike.decide_like_action(
        has_own_account=False, sync_id="sid", age_seconds=1, max_age_seconds=100
    )
    assert action == autolike.SKIP_NO_ACCOUNT


def test_resolved_track_is_liked():
    action = autolike.decide_like_action(
        has_own_account=True, sync_id="sid-1", age_seconds=10, max_age_seconds=100
    )
    assert action == autolike.LIKE


def test_unresolved_within_max_age_retries():
    action = autolike.decide_like_action(
        has_own_account=True, sync_id=None, age_seconds=30, max_age_seconds=100
    )
    assert action == autolike.RETRY


def test_unresolved_beyond_max_age_gives_up():
    action = autolike.decide_like_action(
        has_own_account=True, sync_id=None, age_seconds=101, max_age_seconds=100
    )
    assert action == autolike.GIVE_UP


def test_unknown_age_keeps_retrying():
    # Senza età nota non si abbandona mai: si continua a convergere.
    action = autolike.decide_like_action(
        has_own_account=True, sync_id=None, age_seconds=None, max_age_seconds=100
    )
    assert action == autolike.RETRY


def test_retry_delay_doubles_from_the_base_up_to_an_hour():
    delays = [retry_delay(n, 60, 0, autolike.MAX_RETRY_INTERVAL_SECONDS) for n in range(1, 10)]
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]
    assert retry_delay(10_000, 60, 0, autolike.MAX_RETRY_INTERVAL_SECONDS) == autolike.MAX_RETRY_INTERVAL_SECONDS


def test_retry_delay_jitter_only_shortens_the_wait():
    assert retry_delay(1, 60, 0.999, autolike.MAX_RETRY_INTERVAL_SECONDS) > 60 * (1 - RETRY_JITTER) - 0.01
    assert retry_delay(9, 60, 0.5, autolike.MAX_RETRY_INTERVAL_SECONDS) < autolike.MAX_RETRY_INTERVAL_SECONDS
    assert retry_delay(9, 60, 0.5, autolike.MAX_RETRY_INTERVAL_SECONDS) >= autolike.MAX_RETRY_INTERVAL_SECONDS * (1 - RETRY_JITTER)


def test_like_wins_over_max_age():
    # Se è stata trovata, si mette il like anche se il marcatore è vecchio.
    action = autolike.decide_like_action(
        has_own_account=True, sync_id="sid", age_seconds=99999, max_age_seconds=100
    )
    assert action == autolike.LIKE
