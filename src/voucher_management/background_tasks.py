"""Small GUI-neutral worker primitive for blocking operations.

Tk widgets must stay on the main thread. Workers therefore communicate only
through a Queue; the UI polls that queue from its own event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from threading import Thread
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class BackgroundResult(Generic[T]):
    """Completed worker value or exception, never both."""

    value: T | None = None
    error: Exception | None = None


def start_background_task(worker: Callable[[], T]) -> Queue[BackgroundResult[T]]:
    """Run one blocking callable on a daemon thread and return its result queue."""

    results: Queue[BackgroundResult[T]] = Queue(maxsize=1)

    def run() -> None:
        try:
            results.put(BackgroundResult(value=worker()))
        except Exception as exc:
            results.put(BackgroundResult(error=exc))

    Thread(
        target=run,
        name="voucher-management-worker",
        daemon=True,
    ).start()
    return results
