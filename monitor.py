#!/usr/bin/env python3
"""
=============================================================================
Proxmox Host Power & Electricity Cost Tracker (monitor.py)
=============================================================================
Target: Proxmox LXC Container (Alpine / Debian Slim, 512MB RAM constraint)
Memory Footprint: <20MB RAM
Purpose:
  1. Connect to Zigbee2MQTT local WebSocket event stream (ws://localhost:8080/api).
  2. Ingest power (Watts) and lifetime cumulative energy (kWh) from IKEA INSPELNING plug.
  3. Compute offline-resilient energy deltas and calculate financial cost.
  4. Persist data into SQLite (/data/energy_monitor.db) for Datasette analysis.
=============================================================================
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import websockets
except ImportError:
    websockets = None


# -----------------------------------------------------------------------------
# Configuration & Environment Parsing
# -----------------------------------------------------------------------------
def load_env_file(filepath: str) -> None:
    """Load key-value pairs from a .env configuration file into os.environ."""
    path = Path(filepath)
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print(f"Warning: Could not read env file {filepath}: {e}", file=sys.stderr)


# Search standard configuration locations
for env_candidate in [
    os.getenv("CONFIG_FILE", ""),
    "/etc/power-monitor/config.env",
    str(Path(__file__).parent / "config.env"),
    str(Path(__file__).parent / ".env"),
]:
    if env_candidate:
        load_env_file(env_candidate)

# Configuration Parameters
COST_PER_KWH = float(os.getenv("COST_PER_KWH", "0.25"))
DB_PATH = os.getenv("DB_PATH", "/data/energy_monitor.db")
Z2M_WS_URL = os.getenv("Z2M_WS_URL", "ws://localhost:8080/api")
DEVICE_FRIENDLY_NAME = os.getenv("DEVICE_FRIENDLY_NAME", "proxmox_power_plug").strip()
MIN_POWER_CHANGE_W = float(os.getenv("MIN_POWER_CHANGE_W", "1.0"))
MAX_INTERVAL_SECONDS = float(os.getenv("MAX_INTERVAL_SECONDS", "60.0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Configure Logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("power-monitor")


# -----------------------------------------------------------------------------
# Database Setup & Operations
# -----------------------------------------------------------------------------
def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Create or open SQLite database with WAL mode for fast concurrent reads."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    # Enable WAL mode and synchronous=NORMAL to maximize performance and avoid locks with Datasette
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_database(conn: sqlite3.Connection) -> None:
    """Initialize table schema and analytical views for Datasette."""
    with conn:
        # Core data table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS electricity_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT (DATETIME('now')),
                current_power_w REAL NOT NULL,
                lifetime_energy_kwh REAL NOT NULL,
                calculated_cost REAL NOT NULL DEFAULT 0.0
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_electricity_usage_timestamp
            ON electricity_usage(timestamp);
            """
        )

        # Pre-built SQL View: Daily Costs
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS daily_costs AS
            SELECT
                STRFTIME('%Y-%m-%d', timestamp) AS day,
                ROUND(SUM(calculated_cost), 4) AS total_cost,
                ROUND(MAX(lifetime_energy_kwh) - MIN(lifetime_energy_kwh), 4) AS energy_kwh,
                ROUND(AVG(current_power_w), 1) AS avg_power_w,
                ROUND(MAX(current_power_w), 1) AS peak_power_w,
                COUNT(*) AS sample_count
            FROM electricity_usage
            GROUP BY STRFTIME('%Y-%m-%d', timestamp)
            ORDER BY day DESC;
            """
        )

        # Pre-built SQL View: Monthly Costs (Parent Reimbursement Dashboard)
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS monthly_costs AS
            SELECT
                STRFTIME('%Y-%m', timestamp) AS month,
                ROUND(SUM(calculated_cost), 2) AS total_cost,
                ROUND(MAX(lifetime_energy_kwh) - MIN(lifetime_energy_kwh), 2) AS total_energy_kwh,
                ROUND(AVG(current_power_w), 1) AS avg_power_w,
                ROUND(MAX(current_power_w), 1) AS peak_power_w,
                COUNT(*) AS sample_count
            FROM electricity_usage
            GROUP BY STRFTIME('%Y-%m', timestamp)
            ORDER BY month DESC;
            """
        )
    logger.info(f"Database schema verified at {DB_PATH}")


