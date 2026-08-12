from __future__ import annotations

import threading
import unittest

from quickaccess.commands import (
    CommandBus,
    CommandSource,
    OpenPanelCommand,
    OpenSettingsCommand,
    QuitCommand,
)
from quickaccess.services.tray import TrayService, TrayState, create_tray_icon


class FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text = text
        self.action = action
        self.default = default


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)


class FakeIcon:
    instances: list["FakeIcon"] = []

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False
        self._stop_event = threading.Event()
        self.__class__.instances.append(self)

    def run(self, setup=None):
        if setup is not None:
            setup(self)
        self._stop_event.wait(2)

    def stop(self):
        self._stop_event.set()


class FakeBackend:
    Icon = FakeIcon
    Menu = FakeMenu
    MenuItem = FakeMenuItem


class TrayTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeIcon.instances.clear()

    def test_generated_icon_is_nonempty_rgba(self) -> None:
        icon = create_tray_icon(64)
        self.assertEqual(icon.size, (64, 64))
        self.assertEqual(icon.mode, "RGBA")
        self.assertIsNotNone(icon.getbbox())
        with self.assertRaises(ValueError):
            create_tray_icon(8)

    def test_menu_callbacks_only_enqueue_commands(self) -> None:
        bus = CommandBus()
        tray = TrayService(bus, backend=FakeBackend)
        self.assertTrue(tray.start())
        self.assertTrue(tray.wait_until_ready(1))
        self.assertEqual(tray.state, TrayState.RUNNING)
        self.assertFalse(tray.start())

        icon = FakeIcon.instances[-1]
        panel_item, settings_item, _separator, quit_item = icon.menu
        self.assertTrue(panel_item.default)
        panel_item.action(icon, panel_item)
        settings_item.action(icon, settings_item)
        quit_item.action(icon, quit_item)

        commands = bus.drain()
        self.assertIsInstance(commands[0], OpenPanelCommand)
        self.assertIsInstance(commands[1], OpenSettingsCommand)
        self.assertIsInstance(commands[2], QuitCommand)
        self.assertTrue(all(command.source is CommandSource.TRAY for command in commands))
        self.assertEqual(commands[2].reason, "tray")

        self.assertTrue(tray.stop(1))
        self.assertEqual(tray.state, TrayState.STOPPED)
        self.assertTrue(tray.stop(1))

    def test_late_callback_after_bus_close_is_ignored(self) -> None:
        bus = CommandBus()
        tray = TrayService(bus, backend=FakeBackend)
        tray.start()
        self.assertTrue(tray.wait_until_ready(1))
        bus.close(discard_pending=True)

        FakeIcon.instances[-1].menu[0].action(None, None)
        self.assertEqual(bus.drain(), [])
        self.assertTrue(tray.stop(1))

    def test_immediate_stop_cannot_leave_tray_running(self) -> None:
        bus = CommandBus()
        tray = TrayService(bus, backend=FakeBackend)
        self.assertTrue(tray.start())
        self.assertTrue(tray.stop(1))
        self.assertEqual(tray.state, TrayState.STOPPED)


if __name__ == "__main__":
    unittest.main()
