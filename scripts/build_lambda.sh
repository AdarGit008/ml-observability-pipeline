#!/usr/bin/env bash
# Build the scorer Lambda deploy tree (.build/lambda_dist/).
#
# ADR 0005 Addendum Q1: Terraform's archive_file can't combine
# multiple source roots, so this script stages everything first;
# infra/modules/lambda_scorer only zips the result. Run before
# `terraform plan`.
#
# Layout produced (zip root == /var/task in Lambda):
#   shared/                  <- parity package (COPIED, not imported;
#                               infra is not in the parity set)
#   lambda_scorer/           <- handler (tests stripped)
#   model/artifacts/         <- model.pkl + reference JSON, exactly
#                               where shared/drift.py resolves them:
#                               Path(shared/__file__).parent.parent
#                               / "model" / "artifacts"
#   numpy/ sklearn/ scipy/...<- manylinux2014_x86_64 wheels (Lambda
#                               runs x86_64 Linux; host wheels won't)
#
# boto3 NOT bundled — Lambda runtime provides it; botocore would
# blow the 50 MB zipped direct-upload limit.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$REPO_ROOT/.build/lambda_dist"
PYTHON="${PYTHON:-python3}"

echo "==> Staging into $DIST"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> Installing deps (manylinux2014_x86_64, cp312, binary-only)"
"$PYTHON" -m pip install --quiet --upgrade \
  --target "$DIST" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r "$REPO_ROOT/scripts/lambda_requirements.txt"

echo "==> Copying first-party code + artifacts"
cp -r "$REPO_ROOT/shared" "$DIST/shared"
cp -r "$REPO_ROOT/lambda_scorer" "$DIST/lambda_scorer"
rm -rf "$DIST/lambda_scorer/tests"
mkdir -p "$DIST/model/artifacts"
cp "$REPO_ROOT/model/artifacts/model.pkl" \
   "$REPO_ROOT/model/artifacts/operational_reference_distribution.json" \
   "$DIST/model/artifacts/"

echo "==> Stripping __pycache__ + vendored tests (ADR 0006 §Q4 convention)"
find "$DIST" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$DIST" -type d -name "tests" -prune -exec rm -rf {} +

SIZE_MB=$(du -sm "$DIST" | cut -f1)
echo "==> Unzipped footprint: ${SIZE_MB} MB (ADR 0006 §Q4 baseline ~124 MB; Lambda ceiling 250 MB)"
if [ "$SIZE_MB" -ge 250 ]; then
  echo "FAIL: exceeds Lambda's 250 MB unzipped ceiling" >&2; exit 1
elif [ "$SIZE_MB" -ge 200 ]; then
  echo "WARN: within 20% of the 250 MB ceiling — re-check ADR 0006 §Q4 fallbacks" >&2
fi

echo "==> Smoke-check: handler imports shared.* from the staged tree"
cat > "$REPO_ROOT/.build/smoke_check.py" <<'PYEOF'
"""Deploy-tree smoke check: cold-start import from the staged dist.

Verifies (1) lambda_scorer.handler imports, which exercises the
eager cold-start path — load_reference() reads model/artifacts/ from
the staged tree and version-matches model.pkl (ADR 0007); (2) every
shared.* module physically loads from inside the dist, mirroring the
structural-parity posture (the zip must be self-contained).
"""
import inspect, os, pathlib, sys

dist = pathlib.Path("/work/lambda_dist")
sys.path.insert(0, str(dist))
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:eu-central-1:000000000000:smoke-check")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")

import shared.drift, shared.features, shared.score          # noqa: E402
import lambda_scorer.handler as h                           # noqa: E402

for mod in (shared.drift, shared.features, shared.score, h):
    src = pathlib.Path(inspect.getfile(mod)).resolve()
    assert dist in src.parents, f"{mod.__name__} loaded from OUTSIDE dist: {src}"
assert h.REFERENCE, "REFERENCE empty after cold-start load"
print(f"smoke OK: handler cold-start clean, reference keys={sorted(h.REFERENCE)[:3]}...")
PYEOF

if command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$REPO_ROOT/.build:/work" -w /work python:3.12-slim \
    sh -c "pip install -q boto3 && python /work/smoke_check.py"
else
  echo "WARN: docker not found — smoke-check SKIPPED. The import test needs" >&2
  echo "      linux x86_64 (manylinux wheels won't import on the host OS)." >&2
fi

echo "==> Done. terraform plan will zip $DIST via archive_file."
