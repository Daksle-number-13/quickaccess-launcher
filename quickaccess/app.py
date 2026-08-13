"""QuickAccess Launcher application controller and executable entry point."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ctypes
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog
from typing import Any

import customtkinter as ctk
from PIL import Image

from . import __version__
from .commands import (
    AppCommand,
    CommandBus,
    CommandBusClosedError,
    CommandSource,
    IconReadyCommand,
    LaunchResultCommand,
    OpenPanelCommand,
    OpenSettingsCommand,
    QuickAddCommand,
    QuickAddResultCommand,
    QuitCommand,
    ShowToastCommand,
    ToastLevel,
    UpdateAvailableCommand,
    ValidationResultCommand,
)
from .logging_setup import close_logging, configure_logging, install_exception_hooks
from .models import LauncherConfig, normalize_web_url
from .platform import enable_dpi_awareness, require_windows
from .services.explorer import (
    ExplorerQuickAddService,
    ExplorerTargetResult,
    get_foreground_window,
)
from .services.hotkeys import NativeHotkeyService
from .services.icons import IconImage, IconService, icon_key
from .services.launcher import FileLauncher
from .services.launcher import LaunchResult
from .services.monitor import NativeMonitorService, Point, Rect
from .services.singleton import SingleInstanceGuard
from .services.startup import StartupManager
from .services.tray import TrayService
from .services.update_check import DEFAULT_REPO, UpdateCheckResult, check_for_update
from .services.validation import PathStatus, PathValidationService, ValidationResult
from .storage import ConfigStore, LoadResult
from .ui.dialogs import TextInputDialog, ToastManager, ask_display_name
from .ui.popup import PopupActions, PopupPanel
from .ui.settings import SettingsActions, SettingsWindow


LOGGER = logging.getLogger(__name__)
MUTEX_NAME = "QuickAccessLauncher-2D6B7C9A-0145-4D80-A84C-8297515C16B2"
PUMP_INTERVAL_MS = 16
CTK_APPEARANCE_MODES = {
    "system": "System",
    "light": "Light",
    "dark": "Dark",
}


def apply_appearance_mode(mode: str) -> None:
    """Apply a persisted appearance preference to CustomTkinter."""

    ctk.set_appearance_mode(CTK_APPEARANCE_MODES.get(mode, "System"))


def startup_invocation() -> tuple[str, tuple[str, ...]]:
    """Return the stable command that should be placed in HKCU Run."""

    if getattr(sys, "frozen", False):
        return sys.executable, ("--startup",)
    entry_point = Path(__file__).resolve().parents[1] / "main.py"
    return sys.executable, (str(entry_point), "--startup")


class QuickAccessApp:
    """Coordinate the Tk views and all background Windows integrations."""

    def __init__(
        self,
        root: ctk.CTk,
        store: ConfigStore,
        load_result: LoadResult,
        *,
        started_at_logon: bool = False,
        smoke_test: bool = False,
    ) -> None:
        self.root = root
        self.store = store
        self.config = load_result.config
        self.load_result = load_result
        self.started_at_logon = started_at_logon
        self.smoke_test = smoke_test
        self.bus = CommandBus()
        self.toast = ToastManager(root)
        self.monitor = NativeMonitorService()
        self.launcher = FileLauncher()
        self.explorer = ExplorerQuickAddService()
        self.startup = StartupManager()
        self.hotkeys = NativeHotkeyService(on_callback_error=self._background_error)
        self.tray = TrayService(self.bus)
        self.validator = PathValidationService(
            self._publish_validation_result,
            timeout_seconds=2.0,
            on_callback_error=self._background_error,
        )
        self.icons = IconService(
            self._publish_icon_ready,
            on_callback_error=self._background_error,
        )

        self.statuses: dict[str, PathStatus] = {}
        self.icon_images: dict[str, ctk.CTkImage] = {}
        self.popup: PopupPanel | None = None
        self.settings: SettingsWindow | None = None
        self._last_anchor: Point | None = None
        self._last_work_area: Rect | None = None
        self._stopping = False
        self._quick_add_inflight = False
        self._popup_refresh_after: str | None = None

        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    def start(self) -> None:
        """Start integrations, validation and the Tk-side command pump."""

        if not self.smoke_test and self.config.check_updates:
            self._synchronize_startup_registration()

        hotkey_ready = self._configure_hotkeys(
            self.config.hotkey,
            self.config.quick_add_hotkey,
            show_error=True,
        )
        if self.smoke_test and not hotkey_ready:
            raise RuntimeError("smoke test could not register global hotkeys")
        tray_ready = False
        try:
            self.tray.start()
            tray_ready = self.tray.wait_until_ready(timeout=1.5)
            if not tray_ready:
                error = self.tray.last_error
                raise RuntimeError(error or "트레이 아이콘 준비 시간이 초과되었습니다")
        except Exception as error:
            LOGGER.exception("Unable to start system tray")
            self.toast.show(f"시스템 트레이를 시작하지 못했습니다: {error}", kind="error")
            if self.smoke_test:
                raise RuntimeError("smoke test could not start the system tray") from error

        self.root.after(PUMP_INTERVAL_MS, self._drain_commands)
        self._validate_all_paths()
        self._request_all_icons()
        if not self.smoke_test:
            # Delayed well past startup so a slow or firewalled network call
            # never competes with hotkey/tray registration for attention.
            self.root.after(5000, self._check_for_update)
        # Build the hidden launcher cards while the resident app is settling.
        # The first hotkey press can then reuse the ready widget tree instead
        # of constructing dozens of Tk widgets on the critical display path.
        self.root.after(120, self._prewarm_popup)

        if self.load_result.recovered:
            message = "설정 파일이 손상되어 기본값으로 복구했습니다."
            if self.load_result.backup_path is not None:
                message += f"\n백업: {self.load_result.backup_path.name}"
            self.root.after(350, lambda: self.toast.show(message, kind="warning", duration_ms=5500))
        elif self.load_result.repaired:
            message = "읽을 수 없는 일부 항목을 제외하고 설정을 복구했습니다."
            if self.load_result.backup_path is not None:
                message += f"\n원본 백업: {self.load_result.backup_path.name}"
            self.root.after(
                350,
                lambda: self.toast.show(message, kind="warning", duration_ms=5500),
            )

        if self.load_result.created and not self.config.welcome_shown:
            self._mark_welcome_shown()
            self.root.after(
                650,
                lambda: self.toast.show(
                    "QuickAccess가 실행 중입니다.\nCtrl+Space로 런처를 열 수 있습니다.",
                    kind="info",
                    duration_ms=5000,
                ),
            )

        # If both entry mechanisms failed, expose settings instead of leaving
        # an unreachable resident process.
        if not hotkey_ready and not tray_ready:
            self.root.after(100, self.open_settings)

    def _drain_commands(self) -> None:
        if self._stopping:
            return
        for command in self.bus.drain(40):
            try:
                self._handle_command(command)
            except Exception as error:
                LOGGER.exception("Command handler failed: %r", command)
                self.toast.show(f"요청을 처리하지 못했습니다: {error}", kind="error")
        if not self._stopping:
            self.root.after(PUMP_INTERVAL_MS, self._drain_commands)

    def _handle_command(self, command: AppCommand) -> None:
        if isinstance(command, OpenPanelCommand):
            self.open_panel(command.cursor_position)
        elif isinstance(command, OpenSettingsCommand):
            self.open_settings()
        elif isinstance(command, QuickAddCommand):
            self._begin_quick_add(command.explorer_hwnd)
        elif isinstance(command, QuickAddResultCommand):
            self._finish_quick_add(command.result)
        elif isinstance(command, ValidationResultCommand):
            self._apply_validation_result(command.result)
        elif isinstance(command, LaunchResultCommand):
            self._finish_launch(command.item_name, command.result)
        elif isinstance(command, ShowToastCommand):
            self.toast.show(command.message, kind=command.level.value)
        elif isinstance(command, UpdateAvailableCommand):
            self._apply_update_check(command.result)
        elif isinstance(command, IconReadyCommand):
            self._apply_icon_ready(command.key, command.image)
        elif isinstance(command, QuitCommand):
            self.shutdown()

    def _safe_publish(self, command: AppCommand) -> None:
        try:
            self.bus.publish(command)
        except CommandBusClosedError:
            pass
        except Exception:
            LOGGER.exception("Failed to publish command")

    def _hotkey_open_panel(self) -> None:
        cursor: tuple[int, int] | None = None
        try:
            point = self.monitor.get_cursor_position()
            cursor = (point.x, point.y)
        except Exception:
            LOGGER.exception("Could not capture cursor for hotkey")
        self._safe_publish(
            OpenPanelCommand(source=CommandSource.HOTKEY, cursor_position=cursor)
        )

    def _hotkey_quick_add(self) -> None:
        hwnd: int | None = None
        try:
            hwnd = get_foreground_window()
        except Exception:
            LOGGER.exception("Could not capture foreground Explorer window")
        self._safe_publish(
            QuickAddCommand(source=CommandSource.HOTKEY, explorer_hwnd=hwnd)
        )

    def _configure_hotkeys(
        self,
        panel_hotkey: str,
        quick_add_hotkey: str,
        *,
        show_error: bool,
    ) -> bool:
        try:
            self.hotkeys.configure(
                {
                    "panel": (panel_hotkey, self._hotkey_open_panel),
                    "quick_add": (quick_add_hotkey, self._hotkey_quick_add),
                }
            )
            return True
        except Exception as error:
            LOGGER.exception("Unable to configure global hotkeys")
            if show_error:
                self.toast.show(
                    f"전역 핫키를 등록하지 못했습니다: {error}\n트레이의 설정에서 다른 키를 선택해 주세요.",
                    kind="warning",
                    duration_ms=6500,
                )
            return False

    def open_panel(self, cursor_position: tuple[int, int] | None = None) -> None:
        try:
            anchor = (
                Point(*cursor_position)
                if cursor_position is not None
                else self.monitor.get_cursor_position()
            )
            work_area = self.monitor.get_monitor_work_area(anchor)
        except Exception:
            LOGGER.exception("Falling back to primary monitor geometry")
            anchor = Point(20, 20) if cursor_position is None else Point(*cursor_position)
            work_area = Rect(
                0,
                0,
                max(320, self.root.winfo_screenwidth()),
                max(240, self.root.winfo_screenheight()),
            )

        popup = self._ensure_popup()
        self._last_anchor = anchor
        self._last_work_area = work_area
        popup.show(self.config, self.statuses, anchor, work_area, icons=self.icon_images)

    def _ensure_popup(self) -> PopupPanel:
        if self.popup is None or not self.popup.winfo_exists():
            self.popup = PopupPanel(
                self.root,
                PopupActions(
                    activate=self.activate_item,
                    relocate=self.relocate_item,
                    open_settings=self.open_settings,
                ),
            )
        return self.popup

    def _prewarm_popup(self) -> None:
        if self._stopping:
            return
        try:
            anchor = self.monitor.get_cursor_position()
            work_area = self.monitor.get_monitor_work_area(anchor)
            self._ensure_popup().prepare(
                self.config, self.statuses, work_area, icons=self.icon_images
            )
        except Exception:
            # Prewarming is only a latency optimization.  The normal open path
            # retains its complete monitor fallback and error handling.
            LOGGER.exception("Unable to prewarm launcher popup")

    def open_settings(self) -> None:
        if self.popup is not None:
            self.popup.hide()
        if self.settings is None or not self.settings.winfo_exists():
            self.settings = SettingsWindow(
                self.root,
                SettingsActions(
                    get_config=self.get_config,
                    add_item=self.add_item,
                    delete_item=self.delete_item,
                    rename_item=self.rename_item,
                    move_item=self.move_item,
                    set_appearance_mode=self.set_appearance_mode,
                    set_columns=self.set_columns,
                    set_startup=self.set_startup,
                    set_update_checks=self.set_update_checks,
                    set_hotkeys=self.set_hotkeys,
                    edit_item=self.edit_item,
                ),
            )
        self.settings.show()

    def get_config(self) -> LauncherConfig:
        return deepcopy(self.config)

    def _commit(self, mutator: Any, error_message: str) -> bool:
        candidate = deepcopy(self.config)
        try:
            mutator(candidate)
            self.store.save(candidate)
        except Exception as error:
            LOGGER.exception(error_message)
            self.toast.show(f"{error_message}: {error}", kind="error")
            return False
        self.config = candidate
        self._refresh_visible_popup()
        return True

    def add_item(
        self,
        path: str,
        name: str,
        *,
        item_type: str | None = None,
    ) -> bool:
        item_id: list[str] = []

        def mutate(config: LauncherConfig) -> None:
            item_id.append(
                config.add_item(path, name=name, item_type=item_type).id  # type: ignore[arg-type]
            )

        if not self._commit(mutate, "항목을 저장하지 못했습니다"):
            return False
        item = self.config.get_item(item_id[0])
        self.statuses.pop(item.id, None)
        if item.type != "url":
            self.validator.validate(item.id, item.path)
            self.icons.request(icon_key(item.path, item.type), item.path)
        return True

    def delete_item(self, item_id: str) -> bool:
        try:
            item = self.config.get_item(item_id)
        except KeyError:
            return False
        name, path, item_type, order = item.name, item.path, item.type, item.order

        if not self._commit(
            lambda config: config.delete_item(item_id),
            "항목을 삭제하지 못했습니다",
        ):
            return False
        self.validator.cancel(item_id)
        self.statuses.pop(item_id, None)
        self.toast.show(
            f"'{name}' 항목을 삭제했습니다.",
            kind="warning",
            duration_ms=6000,
            action_text="실행취소",
            action_command=lambda: self._restore_deleted_item(name, path, item_type, order),
        )
        return True

    def _restore_deleted_item(self, name: str, path: str, item_type: str, order: int) -> None:
        restored_id: list[str] = []

        def mutate(config: LauncherConfig) -> None:
            restored = config.add_item(path, name=name, item_type=item_type, position=order)
            restored_id.append(restored.id)

        if not self._commit(mutate, "항목을 복구하지 못했습니다"):
            return
        restored_item = self.config.get_item(restored_id[0])
        self.statuses.pop(restored_item.id, None)
        if restored_item.type != "url":
            self.validator.validate(restored_item.id, restored_item.path)
            self.icons.request(icon_key(restored_item.path, restored_item.type), restored_item.path)
        if self.settings is not None and self.settings.winfo_viewable():
            self.settings.refresh()
        self.toast.show(f"'{name}' 항목을 복구했습니다.", kind="success")

    def rename_item(self, item_id: str, new_name: str) -> bool:
        return self._commit(
            lambda config: config.rename_item(item_id, new_name),
            "표시명을 변경하지 못했습니다",
        )

    def edit_item(
        self,
        item_id: str,
        new_name: str,
        new_path: str,
        *,
        item_type: str | None = None,
    ) -> bool:
        def mutate(config: LauncherConfig) -> None:
            config.rename_item(item_id, new_name)
            config.replace_path(item_id, new_path, item_type=item_type)  # type: ignore[arg-type]

        if not self._commit(mutate, "항목을 수정하지 못했습니다"):
            return False
        self.statuses.pop(item_id, None)
        item = self.config.get_item(item_id)
        if item.type != "url":
            self.validator.validate(item.id, item.path)
            self.icons.request(icon_key(item.path, item.type), item.path)
        return True

    def move_item(self, item_id: str, new_index: int) -> bool:
        return self._commit(
            lambda config: config.move_item(item_id, new_index),
            "항목 순서를 변경하지 못했습니다",
        )

    def set_columns(self, columns: int) -> bool:
        return self._commit(
            lambda config: config.set_columns(columns),
            "격자 열 수를 저장하지 못했습니다",
        )

    def set_appearance_mode(self, mode: str) -> bool:
        if not self._commit(
            lambda config: config.set_appearance_mode(mode),
            "화면 스타일을 저장하지 못했습니다",
        ):
            return False
        apply_appearance_mode(self.config.appearance_mode)
        return True

    def set_hotkeys(self, panel_hotkey: str, quick_add_hotkey: str) -> bool:
        old_panel = self.config.hotkey
        old_quick_add = self.config.quick_add_hotkey
        if not self._configure_hotkeys(panel_hotkey, quick_add_hotkey, show_error=True):
            return False
        registered = self.hotkeys.bindings

        def mutate(config: LauncherConfig) -> None:
            config.hotkey = registered.get("panel", panel_hotkey)
            config.quick_add_hotkey = registered.get("quick_add", quick_add_hotkey)
            config.normalize()

        if self._commit(mutate, "핫키 설정을 저장하지 못했습니다"):
            self.toast.show("핫키를 변경했습니다.", kind="success")
            return True

        # Persistence is authoritative.  Reapply the previous working pair if
        # disk commit failed, leaving native state and JSON in agreement.
        if not self._configure_hotkeys(old_panel, old_quick_add, show_error=False):
            LOGGER.critical(
                "Hotkey rollback failed after configuration persistence failure"
            )
            self.toast.show(
                "이전 핫키를 복구하지 못했습니다. 앱을 다시 시작하면 저장된 설정으로 복구됩니다.",
                kind="error",
                duration_ms=7000,
            )
        return False

    def set_startup(self, enabled: bool) -> bool:
        executable, arguments = startup_invocation()
        previous = self.config.run_on_startup
        try:
            self.startup.set_enabled(enabled, executable, arguments)
        except Exception as error:
            LOGGER.exception("Unable to update startup registry value")
            self.toast.show(f"자동 실행 설정을 변경하지 못했습니다: {error}", kind="error")
            return False

        if self._commit(
            lambda config: setattr(config, "run_on_startup", bool(enabled)),
            "자동 실행 설정을 저장하지 못했습니다",
        ):
            return True

        try:
            self.startup.set_enabled(previous, executable, arguments)
        except Exception as rollback_error:
            LOGGER.exception("Unable to roll back startup registry value")
            self.toast.show(
                "자동 실행 상태를 복구하지 못했습니다. 설정을 다시 시도하거나 앱을 다시 시작해 주세요: "
                f"{rollback_error}",
                kind="error",
                duration_ms=7000,
            )
        return False

    def set_update_checks(self, enabled: bool) -> bool:
        """Persist the explicit consent controlling GitHub release checks."""

        if not self._commit(
            lambda config: setattr(config, "check_updates", bool(enabled)),
            "업데이트 확인 설정을 저장하지 못했습니다",
        ):
            return False
        if enabled:
            self._check_for_update()
        return True

    def _synchronize_startup_registration(self) -> None:
        executable, arguments = startup_invocation()
        try:
            self.startup.set_enabled(
                self.config.run_on_startup,
                executable,
                arguments,
            )
        except Exception as error:
            LOGGER.exception("Unable to register configured startup command")
            if not self.config.run_on_startup:
                self.root.after(
                    250,
                    lambda: self.toast.show(
                        f"남아 있는 자동 실행 항목을 정리하지 못했습니다: {error}",
                        kind="warning",
                        duration_ms=6000,
                    ),
                )
                return
            # Do not claim that auto-start is active when the registry write
            # was rejected by policy or permissions.
            self._commit(
                lambda config: setattr(config, "run_on_startup", False),
                "자동 실행 실패 상태를 저장하지 못했습니다",
            )
            self.root.after(
                250,
                lambda: self.toast.show(
                    f"부팅 시 자동 실행을 등록하지 못했습니다: {error}",
                    kind="warning",
                    duration_ms=6000,
                ),
            )

    def activate_item(self, item_id: str) -> None:
        try:
            item = self.config.get_item(item_id)
        except KeyError:
            return
        item_name = item.name
        item_path = item.path

        def worker() -> None:
            result = self.launcher.launch(item_path)
            self._safe_publish(
                LaunchResultCommand(item_name=item_name, result=result)
            )

        threading.Thread(
            target=worker,
            name=f"QuickAccessLaunch-{item.id}",
            daemon=True,
        ).start()

    def _finish_launch(self, item_name: str, result: object) -> None:
        if isinstance(result, LaunchResult) and not result.success:
            self.toast.show(
                f"'{item_name}'을(를) 열지 못했습니다: {result.error or '알 수 없는 오류'}",
                kind="error",
            )

    def relocate_item(self, item_id: str) -> None:
        try:
            item = self.config.get_item(item_id)
        except KeyError:
            return
        parent = self._dialog_parent()
        if item.type == "url":
            new_path = TextInputDialog(
                parent,
                title="웹 링크 수정",
                prompt="새 인터넷 주소를 입력하세요.",
                initial_value=item.path,
                validator=lambda value: self._web_url_error(value),
            ).show()
            if new_path:
                new_path = normalize_web_url(new_path)
        elif item.type == "folder":
            new_path = filedialog.askdirectory(
                parent=parent,
                title=f"'{item.name}' 경로 재지정",
            )
        else:
            new_path = filedialog.askopenfilename(
                parent=parent,
                title=f"'{item.name}' 경로 재지정",
            )
        if not new_path:
            return
        if self._commit(
            lambda config: config.replace_path(
                item_id,
                new_path,
                item_type=item.type,
            ),
            "경로를 재지정하지 못했습니다",
        ):
            self.statuses.pop(item_id, None)
            new_item = self.config.get_item(item_id)
            if new_item.type != "url":
                self.validator.validate(new_item.id, new_item.path)
                self.icons.request(icon_key(new_item.path, new_item.type), new_item.path)
            self.toast.show("경로를 재지정했습니다.", kind="success")

    @staticmethod
    def _web_url_error(value: str) -> str | None:
        try:
            normalize_web_url(value)
        except ValueError:
            return "http:// 또는 https:// 웹 주소를 입력해 주세요."
        return None

    def _validate_all_paths(self) -> None:
        for item in self.config.items:
            if item.type == "url":
                continue
            try:
                self.validator.validate(item.id, item.path)
            except Exception:
                LOGGER.exception("Failed to schedule path validation for %s", item.path)

    def _request_all_icons(self) -> None:
        if not self.icons.available:
            return
        for item in self.config.items:
            if item.type == "url":
                continue
            try:
                self.icons.request(icon_key(item.path, item.type), item.path)
            except Exception:
                LOGGER.exception("Failed to schedule icon extraction for %s", item.path)

    def _publish_validation_result(self, result: ValidationResult) -> None:
        self._safe_publish(ValidationResultCommand(result=result))

    def _publish_icon_ready(self, key: str, image: IconImage) -> None:
        self._safe_publish(IconReadyCommand(key=key, image=image))

    def _apply_icon_ready(self, key: str, image: object) -> None:
        if not isinstance(image, IconImage) or key in self.icon_images:
            return
        try:
            pil_image = Image.frombuffer(
                "RGBA",
                (image.width, image.height),
                image.bgra,
                "raw",
                "BGRA",
                0,
                1,
            )
            self.icon_images[key] = ctk.CTkImage(
                light_image=pil_image,
                dark_image=pil_image,
                size=(24, 24),
            )
        except Exception:
            LOGGER.exception("Unable to build an icon image for %r", key)
            return
        self._refresh_visible_popup()

    def _apply_validation_result(self, result: object) -> None:
        if not isinstance(result, ValidationResult):
            return
        try:
            current = self.config.get_item(result.item_id)
        except KeyError:
            return
        if current.path != result.path:
            return
        self.statuses[result.item_id] = result.status
        self._refresh_visible_popup()

    def _refresh_visible_popup(self) -> None:
        if self._popup_refresh_after is not None:
            return
        if self.popup is None or not self.popup.visible:
            return
        self._popup_refresh_after = self.root.after_idle(self._flush_popup_refresh)

    def _flush_popup_refresh(self) -> None:
        self._popup_refresh_after = None
        if (
            self._stopping
            or self.popup is None
            or not self.popup.visible
            or self._last_anchor is None
            or self._last_work_area is None
        ):
            return
        self.popup.show(
            self.config,
            self.statuses,
            self._last_anchor,
            self._last_work_area,
            icons=self.icon_images,
        )

    def _begin_quick_add(self, explorer_hwnd: int | None) -> None:
        if self._quick_add_inflight:
            return
        self._quick_add_inflight = True

        def worker() -> None:
            result = self.explorer.get_target(explorer_hwnd)
            self._safe_publish(QuickAddResultCommand(result=result))

        threading.Thread(
            target=worker,
            name="QuickAccessExplorerQuickAdd",
            daemon=True,
        ).start()

    def _finish_quick_add(self, result: object) -> None:
        try:
            if not isinstance(result, ExplorerTargetResult) or not result.success or not result.path:
                message = (
                    result.error
                    if isinstance(result, ExplorerTargetResult) and result.error
                    else "현재 열린 탐색기 창이 없습니다"
                )
                self.toast.show(message, kind="warning")
                return
            name = ask_display_name(
                self._dialog_parent(),
                result.suggested_name or nt_basename(result.path),
                title="빠른 등록",
            )
            if name is not None and self.add_item(
                result.path,
                name,
                item_type=result.item_type,
            ):
                self.toast.show("현재 탐색기 항목을 등록했습니다.", kind="success")
                if self.settings is not None and self.settings.winfo_viewable():
                    self.settings.refresh()
        finally:
            self._quick_add_inflight = False

    def _check_for_update(self) -> None:
        def worker() -> None:
            result = check_for_update(__version__)
            self._safe_publish(UpdateAvailableCommand(result=result))

        threading.Thread(
            target=worker,
            name="QuickAccessUpdateCheck",
            daemon=True,
        ).start()

    def _apply_update_check(self, result: object) -> None:
        if not isinstance(result, UpdateCheckResult) or not result.available:
            return
        if not result.latest_version or result.latest_version == self.config.last_update_notice:
            return
        latest_version = result.latest_version
        release_url = result.release_url or f"https://github.com/{DEFAULT_REPO}/releases/latest"
        self._commit(
            lambda config: setattr(config, "last_update_notice", latest_version),
            "업데이트 확인 상태를 저장하지 못했습니다",
        )
        self.toast.show(
            f"새 버전 {latest_version}이(가) 있습니다.",
            kind="info",
            duration_ms=8000,
            action_text="다운로드 페이지",
            action_command=lambda: self._open_release_page(release_url),
        )

    def _open_release_page(self, url: str) -> None:
        try:
            os.startfile(url)
        except Exception:
            LOGGER.exception("Unable to open the release page")
            self.toast.show(f"페이지를 여는 데 실패했습니다: {url}", kind="warning")

    def _dialog_parent(self) -> tk.Misc:
        if self.settings is not None:
            try:
                if self.settings.winfo_viewable():
                    return self.settings
            except tk.TclError:
                pass
        return self.root

    def _mark_welcome_shown(self) -> None:
        self._commit(
            lambda config: setattr(config, "welcome_shown", True),
            "최초 실행 상태를 저장하지 못했습니다",
        )

    def _background_error(self, error: Exception) -> None:
        LOGGER.error(
            "Background callback failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        self._safe_publish(
            ShowToastCommand(
                message=f"백그라운드 작업에 실패했습니다: {error}",
                level=ToastLevel.ERROR,
            )
        )

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        LOGGER.info("QuickAccess shutdown started")
        try:
            self.validator.close()
        except Exception:
            LOGGER.exception("Path validator shutdown failed")
        try:
            self.hotkeys.stop()
        except Exception:
            LOGGER.exception("Hotkey shutdown failed")
        try:
            self.tray.stop(timeout=2.5)
        except Exception:
            LOGGER.exception("Tray shutdown failed")
        self.bus.close(discard_pending=True)
        self.toast.close()
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass


def nt_basename(path: str) -> str:
    """Return a display name for a Windows path without host-OS assumptions."""

    import ntpath

    return ntpath.basename(path.rstrip("\\/")) or path


def _show_fatal_error(message: str) -> None:
    LOGGER.critical(message)
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "QuickAccess", 0x10)
    except Exception:
        pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QuickAccess Launcher")
    parser.add_argument(
        "--startup",
        action="store_true",
        help="indicates that Windows started the application at logon",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging()
    hooks = install_exception_hooks(chain=not getattr(sys, "frozen", False))
    guard = SingleInstanceGuard(
        f"{MUTEX_NAME}-Smoke" if args.smoke_test else MUTEX_NAME
    )
    root: ctk.CTk | None = None
    smoke_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        require_windows()
        if not guard.acquire():
            return 0
        enable_dpi_awareness()
        if args.smoke_test:
            smoke_directory = tempfile.TemporaryDirectory(prefix="QuickAccessSmoke-")
            store = ConfigStore(Path(smoke_directory.name) / "items.json")
        else:
            store = ConfigStore()
        load_result = store.load()
        if args.smoke_test:
            load_result.config.run_on_startup = False
            load_result.config.welcome_shown = True
            # Keep release verification isolated from a normally running
            # QuickAccess instance, which usually owns Ctrl+Space already.
            load_result.config.hotkey = "ctrl+alt+f23"
            load_result.config.quick_add_hotkey = "ctrl+alt+f24"
            store.save(load_result.config)

        # Apply the persisted preference before constructing any widgets so a
        # forced light/dark mode never flashes the opposite palette at launch.
        apply_appearance_mode(load_result.config.appearance_mode)
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
        root.title("QuickAccess Launcher")
        root.withdraw()
        application = QuickAccessApp(
            root,
            store,
            load_result,
            started_at_logon=args.startup,
            smoke_test=args.smoke_test,
        )
        application.start()
        if args.smoke_test:
            root.after(2500, application.shutdown)
        LOGGER.info("QuickAccess started")
        root.mainloop()
        return 0
    except Exception as error:
        LOGGER.exception("QuickAccess failed during startup")
        _show_fatal_error(f"QuickAccess를 시작하지 못했습니다.\n\n{error}")
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        return 1
    finally:
        guard.close()
        if smoke_directory is not None:
            smoke_directory.cleanup()
        hooks.restore()
        close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
