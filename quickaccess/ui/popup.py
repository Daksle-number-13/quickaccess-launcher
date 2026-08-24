"""Cursor-positioned launcher popup."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import tkinter as tk

import customtkinter as ctk

from ..models import LauncherConfig, LauncherItem
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
        self._broken = self._status_state is not None
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
            self._broken = normalized_status is not None
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
    """A single reusable topmost panel; all methods run on the Tk thread."""

    def __init__(self, root: tk.Misc, actions: PopupActions) -> None:
        super().__init__(root)
        self._actions = actions
        self._visible = False
        self._arming_focus = False
        self._show_generation = 0
        self._render_signature: tuple[object, ...] | None = None
        self._dynamic_signature: tuple[object, ...] | None = None
        self._card_dynamic_states: dict[str, tuple[object, ...]] = {}
        self._layout_signature: tuple[int, int, bool, float] | None = None
        self._layout_columns = 1
        self._layout_rows = 1
        self._scrolling = False
        self._render_count = 0
        self._dynamic_update_count = 0
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._cards: list[_LauncherCard] = []
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
        self.bind("<Escape>", lambda _event: self.hide())
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

    def show(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        anchor: Point,
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage] | None = None,
    ) -> None:
        icons = icons or {}
        self._show_generation += 1
        show_generation = self._show_generation
        self._arming_focus = True
        layout_scale = self._get_window_scaling()
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
        self.deiconify()
        self.lift()
        self._visible = True
        self.after_idle(lambda: self._finish_show(show_generation))
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

    def prepare(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        work_area: Rect,
        icons: Mapping[str, ctk.CTkImage] | None = None,
    ) -> None:
        """Build hidden popup content so the first hotkey press is immediate."""

        icons = icons or {}
        layout_scale = self._get_window_scaling()
        _width, _height, columns, viewport_height = popup_dimensions(
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
        if prepared:
            # Realize geometry while the Toplevel is withdrawn.  Otherwise Tk
            # defers initial construction or a hidden re-grid until the first
            # hotkey deiconifies the panel, moving work back onto that path.
            self.update_idletasks()

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
        self._update_dynamic_content(config, statuses, icons)
        return layout_changed

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
        if not config.items:
            self._add_empty_state(items_frame)
        else:
            for index, item in enumerate(sorted(config.items, key=lambda value: value.order)):
                self._add_item_button(
                    items_frame,
                    item,
                    statuses.get(item.id),
                    index,
                    icons.get(icon_key(item.path, item.type)),
                )
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
        widget_scaling = round(float(items_frame._get_widget_scaling()), 4)
        signature = (columns, viewport_height, scrolling, widget_scaling)
        if signature == self._layout_signature:
            return False

        initial_layout = self._layout_signature is None
        previous_columns = self._layout_columns
        columns_changed = columns != previous_columns
        self._layout_columns = max(1, columns)
        self._layout_rows = max(
            1,
            math.ceil(len(self._cards) / self._layout_columns),
        )
        items_frame.configure(height=viewport_height)

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
        if self._empty_state is not None:
            self._empty_state.place(x=0, y=0, relwidth=1.0)
            self._sync_content_extent()
            return

        for index, card in enumerate(self._cards):
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
            if self._empty_state is not None
            else self._layout_rows * BUTTON_HEIGHT
            + max(0, self._layout_rows - 1) * GAP
        )
        tk.Frame.configure(
            items_frame,
            height=items_frame._apply_widget_scaling(content_height),
        )

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
            if self._cards:
                self._focus_card(0)
        finally:
            self._arming_focus = False

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
        header.grid_columnconfigure(1, weight=1)
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
            count_badge.grid(row=0, column=2, padx=(8, 7))
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
        settings_button.grid(row=0, column=3)
        self._settings_button = settings_button
        self._enable_keyboard_button(settings_button)
        self._header_compact = compact

    def _update_header_layout(self, *, compact: bool) -> None:
        if compact == self._header_compact:
            return
        if (
            self._brand_mark is None
            or self._brand_label is None
            or self._count_badge is None
            or self._settings_button is None
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
            self._count_badge.grid_remove()
        else:
            self._count_badge.grid(row=0, column=2, padx=(8, 7))
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
        card.bind("<Up>", lambda _event, value=index: self._navigate(value, "up"), add="+")
        card.bind(
            "<Down>",
            lambda _event, value=index: self._navigate(value, "down"),
            add="+",
        )
        card.bind(
            "<Left>",
            lambda _event, value=index: self._navigate(value, "left"),
            add="+",
        )
        card.bind(
            "<Right>",
            lambda _event, value=index: self._navigate(value, "right"),
            add="+",
        )
        card.bind("<Home>", lambda _event: self._focus_card(0), add="+")
        card.bind("<End>", lambda _event: self._focus_card(len(self._cards) - 1), add="+")
        card.bind(
            "<Prior>",
            lambda _event, value=index: self._page_navigate(value, -1),
            add="+",
        )
        card.bind(
            "<Next>",
            lambda _event, value=index: self._page_navigate(value, 1),
            add="+",
        )
        self._cards.append(card)

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
        target = grid_navigation_target(row, column, direction, self._layout_columns, len(self._cards))
        if target is not None and 0 <= target < len(self._cards):
            self._focus_card(target)
        return "break"

    def _page_navigate(self, index: int, direction: int) -> str:
        if not self._cards:
            return "break"
        row, column = divmod(index, self._layout_columns)
        viewport_height = (
            self._scroll_canvas.winfo_height()
            if self._scroll_canvas is not None
            else self._cards[0].winfo_height()
        )
        if len(self._cards) > self._layout_columns:
            row_pitch = max(
                1,
                self._cards[self._layout_columns].winfo_y()
                - self._cards[0].winfo_y(),
            )
        else:
            row_pitch = max(1, self._cards[0].winfo_height())
        rows_per_page = max(1, viewport_height // row_pitch)
        last_row = max(0, self._layout_rows - 1)
        target_row = max(0, min(last_row, row + direction * rows_per_page))
        target = min(
            target_row * self._layout_columns + column,
            len(self._cards) - 1,
        )
        return self._focus_card(target)

    def _focus_card(self, index: int) -> str:
        if 0 <= index < len(self._cards):
            self._cards[index].focus_set()
            self.after_idle(lambda value=index: self._scroll_card_into_view(value))
        return "break"

    def _scroll_card_into_view(self, index: int) -> None:
        canvas = self._scroll_canvas
        if canvas is None or not 0 <= index < len(self._cards):
            return
        try:
            card = self._cards[index]
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
        if not self._arming_focus:
            generation = self._show_generation
            self.after_idle(lambda: self._hide_if_focus_left(generation))

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
            self._arming_focus = False
            self.withdraw()
            self._visible = False

    def destroy(self) -> None:
        if self.winfo_exists():
            self._clear()
        super().destroy()

    def _clear(self) -> None:
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
        self._scrolling = False
        self._cards = []
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
