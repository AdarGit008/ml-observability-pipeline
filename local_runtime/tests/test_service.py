"""End-to-end tests for local_runtime.service.ScorerService.

Uses FakeWriter to assert the full per-message path: window append ->
feature extraction -> score -> PSI -> InfluxDB write.

Also pins the mode-parity invariant: ``extract_features``, ``score``,
and ``compute_psi`` are the only call sites that need to match Lambda.
The service module imports each from ``shared/`` (not a local fork),
so this test verifies the importable paths are the parity boundary.

Drift session 2026-06-01 added the per-pump feature-history deque and
the every-Nth-tick PSI cadence (ADR 0007). The PSI-presence test was
updated to inject ``psi_period_ticks=1`` so a single message produces
a row with PSI. Two new tests pin the cadence behaviour: PSI is None
on non-compute ticks and present on the compute boundary.

ADR 0009 (2026-06-03) shrank the PSI surface from 8 features to 4.
The ``row.psi`` dict's key set asserts against ``PSI_FEATURE_NAMES``
rather than ``FEATURE_NAMES``.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import timezone

import pytest

from local_runtime.config import (
    InfluxConfig,
    LocalRuntimeConfig,
    MqttConfig,
)
from local_runtime.influx_writer import ScoredRow
from local_runtime.service import ScorerService
from local_runtime.window import FeatureWindow
from shared.features import FEATURE_NAMES, PSI_FEATURE_NAMES


def _run(coro):
    return asyncio.run(coro)


def _make_config(tick_seconds: float = 2.0) -> LocalRuntimeConfig:
    return LocalRuntimeConfig(
        mqtt=MqttConfig(url="mqtt://localhost:1883", client_id="local-runtime"),
        influx=InfluxConfig(
            url="http://localhost:8086",
            token="test",
            org="ml-obs",
            bucket="pump_telemetry",
        ),
        tick_seconds=tick_seconds,
    )


class FakeWriter:
    def __init__(self) -> None:
        self.rows: list[ScoredRow] = []

    async def write(self, row: ScoredRow) -> None:
        self.rows.append(row)


def _telemetry(
    pump_id: str = "P-00",
    ts: str = "2026-05-29T12:00:00.000Z",
    vibration_amp: float = 0.42,
    bearing_temp: float = 68.3,
    motor_current: float = 4.7,
    rpm: float = 1798.0,
) -> dict:
    return {
        "pump_id": pump_id,
        "ts": ts,
        "vibration_amp": vibration_amp,
        "bearing_temp": bearing_temp,
        "motor_current": motor_current,
        "rpm": rpm,
    }


def test_service_handle_writes_one_row(_=None):
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry())

    _run(_go())
    assert len(writer.rows) == 1
    row = writer.rows[0]
    assert row.pump_id == "P-00"


def test_service_handle_populates_all_eight_features():
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry())

    _run(_go())
    row = writer.rows[0]
    assert set(row.features.keys()) == set(FEATURE_NAMES)
    assert row.features["vibration_amp"] == 0.42
    assert row.features["bearing_temp"] == 68.3


def test_service_handle_window_grows_per_message():
    """Two successive messages -> window of 2 readings for that pump."""
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry(vibration_amp=0.2))
        await svc.handle("P-00", _telemetry(vibration_amp=0.6))

    _run(_go())
    assert svc.window.size("P-00") == 2
    # Second row's rolling mean = (0.2 + 0.6) / 2 = 0.4
    assert writer.rows[1].features["vibration_amp_mean_5m"] == pytest.approx(0.4)


def test_service_handle_per_pump_isolation():
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry(vibration_amp=0.1))
        await svc.handle("P-01", _telemetry(vibration_amp=0.9))

    _run(_go())
    assert svc.window.size("P-00") == 1
    assert svc.window.size("P-01") == 1
    assert writer.rows[0].features["vibration_amp"] == 0.1
    assert writer.rows[1].features["vibration_amp"] == 0.9


def test_service_handle_parses_iso_ts():
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry(ts="2026-05-29T14:32:01.123Z"))

    _run(_go())
    row = writer.rows[0]
    assert row.timestamp.year == 2026
    assert row.timestamp.month == 5
    assert row.timestamp.day == 29
    assert row.timestamp.hour == 14
    assert row.timestamp.tzinfo == timezone.utc


def test_service_handle_missing_field_skips_write():
    """A telemetry message missing one of the 4 raw fields is logged
    and skipped -- not raised. One bad message shouldn't kill the
    service."""
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    bad = {
        "pump_id": "P-00",
        "ts": "2026-05-29T12:00:00Z",
        # missing rpm
        "vibration_amp": 0.3,
        "bearing_temp": 60.0,
        "motor_current": 4.0,
    }

    async def _go():
        await svc.handle("P-00", bad)

    _run(_go())
    # The reading is appended to the window but no row is written
    # because feature extraction failed.
    assert writer.rows == []


def test_service_handle_score_in_zero_one():
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)

    async def _go():
        await svc.handle("P-00", _telemetry(vibration_amp=99.0))

    _run(_go())
    assert 0.0 <= writer.rows[0].score <= 1.0


def test_service_handle_psi_dict_present_when_compute_fires():
    """On a compute tick (period_ticks=1 forces every-tick compute),
    PSI is populated with all PSI_FEATURE_NAMES keys (4 per ADR 0009)."""
    cfg = _make_config()
    writer = FakeWriter()
    # Override: force PSI every tick so the single-message smoke
    # path still exercises the populated branch.
    svc = ScorerService(cfg, writer, psi_period_ticks=1)

    async def _go():
        await svc.handle("P-00", _telemetry())

    _run(_go())
    row = writer.rows[0]
    assert row.psi is not None
    assert set(row.psi.keys()) == set(PSI_FEATURE_NAMES)


def test_service_handle_psi_is_none_on_non_compute_ticks():
    """With psi_period_ticks=3, only the 3rd-tick row carries PSI.
    Ticks 1 and 2 write ``psi=None`` so InfluxDB stores nulls (ADR 0007
    cadence rationale). PSI dict key set is PSI_FEATURE_NAMES per
    ADR 0009."""
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer, psi_period_ticks=3)

    async def _go():
        for _ in range(3):
            await svc.handle("P-00", _telemetry())

    _run(_go())
    assert len(writer.rows) == 3
    assert writer.rows[0].psi is None, "tick 1: pre-compute"
    assert writer.rows[1].psi is None, "tick 2: pre-compute"
    assert writer.rows[2].psi is not None, "tick 3: compute boundary"
    assert set(writer.rows[2].psi.keys()) == set(PSI_FEATURE_NAMES)


def test_service_handle_psi_period_per_pump_isolated():
    """Each pump has its own tick counter -- pump A's compute boundary
    doesn't trigger PSI on pump B."""
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer, psi_period_ticks=2)

    async def _go():
        # P-00: 2 ticks (boundary fires on tick 2)
        await svc.handle("P-00", _telemetry(pump_id="P-00"))
        await svc.handle("P-00", _telemetry(pump_id="P-00"))
        # P-01: 1 tick (no boundary yet)
        await svc.handle("P-01", _telemetry(pump_id="P-01"))

    _run(_go())
    p00_rows = [r for r in writer.rows if r.pump_id == "P-00"]
    p01_rows = [r for r in writer.rows if r.pump_id == "P-01"]
    assert p00_rows[0].psi is None
    assert p00_rows[1].psi is not None
    assert p01_rows[0].psi is None


