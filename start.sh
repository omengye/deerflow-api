#!/usr/bin/env bash
# Start DeerFlow API service
set -e
cd "$(dirname "$0")"

# Install dependencies (if not already installed)
if [ ! -d ".venv" ]; then
    echo "📦 Installing dependencies with uv..."
    uv sync
fi

# Check for config
if [ -f "./config.yaml" ]; then
    export DEER_FLOW_CONFIG_PATH="./config.yaml"
    export DEER_FLOW_HOME="${DEER_FLOW_HOME:-./data/deerflow}"
    echo "📄 Using config: ./config.yaml"
else
    echo "⚠️  No config.yaml found. Copy config.example.yaml and edit it."
    echo "   cp config.example.yaml config.yaml"
    exit 1
fi

# Start uvicorn
echo "🦌 Starting DeerFlow API on http://0.0.0.0:${PORT:-8000}"
exec uv run uvicorn app:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}"
