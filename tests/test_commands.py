from __future__ import annotations

import queue
import threading
import unittest

from quickaccess.commands import (
    CommandBus,
    CommandBusClosedError,
    CommandSource,
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


if __name__ == "__main__":
    unittest.main()
