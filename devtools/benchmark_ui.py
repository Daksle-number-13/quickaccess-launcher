"""Measure the prepared popup's repeat-show path on an interactive desktop.

This is a release-workstation benchmark, not a shared-runner CI test. It uses
multiple samples, reports robust statistics, and only enforces a wall-clock
limit when the caller explicitly supplies one.
"""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import sys
from time import perf_counter, sleep
from typing import Any, Callable

import customtkinter as ctk
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from devtools.benchmarking import (  # noqa: E402
    TimingSummary,
    baseline_regression_limit_ms,
    check_p95_limits,
    summarize_ms,
)
from quickaccess.app import PUMP_INTERVAL_MS, QuickAccessApp  # noqa: E402
from quickaccess.commands import CommandBus, CommandSource, OpenPanelCommand  # noqa: E402
from quickaccess.models import LauncherConfig, LauncherItem  # noqa: E402
from quickaccess.platform import enable_dpi_awareness  # noqa: E402
from quickaccess.services.monitor import MonitorContext, Point, Rect  # noqa: E402
from quickaccess.ui.popup import PopupActions, PopupPanel  # noqa: E402


SCHEMA_VERSION = 3
BENCHMARK_NAME = "quickaccess-popup-warm-path"
DEFAULT_ITEMS = 20
DEFAULT_COLUMNS = 3
DEFAULT_WARMUPS = 5
DEFAULT_ITERATIONS = 30
DEFAULT_E2E_WARMUPS = 2
DEFAULT_E2E_ITERATIONS = 10
DEFAULT_WORK_AREA = Rect(0, 0, 1920, 1040)


class _NoRescheduleRoot:
    """Forward root operations while suppressing the pump's next cycle."""

    def __init__(self, root: ctk.CTk) -> None:
        self._root = root

    @staticmethod
    def after(_delay_ms: int, _callback: Callable[[], None]) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._root, name)


class _FixedMonitor:
    def __init__(self, anchor: Point, work_area: Rect, scale: float) -> None:
        self._anchor = anchor
        self._work_area = work_area
        self._scale = scale

    def get_cursor_position(self) -> Point:
        return self._anchor

    def get_monitor_work_area(self, _anchor: Point) -> Rect:
        return self._work_area

    def get_monitor_context(self, _anchor: Point) -> MonitorContext:
        return MonitorContext(
            identifier="benchmark-display",
            bounds=self._work_area,
            work_area=self._work_area,
            scale=self._scale,
        )


class _NoopToast:
    @staticmethod
    def show(*_args: object, **_kwargs: object) -> None:
        return None


class _CommandPumpHarness:
    """Minimum state needed to exercise the real app command-dispatch path."""

    def __init__(
        self,
        root: ctk.CTk,
        popup: PopupPanel,
        config: LauncherConfig,
        anchor: Point,
        work_area: Rect,
        dpi_scale: float,
    ) -> None:
        self.root = _NoRescheduleRoot(root)
        self.popup = popup
        self.config = config
        self.statuses: dict[str, object] = {}
        self.icon_images: dict[str, ctk.CTkImage] = {}
        self.monitor = _FixedMonitor(anchor, work_area, dpi_scale)
        self.toast = _NoopToast()
        self.bus = CommandBus()
        self._stopping = False
        self._last_anchor: Point | None = None
        self._last_work_area: Rect | None = None
        self._last_monitor_context = None

    def _handle_command(self, command: object) -> None:
        QuickAccessApp._handle_command(self, command)  # type: ignore[arg-type]

    def _drain_commands(self) -> None:
        QuickAccessApp._drain_commands(self)  # type: ignore[arg-type]

    @staticmethod
    def _drain_instance_requests() -> None:
        return None

    def open_panel(self, cursor_position: tuple[int, int] | None = None) -> None:
        QuickAccessApp.open_panel(self, cursor_position)  # type: ignore[arg-type]

    def _monitor_context_at(self, anchor: Point):
        return QuickAccessApp._monitor_context_at(self, anchor)  # type: ignore[arg-type]

    def _ensure_popup(self, _monitor_context: object | None = None) -> PopupPanel:
        return self.popup

    @staticmethod
    def _schedule_icon_requests() -> None:
        # Icon extraction is intentionally outside this UI latency benchmark.
        return None


def sample_config(
    item_count: int = DEFAULT_ITEMS, columns: int = DEFAULT_COLUMNS
) -> LauncherConfig:
    return LauncherConfig(
        columns=columns,
        items=[
            LauncherItem(
                name=f"테스트 항목 {index + 1}",
                path=rf"C:\QuickAccess\Item-{index + 1}",
                type="folder",
                order=index,
            )
            for index in range(item_count)
        ],
    )


def elapsed_ms(callback: Callable[[], None]) -> float:
    start = perf_counter()
    callback()
    return (perf_counter() - start) * 1000.0


def _summary_dict(summary: TimingSummary) -> dict[str, int | float]:
    return summary.as_dict()


