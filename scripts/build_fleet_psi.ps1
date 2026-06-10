<#
.SYNOPSIS
Stage the fleet-PSI deploy tree (.build/fleet_psi_dist/). PO-side
counterpart of scripts/build_fleet_psi.sh — same staging, same checks.

Sibling of build_lambda.ps1 / build_batcher.ps1 / build_adapter.ps1.
This is the DRIFT-ONLY zip (ADR 0018 §Decision 6): numpy + shared/ +
the operational reference JSON, but NOT model.pkl and NOT sklearn.
load_reference skips the model/reference version check when model.pkl
is absent (ADR 0007 §4), so the fleet cold start needs only the
reference — far lighter than the scorer's sklearn stack. Run before
`terraform plan`.

Layout produced (zip root == /var/task in Lambda):
  shared/                  parity package (COPIED, not imported)
  lambda_fleet_psi/        handler (tests stripped)
  model/artifacts/         operational_reference_distribution.json ONLY
                           (NO model.pkl), where shared/drift.py resolves it
  numpy/ ...               manylinux wheel (Lambda runs x86_64 Linux —
                           Windows wheels won't load)

boto3 NOT bundled: the Lambda runtime provides it.

The smoke-check runs in Docker (python:3.12-slim) because the manylinux
numpy wheel can't import on Windows. Skipped with a warning if Docker
isn't running.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Dist     = Join-Path $RepoRoot ".build\fleet_psi_dist"
$Python   = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> Staging into $Dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Write-Host "==> Installing deps (manylinux_2_28/manylinux2014 x86_64, cp312, binary-only)"
# Same platform tags as build_lambda/build_batcher. Lambda python3.12 =
# AL2023 (glibc 2.34); numpy ships wheels under both caps. Only numpy
# here (drift-only - see fleet_psi_requirements.txt).
& $Python -m pip install --quiet --upgrade `
    --target $Dist `
    --platform manylinux_2_28_x86_64 `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all: `
    -r (Join-Path $RepoRoot "scripts\fleet_psi_requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install FAILED" }

Write-Host "==> Copying first-party code + reference artifact (drift-only: NO model.pkl)"
Copy-Item -Recurse (Join-Path $RepoRoot "shared")          (Join-Path $Dist "shared")
Copy-Item -Recurse (Join-Path $RepoRoot "lambda_fleet_psi") (Join-Path $Dist "lambda_fleet_psi")
Remove-Item -Recurse -Force (Join-Path $Dist "lambda_fleet_psi\tests") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "model\artifacts") | Out-Null
# ONLY the reference JSON. model.pkl is deliberately NOT copied - the
# drift-only invariant (ADR 0018 §6); the smoke-check asserts its absence.
Copy-Item (Join-Path $RepoRoot "model\artifacts\operational_reference_distribution.json") `
          (Join-Path $Dist "model\artifacts\")

Write-Host "==> Stripping __pycache__ + vendored tests (ADR 0006 §Q4 convention)"
# numpy is EXEMPT from the tests strip (same lesson as build_lambda,
# 2026-06-04): numpy.testing imports numpy._core.tests._natype at module
# level (numpy 2.4.x), reachable during the cold-start import chain.
# Stripping numpy's tests can break that import.
Get-ChildItem -Path $Dist -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" -or ($_.Name -eq "tests" -and $_.FullName -notmatch "\\numpy\\") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$SizeMB = [math]::Round((Get-ChildItem -Path $Dist -Recurse -File |
    Measure-Object -Sum Length).Sum / 1MB)
Write-Host "==> Unzipped footprint: $SizeMB MB (drift-only - numpy + shared/ + reference; Lambda ceiling 250 MB)"
if ($SizeMB -ge 250) {
    throw "FAIL: exceeds Lambda's 250 MB unzipped ceiling"
} elseif ($SizeMB -ge 200) {
    Write-Warning "within 20% of the 250 MB ceiling - unexpected for a drift-only zip; re-check fleet_psi_requirements.txt"
}

Write-Host "==> Smoke-check: handler cold-start import + drift-only invariant"
$SmokePy = @'
"""Deploy-tree smoke check: cold-start import from the staged fleet dist.

Verifies (1) lambda_fleet_psi.handler imports, exercising the eager
cold-start path - load_reference() reads the reference JSON from the
staged tree (no model.pkl -> the version check is skipped, ADR 0007 §4);
(2) every shared.* module physically loads from inside the dist (the zip
is self-contained); (3) the DRIFT-ONLY invariant: model.pkl is NOT in
the staged tree (ADR 0018 §6).
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
    "model.pkl present - fleet-PSI is drift-only (ADR 0018 §6); the zip must not ship it"
assert not (dist / "sklearn").exists(), "sklearn present - fleet-PSI is drift-only (ADR 0018 §6)"
print(f"smoke OK: fleet handler cold-start clean, reference keys={sorted(h.REFERENCE)[:3]}..., no model.pkl/sklearn")
'@
$SmokePath = Join-Path $RepoRoot ".build\fleet_psi_smoke_check.py"
[System.IO.File]::WriteAllText($SmokePath, $SmokePy)  # UTF-8 no BOM

$BuildDir = Join-Path $RepoRoot ".build"
$DockerOk = $false
try { docker info *> $null; $DockerOk = ($LASTEXITCODE -eq 0) } catch { }
if ($DockerOk) {
    docker run --rm -v "${BuildDir}:/work" -w /work python:3.12-slim `
        sh -c "pip install -q boto3 && python /work/fleet_psi_smoke_check.py"
    if ($LASTEXITCODE -ne 0) { throw "smoke-check FAILED" }
} else {
    Write-Warning "Docker not available - smoke-check SKIPPED. The import test needs"
    Write-Warning "linux x86_64 (manylinux wheels won't import on Windows)."
}

Write-Host "==> Done. terraform plan will zip $Dist via archive_file."
