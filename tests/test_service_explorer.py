from __future__ import annotations

import unittest
from unittest.mock import patch

from quickaccess.services.explorer import (
    ExplorerErrorCode,
    ExplorerQuickAddService,
    ExplorerTargetSource,
)


class _Runtime:
    def __init__(self) -> None:
        self.initialized = 0
        self.uninitialized = 0

    def CoInitialize(self) -> None:
        self.initialized += 1

    def CoUninitialize(self) -> None:
        self.uninitialized += 1


class _Item:
    def __init__(self, path: str, is_folder: bool = False) -> None:
        self.Path = path
        self.IsFolder = is_folder


class _Link:
    def __init__(self, path: str) -> None:
        self.Path = path


class _ShortcutItem:
    def __init__(self, lnk_path: str, target_path: str | None) -> None:
        self.Path = lnk_path
        self.IsFolder = False
        self._target_path = target_path

    @property
    def GetLink(self) -> _Link:
        if self._target_path is None:
            raise RuntimeError("shortcut could not be resolved")
        return _Link(self._target_path)


class _ItemWithoutFolderType:
    def __init__(self, path: str) -> None:
        self.Path = path

    @property
    def IsFolder(self) -> bool:
        raise RuntimeError("shell extension did not expose IsFolder")


class _SelectedItems:
    def __init__(self, paths: list[str]) -> None:
        self._paths = paths
        self.Count = len(paths)

    def Item(self, index: int) -> _Item:
        return _Item(self._paths[index])


class _Document:
    def __init__(self, selected: list[str], folder: str) -> None:
        self._selected = _SelectedItems(selected)
        self.Folder = type("Folder", (), {"Self": _Item(folder)})()

    def SelectedItems(self) -> _SelectedItems:
        return self._selected


class _Window:
    def __init__(self, hwnd: int, selected: list[str], folder: str) -> None:
        self.HWND = hwnd
        self.Document = _Document(selected, folder)


class _Shell:
    def __init__(self, windows: list[_Window]) -> None:
        self._windows = windows

    def Windows(self) -> list[_Window]:
        return self._windows


class _IndexedWindows:
    def __init__(self, windows: list[_Window]) -> None:
        self._windows = windows
        self.Count = len(windows)

    def Item(self, index: int) -> _Window:
        return self._windows[index]


class _IndexedShell:
    def __init__(self, windows: list[_Window]) -> None:
        self._windows = _IndexedWindows(windows)

    def Windows(self) -> _IndexedWindows:
        return self._windows


