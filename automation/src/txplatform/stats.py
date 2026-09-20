"""Statistical tolerance for probabilistic sampling checks.

A random sample never retains exactly its target share, so checks compare the observed count with
a band around the expected count. The band width is chosen from the binomial standard deviation so
that a correct configuration fails the check only with a stated, tiny probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    low: int
    high: int
    expected: float

    def contains(self, value: int) -> bool:
        return self.low <= value <= self.high


def binomial_band(trials: int, probability: float, sigmas: float = 4.5) -> Band:
    """Range of successes expected from `trials` independent draws at `probability`.

    4.5 standard deviations leave roughly a 1-in-150,000 chance of a false failure.
    """
    if trials <= 0 or not 0 <= probability <= 1:
        raise ValueError("trials must be positive and probability between 0 and 1")
    expected = trials * probability
    spread = sigmas * math.sqrt(trials * probability * (1 - probability))
    return Band(max(0, math.floor(expected - spread)), min(trials, math.ceil(expected + spread)), expected)
