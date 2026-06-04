<#
.SYNOPSIS
Build the scorer Lambda deploy tree (.build/lambda_dist/). PO-side
counterpart of scripts/build_lambda.sh — same staging, same checks.

ADR 0005 Addendum Q1: Terraform's archive_file can't combine multiple
source roots, so this script stages everything first;
infra/modules/lambda_scorer only zips the result. Run before
`terraform plan`.

Layout produced (zip root == /var/task in Lambda):
  shared/                   parity package (COPIED, not imported)
  lambda_scorer/            handler (tests stripped)
  model/artifacts/          model.pkl + reference JSON, exactly where
                            shared/drift.py resolves them
  numpy/ sklearn/ scipy/... manylinux wheels (Lambda runs
                            x86_64 Linux — Windows wheels won't load)

boto3 NOT bundled: the Lambda runtime provides it; botocore would
blow the 50 MB zipped direct-upload limit.

The smoke-check runs in Docker (python:3.12-slim) because the
manylinux wheels can't import on Windows. Skipped with a warning if
Docker isn't running.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Dist     = Join-Path $RepoRoot ".build\lambda_dist"
$Python   = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> Staging into $Dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Write-Host "==> Installing deps (manylinux_2_28/manylinux2014 x86_64, cp312, binary-only)"
# manylinux_2_28 added 2026-06-04: sklearn 1.9.0 (the model.pkl
# training version - see lambda_requirements.txt pins) ships no
# manylinux2014 wheel. Lambda python3.12 = AL2023 (glibc 2.34), so
# manylinux_2_28 wheels run fine. Both tags accepted; pip picks the
# best compatible wheel per package.
& $Python -m pip install --quiet --upgrade `
    --target $Dist `
    --platform manylinux_2_28_x86_64 `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all: `
    -r (Join-Path $RepoRoot "scripts\lambda_requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "==> Copying first-party code + artifacts"
Copy-Item -Recurse (Join-Path $RepoRoot "shared")        (Join-Path $Dist "shared")
Copy-Item -Recurse (Join-Path $RepoRoot "lambda_scorer") (Join-Path $Dist "lambda_scorer")
Remove-Item -Recurse -Force (Join-Path $Dist "lambda_scorer\tests") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "model\artifacts") | Out-Null
Copy-Item (Join-Path $RepoRoot "model\artifacts\model.pkl") `
          (Join-Path $Dist "model\artifacts\")
Copy-Item (Join-Path $RepoRoot "model\artifacts\operational_reference_distribution.json") `
          (Join-Path $Dist "model\artifacts\")

Write-Host "==> Stripping __pycache__ + vendored tests (ADR 0006 §Q4 convention)"
# numpy is EXEMPT from the tests strip: numpy.testing imports
# numpy._core.tests._natype at module level (numpy 2.4.x), and scipy's
# array_api_compat does `from numpy import *`, which triggers
# numpy.testing during the handler's cold-start import chain.
# Stripping numpy's tests breaks that import - caught by the Docker
# smoke-check on its first real run (2026-06-04 cold-path session).
Get-ChildItem -Path $Dist -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" -or ($_.Name -eq "tests" -and $_.FullName -notmatch "\\numpy\\") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$SizeMB = [math]::Round((Get-ChildItem -Path $Dist -Recurse -File |
    Measure-Object -Sum Length).Sum / 1MB)
Write-Host "==> Unzipped footprint: $SizeMB MB (ADR 0006 §Q4 baseline ~124 MB; Lambda ceiling 250 MB)"
if ($SizeMB -ge 250) {
    throw "FAIL: exceeds Lambda's 250 MB unzipped ceiling"
} elseif ($SizeMB -ge 200) {
    Write-Warning "within 20% of the 250 MB ceiling - re-check ADR 0006 Q4 fallbacks"
}

Write-Host "==> Smoke-check: handler imports shared.* from the staged tree"
$SmokePy = @'
"""Deploy-tree smoke check: cold-start import from the staged dist.

Verifies (1) lambda_scorer.handler imports, which exercises the
eager cold-start path - load_reference() reads model/artifacts/ from
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
'@
$SmokePath = Join-Path $RepoRoot ".build\smoke_check.py"
[System.IO.File]::WriteAllText($SmokePath, $SmokePy)  # UTF-8 no BOM, no Out-File encoding trap

$BuildDir = Join-Path $RepoRoot ".build"
$DockerOk = $false
try { docker info *> $null; $DockerOk = ($LASTEXITCODE -eq 0) } catch { }
if ($DockerOk) {
    docker run --rm -v "${BuildDir}:/work" -w /work python:3.12-slim `
        sh -c "pip install -q boto3 && python /work/smoke_check.py"
    if ($LASTEXITCODE -ne 0) { throw "smoke-check FAILED" }
} else {
    Write-Warning "Docker not available - smoke-check SKIPPED. The import test needs"
    Write-Warning "linux x86_64 (manylinux wheels won't import on Windows)."
}

Write-Host "==> Done. terraform plan will zip $Dist via archive_file."
