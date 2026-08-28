from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from devtools.benchmarking import (
    TimingSummary,
    baseline_regression_limit_ms,
    check_p95_limits,
    summarize_ms,
)
from devtools.benchmark_ui import SCHEMA_VERSION, _baseline_failures, _load_baseline
from quickaccess.models import LauncherConfig, LauncherItem
from quickaccess.services.monitor import Rect
from quickaccess.ui.popup import PopupPanel


def test_summarize_ms_uses_nearest_rank_p95_and_median_deviation() -> None:
    summary = summarize_ms(range(1, 21))

    assert summary == TimingSummary(
        count=20,
        minimum_ms=1.0,
        median_ms=10.5,
        p95_ms=19.0,
        maximum_ms=20.0,
        mad_ms=5.0,
    )


@pytest.mark.parametrize("samples", [[], [-1.0], [math.inf], [math.nan]])
def test_summarize_ms_rejects_unusable_samples(samples: list[float]) -> None:
    with pytest.raises(ValueError):
        summarize_ms(samples)


def test_p95_gate_is_report_only_when_limit_is_disabled() -> None:
    summary = summarize_ms([5.0, 10.0, 15.0])

    assert check_p95_limits({"warm": summary}, {"warm": None}) == []
    assert check_p95_limits({"warm": summary}, {"warm": 20.0}) == []
    assert check_p95_limits({"warm": summary}, {"warm": 10.0}) == [
        "warm p95 15.0 ms exceeds 10.0 ms"
    ]


def test_popup_prepare_skips_render_for_an_unchanged_signature() -> None:
    class PreparedPopupHarness:
        _render_signature = None
        _dynamic_signature = None
        _card_dynamic_states: dict[str, tuple[object, ...]] = {}
        _scroll_canvas = None
        _items_frame = None
        _ensure_content = PopupPanel._ensure_content
        _content_signature = staticmethod(PopupPanel._content_signature)
        _dynamic_content_signature = staticmethod(
            PopupPanel._dynamic_content_signature
        )
        _dynamic_item_state = staticmethod(PopupPanel._dynamic_item_state)
        _synchronize_dpi_scale = PopupPanel._synchronize_dpi_scale

        def __init__(self) -> None:
            self.render_count = 0

        @staticmethod
        def _get_window_scaling() -> float:
            return 1.0

        def _render_content(self, *_args: object, **_kwargs: object) -> None:
            self.render_count += 1

        @staticmethod
        def _apply_layout(*_args: object, **_kwargs: object) -> bool:
            return False

        @staticmethod
        def _update_dynamic_content(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def _apply_layout(**_kwargs: object) -> bool:
            return False

        @staticmethod
        def update_idletasks() -> None:
            return None

        @staticmethod
        def _prime_warm_mapping() -> None:
            return None

    config = LauncherConfig(
        columns=3,
        items=[
            LauncherItem(
                name="문서",
                path=r"C:\QuickAccess\Document.pdf",
                type="file",
                order=0,
            )
        ],
    )
    work_area = Rect(0, 0, 1920, 1040)
    popup = PreparedPopupHarness()

    PopupPanel.prepare(popup, config, {}, work_area, icons={})  # type: ignore[arg-type]
    PopupPanel.prepare(popup, config, {}, work_area, icons={})  # type: ignore[arg-type]
    assert popup.render_count == 1

    config.items[0].name = "변경된 문서"
    PopupPanel.prepare(popup, config, {}, work_area, icons={})  # type: ignore[arg-type]
    assert popup.render_count == 2


def test_baseline_limit_has_percentage_and_absolute_noise_floor() -> None:
    assert baseline_regression_limit_ms(100.0) == 125.0
    assert baseline_regression_limit_ms(4.0) == 6.0
    assert (
        baseline_regression_limit_ms(
            1000.0,
            max_regression_percent=10.0,
            minimum_slack_ms=150.0,
        )
        == 1150.0
    )


def test_popup_schema_3_baseline_uses_idle_name_and_excludes_e2e_visibility() -> None:
    assert SCHEMA_VERSION == 3
    current = {
        "metrics": {
            "popup_warm_show_call_ms": {"p95_ms": 2.0},
            "popup_warm_idle_complete_ms": {"p95_ms": 10.0},
            "open_panel_command_to_visible_ms": {"p95_ms": 10_000.0},
        }
    }
    baseline = {
        "metrics": {
            "popup_warm_show_call_ms": {"p95_ms": 2.0},
            "popup_warm_idle_complete_ms": {"p95_ms": 2.0},
            "open_panel_command_to_visible_ms": {"p95_ms": 1.0},
        }
    }

    failures = _baseline_failures(
        current,
        baseline,
        max_regression_percent=25.0,
        minimum_slack_ms=2.0,
    )

    assert len(failures) == 1
    assert failures[0].startswith("popup_warm_idle_complete_ms p95")


def test_previous_popup_baseline_schema_is_rejected() -> None:
    previous_schema = json.dumps(
        {
            "schema_version": 2,
            "benchmark": "quickaccess-popup-warm-path",
        }
    )
    with (
        patch.object(Path, "read_text", return_value=previous_schema),
        pytest.raises(ValueError, match="schema version"),
    ):
        _load_baseline(Path("unused-schema-2.json"), {})


@pytest.mark.parametrize(
    ("baseline", "regression", "slack"),
    [(-1.0, 25.0, 2.0), (1.0, -1.0, 2.0), (1.0, 25.0, -1.0)],
)
def test_baseline_limit_rejects_invalid_inputs(
    baseline: float, regression: float, slack: float
) -> None:
    with pytest.raises(ValueError):
        baseline_regression_limit_ms(
            baseline,
            max_regression_percent=regression,
            minimum_slack_ms=slack,
        )
