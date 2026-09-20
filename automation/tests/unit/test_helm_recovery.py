"""A release left pending by a killed deployment must be recovered, but never while Helm may still be working on it."""

import datetime as dt

import pytest

from txplatform import helm

NOW = dt.datetime(2026, 9, 17, 15, 0, tzinfo=dt.UTC)


def _info(status: str, minutes_ago: float) -> dict:
    # Helm prints local time with nanoseconds, which Python's datetime cannot read directly.
    moment = (NOW - dt.timedelta(minutes=minutes_ago)).astimezone(dt.timezone(dt.timedelta(hours=3)))
    return {"status": status, "last_deployed": moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123+03:00"}


def test_helm_timestamps_with_nanoseconds_are_read_exactly():
    assert helm.parse_helm_time("2026-09-17T17:49:41.4597043+03:00") == dt.datetime(2026, 9, 17, 14, 49, 41, 459704, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("status", "minutes_ago", "action"),
    [
        ("deployed", 90, "proceed"),
        ("failed", 90, "proceed"),
        ("pending-upgrade", 5, "wait"),
        ("pending-install", 19, "wait"),
        ("pending-install", 21, "uninstall"),
        ("pending-upgrade", 21, "rollback"),
        ("pending-rollback", 60, "rollback"),
    ],
)
def test_pending_release_decision(status, minutes_ago, action):
    assert helm.pending_release_action(_info(status, minutes_ago), NOW) == action
