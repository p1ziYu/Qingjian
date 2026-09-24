"""A single-worker background queue for file operations.

The previous version ran every classification inside a nested event loop with
the window disabled, so pressing a number key froze the interface until the
copy and its verification finished. Here the key press only enqueues: the view
advances at once and the work happens behind it.

One worker on purpose — the transaction store permits a single journal at a
time, and two concurrent moves of the same shot would be a race.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .logsetup import get_logger
from .safestore import Cancelled

log = get_logger("queue")

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class Job:
    run: Callable[[Callable[[str, int], None], Callable[[], bool]], object]
    label: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = PENDING
    error: BaseException | None = None
    result: object = None
    created: float = field(default_factory=time.time)
    context: dict = field(default_factory=dict)


class OperationQueue:
    """Serial worker with progress, failure retention and a drain barrier."""

    def __init__(self, on_event: Callable[[str, Job], None] | None = None) -> None:
        self._queue: "queue.Queue[Job | None]" = queue.Queue()
        self._on_event = on_event or (lambda event, job: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cancel_current = threading.Event()
        self._lock = threading.RLock()
        self._current: Job | None = None
        self._failed: list[Job] = []
        self._pending_count = 0
        self._idle = threading.Event()
        self._idle.set()

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qingjian-ops", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None

    # -- submission ----------------------------------------------------
    def submit(self, job: Job) -> Job:
        with self._lock:
            self._pending_count += 1
            self._idle.clear()
        self._queue.put(job)
        self._on_event("queued", job)
        self.start()
        return job

    def cancel_current(self) -> None:
        self._cancel_current.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # -- inspection ----------------------------------------------------
    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending_count

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    def failures(self) -> list[Job]:
        with self._lock:
            return list(self._failed)

    def clear_failures(self) -> None:
        with self._lock:
            self._failed.clear()

    def retry_failures(self) -> int:
        with self._lock:
            jobs, self._failed = self._failed, []
        for job in jobs:
            job.state = PENDING
            job.error = None
            self.submit(job)
        return len(jobs)

    # -- worker --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                if self._stop.is_set():
                    break
                continue
            self._cancel_current.clear()
            with self._lock:
                self._current = job
            job.state = RUNNING
            self._on_event("started", job)

            def progress(message: str, percent: int, job=job) -> None:
                # Write first, then announce: the listener runs on another
                # thread and must never see a half-updated context.
                job.context["message"] = message
                job.context["percent"] = percent
                self._on_event("progress", job)

            try:
                job.result = job.run(progress, self._cancel_current.is_set)
                job.state = DONE
                self._on_event("finished", job)
            except BaseException as error:  # noqa: BLE001 - a worker must not die
                job.error = error
                job.state = CANCELLED if _is_cancel(error) else FAILED
                if job.state == FAILED:
                    with self._lock:
                        self._failed.append(job)
                    log.warning("queued operation failed: %s", error)
                self._on_event("failed" if job.state == FAILED else "cancelled", job)
            finally:
                with self._lock:
                    self._current = None
                    self._pending_count = max(0, self._pending_count - 1)
                    if self._pending_count == 0 and self._queue.empty():
                        self._idle.set()
                self._queue.task_done()


def _is_cancel(error: BaseException) -> bool:
    return isinstance(error, Cancelled)
