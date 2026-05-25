#!/usr/bin/env bash
# gemini_review.sh — POST a review packet to the Gemini API; write the response.
#
# Bypasses the Gemini CLI deliberately. See docs/adr/0001-direct-gemini-api-for-reviews.md.
#
# Usage:
#   ./scripts/gemini_review.sh <slug> [model] [date]
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
date="${3:-$(date -u +%Y-%m-%d)}"

packet="review_packets/${date}-${slug}.md"
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