def test_service_feature_history_size_tracks_per_pump():
    """The PSI feature-history deque grows with each handle call and
    is exposed via feature_history_size for the smoke test."""
    cfg = _make_config()
    writer = FakeWriter()
    svc = ScorerService(cfg, writer, psi_period_ticks=10)

    async def _go():
        for _ in range(5):
            await svc.handle("P-00", _telemetry())

    _run(_go())
    assert svc.feature_history_size("P-00") == 5
    assert svc.feature_history_size("P-99") == 0  # never seen


def test_service_psi_period_ticks_default_from_config():
    """When psi_period_ticks is not explicitly passed, it derives from
    ``config.psi_period_ticks`` = ceil(60s / tick_seconds)."""
    cfg = _make_config(tick_seconds=2.0)  # -> 30 ticks default
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)
    assert svc.psi_period_ticks == 30

    cfg2 = _make_config(tick_seconds=4.0)  # -> 15 ticks default
    svc2 = ScorerService(cfg2, writer)
    assert svc2.psi_period_ticks == 15


def test_service_window_size_derived_from_config(_=None):
    """ScorerService picks up window_samples from config."""
    cfg = _make_config(tick_seconds=10.0)  # 30-sample window
    writer = FakeWriter()
    svc = ScorerService(cfg, writer)
    assert svc.window.window_samples == 30


