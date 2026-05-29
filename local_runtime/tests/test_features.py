"""Tests for shared.features.extract_features — the mode-parity boundary.

These tests pin:
- The function is PURE (no I/O, no MQTT, no InfluxDB).
- Output shape is exactly the 8 keys in FEATURE_NAMES.
- Rolling mean/std use population std (ddof=0).
- Empty window raises ValueError loudly.
- Missing required fields raise KeyError with the field name.

Mode-parity rationale: this function is what Lambda and local_runtime
both call. Anything that drifts here drifts the entire scoring path.
"""

from __future__ import annotations

import math

import pytest

from shared.features import FEATURE_NAMES, RAW_SIGNAL_FIELDS, extract_features


def _reading(
    vibration_amp: float = 0.3,
    bearing_temp: float = 60.0,
    motor_current: float = 4.0,
    rpm: float = 1800.0,
) -> dict[str, float]:
    return {
        "vibration_amp": vibration_amp,
        "bearing_temp": bearing_temp,
        "motor_current": motor_current,
        "rpm": rpm,
    }


def test_feature_names_pinned():
    """Order and identity of FEATURE_NAMES is part of the public contract."""
    assert FEATURE_NAMES == (
        "vibration_amp",
        "bearing_temp",
        "motor_current",
        "rpm",
        "vibration_amp_mean_5m",
        "vibration_amp_std_5m",
        "bearing_temp_mean_5m",
        "bearing_temp_std_5m",
    )


def test_raw_signal_fields_pinned():
    assert RAW_SIGNAL_FIELDS == (
        "vibration_amp",
        "bearing_temp",
        "motor_current",
        "rpm",
    )


def test_extract_features_returns_eight_keys():
    """Output shape is exactly the 8-element FEATURE_NAMES."""
    features = extract_features([_reading()])
    assert set(features.keys()) == set(FEATURE_NAMES)
    assert len(features) == 8


def test_extract_features_latest_raw_signals_use_last_reading():
    """The 4 raw features come from the last element, not the mean."""
    window = [
        _reading(vibration_amp=0.1, bearing_temp=50.0, motor_current=3.0, rpm=1700.0),
        _reading(vibration_amp=0.5, bearing_temp=70.0, motor_current=5.0, rpm=1900.0),
    ]
    features = extract_features(window)
    assert features["vibration_amp"] == 0.5
    assert features["bearing_temp"] == 70.0
    assert features["motor_current"] == 5.0
    assert features["rpm"] == 1900.0


def test_extract_features_rolling_mean_over_full_window():
    """vibration_amp_mean_5m is the arithmetic mean of vibration_amp in the window."""
    window = [
        _reading(vibration_amp=0.2),
        _reading(vibration_amp=0.4),
        _reading(vibration_amp=0.6),
    ]
    features = extract_features(window)
    assert features["vibration_amp_mean_5m"] == pytest.approx(0.4)


def test_extract_features_rolling_std_is_population_ddof0():
    """std uses ddof=0 (population). Documented in module docstring;
    pinned because the model session generates training data with the
    same function."""
    window = [_reading(vibration_amp=0.2), _reading(vibration_amp=0.6)]
    # Population std of [0.2, 0.6] = sqrt(0.04) = 0.2.
    # Sample std (ddof=1) would be 0.2828; we want population.
    features = extract_features(window)
    assert features["vibration_amp_std_5m"] == pytest.approx(0.2, rel=1e-9)


def test_extract_features_bearing_temp_rolling_stats_separate_from_vibration():
    """vibration and bearing_temp rolling stats are independent series."""
    window = [
        _reading(vibration_amp=0.1, bearing_temp=60.0),
        _reading(vibration_amp=0.2, bearing_temp=70.0),
        _reading(vibration_amp=0.3, bearing_temp=80.0),
    ]
    features = extract_features(window)
    assert features["vibration_amp_mean_5m"] == pytest.approx(0.2)
    assert features["bearing_temp_mean_5m"] == pytest.approx(70.0)


def test_extract_features_single_sample_window_has_zero_std():
    """A 1-element window has population std of 0.0 — warm-up case."""
    features = extract_features([_reading(vibration_amp=0.42, bearing_temp=68.3)])
    assert features["vibration_amp_std_5m"] == 0.0
    assert features["bearing_temp_std_5m"] == 0.0
    # mean of 1 element = that element
    assert features["vibration_amp_mean_5m"] == 0.42
    assert features["bearing_temp_mean_5m"] == 68.3


def test_extract_features_empty_window_raises():
    """Empty window is a programming error, not a NaN-producing soft case."""
    with pytest.raises(ValueError, match="empty window"):
        extract_features([])


def test_extract_features_missing_raw_field_raises_keyerror():
    """A reading missing one of the 4 raw fields surfaces as KeyError."""
    bad_window = [{"vibration_amp": 0.3, "bearing_temp": 60.0, "motor_current": 4.0}]
    # rpm missing
    with pytest.raises(KeyError, match="rpm"):
        extract_features(bad_window)


def test_extract_features_returns_python_floats_not_numpy_scalars():
    """Caller (InfluxDB writer) needs Python floats — numpy scalars in
    a JSON-encoded payload would break some downstream consumers."""
    features = extract_features([_reading()])
    for name in FEATURE_NAMES:
        assert type(features[name]) is float, (
            f"{name} is {type(features[name]).__name__}, not float"
        )


def test_extract_features_is_pure_no_mutation_of_input():
    """Input list and dicts must not be mutated by the call."""
    window = [_reading(vibration_amp=0.3, bearing_temp=60.0)]
    snapshot_before = (list(window), dict(window[0]))
    extract_features(window)
    assert list(window) == snapshot_before[0]
    assert dict(window[0]) == snapshot_before[1]


def test_extract_features_is_deterministic():
    """Same input → same output, no hidden state."""
    window = [
        _reading(vibration_amp=0.2, bearing_temp=55.0),
        _reading(vibration_amp=0.4, bearing_temp=65.0),
    ]
    first = extract_features(window)
    second = extract_features(window)
    assert first == second


def test_extract_features_handles_deque_input():
    """Accepts a deque (the local_runtime FeatureWindow uses one)."""
    from collections import deque

    window = deque([_reading(vibration_amp=0.3)], maxlen=10)
    features = extract_features(window)
    assert features["vibration_amp"] == 0.3
    assert math.isfinite(features["vibration_amp_mean_5m"])
