#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:9000}"
API_KEY="${A0_LMM_ROUTER_API_KEY:-}"
JSON_OUTPUT=""
REQUIRE_LIVE=false

usage() {
  cat <<'EOF'
Usage: scripts/smoke_provider.sh [options]
  --base-url URL       Provider URL, default http://127.0.0.1:9000
  --api-key KEY        Bearer token for protected endpoints
  --json-output PATH   Write a sanitized machine-readable result
  --require-live       Fail chat, stream, or tools on HTTP 503
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --json-output) JSON_OUTPUT="$2"; shift 2 ;;
    --require-live) REQUIRE_LIVE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

AUTH_ARGS=()
if [[ -n "$API_KEY" ]]; then AUTH_ARGS=(-H "Authorization: Bearer $API_KEY"); fi
WORK_DIR="$(mktemp -d)"
RESULTS="$WORK_DIR/results.tsv"
FAILED=false
LIVE=true
trap 'rm -rf "$WORK_DIR"' EXIT

record() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$RESULTS"; }

request() {
  local name="$1" method="$2" path="$3" body="${4:-}" allowed="${5:-200}"
  local target="$WORK_DIR/$name.body" status
  local args=(-sS -o "$target" -w "%{http_code}" -X "$method" "${AUTH_ARGS[@]}")
  if [[ -n "$body" ]]; then args+=(-H "Content-Type: application/json" --data "$body"); fi
  status="$(curl "${args[@]}" "$BASE_URL$path" 2>/dev/null || printf '000')"
  if [[ ",$allowed," != *",$status,"* ]]; then
    record "$name" fail "$status" "http_$status"
    FAILED=true
    return 1
  fi
  if [[ "$status" == 503 ]]; then
    record "$name" unavailable "$status" model_unavailable
    LIVE=false
    echo "[UNAVAILABLE] $name -> HTTP 503"
    return 0
  fi
  record "$name" pass "$status" ""
  echo "[OK] $name -> HTTP $status"
}

request health GET /health || true
request models GET /v1/models || true
request fleet GET /fleet/status || true
request route POST /routing/request '{"agent_id":"provider-smoke","agent_type":"smoke","role":"chat","task_type":"smoke","local_only":true}' || true
LIVE_ALLOWED=200
if [[ "$REQUIRE_LIVE" == false ]]; then LIVE_ALLOWED=200,503; fi
request chat POST /v1/chat/completions '{"model":"local-chat","stream":false,"messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16}' "$LIVE_ALLOWED" || true
request stream POST /v1/chat/completions '{"model":"local-chat","stream":true,"messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16}' "$LIVE_ALLOWED" || true
request tools POST /v1/chat/completions '{"model":"local-chat","stream":false,"messages":[{"role":"user","content":"Call imperium_ping now."}],"tools":[{"type":"function","function":{"name":"imperium_ping","description":"Return a smoke verification ping","parameters":{"type":"object","properties":{},"additionalProperties":false}}}],"tool_choice":"required","chat_template_kwargs":{"enable_thinking":false},"temperature":0,"max_tokens":64}' "$LIVE_ALLOWED" || true

if [[ "$FAILED" == false && "$LIVE" == true ]]; then
  if ! python - "$WORK_DIR/chat.body" "$WORK_DIR/tools.body" <<'PY'
import json, sys
chat = json.load(open(sys.argv[1], encoding="utf-8"))
tools = json.load(open(sys.argv[2], encoding="utf-8"))
assert chat.get("choices")
calls = ((tools.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
assert any((call.get("function") or {}).get("name") == "imperium_ping" for call in calls)
PY
  then
    record validation fail 0 invalid_live_response
    FAILED=true
  fi
fi

python - "$RESULTS" "$JSON_OUTPUT" "$FAILED" "$REQUIRE_LIVE" "$LIVE" <<'PY'
import json, pathlib, sys
rows = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    name, status, http_status, error_code = line.split("\t")
    row = {"name": name, "status": status}
    if http_status and http_status != "0": row["http_status"] = int(http_status)
    if error_code: row["error_code"] = error_code
    rows.append(row)
report = {
    "schema_version": 1,
    "kind": "provider_smoke",
    "ok": sys.argv[3] == "false",
    "require_live": sys.argv[4] == "true",
    "live": sys.argv[5] == "true" and sys.argv[3] == "false",
    "checks": rows,
}
text = json.dumps(report, indent=2, sort_keys=True)
if sys.argv[2]:
    target = pathlib.Path(sys.argv[2]); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
print(text)
PY

if [[ "$FAILED" == true ]]; then exit 1; fi
