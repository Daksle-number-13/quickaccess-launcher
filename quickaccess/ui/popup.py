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

_GLYPH_FOLDER = "\uE8B7"
_GLYPH_FILE = "\uE8A5"
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

    rows = max(1, math.ceil(item_count / columns))
    natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
    vertical_chrome = PADDING * 2 + HEADER_HEIGHT + HEADER_GAP
    natural_height = vertical_chrome + natural_items_height
    max_height = max(100, int((work_area.height - 16) / scale))
    height = min(natural_height, max_height)
    viewport_height = max(BUTTON_HEIGHT, height - vertical_chrome)
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
        return "응답 없음 · 재지정"
    if status is PathStatus.ERROR:
        return "확인 실패 · 재지정"
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
        self._broken = status is not None and status is not PathStatus.VALID
        self._timed_out = status is PathStatus.TIMEOUT
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
        if icon is not None and not self._broken:
            # A real shell icon is only shown once one has actually been
            # extracted; the broken-path glyph coloring always wins so the
            # card still reads as needing attention.
            icon_label = ctk.CTkLabel(icon_tile, text="", image=icon, width=34, height=34)
        else:
            glyph = _GLYPH_FOLDER if item.type == "folder" else _GLYPH_FILE
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
        status_label = ctk.CTkLabel(
            self,
            text=_status_text(item, status),
            height=18,
            font=font(10),
            text_color=self._status_color,
            anchor="w",
        )
        status_label.grid(row=1, column=1, padx=(0, 8), pady=(0, 11), sticky="new")

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


