#!/usr/bin/env bash
# Open the Halcyon console and bring host mode up with it.
#
# The console is a page served from loopback, and PAT and VarAC dial two other
# loopback ports -- 8300 for commands, 8301 for data -- so clicking the launcher
# starts the console, starts the host service inside it, then opens the page.
# It is safe to click twice: a console already on its port is reused, and a host
# that is already listening (this one, halcyon-switch, or a service unit) is left
# alone rather than fought over.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${HALCYON_PORT:-8730}"
CMD_PORT="${HALCYON_CMD_PORT:-8300}"
DATA_PORT="${HALCYON_DATA_PORT:-8301}"
URL="http://127.0.0.1:${PORT}/"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/halcyon/console.log"
PYTHON="${HALCYON_PYTHON:-python3}"

# A listener on the port is all that is asked for: the console is a browser page
# and the host is a socket, so accepting a connection is the whole test.
listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

logf() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

complain() {
    logf "$1"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="Halcyon" --text="$1"
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "Halcyon" "$1"
    else
        echo "$1" >&2
    fi
}

mkdir -p "$(dirname "$LOG")"

# --------------------------------------------------------------- the console
if ! listening "$PORT"; then
    cd "$ROOT" || exit 1
    # Detached, so closing the browser or the launcher leaves the console up.
    setsid "$PYTHON" -m halcyon.ui --port "$PORT" >>"$LOG" 2>&1 &
    for _ in $(seq 1 50); do
        listening "$PORT" && break
        sleep 0.2
    done
fi
if ! listening "$PORT"; then
    complain "Halcyon console did not start on port ${PORT}. See $LOG"
    exit 1
fi

# ----------------------------------------------------------- the host service
# Started through the console's own API rather than as a second process, so the
# Services panel shows it running and can stop it again.  The console chooses the
# bandwidth PAT asks for later; 2300 is the default the docs use.
if listening "$CMD_PORT"; then
    logf "host service already listening on $CMD_PORT; left as it was"
else
    mycall="${HALCYON_MYCALL:-N0CALL}"
    bandwidth="${HALCYON_BANDWIDTH:-2300}"
    body="{\"mycall\":\"${mycall}\",\"bandwidth\":${bandwidth},\"cmd_port\":${CMD_PORT},\"data_port\":${DATA_PORT}}"
    reply=$(curl -sS --noproxy '*' --max-time 20 -X POST \
        -H 'Content-Type: application/json' --data "$body" \
        "${URL}api/services/start" 2>&1) || true
    if listening "$CMD_PORT"; then
        logf "host service up on $CMD_PORT/$DATA_PORT as ${mycall}, ${bandwidth} Hz"
    else
        # The console is still usable without the host, so open it and say what
        # went wrong rather than refusing to start: the panel shows the same
        # error and the ports can be freed there.
        complain "Halcyon console is up, but the host service did not: ${reply:-no reply}"
    fi
fi

# A headless box has no browser to open, and a launcher that dies there looks
# like a failed start even though the console and host are up.
if ! { command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1; }; then
    logf "console at $URL"
    printf 'Halcyon console is up: %s\n' "$URL"
fi
