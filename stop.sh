#!/usr/bin/env bash
# Stop DeerFlow API service
set -e
cd "$(dirname "$0")"

read_config_value() {
    local key="$1"
    local fallback="$2"
    python3 - "$key" "$fallback" <<'PY'
import sys

key, fallback = sys.argv[1], sys.argv[2]
try:
    in_api = False
    with open("config.yaml", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                in_api = line.strip() == "api:"
                continue
            if not in_api:
                continue
            name, sep, value = line.strip().partition(":")
            if sep and name == key:
                value = value.strip().strip('"\'')
                print(value or fallback)
                break
        else:
            print(fallback)
except Exception:
    print(fallback)
PY
}

APP_PORT="$(read_config_value port "${PORT:-8000}")"

if command -v lsof &>/dev/null; then
    PIDS="$(lsof -tiTCP:"${APP_PORT}" -sTCP:LISTEN || true)"
elif command -v fuser &>/dev/null; then
    PIDS="$(fuser "${APP_PORT}"/tcp 2>/dev/null || true)"
else
    echo "❌ Cannot find lsof or fuser. Install one of them to stop by port."
    exit 1
fi

if [ -z "${PIDS}" ]; then
    echo "ℹ️  No DeerFlow API process listening on port ${APP_PORT}."
    exit 0
fi

echo "🛑 Stopping DeerFlow API process(es) on port ${APP_PORT}: ${PIDS}"
kill ${PIDS}

for _ in 1 2 3 4 5; do
    sleep 1
    RUNNING=""
    for PID in ${PIDS}; do
        if ps -p "${PID}" >/dev/null 2>&1; then
            RUNNING="${RUNNING} ${PID}"
        fi
    done
    if [ -z "${RUNNING}" ]; then
        echo "✅ DeerFlow API stopped."
        exit 0
    fi
done

echo "⚠️  Process(es) still running after SIGTERM:${RUNNING}"
echo "   Run: kill -9${RUNNING}"
exit 1
