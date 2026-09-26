import multiprocessing

import pytest

from voucher_management.single_instance import (
    InstanceAlreadyRunning,
    SingleInstanceGuard,
)


def _hold_instance(path, ready, release):
    """Own the per-user guard in a real child process."""

    guard = SingleInstanceGuard(path)
    guard.acquire()
    ready.set()
    release.wait(5)
    guard.release()


def test_second_instance_is_rejected_immediately(tmp_path):
    path = tmp_path / "application.instance.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_instance, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(InstanceAlreadyRunning):
            SingleInstanceGuard(path).acquire()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0


def test_guard_can_be_reacquired_after_clean_shutdown(tmp_path):
    path = tmp_path / "application.instance.lock"

    first = SingleInstanceGuard(path)
    first.acquire()
    first.release()

    with SingleInstanceGuard(path):
        assert path.exists()


def test_guard_can_be_reacquired_after_owner_crash(tmp_path):
    path = tmp_path / "application.instance.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_instance, args=(path, ready, release))
    process.start()
    assert ready.wait(5)

    process.terminate()
    process.join(5)
    assert process.exitcode is not None

    with SingleInstanceGuard(path):
        assert path.exists()
