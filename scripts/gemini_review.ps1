<#
.SYNOPSIS
  Run a review packet through the Gemini API and write the response.

.DESCRIPTION
  Reads review_packets/<date>-<slug>.md, POSTs the contents to the Gemini
  generateContent endpoint, writes the model's response to
  review_responses/<date>-<slug>.md as UTF-8.

  Bypasses the Gemini CLI deliberately. See docs/adr/0001-direct-gemini-api-for-reviews.md.

.PARAMETER Slug
  Packet slug — the part after the date. For
  review_packets/2026-05-24-simulator-pump.md, pass "simulator-pump".

.PARAMETER Model
  Gemini model to use. Default: gemini-pro-latest (a moving alias —
  always the current top-tier Pro). Fall back to gemini-flash-latest
  if Pro is over capacity (503s) or for cheaper/faster reviews.

.PARAMETER Date
  Date prefix of the packet file. Defaults to today (YYYY-MM-DD).

.EXAMPLE
  .\scripts\gemini_review.ps1 -Slug simulator-pump

.EXAMPLE
  .\scripts\gemini_review.ps1 -Slug simulator-pump -Model gemini-2.5-flash

.NOTES
  Requires $env:GEMINI_API_KEY (get one at https://aistudio.google.com/apikey).
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Slug,
  [string]$Model = "gemini-pro-latest",
  [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
  [switch]$DumpBody
)

$ErrorActionPreference = "Stop"

$packet   = Join-Path "review_packets"   "$Date-$Slug.md"
$response = Join-Path "review_responses" "$Date-$Slug.md"

if (-not (Test-Path $packet))      { Write-Error "Packet not found: $packet"; exit 1 }
if (-not $env:GEMINI_API_KEY)      { Write-Error "GEMINI_API_KEY env var not set. Get one at https://aistudio.google.com/apikey"; exit 1 }

# Read the prompt via .NET with an explicit UTF-8 encoding. Without the
# explicit encoding, Windows PowerShell 5.1 falls back to the system ANSI
# codepage (Windows-1252) for files without a BOM, mojibake-ing em-dashes
# and other non-ASCII characters. Also guarantees a bare System.String
# (no PSObject wrapping that ConvertTo-Json might introspect).
$prompt = [System.IO.File]::ReadAllText((Resolve-Path $packet).Path, [System.Text.Encoding]::UTF8)

# JSON-escape the prompt using .NET, bypassing ConvertTo-Json entirely.
# Earlier attempts using ConvertTo-Json on the nested string produced
# "Starting an object on a scalar field" from the Gemini API — depth-N
# serialization can treat the string as an object with Length/Chars
# properties. HttpUtility.JavaScriptStringEncode returns a JSON-valid
# string with surrounding quotes when the second arg is $true.
Add-Type -AssemblyName System.Web
$promptJson = [System.Web.HttpUtility]::JavaScriptStringEncode($prompt, $true)
$body = '{"contents":[{"role":"user","parts":[{"text":' + $promptJson + '}]}]}'

if ($DumpBody) {
  # Resolve to absolute path: .NET's File.WriteAllText uses [Environment]::CurrentDirectory,
  # which is NOT the same as PowerShell's $PWD. Without this, dumps land in the user's
  # home dir even when the shell is sitting in the project root.
  $dir = Join-Path $PWD "review_responses"
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $dumpPath = Join-Path $dir "$Date-$Slug.request.json"
  [System.IO.File]::WriteAllText($dumpPath, $body, [System.Text.UTF8Encoding]::new($false))
  Write-Host "Request body dumped to $dumpPath ($($body.Length) chars)"
}

$uri = "https://generativelanguage.googleapis.com/v1beta/models/${Model}:generateContent?key=$env:GEMINI_API_KEY"

try {
  # Convert body to UTF-8 bytes BEFORE handing to Invoke-RestMethod. In
  # Windows PowerShell 5.1, IRM with a string -Body can re-encode as
  # ASCII regardless of the charset declared in Content-Type, mangling
  # any non-ASCII characters in transit. Bytes bypass the re-encoding.
  $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
  $r = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json; charset=utf-8" -Body $bodyBytes
}
catch {
  # Surface Gemini's actual error body, not just the HTTP status line.
  # PS 7+ exposes it as $_.ErrorDetails.Message; PS 5.1 needs the response stream.
  $apiError = $null
  if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
    $apiError = $_.ErrorDetails.Message
  }
  elseif ($_.Exception.Response) {
    try {
      $stream = $_.Exception.Response.GetResponseStream()
      $reader = New-Object System.IO.StreamReader($stream)
      $apiError = $reader.ReadToEnd()
      $reader.Close()
    } catch { }
  }

  Write-Host "Request was POST $($uri -replace 'key=[^&]+','key=<redacted>')" -ForegroundColor Yellow
  Write-Host "HTTP status: $($_.Exception.Message)" -ForegroundColor Yellow
  if ($apiError) {
    Write-Host "API response body:" -ForegroundColor Yellow
    Write-Host $apiError
  }

  Write-Error "Gemini API call failed."
  if ($Model -like "*pro*" -and ($apiError -match "UNAVAILABLE|high demand" -or "$_" -match "503")) {
    Write-Host "Pro is over capacity. Retry with: .\scripts\gemini_review.ps1 -Slug $Slug -Model gemini-flash-latest"
  }
  exit 1
}

$text = $r.candidates[0].content.parts[0].text
if (-not $text) { Write-Error "Empty response from Gemini. Raw response: $($r | ConvertTo-Json -Depth 10)"; exit 1 }

# Ensure target dir exists.
$dir = Split-Path $response -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

$text | Out-File -Encoding utf8 $response
Write-Host "Wrote $response ($($text.Length) chars, model=$Model)"