def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    enable_dpi_awareness()
    ctk.set_appearance_mode(args.appearance)
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.withdraw()
    root.update_idletasks()

    config = sample_config(args.items, args.columns)
    anchor = Point(100, 100)
    work_area = DEFAULT_WORK_AREA
    popup: PopupPanel | None = None

    try:
        def construct_popup() -> None:
            nonlocal popup
            popup = PopupPanel(
                root,
                PopupActions(
                    activate=lambda _item: None,
                    relocate=lambda _item: None,
                    open_settings=lambda: None,
                ),
            )

        construct_ms = elapsed_ms(construct_popup)
        assert popup is not None
        dpi_scale = float(ScalingTracker.get_window_dpi_scaling(popup))
        prepare_ms = elapsed_ms(
            lambda: popup.prepare(
                config,
                {},
                work_area,
                icons={},
                target_dpi_scale=dpi_scale,
            )
        )
        # Resolve geometry and widget creation outside the measured warm path.
        root.update_idletasks()
        children = popup.winfo_children()
        if not children:
            raise RuntimeError("popup prepare did not create a render tree")
        prepared_shell = children[0]
        prepared_render_count = popup.render_count
        prepared_dynamic_update_count = popup.dynamic_update_count

        def show_cycle() -> tuple[float, float]:
            started = perf_counter()
            popup.show(
                config,
                {},
                anchor,
                work_area,
                icons={},
                target_dpi_scale=dpi_scale,
            )
            call_ms = (perf_counter() - started) * 1000.0
            root.update_idletasks()
            idle_complete_ms = (perf_counter() - started) * 1000.0
            current_children = popup.winfo_children()
            if (
                not current_children
                or current_children[0] is not prepared_shell
                or popup.render_count != prepared_render_count
            ):
                raise RuntimeError(
                    "unchanged popup rebuilt its render tree on the warm path"
                )
            popup.hide()
            root.update_idletasks()
            return call_ms, idle_complete_ms

        for _ in range(args.warmups):
            show_cycle()

        call_samples: list[float] = []
        idle_complete_samples: list[float] = []
        for _ in range(args.iterations):
            call_ms, idle_complete_ms = show_cycle()
            call_samples.append(call_ms)
            idle_complete_samples.append(idle_complete_ms)

        call_summary = summarize_ms(call_samples)
        idle_complete_summary = summarize_ms(idle_complete_samples)

        pump_harness = _CommandPumpHarness(
            root,
            popup,
            config,
            anchor,
            work_area,
            dpi_scale,
        )
        active_started: float | None = None
        visible_at: float | None = None

        # Drain notifications left by the direct warm-path samples before
        # associating visibility signals with command-pump iterations.
        popup.hide()
        root.update()

        def record_visible(_event: object) -> None:
            nonlocal visible_at
            if active_started is not None and visible_at is None:
                visible_at = perf_counter()

        popup.bind("<<QuickAccessVisible>>", record_visible, add="+")

        def command_to_visible_cycle() -> float:
            nonlocal active_started, visible_at
            if popup.visible:
                popup.hide()
            root.update()
            visible_at = None
            active_started = perf_counter()
            pump_harness.bus.publish(
                OpenPanelCommand(
                    source=CommandSource.HOTKEY,
                    cursor_position=(anchor.x, anchor.y),
                )
            )
            root.after(
                PUMP_INTERVAL_MS,
                pump_harness._drain_commands,
            )
            deadline = active_started + args.e2e_timeout
            while visible_at is None and perf_counter() < deadline:
                root.update()
                if visible_at is None:
                    sleep(0.0005)
            if visible_at is None:
                active_started = None
                popup.hide()
                raise TimeoutError(
                    "OpenPanelCommand did not expose the popup "
                    f"within {args.e2e_timeout:.1f}s"
                )
            elapsed = (visible_at - active_started) * 1000.0
            active_started = None
            root.update_idletasks()
            popup.hide()
            # Process the matching cloak before the next sample.
            root.update()
            return elapsed

        for _ in range(args.e2e_warmups):
            command_to_visible_cycle()
        command_to_visible_samples = [
            command_to_visible_cycle() for _ in range(args.e2e_iterations)
        ]
        command_to_visible_summary = summarize_ms(command_to_visible_samples)
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "environment": {
                "host": platform.node(),
                "platform": platform.platform(),
                "processor": platform.processor(),
                "python": platform.python_version(),
            },
            "parameters": {
                "appearance": args.appearance,
                "items": args.items,
                "columns": args.columns,
                "work_area": [
                    work_area.left,
                    work_area.top,
                    work_area.right,
                    work_area.bottom,
                ],
                "dpi_scale": round(dpi_scale, 4),
                "warmups": args.warmups,
                "iterations": args.iterations,
                "e2e_warmups": args.e2e_warmups,
                "e2e_iterations": args.e2e_iterations,
                "e2e_event": "<<QuickAccessVisible>>",
                "command_pump_interval_ms": PUMP_INTERVAL_MS,
            },
            "observations": {
                "popup_construct_ms": round(construct_ms, 3),
                "popup_cold_prepare_ms": round(prepare_ms, 3),
                "warm_render_tree_rebuilds": (
                    popup.render_count - prepared_render_count
                ),
                "warm_dynamic_updates": (
                    popup.dynamic_update_count - prepared_dynamic_update_count
                ),
                "warm_mapping_enabled": popup.warm_mapping_enabled,
            },
            "metrics": {
                "popup_warm_show_call_ms": _summary_dict(call_summary),
                "popup_warm_idle_complete_ms": _summary_dict(
                    idle_complete_summary
                ),
                "open_panel_command_to_visible_ms": _summary_dict(
                    command_to_visible_summary
                ),
            },
        }
    finally:
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
        root.destroy()