def test_mode_parity_uses_shared_features_module():
    """The mode-parity invariant: service imports extract_features from
    shared.features, not from a local fork. Drift here would be a bug.
    """
    service = importlib.import_module("local_runtime.service")
    shared_features = importlib.import_module("shared.features")
    assert service.extract_features is shared_features.extract_features


def test_mode_parity_uses_shared_score_and_drift():
    """Score and PSI must come from shared/ -- Lambda will import the same."""
    service = importlib.import_module("local_runtime.service")
    shared_score = importlib.import_module("shared.score")
    shared_drift = importlib.import_module("shared.drift")
    assert service.score_fn is shared_score.score
    assert service.compute_psi is shared_drift.compute_psi


def test_service_accepts_externally_supplied_window():
    """Dependency-injectable window -- needed for tests and for a future
    'replay from snapshot' tool."""
    cfg = _make_config()
    writer = FakeWriter()
    custom = FeatureWindow(window_samples=5)
    svc = ScorerService(cfg, writer, window=custom)
    assert svc.window is custom


# -- Stronger structural parity check (per Gemini Q6 of 2026-05-29 review) --


def test_structural_parity_no_vendoring():
    """Verify the actual file path that extract_features executes from
    is inside /shared/, not a vendored copy under /local_runtime/ or
    /lambda_scorer/.

    The earlier `is` check (test_mode_parity_uses_shared_features_module)
    catches a missed rename, but if someone copies features.py into
    local_runtime/ AND updates the import to point at the local copy,
    `is` would still pass (both sides would refer to the local copy).
    This test makes the boundary physically enforceable: the
    extract_features being called from local_runtime.service must
    physically live in the repo's shared/ directory.

    Per Gemini Q6 (2026-05-29 review) -- uses `inspect.getfile` rather
    than relying on `sys.modules` identity.
    """
    import inspect
    from pathlib import Path

    import local_runtime.service as service_mod

    func_file = Path(inspect.getfile(service_mod.extract_features)).resolve()
    # Walk up from this test file to the repo root:
    # tests/ -> local_runtime/ -> repo_root/
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"extract_features is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


def test_structural_parity_score_loads_from_shared():
    """Same check for shared.score.score -- a vendored fork in
    local_runtime/ would defeat mode parity even if the import is_
    check passes (because both sides could agree on the wrong copy)."""
    import inspect
    from pathlib import Path

    import local_runtime.service as service_mod

    func_file = Path(inspect.getfile(service_mod.score_fn)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"score is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


def test_structural_parity_compute_psi_loads_from_shared():
    """Same check for shared.drift.compute_psi."""
    import inspect
    from pathlib import Path

    import local_runtime.service as service_mod

    func_file = Path(inspect.getfile(service_mod.compute_psi)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"compute_psi is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )
