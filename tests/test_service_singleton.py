from __future__ import annotations

import sys
import unittest
import uuid
from dataclasses import dataclass, field

from quickaccess.services.singleton import (
    ERROR_ALREADY_EXISTS,
    InstanceRequest,
    SingleInstanceGuard,
    local_signal_name,
)


@dataclass
class _KernelObject:
    kind: str
    name: str
    references: int = 0
    signaled: bool = False


@dataclass
class _Handle:
    target: _KernelObject
    closed: bool = False


@dataclass
class _FakeKernelState:
    objects: dict[tuple[str, str], _KernelObject] = field(default_factory=dict)
    fail_event_suffix: str | None = None


class _FakeNativeApi:
    def __init__(
        self,
        state: _FakeKernelState | None = None,
        *,
        supported: bool = True,
    ) -> None:
        self.state = state or _FakeKernelState()
        self.supported = supported
        self.calls: list[tuple[str, str | None]] = []
        self._last_error = 0

    def create_mutex(self, name: str) -> tuple[object | None, int]:
        self.calls.append(("create_mutex", name))
        key = ("mutex", name)
        target = self.state.objects.get(key)
        error = 0
        if target is None:
            target = _KernelObject("mutex", name)
            self.state.objects[key] = target
        else:
            error = ERROR_ALREADY_EXISTS
        target.references += 1
        self._last_error = error
        return _Handle(target), error

    def create_event(self, name: str) -> object | None:
        self.calls.append(("create_event", name))
        if self.state.fail_event_suffix and name.endswith(self.state.fail_event_suffix):
            self._last_error = 5
            return None
        key = ("event", name)
        target = self.state.objects.get(key)
        if target is None:
            target = _KernelObject("event", name)
            self.state.objects[key] = target
        target.references += 1
        self._last_error = 0
        return _Handle(target)

    def open_event(self, name: str) -> object | None:
        self.calls.append(("open_event", name))
        target = self.state.objects.get(("event", name))
        if target is None:
            self._last_error = 2
            return None
        target.references += 1
        self._last_error = 0
        return _Handle(target)

    def set_event(self, handle: object) -> bool:
        self.calls.append(("set_event", None))
        if not isinstance(handle, _Handle) or handle.closed:
            self._last_error = 6
            return False
        handle.target.signaled = True
        self._last_error = 0
        return True

    def poll_event(self, handle: object) -> bool:
        self.calls.append(("poll_event", None))
        if not isinstance(handle, _Handle) or handle.closed:
            self._last_error = 6
            return False
        was_signaled = handle.target.signaled
        handle.target.signaled = False
        self._last_error = 0
        return was_signaled

    def close_handle(self, handle: object) -> bool:
        self.calls.append(("close_handle", None))
        if not isinstance(handle, _Handle) or handle.closed:
            self._last_error = 6
            return False
        handle.closed = True
        handle.target.references -= 1
        if handle.target.references == 0:
            self.state.objects.pop((handle.target.kind, handle.target.name), None)
        self._last_error = 0
        return True

    def last_error(self) -> int:
        return self._last_error


class _ExplodingEventApi(_FakeNativeApi):
    def create_event(self, name: str) -> object | None:
        raise RuntimeError(f"event API unavailable: {name}")


class SingleInstanceSignalTests(unittest.TestCase):
    def test_signal_names_are_stable_and_session_local(self) -> None:
        self.assertEqual(
            r"Local\QuickAccess-Signal-ShowPanel",
            local_signal_name("QuickAccess", InstanceRequest.SHOW_PANEL),
        )
        self.assertEqual(
            r"Local\QuickAccess-Signal-OpenSettings",
            local_signal_name(r"Local\QuickAccess", "open-settings"),
        )

    def test_later_instance_can_request_panel_without_taking_mutex(self) -> None:
        state = _FakeKernelState()
        owner = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        later = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        third = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))

        try:
            self.assertTrue(owner.acquire())
            self.assertTrue(owner.signaling_available)
            self.assertFalse(later.acquire())
            self.assertTrue(later.already_running)

            self.assertTrue(later.notify_existing())
            self.assertEqual((InstanceRequest.SHOW_PANEL,), owner.drain_requests())
            self.assertEqual((), owner.drain_requests())

            # Sending a signal did not disturb authoritative mutex ownership.
            self.assertFalse(third.acquire())
        finally:
            later.close()
            third.close()
            owner.close()

    def test_panel_and_settings_requests_are_independent_and_coalesced(self) -> None:
        state = _FakeKernelState()
        owner = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        later = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        try:
            self.assertTrue(owner.acquire())
            self.assertFalse(later.acquire())
            self.assertTrue(later.notify_existing(InstanceRequest.OPEN_SETTINGS))
            self.assertTrue(later.notify_existing(InstanceRequest.OPEN_SETTINGS))
            self.assertTrue(later.notify_existing(InstanceRequest.SHOW_PANEL))

            self.assertEqual(
                (InstanceRequest.SHOW_PANEL, InstanceRequest.OPEN_SETTINGS),
                owner.drain_requests(),
            )
            self.assertEqual((), owner.drain_requests())
        finally:
            later.close()
            owner.close()

    def test_missing_signal_endpoint_fails_cleanly_but_mutex_stays_owned(self) -> None:
        state = _FakeKernelState(fail_event_suffix="OpenSettings")
        owner = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        later = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        third = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        try:
            self.assertTrue(owner.acquire())
            self.assertFalse(owner.signaling_available)
            self.assertFalse(later.acquire())
            self.assertFalse(later.notify_existing(InstanceRequest.SHOW_PANEL))
            self.assertEqual((), owner.drain_requests())
            self.assertFalse(third.acquire())
        finally:
            later.close()
            third.close()
            owner.close()

    def test_unexpected_event_api_failure_does_not_prevent_mutex_ownership(self) -> None:
        state = _FakeKernelState()
        owner = SingleInstanceGuard(
            "QuickAccessTest", native_api=_ExplodingEventApi(state)
        )
        later = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        try:
            self.assertTrue(owner.acquire())
            self.assertFalse(owner.signaling_available)
            self.assertFalse(later.acquire())
            self.assertFalse(later.notify_existing())
        finally:
            later.close()
            owner.close()

    def test_unsupported_host_fails_open_without_touching_native_api(self) -> None:
        api = _FakeNativeApi(supported=False)
        guard = SingleInstanceGuard("QuickAccessTest", native_api=api)

        self.assertTrue(guard.acquire())
        self.assertTrue(guard.acquired)
        self.assertFalse(guard.already_running)
        self.assertFalse(guard.signaling_available)
        self.assertFalse(guard.notify_existing())
        self.assertEqual((), guard.drain_requests())
        self.assertEqual([], api.calls)
        guard.close()

    def test_owner_close_is_idempotent_and_releases_all_kernel_objects(self) -> None:
        state = _FakeKernelState()
        owner = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))
        replacement = SingleInstanceGuard(
            "QuickAccessTest", native_api=_FakeNativeApi(state)
        )

        self.assertTrue(owner.acquire())
        self.assertEqual(3, len(state.objects))
        owner.close()
        owner.close()
        self.assertEqual({}, state.objects)

        self.assertTrue(replacement.acquire())
        replacement.close()
        self.assertEqual({}, state.objects)

    def test_notification_requires_a_detected_owner(self) -> None:
        state = _FakeKernelState()
        guard = SingleInstanceGuard("QuickAccessTest", native_api=_FakeNativeApi(state))

        self.assertFalse(guard.notify_existing())
        self.assertTrue(guard.acquire())
        self.assertFalse(guard.notify_existing())
        guard.close()

    @unittest.skipUnless(sys.platform == "win32", "native Windows event test")
    def test_native_adapter_delivers_settings_request(self) -> None:
        name = f"QuickAccessSignalTest-{uuid.uuid4()}"
        owner = SingleInstanceGuard(name)
        later = SingleInstanceGuard(name)
        try:
            self.assertTrue(owner.acquire())
            self.assertTrue(owner.signaling_available)
            self.assertFalse(later.acquire())
            self.assertTrue(later.notify_existing(InstanceRequest.OPEN_SETTINGS))
            self.assertEqual(
                (InstanceRequest.OPEN_SETTINGS,),
                owner.drain_requests(),
            )
        finally:
            later.close()
            owner.close()


if __name__ == "__main__":
    unittest.main()
