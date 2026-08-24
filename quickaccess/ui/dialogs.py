"""Small modal dialogs and non-blocking toast windows."""

from __future__ import annotations

from collections.abc import Callable
import math
import tkinter as tk

import customtkinter as ctk

from ..services.monitor import (
    NativeMonitorService,
    Point,
    Rect,
    Size,
    center_window_in_work_area,
    clamp_window_to_work_area,
)
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    BG,
    BORDER,
    DANGER,
    MUTED,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    SURFACE_HOVER,
    TEXT,
    WARNING,
    font,
)


# Kept as a compatibility alias for callers outside the package.
FONT = font(12)


def position_geometry(x: int, y: int) -> str:
    """Format a signed Tk position for monitors with negative coordinates."""

    return f"{x:+d}{y:+d}"


class TextInputDialog(ctk.CTkToplevel):
    """A compact, themed input dialog with an editable default value."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        prompt: str,
        initial_value: str = "",
        validator: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self._result: str | None = None
        self._validator = validator
        self._parent = parent

        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=BG)
        parent_is_visible = False
        try:
            parent_is_visible = bool(parent.winfo_viewable())
            if parent_is_visible:
                self.transient(parent)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<Return>", lambda _event: self._confirm())
        self.bind("<Tab>", self._cycle_keyboard_focus, add="+")
        self.bind(
            "<Shift-Tab>",
            lambda event: self._cycle_keyboard_focus(event, reverse=True),
            add="+",
        )

        self._card = ctk.CTkFrame(
            self,
            fg_color=SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        card = self._card
        card.grid(row=0, column=0, padx=12, pady=12, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)

        self._prompt_label = ctk.CTkLabel(
            card,
            text=prompt,
            font=font(13, "bold"),
            text_color=TEXT,
            anchor="w",
        )
        self._prompt_label.grid(
            row=0,
            column=0,
            columnspan=2,
            padx=22,
            pady=(20, 4),
            sticky="ew",
        )
        self._helper_label = ctk.CTkLabel(
            card,
            text="설정에서 언제든 다시 변경할 수 있습니다.",
            font=font(10),
            text_color=MUTED,
            anchor="w",
        )
        self._helper_label.grid(
            row=1,
            column=0,
            columnspan=2,
            padx=22,
            pady=(0, 13),
            sticky="ew",
        )

        self._entry = ctk.CTkEntry(
            card,
            width=390,
            height=40,
            corner_radius=10,
            fg_color=SURFACE_ALT,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self._entry.grid(row=2, column=0, columnspan=2, padx=22, sticky="ew")
        self._entry._entry.configure(takefocus=True)
        self._entry.insert(0, initial_value)
        self._entry.select_range(0, "end")

        self._error = ctk.CTkLabel(
            card,
            text="",
            font=font(10),
            text_color=DANGER,
            anchor="w",
        )
        self._error.grid(row=3, column=0, columnspan=2, padx=22, pady=(6, 0), sticky="ew")
        self._error.grid_remove()

        self._cancel_button = ctk.CTkButton(
            card,
            text="취소",
            width=88,
            height=36,
            corner_radius=9,
            font=font(11, "bold"),
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            command=self._cancel,
        )
        self._cancel_button.grid(
            row=4,
            column=0,
            padx=(22, 6),
            pady=(18, 20),
            sticky="e",
        )
        self._confirm_button = ctk.CTkButton(
            card,
            text="확인",
            width=88,
            height=36,
            corner_radius=9,
            font=font(11, "bold"),
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._confirm,
        )
        self._confirm_button.grid(
            row=4,
            column=1,
            padx=(0, 22),
            pady=(18, 20),
            sticky="e",
        )
        self._enable_keyboard_button(self._cancel_button)
        self._enable_keyboard_button(self._confirm_button)

        self.update_idletasks()
        self._center_over_parent(parent)
        self.deiconify()
        self.lift()
        if not parent_is_visible:
            try:
                self.attributes("-topmost", True)
            except tk.TclError:
                pass
        self.grab_set()
        self.after(20, self._entry.focus_force)

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

    def _cycle_keyboard_focus(
        self,
        _event: tk.Event[tk.Misc],
        *,
        reverse: bool = False,
    ) -> str:
        targets = [
            self._entry._entry,
            self._cancel_button._canvas,
            self._confirm_button._canvas,
        ]
        focused = self.focus_get()
        try:
            current = targets.index(focused) if focused is not None else (-1 if not reverse else 0)
        except ValueError:
            current = -1 if not reverse else 0
        offset = -1 if reverse else 1
        targets[(current + offset) % len(targets)].focus_set()
        return "break"

    def _center_over_parent(self, parent: tk.Misc) -> None:
        monitor = NativeMonitorService()
        parent_is_visible = False
        try:
            parent_is_visible = bool(parent.winfo_viewable())
            if parent_is_visible:
                anchor = Point(
                    parent.winfo_rootx() + parent.winfo_width() // 2,
                    parent.winfo_rooty() + parent.winfo_height() // 2,
                )
            else:
                cursor = monitor.get_cursor_position()
                anchor = cursor
            work_area = monitor.get_monitor_work_area(anchor)
        except Exception:
            work_area = Rect(
                0,
                0,
                max(1, self.winfo_screenwidth()),
                max(1, self.winfo_screenheight()),
            )

        scale = max(0.1, self._get_window_scaling())
        available_width = max(1, int((work_area.width - 24) / scale))
        available_height = max(1, int((work_area.height - 24) / scale))
        compact = available_width < 460 or available_height < 280
        outer_padding = 8 if compact else 12
        inner_padding = 16 if compact else 22
        content_width = max(
            150,
            min(390, available_width - outer_padding * 2 - inner_padding * 2),
        )

        self._card.grid_configure(
            padx=outer_padding,
            pady=outer_padding,
        )
        self._prompt_label.grid_configure(
            padx=inner_padding,
            pady=(12 if compact else 20, 4),
        )
        self._helper_label.grid_configure(
            padx=inner_padding,
            pady=(0, 9 if compact else 13),
        )
        self._entry.configure(width=content_width, height=36 if compact else 40)
        self._entry.grid_configure(padx=inner_padding)
        self._error.configure(wraplength=content_width)
        self._error.grid_configure(padx=inner_padding)
        self._prompt_label.configure(wraplength=content_width)
        self._helper_label.configure(wraplength=content_width)
        self._cancel_button.configure(
            width=80 if compact else 88,
            height=34 if compact else 36,
        )
        self._confirm_button.configure(
            width=80 if compact else 88,
            height=34 if compact else 36,
        )
        self._cancel_button.grid_configure(
            padx=(inner_padding, 6),
            pady=(12 if compact else 18, 12 if compact else 20),
        )
        self._confirm_button.grid_configure(
            padx=(0, inner_padding),
            pady=(12 if compact else 18, 12 if compact else 20),
        )
        self.update_idletasks()

        # ``winfo_req*`` reports physical pixels, while CTk scales explicit
        # geometry dimensions.  Limit the logical size before positioning.
        requested_width = max(1, math.ceil(self.winfo_reqwidth() / scale))
        requested_height = max(1, math.ceil(self.winfo_reqheight() / scale))
        logical_width = min(requested_width, available_width)
        logical_height = min(requested_height, available_height)
        rendered = Size(
            self._apply_window_scaling(logical_width),
            self._apply_window_scaling(logical_height),
        )
        if parent_is_visible:
            desired = Point(
                parent.winfo_rootx() + (parent.winfo_width() - rendered.width) // 2,
                parent.winfo_rooty() + (parent.winfo_height() - rendered.height) // 2,
            )
            position = clamp_window_to_work_area(desired, rendered, work_area)
        else:
            position = center_window_in_work_area(rendered, work_area)
        self.geometry(
            f"{logical_width}x{logical_height}"
            f"{position_geometry(position.x, position.y)}"
        )

    def _confirm(self) -> None:
        value = self._entry.get().strip()
        error = self._validator(value) if self._validator is not None else None
        if error:
            self._error.configure(text=error)
            self._error.grid()
            self._entry.configure(border_color=DANGER)
            self.update_idletasks()
            self._center_over_parent(self._parent)
            self._entry.focus_force()
            return
        self._result = value
        self.destroy()

    def _cancel(self) -> None:
        self._result = None
        self.destroy()

    def show(self) -> str | None:
        self.wait_window()
        return self._result


def ask_display_name(
    parent: tk.Misc,
    initial_value: str,
    *,
    title: str = "표시명 입력",
) -> str | None:
    return TextInputDialog(
        parent,
        title=title,
        prompt="런처에 표시할 이름을 입력하세요.",
        initial_value=initial_value,
        validator=lambda value: None if value else "표시명을 입력해 주세요.",
    ).show()


class ToastManager:
    """Show one compact neutral toast with a semantic status accent."""

    _STYLES = {
        "info": (ACCENT, "i"),
        "success": (SUCCESS, "✓"),
        "warning": (WARNING, "!"),
        "error": (DANGER, "×"),
    }

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._window: ctk.CTkToplevel | None = None
        self._dismiss_after: str | None = None

    def show(
        self,
        message: str,
        *,
        kind: str = "info",
        duration_ms: int = 3200,
        action_text: str | None = None,
        action_command: Callable[[], None] | None = None,
    ) -> None:
        self.close()
        window = ctk.CTkToplevel(self._root)
        self._window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(fg_color=BG)

        color, symbol = self._STYLES.get(kind, self._STYLES["info"])
        estimated_lines = sum(
            max(1, math.ceil(len(line) / 42)) for line in (message.splitlines() or [""])
        )
        has_action = bool(action_text) and action_command is not None
        base_width = 420 if not has_action else 460
        base_height = min(180, 64 + max(0, estimated_lines - 1) * 18)
        card = ctk.CTkFrame(
            window,
            width=base_width,
            height=base_height,
            corner_radius=12,
            fg_color=SURFACE,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="both", expand=True)
        card.grid_propagate(False)
        card.grid_columnconfigure(2, weight=1)
        card.grid_rowconfigure(0, weight=1)
        ctk.CTkFrame(
            card,
            width=4,
            corner_radius=2,
            fg_color=color,
        ).grid(row=0, column=0, padx=(4, 0), pady=5, sticky="ns")
        ctk.CTkLabel(
            card,
            text=symbol,
            width=26,
            height=26,
            corner_radius=13,
            fg_color=SURFACE_ALT,
            text_color=color,
            font=font(13, "bold"),
        ).grid(row=0, column=1, padx=(12, 10), pady=12)
        message_label = ctk.CTkLabel(
            card,
            text=message,
            font=font(11),
            text_color=TEXT,
            justify="left",
            anchor="w",
            wraplength=base_width - (150 if has_action else 90),
        )
        message_label.grid(row=0, column=2, padx=(0, 6 if has_action else 18), pady=13, sticky="ew")
        if has_action:
            ctk.CTkButton(
                card,
                text=action_text,
                width=76,
                height=30,
                corner_radius=8,
                fg_color=color,
                hover_color=color,
                font=font(10, "bold"),
                command=lambda: self._run_action(action_command),
            ).grid(row=0, column=3, padx=(0, 14), pady=13)

        try:
            monitor = NativeMonitorService()
            cursor = monitor.get_cursor_position()
            work_area = monitor.get_monitor_work_area(cursor)
        except Exception:
            work_area = Rect(
                0,
                0,
                window.winfo_screenwidth(),
                window.winfo_screenheight() - 40,
            )
        self._position_toast(
            window,
            card,
            message_label,
            base_width,
            base_height,
            work_area,
            attempts_remaining=2,
        )
        window.deiconify()
        window.lift()
        self._dismiss_after = window.after(max(500, duration_ms), self.close)

    def _position_toast(
        self,
        window: ctk.CTkToplevel,
        card: ctk.CTkFrame,
        message_label: ctk.CTkLabel,
        base_width: int,
        base_height: int,
        work_area: Rect,
        *,
        attempts_remaining: int,
    ) -> None:
        if window is not self._window:
            return
        scale = max(0.1, window._get_window_scaling())
        width = min(base_width, max(180, int((work_area.width - 24) / scale)))
        height = min(base_height, max(56, int((work_area.height - 24) / scale)))
        rendered = Size(
            window._apply_window_scaling(width),
            window._apply_window_scaling(height),
        )
        x = max(work_area.left + 12, work_area.right - rendered.width - 18)
        y = max(work_area.top + 12, work_area.bottom - rendered.height - 18)
        card.configure(width=width, height=height)
        message_label.configure(wraplength=max(110, width - 90))
        window.geometry(f"{width}x{height}{position_geometry(x, y)}")
        if attempts_remaining:
            window.after(
                80,
                lambda: self._position_toast(
                    window,
                    card,
                    message_label,
                    base_width,
                    base_height,
                    work_area,
                    attempts_remaining=attempts_remaining - 1,
                ),
            )

    def _run_action(self, action_command: Callable[[], None]) -> None:
        self.close()
        action_command()

    def close(self) -> None:
        window, self._window = self._window, None
        self._dismiss_after = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
