"""Local-mode equivalent of ``lambda_scorer``.

Subscribes to local Mosquitto on the wildcard topic
``factory/pumps/+/telemetry``, maintains a per-pump 5-minute rolling
feature window, scores and computes PSI via the shared mode-parity
modules (``shared.features``, ``shared.score``, ``shared.drift``),
and writes each row to local InfluxDB.

This is the "zero-cost continuous development" path from PLAN.md §2.1:
the same scoring + drift logic that runs in Lambda also runs here, so
work on the model and drift detector can iterate locally without
spending any AWS credits.

Entry point: ``python -m local_runtime [--config local_runtime/config.yaml]``.
"""
