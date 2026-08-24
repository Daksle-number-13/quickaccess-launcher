"""Measure PyInstaller one-file startup to an externally observable ready log.

The probe launches ``QuickAccess.exe --smoke-test`` with an isolated
``LOCALAPPDATA`` directory.  It therefore observes the real bootloader,
extraction, imports, Tk construction, hotkey setup and tray setup without
reading or changing the user's launcher configuration.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter, sleep
from typing import Any
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from devtools.benchmarking import (  # noqa: E402
    TimingSummary,
    baseline_regression_limit_ms,
    summarize_ms,
)


SCHEMA_VERSION = 1
BENCHMARK_NAME = "quickaccess-onefile-startup"
READY_MARKER = "QuickAccess started"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""


@contextmanager
def _isolated_directory(parent: Path | None):
    """Create a normal-permission directory and remove only that exact child.

    Python 3.12 applies a restrictive Windows ACL for ``TemporaryDirectory``.
    That ACL is useful generally but can make a child EXE launched from a
    constrained build shell unable to write its isolated log.  A UUID child
    created with the normal directory mode keeps the benchmark usable there.
    """

    root = (parent or Path(tempfile.gettempdir())).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = root / f"QuickAccessPerf-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.parent == root and directory.name.startswith("QuickAccessPerf-"):
            shutil.rmtree(directory, ignore_errors=True)


def measure_startup_once(
    executable: Path,
    *,
    ready_timeout_seconds: float,
    exit_timeout_seconds: float,
    poll_interval_seconds: float,
    temporary_root: Path | None = None,
) -> dict[str, float | bool | None]:
    """Return one isolated smoke launch's externally observed milestones."""

    with _isolated_directory(temporary_root) as directory:
        local_app_data = directory / "LocalAppData"
        local_app_data.mkdir()
        log_path = local_app_data / "QuickAccess" / "logs" / "quickaccess.log"
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(local_app_data)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = perf_counter()
        process = subprocess.Popen(
            [str(executable), "--smoke-test"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        log_open_ms: float | None = None
        ready_ms: float | None = None
        try:
            deadline = started + ready_timeout_seconds
            while perf_counter() < deadline:
                now = perf_counter()
                if log_open_ms is None and log_path.is_file():
                    log_open_ms = (now - started) * 1000.0
                log_text = _read_log(log_path)
                if READY_MARKER in log_text:
                    ready_ms = (perf_counter() - started) * 1000.0
                    break
                return_code = process.poll()
                if return_code is not None:
                    detail = log_text.strip() or "no isolated log was written"
                    raise RuntimeError(
                        f"smoke process exited {return_code} before ready: {detail}"
                    )
                sleep(poll_interval_seconds)
            if ready_ms is None:
                detail = _read_log(log_path).strip() or "no ready marker was written"
                raise TimeoutError(
                    f"startup exceeded {ready_timeout_seconds:.1f}s: {detail}"
                )

            clean_exit = True
            try:
                return_code = process.wait(timeout=exit_timeout_seconds)
            except subprocess.TimeoutExpired:
                # Startup timing is still valid.  Preserve shutdown health as
                # a separate observation and stop only this launched probe.
                clean_exit = False
                return_code = None
                _stop_process(process)
            observed_until_ms = (perf_counter() - started) * 1000.0
            if return_code not in (None, 0):
                detail = _read_log(log_path).strip() or "no diagnostic log was written"
                raise RuntimeError(f"smoke process exited {return_code}: {detail}")
            final_log = _read_log(log_path)
            if "Unable to prewarm launcher popup" in final_log:
                raise RuntimeError("smoke process logged a popup prewarm failure")
            return {
                "log_open_ms": log_open_ms if log_open_ms is not None else ready_ms,
                "resident_ready_ms": ready_ms,
                # Smoke mode intentionally waits before shutdown.  This value
                # diagnoses hangs but is never used as a startup gate.
                "process_exit_ms": observed_until_ms if clean_exit else None,
                "clean_exit": clean_exit,
                "observed_until_ms": observed_until_ms,
            }
        finally:
            _stop_process(process)


def _summary_dict(summary: TimingSummary) -> dict[str, int | float]:
    return summary.as_dict()


def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    executable = args.executable.resolve()
    samples: list[dict[str, float | bool | None]] = []
    for index in range(args.runs):
        samples.append(
            measure_startup_once(
                executable,
                ready_timeout_seconds=args.ready_timeout,
                exit_timeout_seconds=args.exit_timeout,
                poll_interval_seconds=args.poll_interval_ms / 1000.0,
                temporary_root=args.temp_root,
            )
        )
        if index + 1 < args.runs:
            sleep(args.cooldown_ms / 1000.0)

    # The first launch is kept as a cold-ish observation.  It depends heavily
    # on OS/Defender cache state, so only the remaining same-session launches
    # form the repeatable comparison set.
    warm_samples = samples[1:]
    def serialize_sample(
        sample: dict[str, float | bool | None]
    ) -> dict[str, float | bool | None]:
        return {
            name: round(value, 3) if isinstance(value, float) else value
            for name, value in sample.items()
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "host": platform.node(),
            "platform": platform.platform(),
        },
        "artifact": {
            "path": str(executable),
            "size_bytes": executable.stat().st_size,
            "sha256": _sha256(executable),
        },
        "parameters": {
            "runs": args.runs,
            "ready_timeout_seconds": args.ready_timeout,
            "poll_interval_ms": args.poll_interval_ms,
            "mode": "--smoke-test",
            "ready_marker": READY_MARKER,
        },
        "observations": {
            "first_launch": serialize_sample(samples[0]),
            "all_launches": [serialize_sample(sample) for sample in samples],
            "clean_exit_count": sum(
                sample["clean_exit"] is True for sample in samples
            ),
        },
        "metrics": {
            "onefile_warm_log_open_ms": _summary_dict(
                summarize_ms(float(sample["log_open_ms"]) for sample in warm_samples)
            ),
            "onefile_warm_resident_ready_ms": _summary_dict(
                summarize_ms(
                    float(sample["resident_ready_ms"]) for sample in warm_samples
                )
            ),
        },
    }


def _load_baseline(path: Path, current: dict[str, Any]) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("baseline schema version does not match this benchmark")
    if baseline.get("benchmark") != BENCHMARK_NAME:
        raise ValueError("baseline was produced by a different benchmark")
    if baseline.get("environment", {}).get("host") != current["environment"]["host"]:
        raise ValueError("baseline host does not match; compare on the same workstation")
    return baseline


def _median_failures(args: argparse.Namespace, result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if (
        args.require_clean_exit
        and result["observations"]["clean_exit_count"] != args.runs
    ):
        failures.append(
            "one or more smoke processes required forced termination after ready"
        )
    limits = {
        "onefile_warm_log_open_ms": args.max_log_open_median_ms,
        "onefile_warm_resident_ready_ms": args.max_ready_median_ms,
    }
    for name, limit in limits.items():
        if limit is None:
            continue
        actual = float(result["metrics"][name]["median_ms"])
        if actual > limit:
            failures.append(
                f"{name} median {actual:.1f} ms exceeds {limit:.1f} ms"
            )
    if args.baseline is None:
        return failures

    baseline = _load_baseline(args.baseline, result)
    for name in limits:
        actual = float(result["metrics"][name]["median_ms"])
        baseline_median = float(baseline["metrics"][name]["median_ms"])
        allowed = baseline_regression_limit_ms(
            baseline_median,
            max_regression_percent=args.max_regression_percent,
            minimum_slack_ms=args.minimum_regression_slack_ms,
        )
        if actual > allowed:
            failures.append(
                f"{name} median {actual:.1f} ms exceeds same-host baseline "
                f"limit {allowed:.1f} ms (baseline {baseline_median:.1f} ms)"
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
        description="Benchmark a frozen QuickAccess one-file executable."
    )
    parser.add_argument("executable", type=Path)
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--ready-timeout", type=float, default=15.0)
    # A one-file parent can spend several seconds removing its extracted
    # bundle after the 2.5-second in-app smoke window has closed.  Keep this
    # health timeout comfortably above that normal cleanup path.
    parser.add_argument("--exit-timeout", type=float, default=15.0)
    parser.add_argument("--poll-interval-ms", type=float, default=10.0)
    parser.add_argument("--cooldown-ms", type=float, default=250.0)
    parser.add_argument(
        "--temp-root",
        type=Path,
        help="optional writable parent for isolated LOCALAPPDATA directories",
    )
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-log-open-median-ms", type=float)
    parser.add_argument("--max-ready-median-ms", type=float)
    parser.add_argument(
        "--require-clean-exit",
        action="store_true",
        help="also fail when smoke mode does not stop within --exit-timeout",
    )
    parser.add_argument("--max-regression-percent", type=float, default=25.0)
    parser.add_argument("--minimum-regression-slack-ms", type=float, default=150.0)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("the one-file startup benchmark requires Windows")
    if not args.executable.is_file():
        parser.error(f"executable does not exist: {args.executable}")
    if args.runs < 3:
        parser.error("--runs must be at least 3 (first observation plus warm runs)")
    if args.ready_timeout <= 0 or args.exit_timeout <= 0:
        parser.error("timeouts must be positive")
    if args.poll_interval_ms <= 0 or args.cooldown_ms < 0:
        parser.error("poll interval must be positive and cooldown non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = _run_benchmark(args)
    failures = _median_failures(args, result)
    first = result["observations"]["first_launch"]
    print(
        f"first_launch: log_open={first['log_open_ms']:.1f}ms "
        f"resident_ready={first['resident_ready_ms']:.1f}ms"
    )
    for name, values in result["metrics"].items():
        summary = TimingSummary(**values)
        print(
            f"{name}: median={summary.median_ms:.1f}ms "
            f"p95={summary.p95_ms:.1f}ms mad={summary.mad_ms:.1f}ms "
            f"n={summary.count}"
        )
    print(f"sha256={result['artifact']['sha256']}")
    if args.json_path is not None:
        _write_json(args.json_path, result)
        print(f"json={args.json_path}")
    for failure in failures:
        print(f"PERFORMANCE GATE FAILED: {failure}", file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
