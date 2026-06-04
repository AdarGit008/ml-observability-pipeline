#!/usr/bin/env bash
# Stage the cold-path batcher deploy tree (.build/batcher_dist/).
#
# Sibling of build_lambda.sh / build_adapter.sh, kept separate on
# purpose: the batcher carries exactly ONE third-party dep (pyarrow,
# ADR 0015 §Decision 2) — heavier than the adapter's nothing, far
# lighter than the scorer's sklearn stack. Run before
# `terraform plan` (infra/modules/lambda_s3_batcher zips the result).
#
# Layout produced (zip root == /var/task in Lambda):
#   lambda_s3_batcher/       <- handler package (tests stripped)
#   pyarrow/ ...             <- manylinux x86_64 wheel (Lambda
#                               runs x86_64 Linux; host wheels won't)
#
# boto3 NOT bundled — Lambda runtime provides it (infra session #1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$REPO_ROOT/.build/batcher_dist"
PYTHON="${PYTHON:-python3}"

echo "==> Staging into $DIST"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> Installing deps (manylinux_2_28/manylinux2014 x86_64, cp312, binary-only)"
# manylinux_2_28 accepted (2026-06-04, same fix as build_lambda):
# pyarrow 21+ ships manylinux_2_28-only wheels; the ==24.0.0 pin (the
# version the moto suite ran against) doesn't exist under a
# manylinux2014-only cap. Lambda python3.12 = AL2023 (glibc 2.34).
"$PYTHON" -m pip install --quiet --upgrade \
  --target "$DIST" \
  --platform manylinux_2_28_x86_64 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r "$REPO_ROOT/scripts/batcher_requirements.txt"

echo "==> Copying first-party code"
cp -r "$REPO_ROOT/lambda_s3_batcher" "$DIST/lambda_s3_batcher"
rm -rf "$DIST/lambda_s3_batcher/tests"

echo "==> Stripping __pycache__ + vendored tests (ADR 0006 §Q4 convention)"
find "$DIST" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$DIST" -type d -name "tests" -prune -exec rm -rf {} +

SIZE_MB=$(du -sm "$DIST" | cut -f1)
echo "==> Unzipped footprint: ${SIZE_MB} MB (ADR 0015 estimate ~100 MB; Lambda ceiling 250 MB)"
if [ "$SIZE_MB" -ge 250 ]; then
  echo "FAIL: exceeds Lambda's 250 MB unzipped ceiling — ADR 0015 names CSV as the recorded fallback" >&2; exit 1
elif [ "$SIZE_MB" -ge 200 ]; then
  echo "WARN: within 20% of the 250 MB ceiling — re-check ADR 0015 §Decision 2" >&2
fi

echo "==> Smoke-check: staged tree shape + no shared/ import"
python3 - "$DIST" <<'PYEOF'
import pathlib, sys
dist = pathlib.Path(sys.argv[1])
files = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("lambda_s3_batcher/*.py"))
assert "lambda_s3_batcher/handler.py" in files, f"handler missing; staged: {files}"
assert (dist / "pyarrow").is_dir(), "pyarrow wheel missing from the staged tree"
src = (dist / "lambda_s3_batcher/handler.py").read_text(encoding="utf-8")
assert "import shared" not in src and "from shared" not in src, \
    "batcher imports shared/ — it must stay outside the parity set (ADR 0015 Principle)"
print(f"smoke OK: staged {files} + pyarrow")
PYEOF

echo "==> Done. terraform plan will zip $DIST via archive_file."
