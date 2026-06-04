#!/usr/bin/env bash
# Stage the Grafana adapter deploy tree (.build/adapter_dist/).
#
# Sibling of build_lambda.sh, kept separate on purpose: the adapter
# has ZERO third-party deps (boto3 is runtime-provided — same posture
# as the scorer), so its staging is a copy + strip, and bolting it
# onto the scorer's heavyweight build (pip wheels, Docker smoke-check)
# would couple a few-KB zip to a multi-minute build. Run before
# `terraform plan` (infra/modules/dashboards_adapter zips the result).
#
# Layout produced (zip root == /var/task in Lambda):
#   dashboards_adapter/      <- handler package (tests stripped)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$REPO_ROOT/.build/adapter_dist"

echo "==> Staging into $DIST"
rm -rf "$DIST"
mkdir -p "$DIST"
cp -r "$REPO_ROOT/dashboards_adapter" "$DIST/dashboards_adapter"
rm -rf "$DIST/dashboards_adapter/tests"
find "$DIST" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "==> Smoke-check: handler imports from the staged tree (pure stdlib + boto3 deferral)"
python3 - "$DIST" <<'PYEOF'
import pathlib, sys
dist = pathlib.Path(sys.argv[1])
files = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("*.py"))
assert "dashboards_adapter/handler.py" in files, f"handler missing; staged: {files}"
src = (dist / "dashboards_adapter/handler.py").read_text(encoding="utf-8")
assert "import shared" not in src and "from shared" not in src, \
    "adapter imports shared/ — it must stay outside the parity set (ADR 0014 §Decision 5)"
print(f"smoke OK: staged {files}")
PYEOF

echo "==> Done. terraform plan will zip $DIST via archive_file."
