#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_WAIT=10  # seconds to wait for process to exit
MODE="${1:-}"  # optional: "wecom" for WeCom bot mode

# Check if tmux session exists
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Error: tmux session '$TMUX_SESSION' does not exist"
    exit 1
fi

# Create __main__ window if it doesn't exist
if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Creating window '$TMUX_WINDOW'..."
    tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" -d
fi

# Get the pane PID (kept for diagnostics; detection uses pgrep to avoid pstree).
PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')

# pgrep over the bot's full command line. pstree was previously used but isn't
# installed on minimal containers, which silently failed and made restart a
# no-op. The bot is a singleton per host (one Telegram token = one updater),
# so matching globally is safe.
is_ccbot_running() {
    pgrep -f '/\.venv/bin/ccbot( |$)' >/dev/null 2>&1 \
        || pgrep -f 'uv run ccbot( |$)' >/dev/null 2>&1
}

# Stop existing process if running
if is_ccbot_running; then
    echo "Found running ccbot process, sending Ctrl-C..."
    tmux send-keys -t "$TARGET" C-c

    # Wait for process to exit
    waited=0
    while is_ccbot_running && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
    done

    if is_ccbot_running; then
        echo "Process did not exit after ${MAX_WAIT}s, sending SIGTERM..."
        # Kill the uv process directly
        UV_PID=$(pgrep -f 'uv run ccbot( |$)' | head -1)
        if [ -n "$UV_PID" ]; then
            kill "$UV_PID" 2>/dev/null || true
            sleep 2
        fi
        if is_ccbot_running; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$UV_PID" 2>/dev/null || true
            sleep 1
        fi
    fi

    echo "Process stopped."
else
    echo "No ccbot process running in $TARGET"
fi

# Brief pause to let the shell settle
sleep 1

# Start ccbot
CMD="cd ${PROJECT_DIR} && uv run ccbot"
if [ "$MODE" = "wecom" ]; then
    CMD="$CMD wecom"
fi
echo "Starting ccbot${MODE:+ ($MODE)} in $TARGET..."
tmux send-keys -t "$TARGET" "$CMD" Enter

# Verify startup and show logs
sleep 3
if is_ccbot_running; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
