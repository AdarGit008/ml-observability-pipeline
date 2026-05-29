"""Service orchestrator: subscriber → features → score → drift → writer.

This module glues the pieces together but contains no logic that
needs to land in Lambda. The mode-parity boundary is the pure
``shared.features.extract_features`` call (and the future
``shared.score.score`` / ``shared.drift.compute_psi``); everything in
this file is local-only orchestration.

Per-message flow:

1. Subscriber yields ``(pump_id, telemetry_dict)``.
2. ``FeatureWindow.append(pump_id, telemetry)`` — adds to the per-pump
   rolling deque.
3. ``shared.features.extract_features(window)`` — pure projection to
   the 8-feature vector.
4. ``shared.score.score(features)`` — stub returns deterministic
   placeholder.
5. ``shared.drift.compute_psi(window_features, reference)`` — stub
   returns sentinel PSI dict.
6. ``InfluxWriter.write(ScoredRow)`` — one point per scored reading.

Step 3 is the same call site Lambda will use. Step 2's window source
is the only thing that differs between modes: locally it's an
in-memory deque; in Lambda it's a DynamoDB read + append. Same data
shape, same downstream calls.

Warm-up policy: the first message for each pump produces a 1-sample
"window" which is meaningless for rolling stats. We score it anyway
(the stub doesn't care about window length) so the InfluxDB row count
matches the message count exactly — useful for the smoke step in the
session DoD. The real model will need a warm-up gate, which is the
drift session's territory.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from local_runtime.config import LocalRuntimeConfig
from local_runtime.influx_writer import InfluxWriter, ScoredRow
from local_runtime.window import FeatureWindow
from shared.drift import compute_psi
from shared.features import extract_features
from shared.score import score as score_fn


log = logging.getLogger(__name__)


class ScorerService:
    """Per-message orchestrator.

    Holds the rolling-window state + the InfluxDB writer; exposes a
    single async ``handle`` method matching ``MessageHandler``. The
    subscriber drives ``handle`` directly so the asyncio shape is
    one task per message dispatched serially through the handler.

    Mode parity invariant: ``handle`` calls ``extract_features``,
    ``score_fn``, and ``compute_psi`` exactly the way the Lambda
    handler will. If a future change here needs to do something the
    Lambda can't (e.g., long-lived in-memory state besides the rolling
    window itself), the right move is to extract the pure-logic core
    into ``shared/`` first.
    """

    def __init__(
        self,
        config: LocalRuntimeConfig,
        writer: InfluxWriter,
        *,
        window: FeatureWindow | None = None,
    ) -> None:
        self._config = config
        self._writer = writer
        self._window = window or FeatureWindow(
            window_samples=config.window_samples
        )

    @property
    def window(self) -> FeatureWindow:
        return self._window

    async def handle(self, pump_id: str, telemetry: dict[str, Any]) -> None:
        """Score one telemetry message and write the row to InfluxDB.

        Robustness: missing-field errors in ``extract_features`` are
        caught and logged — one bad message shouldn't kill the
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
        # Drift stub takes a window of feature dicts; for now we hand
        # in a single-element list. The real implementation will use
        # the full 1-hour PSI window per ``context/_interfaces.md``;
        # the call site stays the same.
        psi = compute_psi([features], reference=None)

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
    the current UTC time — the InfluxDB row still lands but the
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
