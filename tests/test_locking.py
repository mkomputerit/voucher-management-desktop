import multiprocessing
import os
import time

import pytest

from voucher_management import locking
from voucher_management.locking import LockTimeout, exclusive_file_lock


def _hold_lock(path, ready, release):
    """Child-process helper: hold the real OS lock until the parent releases it."""

    with exclusive_file_lock(path, timeout=2):
        ready.set()
        release.wait(5)


def test_lock_file_is_persistent_and_reusable(tmp_path):
    path = tmp_path / "history.lock"

    with exclusive_file_lock(path):
        assert path.exists()

    # Persistent lock paths are intentional: unlinking an advisory-lock file
    # can let two processes lock two different filesystem objects.
    assert path.exists()
    with exclusive_file_lock(path):
        assert path.exists()


def test_old_timestamp_does_not_trigger_unlink(tmp_path):
    path = tmp_path / "history.lock"
    path.write_bytes(b"1")
    stale_time = time.time() - 300
    os.utime(path, (stale_time, stale_time))
    inode_before = path.stat().st_ino

    with exclusive_file_lock(path, stale_after=0):
        assert path.stat().st_ino == inode_before

    assert path.exists()


def test_competing_process_times_out_without_deleting_lock(tmp_path):
    path = tmp_path / "history.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(5), "child process did not acquire the lock"
        with pytest.raises(LockTimeout):
            with exclusive_file_lock(path, timeout=0.05, stale_after=0):
                pass
        assert path.exists()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0


def test_crashed_process_releases_os_lock_without_stale_cleanup(tmp_path):
    path = tmp_path / "history.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(path, ready, release))
    process.start()
    assert ready.wait(5), "child process did not acquire the lock"

    # Termination simulates an abrupt process exit: no Python finally block can
    # run, so successful reacquisition proves the OS released the lock.
    process.terminate()
    process.join(5)
    assert process.exitcode is not None

    with exclusive_file_lock(path, timeout=2, stale_after=0):
        assert path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt error mapping")
def test_windows_try_lock_retries_only_real_contention(monkeypatch, tmp_path):
    """Unrelated Windows I/O errors must never masquerade as lock contention."""

    import errno
    import msvcrt

    path = tmp_path / "windows.lock"
    path.write_bytes(b"1")
    fd = os.open(str(path), os.O_RDWR)
    try:
        monkeypatch.setattr(
            msvcrt,
            "locking",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EACCES, "busy")),
        )
        assert locking._try_lock(fd) is False

        monkeypatch.setattr(
            msvcrt,
            "locking",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EPERM, "denied")),
        )
        with pytest.raises(OSError) as exc_info:
            locking._try_lock(fd)
        assert exc_info.value.errno == errno.EPERM
    finally:
        os.close(fd)
