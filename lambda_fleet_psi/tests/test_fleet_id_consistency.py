"""Cross-component guard: the AWS fleet pump-id enumeration.

``FLEET_PUMP_IDS = tuple(f"P-{i:02d}" for i in range(FLEET_SIZE))`` is
duplicated across three independently-packaged Lambdas
(``dashboards_adapter``, ``lambda_s3_batcher``, ``lambda_fleet_psi``),
each building it from its own ``FLEET_SIZE`` env var.

History: the adapter was fixed to ``range(FLEET_SIZE)`` (P-00..P-14)
on 2026-06-07, but the batcher and fleet Lambda kept the 1-indexed
``range(1, FLEET_SIZE + 1)`` (P-01..P-15) until 2026-06-11 — dropping
P-00 and querying a phantom P-15 (batcher: silent S3 archival loss for
P-00; fleet: pooled-window count short by one, observed live as
``pumps_pooled: 14``). All three are now 0-indexed.

NOTE — what this guard does and does NOT do (DeepSeek review 2026-06-11
Q2). It is a SOURCE-level check, not an import: it needs none of the
handlers' cold-start env vars, runs in milliseconds, and pins the exact
expression that drifted. Limits: (a) it asserts the *canonical*
``range(FLEET_SIZE)`` form — an equivalent-but-different spelling like
``range(0, FLEET_SIZE)`` would (correctly) fail and force the canonical
form; (b) it scopes the literal checks to the ``FLEET_PUMP_IDS``
assignment block so a comment elsewhere mentioning the old expression
won't trip it; (c) the discovery assertion below fails if a NEW
``handler.py`` defines ``FLEET_PUMP_IDS`` without being added to
``_HANDLERS`` — so a fourth Lambda cannot silently escape the guard.
The real dedup (a shared, NON-parity fleet-id home — ``shared/`` is the
locked ADR 0005 parity boundary and the wrong place for an AWS-fleet
concept) is deferred as SSOT debt (2026-06-11 session log; _global.md
cross-component invariants).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_HANDLERS = (
    "dashboards_adapter/handler.py",
    "lambda_s3_batcher/handler.py",
    "lambda_fleet_psi/handler.py",
)


def _assignment_block(src: str) -> str:
    """Return the ``FLEET_PUMP_IDS = tuple(... )`` assignment text only,
    so comments elsewhere in the file can't influence the literal checks."""
    m = re.search(r"FLEET_PUMP_IDS\b.*?\)", src, re.DOTALL)
    assert m, "FLEET_PUMP_IDS assignment not found"
    return m.group(0)


def test_every_handler_defining_fleet_pump_ids_is_covered() -> None:
    """A new handler.py that defines FLEET_PUMP_IDS must be listed in
    _HANDLERS — closes the 'forgot to add it' gap (review Q2)."""
    found = {
        str(p.relative_to(_REPO)).replace("\\", "/")
        for p in _REPO.glob("*/handler.py")
        if "FLEET_PUMP_IDS" in p.read_text(encoding="utf-8")
    }
    assert found == set(_HANDLERS), (
        f"handlers defining FLEET_PUMP_IDS = {sorted(found)} but the guard "
        f"covers {sorted(_HANDLERS)}; add new handlers to _HANDLERS."
    )


@pytest.mark.parametrize("rel", _HANDLERS)
def test_fleet_pump_ids_are_zero_indexed(rel: str) -> None:
    block = _assignment_block((_REPO / rel).read_text(encoding="utf-8"))
    assert "for i in range(FLEET_SIZE)" in block, (
        f"{rel}: FLEET_PUMP_IDS must enumerate P-00..P-(FLEET_SIZE-1) "
        "via range(FLEET_SIZE)."
    )
    assert "range(1, FLEET_SIZE + 1)" not in block, (
        f"{rel}: the 1-indexed range(1, FLEET_SIZE + 1) is the off-by-one "
        "that drops P-00 and queries a phantom P-(FLEET_SIZE) "
        "(fixed 2026-06-11)."
    )
