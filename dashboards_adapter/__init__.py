"""Read-only Grafana fleet-snapshot adapter (ADR 0014).

Deliberately OUTSIDE the ADR 0005 parity set: this package never
imports ``shared/`` — it projects pre-computed STATE rows to JSON and
computes nothing. ``tests/test_adapter.py::test_adapter_does_not_import_shared``
pins that boundary.
"""
