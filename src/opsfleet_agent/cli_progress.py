"""One-line stage spinner while a chat turn runs (D-147).

``⠋ Understanding the question…`` → ``⠙ Reading the schema…`` → ``⠹ Running SQL (2)…``,
redrawn in place on stdout and erased completely before anything else is printed.

Output-only by design:

* the labels are the fixed strings in :data:`STAGE_LABELS` (plus an int SQL counter): no
  model output and no user text is ever shown here, so nothing bypasses the output guard;
* no tokens are streamed; the answer is still printed only after the guard, as before;
* only when stdout is a TTY (and ``TERM`` is not ``dumb``); otherwise nothing is written;
* the drawing thread is a daemon, its loop is bounded (:data:`MAX_FRAMES`), and
  :meth:`Spinner.stop` is idempotent and always erases what was drawn;
* events come through :mod:`opsfleet_agent.obs.progress`, which swallows hook errors.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from contextlib import ExitStack
from typing import Final, TextIO

from opsfleet_agent.obs import progress

__all__ = ["STAGE_LABELS", "Spinner", "stage_label"]

log = logging.getLogger(__name__)

FRAMES: Final = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVAL_S: Final = 0.1
MAX_FRAMES: Final = 6000  # ~10 min at INTERVAL_S: longer than any turn budget, then it stops
JOIN_BOUND_S: Final = 0.5
ERASE: Final = "\r\x1b[2K"  # carriage return + erase the whole line
START_LABEL: Final = "Working…"
SQL_EVENT: Final = progress.TOOL_PREFIX + "run_sql"
SQL_LABEL: Final = "Running SQL ({n})…"

# Graph node names and requested tools -> what the user sees. One table; an event that is
# not here keeps the current label.
STAGE_LABELS: Final[dict[str, str]] = {
    "input_guard": "Understanding the question…",
    "light": "Writing the answer…",
    "load_context": "Gathering context…",
    "quick": "Planning the analysis…",
    "deep": "Planning the analysis…",
    progress.TOOL_PREFIX + "list_tables": "Reading the schema…",
    progress.TOOL_PREFIX + "get_schema": "Reading the schema…",
    "force_answer": "Writing the answer…",
    "grounding": "Checking the numbers…",
    "report_writer": "Writing the report…",
    "retry_writer": "Writing the report…",  # iteration 33: "retry report"
    "confirm_save": "Preparing the report…",
    "library": "Working with your saved reports…",  # iteration 46
    progress.TOOL_PREFIX + "list_reports": "Looking through your reports…",
    progress.TOOL_PREFIX + "search_reports": "Searching your reports…",
    progress.TOOL_PREFIX + "view_report": "Opening the report…",
    progress.TOOL_PREFIX + "rename_report": "Renaming the report…",
    progress.TOOL_PREFIX + "export_report": "Exporting the report…",
    progress.TOOL_PREFIX + "set_preference": "Saving your preference…",
    "delete_preview": "Preparing the delete preview…",
    "confirm_delete": "Checking the confirmation…",
    "execute_delete": "Deleting…",
    "finalize": "Checking the answer…",
}


def stage_label(event: str, sql_count: int) -> str | None:
    """The label for ``event``; ``None`` keeps the current one. Fixed strings only."""
    if event == SQL_EVENT:
        return SQL_LABEL.format(n=int(sql_count))
    return STAGE_LABELS.get(event)


def _is_tty(stream: TextIO) -> bool:
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # a closed or exotic stream: no spinner
        return False


class Spinner:
    """Draws the current stage on one line while a turn runs; a no-op off a TTY."""

    def __init__(self, stream: TextIO | None = None, *, interval_s: float | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._interval = INTERVAL_S if interval_s is None else interval_s
        self.enabled = _is_tty(self._stream)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._label = START_LABEL
        self._sql = 0
        self._drawn = False
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._hooks = ExitStack()

    @property
    def label(self) -> str:
        return self._label

    def on_event(self, event: str) -> None:
        """Progress hook: update the label (the thread redraws it)."""
        if event == SQL_EVENT:
            self._sql += 1
        label = stage_label(event, self._sql)
        if label is not None:
            with self._lock:
                self._label = label

    def start(self) -> None:
        """Install the progress hook and start drawing. Off a TTY: does nothing."""
        if not self.enabled or self._thread is not None or self._stopped:
            return
        self._hooks.enter_context(progress.reporting(self.on_event))
        self._thread = threading.Thread(target=self._run, name="cli-spinner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop drawing, erase the line, remove the hook. Idempotent; never raises."""
        try:
            with self._lock:
                self._stopped = True
                self._done.set()
                if self._drawn:
                    self._drawn = False
                    self._write(ERASE)
        finally:
            self._hooks.close()
            thread, self._thread = self._thread, None
            if thread is not None and thread is not threading.current_thread():
                thread.join(JOIN_BOUND_S)

    def _run(self) -> None:
        for i in range(MAX_FRAMES):
            if self._done.wait(self._interval):  # the first frame waits too: no flicker
                return
            with self._lock:
                if self._stopped:
                    return
                if not self._write(f"{ERASE}{FRAMES[i % len(FRAMES)]} {self._fit(self._label)}"):
                    return
                self._drawn = True

    def _fit(self, label: str) -> str:
        """Never wrap: a wrapped line cannot be erased with one carriage return."""
        width = shutil.get_terminal_size((80, 24)).columns - 3  # frame, space, margin
        return label if len(label) <= width else label[: max(width, 0)]

    def _write(self, text: str) -> bool:
        try:
            self._stream.write(text)
            self._stream.flush()
            return True
        except Exception as exc:  # a closed pipe or terminal: stop drawing, keep the turn
            log.warning("spinner write failed: %s", type(exc).__name__)
            self._stopped = True
            self._done.set()
            return False
