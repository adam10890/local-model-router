#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:9000}"
API_KEY="${A0_LMM_ROUTER_API_KEY:-}"

usage() {
  cat <<'EOF'
Usage: scripts/smoke_provider.sh [options]

Options:
  --base-url URL    Provider URL, default http://127.0.0.1:9000
  --api-key KEY     Bearer token for protected endpoints
  -h, --help        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="${2%/}"; shift 2 ;;
    --api-key)
      API_KEY="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

AUTH_ARGS=()
if [[ -n "$API_KEY" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer $API_KEY")
fi

request() {
  local name="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  local allowed="${5:-200}"
  local tmp
  tmp="$(mktemp)"

  local args=(-sS -o "$tmp" -w "%{http_code}" -X "$method" "${AUTH_ARGS[@]}")
  if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" --data "$body")
  fi

  local status
  status="$(curl "${args[@]}" "$BASE_URL$path")"
  if [[ ",$allowed," != *",$status,"* ]]; then
    echo "[FAIL] $name -> HTTP $status" >&2
    cat "$tmp" >&2
    echo >&2
    rm -f "$tmp"
    exit 1
  fi

  echo "[OK] $name -> HTTP $status"
  rm -f "$tmp"
}

request "health" GET "/health" "" "200"

request "fleet status" GET "/fleet/status" "" "200"

request "routing request" POST "/routing/request" '{
  "agent_id": "phase9-smoke",
  "agent_type": "smoke",
  "role": "chat",
  "task_type": "smoke",
  "local_only": true
}' "200"

request "chat completions" POST "/v1/chat/completions" '{
  "model": "local-chat",
  "stream": false,
  "messages": [{"role": "user", "content": "phase9 smoke test"}]
}' "200,503"

echo "Smoke complete."
