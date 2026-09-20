"""Application expectations decide pass or fail of a run; loose rules would hide real regressions."""

import pytest

from txplatform.scenarios import expectations
from txplatform.scenarios.schema import ApplicationExpectations, CountRule


def _expect(**fields) -> ApplicationExpectations:
    return ApplicationExpectations.model_validate(fields)


@pytest.mark.parametrize(
    ("rule", "count", "total", "passed"),
    [
        ({"all": True}, 5, 5, True),
        ({"all": True}, 4, 5, False),
        ({"all": True}, 0, 0, False),
        ({"exactly": 3}, 3, 9, True),
        ({"exactly": 3}, 2, 9, False),
        ({"min": 2, "max": 4}, 4, 9, True),
        ({"min": 2, "max": 4}, 5, 9, False),
        ({"max": 0}, 0, 9, True),
    ],
)
def test_count_rules(rule, count, total, passed):
    assert expectations.count_matches(CountRule.model_validate(rule), count, total) is passed


def test_a_status_that_is_not_listed_fails_the_run():
    result = expectations.evaluate_statuses(_expect(statuses={201: {"min": 1}}), [201, 201, 500])

    assert not result.passed
    assert "unexpected statuses" in result.detail


def test_statuses_pass_with_a_readable_summary():
    result = expectations.evaluate_statuses(_expect(statuses={201: {"exactly": 2}, 429: {"min": 1}}), [429, 201, 201])

    assert result.passed
    assert result.detail == "201x2, 429x1"


def test_states_are_skipped_when_the_scenario_declares_none():
    assert expectations.evaluate_states(_expect(statuses={401: {"all": True}}), []) is None


def test_compensation_must_run_every_step_successfully_and_in_order():
    rule = _expect(statuses={502: {"all": True}}, compensation=["VoidPayment", "ReleaseInventory"])
    done = {"orderId": "a", "compensation": [{"action": "VoidPayment", "status": "Succeeded"}, {"action": "ReleaseInventory", "status": "Succeeded"}]}
    reversed_order = {"orderId": "b", "compensation": list(reversed(done["compensation"]))}
    failed_step = {"orderId": "c", "compensation": [{"action": "VoidPayment", "status": "Failed"}, {"action": "ReleaseInventory", "status": "Succeeded"}]}

    assert expectations.evaluate_compensation(rule, [done]).passed
    assert not expectations.evaluate_compensation(rule, [done, reversed_order]).passed
    assert not expectations.evaluate_compensation(rule, [failed_step]).passed


def test_compensation_rule_fails_when_no_order_failed_at_all():
    rule = _expect(statuses={502: {"all": True}}, compensation=["ReleaseInventory"])

    assert not expectations.evaluate_compensation(rule, []).passed


def test_a_double_charge_fails_the_authorization_rule():
    rule = _expect(statuses={201: {"all": True}}, authorizationsPerOrder=1)

    result = expectations.evaluate_authorizations(rule, {"order-1": 1, "order-2": 2})

    assert not result.passed
    assert "order-2" in result.detail
