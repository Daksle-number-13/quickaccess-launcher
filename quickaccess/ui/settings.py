"""Launcher item and application settings window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ntpath
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..models import LauncherConfig, LauncherItem
from ..services.monitor import NativeMonitorService, Rect, Size, center_window_in_work_area
from .dialogs import ask_display_name, position_geometry
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_SOFT,
    BG,
    BORDER,
    DANGER,
    DANGER_HOVER,
    DANGER_SOFT,
    MUTED,
    SURFACE,
    SURFACE_ALT,
    SURFACE_HOVER,
    TEXT,
    brand_image,
    font,
    icon_font,
)


WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
SIDEBAR_WIDTH = 214
WINDOW_FRAME_BUDGET = 120

ICON_FOLDER = "\ue8b7"
ICON_FILE = "\ue8a5"
ICON_EDIT = "\ue70f"
ICON_DELETE = "\ue74d"

APPEARANCE_LABELS = {
    "system": "시스템",
    "light": "밝게",
    "dark": "어둡게",
}
APPEARANCE_MODES = {label: mode for mode, label in APPEARANCE_LABELS.items()}


def settings_dimensions(work_area_width: int, work_area_height: int, scale: float) -> tuple[int, int]:
    """Return logical client dimensions with room for the native window frame."""

    safe_scale = max(0.1, float(scale))
    available_width = max(
        1,
        int((work_area_width - WINDOW_FRAME_BUDGET) / safe_scale),
    )
    available_height = max(
        1,
        int((work_area_height - WINDOW_FRAME_BUDGET) / safe_scale),
    )
    width = min(WINDOW_WIDTH, available_width)
    height = min(WINDOW_HEIGHT, available_height)
    return width, height


def _ellipsize_middle(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = max(1, (limit - 1) // 2)
    tail = max(1, limit - head - 1)
    return f"{value[:head]}…{value[-tail:]}"


@dataclass(frozen=True, slots=True)
class SettingsActions:
    get_config: Callable[[], LauncherConfig]
    add_item: Callable[..., bool]
    delete_item: Callable[[str], bool]
    rename_item: Callable[[str, str], bool]
    move_item: Callable[[str, int], bool]
    set_appearance_mode: Callable[[str], bool]
    set_columns: Callable[[int], bool]
    set_startup: Callable[[bool], bool]
    set_hotkeys: Callable[[str, str], bool]


class SettingsWindow(ctk.CTkToplevel):
    """A singleton settings window with a compact two-page layout."""

    def __init__(self, root: tk.Misc, actions: SettingsActions) -> None:
        super().__init__(root)
        self.withdraw()
        self._actions = actions
        self._refreshing = False
        self._active_page = "items"
        self._ultra_compact = False
        self._items_signature: tuple[object, ...] | None = None

        self.title("QuickAccess")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(320, 200)
        # The app computes a monitor-aware size for every open.  Keeping the
        # settings shell fixed avoids CTk's DPI-scaled children drifting out
        # of their responsive breakpoint during a manual native resize.
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.grid_columnconfigure(0, minsize=SIDEBAR_WIDTH)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._monitor = NativeMonitorService()
        self._build_sidebar()
        self._build_items_page()
        self._build_preferences_page()
        self._select_page("items")
        self.refresh()

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(
            self,
            width=SIDEBAR_WIDTH,
            corner_radius=0,
            fg_color=SURFACE,
        )
        self._sidebar = sidebar
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(4, weight=1)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        self._brand = brand
        brand.grid(row=0, column=0, padx=20, pady=(22, 26), sticky="ew")
        brand.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            brand,
            text="",
            image=brand_image(38),
            width=38,
            height=38,
            fg_color="transparent",
        ).grid(row=0, column=0, rowspan=2, padx=(0, 11))
        self._brand_name = ctk.CTkLabel(
            brand,
            text="QuickAccess",
            text_color=TEXT,
            font=font(15, "bold"),
            anchor="w",
        )
        self._brand_name.grid(row=0, column=1, sticky="sw")
        self._brand_subtitle = ctk.CTkLabel(
            brand,
            text="Launcher",
            text_color=MUTED,
            font=font(10),
            anchor="w",
        )
        self._brand_subtitle.grid(row=1, column=1, sticky="nw")

        self._sidebar_section_label = ctk.CTkLabel(
            sidebar,
            text="관리",
            text_color=MUTED,
            font=font(10, "bold"),
            anchor="w",
        )
        self._sidebar_section_label.grid(
            row=1,
            column=0,
            padx=24,
            pady=(0, 8),
            sticky="ew",
        )

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._nav_buttons["items"] = self._nav_button(
            sidebar,
            row=2,
            text="01   바로가기",
            command=lambda: self._select_page("items"),
        )
        self._nav_buttons["preferences"] = self._nav_button(
            sidebar,
            row=3,
            text="02   환경 설정",
            command=lambda: self._select_page("preferences"),
        )

        hint = ctk.CTkFrame(
            sidebar,
            fg_color=SURFACE_ALT,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )
        self._sidebar_hint = hint
        hint.grid(row=5, column=0, padx=16, pady=18, sticky="sew")
        ctk.CTkLabel(
            hint,
            text="빠른 실행",
            font=font(10, "bold"),
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x", padx=13, pady=(11, 3))
        self._shortcut_hint = ctk.CTkLabel(
            hint,
            text="Ctrl  +  Space",
            font=font(11, "bold"),
            text_color=TEXT,
            anchor="w",
        )
        self._shortcut_hint.pack(fill="x", padx=13, pady=(0, 11))

    def _nav_button(
        self,
        parent: ctk.CTkFrame,
        *,
        row: int,
        text: str,
        command: Callable[[], None],
    ) -> ctk.CTkButton:
        button = ctk.CTkButton(
            parent,
            text=text,
            height=42,
            corner_radius=10,
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            text_color=MUTED,
            font=font(12, "bold"),
            anchor="w",
            command=command,
        )
        button.grid(row=row, column=0, padx=14, pady=3, sticky="ew")
        return button

    def _build_items_page(self) -> None:
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self._items_page = page
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent")
        self._items_header = header
        header.grid(row=0, column=0, padx=28, pady=(26, 18), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self._items_title = ctk.CTkLabel(
            header,
            text="바로가기",
            font=font(24, "bold"),
            text_color=TEXT,
            anchor="w",
            width=1,
        )
        self._items_title.grid(row=0, column=0, sticky="w")
        self._items_subtitle = ctk.CTkLabel(
            header,
            text="자주 쓰는 파일과 폴더를 원하는 순서로 관리하세요.",
            font=font(11),
            text_color=MUTED,
            anchor="w",
        )
        self._items_subtitle.grid(row=1, column=0, pady=(3, 0), sticky="w")
        self._add_folder_button = ctk.CTkButton(
            header,
            text="＋  폴더 추가",
            width=112,
            height=38,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=font(11, "bold"),
            command=self._add_folder,
        )
        self._add_folder_button.grid(
            row=0,
            column=1,
            rowspan=2,
            padx=(12, 8),
        )
        self._add_file_button = ctk.CTkButton(
            header,
            text="＋  파일 추가",
            width=112,
            height=38,
            corner_radius=10,
            fg_color=ACCENT_SOFT,
            hover_color=SURFACE_HOVER,
            text_color=ACCENT,
            font=font(11, "bold"),
            command=self._add_file,
        )
        self._add_file_button.grid(row=0, column=2, rowspan=2)

        list_card = ctk.CTkFrame(
            page,
            fg_color=SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self._list_card = list_card
        list_card.grid(row=1, column=0, padx=28, pady=(0, 28), sticky="nsew")
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(1, weight=1)

        list_header = ctk.CTkFrame(list_card, fg_color="transparent")
        list_header.grid(row=0, column=0, padx=18, pady=(15, 9), sticky="ew")
        list_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            list_header,
            text="등록 항목",
            font=font(12, "bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._items_count = ctk.CTkLabel(
            list_header,
            text="0개",
            height=24,
            corner_radius=8,
            fg_color=SURFACE_ALT,
            text_color=MUTED,
            font=font(10, "bold"),
        )
        self._items_count.grid(row=0, column=1, ipadx=8)

        self._list = ctk.CTkScrollableFrame(
            list_card,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED,
        )
        self._list.grid(row=1, column=0, padx=(12, 6), pady=(0, 12), sticky="nsew")
        self._list.grid_columnconfigure(0, weight=1)

    def _build_preferences_page(self) -> None:
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self._preferences_page = page
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent")
        self._preferences_header = header
        header.grid(row=0, column=0, padx=28, pady=(26, 18), sticky="ew")
        self._preferences_title = ctk.CTkLabel(
            header,
            text="환경 설정",
            font=font(24, "bold"),
            text_color=TEXT,
            anchor="w",
            width=1,
        )
        self._preferences_title.pack(fill="x")
        self._preferences_subtitle = ctk.CTkLabel(
            header,
            text="패널 모양과 Windows 동작 방식을 설정합니다.",
            font=font(11),
            text_color=MUTED,
            anchor="w",
            width=1,
        )
        self._preferences_subtitle.pack(fill="x", pady=(3, 0))

        scroll = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED,
        )
        self._preferences_scroll = scroll
        scroll.grid(row=1, column=0, padx=(28, 22), pady=(0, 28), sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        appearance = self._settings_card(
            scroll,
            row=0,
            title="화면 스타일",
            description="시스템 설정을 따르거나 밝게·어둡게 고정합니다.",
        )
        self._appearance_card = appearance
        self._appearance = ctk.CTkSegmentedButton(
            appearance,
            values=list(APPEARANCE_MODES),
            width=198,
            height=34,
            corner_radius=9,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE_ALT,
            unselected_hover_color=SURFACE_HOVER,
            text_color=TEXT,
            font=font(11, "bold"),
            command=self._appearance_changed,
        )
        self._appearance.grid(
            row=0,
            column=1,
            rowspan=2,
            padx=18,
            pady=16,
            sticky="e",
        )

        layout = self._settings_card(
            scroll,
            row=1,
            title="패널 레이아웃",
            description="한 줄에 표시할 바로가기 수를 선택합니다.",
        )
        self._layout_card = layout
        self._columns = ctk.CTkSegmentedButton(
            layout,
            values=["2열", "3열", "4열", "5열"],
            height=34,
            corner_radius=9,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE_ALT,
            unselected_hover_color=SURFACE_HOVER,
            text_color=TEXT,
            font=font(11, "bold"),
            command=self._columns_changed,
        )
        self._columns.grid(row=0, column=1, rowspan=2, padx=18, pady=16, sticky="e")

        hotkeys = self._settings_card(
            scroll,
            row=2,
            title="키보드 단축키",
            description="다른 앱과 겹치면 수정한 뒤 적용하세요.",
            content_below=True,
        )
        self._hotkeys_card = hotkeys
        fields = ctk.CTkFrame(hotkeys, fg_color="transparent")
        self._hotkey_fields = fields
        fields.grid(row=2, column=0, columnspan=2, padx=18, pady=(3, 18), sticky="ew")
        fields.grid_columnconfigure(0, weight=1)
        fields.grid_columnconfigure(1, weight=1)
        self._panel_hotkey = self._hotkey_field(fields, 0, "패널 열기")
        self._quick_hotkey = self._hotkey_field(fields, 1, "탐색기에서 빠른 등록")
        self._apply_hotkeys_button = ctk.CTkButton(
            fields,
            text="변경사항 적용",
            width=112,
            height=36,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=font(11, "bold"),
            command=self._apply_hotkeys,
        )
        self._apply_hotkeys_button.grid(row=2, column=1, pady=(12, 0), sticky="e")

        startup = self._settings_card(
            scroll,
            row=3,
            title="Windows 시작 시 실행",
            description="로그인하면 QuickAccess를 트레이에서 자동으로 시작합니다.",
        )
        self._startup_card = startup
        self._startup_variable = tk.BooleanVar(value=False)
        self._startup = ctk.CTkSwitch(
            startup,
            text="",
            width=46,
            variable=self._startup_variable,
            progress_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            command=self._startup_changed,
        )
        self._startup.grid(row=0, column=1, rowspan=2, padx=22, pady=16, sticky="e")

    def _settings_card(
        self,
        parent: ctk.CTkScrollableFrame,
        *,
        row: int,
        title: str,
        description: str,
        content_below: bool = False,
    ) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=row, column=0, pady=(0, 12), sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text=title,
            width=1,
            font=font(13, "bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(16, 2), sticky="ew")
        description_label = ctk.CTkLabel(
            card,
            text=description,
            width=1,
            font=font(10),
            text_color=MUTED,
            anchor="w",
        )
        description_label.grid(
            row=1,
            column=0,
            columnspan=1 if content_below else 1,
            padx=18,
            pady=(0, 16 if not content_below else 6),
            sticky="ew",
        )
        if not hasattr(self, "_settings_descriptions"):
            self._settings_descriptions: list[ctk.CTkLabel] = []
        self._settings_descriptions.append(description_label)
        return card

    def _hotkey_field(self, parent: ctk.CTkFrame, column: int, label: str) -> ctk.CTkEntry:
        label_widget = ctk.CTkLabel(
            parent,
            text=label,
            font=font(10, "bold"),
            text_color=MUTED,
            anchor="w",
        )
        label_widget.grid(
            row=0,
            column=column,
            padx=(0, 8) if column == 0 else (8, 0),
            sticky="ew",
        )
        entry = ctk.CTkEntry(
            parent,
            height=38,
            corner_radius=9,
            fg_color=SURFACE_ALT,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
        )
        entry.grid(
            row=1,
            column=column,
            padx=(0, 8) if column == 0 else (8, 0),
            pady=(5, 0),
            sticky="ew",
        )
        if not hasattr(self, "_hotkey_labels"):
            self._hotkey_labels: list[ctk.CTkLabel] = []
        self._hotkey_labels.append(label_widget)
        return entry

    def _select_page(self, page_name: str) -> None:
        self._active_page = page_name
        pages = {"items": self._items_page, "preferences": self._preferences_page}
        for name, page in pages.items():
            if name == page_name:
                page.grid(row=0, column=1, sticky="nsew")
            else:
                page.grid_forget()
        for name, button in self._nav_buttons.items():
            active = name == page_name
            button.configure(
                fg_color=ACCENT_SOFT if active else "transparent",
                hover_color=ACCENT_SOFT if active else SURFACE_HOVER,
                text_color=ACCENT if active else MUTED,
            )

    def _center_on_first_show(self) -> None:
        self.update_idletasks()
        try:
            cursor = self._monitor.get_cursor_position()
            work_area = self._monitor.get_monitor_work_area(cursor)
            scale = max(0.1, self._get_window_scaling())
            logical_width, logical_height = settings_dimensions(
                work_area.width,
                work_area.height,
                scale,
            )
            self._apply_compact_layout(logical_width)
            rendered = Size(
                self._apply_window_scaling(logical_width),
                self._apply_window_scaling(logical_height),
            )
            position = center_window_in_work_area(rendered, work_area)
            # CustomTkinter temporarily locks native min/max bounds during a
            # per-monitor DPI transition.  This window is moved
            # programmatically (never dragged here), so restore its scaled
            # constraints before applying the requested geometry.
            self.minsize(min(320, logical_width), min(200, logical_height))
            self._set_scaled_min_max()
            self.geometry(
                f"{logical_width}x{logical_height}"
                f"{position_geometry(position.x, position.y)}"
            )
        except Exception:
            scale = max(0.1, self._get_window_scaling())
            fallback_area = Rect(
                0,
                0,
                self.winfo_screenwidth(),
                max(1, self.winfo_screenheight() - 48),
            )
            logical_width, logical_height = settings_dimensions(
                fallback_area.width,
                fallback_area.height,
                scale,
            )
            self._apply_compact_layout(logical_width)
            rendered = Size(
                self._apply_window_scaling(logical_width),
                self._apply_window_scaling(logical_height),
            )
            position = center_window_in_work_area(rendered, fallback_area)
            self.minsize(min(320, logical_width), min(200, logical_height))
            self._set_scaled_min_max()
            self.geometry(
                f"{logical_width}x{logical_height}"
                f"{position_geometry(position.x, position.y)}"
            )

    def _apply_compact_layout(self, logical_width: int) -> None:
        compact = logical_width < 800
        ultra_compact = logical_width < 520
        needs_refresh = ultra_compact != self._ultra_compact
        self._ultra_compact = ultra_compact
        sidebar_width = 104 if ultra_compact else (160 if compact else SIDEBAR_WIDTH)
        self.grid_columnconfigure(0, minsize=sidebar_width)
        self._sidebar.configure(width=sidebar_width)
        self._brand.grid_configure(padx=8 if ultra_compact else (12 if compact else 20))
        if ultra_compact:
            self._brand_name.grid_remove()
            self._brand_subtitle.grid_remove()
            self._sidebar_section_label.grid_remove()
        else:
            self._brand_name.grid()
            self._brand_subtitle.grid()
            self._sidebar_section_label.grid()
        self._nav_buttons["items"].configure(
            text="항목" if ultra_compact else ("01   항목" if compact else "01   바로가기")
        )
        self._nav_buttons["preferences"].configure(
            text="설정" if ultra_compact else ("02   설정" if compact else "02   환경 설정")
        )
        for button in self._nav_buttons.values():
            button.grid_configure(padx=6 if ultra_compact else (8 if compact else 14))
        if compact:
            self._sidebar_hint.grid_remove()
            self._items_subtitle.grid_remove()
            self._preferences_subtitle.pack_forget()
            for description in self._settings_descriptions:
                description.grid_remove()
        else:
            self._sidebar_hint.grid()
            self._items_subtitle.grid()
            self._preferences_subtitle.pack(fill="x", pady=(3, 0))
            for description in self._settings_descriptions:
                description.grid()

        if ultra_compact:
            self._items_header.grid_columnconfigure(0, weight=1)
            self._items_header.grid_columnconfigure(1, weight=1)
            self._items_title.grid_configure(row=0, column=0, columnspan=2)
            self._add_folder_button.configure(text="폴더", width=82, height=34)
            self._add_folder_button.grid_configure(
                row=1,
                column=0,
                rowspan=1,
                padx=(0, 4),
                pady=(8, 0),
                sticky="ew",
            )
            self._add_file_button.configure(text="파일", width=82, height=34)
            self._add_file_button.grid_configure(
                row=1,
                column=1,
                rowspan=1,
                padx=(4, 0),
                pady=(8, 0),
                sticky="ew",
            )
            self._columns.grid_configure(
                row=2,
                column=0,
                columnspan=2,
                rowspan=1,
                padx=12,
                pady=(0, 12),
                sticky="ew",
            )
            self._appearance.grid_configure(
                row=2,
                column=0,
                columnspan=2,
                rowspan=1,
                padx=12,
                pady=(0, 12),
                sticky="ew",
            )
            self._startup.grid_configure(
                row=2,
                column=0,
                columnspan=2,
                rowspan=1,
                padx=18,
                pady=(0, 12),
                sticky="w",
            )
            self._layout_hotkeys_for_narrow_width()
        else:
            self._items_header.grid_columnconfigure(1, weight=0)
            self._items_title.grid_configure(row=0, column=0, columnspan=1)
            self._add_folder_button.configure(text="＋  폴더 추가", width=112, height=38)
            self._add_folder_button.grid_configure(
                row=0,
                column=1,
                rowspan=2,
                padx=(12, 8),
                pady=0,
                sticky="",
            )
            self._add_file_button.configure(text="＋  파일 추가", width=112, height=38)
            self._add_file_button.grid_configure(
                row=0,
                column=2,
                rowspan=2,
                padx=0,
                pady=0,
                sticky="",
            )
            self._columns.grid_configure(
                row=0,
                column=1,
                columnspan=1,
                rowspan=2,
                padx=18,
                pady=16,
                sticky="e",
            )
            self._appearance.grid_configure(
                row=0,
                column=1,
                columnspan=1,
                rowspan=2,
                padx=18,
                pady=16,
                sticky="e",
            )
            self._startup.grid_configure(
                row=0,
                column=1,
                columnspan=1,
                rowspan=2,
                padx=22,
                pady=16,
                sticky="e",
            )
            self._layout_hotkeys_for_normal_width()

        page_padding = 8 if ultra_compact else (16 if compact else 28)
        self._items_header.grid_configure(
            padx=page_padding,
            pady=(18 if compact else 26, 12 if compact else 18),
        )
        self._list_card.grid_configure(
            padx=page_padding,
            pady=(0, page_padding),
        )
        self._preferences_header.grid_configure(
            padx=page_padding,
            pady=(18 if compact else 26, 12 if compact else 18),
        )
        self._preferences_scroll.grid_configure(
            padx=(page_padding, max(8, page_padding - 6)),
            pady=(0, page_padding),
        )
        if needs_refresh:
            self.refresh()

    def _layout_hotkeys_for_narrow_width(self) -> None:
        self._hotkey_fields.grid_columnconfigure(0, weight=1)
        self._hotkey_fields.grid_columnconfigure(1, weight=0)
        for row, (label, entry) in enumerate(
            zip(self._hotkey_labels, (self._panel_hotkey, self._quick_hotkey))
        ):
            base_row = row * 2
            label.grid_configure(
                row=base_row,
                column=0,
                padx=0,
                pady=(7 if row else 0, 0),
                sticky="ew",
            )
            entry.grid_configure(
                row=base_row + 1,
                column=0,
                padx=0,
                pady=(5, 0),
                sticky="ew",
            )
        self._apply_hotkeys_button.grid_configure(
            row=4,
            column=0,
            padx=0,
            pady=(12, 0),
            sticky="ew",
        )

    def _layout_hotkeys_for_normal_width(self) -> None:
        self._hotkey_fields.grid_columnconfigure(0, weight=1)
        self._hotkey_fields.grid_columnconfigure(1, weight=1)
        for column, (label, entry) in enumerate(
            zip(self._hotkey_labels, (self._panel_hotkey, self._quick_hotkey))
        ):
            padding = (0, 8) if column == 0 else (8, 0)
            label.grid_configure(
                row=0,
                column=column,
                padx=padding,
                pady=0,
                sticky="ew",
            )
            entry.grid_configure(
                row=1,
                column=column,
                padx=padding,
                pady=(5, 0),
                sticky="ew",
            )
        self._apply_hotkeys_button.grid_configure(
            row=2,
            column=1,
            padx=0,
            pady=(12, 0),
            sticky="e",
        )

    def show(self) -> None:
        self.refresh()
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after_idle(self._center_on_first_show)
        self.after(80, lambda: self._settle_dpi(2))

    def _settle_dpi(self, attempts_remaining: int) -> None:
        try:
            if not self.winfo_viewable():
                return
            self._center_on_first_show()
            if attempts_remaining:
                self.after(80, lambda: self._settle_dpi(attempts_remaining - 1))
        except tk.TclError:
            return

    def refresh(self) -> None:
        self._refreshing = True
        try:
            config = self._actions.get_config()
            ordered_items = sorted(config.items, key=lambda value: value.order)
            items_signature: tuple[object, ...] = (
                self._ultra_compact,
                tuple(
                    (item.id, item.name, item.path, item.type, item.order)
                    for item in ordered_items
                ),
            )
            if items_signature != self._items_signature:
                for child in self._list.winfo_children():
                    child.destroy()
                for row, item in enumerate(ordered_items):
                    self._add_item_row(row, item, len(ordered_items))
                if not ordered_items:
                    self._add_empty_state()
                self._items_signature = items_signature

            self._items_count.configure(text=f"{len(config.items)}개")
            self._shortcut_hint.configure(text=self._format_hotkey(config.hotkey))

            self._columns.set(f"{config.columns}열")
            self._appearance.set(
                APPEARANCE_LABELS.get(config.appearance_mode, "시스템")
            )
            self._startup_variable.set(config.run_on_startup)
            self._replace_entry(self._panel_hotkey, config.hotkey)
            self._replace_entry(self._quick_hotkey, config.quick_add_hotkey)
        finally:
            self._refreshing = False

    def _add_empty_state(self) -> None:
        empty = ctk.CTkFrame(self._list, fg_color="transparent")
        empty.grid(row=0, column=0, padx=24, pady=72, sticky="ew")
        ctk.CTkLabel(
            empty,
            text="＋",
            width=46,
            height=46,
            corner_radius=14,
            fg_color=ACCENT_SOFT,
            text_color=ACCENT,
            font=font(24),
        ).pack(pady=(0, 12))
        ctk.CTkLabel(
            empty,
            text="항목이 없습니다" if self._ultra_compact else "아직 등록된 바로가기가 없습니다",
            width=1,
            text_color=TEXT,
            font=font(13, "bold"),
        ).pack()
        ctk.CTkLabel(
            empty,
            text=(
                "위 버튼에서 추가하세요."
                if self._ultra_compact
                else "위의 추가 버튼으로 첫 번째 항목을 만들어 보세요."
            ),
            width=1,
            text_color=MUTED,
            font=font(10),
        ).pack(pady=(4, 0))

    def _add_item_row(self, row: int, item: LauncherItem, item_count: int) -> None:
        card = ctk.CTkFrame(
            self._list,
            fg_color=SURFACE_ALT,
            corner_radius=11,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=row, column=0, padx=2, pady=4, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        is_folder = item.type == "folder"
        ctk.CTkLabel(
            card,
            text=ICON_FOLDER if is_folder else ICON_FILE,
            width=40,
            height=40,
            corner_radius=10,
            fg_color=ACCENT_SOFT,
            text_color=ACCENT,
            font=icon_font(17),
        ).grid(row=0, column=0, rowspan=2, padx=(12, 11), pady=11)
        ctk.CTkLabel(
            card,
            text=_ellipsize_middle(item.name, 34),
            width=1,
            font=font(12, "bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=1, pady=(11, 1), sticky="sew")
        ctk.CTkLabel(
            card,
            text=_ellipsize_middle(item.path, 52),
            width=1,
            font=font(9),
            text_color=MUTED,
            anchor="w",
        ).grid(row=1, column=1, pady=(0, 11), sticky="new")

        button_parent: ctk.CTkFrame = card
        button_columns = (2, 3, 4, 5)
        if self._ultra_compact:
            button_parent = ctk.CTkFrame(card, fg_color="transparent")
            button_parent.grid(
                row=2,
                column=0,
                columnspan=2,
                padx=8,
                pady=(0, 6),
                sticky="e",
            )
            button_columns = (0, 1, 2, 3)

        self._row_button(
            button_parent,
            text="수정",
            column=button_columns[0],
            command=lambda value=item: self._rename(value),
        )
        self._row_button(
            button_parent,
            text=ICON_DELETE,
            column=button_columns[1],
            command=lambda value=item: self._delete(value),
            danger=True,
            use_icon_font=True,
        )
        self._row_button(
            button_parent,
            text="↑",
            column=button_columns[2],
            command=lambda value=item: self._move(value, value.order - 1),
            state="disabled" if item.order == 0 else "normal",
        )
        self._row_button(
            button_parent,
            text="↓",
            column=button_columns[3],
            command=lambda value=item: self._move(value, value.order + 1),
            state="disabled" if item.order >= item_count - 1 else "normal",
        )

    def _row_button(
        self,
        parent: ctk.CTkFrame,
        *,
        text: str,
        column: int,
        command: Callable[[], None],
        danger: bool = False,
        use_icon_font: bool = False,
        state: str = "normal",
    ) -> None:
        ctk.CTkButton(
            parent,
            text=text,
            width=48 if text == "수정" else 34,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            hover_color=DANGER_SOFT if danger else SURFACE_HOVER,
            text_color=DANGER if danger else MUTED,
            border_width=0,
            font=icon_font(15) if use_icon_font else font(10, "bold"),
            state=state,
            command=command,
        ).grid(row=0, column=column, rowspan=2, padx=(0, 5), pady=14)

    def _choose_and_add(self, path: str, item_type: str) -> None:
        if not path:
            return
        default_name = ntpath.basename(path.rstrip("\\/")) or path
        name = ask_display_name(self, default_name)
        if name is not None:
            self._actions.add_item(path, name, item_type=item_type)
            self.refresh()

    def _add_folder(self) -> None:
        self._choose_and_add(
            filedialog.askdirectory(parent=self, title="폴더 선택"),
            "folder",
        )

    def _add_file(self) -> None:
        self._choose_and_add(
            filedialog.askopenfilename(parent=self, title="파일 선택"),
            "file",
        )

    def _rename(self, item: LauncherItem) -> None:
        name = ask_display_name(self, item.name, title="표시명 수정")
        if name is not None:
            self._actions.rename_item(item.id, name)
            self.refresh()

    def _delete(self, item: LauncherItem) -> None:
        if messagebox.askyesno(
            "항목 삭제",
            f"'{item.name}' 항목을 삭제할까요?",
            parent=self,
        ):
            self._actions.delete_item(item.id)
            self.refresh()

    def _move(self, item: LauncherItem, new_index: int) -> None:
        self._actions.move_item(item.id, new_index)
        self.refresh()

    def _columns_changed(self, value: str) -> None:
        if not self._refreshing:
            self._actions.set_columns(int(value.removesuffix("열")))
            self.refresh()

    def _appearance_changed(self, value: str) -> None:
        if not self._refreshing:
            self._actions.set_appearance_mode(APPEARANCE_MODES[value])
            self.refresh()

    def _startup_changed(self) -> None:
        if not self._refreshing:
            self._actions.set_startup(bool(self._startup_variable.get()))
            self.refresh()

    def _apply_hotkeys(self) -> None:
        self._actions.set_hotkeys(
            self._panel_hotkey.get().strip(),
            self._quick_hotkey.get().strip(),
        )
        self.refresh()

    @staticmethod
    def _replace_entry(entry: ctk.CTkEntry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    @staticmethod
    def _format_hotkey(value: str) -> str:
        return "  +  ".join(part.capitalize() for part in value.split("+") if part)
