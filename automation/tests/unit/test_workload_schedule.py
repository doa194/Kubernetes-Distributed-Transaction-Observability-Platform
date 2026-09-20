"""Open-loop schedules must plan exactly the requested traffic, independent of how fast the system answers."""

import base64
import json

import pytest

from txplatform.scenarios.schema import WorkloadStep
from txplatform.scenarios.workload import schedule, tamper


def _step(**fields) -> WorkloadStep:
    return WorkloadStep.model_validate(fields)


def test_burst_and_concurrent_steps_start_every_request_at_once():
    assert schedule(_step(profile="burst", requests=4)) == [0.0, 0.0, 0.0, 0.0]
    assert schedule(_step(profile="concurrent", requests=3, concurrency=1)) == [0.0, 0.0, 0.0]


def test_steady_step_spaces_requests_evenly():
    offsets = schedule(_step(profile="steady", ratePerSecond=4, durationSeconds=2))

    assert offsets == [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75]


def test_spike_step_raises_the_rate_only_in_the_middle_window():
    offsets = schedule(_step(profile="spike", ratePerSecond=2, durationSeconds=10, spikeRatePerSecond=10, spikeSeconds=2))

    in_spike = [o for o in offsets if 4.0 <= o < 6.0]
    before = [o for o in offsets if o < 4.0]
    after = [o for o in offsets if o >= 6.0]
    assert (len(before), len(in_spike), len(after)) == (8, 20, 8)
    assert offsets == sorted(offsets)


def test_tampered_token_keeps_its_structure_but_not_its_signature():
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    token = f"{header}.payload.signatureA"

    forged = tamper(token)

    assert forged.split(".")[:2] == token.split(".")[:2]
    assert forged != token
    assert len(forged) == len(token)


@pytest.mark.parametrize("last_char", ["A", "x"])
def test_tampering_always_changes_the_last_signature_character(last_char):
    assert tamper(f"h.p.sig{last_char}")[-1] != last_char
