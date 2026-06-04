"""Service orchestrator: subscriber -> features -> score -> drift -> writer.

This module glues the pieces together but contains no logic that
needs to land in Lambda. The mode-parity boundary is the pure
``shared.features.extract_features`` call (and
``shared.score.score`` / ``shared.drift.compute_psi``); everything in
this file is local-only orchestration.

Per-message flow:

1. Subscriber yields ``(pump_id, telemetry_dict)``.
2. ``FeatureWindow.append(pump_id, telemetry)`` adds to the per-pump
   rolling deque used by ``extract_features`` for the 5-min rolling
   stats.
3. ``shared.features.extract_features(window)`` projects to the
   8-feature vector.
4. ``shared.score.score(features)`` returns the failure probability.
5. The per-pump *feature history* deque (separate from the telemetry
   window above) records the resulting feature dict; it spans the
   PSI window length (1 hour by default = 1800 samples at 2s tick).
6. Every ``psi_period_ticks`` ticks per pump, ``shared.drift.compute_psi``
   runs over the feature-history deque (with the cached reference
   loaded at init) and the result lands in ``ScoredRow.psi``. On
   non-compute ticks ``psi`` is ``None`` and ``InfluxWriter`` skips
   the ``psi_*`` fields (decided in ADR 0007).
7. ``InfluxWriter.write(ScoredRow)`` writes one point per scored reading.

Reference loading: the drift module's ``load_reference()`` is the
single I/O entry for the reference distribution (Gemini Q2 of the
2026-06-01 review made this explicit, replacing the previous
``compute_psi(reference=None)`` implicit-load shape). ``ScorerService``
calls it once at init and stores the dict; ``compute_psi`` is then
called with the explicit reference on each tick.

Step 3 is the same call site Lambda will use. Steps 2 + 5 own
in-memory state that's local-only by design; Lambda reconstructs both
windows from DynamoDB at invocation time. See ``context/local_runtime.md``
section "Mode parity invariant" for the local-vs-Lambda state-management
asymmetry.

Warm-up policy: the first message for each pump produces a 1-sample
"window" which is meaningless for rolling stats. We score it anyway
(the score function tolerates short windows) so the InfluxDB row count
matches the message count exactly. PSI on a 1-sample window is
mathematically valid (one bin holds 100% of mass) but a clear outlier
versus a healthy steady-state; the every-Nth-tick cadence (default 30
ticks at 2s tick = once per minute) ensures the first PSI value
doesn't drop into the InfluxDB row until the window has had time to
fill.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Mapping, Optional

from local_runtime.config import LocalRuntimeConfig
from local_runtime.influx_writer import InfluxWriter, ScoredRow
from local_runtime.window import FeatureWindow
from shared.drift import compute_psi, load_reference
from shared.features import extract_features
from shared.score import score as score_fn


log = logging.getLogger(__name__)


class ScorerService:
    """Per-message orchestrator.

    Holds the rolling-window state + the per-pump PSI feature history
    + the cached reference distribution + the InfluxDB writer;
    exposes a single async ``handle`` method matching
    ``MessageHandler``. The subscriber drives ``handle`` directly so
    the asyncio shape is one task per message dispatched serially
    through the handler.

    Mode parity invariant: ``handle`` calls ``extract_features``,
    ``score_fn``, and ``compute_psi`` exactly the way the Lambda
    handler will. If a future change here needs to do something the
    Lambda can't (e.g., long-lived in-memory state besides the rolling
    windows themselves), the right move is to extract the pure-logic
    core into ``shared/`` first.
    """

    def __init__(
        self,
        config: LocalRuntimeConfig,
        writer: InfluxWriter,
        *,
        window: FeatureWindow | None = None,
        psi_period_ticks: int | None = None,
        reference: Mapping[str, object] | None = None,
    ) -> None:
        self._config = config
        self._writer = writer
        self._window = window or FeatureWindow(
            window_samples=config.window_samples
        )
        # Period in ticks between PSI computations. Defaults to the
        # config-derived value (60s / tick_seconds). Tests inject 1 to
        # force every-tick PSI without changing tick_seconds (which
        # would distort the feature-window size assertions).
        self._psi_period_ticks: int = (
            psi_period_ticks
            if psi_period_ticks is not None
            else config.psi_period_ticks
        )
        # Reference distribution -- loaded once at init via the drift
        # module's single I/O entry point (Gemini Q2 of 2026-06-01
        # review). Tests inject a synthetic dict to skip the disk
        # path; the live service path calls ``load_reference()`` with
        # the default paths.
        self._reference: Mapping[str, object] = (
            reference if reference is not None else load_reference()
        )
        # Per-pump rolling deque of past *feature* dicts, used as the
        # PSI window source. Distinct from ``self._window`` (which
        # holds raw telemetry for the 5-min rolling stats). Length =
        # PSI window in samples (1 hour at the default tick).
        self._feature_history: dict[str, Deque[Mapping[str, float]]] = {}
        # Per-pump tick counter. Increments on every ``handle`` call
        # for that pump; the modulo against ``_psi_period_ticks`` picks
        # which ticks compute PSI.
        self._tick_count: dict[str, int] = {}

    @property
    def window(self) -> FeatureWindow:
        return self._window

    @property
    def psi_period_ticks(self) -> int:
        """Exposed so tests can pin the value without poking the field."""
        return self._psi_period_ticks

    def feature_history_size(self, pump_id: str) -> int:
        """Current depth of ``pump_id``'s PSI feature history deque.

        Useful for the smoke test ("after 30 messages a pump should
        have 30 entries in its PSI window") and a future warm-up gate.
        Returns 0 if the pump has never been seen.
        """
        hist = self._feature_history.get(pump_id)
        return len(hist) if hist is not None else 0

    async def handle(self, pump_id: str, telemetry: dict[str, Any]) -> None:
        """Score one telemetry message and write the row to InfluxDB.

        Robustness: missing-field errors in ``extract_features`` are
        caught and logged -- one bad message shouldn't kill the
        service. Transient InfluxDB write failures are left to bubble
        (the subscriber's retry loop handles transport recovery, but
        the writer's network errors don't share that path); a future
        session may add a write-side retry.
        """
        self._window.append(pump_id, telemetry)
        window_snapshot = self._window.snapshot(pump_id)

        try:
            features = extract_features(window_snapshot)
        except (KeyError, ValueError) as e:
            log.warning(
                "feature extraction failed for %s: %s; skipping row",
                pump_id, e,
            )
            return

        score_value = score_fn(features)

        # Append to the PSI feature history. Lazy-create the deque so
        # we don't allocate 1800 slots per pump until a pump is first
        # seen.
        feat_hist = self._feature_history.get(pump_id)
        if feat_hist is None:
            feat_hist = deque(maxlen=self._config.psi_window_samples)
            self._feature_history[pump_id] = feat_hist
        feat_hist.append(features)

        # Tick bookkeeping + cadence check. tick_count starts at 0 for
        # new pumps; we increment *before* the modulo so the first
        # compute happens at tick == psi_period_ticks rather than at
        # tick 0 (which would mean computing PSI on a 1-sample window
        # at every pump's first message -- noisy and visually confusing
        # on the demo dashboard).
        tick = self._tick_count.get(pump_id, 0) + 1
        self._tick_count[pump_id] = tick

        psi: Optional[Mapping[str, float]]
        if tick % self._psi_period_ticks == 0:
            psi = compute_psi(list(feat_hist), reference=self._reference)
        else:
            psi = None

        row = ScoredRow(
            pump_id=pump_id,
            timestamp=_parse_ts(telemetry.get("ts")),
            features=features,
            score=score_value,
            psi=psi,
        )
        await self._writer.write(row)


def _parse_ts(value: Any) -> datetime:
    """Parse the simulator's ISO-8601 ts field; fall back to "now" UTC.

    The simulator publishes ``ts: "YYYY-MM-DDTHH:MM:SS.fffZ"`` per
    ``context/_interfaces.md``. ``datetime.fromisoformat`` in 3.10
    doesn't accept the trailing ``Z`` so we normalize to ``+00:00``
    first. A missing or malformed ts logs WARNING and falls back to
    the current UTC time -- the InfluxDB row still lands but the
    timestamp gap is a signal worth catching in logs.
    """
    if not isinstance(value, str):
        log.warning("telemetry missing ts; falling back to now()")
        return datetime.now(timezone.utc)
    iso = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as e:
        log.warning("telemetry ts %r unparseable: %s; falling back to now()", value, e)
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