def get_last_lifetime_energy(conn: sqlite3.Connection) -> Optional[float]:
    """Retrieve the most recent lifetime_energy_kwh recorded in the database."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT lifetime_energy_kwh
        FROM electricity_usage
        ORDER BY id DESC
        LIMIT 1;
        """
    )
    row = cursor.fetchone()
    if row is not None and row[0] is not None:
        return float(row[0])
    return None


def record_usage(
    conn: sqlite3.Connection,
    power_w: float,
    lifetime_kwh: float,
    cost: float,
) -> None:
    """Insert a new measurement record into electricity_usage."""
    with conn:
        conn.execute(
            """
            INSERT INTO electricity_usage (timestamp, current_power_w, lifetime_energy_kwh, calculated_cost)
            VALUES (DATETIME('now'), ?, ?, ?);
            """,
            (round(power_w, 2), round(lifetime_kwh, 4), round(cost, 6)),
        )


# -----------------------------------------------------------------------------
# Payload Extraction & Normalization
# -----------------------------------------------------------------------------
def parse_z2m_message(raw_msg: str, target_device: str) -> Optional[Tuple[float, float]]:
    """
    Parse a WebSocket message emitted by Zigbee2MQTT.

    Returns (current_power_w, lifetime_energy_kwh) if relevant, otherwise None.
    Handles multiple Zigbee2MQTT frame structures:
      1. Standard MQTT bridge frame: {"topic": "zigbee2mqtt/...", "payload": {...}}
      2. Nested JSON string payload: {"topic": "zigbee2mqtt/...", "payload": "{...}"}
      3. Direct device state object: {"power": 45.2, "energy": 12.3}
    """
    try:
        data = json.loads(raw_msg)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    topic = data.get("topic", "")
    payload: Any = data.get("payload", data)

    # If payload is a serialized JSON string, decode it
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None

    if not isinstance(payload, dict):
        return None

    # Check device filter if topic is present
    if topic and target_device:
        # Topic is typically "zigbee2mqtt/<friendly_name>"
        topic_suffix = topic.split("/")[-1]
        if topic_suffix != target_device and topic != target_device:
            return None

    # Alternatively check friendly_name inside payload or data wrapper
    if target_device and "friendly_name" in payload:
        if payload["friendly_name"] != target_device:
            return None

    # Extract power (W) and energy (kWh)
    power_raw = payload.get("power")
    energy_raw = payload.get("energy")

    if power_raw is None or energy_raw is None:
        return None

    try:
        power = float(power_raw)
        energy = float(energy_raw)
        return (power, energy)
    except (ValueError, TypeError):
        return None


