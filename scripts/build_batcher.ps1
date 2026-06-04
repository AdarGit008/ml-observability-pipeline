<#
.SYNOPSIS
Stage the cold-path batcher deploy tree (.build/batcher_dist/).
PO-side counterpart of scripts/build_batcher.sh — same staging, same
checks.

Sibling of build_lambda.ps1 / build_adapter.ps1: one third-party dep
(pyarrow, manylinux x86_64 — Lambda runs x86_64 Linux, so the
wheel can't import on Windows; the smoke-check is static, like the
adapter's, not a Docker import like the scorer's). Run before
`terraform plan`.

Layout produced (zip root == /var/task in Lambda):
  lambda_s3_batcher/       handler package (tests stripped)
  pyarrow/ ...             manylinux wheel
#>
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Dist     = Join-Path $RepoRoot ".build\batcher_dist"
$Python   = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> Staging into $Dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Write-Host "==> Installing deps (manylinux_2_28/manylinux2014 x86_64, cp312, binary-only)"
# manylinux_2_28 accepted (2026-06-04, same fix as build_lambda):
# pyarrow 21+ ships manylinux_2_28-only wheels; the ==24.0.0 pin (the
# version the moto suite ran against) doesn't exist under a
# manylinux2014-only cap. Lambda python3.12 = AL2023 (glibc 2.34).
& $Python -m pip install --quiet --upgrade `
  --target $Dist `
  --platform manylinux_2_28_x86_64 `
  --platform manylinux2014_x86_64 `
  --implementation cp `
  --python-version 3.12 `
  --only-binary=:all: `
  -r (Join-Path $RepoRoot "scripts\batcher_requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install FAILED" }

Write-Host "==> Copying first-party code"
Copy-Item -Recurse (Join-Path $RepoRoot "lambda_s3_batcher") (Join-Path $Dist "lambda_s3_batcher")
Remove-Item -Recurse -Force (Join-Path $Dist "lambda_s3_batcher\tests") -ErrorAction SilentlyContinue
Get-ChildItem -Path $Dist -Recurse -Directory -Force |
    Where-Object { $_.Name -in @("__pycache__", "tests") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$SizeMB = [math]::Round((Get-ChildItem -Recurse $Dist | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "==> Unzipped footprint: $SizeMB MB (ADR 0015 estimate ~100 MB; Lambda ceiling 250 MB)"
if ($SizeMB -ge 250) {
    throw "FAIL: exceeds Lambda's 250 MB unzipped ceiling - ADR 0015 names CSV as the recorded fallback"
} elseif ($SizeMB -ge 200) {
    Write-Warning "within 20% of the 250 MB ceiling - re-check ADR 0015 Decision 2"
}

Write-Host "==> Smoke-check: staged tree shape + no shared/ import"
$SmokePy = @'
import pathlib, sys
dist = pathlib.Path(sys.argv[1])
files = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("lambda_s3_batcher/*.py"))
assert "lambda_s3_batcher/handler.py" in files, f"handler missing; staged: {files}"
assert (dist / "pyarrow").is_dir(), "pyarrow wheel missing from the staged tree"
src = (dist / "lambda_s3_batcher/handler.py").read_text(encoding="utf-8")
assert "import shared" not in src and "from shared" not in src, \
    "batcher imports shared/ - it must stay outside the parity set (ADR 0015 Principle)"
print(f"smoke OK: staged {files} + pyarrow")
'@
$SmokePath = Join-Path $RepoRoot ".build\batcher_smoke_check.py"
[System.IO.File]::WriteAllText($SmokePath, $SmokePy)  # UTF-8 no BOM
& $Python $SmokePath $Dist
if ($LASTEXITCODE -ne 0) { throw "smoke-check FAILED" }

Write-Host "==> Done. terraform plan will zip $Dist via archive_file."
