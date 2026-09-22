from __future__ import annotations

import threading

from voucher_management.background_tasks import start_background_task


def test_background_task_runs_off_calling_thread_and_returns_value():
    main_thread = threading.get_ident()

    results = start_background_task(
        lambda: (threading.get_ident(), "done")
    )
    outcome = results.get(timeout=2)

    assert outcome.error is None
    assert outcome.value[0] != main_thread
    assert outcome.value[1] == "done"


def test_background_task_returns_exception_without_raising_on_worker():
    results = start_background_task(
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic"))
    )
    outcome = results.get(timeout=2)

    assert outcome.value is None
    assert isinstance(outcome.error, RuntimeError)
    assert str(outcome.error) == "synthetic"
