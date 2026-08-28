"""Cursor-positioned launcher popup."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import sys
import tkinter as tk

import customtkinter as ctk
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

from ..models import LauncherConfig, LauncherItem
from ..search import LauncherSearchIndex
from ..services.icons import icon_key
from ..services.monitor import Point, Rect, Size, clamp_window_to_work_area
from ..services.validation import PathStatus
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_SOFT,
    BORDER,
    BORDER_WIDTH,
    CARD_RADIUS,
    DANGER,
    DANGER_SOFT,
    DANGER_SOFT_HOVER,
    MUTED,
    SURFACE,
    SURFACE_ALT,
    SURFACE_HOVER,
    TEXT,
    WARNING,
    WARNING_SOFT,
    WARNING_SOFT_HOVER,
    WINDOW_RADIUS,
    brand_image,
    font,
    icon_font,
)


BUTTON_WIDTH = 136
BUTTON_HEIGHT = 68
GAP = 8
PADDING = 12
HEADER_HEIGHT = 42
HEADER_GAP = 10
SCROLLBAR_RESERVE = 16

_GLYPH_FOLDER = "\uE8B7"
_GLYPH_FILE = "\uE8A5"
_GLYPH_LINK = "\uE71B"
_GLYPH_SETTINGS = "\uE713"
_TRANSPARENT_KEY = "#010203"
_DWM_CLOAK = 13
_DWM_CLOAKED = 14
_DWM_CLOAKED_APP = 0x00000001
_FOCUS_LOSS_SETTLE_MS = 40
_FOCUS_ARMING_MS = 240


@dataclass(frozen=True, slots=True)
class PopupActions:
    activate: Callable[[str], None]
    relocate: Callable[[str], None]
    open_settings: Callable[[], None]


def geometry_string(width: int, height: int, position: Point) -> str:
    """Format signed Tk coordinates correctly for left/upper monitors."""

    return f"{width}x{height}{position.x:+d}{position.y:+d}"


def popup_dimensions(
    item_count: int,
    configured_columns: int,
    work_area: Rect,
    window_scaling: float = 1.0,
) -> tuple[int, int, int, int]:
    """Return logical dimensions constrained to a physical work area."""

    scale = max(0.1, float(window_scaling))
    available_width = max(1, int((work_area.width - 16) / scale))
    width_limited_columns = max(
        1,
        (available_width - PADDING * 2 + GAP) // (BUTTON_WIDTH + GAP),
    )
    columns = max(
        1,
        min(configured_columns, max(1, item_count), width_limited_columns),
    )
    natural_width = PADDING * 2 + columns * BUTTON_WIDTH + (columns - 1) * GAP
    width = min(natural_width, available_width)
    if item_count == 0:
        width = min(max(360, width), available_width)

    vertical_chrome = PADDING * 2 + HEADER_HEIGHT + HEADER_GAP
    rows = max(1, math.ceil(item_count / columns))
    natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
    natural_height = vertical_chrome + natural_items_height
    max_height = max(100, int((work_area.height - 16) / scale))
    height = min(natural_height, max_height)
    viewport_height = max(BUTTON_HEIGHT, height - vertical_chrome)
    if natural_items_height > viewport_height:
        # A visible scrollbar consumes horizontal space inside the item frame.
        # If the popup is already at the monitor-width cap, adding its reserve
        # cannot widen the window and would clip the last card column instead.
        # Recalculate the column count against the usable canvas width first.
        scroll_width_limited_columns = max(
            1,
            (
                available_width
                - SCROLLBAR_RESERVE
                - PADDING * 2
                + GAP
            )
            // (BUTTON_WIDTH + GAP),
        )
        if columns > scroll_width_limited_columns:
            columns = scroll_width_limited_columns
            rows = max(1, math.ceil(item_count / columns))
            natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
            natural_width = (
                PADDING * 2
                + columns * BUTTON_WIDTH
                + (columns - 1) * GAP
            )
            width = min(natural_width, available_width)
            natural_height = vertical_chrome + natural_items_height
            height = min(natural_height, max_height)
            viewport_height = max(BUTTON_HEIGHT, height - vertical_chrome)
        width = min(width + SCROLLBAR_RESERVE, available_width)
    return width, height, columns, viewport_height


def grid_navigation_target(
    row: int,
    column: int,
    direction: str,
    columns: int,
    item_count: int,
) -> int | None:
    """Return the row-major card index reached from ``(row, column)``.

    Returns ``None`` at a grid edge so callers can leave focus where it is
    instead of wrapping to an unrelated card.
    """

    index = row * columns + column
    if direction == "left":
        return index - 1 if column > 0 else None
    if direction == "right":
        target = index + 1
        return target if column < columns - 1 and target < item_count else None
    if direction == "up":
        return index - columns if row > 0 else None
    if direction == "down":
        target = index + columns
        return target if target < item_count else None
    return None


def _ellipsize(value: str, limit: int = 22) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def _status_text(item: LauncherItem, status: PathStatus | None) -> str:
    if status is PathStatus.MISSING:
        return "경로 없음 · 재지정"
    if status is PathStatus.TIMEOUT:
        return "응답 지연 · 열기 시도"
    if status is PathStatus.ERROR:
        return "확인 실패 · 재지정"
    if item.type == "url":
        return "웹 링크"
    return "폴더" if item.type == "folder" else "파일"


def _is_broken_path_status(status: PathStatus | None) -> bool:
    """Return whether a status requires replacing the target before launch."""

    if status in (PathStatus.MISSING, PathStatus.ERROR):
        return True
    # Keep compatibility if validation later splits an inaccessible target
    # from the current generic ERROR state.
    inaccessible = getattr(PathStatus, "INACCESSIBLE", None)
    return inaccessible is not None and status is inaccessible


class _LauncherCard(ctk.CTkFrame):
    """A compact two-line launcher card with mouse and keyboard activation."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        item: LauncherItem,
        status: PathStatus | None,
        command: Callable[[], None],
        on_context_menu: Callable[[tk.Event[tk.Misc]], None] | None = None,
        icon: ctk.CTkImage | None = None,
    ) -> None:
        self._status_state = None if status in (None, PathStatus.VALID) else status
        self._current_icon = icon
        self._broken = _is_broken_path_status(status)
        self._timed_out = status is PathStatus.TIMEOUT
        if self._timed_out:
            self._style_state = "warning"
            self._normal_color = WARNING_SOFT
            self._hover_color = WARNING_SOFT_HOVER
            self._resting_border = WARNING
            self._status_color = WARNING
        elif self._broken:
            self._style_state = "danger"
            self._normal_color = DANGER_SOFT
            self._hover_color = DANGER_SOFT_HOVER
            self._resting_border = DANGER
            self._status_color = DANGER
        else:
            self._style_state = "normal"
            self._normal_color = SURFACE_ALT
            self._hover_color = SURFACE_HOVER
            self._resting_border = BORDER
            self._status_color = MUTED
        self._command = command

        super().__init__(
            parent,
            width=BUTTON_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=CARD_RADIUS,
            border_width=BORDER_WIDTH,
            border_color=self._resting_border,
            fg_color=self._normal_color,
            cursor="hand2",
        )
        try:
            # CTkFrame routes ``bind`` to its internal canvas, so the canvas
            # must also own keyboard focus for Return/Space activation.
            self._canvas.configure(takefocus=True)
        except tk.TclError:
            pass
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        icon_tile = ctk.CTkFrame(
            self,
            width=34,
            height=34,
            corner_radius=10,
            fg_color=SURFACE if self._broken else ACCENT_SOFT,
        )
        icon_tile.grid(row=0, column=0, rowspan=2, padx=(8, 7), pady=16)
        icon_tile.grid_propagate(False)
        self._icon_tile = icon_tile
        if icon is not None and not self._broken:
            # A real shell icon is only shown once one has actually been
            # extracted; the broken-path glyph coloring always wins so the
            # card still reads as needing attention.
            icon_label = ctk.CTkLabel(icon_tile, text="", image=icon, width=34, height=34)
        else:
            glyph = (
                _GLYPH_LINK
                if item.type == "url"
                else (_GLYPH_FOLDER if item.type == "folder" else _GLYPH_FILE)
            )
            icon_label = ctk.CTkLabel(
                icon_tile,
                text=glyph,
                width=34,
                height=34,
                font=icon_font(16),
                text_color=self._resting_border if self._broken else ACCENT,
            )
        icon_label.pack(fill="both", expand=True)
        self._icon_label = icon_label

        name_label = ctk.CTkLabel(
            self,
            text=_ellipsize(item.name, 10),
            height=22,
            font=font(11, "bold"),
            text_color=TEXT,
            anchor="w",
        )
        name_label.grid(row=0, column=1, padx=(0, 8), pady=(11, 0), sticky="sew")
        self._name_label = name_label
        status_label = ctk.CTkLabel(
            self,
            text=_status_text(item, status),
            height=18,
            font=font(10),
            text_color=self._status_color,
            anchor="w",
        )
        status_label.grid(row=1, column=1, padx=(0, 8), pady=(0, 11), sticky="new")
        self._status_label = status_label

        self._interactive_widgets: tuple[tk.Misc, ...] = (
            self,
            icon_tile,
            icon_label,
            name_label,
            status_label,
        )
        for widget in self._interactive_widgets:
            widget.bind("<Button-1>", self._invoke, add="+")
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
            if on_context_menu is not None:
                widget.bind("<Button-3>", on_context_menu, add="+")
        self.bind("<Return>", self._invoke, add="+")
        self.bind("<space>", self._invoke, add="+")
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")

    def update_state(
        self,
        *,
        item: LauncherItem,
        status: PathStatus | None,
        command: Callable[[], None],
        icon: ctk.CTkImage | None,
    ) -> None:
        """Update status/icon presentation without replacing the widget tree."""

        normalized_status = None if status in (None, PathStatus.VALID) else status
        status_changed = normalized_status != self._status_state
        icon_changed = icon is not self._current_icon
        if not status_changed and not icon_changed:
            return

        style_changed = False
        if status_changed:
            previous_broken = self._broken
            self._status_state = normalized_status
            self._broken = _is_broken_path_status(status)
            self._timed_out = status is PathStatus.TIMEOUT
            next_style = (
                "warning"
                if self._timed_out
                else ("danger" if self._broken else "normal")
            )
            style_changed = next_style != self._style_state
            self._style_state = next_style
            self._command = command

            if self._timed_out:
                self._normal_color = WARNING_SOFT
                self._hover_color = WARNING_SOFT_HOVER
                self._resting_border = WARNING
                self._status_color = WARNING
            elif self._broken:
                self._normal_color = DANGER_SOFT
                self._hover_color = DANGER_SOFT_HOVER
                self._resting_border = DANGER
                self._status_color = DANGER
            else:
                self._normal_color = SURFACE_ALT
                self._hover_color = SURFACE_HOVER
                self._resting_border = BORDER
                self._status_color = MUTED

            if style_changed:
                focused = self.focus_get() is getattr(self, "_canvas", None)
                self.configure(
                    fg_color=self._normal_color,
                    border_color=ACCENT if focused else self._resting_border,
                )
                if previous_broken != self._broken:
                    self._icon_tile.configure(
                        fg_color=SURFACE if self._broken else ACCENT_SOFT
                    )

            status_options: dict[str, object] = {"text": _status_text(item, status)}
            if style_changed:
                status_options["text_color"] = self._status_color
            self._status_label.configure(**status_options)

        if style_changed or (icon_changed and not self._broken):
            self._render_icon(item, icon)
        self._current_icon = icon

    def _render_icon(self, item: LauncherItem, icon: ctk.CTkImage | None) -> None:
        if icon is not None and not self._broken:
            self._icon_label.configure(text="", image=icon)
        else:
            glyph = (
                _GLYPH_LINK
                if item.type == "url"
                else (_GLYPH_FOLDER if item.type == "folder" else _GLYPH_FILE)
            )
            self._icon_label.configure(
                text=glyph,
                image=None,
                font=icon_font(16),
                text_color=self._resting_border if self._broken else ACCENT,
            )

    def _invoke(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        self.focus_set()
        self._command()
        return "break"

    def focus_set(self) -> None:
        self._canvas.focus_set()

    def _on_enter(self, _event: tk.Event[tk.Misc]) -> None:
        self.configure(fg_color=self._hover_color)

    def _on_leave(self, _event: tk.Event[tk.Misc]) -> None:
        self.after_idle(self._restore_if_pointer_left)

    def _restore_if_pointer_left(self) -> None:
        try:
            pointer_x, pointer_y = self.winfo_pointerxy()
            left, top = self.winfo_rootx(), self.winfo_rooty()
            inside = (
                left <= pointer_x < left + self.winfo_width()
                and top <= pointer_y < top + self.winfo_height()
            )
        except tk.TclError:
            inside = False
        if not inside:
            self.configure(fg_color=self._normal_color)

    def _on_focus_in(self, _event: tk.Event[tk.Misc]) -> None:
        self.configure(border_color=ACCENT)

    def _on_focus_out(self, _event: tk.Event[tk.Misc]) -> None:
        self.configure(border_color=self._resting_border)


class _OwnedScrollableFrame(ctk.CTkScrollableFrame):
    """Scrollable frame that removes only the global bindings it owns."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._owned_global_bindings: list[tuple[str, str]] = []
        super().__init__(*args, **kwargs)

    def bind_all(
        self,
        sequence: str | None = None,
        func: Callable[..., object] | None = None,
        add: str | bool | None = None,
    ) -> str | None:
        func_id = super().bind_all(sequence, func, add)
        if sequence is not None and func_id:
            self._owned_global_bindings.append((sequence, func_id))
        return func_id

    def destroy(self) -> None:
        bindings, self._owned_global_bindings = self._owned_global_bindings, []
        root = self._root()
        for sequence, func_id in bindings:
            try:
                root._unbind(("bind", "all", sequence), func_id)
            except tk.TclError:
                pass
        super().destroy()


class PopupPanel(ctk.CTkToplevel):
    """A monitor-local reusable topmost panel; all methods run on Tk."""

    def __init__(self, root: tk.Misc, actions: PopupActions) -> None:
        super().__init__(root)
        self._actions = actions
        self._visible = False
        self._show_generation = 0
        self._focus_arming_generation: int | None = None
        self._focus_arm_after: str | None = None
        self._focus_loss_after: str | None = None
        self._render_signature: tuple[object, ...] | None = None
        self._dynamic_signature: tuple[object, ...] | None = None
        self._card_dynamic_states: dict[str, tuple[object, ...]] = {}
        self._layout_signature: tuple[int, int, bool, float] | None = None
        self._layout_columns = 1
        self._layout_rows = 1
        self._scrolling = False
        self._render_count = 0
        self._dynamic_update_count = 0
        self._prepared_dpi_scale: float | None = None
        self._prepared_monitor_signature: tuple[object, ...] | None = None
        # Windows can keep a fully realized popup in DWM while cloaking it
        # between invocations.  This avoids the occasionally very expensive
        # withdraw/deiconify remap on the hotkey path.  The optimization is
        # enabled only after both the native cloak and one hidden map succeed;
        # every unsupported/error path retains Tk's ordinary withdraw flow.
        self._warm_mapped = False
        self._native_cloak_available: bool | None = None
        self._native_window_handle: int | None = None
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._cards: list[_LauncherCard] = []
        self._card_by_item_id: dict[str, _LauncherCard] = {}
        self._visible_cards: list[_LauncherCard] = []
        self._search_index: LauncherSearchIndex | None = None
        self._search_variable: tk.StringVar | None = None
        self._search_trace_id: str | None = None
        self._search_entry: ctk.CTkEntry | None = None
        self._search_empty_state: ctk.CTkFrame | None = None
        self._search_empty_button: ctk.CTkButton | None = None
        self._showing_search_empty = False
        self._layout_viewport_height = BUTTON_HEIGHT
        self._items_frame: _OwnedScrollableFrame | None = None
        self._scroll_canvas: tk.Canvas | None = None
        self._empty_state: ctk.CTkFrame | None = None
        self._header_compact: bool | None = None
        self._brand_images: dict[int, ctk.CTkImage] = {}
        self._brand_mark: ctk.CTkLabel | None = None
        self._brand_label: ctk.CTkLabel | None = None
        self._count_badge: ctk.CTkLabel | None = None
        self._settings_button: ctk.CTkButton | None = None
        self._window_background: str | tuple[str, str] = SURFACE
        try:
            self.configure(fg_color=_TRANSPARENT_KEY)
            self.attributes("-transparentcolor", _TRANSPARENT_KEY)
            self._window_background = _TRANSPARENT_KEY
        except tk.TclError:
            # ``-transparentcolor`` is Windows-specific; a matching surface
            # background remains a clean fallback for tests and other Tk ports.
            self.configure(fg_color=SURFACE)
        self.bind("<Escape>", self._on_escape)
        self.bind("<Control-f>", self._focus_search, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")

    @staticmethod
    def _enable_keyboard_button(button: ctk.CTkButton) -> None:
        target = getattr(button, "_canvas", None)
        if not isinstance(target, tk.Misc):
            return
        target.configure(takefocus=True)
        resting_border_width = int(button.cget("border_width"))
        resting_border_color = button.cget("border_color")

        def activate(_event: tk.Event[tk.Misc]) -> str:
            button.invoke()
            return "break"

        button.bind("<Return>", activate, add="+")
        button.bind("<space>", activate, add="+")
        button.bind(
            "<FocusIn>",
            lambda _event: button.configure(border_width=2, border_color=ACCENT),
            add="+",
        )
        button.bind(
            "<FocusOut>",
            lambda _event: button.configure(
                border_width=resting_border_width,
                border_color=resting_border_color,
            ),
            add="+",
        )

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def render_count(self) -> int:
        """Number of full widget-tree builds, exposed for performance checks."""

        return self._render_count

    @property
    def dynamic_update_count(self) -> int:
        return self._dynamic_update_count

    @property
    def warm_mapping_enabled(self) -> bool:
        """Whether this popup is parked in DWM instead of being remapped."""

        return self._warm_mapped

    def apply_runtime_state(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        icons: Mapping[str, ctk.CTkImage] | None = None,
    ) -> bool:
        """Update validation and icon state without remapping the popup.

        Background results can arrive while the user is navigating the panel.
        Keeping this path separate from :meth:`show` preserves focus, scroll
        position and the current mapped window while updating only cards whose
        runtime presentation changed.  ``False`` tells the controller that a
        structural config change requires a normal layout refresh instead.
        """

        if self._content_signature(config) != self._render_signature:
            return False
        self._update_dynamic_content(config, statuses, icons or {})
        return True

    def show(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        anchor: Point,
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage] | None = None,
        *,
        target_dpi_scale: float | None = None,
    ) -> None:
        icons = icons or {}
        self._show_generation += 1
        show_generation = self._show_generation
        self._cancel_focus_arming()
        self._cancel_focus_loss_check()
        self._focus_arming_generation = show_generation
        # Search state belongs to one invocation of the launcher.  Restore
        # the already-created cards while the window is still withdrawn so a
        # reopened popup never flashes stale results or pays for a remap.
        self._reset_search_for_show()
        layout_scale = self._synchronize_dpi_scale(target_dpi_scale)
        self._layout_popup(
            config,
            statuses,
            anchor,
            work_area,
            icons,
            layout_scale=layout_scale,
        )
        self._expose_window()
        self._visible = True
        self.after_idle(lambda: self._finish_show(show_generation))
        # Legacy callers without a known target scale retain the delayed
        # fallback.  The application supplies a target and selects a popup
        # already prewarmed on that monitor, so no post-map DPI polling or
        # recursive second show occurs on the normal hotkey path.
        if target_dpi_scale is None:
            self.after(
                80,
                lambda: self._settle_dpi(
                    show_generation,
                    config,
                    statuses,
                    anchor,
                    work_area,
                    applied_scale=layout_scale,
                    attempts_remaining=2,
                    icons=icons,
                ),
            )
        else:
            # Confirm the HWND actually landed on the expected DPI at the
            # next idle turn.  A display-setting race is corrected by a
            # layout-only pass; it never maps, focuses or calls show again.
            self.after_idle(
                lambda: self._validate_mapped_dpi(
                    show_generation,
                    target_dpi_scale,
                    config,
                    statuses,
                    anchor,
                    work_area,
                    icons,
                )
            )

    def _layout_popup(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        anchor: Point,
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage],
        *,
        layout_scale: float,
    ) -> None:
        width, height, columns, viewport_height = popup_dimensions(
            len(config.items),
            config.columns,
            work_area,
            layout_scale,
        )
        rows = max(1, math.ceil(len(config.items) / columns))
        natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
        render_viewport_height = (
            viewport_height if natural_items_height > viewport_height else None
        )

        self._ensure_content(
            config,
            statuses,
            columns=columns,
            viewport_height=viewport_height,
            render_viewport_height=render_viewport_height,
            icons=icons,
        )

        rendered_size = Size(
            self._apply_window_scaling(width),
            self._apply_window_scaling(height),
        )
        position = clamp_window_to_work_area(anchor, rendered_size, work_area)
        self.geometry(geometry_string(width, height, position))
        if self._scroll_canvas is not None:
            self._scroll_canvas.yview_moveto(0.0)

    def prepare(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage] | None = None,
        *,
        target_dpi_scale: float | None = None,
    ) -> None:
        """Build hidden popup content so the first hotkey press is immediate."""

        icons = icons or {}
        monitor_signature: tuple[object, ...] | None = None
        needs_staging = False
        if target_dpi_scale is not None:
            try:
                scale_key: object = round(float(target_dpi_scale), 4)
            except (TypeError, ValueError):
                scale_key = target_dpi_scale
            monitor_signature = (
                work_area.left,
                work_area.top,
                work_area.right,
                work_area.bottom,
                scale_key,
            )
            needs_staging = monitor_signature != self._prepared_monitor_signature
        if needs_staging:
            # A withdrawn HWND can still be assigned to a monitor.  Position
            # it first so CustomTkinter's next background DPI check observes
            # the same display scale that we apply below.
            origin = Point(work_area.left + 8, work_area.top + 8)
            tk.Toplevel.geometry(self, f"{origin.x:+d}{origin.y:+d}")
            self.update_idletasks()
        layout_scale = self._synchronize_dpi_scale(target_dpi_scale)
        width, height, columns, viewport_height = popup_dimensions(
            len(config.items),
            config.columns,
            work_area,
            layout_scale,
        )
        rows = max(1, math.ceil(len(config.items) / columns))
        natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
        render_viewport_height = (
            viewport_height if natural_items_height > viewport_height else None
        )
        prepared = self._ensure_content(
            config,
            statuses,
            columns=columns,
            viewport_height=viewport_height,
            render_viewport_height=render_viewport_height,
            icons=icons,
        )
        if needs_staging:
            origin = Point(work_area.left + 8, work_area.top + 8)
            self.geometry(geometry_string(width, height, origin))
        if prepared or needs_staging:
            # Realize geometry while the Toplevel is hidden.  Otherwise Tk
            # defers initial construction or a hidden re-grid until the first
            # hotkey exposes the panel, moving work back onto that path.
            self.update_idletasks()
        if monitor_signature is not None:
            self._prepared_monitor_signature = monitor_signature
        self._prime_warm_mapping()

    def _get_native_window_handle(self) -> int | None:
        """Return the outer Win32 HWND used by DWM for this Tk toplevel."""

        if sys.platform != "win32":
            return None
        if self._native_window_handle is not None:
            return self._native_window_handle
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_parent = user32.GetParent
            get_parent.argtypes = [wintypes.HWND]
            get_parent.restype = wintypes.HWND
            client_handle = int(self.winfo_id())
            outer_handle = int(get_parent(client_handle) or client_handle)
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            return None
        if outer_handle <= 0:
            return None
        self._native_window_handle = outer_handle
        return outer_handle

    def _set_native_cloak(self, cloaked: bool) -> bool:
        """Set the documented Windows DWM cloak flag, if supported."""

        handle = self._get_native_window_handle()
        if handle is None:
            return False
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            setter = dwmapi.DwmSetWindowAttribute
            setter.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                wintypes.LPCVOID,
                wintypes.DWORD,
            ]
            setter.restype = ctypes.c_long
            value = wintypes.BOOL(bool(cloaked))
            result = int(
                setter(
                    handle,
                    _DWM_CLOAK,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return result == 0

    def _is_native_cloaked(self) -> bool | None:
        """Return the app-cloak state, or ``None`` when DWM is unavailable."""

        handle = self._get_native_window_handle()
        if handle is None:
            return None
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            getter = dwmapi.DwmGetWindowAttribute
            getter.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            getter.restype = ctypes.c_long
            value = wintypes.DWORD(0)
            result = int(
                getter(
                    handle,
                    _DWM_CLOAKED,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        if result != 0:
            return None
        return bool(int(value.value) & _DWM_CLOAKED_APP)

    def _prime_warm_mapping(self) -> None:
        """Map once while cloaked so later hotkey opens do not remap HWND."""

        if self._warm_mapped or self._visible or self._native_cloak_available is False:
            return
        if not self._set_native_cloak(True):
            self._native_cloak_available = False
            return
        try:
            self.deiconify()
            # Pay creation/mapping/DWM synchronization during application
            # prewarm.  DWM keeps the cloaked window invisible and excludes it
            # from hit testing while the complete widget tree is realized.
            self.update_idletasks()
            if not self.winfo_ismapped() or self._is_native_cloaked() is not True:
                raise tk.TclError("DWM did not keep the prepared popup cloaked")
        except tk.TclError:
            try:
                self.withdraw()
            except tk.TclError:
                pass
            self._set_native_cloak(False)
            self._native_cloak_available = False
            return
        self._native_cloak_available = True
        self._warm_mapped = True

    def _expose_window(self) -> None:
        """Expose a prepared popup, falling back to ordinary Tk mapping."""

        if self._warm_mapped:
            # Move/reflow occurred while cloaked.  Raise first, then uncloak so
            # the first user-visible frame is already at its final geometry.
            self.lift()
            if self._set_native_cloak(False):
                self.event_generate("<<QuickAccessVisible>>", when="tail")
                return

            # A DWM reset is exceptionally rare.  Returning to the conventional
            # path is safer than leaving a logically visible but cloaked panel.
            self._warm_mapped = False
            self._native_cloak_available = False
            self.withdraw()
            self._set_native_cloak(False)

        self.deiconify()
        self.lift()
        self.event_generate("<<QuickAccessVisible>>", when="tail")

    def _synchronize_dpi_scale(self, target_dpi_scale: float | None) -> float:
        """Apply a known monitor DPI before the popup becomes visible.

        CustomTkinter 5.2.2 normally discovers cross-monitor changes on a
        100 ms polling loop and then updates every child widget.  QuickAccess
        pins that dependency version and updates its tracker once while each
        pooled popup is still hidden.  Repeated hotkey opens therefore do
        not pay the full-tree scaling cost or show a stale first frame.
        """

        if target_dpi_scale is None:
            return self._get_window_scaling()
        try:
            target = float(target_dpi_scale)
        except (TypeError, ValueError):
            return self._get_window_scaling()
        if not math.isfinite(target) or target <= 0:
            return self._get_window_scaling()

        current = ScalingTracker.window_dpi_scaling_dict.get(self)
        if current is None or abs(float(current) - target) > 0.001:
            ScalingTracker.window_dpi_scaling_dict[self] = target
            ScalingTracker.update_scaling_callbacks_for_window(self)
            # CTkToplevel temporarily pins min/max dimensions while scaling.
            # Restore the configured bounds immediately instead of waiting
            # for its built-in one-second timer.
            self._set_scaled_min_max()
        self._prepared_dpi_scale = target
        return self._get_window_scaling()

    def _validate_mapped_dpi(
        self,
        show_generation: int,
        target_dpi_scale: float,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        anchor: Point,
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage],
    ) -> None:
        if show_generation != self._show_generation or not self._visible:
            return
        try:
            actual_dpi_scale = float(ScalingTracker.get_window_dpi_scaling(self))
            expected_dpi_scale = float(target_dpi_scale)
        except (KeyError, TypeError, ValueError, tk.TclError):
            return
        if (
            not math.isfinite(actual_dpi_scale)
            or actual_dpi_scale <= 0
            or abs(actual_dpi_scale - expected_dpi_scale) <= 0.01
        ):
            return
        layout_scale = self._synchronize_dpi_scale(actual_dpi_scale)
        self._layout_popup(
            config,
            statuses,
            anchor,
            work_area,
            icons,
            layout_scale=layout_scale,
        )

    def _ensure_content(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        *,
        columns: int,
        viewport_height: int,
        render_viewport_height: int | None,
        icons: Mapping[str, ctk.CTkImage],
    ) -> bool:
        signature = self._content_signature(config)
        scrolling = render_viewport_height is not None
        if signature != self._render_signature:
            self._render_content(
                config,
                statuses,
                columns=columns,
                viewport_height=viewport_height,
                scrolling=scrolling,
                icons=icons,
            )
            self._render_signature = signature
            self._dynamic_signature = self._dynamic_content_signature(
                config, statuses, icons
            )
            self._card_dynamic_states = {
                item.id: self._dynamic_item_state(item, statuses, icons)
                for item in sorted(config.items, key=lambda value: value.order)
            }
            return True

        layout_changed = self._apply_layout(
            columns=columns,
            viewport_height=viewport_height,
            scrolling=scrolling,
        )
        previous_dynamic_signature = self._dynamic_signature
        self._update_dynamic_content(config, statuses, icons)
        return layout_changed or self._dynamic_signature != previous_dynamic_signature

    @staticmethod
    def _content_signature(config: LauncherConfig) -> tuple[object, ...]:
        ordered_items = sorted(config.items, key=lambda value: value.order)
        return tuple(
            (
                item.id,
                item.name,
                item.path,
                item.type,
                item.order,
            )
            for item in ordered_items
        )

    @staticmethod
    def _dynamic_content_signature(
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        icons: Mapping[str, ctk.CTkImage],
    ) -> tuple[object, ...]:
        return tuple(
            (item.id, *PopupPanel._dynamic_item_state(item, statuses, icons))
            for item in sorted(config.items, key=lambda value: value.order)
        )

    @staticmethod
    def _dynamic_item_state(
        item: LauncherItem,
        statuses: Mapping[str, PathStatus],
        icons: Mapping[str, ctk.CTkImage],
    ) -> tuple[object, ...]:
        status = statuses.get(item.id)
        normalized_status = None if status in (None, PathStatus.VALID) else status
        return normalized_status, icons.get(icon_key(item.path, item.type))

    def _update_dynamic_content(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        icons: Mapping[str, ctk.CTkImage],
    ) -> None:
        signature = self._dynamic_content_signature(config, statuses, icons)
        if signature == self._dynamic_signature:
            return
        ordered_items = sorted(config.items, key=lambda value: value.order)
        if len(ordered_items) != len(self._cards):
            return
        current_states: dict[str, tuple[object, ...]] = {}
        for card, item in zip(self._cards, ordered_items):
            state = self._dynamic_item_state(item, statuses, icons)
            current_states[item.id] = state
            if self._card_dynamic_states.get(item.id) == state:
                continue
            status = statuses.get(item.id)
            card.update_state(
                item=item,
                status=status,
                command=self._item_command(item.id, status),
                icon=icons.get(icon_key(item.path, item.type)),
            )
        self._card_dynamic_states = current_states
        self._dynamic_signature = signature
        self._dynamic_update_count += 1

    def _render_content(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        *,
        columns: int,
        viewport_height: int,
        scrolling: bool,
        icons: Mapping[str, ctk.CTkImage],
    ) -> None:
        self._clear()
        self._render_count += 1
        ordered_items = tuple(sorted(config.items, key=lambda value: value.order))
        self._search_index = LauncherSearchIndex(ordered_items)

        shell = ctk.CTkFrame(
            self,
            corner_radius=WINDOW_RADIUS,
            border_width=BORDER_WIDTH,
            border_color=BORDER,
            fg_color=SURFACE,
            bg_color=self._window_background,
        )
        shell.pack(fill="both", expand=True)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(1, weight=1)

        self._build_header(shell, len(config.items), compact=columns == 1)

        # Keep one scroll-capable container for the lifetime of this item tree.
        # Switching monitor width, work-area height, or DPI can then re-grid
        # the existing cards and show/hide its scrollbar without replacing any
        # widgets on the hotkey path.
        items_frame = _OwnedScrollableFrame(
            shell,
            height=viewport_height,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED,
        )
        self._items_frame = items_frame
        items_frame.grid(row=1, column=0, padx=PADDING, pady=(0, PADDING), sticky="nsew")

        self._cards = []
        self._card_by_item_id = {}
        if not config.items:
            self._add_empty_state(items_frame)
        else:
            for index, item in enumerate(ordered_items):
                self._add_item_button(
                    items_frame,
                    item,
                    statuses.get(item.id),
                    index,
                    icons.get(icon_key(item.path, item.type)),
                )
            self._add_search_empty_state(items_frame)
        self._visible_cards = list(self._cards)
        self._layout_signature = None
        self._apply_layout(
            columns=columns,
            viewport_height=viewport_height,
            scrolling=scrolling,
        )

    def _apply_layout(
        self,
        *,
        columns: int,
        viewport_height: int,
        scrolling: bool,
    ) -> bool:
        items_frame = self._items_frame
        if items_frame is None:
            return False
        self._layout_viewport_height = viewport_height
        visible_count = len(self._visible_cards)
        visible_height = (
            BUTTON_HEIGHT
            if self._empty_state is not None or self._showing_search_empty
            else max(1, math.ceil(visible_count / max(1, columns))) * BUTTON_HEIGHT
            + max(
                0,
                math.ceil(visible_count / max(1, columns)) - 1,
            )
            * GAP
        )
        effective_scrolling = scrolling and visible_height > viewport_height
        widget_scaling = round(float(items_frame._get_widget_scaling()), 4)
        signature = (
            columns,
            viewport_height,
            effective_scrolling,
            widget_scaling,
        )
        if signature == self._layout_signature:
            return False

        initial_layout = self._layout_signature is None
        previous_columns = self._layout_columns
        columns_changed = columns != previous_columns
        self._layout_columns = max(1, columns)
        self._layout_rows = max(
            1,
            math.ceil(len(self._visible_cards) / self._layout_columns),
        )
        items_frame.configure(height=viewport_height)
        self._set_scrolling(effective_scrolling)

        self._update_header_layout(compact=self._layout_columns == 1)
        if initial_layout or columns_changed:
            self._regrid_cards()
        else:
            # CustomTkinter rescales place-managed cards on a DPI change, but
            # the raw tkinter.Frame height below is not tracked by its scaling
            # callbacks. Keep the canvas window extent in step with the cards
            # even when the effective column count itself does not change.
            self._sync_content_extent()
        self._layout_signature = signature
        return True

    def _regrid_cards(self) -> None:
        items_frame = self._items_frame
        if items_frame is None:
            return
        for card in self._cards:
            card.place_forget()
        if self._empty_state is not None:
            if self._search_empty_state is not None:
                self._search_empty_state.place_forget()
            self._empty_state.place(x=0, y=0, relwidth=1.0)
            self._sync_content_extent()
            return

        if self._search_empty_state is not None:
            if self._showing_search_empty:
                self._search_empty_state.place(x=0, y=0, relwidth=1.0)
                self._sync_content_extent()
                return
            self._search_empty_state.place_forget()

        for index, card in enumerate(self._visible_cards):
            row, column = divmod(index, self._layout_columns)
            card.place(
                x=column * (BUTTON_WIDTH + GAP),
                y=row * (BUTTON_HEIGHT + GAP),
            )
        self._sync_content_extent()

    def _sync_content_extent(self) -> None:
        items_frame = self._items_frame
        if items_frame is None:
            return
        content_height = (
            BUTTON_HEIGHT
            if self._empty_state is not None or self._showing_search_empty
            else self._layout_rows * BUTTON_HEIGHT
            + max(0, self._layout_rows - 1) * GAP
        )
        tk.Frame.configure(
            items_frame,
            height=items_frame._apply_widget_scaling(content_height),
        )

    def _set_scrolling(self, scrolling: bool) -> None:
        items_frame = self._items_frame
        if items_frame is None:
            return
        scrollbar = items_frame._scrollbar
        canvas = items_frame._parent_canvas
        if scrolling:
            if not scrollbar.winfo_manager():
                scrollbar.grid()
            self._scroll_canvas = canvas
        else:
            if scrollbar.winfo_manager():
                scrollbar.grid_remove()
            canvas.yview_moveto(0.0)
            self._scroll_canvas = None
        self._scrolling = scrolling

    def _settle_dpi(
        self,
        show_generation: int,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        anchor: Point,
        work_area: Rect,
        *,
        applied_scale: float,
        attempts_remaining: int,
        icons: Mapping[str, ctk.CTkImage] | None = None,
    ) -> None:
        if show_generation != self._show_generation or not self._visible:
            return
        current_scale = self._get_window_scaling()
        if abs(current_scale - applied_scale) > 0.01:
            # WM_DPICHANGED has settled after crossing monitors. Reflow the
            # existing cards for the effective column count and viewport.
            self.show(config, statuses, anchor, work_area, icons=icons)
            return
        if attempts_remaining:
            self.after(
                80,
                lambda: self._settle_dpi(
                    show_generation,
                    config,
                    statuses,
                    anchor,
                    work_area,
                    applied_scale=applied_scale,
                    attempts_remaining=attempts_remaining - 1,
                    icons=icons,
                ),
            )

    def _finish_show(self, show_generation: int) -> None:
        if show_generation != self._show_generation or not self._visible:
            return
        try:
            self.focus_force()
            self._focus_search()
        finally:
            # DWM uncloaking can deliver a delayed FocusOut after focus_force
            # has already returned.  An idle callback is too short on slower
            # mixed-DPI desktops, so protect the initial native transition for
            # a small bounded interval before outside-focus checks may hide it.
            self._cancel_focus_arming()
            self._focus_arm_after = self.after(
                _FOCUS_ARMING_MS,
                lambda: self._release_focus_arming(show_generation),
            )

    def _release_focus_arming(self, show_generation: int) -> None:
        self._focus_arm_after = None
        if (
            show_generation == self._show_generation
            and self._visible
            and self._focus_arming_generation == show_generation
        ):
            self._focus_arming_generation = None

    def _cancel_focus_arming(self) -> None:
        after_id, self._focus_arm_after = self._focus_arm_after, None
        if after_id is None:
            return
        try:
            self.after_cancel(after_id)
        except tk.TclError:
            pass

    def _build_header(
        self,
        shell: ctk.CTkFrame,
        item_count: int,
        *,
        compact: bool,
    ) -> None:
        header = ctk.CTkFrame(
            shell,
            height=HEADER_HEIGHT,
            fg_color="transparent",
            corner_radius=0,
        )
        header.grid(row=0, column=0, padx=PADDING, pady=(PADDING, HEADER_GAP), sticky="ew")
        header.grid_columnconfigure(2, weight=1)
        header.grid_propagate(False)

        for size in (24, 30):
            if size not in self._brand_images:
                self._brand_images[size] = brand_image(size)
        mark_size = 24 if compact else 30
        brand_mark = ctk.CTkLabel(
            header,
            text="",
            image=self._brand_images[mark_size],
            width=mark_size,
            height=mark_size,
            fg_color="transparent",
        )
        brand_mark.grid(row=0, column=0, padx=(0, 6 if compact else 9))
        self._brand_mark = brand_mark
        brand_label = ctk.CTkLabel(
            header,
            text="QuickAccess",
            font=font(12 if compact else 14, "bold"),
            text_color=TEXT,
            anchor="w",
        )
        brand_label.grid(row=0, column=1, sticky="w")
        self._brand_label = brand_label

        self._search_variable = tk.StringVar(master=self, value="")
        search_entry = ctk.CTkEntry(
            header,
            textvariable=self._search_variable,
            height=32,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
            fg_color=SURFACE_ALT,
            text_color=TEXT,
            placeholder_text="바로가기 검색",
            placeholder_text_color=MUTED,
            font=font(11),
        )
        search_entry.grid(row=0, column=2, padx=(10, 8), sticky="ew")
        search_entry._entry.configure(takefocus=True)
        search_entry.bind("<Up>", lambda _event: self._focus_search_result(-1), add="+")
        search_entry.bind("<Down>", lambda _event: self._focus_search_result(1), add="+")
        search_entry.bind("<Return>", self._activate_first_search_result, add="+")
        search_entry.bind("<KP_Enter>", self._activate_first_search_result, add="+")
        self._search_entry = search_entry
        self._search_trace_id = self._search_variable.trace_add(
            "write",
            self._on_search_changed,
        )

        count_badge = ctk.CTkLabel(
            header,
            text=f"{item_count}개",
            width=42,
            height=24,
            corner_radius=12,
            fg_color=ACCENT_SOFT,
            text_color=ACCENT,
            font=font(10, "bold"),
        )
        if not compact:
            count_badge.grid(row=0, column=3, padx=(0, 7))
        self._count_badge = count_badge
        settings_button = ctk.CTkButton(
            header,
            text=_GLYPH_SETTINGS,
            width=28 if compact else 32,
            height=28 if compact else 32,
            corner_radius=9 if compact else 10,
            font=icon_font(14 if compact else 15),
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            text_color=MUTED,
            command=self._open_settings,
        )
        settings_button.grid(row=0, column=4)
        self._settings_button = settings_button
        self._enable_keyboard_button(settings_button)
        self._header_compact = None
        self._update_header_layout(compact=compact)

    def _update_header_layout(self, *, compact: bool) -> None:
        if compact == self._header_compact:
            return
        if (
            self._brand_mark is None
            or self._brand_label is None
            or self._count_badge is None
            or self._settings_button is None
            or self._search_entry is None
        ):
            return

        mark_size = 24 if compact else 30
        self._brand_mark.configure(
            image=self._brand_images[mark_size],
            width=mark_size,
            height=mark_size,
        )
        self._brand_mark.grid_configure(padx=(0, 6 if compact else 9))
        self._brand_label.configure(font=font(12 if compact else 14, "bold"))
        if compact:
            self._brand_mark.grid_remove()
            self._brand_label.grid_remove()
            self._count_badge.grid_remove()
            self._search_entry.grid_configure(
                row=0,
                column=0,
                columnspan=4,
                padx=(0, 6),
            )
        else:
            self._brand_mark.grid(row=0, column=0, padx=(0, 9))
            self._brand_label.grid(row=0, column=1, sticky="w")
            self._search_entry.grid_configure(
                row=0,
                column=2,
                columnspan=1,
                padx=(10, 8),
            )
            self._count_badge.grid(row=0, column=3, padx=(0, 7))
        self._settings_button.configure(
            width=28 if compact else 32,
            height=28 if compact else 32,
            corner_radius=9 if compact else 10,
            font=icon_font(14 if compact else 15),
        )
        self._header_compact = compact

    def _add_empty_state(self, parent: ctk.CTkFrame | ctk.CTkScrollableFrame) -> None:
        empty = ctk.CTkFrame(
            parent,
            height=BUTTON_HEIGHT,
            corner_radius=CARD_RADIUS,
            border_width=BORDER_WIDTH,
            border_color=BORDER,
            fg_color=SURFACE_ALT,
        )
        empty.grid(row=0, column=0, sticky="nsew")
        empty.grid_propagate(False)
        self._empty_state = empty
        ctk.CTkLabel(
            empty,
            text="등록된 항목이 없습니다",
            font=font(12, "bold"),
            text_color=TEXT,
        ).pack(pady=(12, 0))
        ctk.CTkLabel(
            empty,
            text="설정에서 폴더나 파일을 추가하세요.",
            font=font(10),
            text_color=MUTED,
        ).pack(pady=(0, 10))

    def _add_search_empty_state(
        self,
        parent: ctk.CTkFrame | ctk.CTkScrollableFrame,
    ) -> None:
        empty = ctk.CTkFrame(
            parent,
            height=BUTTON_HEIGHT,
            corner_radius=CARD_RADIUS,
            border_width=BORDER_WIDTH,
            border_color=BORDER,
            fg_color=SURFACE_ALT,
        )
        empty.grid_propagate(False)
        empty.grid_columnconfigure(0, weight=1)
        self._search_empty_state = empty
        ctk.CTkLabel(
            empty,
            text="검색 결과가 없습니다",
            font=font(11, "bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, pady=(7, 1))
        button = ctk.CTkButton(
            empty,
            text="설정에서 추가",
            width=92,
            height=26,
            corner_radius=8,
            fg_color=ACCENT_SOFT,
            hover_color=SURFACE_HOVER,
            text_color=ACCENT,
            font=font(9, "bold"),
            command=self._open_settings,
        )
        button.grid(row=1, column=0, pady=(0, 7))
        self._enable_keyboard_button(button)
        self._search_empty_button = button

    def _on_search_changed(self, *_trace_arguments: str) -> None:
        variable = self._search_variable
        if variable is not None:
            self._apply_search_filter(variable.get())

    def _apply_search_filter(self, query: str) -> None:
        search_index = self._search_index
        if search_index is None or self._items_frame is None:
            return

        matches = search_index.search(query)
        self._visible_cards = [
            card
            for item in matches
            if (card := self._card_by_item_id.get(item.id)) is not None
        ]
        self._showing_search_empty = bool(query.split()) and not self._visible_cards and bool(
            self._cards
        )
        self._layout_rows = max(
            1,
            math.ceil(len(self._visible_cards) / max(1, self._layout_columns)),
        )
        self._regrid_cards()

        canvas = self._items_frame._parent_canvas
        canvas.yview_moveto(0.0)
        content_height = (
            BUTTON_HEIGHT
            if self._showing_search_empty or self._empty_state is not None
            else self._layout_rows * BUTTON_HEIGHT
            + max(0, self._layout_rows - 1) * GAP
        )
        scrolling = content_height > self._layout_viewport_height
        self._set_scrolling(scrolling)
        widget_scaling = round(float(self._items_frame._get_widget_scaling()), 4)
        self._layout_signature = (
            self._layout_columns,
            self._layout_viewport_height,
            scrolling,
            widget_scaling,
        )
        if self._count_badge is not None:
            self._count_badge.configure(text=f"{len(self._visible_cards)}개")

    def _reset_search_for_show(self) -> None:
        variable = self._search_variable
        if variable is None:
            return
        if variable.get():
            variable.set("")
        elif self._visible_cards != self._cards or self._showing_search_empty:
            self._apply_search_filter("")

    def _focus_search(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> str:
        entry = self._search_entry
        if entry is not None:
            try:
                entry._entry.focus_set()
                entry._entry.icursor("end")
            except tk.TclError:
                pass
        return "break"

    def _focus_search_result(self, direction: int) -> str:
        if not self._visible_cards:
            return "break"
        return self._focus_card(0 if direction >= 0 else len(self._visible_cards) - 1)

    def _activate_first_search_result(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> str:
        if self._visible_cards:
            self._visible_cards[0]._invoke()
        return "break"

    def _on_escape(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        variable = self._search_variable
        if variable is not None and variable.get():
            variable.set("")
            self._focus_search()
        else:
            self.hide()
        return "break"

    def _add_item_button(
        self,
        parent: ctk.CTkFrame | ctk.CTkScrollableFrame,
        item: LauncherItem,
        status: PathStatus | None,
        index: int,
        icon: ctk.CTkImage | None = None,
    ) -> None:
        card = _LauncherCard(
            parent,
            item=item,
            status=status,
            command=self._item_command(item.id, status),
            on_context_menu=lambda event, name=item.name, path=item.path: (
                self._show_context_menu(event, name, path)
            ),
            icon=icon,
        )
        card.bind(
            "<Up>",
            lambda _event, value=card: self._navigate_from_card(value, "up"),
            add="+",
        )
        card.bind(
            "<Down>",
            lambda _event, value=card: self._navigate_from_card(value, "down"),
            add="+",
        )
        card.bind(
            "<Left>",
            lambda _event, value=card: self._navigate_from_card(value, "left"),
            add="+",
        )
        card.bind(
            "<Right>",
            lambda _event, value=card: self._navigate_from_card(value, "right"),
            add="+",
        )
        card.bind("<Home>", lambda _event: self._focus_card(0), add="+")
        card.bind(
            "<End>",
            lambda _event: self._focus_card(len(self._visible_cards) - 1),
            add="+",
        )
        card.bind(
            "<Prior>",
            lambda _event, value=card: self._page_navigate_from_card(value, -1),
            add="+",
        )
        card.bind(
            "<Next>",
            lambda _event, value=card: self._page_navigate_from_card(value, 1),
            add="+",
        )
        self._cards.append(card)
        self._card_by_item_id[item.id] = card

    def _item_command(
        self,
        item_id: str,
        status: PathStatus | None,
    ) -> Callable[[], None]:
        if status in (PathStatus.MISSING, PathStatus.ERROR):
            return lambda value=item_id: self._relocate(value)
        return lambda value=item_id: self._activate(value)

    def _navigate(self, index: int, direction: str) -> str:
        row, column = divmod(index, self._layout_columns)
        target = grid_navigation_target(
            row,
            column,
            direction,
            self._layout_columns,
            len(self._visible_cards),
        )
        if target is not None and 0 <= target < len(self._visible_cards):
            self._focus_card(target)
        elif direction == "up" and row == 0:
            self._focus_search()
        return "break"

    def _navigate_from_card(self, card: _LauncherCard, direction: str) -> str:
        try:
            index = self._visible_cards.index(card)
        except ValueError:
            return "break"
        return self._navigate(index, direction)

    def _page_navigate_from_card(
        self,
        card: _LauncherCard,
        direction: int,
    ) -> str:
        try:
            index = self._visible_cards.index(card)
        except ValueError:
            return "break"
        return self._page_navigate(index, direction)

    def _page_navigate(self, index: int, direction: int) -> str:
        if not self._visible_cards:
            return "break"
        row, column = divmod(index, self._layout_columns)
        viewport_height = (
            self._scroll_canvas.winfo_height()
            if self._scroll_canvas is not None
            else self._visible_cards[0].winfo_height()
        )
        if len(self._visible_cards) > self._layout_columns:
            row_pitch = max(
                1,
                self._visible_cards[self._layout_columns].winfo_y()
                - self._visible_cards[0].winfo_y(),
            )
        else:
            row_pitch = max(1, self._visible_cards[0].winfo_height())
        rows_per_page = max(1, viewport_height // row_pitch)
        last_row = max(0, self._layout_rows - 1)
        target_row = max(0, min(last_row, row + direction * rows_per_page))
        target = min(
            target_row * self._layout_columns + column,
            len(self._visible_cards) - 1,
        )
        return self._focus_card(target)

    def _focus_card(self, index: int) -> str:
        if 0 <= index < len(self._visible_cards):
            self._visible_cards[index].focus_set()
            self.after_idle(lambda value=index: self._scroll_card_into_view(value))
        return "break"

    def _scroll_card_into_view(self, index: int) -> None:
        canvas = self._scroll_canvas
        if canvas is None or not 0 <= index < len(self._visible_cards):
            return
        try:
            card = self._visible_cards[index]
            canvas.update_idletasks()
            bounds = canvas.bbox("all")
            if bounds is None:
                return
            content_top, content_bottom = bounds[1], bounds[3]
            content_height = max(1, content_bottom - content_top)
            card_top = card.winfo_y()
            card_bottom = card_top + card.winfo_height()
            viewport_top = canvas.canvasy(0)
            viewport_height = canvas.winfo_height()
            viewport_bottom = viewport_top + viewport_height
            if card_top < viewport_top:
                target_top = card_top
            elif card_bottom > viewport_bottom:
                target_top = card_bottom - viewport_height
            else:
                return
            fraction = (target_top - content_top) / content_height
            canvas.yview_moveto(max(0.0, min(1.0, fraction)))
        except tk.TclError:
            return

    def _show_context_menu(
        self,
        event: tk.Event[tk.Misc],
        name: str,
        path: str,
    ) -> str:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(
            label=f"'{_ellipsize(name, 18)}' 경로 복사",
            command=lambda: self._copy_path(path),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _copy_path(self, path: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(path)
        except tk.TclError:
            pass

    def _activate(self, item_id: str) -> None:
        self.hide()
        self._actions.activate(item_id)

    def _relocate(self, item_id: str) -> None:
        self.hide()
        self._actions.relocate(item_id)

    def _open_settings(self) -> None:
        self.hide()
        self._actions.open_settings()

    def _on_focus_out(self, _event: tk.Event[tk.Misc]) -> None:
        if self._visible:
            self._schedule_focus_loss_check(self._show_generation)

    def _schedule_focus_loss_check(self, show_generation: int) -> None:
        self._cancel_focus_loss_check()
        self._focus_loss_after = self.after(
            _FOCUS_LOSS_SETTLE_MS,
            lambda: self._run_focus_loss_check(show_generation),
        )

    def _run_focus_loss_check(self, show_generation: int) -> None:
        self._focus_loss_after = None
        if show_generation != self._show_generation or not self._visible:
            return
        if self._focus_arming_generation == show_generation:
            self._schedule_focus_loss_check(show_generation)
            return
        self._hide_if_focus_left(show_generation)

    def _cancel_focus_loss_check(self) -> None:
        after_id, self._focus_loss_after = self._focus_loss_after, None
        if after_id is None:
            return
        try:
            self.after_cancel(after_id)
        except tk.TclError:
            pass

    def _hide_if_focus_left(self, show_generation: int | None = None) -> None:
        if (
            not self._visible
            or (
                show_generation is not None
                and show_generation != self._show_generation
            )
        ):
            return
        try:
            focused = self.focus_get()
            if focused is not None and str(focused).startswith(str(self)):
                return
        except tk.TclError:
            pass
        self.hide()

    def hide(self) -> None:
        if self._visible:
            self._show_generation += 1
            self._cancel_focus_arming()
            self._focus_arming_generation = None
            self._cancel_focus_loss_check()
            self._visible = False
            if self._warm_mapped and self._set_native_cloak(True):
                return
            self._warm_mapped = False
            self._native_cloak_available = False
            self.withdraw()

    def destroy(self) -> None:
        if self.winfo_exists():
            self._cancel_focus_arming()
            self._cancel_focus_loss_check()
            if self._warm_mapped:
                self._set_native_cloak(True)
            self._clear()
        super().destroy()

    def _clear(self) -> None:
        if self._search_variable is not None and self._search_trace_id is not None:
            try:
                self._search_variable.trace_remove("write", self._search_trace_id)
            except tk.TclError:
                pass
        items_frame = self._items_frame
        if items_frame is not None:
            try:
                items_frame.destroy()
            except tk.TclError:
                pass
        self._render_signature = None
        self._dynamic_signature = None
        self._card_dynamic_states = {}
        self._layout_signature = None
        self._layout_columns = 1
        self._layout_rows = 1
        self._layout_viewport_height = BUTTON_HEIGHT
        self._scrolling = False
        self._cards = []
        self._card_by_item_id = {}
        self._visible_cards = []
        self._search_index = None
        self._search_variable = None
        self._search_trace_id = None
        self._search_entry = None
        self._search_empty_state = None
        self._search_empty_button = None
        self._showing_search_empty = False
        self._items_frame = None
        self._scroll_canvas = None
        self._empty_state = None
        self._header_compact = None
        self._brand_mark = None
        self._brand_label = None
        self._count_badge = None
        self._settings_button = None
        for child in self.winfo_children():
            child.destroy()
