"""Exercise the prewarmed popup pool across real mixed-DPI monitors.

This benchmark is intended for an interactive Windows release workstation.
It enumerates the active displays, prepares one popup on every display before
measurement, then repeatedly opens the popups in alternating monitor order.
The JSON report includes both the direct ``PopupPanel.show`` path and the
normal ``OpenPanelCommand`` dispatch-to-visible/stable path.

The probe also enforces the two properties that make the mixed-DPI fast path
safe: an unchanged popup must not rebuild its widget tree and a call supplied
with a known target DPI must not schedule the legacy 80 ms DPI re-show.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import platform
from pathlib import Path
import sys
from time import perf_counter, sleep
from typing import Any, Callable

import customtkinter as ctk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from devtools.benchmarking import TimingSummary, summarize_ms  # noqa: E402
from quickaccess.app import PUMP_INTERVAL_MS, QuickAccessApp  # noqa: E402
from quickaccess.commands import CommandBus, CommandSource, OpenPanelCommand  # noqa: E402
from quickaccess.models import LauncherConfig, LauncherItem  # noqa: E402
from quickaccess.platform import enable_dpi_awareness  # noqa: E402
from quickaccess.services.monitor import (  # noqa: E402
    MonitorContext,
    NativeMonitorService,
    Point,
)
from quickaccess.ui.popup import PopupActions, PopupPanel  # noqa: E402


SCHEMA_VERSION = 2
BENCHMARK_NAME = "quickaccess-popup-mixed-dpi"
DEFAULT_ITEMS = 20
DEFAULT_COLUMNS = 3
DEFAULT_WARMUPS = 2
DEFAULT_ITERATIONS = 6
DEFAULT_E2E_WARMUPS = 1
DEFAULT_E2E_ITERATIONS = 3


@dataclass(slots=True)
class _Probe:
    started_at: float
    visible_at: float | None = None
    stable_at: float | None = None


class _InstrumentedPopup(PopupPanel):
    """PopupPanel with observation hooks that do not alter the show path."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.show_invocations = 0
        self.delayed_80ms_schedules = 0
        self.active_probe: _Probe | None = None
        super().__init__(*args, **kwargs)
        self.bind("<<QuickAccessVisible>>", self._record_visible, add="+")

    def show(self, *args: object, **kwargs: object) -> None:
        self.show_invocations += 1
        super().show(*args, **kwargs)

    def after(
        self,
        ms: int | str,
        func: Callable[..., object] | None = None,
        *args: object,
    ) -> str | None:
        # ``after_idle`` delegates here with the literal string ``"idle"``.
        if ms == 80 or ms == "80":
            self.delayed_80ms_schedules += 1
        return super().after(ms, func, *args)

    def _record_visible(self, _event: object) -> None:
        probe = self.active_probe
        if probe is not None and probe.visible_at is None:
            probe.visible_at = perf_counter()

    def _finish_show(self, show_generation: int) -> None:
        probe = self.active_probe
        if (
            probe is not None
            and probe.stable_at is None
            and show_generation == self._show_generation
            and self.visible
        ):
            probe.stable_at = perf_counter()
        super()._finish_show(show_generation)


class _NoRescheduleRoot:
    """Forward Tk operations while stopping the command pump after one drain."""

    def __init__(self, root: ctk.CTk) -> None:
        self._root = root

    @staticmethod
    def after(_delay_ms: int, _callback: Callable[[], None]) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._root, name)


class _NoopToast:
    @staticmethod
    def show(*_args: object, **_kwargs: object) -> None:
        return None


