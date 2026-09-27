"""The lifecycle a held-key tactic needs, written once. CR-018-10 Phase B.

Each tactic thread in the controller hand-rolled the same parts: a running flag set
synchronously before the spawn (the ADR 070 d8 pattern, so a second trigger in the
same tick cannot start a rival hold), a stop event cleared on start, a thread
handle, and a `finally` that clears the running flag. The copies drifted in what
they cleared and when.

`HoldTactic` is those parts. It wraps a tactic's existing `threading.Event`s rather
than replacing them, so the attributes the rest of the controller and the tests
use keep working while tactics move onto it one at a time. The flight-writer
registry (Phase A, `Controller._flight_writers`) reads `stop` and `thread` from it.
"""

import threading


class HoldTactic:
    def __init__(self, name: str, *, running: "threading.Event | None" = None,
                 stop: "threading.Event | None" = None):
        self.name = name
        self.running = running if running is not None else threading.Event()
        self.stop_event = stop if stop is not None else threading.Event()
        self.thread: "threading.Thread | None" = None

    def start(self, body, *, thread_name: "str | None" = None) -> bool:
        """Run ``body()`` on a daemon thread, unless the tactic is already running.

        The running flag is set here, before the thread exists, and cleared in
        the thread's ``finally`` after ``body`` returns or raises. ``body`` does
        its own key releases in its own ``finally``, which runs first.
        """
        if self.running.is_set():
            return False
        self.running.set()
        self.stop_event.clear()

        def _run():
            try:
                body()
            finally:
                self.running.clear()

        self.thread = threading.Thread(target=_run, daemon=True,
                                       name=thread_name or self.name)
        self.thread.start()
        return True

    def stop(self, _reason: str = "") -> None:
        self.stop_event.set()

    def is_running(self) -> bool:
        return self.running.is_set()

    def join(self, timeout: float) -> bool:
        """True when the thread has finished (or never started) within ``timeout``."""
        thread = self.thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()
