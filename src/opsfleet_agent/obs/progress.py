"""Turn progress events for the CLI spinner (D-147): output-only, never part of the turn.

The graph calls :func:`report` with a fixed event name (a node name, or ``tool:<name>``
for a tool the analyst requested). Nothing else crosses this seam: no model output, no
user text, no state. With no hook installed (evals, tests, non-TTY CLI) it is a no-op.

A hook that raises is swallowed and removed for the rest of its :func:`reporting` block,
so progress display can never break, change or slow a turn.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

__all__ = ["TOOL_PREFIX", "report", "reporting"]

log = logging.getLogger(__name__)

TOOL_PREFIX: Final = "tool:"

ProgressHook = Callable[[str], None]

_lock = threading.Lock()
_hook: ProgressHook | None = None


def report(event: str) -> None:
    """Pass ``event`` to the installed hook, if any. Never raises."""
    global _hook
    hook = _hook
    if hook is None:
        return
    try:
        hook(event)
    except Exception as exc:  # a broken display never breaks the turn: disable it
        log.warning("progress hook failed (%s); progress display off", type(exc).__name__)
        with _lock:
            if _hook is hook:
                _hook = None


@contextmanager
def reporting(hook: ProgressHook | None) -> Iterator[None]:
    """Install ``hook`` for the block; the previous hook is always restored."""
    global _hook
    with _lock:
        previous, _hook = _hook, hook
    try:
        yield
    finally:
        with _lock:
            _hook = previous