class _CommandHarness:
    """Minimum controller state for the application's real open-panel path."""

    def __init__(
        self,
        root: ctk.CTk,
        monitor: NativeMonitorService,
        config: LauncherConfig,
        popups: dict[tuple[str, int | None], _InstrumentedPopup],
    ) -> None:
        self.root = _NoRescheduleRoot(root)
        self.monitor = monitor
        self.config = config
        self.statuses: dict[str, object] = {}
        self.icon_images: dict[str, ctk.CTkImage] = {}
        self.popup: _InstrumentedPopup | None = None
        self._popup_pool = popups
        self._popup_contexts: dict[tuple[str, int | None], MonitorContext] = {}
        self._last_anchor: Point | None = None
        self._last_work_area = None
        self._last_monitor_context: MonitorContext | None = None
        self.bus = CommandBus()
        self.toast = _NoopToast()
        self._stopping = False

    def _handle_command(self, command: object) -> None:
        QuickAccessApp._handle_command(self, command)  # type: ignore[arg-type]

    def _drain_commands(self) -> None:
        QuickAccessApp._drain_commands(self)  # type: ignore[arg-type]

    @staticmethod
    def _drain_instance_requests() -> None:
        return None

    def open_panel(self, cursor_position: tuple[int, int] | None = None) -> None:
        QuickAccessApp.open_panel(self, cursor_position)  # type: ignore[arg-type]

    def _monitor_context_at(self, anchor: Point) -> MonitorContext:
        return self.monitor.get_monitor_context(anchor)

    def _ensure_popup(
        self,
        monitor_context: MonitorContext | None = None,
    ) -> _InstrumentedPopup:
        if monitor_context is None:
            if self.popup is None:
                raise RuntimeError("benchmark popup was not initialized")
            return self.popup
        try:
            return self._popup_pool[monitor_context.cache_key]
        except KeyError as error:
            raise RuntimeError(
                f"no prewarmed popup for {monitor_context.identifier}"
            ) from error

    @staticmethod
    def _schedule_icon_requests() -> None:
        return None


def _sample_config(item_count: int, columns: int) -> LauncherConfig:
    return LauncherConfig(
        columns=columns,
        items=[
            LauncherItem(
                name=f"혼합 DPI 항목 {index + 1}",
                path=rf"C:\QuickAccess\Mixed-DPI-{index + 1}",
                type="folder",
                order=index,
            )
            for index in range(item_count)
        ],
    )


