"""Small deterministic helpers shared by the performance probes.

The GUI and frozen-executable probes deliberately keep wall-clock collection
outside the unit-test suite.  This module contains only the statistics and
gate logic, so their interpretation can still be covered by normal CI tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import median
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class TimingSummary:
    """Robust summary for a non-empty collection of millisecond samples."""

    count: int
    minimum_ms: float
    median_ms: float
    p95_ms: float
    maximum_ms: float
    mad_ms: float

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


def summarize_ms(samples: Iterable[float]) -> TimingSummary:
    """Summarize samples using median, nearest-rank p95 and median deviation.

    Nearest-rank p95 is intentionally used instead of an interpolated
    percentile.  It has a simple, stable definition for the relatively small
    sample sets produced by the desktop benchmarks.
    """

    values = sorted(float(value) for value in samples)
    if not values:
        raise ValueError("at least one timing sample is required")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("timing samples must be finite and non-negative")

    sample_median = float(median(values))
    deviations = [abs(value - sample_median) for value in values]
    p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
    return TimingSummary(
        count=len(values),
        minimum_ms=values[0],
        median_ms=sample_median,
        p95_ms=values[p95_index],
        maximum_ms=values[-1],
        mad_ms=float(median(deviations)),
    )


def check_p95_limits(
    summaries: Mapping[str, TimingSummary],
    limits_ms: Mapping[str, float | None],
) -> list[str]:
    """Return human-readable failures for enabled p95 limits.

    A ``None`` limit means report-only.  Keeping that state explicit lets the
    same benchmark be non-blocking on shared CI runners and blocking on a
    controlled release workstation.
    """

    failures: list[str] = []
    for name, limit in limits_ms.items():
        if limit is None:
            continue
        numeric_limit = float(limit)
        if not math.isfinite(numeric_limit) or numeric_limit <= 0:
            raise ValueError(f"p95 limit for {name!r} must be finite and positive")
        try:
            actual = summaries[name].p95_ms
        except KeyError as error:
            raise KeyError(f"missing timing summary for {name!r}") from error
        if actual > numeric_limit:
            failures.append(
                f"{name} p95 {actual:.1f} ms exceeds {numeric_limit:.1f} ms"
            )
    return failures


def baseline_regression_limit_ms(
    baseline_ms: float,
    *,
    max_regression_percent: float = 25.0,
    minimum_slack_ms: float = 2.0,
) -> float:
    """Return a noise-tolerant upper limit derived from a prior measurement."""

    baseline = float(baseline_ms)
    regression = float(max_regression_percent)
    slack = float(minimum_slack_ms)
    if not math.isfinite(baseline) or baseline < 0:
        raise ValueError("baseline must be finite and non-negative")
    if not math.isfinite(regression) or regression < 0:
        raise ValueError("max regression percent must be finite and non-negative")
    if not math.isfinite(slack) or slack < 0:
        raise ValueError("minimum slack must be finite and non-negative")
    return max(baseline * (1.0 + regression / 100.0), baseline + slack)
