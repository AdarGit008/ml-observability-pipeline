"""Pure 8-feature extractor — the mode-parity boundary.

Called identically from ``local_runtime`` (with a window built from an
in-memory deque) and from ``lambda_scorer`` (with a window read from
DynamoDB on each invocation). Same function, same output.

Feature set per PLAN.md §2.3 — 4 raw signals + rolling mean/std of
vibration_amp and bearing_temp over the 5-minute window = 8 features:

- ``vibration_amp``        — latest reading
- ``bearing_temp``         — latest reading
- ``motor_current``        — latest reading
- ``rpm``                  — latest reading
- ``vibration_amp_mean_5m``
- ``vibration_amp_std_5m``
- ``bearing_temp_mean_5m``
- ``bearing_temp_std_5m``

Why "latest" for the raw signals and "5m rolling" for the two derived
ones: the rolling stats catch process drift (the slow rise the model
needs to learn from), while the raw values let the scorer see the
current operating point. PLAN.md §2.3 specifies this split explicitly.

Window semantics: the caller is responsible for handing in a list of
telemetry dicts ordered oldest-to-newest. ``extract_features`` does no
filtering, no time-windowing, no deduping — the rolling-window logic
lives in the caller (``local_runtime.window`` for local; the DynamoDB
read+append for Lambda). This keeps the function pure and trivially
unit-testable.

Edge case: an empty window is a programming error (the caller should
never invoke us before the first reading lands). Raises ``ValueError``
loudly so the bug surfaces immediately.

Std deviation: uses population std (ddof=0). With a 150-sample window
at 2-second ticks, the sample-vs-population difference is <1% and
ddof=0 keeps the function importable without scipy. This matches what
``numpy.std`` returns by default. Documented so the model session
doesn't get bitten when training data is generated with the same
function.
"""

from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np


# Feature names in stable order. Tests pin this — both as a guard against
# accidental renames and because the InfluxDB writer iterates over the
# dict and we want predictable column order in the TSDB.
#
# This is the SCORER input contract: ``shared.score.score`` consumes a
# dict keyed by every name here. See ``PSI_FEATURE_NAMES`` below for the
# drift surface contract (a strict subset of this tuple, per ADR 0009).
FEATURE_NAMES: tuple[str, ...] = (
    "vibration_amp",
    "bearing_temp",
    "motor_current",
    "rpm",
    "vibration_amp_mean_5m",
    "vibration_amp_std_5m",
    "bearing_temp_mean_5m",
    "bearing_temp_std_5m",
)

# Feature names the PSI drift surface iterates — a STRICT SUBSET of
# ``FEATURE_NAMES`` (the scorer input set). Per ADR 0009: the four
# rolling features are scorer inputs only, never PSI surface members,
# because their 149/150-overlap windows violate PSI's IID assumption
# and produce 0.10–0.40 autocorrelation noise on healthy fleets.
#
# Two related but distinct contracts:
# - ``FEATURE_NAMES`` (8) — scorer input. ``score(features)`` consumes
#   all eight; rolling features add temporal smoothing for prediction.
# - ``PSI_FEATURE_NAMES`` (4) — drift surface. ``compute_psi`` iterates
#   only the per-tick IID raw signals. The on-disk reference
#   distribution's ``features`` map covers exactly this tuple.
#
# Pinned by ``test_psi_feature_names_is_subset_of_feature_names`` so a
# "let me add a feature to PSI" regression has to update ADR 0009.
PSI_FEATURE_NAMES: tuple[str, ...] = (
    "vibration_amp",
    "bearing_temp",
    "motor_current",
    "rpm",
)

# Raw signal fields the simulator publishes — see context/_interfaces.md
# (Telemetry payload). Order doesn't matter functionally but is pinned
# for predictability. Today this happens to equal ``PSI_FEATURE_NAMES``
# (both are "the four raw sensors"), but they're conceptually distinct:
# ``RAW_SIGNAL_FIELDS`` describes the simulator's wire format;
# ``PSI_FEATURE_NAMES`` describes the drift surface. Keeping them as
# separate constants lets one evolve without dragging the other along.
RAW_SIGNAL_FIELDS: tuple[str, ...] = (
    "vibration_amp",
    "bearing_temp",
    "motor_current",
    "rpm",
)


def extract_features(window: Iterable[Mapping[str, float]]) -> dict[str, float]:
    """Project a window of telemetry readings to the 8-feature vector.

    Args:
        window: ordered oldest-to-newest list of telemetry dicts. Each
            dict must include the four raw signal fields. The last
            element supplies the "latest" raw values; the full window
            feeds the rolling mean/std.

    Returns:
        Dict keyed by ``FEATURE_NAMES`` (8 floats). Order is stable.

    Raises:
        ValueError: window is empty. The caller is responsible for
            never invoking this before the first reading.
        KeyError: a window entry is missing one of the raw signal
            fields. Surfaces as a hard error rather than silently
            producing NaN — a missing field means the upstream
            telemetry parser is broken, and we want that loud.
    """
    # Materialize once so we can both index into the last element and
    # iterate for the np.array build. ``window`` may be a deque (local
    # mode) or a list pulled from DynamoDB (AWS mode) — both are fine
    # iterables but neither supports negative indexing reliably (deque
    # does, but we don't want to depend on that across the parity
    # boundary).
    readings = list(window)
    if not readings:
        raise ValueError(
            "extract_features called on empty window — caller must hold "
            "at least one reading before invoking the feature extractor"
        )

    latest = readings[-1]

    # Build a single numpy column per windowed signal in one pass. Using
    # np.fromiter here would be marginally faster but the explicit loop
    # gives a clean KeyError site with the missing field name.
    vibration_window = np.array(
        [_required(r, "vibration_amp") for r in readings], dtype=np.float64
    )
    bearing_window = np.array(
        [_required(r, "bearing_temp") for r in readings], dtype=np.float64
    )

    return {
        "vibration_amp": float(_required(latest, "vibration_amp")),
        "bearing_temp": float(_required(latest, "bearing_temp")),
        "motor_current": float(_required(latest, "motor_current")),
        "rpm": float(_required(latest, "rpm")),
        # ddof=0 is numpy's default — population std, not sample. With
        # a 150-sample window the difference is <1% and avoids dragging
        # scipy into the dep tree. Documented in module docstring.
        "vibration_amp_mean_5m": float(vibration_window.mean()),
        "vibration_amp_std_5m": float(vibration_window.std()),
        "bearing_temp_mean_5m": float(bearing_window.mean()),
        "bearing_temp_std_5m": float(bearing_window.std()),
    }


def _required(reading: Mapping[str, float], field: str) -> float:
    """Lookup with a precise KeyError if the field is missing.

    ``Mapping.__getitem__`` raises ``KeyError(field)`` already; this
    wrapper is here so the model session can swap in a stricter check
    (e.g., NaN detection) without rewriting call sites.
    """
    return reading[field]
