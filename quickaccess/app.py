"""QuickAccess Launcher application controller and executable entry point."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ctypes
import logging
import ntpath
import os
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
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
from .diagnostics import collect_diagnostics
from .logging_setup import close_logging, configure_logging, install_exception_hooks
from .models import LauncherConfig, normalize_web_url
from .platform import enable_dpi_awareness, require_windows
from .services.explorer import (
    ExplorerQuickAddService,
    ExplorerTarget,
    ExplorerTargetResult,
    get_foreground_window,
)
from .services.config_transfer import (
    ConfigImportPreview,
    apply_config_import,
    preview_config_import,
    write_portable_config,
)
from .services.hotkeys import NativeHotkeyService
from .services.icons import IconImage, IconService, icon_key
from .services.launcher import FileLauncher
from .services.launcher import LaunchResult
from .services.monitor import MonitorContext, NativeMonitorService, Point, Rect
from .services.singleton import InstanceRequest, SingleInstanceGuard
from .services.startup import (
    StartupManager,
    StartupRegistrationState,
    StartupRegistrationStatus,
    build_startup_command,
)
from .services.tray import TrayService
from .services.update_check import DEFAULT_REPO, UpdateCheckResult, check_for_update
from .services.validation import PathStatus, PathValidationService, ValidationResult
from .storage import ConfigStore, LoadResult
from .ui.dialogs import TextInputDialog, ToastManager, ask_display_name
from .ui.popup import PopupActions, PopupPanel
from .ui.settings import SettingsActions, SettingsWindow


LOGGER = logging.getLogger(__name__)
MUTEX_NAME = "QuickAccessLauncher-2D6B7C9A-0145-4D80-A84C-8297515C16B2"
PUMP_INTERVAL_MS = 8
MONITOR_REFRESH_INTERVAL_MS = 1000
QUICK_ADD_TIMEOUT_MS = 8000
MAX_CONCURRENT_LAUNCHES = 4
MAX_IMPORT_BYTES = 2 * 1024 * 1024
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
        instance_guard: SingleInstanceGuard | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.config = load_result.config
        self.load_result = load_result
        self.started_at_logon = started_at_logon
        self.smoke_test = smoke_test
        self._instance_guard = instance_guard
        self.bus = CommandBus()
        self.toast = ToastManager(root)
        self.monitor = NativeMonitorService()
        self.launcher = FileLauncher()
        self.explorer = ExplorerQuickAddService()
        self.startup = StartupManager()
        self._startup_status: StartupRegistrationStatus | None = None
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
        self._popup_pool: dict[tuple[str, int | None], PopupPanel] = {}
        self._popup_contexts: dict[tuple[str, int | None], MonitorContext] = {}
        self.settings: SettingsWindow | None = None
        self._last_anchor: Point | None = None
        self._last_work_area: Rect | None = None
        self._last_monitor_context: MonitorContext | None = None
        self._stopping = False
        self._quick_add_inflight = False
        self._quick_add_generation = 0
        self._quick_add_timeout_after: str | None = None
        self._launch_inflight: set[str] = set()
        self._launch_slots = threading.BoundedSemaphore(MAX_CONCURRENT_LAUNCHES)
        self._popup_refresh_after: str | None = None
        self._popup_refresh_requires_layout = False
        self._icon_request_after: str | None = None
        self._monitor_refresh_after: str | None = None
        self._monitor_topology_signature: tuple[object, ...] = ()
        self._update_check_after: str | None = None
        self._update_check_generation = 0
        self._update_check_inflight_generations: set[int] = set()

        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    def start(self) -> None:
        """Start integrations, validation and the Tk-side command pump."""

        # Reconcile the persisted preference with HKCU Run on every normal
        # launch.  This also repairs the command after the EXE is moved or
        # renamed, independently of the opt-in network update setting.
        if not self.smoke_test:
            self._synchronize_startup_registration()

        # Finish one widget-tree build per monitor before the global hotkeys
        # become available.  Startup may take a little longer, but once
        # Ctrl+Space can be received the correctly scaled popup is already
        # realized for every active display.
        self._prewarm_popup()

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
        self._schedule_monitor_refresh()
        self._validate_all_paths()
        self._request_all_icons()
        if not self.smoke_test and self.config.check_updates:
            # Delayed well past startup so a slow or firewalled network call
            # never competes with hotkey/tray registration for attention.
            self._schedule_update_check(5000)
        if self.load_result.recovered:
            if getattr(self.load_result, "restored_from_backup", False):
                message = "설정 파일이 손상되어 이전 백업으로 복구했습니다."
            else:
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
        self._drain_instance_requests()
        for command in self.bus.drain_for_ui(40):
            try:
                self._handle_command(command)
            except Exception as error:
                LOGGER.exception("Command handler failed: %r", command)
                self.toast.show(f"요청을 처리하지 못했습니다: {error}", kind="error")
        if not self._stopping:
            self.root.after(PUMP_INTERVAL_MS, self._drain_commands)

    def _drain_instance_requests(self) -> None:
        """Consume later-process activation requests on the existing UI pump."""

        guard = self._instance_guard
        if guard is None:
            return
        try:
            requests = guard.drain_requests()
        except Exception:
            # Activation is a convenience channel.  The resident process and
            # its authoritative mutex must remain healthy if it is unavailable.
            LOGGER.exception("Unable to drain single-instance activation requests")
            return

        for request in requests:
            try:
                if request is InstanceRequest.OPEN_SETTINGS:
                    self.open_settings()
                elif request is InstanceRequest.SHOW_PANEL:
                    self.open_panel()
            except Exception:
                LOGGER.exception("Unable to handle single-instance request: %s", request)

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
            monitor_context = self._monitor_context_at(anchor)
        except Exception:
            LOGGER.exception("Falling back to primary monitor geometry")
            anchor = Point(20, 20) if cursor_position is None else Point(*cursor_position)
            work_area = Rect(
                0,
                0,
                max(320, self.root.winfo_screenwidth()),
                max(240, self.root.winfo_screenheight()),
            )
            monitor_context = MonitorContext(
                identifier="fallback-primary",
                bounds=work_area,
                work_area=work_area,
                scale=None,
            )

        work_area = monitor_context.work_area
        popup = self._ensure_popup(monitor_context)
        previous_popup = self.popup
        if previous_popup is not None and previous_popup is not popup:
            try:
                if previous_popup.winfo_exists():
                    previous_popup.hide()
            except tk.TclError:
                pass
        self.popup = popup
        self._last_anchor = anchor
        self._last_work_area = work_area
        self._last_monitor_context = monitor_context
        popup.show(
            self.config,
            self.statuses,
            anchor,
            work_area,
            icons=self.icon_images,
            target_dpi_scale=monitor_context.scale,
        )
        # Ask again only after the visible frame has been handed back to Tk.
        # Extraction remains entirely off the hotkey path, while a transient
        # shell failure can still recover on a later panel open.
        self._schedule_icon_requests()

    def _monitor_context_at(self, anchor: Point) -> MonitorContext:
        getter = getattr(self.monitor, "get_monitor_context", None)
        if callable(getter):
            return getter(anchor)
        # Compatibility fallback for simple monitor adapters and older
        # Windows versions where only work-area lookup is available.
        work_area = self.monitor.get_monitor_work_area(anchor)
        identifier = (
            f"work-area:{work_area.left}:{work_area.top}:"
            f"{work_area.right}:{work_area.bottom}"
        )
        return MonitorContext(identifier, work_area, work_area, None)

    def _new_popup(self) -> PopupPanel:
        return PopupPanel(
            self.root,
            PopupActions(
                activate=self.activate_item,
                relocate=self.relocate_item,
                open_settings=self.open_settings,
            ),
        )

    def _ensure_popup(
        self,
        monitor_context: MonitorContext | None = None,
    ) -> PopupPanel:
        if monitor_context is None:
            if self.popup is None or not self.popup.winfo_exists():
                self.popup = self._new_popup()
            return self.popup

        key = monitor_context.cache_key
        popup = self._popup_pool.get(key)
        try:
            popup_exists = popup is not None and bool(popup.winfo_exists())
        except tk.TclError:
            popup_exists = False
        if popup_exists:
            self._popup_contexts[key] = monitor_context
            return popup
        if popup is not None:
            self._popup_pool.pop(key, None)
            self._popup_contexts.pop(key, None)

        # A DPI setting change creates one fresh pre-scaled popup for that
        # display.  Retire any old-scale entry for the same device so a stale
        # CustomTkinter window can never be selected later.
        for stale_key, stale_popup in tuple(self._popup_pool.items()):
            if stale_key[0] != monitor_context.identifier or stale_key == key:
                continue
            self._popup_pool.pop(stale_key, None)
            self._popup_contexts.pop(stale_key, None)
            try:
                stale_popup.hide()
                stale_popup.destroy()
            except tk.TclError:
                pass
            if self.popup is stale_popup:
                self.popup = None

        popup = self._new_popup()
        self._popup_pool[key] = popup
        self._popup_contexts[key] = monitor_context
        return popup

    def _prewarm_popup(self) -> None:
        if self._stopping:
            return
        try:
            getter = getattr(self.monitor, "get_monitor_contexts", None)
            if callable(getter):
                monitor_contexts = tuple(getter())
            else:
                anchor = self.monitor.get_cursor_position()
                monitor_contexts = (self._monitor_context_at(anchor),)
            if not monitor_contexts:
                raise RuntimeError("Windows returned no active monitors")

            live_keys = {context.cache_key for context in monitor_contexts}
            for context in monitor_contexts:
                self._ensure_popup(context).prepare(
                    self.config,
                    self.statuses,
                    context.work_area,
                    icons=self.icon_images,
                    target_dpi_scale=context.scale,
                )
            self._discard_stale_popups(live_keys)
            self._monitor_topology_signature = self._topology_signature(
                monitor_contexts
            )
        except Exception:
            # Prewarming is only a latency optimization.  The normal open path
            # retains its complete monitor fallback and error handling.
            LOGGER.exception("Unable to prewarm launcher popup")

    @staticmethod
    def _topology_signature(
        monitor_contexts: tuple[MonitorContext, ...],
    ) -> tuple[object, ...]:
        return tuple(
            (
                context.identifier,
                context.bounds,
                context.work_area,
                context.cache_key[1],
            )
            for context in monitor_contexts
        )

    def _schedule_monitor_refresh(self) -> None:
        if self._stopping or self._monitor_refresh_after is not None:
            return
        self._monitor_refresh_after = self.root.after(
            MONITOR_REFRESH_INTERVAL_MS,
            self._poll_monitor_topology,
        )

    def _poll_monitor_topology(self) -> None:
        self._monitor_refresh_after = None
        if self._stopping:
            return
        try:
            getter = getattr(self.monitor, "get_monitor_contexts", None)
            if callable(getter):
                monitor_contexts = tuple(getter())
                signature = self._topology_signature(monitor_contexts)
                if monitor_contexts and signature != self._monitor_topology_signature:
                    live_keys = {context.cache_key for context in monitor_contexts}
                    for context in monitor_contexts:
                        self._ensure_popup(context).prepare(
                            self.config,
                            self.statuses,
                            context.work_area,
                            icons=self.icon_images,
                            target_dpi_scale=context.scale,
                        )
                    self._discard_stale_popups(live_keys)
                    self._monitor_topology_signature = signature
        except Exception:
            LOGGER.exception("Unable to refresh monitor topology")
        finally:
            self._schedule_monitor_refresh()

    def _cancel_monitor_refresh(self) -> None:
        after_id, self._monitor_refresh_after = self._monitor_refresh_after, None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _discard_stale_popups(
        self,
        live_keys: set[tuple[str, int | None]],
    ) -> None:
        for key, popup in tuple(self._popup_pool.items()):
            if key in live_keys:
                continue
            self._popup_pool.pop(key, None)
            self._popup_contexts.pop(key, None)
            try:
                popup.hide()
                popup.destroy()
            except tk.TclError:
                pass
            if self.popup is popup:
                self.popup = None

    def open_settings(self) -> None:
        popups = list(self._popup_pool.values())
        if self.popup is not None and all(
            popup is not self.popup for popup in popups
        ):
            popups.append(self.popup)
        for popup in popups:
            try:
                popup.hide()
            except tk.TclError:
                pass
        if self.settings is None or not self.settings.winfo_exists():
            self.settings = SettingsWindow(
                self.root,
                self._build_settings_actions(),
            )
        self.settings.show()

    def _build_settings_actions(self) -> SettingsActions:
        return SettingsActions(
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
            get_startup_status=self.get_startup_status,
            import_config=self.import_config,
            export_config=self.export_config,
            copy_diagnostics=self.copy_diagnostics,
        )

    def get_config(self) -> LauncherConfig:
        return deepcopy(self.config)

    def get_startup_status(self) -> StartupRegistrationStatus:
        """Return actual Windows startup state without changing user intent."""

        executable, arguments = startup_invocation()
        try:
            status = self.startup.inspect(
                self.config.run_on_startup,
                executable,
                arguments,
            )
        except Exception as error:
            status = StartupRegistrationStatus(
                state=StartupRegistrationState.UNREADABLE,
                desired_enabled=bool(self.config.run_on_startup),
                expected_command=build_startup_command(executable, arguments),
                error=f"{type(error).__name__}: {error}",
            )
        self._startup_status = status
        return status

    def export_config(self) -> bool:
        """Let the user atomically export a portable, human-readable backup."""

        destination = filedialog.asksaveasfilename(
            parent=self._dialog_parent(),
            title="QuickAccess 설정 내보내기",
            defaultextension=".json",
            initialfile="quickaccess-settings.json",
            filetypes=(("JSON 설정 파일", "*.json"), ("모든 파일", "*.*")),
        )
        if not destination:
            return False
        try:
            write_portable_config(destination, self.config)
        except Exception as error:
            LOGGER.exception("Unable to export portable configuration")
            self.toast.show(f"설정을 내보내지 못했습니다: {error}", kind="error")
            return False
        self.toast.show("설정 백업 파일을 저장했습니다.", kind="success")
        return True

    def import_config(self) -> bool:
        """Preview and safely apply a user-selected portable configuration."""

        source_path = filedialog.askopenfilename(
            parent=self._dialog_parent(),
            title="QuickAccess 설정 가져오기",
            filetypes=(("JSON 설정 파일", "*.json"), ("모든 파일", "*.*")),
        )
        if not source_path:
            return False
        try:
            with open(source_path, "rb") as stream:
                source = stream.read(MAX_IMPORT_BYTES + 1)
            if len(source) > MAX_IMPORT_BYTES:
                raise ValueError("가져오기 파일은 2MB 이하여야 합니다")
        except Exception as error:
            LOGGER.exception("Unable to read portable configuration")
            self.toast.show(f"설정 파일을 읽지 못했습니다: {error}", kind="error")
            return False

        merge_choice = messagebox.askyesnocancel(
            "가져오기 방식",
            "기존 설정에 항목을 합칠까요?\n\n"
            "예: 현재 설정 유지 + 항목 병합\n"
            "아니요: 파일 내용으로 전체 교체\n"
            "취소: 가져오기 중단",
            parent=self._dialog_parent(),
        )
        if merge_choice is None:
            return False
        mode = "merge" if merge_choice else "replace"
        try:
            preview = preview_config_import(self.config, source, mode=mode)
        except Exception as error:
            LOGGER.exception("Unable to preview portable configuration")
            self.toast.show(f"설정 파일을 분석하지 못했습니다: {error}", kind="error")
            return False

        summary = self._format_import_preview(preview)
        if not preview.changed:
            messagebox.showinfo(
                "가져오기 미리보기",
                summary + "\n\n적용할 변경 사항이 없습니다.",
                parent=self._dialog_parent(),
            )
            return False
        if not messagebox.askyesno(
            "가져오기 미리보기",
            summary + "\n\n이 변경 사항을 적용할까요?",
            parent=self._dialog_parent(),
        ):
            return False
        if preview.mode == "replace" and (
            preview.removed_item_ids or preview.settings_changed
        ):
            if not messagebox.askyesno(
                "전체 교체 확인",
                "현재 바로가기와 앱 설정이 백업 파일 내용으로 교체됩니다.\n"
                "이 작업은 자동으로 되돌릴 수 없습니다. 계속할까요?",
                icon="warning",
                parent=self._dialog_parent(),
            ):
                return False

        candidate = apply_config_import(preview)
        if not self._apply_imported_config(candidate):
            return False
        self.toast.show("설정을 안전하게 가져왔습니다.", kind="success")
        return True

    @staticmethod
    def _format_import_preview(preview: ConfigImportPreview) -> str:
        counts = preview.counts
        mode = "기존 설정에 병합" if preview.mode == "merge" else "전체 설정 교체"
        lines = [
            f"방식: {mode}",
            f"추가 {counts['added']}개 · 변경 {counts['updated']}개",
            f"건너뜀 {counts['skipped']}개 · 제거 {counts['removed']}개",
        ]
        if counts["invalid"] or counts["conflicts"]:
            lines.append(
                f"적용 제외: 잘못된 항목 {counts['invalid']}개 · 충돌 {counts['conflicts']}개"
            )
        if preview.settings_changed:
            lines.append("단축키·화면·자동 실행 등 앱 설정도 변경됩니다.")
        return "\n".join(lines)

    def _apply_imported_config(self, candidate: LauncherConfig) -> bool:
        """Commit an imported config with compensating runtime rollbacks."""

        previous = deepcopy(self.config)
        hotkeys_changed = (
            candidate.hotkey != previous.hotkey
            or candidate.quick_add_hotkey != previous.quick_add_hotkey
        )
        startup_changed = candidate.run_on_startup != previous.run_on_startup
        appearance_changed = candidate.appearance_mode != previous.appearance_mode
        hotkeys_attempted = False
        startup_applied = False
        appearance_applied = False
        executable, arguments = startup_invocation()

        try:
            if hotkeys_changed:
                # Native registration can fail after releasing one or both old
                # bindings.  Record the attempt before entering the service so
                # every failure path explicitly restores the previous pair.
                hotkeys_attempted = True
                if not self._configure_hotkeys(
                    candidate.hotkey,
                    candidate.quick_add_hotkey,
                    show_error=True,
                ):
                    raise RuntimeError("가져온 단축키를 등록할 수 없습니다")
                bindings = self.hotkeys.bindings
                candidate.hotkey = bindings.get("panel", candidate.hotkey)
                candidate.quick_add_hotkey = bindings.get(
                    "quick_add", candidate.quick_add_hotkey
                )
                candidate.normalize()

            if startup_changed:
                # Reconcile can write the registry and still return an
                # unverifiable post-state.  Mark the attempt first so every
                # failure path restores the previous user preference.
                startup_applied = True
                status = self.startup.reconcile(
                    candidate.run_on_startup,
                    executable,
                    arguments,
                )
                self._require_startup_in_sync(status)
                self._startup_status = status

            if appearance_changed:
                appearance_applied = True
                apply_appearance_mode(candidate.appearance_mode)

            self.store.save(candidate)
        except Exception as error:
            LOGGER.exception("Unable to apply imported configuration")
            rollback_errors: list[str] = []
            if appearance_applied:
                try:
                    apply_appearance_mode(previous.appearance_mode)
                except Exception as rollback_error:
                    rollback_errors.append(f"화면 스타일: {rollback_error}")
            if startup_applied:
                try:
                    status = self.startup.reconcile(
                        previous.run_on_startup,
                        executable,
                        arguments,
                    )
                    self._require_startup_in_sync(status)
                    self._startup_status = status
                except Exception as rollback_error:
                    rollback_errors.append(f"자동 실행: {rollback_error}")
            if hotkeys_attempted and not self._configure_hotkeys(
                previous.hotkey,
                previous.quick_add_hotkey,
                show_error=False,
            ):
                rollback_errors.append("단축키")
            detail = f"설정을 가져오지 못했습니다: {error}"
            if rollback_errors:
                detail += "\n일부 실행 상태를 복구하지 못했습니다: " + ", ".join(
                    rollback_errors
                )
            self.toast.show(detail, kind="error", duration_ms=7500)
            return False

        old_items = {item.id: item for item in previous.items}
        new_items = {item.id: item for item in candidate.items}
        self.config = candidate
        if candidate.check_updates != previous.check_updates:
            self._update_check_generation += 1
            if candidate.check_updates:
                self._check_for_update()
            else:
                self._cancel_update_check_schedule()

        for item_id, old_item in old_items.items():
            new_item = new_items.get(item_id)
            if new_item is None or (
                new_item.path != old_item.path or new_item.type != old_item.type
            ):
                self.validator.cancel(item_id)
                self.statuses.pop(item_id, None)
        self._validate_all_paths()
        self._request_all_icons()
        self._refresh_visible_popup(layout_required=True)
        return True

    def copy_diagnostics(self) -> bool:
        """Copy a privacy-conscious diagnostic report to the clipboard."""

        try:
            status = self.get_startup_status()
            report = collect_diagnostics(
                deepcopy(self.config),
                startup_status=status,
            ).render()
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            self.root.update_idletasks()
        except Exception as error:
            LOGGER.exception("Unable to copy diagnostics")
            self.toast.show(f"진단 정보를 복사하지 못했습니다: {error}", kind="error")
            return False
        self.toast.show("개인 경로를 제외한 진단 정보를 복사했습니다.", kind="success")
        return True

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
        self._refresh_visible_popup(layout_required=True)
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
        requested = bool(enabled)
        previous = bool(self.config.run_on_startup)
        try:
            status = self.startup.reconcile(requested, executable, arguments)
            self._startup_status = status
            self._require_startup_in_sync(status)
        except Exception as error:
            LOGGER.exception("Unable to update startup registry value")
            self.toast.show(f"자동 실행 설정을 변경하지 못했습니다: {error}", kind="error")
            if requested != previous:
                self._rollback_startup_registration(previous, executable, arguments)
            return False

        if self._commit(
            lambda config: setattr(config, "run_on_startup", requested),
            "자동 실행 설정을 저장하지 못했습니다",
        ):
            return True

        self._rollback_startup_registration(previous, executable, arguments)
        return False

    @staticmethod
    def _require_startup_in_sync(status: StartupRegistrationStatus) -> None:
        if status.in_sync:
            return
        if status.state is StartupRegistrationState.UNREADABLE:
            detail = status.error or "Windows 시작프로그램 상태를 읽을 수 없습니다"
        elif status.desired_enabled:
            detail = "Windows 시작프로그램에 현재 실행 파일이 등록되지 않았습니다"
        else:
            detail = "Windows 시작프로그램 등록이 제거되지 않았습니다"
        raise RuntimeError(detail)

    def _rollback_startup_registration(
        self,
        desired_enabled: bool,
        executable: str,
        arguments: tuple[str, ...],
    ) -> bool:
        try:
            status = self.startup.reconcile(
                bool(desired_enabled),
                executable,
                arguments,
            )
            self._startup_status = status
            self._require_startup_in_sync(status)
            return True
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

        previous = self.config.check_updates
        if not self._commit(
            lambda config: setattr(config, "check_updates", bool(enabled)),
            "업데이트 확인 설정을 저장하지 못했습니다",
        ):
            return False

        if bool(enabled) != previous:
            # Every opt-in period gets its own generation.  A result from a
            # worker started before opt-out must remain stale even if the user
            # opts in again before that old request finishes.
            self._update_check_generation += 1

        if enabled:
            self._check_for_update()
        else:
            self._cancel_update_check_schedule()
        return True

    def _schedule_update_check(self, delay_ms: int) -> None:
        self._cancel_update_check_schedule()
        self._update_check_after = self.root.after(
            max(0, int(delay_ms)),
            self._run_scheduled_update_check,
        )

    def _run_scheduled_update_check(self) -> None:
        self._update_check_after = None
        self._check_for_update()

    def _cancel_update_check_schedule(self) -> None:
        after_id, self._update_check_after = self._update_check_after, None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _synchronize_startup_registration(self) -> None:
        executable, arguments = startup_invocation()
        try:
            status = self.startup.reconcile(
                self.config.run_on_startup,
                executable,
                arguments,
            )
            self._startup_status = status
            self._require_startup_in_sync(status)
        except Exception as error:
            LOGGER.exception("Unable to register configured startup command")
            action = (
                "등록하지 못했습니다"
                if self.config.run_on_startup
                else "남아 있는 항목을 정리하지 못했습니다"
            )
            warning_message = f"부팅 시 자동 실행을 {action}: {error}"
            self.root.after(
                250,
                lambda message=warning_message: self.toast.show(
                    message,
                    kind="warning",
                    duration_ms=6000,
                ),
            )

    def activate_item(self, item_id: str) -> None:
        if item_id in self._launch_inflight:
            return
        try:
            item = self.config.get_item(item_id)
        except KeyError:
            return
        if not self._launch_slots.acquire(blocking=False):
            self.toast.show(
                "열기 작업이 많아 잠시 대기 중입니다. 잠시 후 다시 시도해 주세요.",
                kind="warning",
            )
            return
        item_name = item.name
        item_path = item.path
        self._launch_inflight.add(item_id)

        def worker() -> None:
            try:
                result = self.launcher.launch(item_path)
                self._safe_publish(
                    LaunchResultCommand(item_name=item_name, result=(item_id, result))
                )
            finally:
                self._launch_slots.release()

        try:
            threading.Thread(
                target=worker,
                name=f"QuickAccessLaunch-{item.id}",
                daemon=True,
            ).start()
        except Exception:
            self._launch_inflight.discard(item_id)
            self._launch_slots.release()
            raise

    def _finish_launch(self, item_name: str, result: object) -> None:
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], str)
        ):
            item_id, result = result
            self._launch_inflight.discard(item_id)
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

    def _schedule_icon_requests(self) -> None:
        if self._stopping or self._icon_request_after is not None:
            return
        try:
            self._icon_request_after = self.root.after_idle(
                self._flush_icon_requests
            )
        except tk.TclError:
            self._icon_request_after = None

    def _flush_icon_requests(self) -> None:
        self._icon_request_after = None
        if not self._stopping:
            self._request_all_icons()

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

    def _refresh_visible_popup(self, *, layout_required: bool = False) -> None:
        self._popup_refresh_requires_layout = (
            self._popup_refresh_requires_layout or layout_required
        )
        if self._popup_refresh_after is not None:
            return
        self._popup_refresh_after = self.root.after_idle(self._flush_popup_refresh)

    def _flush_popup_refresh(self) -> None:
        self._popup_refresh_after = None
        layout_required = self._popup_refresh_requires_layout
        self._popup_refresh_requires_layout = False
        if self._stopping:
            return

        popups = list(self._popup_pool.values())
        if self.popup is not None and all(
            popup is not self.popup for popup in popups
        ):
            popups.append(self.popup)
        if not layout_required and popups:
            applied_to_any_popup = False
            runtime_state_applied = True
            for popup in popups:
                try:
                    if not popup.winfo_exists():
                        continue
                    applied_to_any_popup = True
                    runtime_state_applied = popup.apply_runtime_state(
                        self.config,
                        self.statuses,
                        self.icon_images,
                    ) and runtime_state_applied
                except tk.TclError:
                    continue
                except Exception:
                    runtime_state_applied = False
                    LOGGER.exception("Unable to apply popup runtime state")
            if applied_to_any_popup and runtime_state_applied:
                return

        if (
            self.popup is not None
            and self.popup.visible
            and self._last_anchor is not None
            and self._last_work_area is not None
        ):
            self.popup.show(
                self.config,
                self.statuses,
                self._last_anchor,
                self._last_work_area,
                icons=self.icon_images,
                target_dpi_scale=(
                    self._last_monitor_context.scale
                    if getattr(self, "_last_monitor_context", None) is not None
                    else None
                ),
            )
            self._prepare_inactive_popups(self.popup)
            return
        self._prewarm_popup()

    def _prepare_inactive_popups(self, active_popup: PopupPanel) -> None:
        """Refresh hidden monitor-local trees outside the next hotkey path."""

        for key, popup in tuple(self._popup_pool.items()):
            if popup is active_popup:
                continue
            context = self._popup_contexts.get(key)
            if context is None:
                continue
            try:
                if not popup.winfo_exists():
                    continue
                popup.prepare(
                    self.config,
                    self.statuses,
                    context.work_area,
                    icons=self.icon_images,
                    target_dpi_scale=context.scale,
                )
            except tk.TclError:
                continue
            except Exception:
                LOGGER.exception(
                    "Unable to refresh hidden popup for %s",
                    context.identifier,
                )

    def _begin_quick_add(self, explorer_hwnd: int | None) -> None:
        if self._quick_add_inflight:
            return
        self._quick_add_inflight = True
        self._quick_add_generation += 1
        generation = self._quick_add_generation
        self._quick_add_timeout_after = self.root.after(
            QUICK_ADD_TIMEOUT_MS,
            lambda: self._expire_quick_add(generation),
        )

        def worker() -> None:
            result = self.explorer.get_target(explorer_hwnd)
            self._safe_publish(QuickAddResultCommand(result=(generation, result)))

        try:
            threading.Thread(
                target=worker,
                name="QuickAccessExplorerQuickAdd",
                daemon=True,
            ).start()
        except Exception:
            self._cancel_quick_add_timeout()
            self._quick_add_inflight = False
            self._quick_add_generation += 1
            raise

    def _expire_quick_add(self, generation: int) -> None:
        if generation != self._quick_add_generation or not self._quick_add_inflight:
            return
        self._quick_add_timeout_after = None
        self._quick_add_inflight = False
        self._quick_add_generation += 1
        self.toast.show(
            "탐색기 응답이 지연되어 빠른 등록을 중단했습니다. 다시 시도해 주세요.",
            kind="warning",
        )

    def _cancel_quick_add_timeout(self) -> None:
        after_id, self._quick_add_timeout_after = self._quick_add_timeout_after, None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _finish_quick_add(self, result: object) -> None:
        generation = self._quick_add_generation
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], int)
        ):
            generation, result = result
        if generation != self._quick_add_generation or not self._quick_add_inflight:
            return
        self._cancel_quick_add_timeout()
        try:
            if (
                not isinstance(result, ExplorerTargetResult)
                or not result.success
                or not result.path
            ):
                message = (
                    result.error
                    if isinstance(result, ExplorerTargetResult) and result.error
                    else "현재 열린 탐색기 창이 없습니다"
                )
                self.toast.show(message, kind="warning")
                return
            targets = result.targets or (
                ExplorerTarget(
                    path=result.path,
                    suggested_name=(
                        result.suggested_name or nt_basename(result.path)
                    ),
                    item_type=result.item_type or "file",
                ),
            )
            if len(targets) > 1:
                self._add_quick_add_targets(targets)
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
            self._quick_add_generation += 1

    def _add_quick_add_targets(
        self,
        targets: tuple[ExplorerTarget, ...],
    ) -> bool:
        """Add a multi-selection with one durable save and one layout refresh."""

        existing = {
            self._local_target_identity(item.path)
            for item in self.config.items
            if item.type != "url"
        }
        accepted: list[ExplorerTarget] = []
        skipped = 0
        for target in targets:
            identity = self._local_target_identity(target.path)
            if identity in existing:
                skipped += 1
                continue
            existing.add(identity)
            accepted.append(target)

        if not accepted:
            self.toast.show(
                "선택한 항목이 모두 이미 등록되어 있습니다.",
                kind="warning",
            )
            return False

        added_ids: list[str] = []

        def mutate(config: LauncherConfig) -> None:
            for target in accepted:
                item = config.add_item(
                    target.path,
                    name=target.suggested_name or nt_basename(target.path),
                    item_type=target.item_type,  # type: ignore[arg-type]
                )
                added_ids.append(item.id)

        if not self._commit(mutate, "선택한 항목을 저장하지 못했습니다"):
            return False

        for item_id in added_ids:
            item = self.config.get_item(item_id)
            self.statuses.pop(item.id, None)
            try:
                self.validator.validate(item.id, item.path)
            except Exception:
                LOGGER.exception("Failed to validate quick-added item %s", item.path)
            try:
                self.icons.request(icon_key(item.path, item.type), item.path)
            except Exception:
                LOGGER.exception("Failed to request quick-added icon %s", item.path)

        message = f"선택한 항목 {len(accepted)}개를 한 번에 등록했습니다."
        if skipped:
            message += f" 이미 등록된 {skipped}개는 건너뛰었습니다."
        self.toast.show(message, kind="success")
        if self.settings is not None and self.settings.winfo_viewable():
            self.settings.refresh()
        return True

    @staticmethod
    def _local_target_identity(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(path.strip()))

    def _check_for_update(self) -> None:
        if self._stopping or not self.config.check_updates:
            return
        generation = self._update_check_generation
        if generation in self._update_check_inflight_generations:
            return
        self._update_check_inflight_generations.add(generation)

        def worker() -> None:
            result = check_for_update(__version__)
            self._safe_publish(UpdateAvailableCommand(result=(generation, result)))

        threading.Thread(
            target=worker,
            name="QuickAccessUpdateCheck",
            daemon=True,
        ).start()

    def _apply_update_check(self, result: object) -> None:
        generation = self._update_check_generation
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], int)
        ):
            generation, result = result
        self._update_check_inflight_generations.discard(generation)
        if (
            generation != self._update_check_generation
            or not self.config.check_updates
        ):
            return
        if not isinstance(result, UpdateCheckResult) or not result.available:
            return
        if not result.latest_version or result.latest_version == self.config.last_update_notice:
            return
        latest_version = result.latest_version
        release_url = result.release_url or f"https://github.com/{DEFAULT_REPO}/releases/latest"
        if not self._commit(
            lambda config: setattr(config, "last_update_notice", latest_version),
            "업데이트 확인 상태를 저장하지 못했습니다",
        ):
            return
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
        self._cancel_monitor_refresh()
        self._cancel_update_check_schedule()
        self._cancel_quick_add_timeout()
        try:
            self.validator.close()
        except Exception:
            LOGGER.exception("Path validator shutdown failed")
        try:
            self.icons.close()
        except Exception:
            LOGGER.exception("Icon service shutdown failed")
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
        "--settings",
        action="store_true",
        help="open settings, or ask the resident instance to open settings",
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
            request = (
                InstanceRequest.OPEN_SETTINGS
                if args.settings
                else InstanceRequest.SHOW_PANEL
            )
            notified = guard.notify_existing(request)
            LOGGER.info(
                "Existing QuickAccess instance detected; activation %s (%s)",
                request.value,
                "sent" if notified else "unavailable",
            )
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
            instance_guard=guard,
        )
        application.start()
        if args.settings:
            root.after(0, application.open_settings)
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
