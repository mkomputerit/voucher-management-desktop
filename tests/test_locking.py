import os
import time

import pytest

from voucher_management import locking
from voucher_management.locking import LockTimeout, exclusive_file_lock


def test_lock_is_created_and_removed(tmp_path):
    path = tmp_path / "history.lock"

    with exclusive_file_lock(path):
        assert path.exists()

    assert not path.exists()


def test_stale_lock_is_recovered(tmp_path):
    path = tmp_path / "history.lock"
    path.write_text("stale", encoding="utf-8")
    stale_time = time.time() - 300
    os.utime(path, (stale_time, stale_time))

    with exclusive_file_lock(path, stale_after=1):
        assert path.exists()

    assert not path.exists()


def test_active_lock_times_out_without_deleting_it(tmp_path):
    path = tmp_path / "history.lock"
    path.write_text("active", encoding="utf-8")

    with pytest.raises(LockTimeout):
        with exclusive_file_lock(
            path,
            timeout=0.05,
            stale_after=300,
        ):
            pass

    assert path.exists()


def test_failed_lock_metadata_write_removes_partial_file(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "history.lock"

    def fail_write(_fd, _data):
        raise OSError("simulated write failure")

    monkeypatch.setattr(locking.os, "write", fail_write)

    with pytest.raises(OSError):
        with exclusive_file_lock(path):
            pass

    assert not path.exists()
