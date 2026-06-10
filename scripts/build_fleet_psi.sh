#!/usr/bin/env bash
# Stage the fleet-PSI deploy tree (.build/fleet_psi_dist/).
#
# Sibling of build_lambda.sh / build_batcher.sh / build_adapter.sh.
# This is the DRIFT-ONLY zip (ADR 0018 §Decision 6): numpy + shared/ +
# the operational reference JSON, but NOT model.pkl and NOT sklearn.
# load_reference skips the model/reference version check when model.pkl
# is absent (ADR 0007 §4), so the fleet cold start needs only the
# reference — a much lighter zip than the scorer's sklearn stack. Run
# before `terraform plan` (infra/modules/fleet_psi zips the result).
#
# Layout produced (zip root == /var/task in Lambda):
#   shared/                  <- parity package (COPIED, not imported;
#                               infra is not in the parity set)
#   lambda_fleet_psi/        <- handler (tests stripped)
#   model/artifacts/         <- operational_reference_distribution.json
#                               ONLY (NO model.pkl), exactly where
#                               shared/drift.py resolves it:
#                               Path(shared/__file__).parent.parent
#                               / "model" / "artifacts"
#   numpy/ ...               <- manylinux x86_64 wheel (Lambda
#                               runs x86_64 Linux; host wheels won't)
#
# boto3 NOT bundled — Lambda runtime provides it (infra session #1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$REPO_ROOT/.build/fleet_psi_dist"
PYTHON="${PYTHON:-python3}"

echo "==> Staging into $DIST"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> Installing deps (manylinux_2_28/manylinux2014 x86_64, cp312, binary-only)"
# Same platform tags as build_lambda/build_batcher. Lambda python3.12 =
# AL2023 (glibc 2.34); numpy ships wheels under both caps — pip picks
# the best. Only numpy here (drift-only — see fleet_psi_requirements.txt).
"$PYTHON" -m pip install --quiet --upgrade \
  --target "$DIST" \
  --platform manylinux_2_28_x86_64 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r "$REPO_ROOT/scripts/fleet_psi_requirements.txt"

echo "==> Copying first-party code + reference artifact (drift-only: NO model.pkl)"
cp -r "$REPO_ROOT/shared" "$DIST/shared"
cp -r "$REPO_ROOT/lambda_fleet_psi" "$DIST/lambda_fleet_psi"
rm -rf "$DIST/lambda_fleet_psi/tests"
mkdir -p "$DIST/model/artifacts"
# ONLY the reference JSON. model.pkl is deliberately NOT copied — the
# drift-only invariant (ADR 0018 §6); the smoke-check asserts its absence.
cp "$REPO_ROOT/model/artifacts/operational_reference_distribution.json" \
   "$DIST/model/artifacts/"

echo "==> Stripping __pycache__ + vendored tests (ADR 0006 §Q4 convention)"
# numpy is EXEMPT from the tests strip (same lesson as build_lambda,
# 2026-06-04): numpy.testing imports numpy._core.tests._natype at module
# level (numpy 2.4.x), reachable through numpy's __getattr__ during the
# cold-start import chain. Stripping numpy's tests can break that import.
# Costs a few MB against the 250 MB ceiling.
find "$DIST" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$DIST" -type d -name "tests" -not -path "*/numpy/*" -prune -exec rm -rf {} +

SIZE_MB=$(du -sm "$DIST" | cut -f1)
echo "==> Unzipped footprint: ${SIZE_MB} MB (drift-only — numpy + shared/ + reference; Lambda ceiling 250 MB)"
if [ "$SIZE_MB" -ge 250 ]; then
  echo "FAIL: exceeds Lambda's 250 MB unzipped ceiling" >&2; exit 1
elif [ "$SIZE_MB" -ge 200 ]; then
  echo "WARN: within 20% of the 250 MB ceiling — unexpected for a drift-only zip; re-check fleet_psi_requirements.txt" >&2
fi

echo "==> Smoke-check: handler cold-start import + drift-only invariant"
cat > "$REPO_ROOT/.build/fleet_psi_smoke_check.py" <<'PYEOF'
"""Deploy-tree smoke check: cold-start import from the staged fleet dist.

Verifies (1) lambda_fleet_psi.handler imports, exercising the eager
cold-start path — load_reference() reads the reference JSON from the
staged tree (no model.pkl → the version check is skipped, ADR 0007 §4);
(2) every shared.* module physically loads from inside the dist (the zip
is self-contained — structural-parity posture); (3) the DRIFT-ONLY
invariant: model.pkl is NOT in the staged tree (ADR 0018 §6).
"""
import inspect, os, pathlib, sys

dist = pathlib.Path("/work/fleet_psi_dist")
sys.path.insert(0, str(dist))
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:eu-central-1:000000000000:smoke-check")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")

import shared.drift, shared.features                        # noqa: E402
import lambda_fleet_psi.handler as h                        # noqa: E402

for mod in (shared.drift, shared.features, h):
    src = pathlib.Path(inspect.getfile(mod)).resolve()
    assert dist in src.parents, f"{mod.__name__} loaded from OUTSIDE dist: {src}"
assert h.REFERENCE, "REFERENCE empty after cold-start load"
assert not (dist / "model" / "artifacts" / "model.pkl").exists(), \
    "model.pkl present — fleet-PSI is drift-only (ADR 0018 §6); the zip must not ship it"
assert not (dist / "sklearn").exists(), "sklearn present — fleet-PSI is drift-only (ADR 0018 §6)"
print(f"smoke OK: fleet handler cold-start clean, reference keys={sorted(h.REFERENCE)[:3]}..., no model.pkl/sklearn")
PYEOF

if command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$REPO_ROOT/.build:/work" -w /work python:3.12-slim \
    sh -c "pip install -q boto3 && python /work/fleet_psi_smoke_check.py"
else
  echo "WARN: docker not found — cold-start smoke-check SKIPPED. The import test needs" >&2
  echo "      linux x86_64 (manylinux wheels won't import on the host OS)." >&2
fi

echo "==> Done. terraform plan will zip $DIST via archive_file."