class ExplorerQuickAddTests(unittest.TestCase):
    def test_default_com_dependencies_are_loaded_only_on_first_use(self) -> None:
        with patch(
            "quickaccess.services.explorer._load_default_dependencies",
            return_value=(None, None),
        ) as load_dependencies:
            service = ExplorerQuickAddService(foreground_window=lambda: 22)
            load_dependencies.assert_not_called()

            self.assertFalse(service.available)
            load_dependencies.assert_called_once_with()

    def make_service(self, shell: _Shell, hwnd: int = 22) -> tuple[ExplorerQuickAddService, _Runtime]:
        runtime = _Runtime()
        service = ExplorerQuickAddService(
            shell_factory=lambda: shell,
            com_runtime=runtime,
            foreground_window=lambda: hwnd,
        )
        return service, runtime

    def test_selected_item_wins_over_current_folder(self) -> None:
        shell = _Shell(
            [_Window(11, [], r"C:\Other"), _Window(22, [r"C:\품질\불량.xlsx"], r"C:\품질")]
        )
        service, runtime = self.make_service(shell)
        result = service.get_target()

        self.assertTrue(result.success)
        self.assertEqual(result.path, r"C:\품질\불량.xlsx")
        self.assertEqual(result.suggested_name, "불량.xlsx")
        self.assertEqual(result.source, ExplorerTargetSource.SELECTION)
        self.assertEqual(result.item_type, "file")
        self.assertEqual((runtime.initialized, runtime.uninitialized), (1, 1))

    def test_current_folder_is_used_when_nothing_is_selected(self) -> None:
        service, _runtime = self.make_service(_Shell([_Window(22, [], r"D:\검사 자료")]))
        result = service.get_target()
        self.assertEqual(result.path, r"D:\검사 자료")
        self.assertEqual(result.source, ExplorerTargetSource.CURRENT_FOLDER)
        self.assertEqual(result.item_type, "folder")

    def test_unknown_selection_type_does_not_require_filesystem_probe(self) -> None:
        window = _Window(22, [], r"C:\Folder")
        selected = type(
            "Selected",
            (),
            {"Count": 1, "Item": lambda _self, _index: _ItemWithoutFolderType(r"Z:\offline")},
        )()
        window.Document.SelectedItems = lambda: selected
        service, _runtime = self.make_service(_Shell([window]))

        result = service.get_target()

        self.assertTrue(result.success)
        self.assertEqual(result.item_type, "file")

    def test_indexed_com_collection_is_supported(self) -> None:
        runtime = _Runtime()
        service = ExplorerQuickAddService(
            shell_factory=lambda: _IndexedShell([_Window(22, [], r"D:\Indexed")]),
            com_runtime=runtime,
            foreground_window=lambda: 22,
        )
        result = service.get_target()
        self.assertTrue(result.success)
        self.assertEqual(result.path, r"D:\Indexed")

    def test_only_foreground_hwnd_is_accepted(self) -> None:
        service, _runtime = self.make_service(_Shell([_Window(11, [], r"C:\Other")]))
        result = service.get_target()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, ExplorerErrorCode.NO_FOREGROUND_EXPLORER)
        self.assertEqual(result.error, "현재 열린 탐색기 창이 없습니다")

    def test_captured_hwnd_avoids_worker_focus_race(self) -> None:
        runtime = _Runtime()

        def changed_focus() -> int:
            raise AssertionError("foreground should not be queried in worker")

        service = ExplorerQuickAddService(
            shell_factory=lambda: _Shell([_Window(22, [], r"C:\Captured")]),
            com_runtime=runtime,
            foreground_window=changed_focus,
        )
        result = service.get_target(foreground_hwnd=22)
        self.assertTrue(result.success)
        self.assertEqual(result.path, r"C:\Captured")

    def test_virtual_selection_is_not_silently_replaced_by_folder(self) -> None:
        service, _runtime = self.make_service(
            _Shell([_Window(22, ["::{virtual}"], r"C:\Folder")])
        )
        result = service.get_target()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, ExplorerErrorCode.NO_FILESYSTEM_PATH)

    def test_lnk_selection_preserves_the_shortcut_file(self) -> None:
        window = _Window(22, [], r"C:\Ignored")
        shortcut = _ShortcutItem(r"C:\Desktop\바로가기.lnk", r"D:\실제\대상 폴더")
        selected = type(
            "Selected",
            (),
            {"Count": 1, "Item": lambda _self, _index: shortcut},
        )()
        window.Document.SelectedItems = lambda: selected
        service, _runtime = self.make_service(_Shell([window]))

        result = service.get_target()

        self.assertTrue(result.success)
        self.assertEqual(result.path, r"C:\Desktop\바로가기.lnk")
        self.assertEqual(result.item_type, "file")
        self.assertEqual(result.suggested_name, "바로가기.lnk")

    def test_lnk_selection_falls_back_to_shortcut_file_when_unresolvable(self) -> None:
        window = _Window(22, [], r"C:\Ignored")
        shortcut = _ShortcutItem(r"C:\Desktop\깨진.lnk", None)
        selected = type(
            "Selected",
            (),
            {"Count": 1, "Item": lambda _self, _index: shortcut},
        )()
        window.Document.SelectedItems = lambda: selected
        service, _runtime = self.make_service(_Shell([window]))

        result = service.get_target()

        self.assertTrue(result.success)
        self.assertEqual(result.path, r"C:\Desktop\깨진.lnk")
        self.assertEqual(result.item_type, "file")

    def test_com_exception_becomes_safe_result_and_uninitializes(self) -> None:
        runtime = _Runtime()

        def fail() -> object:
            raise RuntimeError("policy denied COM")

        service = ExplorerQuickAddService(
            shell_factory=fail,
            com_runtime=runtime,
            foreground_window=lambda: 22,
        )
        result = service.get_target()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, ExplorerErrorCode.COM_FAILURE)
        self.assertEqual(result.error, "현재 열린 탐색기 창이 없습니다")
        self.assertIn("policy denied COM", result.detail or "")
        self.assertEqual((runtime.initialized, runtime.uninitialized), (1, 1))


if __name__ == "__main__":
    unittest.main()