def _anchor_for(context: MonitorContext) -> Point:
    work = context.work_area
    return Point(
        work.left + max(0, work.width // 2),
        work.top + max(0, work.height // 2),
    )


def _alternating_contexts(
    contexts: tuple[MonitorContext, ...],
) -> tuple[MonitorContext, ...]:
    """Order displays low/high/next-low/next-high to maximize DPI changes."""

    ordered = sorted(
        contexts,
        key=lambda context: (
            float(context.scale or 0.0),
            context.bounds.left,
            context.bounds.top,
            context.identifier,
        ),
    )
    result: list[MonitorContext] = []
    low = 0
    high = len(ordered) - 1
    while low <= high:
        result.append(ordered[low])
        low += 1
        if low <= high:
            result.append(ordered[high])
            high -= 1
    return tuple(result)


def _wait_for_probe(
    root: ctk.CTk,
    popup: _InstrumentedPopup,
    probe: _Probe,
    timeout_seconds: float,
) -> tuple[float, float]:
    deadline = probe.started_at + timeout_seconds
    while (
        (probe.visible_at is None or probe.stable_at is None)
        and perf_counter() < deadline
    ):
        root.update()
        if probe.visible_at is None or probe.stable_at is None:
            sleep(0.0005)
    if probe.visible_at is None or probe.stable_at is None:
        popup.active_probe = None
        popup.hide()
        root.update()
        missing = "visible signal" if probe.visible_at is None else "stable idle"
        raise TimeoutError(
            f"{popup!r} did not reach {missing} within {timeout_seconds:.1f}s"
        )
    return (
        (probe.visible_at - probe.started_at) * 1000.0,
        (probe.stable_at - probe.started_at) * 1000.0,
    )


def _hide_all(root: ctk.CTk, popups: tuple[_InstrumentedPopup, ...]) -> None:
    for popup in popups:
        popup.active_probe = None
        popup.hide()
    # Consume matching cloak and focus notifications before the next sample.
    root.update()


def _context_dict(context: MonitorContext) -> dict[str, object]:
    return {
        "identifier": context.identifier,
        "scale": context.scale,
        "bounds": [
            context.bounds.left,
            context.bounds.top,
            context.bounds.right,
            context.bounds.bottom,
        ],
        "work_area": [
            context.work_area.left,
            context.work_area.top,
            context.work_area.right,
            context.work_area.bottom,
        ],
    }


def _summary(samples: list[float]) -> dict[str, int | float]:
    return summarize_ms(samples).as_dict()


def _skip_result(
    args: argparse.Namespace,
    contexts: tuple[MonitorContext, ...],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "status": "skipped",
        "reason": reason,
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
        },
        "monitors": [_context_dict(context) for context in contexts],
    }


def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    enable_dpi_awareness()
    monitor = NativeMonitorService()
    contexts = monitor.get_monitor_contexts()
    if not contexts:
        raise RuntimeError("Windows returned no active monitor contexts")

    known_scales = {
        round(float(context.scale), 3)
        for context in contexts
        if context.scale is not None
        and math.isfinite(float(context.scale))
        and float(context.scale) > 0
    }
    if len(known_scales) < 2:
        reason = (
            "mixed-DPI benchmark requires at least two distinct detected "
            f"scales; found {sorted(known_scales)}"
        )
        if args.allow_single_scale_skip:
            return _skip_result(args, contexts, reason)
        raise RuntimeError(reason + " (use --allow-single-scale-skip to skip)")
    missing_scales = [
        context.identifier for context in contexts if context.scale is None
    ]
    if missing_scales:
        raise RuntimeError(
            "DPI scale lookup failed for active monitors: "
            + ", ".join(missing_scales)
        )

    ctk.set_appearance_mode(args.appearance)
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.withdraw()
    root.update_idletasks()

    config = _sample_config(args.items, args.columns)
    actions = PopupActions(
        activate=lambda _item: None,
        relocate=lambda _item: None,
        open_settings=lambda: None,
    )
    popup_by_key: dict[tuple[str, int | None], _InstrumentedPopup] = {}
    prepare_ms: dict[tuple[str, int | None], float] = {}

    try:
        for context in contexts:
            started = perf_counter()
            popup = _InstrumentedPopup(root, actions)
            popup.prepare(
                config,
                {},
                context.work_area,
                icons={},
                target_dpi_scale=context.scale,
            )
            root.update_idletasks()
            prepare_ms[context.cache_key] = (perf_counter() - started) * 1000.0
            popup_by_key[context.cache_key] = popup

        popups = tuple(popup_by_key.values())
        root.update()
        _hide_all(root, popups)
        ordered_contexts = _alternating_contexts(contexts)
        baseline = {
            key: {
                "render_count": popup.render_count,
                "shell": popup.winfo_children()[0]
                if popup.winfo_children()
                else None,
                "show_invocations": popup.show_invocations,
                "delayed_80ms_schedules": popup.delayed_80ms_schedules,
            }
            for key, popup in popup_by_key.items()
        }
        expected_shows = 0

        direct_show_samples: list[float] = []
        direct_visible_samples: list[float] = []
        direct_stable_samples: list[float] = []
        direct_by_monitor: dict[str, dict[str, list[float]]] = {
            context.identifier: {"show": [], "visible": [], "stable": []}
            for context in contexts
        }

        def direct_cycle(context: MonitorContext, *, collect: bool) -> None:
            nonlocal expected_shows
            _hide_all(root, popups)
            popup = popup_by_key[context.cache_key]
            probe = _Probe(perf_counter())
            popup.active_probe = probe
            popup.show(
                config,
                {},
                _anchor_for(context),
                context.work_area,
                icons={},
                target_dpi_scale=context.scale,
            )
            expected_shows += 1
            show_ms = (perf_counter() - probe.started_at) * 1000.0
            visible_ms, stable_ms = _wait_for_probe(
                root, popup, probe, args.timeout
            )
            if collect:
                direct_show_samples.append(show_ms)
                direct_visible_samples.append(visible_ms)
                direct_stable_samples.append(stable_ms)
                bucket = direct_by_monitor[context.identifier]
                bucket["show"].append(show_ms)
                bucket["visible"].append(visible_ms)
                bucket["stable"].append(stable_ms)
            _hide_all(root, popups)

        for index in range(args.warmups * len(ordered_contexts)):
            direct_cycle(
                ordered_contexts[index % len(ordered_contexts)], collect=False
            )
        for index in range(args.iterations * len(ordered_contexts)):
            direct_cycle(
                ordered_contexts[index % len(ordered_contexts)], collect=True
            )

        harness = _CommandHarness(root, monitor, config, popup_by_key)
        command_visible_samples: list[float] = []
        command_stable_samples: list[float] = []
        command_by_monitor: dict[str, dict[str, list[float]]] = {
            context.identifier: {"visible": [], "stable": []}
            for context in contexts
        }

        def command_cycle(context: MonitorContext, *, collect: bool) -> None:
            nonlocal expected_shows
            _hide_all(root, popups)
            popup = popup_by_key[context.cache_key]
            probe = _Probe(perf_counter())
            popup.active_probe = probe
            anchor = _anchor_for(context)
            harness.bus.publish(
                OpenPanelCommand(
                    source=CommandSource.HOTKEY,
                    cursor_position=(anchor.x, anchor.y),
                )
            )
            root.after(PUMP_INTERVAL_MS, harness._drain_commands)
            expected_shows += 1
            visible_ms, stable_ms = _wait_for_probe(
                root, popup, probe, args.timeout
            )
            if collect:
                command_visible_samples.append(visible_ms)
                command_stable_samples.append(stable_ms)
                bucket = command_by_monitor[context.identifier]
                bucket["visible"].append(visible_ms)
                bucket["stable"].append(stable_ms)
            _hide_all(root, popups)

        for index in range(args.e2e_warmups * len(ordered_contexts)):
            command_cycle(
                ordered_contexts[index % len(ordered_contexts)], collect=False
            )
        for index in range(args.e2e_iterations * len(ordered_contexts)):
            command_cycle(
                ordered_contexts[index % len(ordered_contexts)], collect=True
            )

        # Run the event loop past the old two-attempt 80 ms settle window.  If
        # that legacy path was scheduled, the counters/show counts below make
        # the failure deterministic instead of depending on timing luck.
        settle_deadline = perf_counter() + 0.20
        while perf_counter() < settle_deadline:
            root.update()
            sleep(0.001)

        render_rebuilds = sum(
            popup.render_count - int(baseline[key]["render_count"])
            for key, popup in popup_by_key.items()
        )
        delayed_80ms_schedules = sum(
            popup.delayed_80ms_schedules
            - int(baseline[key]["delayed_80ms_schedules"])
            for key, popup in popup_by_key.items()
        )
        actual_shows = sum(
            popup.show_invocations - int(baseline[key]["show_invocations"])
            for key, popup in popup_by_key.items()
        )
        replaced_shells = sum(
            1
            for key, popup in popup_by_key.items()
            if not popup.winfo_children()
            or popup.winfo_children()[0] is not baseline[key]["shell"]
        )
        invariant_failures: list[str] = []
        if render_rebuilds:
            invariant_failures.append(
                f"warm monitor switching rebuilt {render_rebuilds} render trees"
            )
        if replaced_shells:
            invariant_failures.append(
                f"warm monitor switching replaced {replaced_shells} popup shells"
            )
        if delayed_80ms_schedules:
            invariant_failures.append(
                "target-DPI shows scheduled "
                f"{delayed_80ms_schedules} legacy 80 ms DPI checks"
            )
        if actual_shows != expected_shows:
            invariant_failures.append(
                f"observed {actual_shows} show calls for {expected_shows} "
                "explicit opens (possible delayed re-show)"
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "status": "passed" if not invariant_failures else "failed",
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
                "warmups_per_monitor": args.warmups,
                "iterations_per_monitor": args.iterations,
                "e2e_warmups_per_monitor": args.e2e_warmups,
                "e2e_iterations_per_monitor": args.e2e_iterations,
                "timeout_seconds": args.timeout,
                "command_pump_interval_ms": PUMP_INTERVAL_MS,
                "transition_order": [
                    context.identifier for context in ordered_contexts
                ],
            },
            "monitors": [
                {
                    **_context_dict(context),
                    "popup_prepare_ms": round(
                        prepare_ms[context.cache_key], 3
                    ),
                }
                for context in contexts
            ],
            "invariants": {
                "render_tree_rebuilds": render_rebuilds,
                "replaced_popup_shells": replaced_shells,
                "legacy_80ms_dpi_schedules": delayed_80ms_schedules,
                "explicit_show_calls": expected_shows,
                "observed_show_calls": actual_shows,
                "failures": invariant_failures,
            },
            "metrics": {
                "popup_show_call_ms": _summary(direct_show_samples),
                "popup_show_to_visible_ms": _summary(direct_visible_samples),
                "popup_show_to_stable_ms": _summary(direct_stable_samples),
                "open_panel_command_to_visible_ms": _summary(
                    command_visible_samples
                ),
                "open_panel_command_to_stable_ms": _summary(
                    command_stable_samples
                ),
            },
            "metrics_by_monitor": {
                context.identifier: {
                    "scale": context.scale,
                    "popup_show_call_ms": _summary(
                        direct_by_monitor[context.identifier]["show"]
                    ),
                    "popup_show_to_visible_ms": _summary(
                        direct_by_monitor[context.identifier]["visible"]
                    ),
                    "popup_show_to_stable_ms": _summary(
                        direct_by_monitor[context.identifier]["stable"]
                    ),
                    "open_panel_command_to_visible_ms": _summary(
                        command_by_monitor[context.identifier]["visible"]
                    ),
                    "open_panel_command_to_stable_ms": _summary(
                        command_by_monitor[context.identifier]["stable"]
                    ),
                }
                for context in contexts
            },
        }
    finally:
        for popup in popup_by_key.values():
            try:
                popup.destroy()
            except Exception:
                pass
        root.destroy()


