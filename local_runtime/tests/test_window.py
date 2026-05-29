"""Tests for local_runtime.window.FeatureWindow.

Pins:
- Sliding semantics (deque maxlen evicts oldest).
- Per-pump isolation (P-00's window doesn't bleed into P-01's).
- snapshot() returns a defensive copy.
- size() and pumps() behave for the empty / partially-filled cases.
"""

from __future__ import annotations

import pytest

from local_runtime.window import FeatureWindow


def _reading(value: float) -> dict[str, float]:
    return {
        "vibration_amp": value,
        "bearing_temp": 60.0,
        "motor_current": 4.0,
        "rpm": 1800.0,
    }


def test_window_requires_positive_size():
    with pytest.raises(ValueError, match="window_samples"):
        FeatureWindow(window_samples=0)


def test_append_creates_pump_lazily():
    w = FeatureWindow(window_samples=10)
    assert w.size("P-00") == 0
    w.append("P-00", _reading(0.3))
    assert w.size("P-00") == 1


def test_snapshot_returns_empty_list_for_unknown_pump():
    """An unseen pump returns [], not KeyError — caller treats as 'no data yet'."""
    w = FeatureWindow(window_samples=10)
    assert w.snapshot("P-99") == []


def test_window_evicts_oldest_at_capacity():
    """deque(maxlen=N) sliding semantics."""
    w = FeatureWindow(window_samples=3)
    for v in (0.1, 0.2, 0.3, 0.4):
        w.append("P-00", _reading(v))
    snapshot = w.snapshot("P-00")
    assert len(snapshot) == 3
    # 0.1 evicted, 0.4 newest
    assert [r["vibration_amp"] for r in snapshot] == [0.2, 0.3, 0.4]


def test_per_pump_isolation():
    """Appending to P-00 doesn't affect P-01."""
    w = FeatureWindow(window_samples=10)
    w.append("P-00", _reading(0.1))
    w.append("P-01", _reading(0.9))
    w.append("P-00", _reading(0.2))
    p00 = w.snapshot("P-00")
    p01 = w.snapshot("P-01")
    assert [r["vibration_amp"] for r in p00] == [0.1, 0.2]
    assert [r["vibration_amp"] for r in p01] == [0.9]


def test_snapshot_returns_defensive_copy():
    """Mutating the returned list/dict must not affect the underlying window."""
    w = FeatureWindow(window_samples=10)
    w.append("P-00", _reading(0.3))
    snap = w.snapshot("P-00")
    snap.append(_reading(99.9))  # mutate the returned list
    assert w.size("P-00") == 1  # underlying window unchanged


def test_pumps_iterates_seen_ids():
    w = FeatureWindow(window_samples=10)
    w.append("P-00", _reading(0.1))
    w.append("P-02", _reading(0.2))
    assert set(w.pumps()) == {"P-00", "P-02"}


def test_window_samples_property_reflects_constructor_arg():
    w = FeatureWindow(window_samples=150)
    assert w.window_samples == 150
