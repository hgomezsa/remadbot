#!/bin/bash
# watchdog.sh — Asegura que remadbot está corriendo. Relanza si ha caído.
#
# El bot controla internamente el horario de apertura:
#   - Dentro del horario: escanea catálogo + comprueba watchlist
#   - Fuera del horario: solo comprueba watchlist (24/7)
#
# Este script solo garantiza que el proceso esté vivo.
# Crontab: */2 * * * * /home/pi/remadbot/watchdog.sh

BOT_DIR="/home/pi/remadbot"
BOT_SCRIPT="remadbot.py"
LOG="$BOT_DIR/watchdog.log"
CONDA_BASE="/home/pi/miniforge3"
PID_FILE="$BOT_DIR/remadbot.pid"

# Python del entorno conda — sin necesidad de activarlo
PYTHON="$CONDA_BASE/envs/remadbot/bin/python3"

# ── Comprobar si el PID guardado sigue vivo ───────────────────────────────────

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        exit 0  # está vivo, no hacer nada
    fi
    rm -f "$PID_FILE"
fi

# ── Fallback: buscar proceso sin PID file (arranque manual, etc.) ─────────────

VIVO=$(pgrep -f "python.*remadbot\.py" 2>/dev/null | head -1)
if [ -n "$VIVO" ]; then
    echo "$VIVO" > "$PID_FILE"
    exit 0  # ya está corriendo, registrar PID y salir
fi

# ── No está corriendo: registrar y relanzar ───────────────────────────────────

echo "$(date '+%Y-%m-%d %H:%M:%S') remadbot caído, relanzando..." >> "$LOG"

cd "$BOT_DIR"
nohup "$PYTHON" "$BOT_SCRIPT" > "$BOT_DIR/remadbot.log" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') relanzado con PID $(cat $PID_FILE)" >> "$LOG"
