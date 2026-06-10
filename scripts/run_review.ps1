<#
.SYNOPSIS
  Run a review packet through an LLM API and write the response.

.DESCRIPTION
  Reads review_packets/<date>-<slug>.md, POSTs the contents to a model
  provider's generate endpoint, writes the response to
  review_responses/<date>-<slug>.md as UTF-8.

  Default: cascades through providers gemini -> openrouter -> groq ->
  cerebras, picking the first one whose API key env var is set and
  whose call succeeds. This protects against Gemini being out of
  capacity (503) or out of credits (4xx). Each provider in the chain
  uses its own free-tier-friendly default model unless overridden via
  -Model (which applies only to the first provider tried).

  Bypasses the Gemini CLI deliberately. See docs/adr/0001-direct-gemini-api-for-reviews.md.

.PARAMETER Slug
  Packet slug -- the part after the date. For
  review_packets/2026-05-24-simulator-pump.md, pass "simulator-pump".

.PARAMETER Model
  Model identifier to use for the FIRST provider tried. Subsequent
  fallback providers use their own defaults (mixing model names
  across providers doesn't work -- gemini-pro-latest is invalid for
  Groq, llama-3.3-70b is invalid for Gemini). If you want a specific
  model from a specific fallback, pair this with -Provider.

.PARAMETER Date
  Date prefix of the packet file. When omitted, the script globs
  review_packets/*-<slug>.md and picks the newest match.

.PARAMETER Provider
  auto (default)   : cascade gemini -> openrouter -> groq -> cerebras
                     (skipping providers without an API key env var)
  gemini           : Google Gemini only (no cascade)
  openrouter       : OpenRouter only
  groq             : Groq only
  cerebras         : Cerebras only

.PARAMETER DumpBody
  Dump each provider's outbound request body to
  review_responses/<date>-<slug>.<provider>.request.json for debugging.

.EXAMPLE
  .\scripts\gemini_review.ps1 -Slug simulator-pump
  # tries gemini, falls back through openrouter/groq/cerebras on failure

.EXAMPLE
  .\scripts\gemini_review.ps1 -Slug simulator-pump -Provider groq

.EXAMPLE
  .\scripts\gemini_review.ps1 -Slug simulator-pump -Model gemini-2.5-flash
  # tries gemini with the explicit model; falls back if it fails

.NOTES
  Required env vars per provider (set whichever you have):
    GEMINI_API_KEY      https://aistudio.google.com/apikey
    OPENROUTER_API_KEY  https://openrouter.ai/keys
    GROQ_API_KEY        https://console.groq.com/keys
    CEREBRAS_API_KEY    https://cloud.cerebras.ai/?tab=api-keys
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Slug,
  [string]$Model,
  [string]$Date,
  [ValidateSet("auto", "gemini", "openrouter", "groq", "cerebras")]
  [string]$Provider = "auto",
  [switch]$DumpBody
)

$ErrorActionPreference = "Stop"

# --- Provider config map ---
#
# Each entry carries the free-tier-friendly default model and the env
# var the script reads the API key from. The OpenAI-compatible
# providers (openrouter/groq/cerebras) share one Invoke-OpenAICompat
# helper; Gemini has its own native shape.

$providers = [ordered]@{
  gemini     = @{ DefaultModel = "gemini-pro-latest"; EnvVar = "GEMINI_API_KEY"; Endpoint = $null }
  openrouter = @{ DefaultModel = "deepseek/deepseek-r1:free"; EnvVar = "OPENROUTER_API_KEY"; Endpoint = "https://openrouter.ai/api/v1/chat/completions" }
  groq       = @{ DefaultModel = "llama-3.3-70b-versatile"; EnvVar = "GROQ_API_KEY"; Endpoint = "https://api.groq.com/openai/v1/chat/completions" }
  cerebras   = @{ DefaultModel = "llama-3.3-70b"; EnvVar = "CEREBRAS_API_KEY"; Endpoint = "https://api.cerebras.ai/v1/chat/completions" }
}

# --- Packet auto-pick (unchanged from pre-cascade behavior) ---

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

# --- Provider invocation helpers ---
#
# Each helper throws on failure (after printing the API's error body
# to the console). The outer cascade catches and moves to the next
# provider. Successful return: the response text as a [string].

function Invoke-Gemini {
  param([string]$Model, [string]$Key, [string]$Prompt, [string]$DumpPath)

  $promptJson = [System.Web.HttpUtility]::JavaScriptStringEncode($Prompt, $true)
  $body = '{"contents":[{"role":"user","parts":[{"text":' + $promptJson + '}]}]}'

  if ($DumpPath) {
    [System.IO.File]::WriteAllText($DumpPath, $body, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[gemini] request body dumped to $DumpPath ($($body.Length) chars)" -ForegroundColor DarkGray
  }

  $uri = "https://generativelanguage.googleapis.com/v1beta/models/${Model}:generateContent?key=$Key"
  $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

  try {
    $r = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json; charset=utf-8" -Body $bodyBytes
  } catch {
    Write-ApiError -ProviderName "gemini" -Uri ($uri -replace 'key=[^&]+','key=<redacted>') -Err $_
    throw
  }

  $text = $r.candidates[0].content.parts[0].text
  if (-not $text) { throw "[gemini] empty response. Raw: $($r | ConvertTo-Json -Depth 10)" }
  return $text
}

function Invoke-OpenAICompat {
  param([string]$ProviderName, [string]$Endpoint, [string]$Model, [string]$Key, [string]$Prompt, [string]$DumpPath)

  $promptJson = [System.Web.HttpUtility]::JavaScriptStringEncode($Prompt, $true)
  $modelJson = [System.Web.HttpUtility]::JavaScriptStringEncode($Model, $true)
  # OpenAI-compatible chat completions shape. temperature defaults are
  # fine for review prompts; max_tokens unset = provider default
  # (usually generous on free tiers).
  $body = '{"model":' + $modelJson + ',"messages":[{"role":"user","content":' + $promptJson + '}]}'

  if ($DumpPath) {
    [System.IO.File]::WriteAllText($DumpPath, $body, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[$ProviderName] request body dumped to $DumpPath ($($body.Length) chars)" -ForegroundColor DarkGray
  }

  $headers = @{
    "Authorization" = "Bearer $Key"
    "Content-Type"  = "application/json; charset=utf-8"
  }
  # OpenRouter recommends an HTTP-Referer for free-tier rate-limit
  # heuristics; the value doesn't have to resolve. Skipping it doesn't
  # error but can lower priority.
  if ($ProviderName -eq "openrouter") {
    $headers["HTTP-Referer"] = "https://github.com/local-ml-obs-pipeline"
    $headers["X-Title"]      = "ML Observability Pipeline review"
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

# --- Cascade ---

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

  # -Model applies only to the FIRST provider tried (avoids the
  # cross-provider model-name mismatch). Subsequent providers use
  # their own free-tier defaults.
  $effectiveModel = if ($Model -and $p -eq $chain[0]) { $Model } else { $cfg.DefaultModel }

  $dumpPath = if ($DumpBody) {
    Join-Path $dir "$Date-$Slug.$p.request.json"
  } else { $null }

  Write-Host "[$p] trying model=$effectiveModel..." -ForegroundColor Cyan

  try {
    if ($p -eq "gemini") {
      $responseText = Invoke-Gemini -Model $effectiveModel -Key $key -Prompt $prompt -DumpPath $dumpPath
    } else {
      $responseText = Invoke-OpenAICompat -ProviderName $p -Endpoint $cfg.Endpoint -Model $effectiveModel -Key $key -Prompt $prompt -DumpPath $dumpPath
    }
    $usedProvider = $p
    $usedModel = $effectiveModel
    break
  } catch {
    Write-Host "[$p] failed; moving on to next provider in chain" -ForegroundColor Yellow
    continue
  }
}

if (-not $responseText) {
  $checked = ($chain | ForEach-Object { "$($_) ($($providers[$_].EnvVar))" }) -join ", "
  Write-Error "All providers exhausted. Tried: $checked. Set at least one API key env var or wait for capacity."
  exit 1
}

# --- Write response with provenance footer ---

$footer = "`n`n---`n_Generated by **$usedProvider** (``$usedModel``) on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')._`n"
($responseText + $footer) | Out-File -Encoding utf8 $response

Write-Host "Wrote $response ($($responseText.Length) chars, provider=$usedProvider, model=$usedModel)" -ForegroundColor Green
