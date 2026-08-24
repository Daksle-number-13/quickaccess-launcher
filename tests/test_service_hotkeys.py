from __future__ import annotations

import queue
import threading
import time
import unittest
from types import SimpleNamespace

from quickaccess.services.hotkeys import (
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
    HotkeyBinding,
    HotkeyParseError,
    HotkeyRegistrationError,
    HotkeyUnavailableError,
    NativeHotkeyService,
    _RegistrationManager,
    describe_hotkey_conflict_risk,
    parse_hotkey,
    prepare_bindings,
)


class HotkeyParserTests(unittest.TestCase):
    def test_parser_canonicalizes_aliases_and_adds_no_repeat(self) -> None:
        parsed = parse_hotkey(" Control + SHIFT + Space ")
        self.assertEqual(parsed.canonical, "ctrl+shift+space")
        self.assertEqual(parsed.modifiers, MOD_CONTROL | MOD_SHIFT)
        self.assertEqual(
            parsed.registration_modifiers,
            MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT,
        )
        self.assertEqual(parsed.virtual_key, 0x20)

    def test_parser_supports_function_and_character_keys(self) -> None:
        self.assertEqual(parse_hotkey("alt+f24").virtual_key, 0x87)
        self.assertEqual(parse_hotkey("win+a").virtual_key, ord("A"))
        self.assertEqual(parse_hotkey("ctrl+numpad7").virtual_key, 0x67)

    def test_parser_rejects_ambiguous_or_unsafe_shortcuts(self) -> None:
        invalid = ("", "space", "ctrl", "ctrl++space", "ctrl+a+b", "ctrl+ctrl+a")
        for shortcut in invalid:
            with self.subTest(shortcut=shortcut):
                with self.assertRaises(HotkeyParseError):
                    parse_hotkey(shortcut)

    def test_binding_set_rejects_duplicate_native_combinations(self) -> None:
        with self.assertRaises(HotkeyParseError):
            prepare_bindings(
                {
                    "panel": HotkeyBinding("control+space", lambda: None),
                    "quick_add": HotkeyBinding("CTRL+SPACE", lambda: None),
                }
            )

    def test_conflict_risk_flags_known_combinations_regardless_of_formatting(self) -> None:
        self.assertIsNotNone(describe_hotkey_conflict_risk("Control + Space"))
        self.assertIsNotNone(describe_hotkey_conflict_risk("ctrl+shift+space"))

    def test_conflict_risk_is_none_for_unlisted_or_invalid_shortcuts(self) -> None:
        self.assertIsNone(describe_hotkey_conflict_risk("ctrl+alt+q"))
        self.assertIsNone(describe_hotkey_conflict_risk("not a hotkey"))


class _FakeRegistrationApi:
    def __init__(self) -> None:
        self.registered: dict[int, tuple[int, int]] = {}
        self.fail_identity: tuple[int, int] | None = None
        self.register_calls = 0
        self.unregister_calls = 0

    def register(self, hotkey_id: int, hotkey: object) -> None:
        self.register_calls += 1
        if hotkey.identity == self.fail_identity:
            raise OSError("simulated registration conflict")
        if hotkey.identity in self.registered.values():
            raise OSError("duplicate native shortcut")
        self.registered[hotkey_id] = hotkey.identity

    def unregister(self, hotkey_id: int) -> None:
        self.unregister_calls += 1
        if hotkey_id not in self.registered:
            raise OSError("not registered")
        self.registered.pop(hotkey_id)


class RegistrationTransactionTests(unittest.TestCase):
    def test_failed_reconfiguration_restores_previous_bindings(self) -> None:
        api = _FakeRegistrationApi()
        manager = _RegistrationManager(api)
        old = prepare_bindings({"panel": ("ctrl+space", lambda: None)})
        manager.apply(old)
        old_native_state = dict(api.registered)

        replacement = prepare_bindings({"panel": ("ctrl+alt+space", lambda: None)})
        api.fail_identity = replacement[0].hotkey.identity
        with self.assertRaises(HotkeyRegistrationError) as caught:
            manager.apply(replacement)

        self.assertTrue(caught.exception.rollback_succeeded)
        self.assertEqual(manager.snapshot(), {"panel": "ctrl+space"})
        self.assertEqual(api.registered, old_native_state)

    def test_callback_only_change_does_not_reregister(self) -> None:
        api = _FakeRegistrationApi()
        manager = _RegistrationManager(api)
        manager.apply(prepare_bindings({"panel": ("ctrl+space", lambda: 1)}))
        calls = (api.register_calls, api.unregister_calls)
        replacement_callback = lambda: 2
        manager.apply(
            prepare_bindings({"panel": ("ctrl+space", replacement_callback)})
        )
        self.assertEqual((api.register_calls, api.unregister_calls), calls)
        self.assertIs(manager.callback_for(next(iter(api.registered))), replacement_callback)

    def test_failed_unregister_rollback_reports_only_native_bindings_that_remain(
        self,
    ) -> None:
        api = _FakeRegistrationApi()
        manager = _RegistrationManager(api)
        old = prepare_bindings(
            {
                "panel": ("ctrl+alt+q", lambda: None),
                "quick_add": ("ctrl+alt+w", lambda: None),
            }
        )
        manager.apply(old)
        second_id = max(api.registered)
        original_unregister = api.unregister

        def fail_second_unregister(hotkey_id: int) -> None:
            if hotkey_id == second_id:
                raise OSError("simulated unregister failure")
            original_unregister(hotkey_id)

        api.unregister = fail_second_unregister  # type: ignore[method-assign]
        api.fail_identity = old[0].hotkey.identity
        replacement = prepare_bindings(
            {
                "panel": ("ctrl+alt+e", lambda: None),
                "quick_add": ("ctrl+alt+r", lambda: None),
            }
        )

        with self.assertRaises(HotkeyRegistrationError) as caught:
            manager.apply(replacement)

        self.assertFalse(caught.exception.rollback_succeeded)
        self.assertEqual({second_id: old[1].hotkey.identity}, api.registered)
        self.assertEqual({"quick_add": "ctrl+alt+w"}, manager.snapshot())


class _FakeMessageApi(_FakeRegistrationApi):
    def __init__(self) -> None:
        super().__init__()
        self.messages: queue.Queue[SimpleNamespace] = queue.Queue()

    def create_message_queue(self) -> int:
        return 77

    def post(self, thread_id: int, message: int) -> None:
        self.messages.put(SimpleNamespace(message=message, wParam=0))

    def get_message(self) -> tuple[int, SimpleNamespace]:
        return 1, self.messages.get(timeout=2)

    def fire(self, hotkey_id: int) -> None:
        self.messages.put(SimpleNamespace(message=WM_HOTKEY, wParam=hotkey_id))


class _FailingUnregisterMessageApi(_FakeMessageApi):
    def unregister(self, hotkey_id: int) -> None:
        raise OSError("simulated native cleanup failure")


class _SlowRegisterMessageApi(_FakeMessageApi):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def register(self, hotkey_id: int, hotkey: object) -> None:
        time.sleep(self.delay)
        super().register(hotkey_id, hotkey)


class NativeHotkeyServiceTests(unittest.TestCase):
    def test_service_configures_dispatches_and_stops_on_its_thread(self) -> None:
        api = _FakeMessageApi()
        invoked = threading.Event()
        service = NativeHotkeyService(api_factory=lambda: api, command_timeout=1)
        try:
            configured = service.configure(
                {"panel": HotkeyBinding("ctrl+space", invoked.set)}
            )
            self.assertEqual(configured, {"panel": "ctrl+space"})
            hotkey_id = next(iter(api.registered))
            api.fire(hotkey_id)
            self.assertTrue(invoked.wait(1))
        finally:
            service.stop()
        self.assertFalse(service.running)
        self.assertEqual(api.registered, {})

    def test_stop_exits_message_thread_even_if_unregister_reports_failure(self) -> None:
        api = _FailingUnregisterMessageApi()
        service = NativeHotkeyService(api_factory=lambda: api, command_timeout=1)
        service.configure({"panel": ("ctrl+space", lambda: None)})
        service.stop()
        self.assertFalse(service.running)

    def test_timed_out_configuration_is_rolled_back_instead_of_applying_late(self) -> None:
        api = _SlowRegisterMessageApi(delay=0.15)
        service = NativeHotkeyService(api_factory=lambda: api, command_timeout=0.03)
        try:
            with self.assertRaisesRegex(HotkeyUnavailableError, "timed out"):
                service.configure({"panel": ("ctrl+alt+q", lambda: None)})

            deadline = time.monotonic() + 1.0
            while (service.bindings or api.registered) and time.monotonic() < deadline:
                time.sleep(0.01)
            # The worker can still be inside the delayed native call when the
            # timeout is raised, so also wait until that call has had time to
            # finish and perform its compensating rollback.
            time.sleep(0.2)
            self.assertEqual({}, service.bindings)
            self.assertEqual({}, api.registered)
        finally:
            service._command_timeout = 1.0
            service.stop()


if __name__ == "__main__":
    unittest.main()
