<#
.SYNOPSIS
Stage the Grafana adapter deploy tree (.build/adapter_dist/). PO-side
counterpart of scripts/build_adapter.sh — same staging, same check.

Sibling of build_lambda.ps1, kept separate on purpose: the adapter has
ZERO third-party deps (boto3 is runtime-provided), so staging is a
copy + strip — no pip wheels, no Docker. Run before `terraform plan`.

Layout produced (zip root == /var/task in Lambda):
  dashboards_adapter/      handler package (tests stripped)
#>
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Dist     = Join-Path $RepoRoot ".build\adapter_dist"
$Python   = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> Staging into $Dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
Copy-Item -Recurse (Join-Path $RepoRoot "dashboards_adapter") (Join-Path $Dist "dashboards_adapter")
Remove-Item -Recurse -Force (Join-Path $Dist "dashboards_adapter\tests") -ErrorAction SilentlyContinue
Get-ChildItem -Path $Dist -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" } |
    Remove-Item -Recurse -Force

Write-Host "==> Smoke-check: staged tree shape + no shared/ import"
$SmokePy = @'
import pathlib, sys
dist = pathlib.Path(sys.argv[1])
files = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("*.py"))
assert "dashboards_adapter/handler.py" in files, f"handler missing; staged: {files}"
src = (dist / "dashboards_adapter/handler.py").read_text(encoding="utf-8")
assert "import shared" not in src and "from shared" not in src, \
    "adapter imports shared/ - it must stay outside the parity set (ADR 0014 Decision 5)"
print(f"smoke OK: staged {files}")
'@
$SmokePath = Join-Path $RepoRoot ".build\adapter_smoke_check.py"
[System.IO.File]::WriteAllText($SmokePath, $SmokePy)  # UTF-8 no BOM
& $Python $SmokePath $Dist
if ($LASTEXITCODE -ne 0) { throw "smoke-check FAILED" }

Write-Host "==> Done. terraform plan will zip $Dist via archive_file."
