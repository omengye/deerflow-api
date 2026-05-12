#!/usr/bin/env bash
# Run DeerFlow API as a simple background daemon with automatic restart.
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="${PID_FILE:-data/deerflow-api.daemon.pid}"
STOP_FILE="${STOP_FILE:-data/deerflow-api.daemon.stop}"
LOG_FILE="${LOG_FILE:-deerflow-api.log}"
RESTART_DELAY="${RESTART_DELAY:-5}"

usage() {
    printf 'Usage: %s {start|stop|restart|status|logs}\n' "$0"
}

is_daemon_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi

    local pid
    pid="$(cat "$PID_FILE")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_daemon() {
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$STOP_FILE")"

    if is_daemon_running; then
        printf 'ℹ️  DeerFlow API daemon already running (pid %s).\n' "$(cat "$PID_FILE")"
        return 0
    fi

    rm -f "$STOP_FILE"
    (
        while [ ! -f "$STOP_FILE" ]; do
            if ./start.sh; then
                exit_code=0
            else
                exit_code=$?
            fi

            if [ -f "$STOP_FILE" ]; then
                break
            fi

            printf '[%s] DeerFlow API exited with code %s; restarting in %ss.\n' \
                "$(date '+%Y-%m-%d %H:%M:%S')" "$exit_code" "$RESTART_DELAY"
            sleep "$RESTART_DELAY"
        done
    ) >> "$LOG_FILE" 2>&1 &

    printf '%s\n' "$!" > "$PID_FILE"
    printf '✅ DeerFlow API daemon started (pid %s, log %s).\n' "$(cat "$PID_FILE")" "$LOG_FILE"
}

stop_daemon() {
    mkdir -p "$(dirname "$STOP_FILE")"
    touch "$STOP_FILE"

    if [ -x ./stop.sh ]; then
        ./stop.sh || true
    fi

    if is_daemon_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        for _ in 1 2 3 4 5; do
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    fi

    rm -f "$PID_FILE" "$STOP_FILE"
    printf '✅ DeerFlow API daemon stopped.\n'
}

status_daemon() {
    if is_daemon_running; then
        printf '✅ DeerFlow API daemon is running (pid %s).\n' "$(cat "$PID_FILE")"
    else
        printf 'ℹ️  DeerFlow API daemon is not running.\n'
        return 1
    fi
}

case "${1:-}" in
    start)
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        start_daemon
        ;;
    status)
        status_daemon
        ;;
    logs)
        if [ -f "$LOG_FILE" ]; then
            less +F "$LOG_FILE"
        else
            printf 'ℹ️  Log file does not exist yet: %s\n' "$LOG_FILE"
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac
