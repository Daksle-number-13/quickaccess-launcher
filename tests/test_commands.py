from __future__ import annotations

import queue
import threading
import unittest

from quickaccess.commands import (
    CommandBus,
    CommandBusClosedError,
    CommandSource,
    IconReadyCommand,
    OpenPanelCommand,
    OpenSettingsCommand,
    QuitCommand,
)


class CommandBusTests(unittest.TestCase):
    def test_fifo_and_bounded_drain(self) -> None:
        bus = CommandBus()
        first = OpenPanelCommand(source=CommandSource.HOTKEY, cursor_position=(3, 7))
        second = OpenSettingsCommand(source=CommandSource.TRAY)
        third = QuitCommand(reason="test")
        bus.publish_many([first, second, third])

        self.assertEqual(bus.drain(2), [first, second])
        self.assertEqual(bus.get_nowait(), third)
        with self.assertRaises(queue.Empty):
            bus.get_nowait()

    def test_close_retains_pending_and_wakes_waiter(self) -> None:
        bus = CommandBus()
        pending = OpenPanelCommand()
        bus.publish(pending)
        bus.close()

        self.assertEqual(bus.get(), pending)
        with self.assertRaises(CommandBusClosedError):
            bus.get()
        with self.assertRaises(CommandBusClosedError):
            bus.publish(QuitCommand())

        waiting_bus = CommandBus()
        observed: list[type[BaseException]] = []

        def wait_for_command() -> None:
            try:
                waiting_bus.get(timeout=2)
            except BaseException as exc:  # test captures the wake-up result
                observed.append(type(exc))

        thread = threading.Thread(target=wait_for_command)
        thread.start()
        waiting_bus.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [CommandBusClosedError])

    def test_concurrent_publishers_do_not_lose_commands(self) -> None:
        bus = CommandBus()
        publisher_count = 6
        commands_per_publisher = 80

        def publish(index: int) -> None:
            for sequence in range(commands_per_publisher):
                bus.publish(
                    QuitCommand(
                        source=CommandSource.WORKER,
                        reason=f"{index}:{sequence}",
                    )
                )

        threads = [
            threading.Thread(target=publish, args=(index,))
            for index in range(publisher_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        commands = bus.drain()
        self.assertEqual(len(commands), publisher_count * commands_per_publisher)
        self.assertEqual(len({command.reason for command in commands}), len(commands))

    def test_ui_drain_prioritizes_hotkey_over_background_backlog(self) -> None:
        bus = CommandBus()
        for index in range(1000):
            bus.publish(IconReadyCommand(key=f"icon-{index}", image=object()))
        panel = OpenPanelCommand(
            source=CommandSource.HOTKEY,
            cursor_position=(120, 240),
        )
        bus.publish(panel)

        drained = bus.drain_for_ui(40)

        self.assertIs(panel, drained[0])
        self.assertEqual(39, len(drained[1:]))

    def test_ui_drain_coalesces_idempotent_commands_to_newest_value(self) -> None:
        bus = CommandBus()
        first_panel = OpenPanelCommand(cursor_position=(1, 1))
        latest_panel = OpenPanelCommand(cursor_position=(9, 9))
        first_icon = IconReadyCommand(key=".xlsx", image="old")
        latest_icon = IconReadyCommand(key=".xlsx", image="new")
        bus.publish_many([first_panel, first_icon, latest_panel, latest_icon])

        drained = bus.drain_for_ui()

        self.assertEqual([latest_panel, latest_icon], drained)
        self.assertEqual(0, len(bus))

    def test_fifo_contract_survives_a_priority_drain(self) -> None:
        bus = CommandBus()
        background = IconReadyCommand(key=".pdf", image="ready")
        panel = OpenPanelCommand(source=CommandSource.HOTKEY)
        settings = OpenSettingsCommand(source=CommandSource.TRAY)
        bus.publish_many([background, panel, settings])

        self.assertEqual([panel], bus.drain_for_ui(1))
        self.assertEqual([background, settings], bus.drain())

    def test_plain_fifo_drain_does_not_coalesce_duplicates(self) -> None:
        bus = CommandBus()
        first = OpenPanelCommand(cursor_position=(1, 1))
        second = OpenPanelCommand(cursor_position=(2, 2))
        bus.publish_many([first, second])

        self.assertEqual([first, second], bus.drain())


if __name__ == "__main__":
    unittest.main()
