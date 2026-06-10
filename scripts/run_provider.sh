#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

HOST_NAME="${OBSERVER_HOST:-127.0.0.1}"
PORT="${OBSERVER_PORT:-9000}"
CONFIG_PATH="${A0_LMM_ROUTER_CONFIG:-$PLUGIN_ROOT/conf/llama_cpp_servers.yaml}"
API_KEY="${A0_LMM_ROUTER_API_KEY:-}"
ALLOW_PUBLIC_NO_AUTH="${A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH:-}"
INSTALL_DEPS=0
PYTHON_BIN="${PYTHON:-python3}"

usage() {
  cat <<'EOF'
Usage: scripts/run_provider.sh [options]

Options:
  --host HOST              Bind host, default 127.0.0.1
  --port PORT              Bind port, default 9000
  --config PATH            llama_cpp_servers.yaml path
  --api-key KEY            Require Authorization: Bearer KEY
  --public-no-auth         Allow non-local bind without an API key
  --install-deps           pip install -e . before start
  --python BIN             Python executable, default python3
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST_NAME="$2"; shift 2 ;;
    --port)
      PORT="$2"; shift 2 ;;
    --config)
      CONFIG_PATH="$2"; shift 2 ;;
    --api-key)
      API_KEY="$2"; shift 2 ;;
    --public-no-auth)
      ALLOW_PUBLIC_NO_AUTH=1; shift ;;
    --install-deps)
      INSTALL_DEPS=1; shift ;;
    --python)
      PYTHON_BIN="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config file not found: $CONFIG_PATH" >&2
  exit 1
fi

export OBSERVER_HOST="$HOST_NAME"
export OBSERVER_PORT="$PORT"
export A0_LMM_ROUTER_CONFIG="$CONFIG_PATH"

if [[ -n "$API_KEY" ]]; then
  export A0_LMM_ROUTER_API_KEY="$API_KEY"
fi
if [[ -n "$ALLOW_PUBLIC_NO_AUTH" ]]; then
  export A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH="$ALLOW_PUBLIC_NO_AUTH"
fi

if [[ "$INSTALL_DEPS" == "1" ]]; then
  "$PYTHON_BIN" -m pip install -e "$PLUGIN_ROOT"
fi

echo "Starting a0_lmm_router Fleet Manager"
echo "  Bind:   http://$OBSERVER_HOST:$OBSERVER_PORT"
echo "  Config: $A0_LMM_ROUTER_CONFIG"
if [[ -n "${A0_LMM_ROUTER_API_KEY:-}" ]]; then
  echo "  Auth:   Bearer token required"
else
  echo "  Auth:   dev/no key"
fi

cd "$PLUGIN_ROOT"
exec "$PYTHON_BIN" -m local_model_router.service
