"""Per-pump rolling feature window.

``FeatureWindow`` holds a fixed-size ``deque`` per pump_id. Each
incoming telemetry dict is appended to the matching pump's deque,
oldest entries are evicted automatically by ``deque(maxlen=...)``, and
``snapshot(pump_id)`` returns the current window as a list (a
defensive copy — callers mustn't mutate it).

This module is **local-only** state. The Lambda hot path reconstructs
its window from a DynamoDB read on each invocation, so there's no
analogous structure in ``shared/`` or ``lambda_scorer/`` and that's
correct — the in-memory deque here is an implementation detail of the
local subscriber, not part of the mode-parity boundary. The boundary
is the pure ``shared.features.extract_features`` function: both the
local subscriber and the Lambda handler call it with a list of
telemetry dicts, and the source of that list (deque vs. DynamoDB query)
is the local-vs-AWS difference.

Concurrency: ``FeatureWindow`` is NOT thread-safe but IS asyncio-safe
in the obvious single-loop sense — the subscriber task is the only
caller and a single asyncio loop is single-threaded by construction.
If a future session adds a second consumer, that's the point where a
lock would be justified.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping


class FeatureWindow:
    """Per-pump bounded rolling buffer of telemetry dicts.

    Each pump_id gets its own ``deque(maxlen=window_samples)``.
    Pumps are created lazily on first ``append``.

    Window sizing comes from ``LocalRuntimeConfig.window_samples``
    (= ceil(300 / tick_seconds)). At the 2-second default tick rate
    this is 150 samples = exactly 5 minutes of wall clock. Larger
    windows are accepted but the rolling stats start to lag the
    process; smaller windows make the std estimate noisier.
    """

    def __init__(self, window_samples: int) -> None:
        if window_samples < 1:
            raise ValueError(
                f"window_samples must be >= 1, got {window_samples}"
            )
        self._window_samples = window_samples
        self._windows: dict[str, deque[Mapping[str, float]]] = {}

    @property
    def window_samples(self) -> int:
        return self._window_samples

    def append(self, pump_id: str, reading: Mapping[str, float]) -> None:
        """Append a telemetry reading to ``pump_id``'s rolling window.

        Creates the deque lazily on first sight of a pump_id. Evicts
        the oldest entry automatically once the window is full.
        """
        if pump_id not in self._windows:
            self._windows[pump_id] = deque(maxlen=self._window_samples)
        self._windows[pump_id].append(reading)

    def snapshot(self, pump_id: str) -> list[Mapping[str, float]]:
        """Return a list copy of ``pump_id``'s current window.

        Empty list if the pump has never been seen — callers should
        treat this as "no data yet" rather than as an error. Returns
        a defensive copy so the caller can pass it into
        ``shared.features.extract_features`` without risking the
        deque mutating mid-iteration.
        """
        window = self._windows.get(pump_id)
        if window is None:
            return []
        return list(window)

    def size(self, pump_id: str) -> int:
        """Current number of readings held for ``pump_id``.

        Useful for the warm-up check ("don't score until the window is
        at least N samples deep"). Returns 0 if the pump has never
        been seen.
        """
        window = self._windows.get(pump_id)
        return len(window) if window is not None else 0

    def pumps(self) -> Iterable[str]:
        """Iterate the pump_ids currently held."""
        return self._windows.keys()
