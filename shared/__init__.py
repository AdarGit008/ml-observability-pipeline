"""Mode-parity shared logic.

This package contains the pure-Python (+ numpy) modules that both
``local_runtime`` and ``lambda_scorer`` import as peers. The
mode-parity invariant from ``context/_global.md`` north star #4 lives
here: anything in this package MUST produce identical results when
called from the local subscriber and from a Lambda handler over the
same input stream.

Dependency ceiling: standard library + ``numpy``. No I/O, no MQTT, no
DynamoDB, no InfluxDB. If a feature needs those, it belongs in
``local_runtime/`` (local-only) or ``lambda_scorer/`` (AWS-only), and
the pure-logic core gets extracted here.

See ADR 0005 for why ``shared/`` lives at the repo root rather than
under either of its consumers.
"""