def _write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark prewarmed QuickAccess popups while alternating across "
            "real mixed-DPI monitors."
        )
    )
    parser.add_argument("--items", type=int, default=DEFAULT_ITEMS)
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--e2e-warmups", type=int, default=DEFAULT_E2E_WARMUPS)
    parser.add_argument(
        "--e2e-iterations", type=int, default=DEFAULT_E2E_ITERATIONS
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--appearance",
        choices=("Light", "Dark"),
        default="Light",
    )
    parser.add_argument(
        "--allow-single-scale-skip",
        action="store_true",
        help="exit successfully with a skipped JSON result on a single-DPI PC",
    )
    parser.add_argument("--json", type=Path, dest="json_path")
    args = parser.parse_args(argv)
    if args.items < 0:
        parser.error("--items must be non-negative")
    if args.columns < 1:
        parser.error("--columns must be positive")
    if args.warmups < 0:
        parser.error("--warmups must be non-negative")
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.e2e_warmups < 0:
        parser.error("--e2e-warmups must be non-negative")
    if args.e2e_iterations < 1:
        parser.error("--e2e-iterations must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def _print_summary(result: dict[str, Any]) -> None:
    if result["status"] == "skipped":
        print(f"SKIPPED: {result['reason']}")
        return
    monitor_text = ", ".join(
        f"{monitor['identifier']}={float(monitor['scale']):.2f}x"
        for monitor in result["monitors"]
    )
    print(f"monitors: {monitor_text}")
    for name, values in result["metrics"].items():
        summary = TimingSummary(**values)
        print(
            f"{name}: median={summary.median_ms:.1f}ms "
            f"p95={summary.p95_ms:.1f}ms mad={summary.mad_ms:.1f}ms "
            f"n={summary.count}"
        )
    invariants = result["invariants"]
    print(
        "invariants: "
        f"rebuilds={invariants['render_tree_rebuilds']} "
        f"delayed_80ms={invariants['legacy_80ms_dpi_schedules']} "
        f"shows={invariants['observed_show_calls']}/"
        f"{invariants['explicit_show_calls']}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = _run_benchmark(args)
    _print_summary(result)
    if args.json_path is not None:
        _write_json(args.json_path, result)
        print(f"json={args.json_path}")
    if result["status"] == "failed":
        for failure in result["invariants"]["failures"]:
            print(f"INVARIANT FAILED: {failure}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
