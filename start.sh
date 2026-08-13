#!/usr/bin/env bash
# Start DeerFlow API service
set -e
cd "$(dirname "$0")"

# --- Proxy hygiene -----------------------------------------------------------
# An https_proxy is set in this environment (e.g. http://172.17.0.1:11055) so
# outbound calls that genuinely need it (web search, foreign gateways) keep
# working. But with no NO_PROXY, *every* httpx call — including loopback/LAN and
# the domestic DashScope endpoint — would be tunnelled through the proxy, which
# is slow and can make internal requests hang ("no response"). Exempt internal
# and domestic traffic here. Append to any pre-existing NO_PROXY.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.local,dashscope.aliyuncs.com"
export no_proxy="$NO_PROXY"

# Silence langchain-openai's "custom httpx transport disables proxy
# auto-detection" warning and restore httpx's native env-proxy/NO_PROXY
# handling on every path. DeerFlow already injects no-keepalive httpx clients
# on hot paths (see deerflow/models/factory.py), so disabling langchain's
# socket-option transport here loses nothing in practice.
export LANGCHAIN_OPENAI_TCP_KEEPALIVE=0
# -----------------------------------------------------------------------------

# Reconcile the environment on every deployment/start.  ``--frozen`` makes
# uv.lock authoritative, while ``--inexact`` retains explicitly installed
# optional/operational packages (for example Feishu or sandbox extras).
echo "📦 Verifying dependencies from uv.lock..."
uv sync --frozen --inexact

# Install readabilipy's Readability.js dependencies (Node.js-based extraction)
READABILIPY_JSDIR="$(uv run python -c "import readabilipy.simple_json as m, os; print(os.path.join(os.path.dirname(m.__file__), 'javascript'))" 2>/dev/null)"
if [ -n "$READABILIPY_JSDIR" ] && [ ! -d "$READABILIPY_JSDIR/node_modules" ]; then
    if command -v npm &>/dev/null; then
        echo "📦 Installing Readability.js dependencies..."
        (cd "$READABILIPY_JSDIR" && npm install --no-audit --no-fund --loglevel=error)
    else
        echo "⚠️  npm not found — Readability.js unavailable, falling back to pure-Python extraction."
    fi
fi

# Check for config
if [ -f "./config.yaml" ]; then
    echo "📄 Using config: ./config.yaml"
else
    echo "⚠️  No config.yaml found. Copy config.example.yaml and edit it."
    echo "   cp config.example.yaml config.yaml"
    exit 1
fi

read_config_value() {
    local key="$1"
    local fallback="$2"
    uv run python - "$key" "$fallback" <<'PY'
import sys
import yaml

key, fallback = sys.argv[1], sys.argv[2]
try:
    with open("config.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    value = data.get("api", {}).get(key, fallback)
    if value is None:
        value = fallback
    print(value)
except Exception:
    print(fallback)
PY
}

APP_HOST="$(read_config_value host "${HOST:-127.0.0.1}")"
APP_PORT="$(read_config_value port "${PORT:-8000}")"

# Start uvicorn
echo "🦌 Starting DeerFlow API on http://${APP_HOST}:${APP_PORT}"
exec uv run uvicorn app:app \
    --host "${APP_HOST}" \
    --port "${APP_PORT}"