# -----------------------------------------------------------------------------
# Core Monitor Daemon
# -----------------------------------------------------------------------------
class PowerMonitor:
    def __init__(self, db_path: str, ws_url: str, cost_per_kwh: float, target_device: str):
        self.db_path = db_path
        self.ws_url = ws_url
        self.cost_per_kwh = cost_per_kwh
        self.target_device = target_device
        self.running = True
        self.conn: Optional[sqlite3.Connection] = None

        # Tracking state
        self.last_logged_power: Optional[float] = None
        self.last_logged_energy: Optional[float] = None
        self.last_logged_time: float = 0.0

    def start(self) -> None:
        """Initialize database connection and launch async event loop."""
        logger.info("Starting Power & Electricity Cost Tracker")
        logger.info(f"Target Device: '{self.target_device or 'ANY'}'")
        logger.info(f"Cost per kWh: {self.cost_per_kwh:.4f}")
        logger.info(f"Database Path: {self.db_path}")
        logger.info(f"WebSocket URL: {self.ws_url}")

        self.conn = get_db_connection(self.db_path)
        init_database(self.conn)

        # Baseline check on startup
        last_recorded = get_last_lifetime_energy(self.conn)
        if last_recorded is not None:
            self.last_logged_energy = last_recorded
            logger.info(f"Existing cumulative baseline loaded: {last_recorded:.4f} kWh")
        else:
            logger.info("No prior usage data found; waiting for first reading to set baseline.")

    def stop(self) -> None:
        """Gracefully close database connection and terminate loop."""
        self.running = False
        if self.conn:
            try:
                self.conn.close()
                logger.info("Database connection cleanly closed.")
            except Exception as e:
                logger.warning(f"Error closing DB: {e}")

    def process_telemetry(self, power_w: float, lifetime_kwh: float) -> None:
        """
        Apply offline delta math, calculate cost, and persist according to deadbands.
        """
        if self.conn is None:
            return

        now = time.time()
        prev_energy = get_last_lifetime_energy(self.conn)

        # 1. Offline & Reboot Delta Calculation
        if prev_energy is None:
            # First reading ever in the system: baseline row with 0 cost
            delta_kwh = 0.0
            cost = 0.0
            logger.info(
                f"Initial reading baseline recorded: {power_w:.1f}W | {lifetime_kwh:.4f} kWh | Cost: $0.00"
            )
            record_usage(self.conn, power_w, lifetime_kwh, cost)
            self.last_logged_power = power_w
            self.last_logged_energy = lifetime_kwh
            self.last_logged_time = now
            return

        # Subsequent readings: calculate delta from the last recorded value in SQLite
        hardware_reset = False
        if lifetime_kwh >= prev_energy:
            delta_kwh = lifetime_kwh - prev_energy
        else:
            # Meter rollover, plug swap, or firmware reset
            logger.warning(
                f"Lifetime energy decreased: received {lifetime_kwh:.4f} kWh < stored {prev_energy:.4f} kWh. "
                "Assuming hardware reset; delta set to 0.0 and updating baseline."
            )
            delta_kwh = 0.0
            hardware_reset = True

        cost = delta_kwh * self.cost_per_kwh

        # 2. Storage Throttling / Deadband Decision
        # Condition A: Cumulative energy increased (accumulated kWh & cost MUST be stored)
        energy_changed = delta_kwh > 0.0001

        # Condition B: Power load shifted significantly (>= MIN_POWER_CHANGE_W)
        power_changed = (
            self.last_logged_power is None
            or abs(power_w - self.last_logged_power) >= MIN_POWER_CHANGE_W
        )

        # Condition C: Maximum time interval elapsed (heartbeat write)
        time_elapsed = (now - self.last_logged_time) >= MAX_INTERVAL_SECONDS

        if energy_changed or power_changed or time_elapsed or hardware_reset:
            record_usage(self.conn, power_w, lifetime_kwh, cost)

            if energy_changed:
                logger.info(
                    f"Energy delta recorded: +{delta_kwh:.4f} kWh | Load: {power_w:.1f}W | "
                    f"Incremental Cost: ${cost:.4f} | Total: {lifetime_kwh:.4f} kWh"
                )
            else:
                logger.debug(
                    f"Periodic/Load sample: {power_w:.1f}W | {lifetime_kwh:.4f} kWh | Cost: $0.00"
                )

            self.last_logged_power = power_w
            self.last_logged_energy = lifetime_kwh
            self.last_logged_time = now

    async def run(self) -> None:
        """Main async loop with robust WebSocket connection & auto-reconnect."""
        if websockets is None:
            logger.error("The 'websockets' library is required. Install via: pip install websockets")
            return
        self.start()
        retry_delay = 2.0
        max_retry_delay = 30.0

        while self.running:
            try:
                logger.info(f"Connecting to Zigbee2MQTT WebSocket at {self.ws_url}...")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    logger.info("Successfully connected to Zigbee2MQTT event stream.")
                    retry_delay = 2.0  # Reset backoff on successful connect

                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except asyncio.TimeoutError:
                            # Send a ping to verify connection liveness
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10.0)
                            continue

                        parsed = parse_z2m_message(str(msg), self.target_device)
                        if parsed is not None:
                            power_w, lifetime_kwh = parsed
                            self.process_telemetry(power_w, lifetime_kwh)

            except asyncio.CancelledError:
                logger.info("Async loop cancelled.")
                break
            except Exception as e:
                if not self.running:
                    break
                logger.warning(
                    f"WebSocket disconnected or failed ({e}). Reconnecting in {retry_delay:.1f}s..."
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)

        self.stop()


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
def main() -> None:
    monitor = PowerMonitor(
        db_path=DB_PATH,
        ws_url=Z2M_WS_URL,
        cost_per_kwh=COST_PER_KWH,
        target_device=DEVICE_FRIENDLY_NAME,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown_handler(signum, frame):
        logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
        monitor.running = False
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        loop.run_until_complete(monitor.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.close()
        logger.info("Monitor service stopped cleanly.")


if __name__ == "__main__":
    main()
