"""The event log the console polls.

An in-memory ring with a monotonic sequence number. The browser sends the
highest `seq` it has seen and gets everything after it, which is the whole
mechanism behind the toasts: no websocket, no SSE, no push channel. The console
already polls /api/state at 1 Hz, so riding along on that poll costs nothing
and survives a dropped connection by construction — a reconnecting client just
asks from its old cursor again.

Deliberately not persisted. These are notifications about things happening now;
a console reloaded an hour later should not replay an hour of attacks. The
durable record is the MLflow run.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

#: Enough to cover a long demo without unbounded growth. At one attack every
#: five minutes and two events each, this is several hours of history.
_MAX_EVENTS = 200

_lock = threading.Lock()
_seq = itertools.count(1)
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)


def emit(**fields: Any) -> dict[str, Any]:
    """Append one event and return it, stamped with its sequence and time."""
    with _lock:
        event = {
            "seq": next(_seq),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        _events.append(event)
        return event


def since(cursor: int) -> list[dict[str, Any]]:
    """Every event after `cursor`, oldest first."""
    with _lock:
        return [e for e in _events if e["seq"] > cursor]


def latest_seq() -> int:
    with _lock:
        return _events[-1]["seq"] if _events else 0


def reset() -> None:
    """Test seam. Never called in normal operation."""
    global _seq
    with _lock:
        _events.clear()
        _seq = itertools.count(1)
