#!/bin/bash
# watchdog.sh — Comprueba que remadbot está corriendo y lo relanza si no.
# Crontab: */2 * * * * /home/pi/remadbot/watchdog.sh
#
# Horario de operación:
#   Lunes–viernes : 15:00 – 20:00
#   Sábados       : 08:00 – 20:00
#   Domingos y festivos : 09:00 – 14:00
#
# Festivos: añadir una línea YYYY-MM-DD en festivos.txt (mismo directorio)

BOT_DIR="/home/pi/remadbot"
BOT_SCRIPT="remadbot.py"
LOG="$BOT_DIR/watchdog.log"
CONDA_BASE="/home/pi/miniforge3"
PID_FILE="$BOT_DIR/remadbot.pid"
FESTIVOS_FILE="$BOT_DIR/festivos.txt"

# Python del entorno conda — sin necesidad de activarlo
PYTHON="$CONDA_BASE/envs/remadbot/bin/python3"

# ── Calcular si estamos en horario de apertura ────────────────────────────────

HOY=$(date '+%Y-%m-%d')
DIA=$(date '+%u')          # 1=lunes … 6=sábado … 7=domingo
HORA=$(date '+%H')
MIN=$(date '+%M')
HORA_MIN=$(( 10#$HORA * 60 + 10#$MIN ))

# Comprobar festivo
ES_FESTIVO=false
if [ -f "$FESTIVOS_FILE" ] && grep -qx "$HOY" "$FESTIVOS_FILE" 2>/dev/null; then
    ES_FESTIVO=true
fi

EN_HORARIO=false
if [ "$ES_FESTIVO" = true ] || [ "$DIA" -eq 7 ]; then
    # Domingo o festivo: 09:00 – 14:00  (540 – 840 min)
    [ $HORA_MIN -ge 540 ] && [ $HORA_MIN -lt 840 ] && EN_HORARIO=true
elif [ "$DIA" -eq 6 ]; then
    # Sábado: 08:00 – 20:00  (480 – 1200 min)
    [ $HORA_MIN -ge 480 ] && [ $HORA_MIN -lt 1200 ] && EN_HORARIO=true
else
    # Lunes–viernes: 15:00 – 20:00  (900 – 1200 min)
    [ $HORA_MIN -ge 900 ] && [ $HORA_MIN -lt 1200 ] && EN_HORARIO=true
fi

# ── Fuera de horario: apagar el bot si está corriendo ────────────────────────

if [ "$EN_HORARIO" = false ]; then
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') Fuera de horario — parando bot (PID $PID)" >> "$LOG"
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WARNING] remadbot detenido por watchdog (fuera de horario)" >> "$BOT_DIR/remadbot.log"
            kill "$PID"
            rm -f "$PID_FILE"
        fi
    fi
    exit 0
fi

# ── En horario: comprobar si el PID guardado sigue vivo ──────────────────────

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        exit 0  # está vivo, no hacer nada
    fi
fi

# No está corriendo — registrar y relanzar
echo "$(date '+%Y-%m-%d %H:%M:%S') remadbot caído, relanzando..." >> "$LOG"

cd "$BOT_DIR"
nohup "$PYTHON" "$BOT_SCRIPT" > "$BOT_DIR/remadbot.log" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') relanzado con PID $(cat $PID_FILE)" >> "$LOG"
