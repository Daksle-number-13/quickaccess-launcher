"""Windows system-tray integration.

The tray owns no UI state.  Every menu callback only publishes a command for
the Tk thread, which avoids cross-thread Tk calls from pystray's event loop.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
from typing import Any

from PIL import Image

from quickaccess.commands import (
    CommandBus,
    CommandBusClosedError,
    CommandSource,
    OpenPanelCommand,
    OpenSettingsCommand,
    QuitCommand,
)


LOGGER = logging.getLogger(__name__)


class TrayUnavailableError(RuntimeError):
    """Raised when pystray is not installed or cannot be imported."""


class TrayState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


def create_tray_icon(size: int = 64) -> Image.Image:
    """Generate the tray bitmap in memory, avoiding a runtime asset file."""

    if size < 16:
        raise ValueError("tray icon size must be at least 16 pixels")

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inset = max(2, size // 16)
    radius = max(3, size // 5)
    _fill_rounded_rectangle(
        image,
        (inset, inset, size - inset - 1, size - inset - 1),
        radius,
        (37, 99, 235, 255),
    )

    # A high-contrast 2x2 launcher grid remains legible after Windows scales
    # the source image down to a 16px notification-area icon.
    cell = max(2, size // 5)
    gap = max(2, size // 10)
    grid_size = cell * 2 + gap
    origin = (size - grid_size) // 2
    cell_radius = max(1, size // 32)
    for row in range(2):
        for column in range(2):
            left = origin + column * (cell + gap)
            top = origin + row * (cell + gap)
            _fill_rounded_rectangle(
                image,
                (left, top, left + cell, top + cell),
                cell_radius,
                (255, 255, 255, 255),
            )
    return image


def _fill_rounded_rectangle(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    radius: int,
    color: tuple[int, int, int, int],
) -> None:
    """Draw a tiny RGBA rounded rectangle without importing Pillow plugins."""

    left, top, right, bottom = bounds
    radius = max(0, min(radius, (right - left + 1) // 2, (bottom - top + 1) // 2))
    pixels = image.load()
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            if left + radius <= x <= right - radius or top + radius <= y <= bottom - radius:
                pixels[x, y] = color
                continue
            center_x = left + radius if x < left + radius else right - radius
            center_y = top + radius if y < top + radius else bottom - radius
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2:
                pixels[x, y] = color


def _load_pystray() -> Any:
    try:
        import pystray  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TrayUnavailableError(
            "pystray is required to display the QuickAccess tray icon"
        ) from exc
    return pystray


class TrayService:
    """Own and safely start/stop a pystray icon on its worker thread."""

    def __init__(
        self,
        command_bus: CommandBus,
        *,
        backend: Any | None = None,
        icon_name: str = "quickaccess",
        title: str = "QuickAccess Launcher",
    ) -> None:
        self._command_bus = command_bus
        self._backend = backend
        self._icon_name = icon_name
        self._title = title
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()
        self._stopped_event.set()
        self._state = TrayState.STOPPED
        self._last_error: BaseException | None = None

    @property
    def state(self) -> TrayState:
        with self._lock:
            return self._state

    @property
    def last_error(self) -> BaseException | None:
        with self._lock:
            return self._last_error

    @property
    def is_running(self) -> bool:
        return self.state in (TrayState.STARTING, TrayState.RUNNING)

    def start(self) -> bool:
        """Start the tray loop; return ``False`` if it was already active."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            backend = self._backend if self._backend is not None else _load_pystray()
            menu = backend.Menu(
                backend.MenuItem(
                    "패널 열기", self._on_open_panel, default=True
                ),
                backend.MenuItem("설정", self._on_open_settings),
                backend.Menu.SEPARATOR,
                backend.MenuItem("종료", self._on_quit),
            )
            icon = backend.Icon(
                self._icon_name,
                icon=create_tray_icon(),
                title=self._title,
                menu=menu,
            )
            self._icon = icon
            self._last_error = None
            self._ready_event.clear()
            self._stopped_event.clear()
            self._state = TrayState.STARTING
            self._thread = threading.Thread(
                target=self._run,
                args=(icon,),
                name="QuickAccessTray",
                daemon=True,
            )
            self._thread.start()
            return True

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait for setup and report whether the icon became visible."""

        self._ready_event.wait(timeout)
        return self.state is TrayState.RUNNING

    def stop(self, timeout: float = 3.0) -> bool:
        """Request shutdown and wait briefly for the tray loop to finish.

        The method is idempotent.  It never joins the current thread, so it is
        also safe if an alternate backend invokes shutdown from its own loop.
        """

        if timeout < 0:
            raise ValueError("timeout must be non-negative")

        with self._lock:
            icon = self._icon
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = TrayState.STOPPED
                self._stopped_event.set()
                return True
            self._state = TrayState.STOPPING

        try:
            if icon is not None:
                icon.stop()
        except Exception as exc:  # pragma: no cover - backend-dependent
            LOGGER.exception("Failed to request tray shutdown")
            with self._lock:
                self._last_error = exc

        if thread is not threading.current_thread():
            thread.join(timeout)

        stopped = not thread.is_alive()
        with self._lock:
            if stopped:
                self._state = TrayState.STOPPED
                self._stopped_event.set()
            else:
                LOGGER.warning("Tray thread did not stop within %.2f seconds", timeout)
        return stopped

    def _run(self, icon: Any) -> None:
        try:
            icon.run(setup=self._on_setup)
        except Exception as exc:  # pragma: no cover - backend-dependent
            LOGGER.exception("System tray loop failed")
            with self._lock:
                self._last_error = exc
                self._state = TrayState.FAILED
        finally:
            self._ready_event.set()
            self._stopped_event.set()
            with self._lock:
                if self._state is not TrayState.FAILED:
                    self._state = TrayState.STOPPED

    def _on_setup(self, icon: Any) -> None:
        with self._lock:
            stopping = self._state in (TrayState.STOPPING, TrayState.STOPPED)
        if stopping:
            # stop() can win the race with pystray's asynchronous setup
            # callback.  Do not make an icon visible after shutdown began.
            try:
                icon.stop()
            finally:
                self._ready_event.set()
            return

        try:
            icon.visible = True
        except Exception as exc:  # pragma: no cover - backend-dependent
            LOGGER.exception("Failed to make tray icon visible")
            with self._lock:
                self._last_error = exc
                self._state = TrayState.FAILED
            try:
                icon.stop()
            finally:
                self._ready_event.set()
            return

        with self._lock:
            self._state = TrayState.RUNNING
        self._ready_event.set()

    def _publish(self, command: Any) -> None:
        try:
            self._command_bus.publish(command)
        except CommandBusClosedError:
            # A late notification-area click can race with coordinated shutdown.
            LOGGER.debug("Ignoring tray command after command bus shutdown")
        except Exception:
            # Exceptions must never escape into pystray's native callback loop.
            LOGGER.exception("Failed to enqueue tray command")

    def _on_open_panel(self, _icon: Any = None, _item: Any = None) -> None:
        self._publish(OpenPanelCommand(source=CommandSource.TRAY))

    def _on_open_settings(self, _icon: Any = None, _item: Any = None) -> None:
        self._publish(OpenSettingsCommand(source=CommandSource.TRAY))

    def _on_quit(self, _icon: Any = None, _item: Any = None) -> None:
        self._publish(QuitCommand(source=CommandSource.TRAY, reason="tray"))
