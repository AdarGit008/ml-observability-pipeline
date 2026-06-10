<#
.SYNOPSIS
  Run a review packet through the DeepSeek API and write the response.

.DESCRIPTION
  Reads review_packets/<date>-<slug>.md, POSTs the contents to DeepSeek's
  OpenAI-compatible chat-completions endpoint, and writes the response to
  review_responses/<date>-<slug>.md as UTF-8.

  Single provider: DeepSeek (api.deepseek.com), reading the key from
  DEEPSEEK_API_KEY. Trimmed to DeepSeek-only 2026-06-10 (PO call) after
  the gemini/openrouter/groq/cerebras keys were rotated; the prior
  multi-provider cascade (ADR 0011) is recoverable from git history.
  -Model overrides the default model.

  Bypasses the Gemini CLI deliberately. See docs/adr/0001-direct-gemini-api-for-reviews.md.

.PARAMETER Slug
  Packet slug -- the part after the date. For
  review_packets/2026-05-24-simulator-pump.md, pass "simulator-pump".

.PARAMETER Model
  Model identifier. Defaults to deepseek-reasoner (R1). Pass deepseek-chat
  for DeepSeek V3.

.PARAMETER Date
  Date prefix of the packet file. When omitted, the script globs
  review_packets/*-<slug>.md and picks the newest match.

.PARAMETER Provider
  auto (default)   : DeepSeek (the only configured provider)
  deepseek         : DeepSeek explicitly

.PARAMETER DumpBody
  Dump the outbound request body to
  review_responses/<date>-<slug>.<provider>.request.json for debugging.

.EXAMPLE
  .\scripts\run_review.ps1 -Slug simulator-pump
  # runs the DeepSeek reviewer (deepseek-reasoner)

.EXAMPLE
  .\scripts\run_review.ps1 -Slug simulator-pump -Model deepseek-chat
  # use DeepSeek V3 (chat) instead of the R1 reasoner default

.NOTES
  Required env var:
    DEEPSEEK_API_KEY    https://platform.deepseek.com/api_keys
  (Set locally via scripts/review_keys.local.ps1 -- gitignored.)
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Slug,
  [string]$Model,
  [string]$Date,
  [ValidateSet("auto", "deepseek")]
  [string]$Provider = "auto",
  [switch]$DumpBody
)

$ErrorActionPreference = "Stop"

# --- Local API key (gitignored; not committed) ---
# Auto-source the per-developer reviewer key if present so the provider
# below sees DEEPSEEK_API_KEY without touching the shell profile.
$localKeys = Join-Path $PSScriptRoot "review_keys.local.ps1"
if (Test-Path $localKeys) { . $localKeys }

# --- Provider config map ---
#
# Single provider: DeepSeek (OpenAI-compatible chat completions). Carries
# the default model and the env var the script reads the API key from.
# Served by the Invoke-OpenAICompat helper.

$providers = [ordered]@{
  deepseek = @{ DefaultModel = "deepseek-reasoner"; EnvVar = "DEEPSEEK_API_KEY"; Endpoint = "https://api.deepseek.com/chat/completions" }
}

# --- Packet auto-pick ---

if (-not $PSBoundParameters.ContainsKey("Date")) {
  $candidates = Get-ChildItem -Path "review_packets" -Filter "*-$Slug.md" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending
  if (-not $candidates) {
    Write-Error "No packet found for slug '$Slug' under review_packets/. Tried glob '*-$Slug.md'. Pass -Date YYYY-MM-DD if the slug differs."
    exit 1
  }
  $packet = $candidates[0].FullName
  $Date = $candidates[0].BaseName -replace "-$Slug$", ""
  Write-Host "Auto-selected packet: $($candidates[0].Name)" -ForegroundColor Cyan
} else {
  $packet = Join-Path "review_packets" "$Date-$Slug.md"
}
$response = Join-Path "review_responses" "$Date-$Slug.md"

if (-not (Test-Path $packet)) { Write-Error "Packet not found: $packet"; exit 1 }

# Read prompt with explicit UTF-8 (Windows PowerShell 5.1 defaults to ANSI for BOM-less files).
$prompt = [System.IO.File]::ReadAllText((Resolve-Path $packet).Path, [System.Text.Encoding]::UTF8)

Add-Type -AssemblyName System.Web

# --- Provider invocation helper ---
#
# Throws on failure (after printing the API's error body to the console).
# Successful return: the response text as a [string].

function Invoke-OpenAICompat {
  param([string]$ProviderName, [string]$Endpoint, [string]$Model, [string]$Key, [string]$Prompt, [string]$DumpPath)

  $promptJson = [System.Web.HttpUtility]::JavaScriptStringEncode($Prompt, $true)
  $modelJson = [System.Web.HttpUtility]::JavaScriptStringEncode($Model, $true)
  # OpenAI-compatible chat completions shape. temperature defaults are
  # fine for review prompts; max_tokens unset = provider default.
  $body = '{"model":' + $modelJson + ',"messages":[{"role":"user","content":' + $promptJson + '}]}'

  if ($DumpPath) {
    [System.IO.File]::WriteAllText($DumpPath, $body, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[$ProviderName] request body dumped to $DumpPath ($($body.Length) chars)" -ForegroundColor DarkGray
  }

  $headers = @{
    "Authorization" = "Bearer $Key"
    "Content-Type"  = "application/json; charset=utf-8"
  }

  $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

  try {
    $r = Invoke-RestMethod -Method Post -Uri $Endpoint -Headers $headers -Body $bodyBytes
  } catch {
    Write-ApiError -ProviderName $ProviderName -Uri $Endpoint -Err $_
    throw
  }

  $text = $r.choices[0].message.content
  if (-not $text) { throw "[$ProviderName] empty response. Raw: $($r | ConvertTo-Json -Depth 10)" }
  return $text
}

function Write-ApiError {
  param([string]$ProviderName, [string]$Uri, $Err)

  $apiError = $null
  if ($Err.ErrorDetails -and $Err.ErrorDetails.Message) {
    $apiError = $Err.ErrorDetails.Message
  } elseif ($Err.Exception.Response) {
    try {
      $stream = $Err.Exception.Response.GetResponseStream()
      $reader = New-Object System.IO.StreamReader($stream)
      $apiError = $reader.ReadToEnd()
      $reader.Close()
    } catch { }
  }

  Write-Host "[$ProviderName] POST $Uri" -ForegroundColor Yellow
  Write-Host "[$ProviderName] HTTP status: $($Err.Exception.Message)" -ForegroundColor Yellow
  if ($apiError) {
    Write-Host "[$ProviderName] API response body:" -ForegroundColor Yellow
    Write-Host $apiError
  }
}

# --- Run ---

if ($Provider -eq "auto") {
  $chain = @($providers.Keys)
} else {
  $chain = @($Provider)
}

$dir = Split-Path $response -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

$responseText = $null
$usedProvider = $null
$usedModel = $null

foreach ($p in $chain) {
  $cfg = $providers[$p]
  $key = [Environment]::GetEnvironmentVariable($cfg.EnvVar, "User")
  if (-not $key) { $key = [Environment]::GetEnvironmentVariable($cfg.EnvVar, "Process") }
  if (-not $key) {
    Write-Host "[$p] skipped -- $($cfg.EnvVar) env var not set" -ForegroundColor DarkGray
    continue
  }

  $effectiveModel = if ($Model) { $Model } else { $cfg.DefaultModel }

  $dumpPath = if ($DumpBody) {
    Join-Path $dir "$Date-$Slug.$p.request.json"
  } else { $null }

  Write-Host "[$p] trying model=$effectiveModel..." -ForegroundColor Cyan

  try {
    $responseText = Invoke-OpenAICompat -ProviderName $p -Endpoint $cfg.Endpoint -Model $effectiveModel -Key $key -Prompt $prompt -DumpPath $dumpPath
    $usedProvider = $p
    $usedModel = $effectiveModel
    break
  } catch {
    Write-Host "[$p] failed; moving on" -ForegroundColor Yellow
    continue
  }
}

if (-not $responseText) {
  $checked = ($chain | ForEach-Object { "$($_) ($($providers[$_].EnvVar))" }) -join ", "
  Write-Error "All providers exhausted. Tried: $checked. Set DEEPSEEK_API_KEY or wait for capacity."
  exit 1
}

# --- Write response with provenance footer ---

$footer = "`n`n---`n_Generated by **$usedProvider** (``$usedModel``) on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')._`n"
($responseText + $footer) | Out-File -Encoding utf8 $response

Write-Host "Wrote $response ($($responseText.Length) chars, provider=$usedProvider, model=$usedModel)" -ForegroundColor Green
