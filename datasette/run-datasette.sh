#!/usr/bin/env bash
# ==============================================================================
# Datasette Web UI Launcher with datasette-plot
# ==============================================================================
# Binds to 0.0.0.0:8001 for access via Tailscale / local network

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load config if available
if [[ -f "/etc/power-monitor/config.env" ]]; then
    # shellcheck disable=SC1091
    source /etc/power-monitor/config.env
elif [[ -f "${PROJECT_ROOT}/config.env" ]]; then
    # shellcheck disable=SC1091
    source "${PROJECT_ROOT}/config.env"
fi

HOST="${DATASETTE_HOST:-0.0.0.0}"
PORT="${DATASETTE_PORT:-8001}"
DB_PATH="${DB_PATH:-/data/energy_monitor.db}"
METADATA_PATH="${METADATA_PATH:-${SCRIPT_DIR}/metadata.json}"

# Check virtualenv if it exists
if [[ -d "/opt/power-tracker/venv/bin" ]]; then
    export PATH="/opt/power-tracker/venv/bin:$PATH"
fi

# Ensure database exists before launching
if [[ ! -f "$DB_PATH" ]]; then
    echo "[datasette-launcher] Database not found at $DB_PATH. Initializing..."
    python3 "${PROJECT_ROOT}/init_db.py" --db-path "$DB_PATH"
fi

# Ensure datasette and datasette-plot are installed
if ! command -v datasette >/dev/null 2>&1; then
    echo "[datasette-launcher] Datasette not found. Installing datasette and datasette-plot..."
    pip install --quiet datasette datasette-plot
fi

# Verify datasette-plot plugin is present
if ! datasette plugins | grep -q "datasette-plot"; then
    echo "[datasette-launcher] datasette-plot plugin not detected. Installing..."
    pip install --quiet datasette-plot
fi

echo "[datasette-launcher] Starting Datasette on http://${HOST}:${PORT}"
echo "[datasette-launcher] Database: ${DB_PATH}"
echo "[datasette-launcher] Metadata: ${METADATA_PATH}"

exec datasette "$DB_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --metadata "$METADATA_PATH" \
    --setting sql_time_limit_ms 5000 \
    --setting max_returned_rows 2000