def _load_baseline(path: Path, current: dict[str, Any]) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("baseline schema version does not match this benchmark")
    if baseline.get("benchmark") != BENCHMARK_NAME:
        raise ValueError("baseline was produced by a different benchmark")

    comparable_parameters = ("appearance", "items", "columns", "work_area")
    for name in comparable_parameters:
        if baseline.get("parameters", {}).get(name) != current["parameters"][name]:
            raise ValueError(f"baseline parameter {name!r} does not match")
    if baseline.get("environment", {}).get("host") != current["environment"]["host"]:
        raise ValueError("baseline host does not match; compare on the same workstation")
    if baseline.get("environment", {}).get("python") != current["environment"]["python"]:
        raise ValueError("baseline Python version does not match")
    return baseline


def _baseline_failures(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    max_regression_percent: float,
    minimum_slack_ms: float,
) -> list[str]:
    failures: list[str] = []
    for name in ("popup_warm_show_call_ms", "popup_warm_idle_complete_ms"):
        current_p95 = float(current["metrics"][name]["p95_ms"])
        baseline_p95 = float(baseline["metrics"][name]["p95_ms"])
        limit = baseline_regression_limit_ms(
            baseline_p95,
            max_regression_percent=max_regression_percent,
            minimum_slack_ms=minimum_slack_ms,
        )
        if current_p95 > limit:
            failures.append(
                f"{name} p95 {current_p95:.1f} ms exceeds same-host baseline "
                f"limit {limit:.1f} ms (baseline {baseline_p95:.1f} ms)"
            )
    return failures


def _write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the already-prepared popup repeat-show path."
    )
    parser.add_argument("--items", type=int, default=DEFAULT_ITEMS)
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--e2e-warmups", type=int, default=DEFAULT_E2E_WARMUPS)
    parser.add_argument("--e2e-iterations", type=int, default=DEFAULT_E2E_ITERATIONS)
    parser.add_argument("--e2e-timeout", type=float, default=2.0)
    parser.add_argument(
        "--appearance",
        choices=("Light", "Dark"),
        default="Light",
        help="fixed appearance mode; Light is the release baseline",
    )
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-warm-p95-ms", type=float)
    parser.add_argument("--max-idle-p95-ms", type=float)
    parser.add_argument("--max-regression-percent", type=float, default=25.0)
    parser.add_argument("--minimum-regression-slack-ms", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.items < 0:
        parser.error("--items must be non-negative")
    if args.columns < 1:
        parser.error("--columns must be positive")
    if args.warmups < 1:
        parser.error("--warmups must be positive")
    if args.iterations < 5:
        parser.error("--iterations must be at least 5")
    if args.e2e_warmups < 1:
        parser.error("--e2e-warmups must be positive")
    if args.e2e_iterations < 3:
        parser.error("--e2e-iterations must be at least 3")
    if args.e2e_timeout <= 0:
        parser.error("--e2e-timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = _run_benchmark(args)
    metrics = {
        name: TimingSummary(**summary)
        for name, summary in result["metrics"].items()
    }
    failures = check_p95_limits(
        metrics,
        {
            "popup_warm_show_call_ms": args.max_warm_p95_ms,
            "popup_warm_idle_complete_ms": args.max_idle_p95_ms,
        },
    )
    if args.baseline is not None:
        failures.extend(
            _baseline_failures(
                result,
                _load_baseline(args.baseline, result),
                max_regression_percent=args.max_regression_percent,
                minimum_slack_ms=args.minimum_regression_slack_ms,
            )
        )

    print(
        f"popup_construct={result['observations']['popup_construct_ms']:.1f}ms"
    )
    print(
        f"popup_cold_prepare={result['observations']['popup_cold_prepare_ms']:.1f}ms"
    )
    for name, summary in metrics.items():
        print(
            f"{name}: median={summary.median_ms:.1f}ms "
            f"p95={summary.p95_ms:.1f}ms mad={summary.mad_ms:.1f}ms "
            f"n={summary.count}"
        )
    if args.json_path is not None:
        _write_json(args.json_path, result)
        print(f"json={args.json_path}")
    for failure in failures:
        print(f"PERFORMANCE GATE FAILED: {failure}", file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