class PopupPanel(ctk.CTkToplevel):
    """A single reusable topmost panel; all methods run on the Tk thread."""

    def __init__(self, root: tk.Misc, actions: PopupActions) -> None:
        super().__init__(root)
        self._actions = actions
        self._visible = False
        self._arming_focus = False
        self._show_generation = 0
        self._render_signature: tuple[object, ...] | None = None
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._cards: list[_LauncherCard] = []
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

    @property
    def visible(self) -> bool:
        return self._visible

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

        signature = self._content_signature(
            config,
            statuses,
            columns,
            render_viewport_height,
            icons,
        )
        if signature != self._render_signature:
            self._render_content(
                config,
                statuses,
                columns=columns,
                viewport_height=viewport_height,
                icons=icons,
            )
            self._render_signature = signature

        rendered_size = Size(
            self._apply_window_scaling(width),
            self._apply_window_scaling(height),
        )
        position = clamp_window_to_work_area(anchor, rendered_size, work_area)
        self.geometry(geometry_string(width, height, position))
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
        signature = self._content_signature(
            config,
            statuses,
            columns,
            render_viewport_height,
            icons,
        )
        if signature == self._render_signature:
            return
        self._render_content(
            config,
            statuses,
            columns=columns,
            viewport_height=viewport_height,
            icons=icons,
        )
        self._render_signature = signature

    @staticmethod
    def _content_signature(
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        columns: int,
        viewport_height: int | None,
        icons: Mapping[str, ctk.CTkImage],
    ) -> tuple[object, ...]:
        ordered_items = sorted(config.items, key=lambda value: value.order)
        return (
            columns,
            viewport_height,
            tuple(
                (
                    item.id,
                    item.name,
                    item.path,
                    item.type,
                    item.order,
                    (
                        None
                        if statuses.get(item.id) in (None, PathStatus.VALID)
                        else statuses.get(item.id)
                    ),
                    icons.get(icon_key(item.path, item.type)),
                )
                for item in ordered_items
            ),
        )

    def _render_content(
        self,
        config: LauncherConfig,
        statuses: Mapping[str, PathStatus],
        *,
        columns: int,
        viewport_height: int,
        icons: Mapping[str, ctk.CTkImage],
    ) -> None:
        self._clear()

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

        rows = max(1, math.ceil(len(config.items) / columns))
        self._layout_columns = columns
        self._layout_rows = rows
        natural_items_height = rows * BUTTON_HEIGHT + max(0, rows - 1) * GAP
        scrolling = natural_items_height > viewport_height
        if scrolling:
            items_frame: ctk.CTkFrame | ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(
                shell,
                height=viewport_height,
                fg_color="transparent",
                corner_radius=0,
                scrollbar_button_color=BORDER,
                scrollbar_button_hover_color=MUTED,
            )
        else:
            items_frame = ctk.CTkFrame(shell, fg_color="transparent", corner_radius=0)
        items_frame.grid(row=1, column=0, padx=PADDING, pady=(0, PADDING), sticky="nsew")
        for column in range(columns):
            items_frame.grid_columnconfigure(column, weight=1, uniform="launcher-card")

        self._cards = []
        if not config.items:
            self._add_empty_state(items_frame)
        else:
            for index, item in enumerate(sorted(config.items, key=lambda value: value.order)):
                self._add_item_button(
                    items_frame,
                    item,
                    statuses.get(item.id),
                    index // columns,
                    index % columns,
                    icons.get(icon_key(item.path, item.type)),
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
            # WM_DPICHANGED has settled after crossing monitors.  Rebuilding
            # once also recalculates the effective column count and viewport.
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
                self._cards[0].focus_set()
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

        brand_mark = ctk.CTkLabel(
            header,
            text="",
            image=brand_image(24 if compact else 30),
            width=24 if compact else 30,
            height=24 if compact else 30,
            fg_color="transparent",
        )
        brand_mark.grid(row=0, column=0, padx=(0, 6 if compact else 9))
        ctk.CTkLabel(
            header,
            text="QuickAccess",
            font=font(12 if compact else 14, "bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        if not compact:
            ctk.CTkLabel(
                header,
                text=f"{item_count}개",
                width=42,
                height=24,
                corner_radius=12,
                fg_color=ACCENT_SOFT,
                text_color=ACCENT,
                font=font(10, "bold"),
            ).grid(row=0, column=2, padx=(8, 7))
        ctk.CTkButton(
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
        ).grid(row=0, column=3)

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
        row: int,
        column: int,
        icon: ctk.CTkImage | None = None,
    ) -> None:
        broken = status is not None and status is not PathStatus.VALID
        callback = (
            (lambda item_id=item.id: self._relocate(item_id))
            if broken
            else (lambda item_id=item.id: self._activate(item_id))
        )
        card = _LauncherCard(
            parent,
            item=item,
            status=status,
            command=callback,
            on_context_menu=lambda event, name=item.name, path=item.path: (
                self._show_context_menu(event, name, path)
            ),
            icon=icon,
        )
        horizontal_padding = (
            GAP // 2 if column > 0 else 0,
            GAP // 2 if column < self._layout_columns - 1 else 0,
        )
        vertical_padding = (
            GAP // 2 if row > 0 else 0,
            GAP // 2 if row < self._layout_rows - 1 else 0,
        )
        card.grid(
            row=row,
            column=column,
            padx=horizontal_padding,
            pady=vertical_padding,
            sticky="nsew",
        )
        card.bind("<Up>", lambda _e, r=row, c=column: self._navigate(r, c, "up"), add="+")
        card.bind("<Down>", lambda _e, r=row, c=column: self._navigate(r, c, "down"), add="+")
        card.bind("<Left>", lambda _e, r=row, c=column: self._navigate(r, c, "left"), add="+")
        card.bind(
            "<Right>", lambda _e, r=row, c=column: self._navigate(r, c, "right"), add="+"
        )
        self._cards.append(card)

    def _navigate(self, row: int, column: int, direction: str) -> str:
        target = grid_navigation_target(row, column, direction, self._layout_columns, len(self._cards))
        if target is not None and 0 <= target < len(self._cards):
            self._cards[target].focus_set()
        return "break"

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

    def _clear(self) -> None:
        self._render_signature = None
        for child in self.winfo_children():
            child.destroy()
