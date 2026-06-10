#!/usr/bin/env bash
# gemini_review.sh — POST a review packet to the Gemini API; write the response.
#
# Bypasses the Gemini CLI deliberately. See docs/adr/0001-direct-gemini-api-for-reviews.md.
#
# Usage:
#   ./scripts/gemini_review.sh <slug> [model] [date]
#
# When [date] is omitted the script globs review_packets/*-<slug>.md
# and picks the newest match (lexicographic on YYYY-MM-DD == newest
# chronologically). Pass an explicit [date] to override — useful when
# re-running an older packet or when two sessions share a slug. Added
# 2026-05-28; the today-only default broke when reviews lagged a day
# behind the packet date.
#
# Examples:
#   ./scripts/gemini_review.sh simulator-pump
#   ./scripts/gemini_review.sh simulator-pump gemini-2.5-flash
#   ./scripts/gemini_review.sh simulator-pump gemini-2.5-pro 2026-05-24
#
# Requires: curl, jq, $GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)

set -euo pipefail

slug="${1:?Usage: $0 <slug> [model] [date]}"
model="${2:-gemini-pro-latest}"

# Date resolution: explicit third arg wins; otherwise glob + pick newest.
# Use ${3-} (not ${3:-}) to distinguish unset from empty — an explicitly
# empty date is a user error we want to surface, not silently glob over.
if [[ -n "${3-}" ]]; then
  date="$3"
  packet="review_packets/${date}-${slug}.md"
else
  shopt -s nullglob
  candidates=(review_packets/*-"${slug}".md)
  shopt -u nullglob
  if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "No packet found for slug '${slug}' under review_packets/. Tried glob '*-${slug}.md'. Pass [date] if the slug differs." >&2
    exit 1
  fi
  # Sort descending so the newest YYYY-MM-DD prefix wins.
  IFS=$'\n' read -r -d '' -a sorted < <(printf '%s\n' "${candidates[@]}" | sort -r && printf '\0')
  packet="${sorted[0]}"
  # Re-derive the date prefix from the chosen filename so the response
  # file lands at the same date.
  filename="${packet##*/}"
  filename="${filename%.md}"
  date="${filename%-${slug}}"
  echo "Auto-selected packet: ${packet##*/}" >&2
fi
response="review_responses/${date}-${slug}.md"

[[ -f "$packet" ]] || { echo "Packet not found: $packet" >&2; exit 1; }
[[ -n "${GEMINI_API_KEY:-}" ]] || {
  echo "GEMINI_API_KEY env var not set. Get one at https://aistudio.google.com/apikey" >&2
  exit 1
}

mkdir -p "$(dirname "$response")"

# Build JSON body with jq so the packet content is properly escaped.
body=$(jq -n --rawfile prompt "$packet" \
  '{contents: [{role: "user", parts: [{text: $prompt}]}]}')

url="https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}"

http_response=$(curl -sS -w "\n%{http_code}" -X POST "$url" \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary "$body")

http_code=$(echo "$http_response" | tail -n1)
body_response=$(echo "$http_response" | sed '$d')

if [[ "$http_code" != "200" ]]; then
  echo "Gemini API returned HTTP $http_code:" >&2
  echo "$body_response" >&2
  if [[ "$model" == *"pro"* && "$body_response" == *"UNAVAILABLE"* ]]; then
    echo "Pro is over capacity. Retry with: $0 $slug gemini-flash-latest" >&2
  fi
  exit 1
fi

text=$(echo "$body_response" | jq -r '.candidates[0].content.parts[0].text // empty')
if [[ -z "$text" ]]; then
  echo "Empty response from Gemini. Raw:" >&2
  echo "$body_response" >&2
  exit 1
fi

printf "%s" "$text" > "$response"
echo "Wrote $response (${#text} chars, model=$model)"
