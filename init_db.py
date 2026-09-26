#!/usr/bin/env python3
"""
=============================================================================
Database Initialization & Maintenance Tool (init_db.py)
=============================================================================
Initializes the SQLite database (/data/energy_monitor.db), creates tables,
indexes, and analytical views for Datasette.
Optionally generates sample telemetry for dashboard testing.
=============================================================================
"""

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def init_db(db_path: str) -> None:
    """Create directory, table schema, indexes, and views."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)

    # Enable WAL mode for high performance concurrent access with Datasette
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    with conn:
        # 1. Main Telemetry Table
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

        # 2. View: Daily Costs
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

        # 3. View: Monthly Costs (Parent Reimbursement Dashboard)
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

        # 4. View: Parents Bill Summary
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS parents_bill_summary AS
            SELECT
                STRFTIME('%Y-%m', timestamp) AS billing_month,
                ROUND(SUM(calculated_cost), 2) AS amount_owed_dollars,
                ROUND(MAX(lifetime_energy_kwh) - MIN(lifetime_energy_kwh), 2) AS kwh_consumed,
                ROUND(AVG(current_power_w), 1) AS avg_load_watts,
                MIN(DATE(timestamp)) AS billing_start,
                MAX(DATE(timestamp)) AS billing_end
            FROM electricity_usage
            GROUP BY STRFTIME('%Y-%m', timestamp)
            ORDER BY billing_month DESC;
            """
        )

    conn.close()
    print(f"Database successfully initialized at: {db_path}")


def seed_sample_data(db_path: str, days: int = 14, cost_per_kwh: float = 0.25) -> None:
    """Populate database with synthetic historical data for UI preview."""
    conn = sqlite3.connect(db_path)
    now = datetime.now()
    start_time = now - timedelta(days=days)

    current_time = start_time
    lifetime_kwh = 100.0  # starting baseline
    rows = []

    # Baseline entry
    rows.append((current_time.strftime("%Y-%m-%d %H:%M:%S"), 45.0, lifetime_kwh, 0.0))

    while current_time < now:
        # Step forward 10 to 30 minutes
        minutes_step = random.randint(15, 30)
        current_time += timedelta(minutes=minutes_step)

        # Typical Proxmox home server load: 35W idle to 85W under load
        hour = current_time.hour
        base_watts = 40.0
        if 9 <= hour <= 23:
            # Active hours with VM workloads, backups, or streaming
            base_watts += random.uniform(10.0, 45.0)
        else:
            # Idle night hours
            base_watts += random.uniform(0.0, 8.0)

        hours_fraction = minutes_step / 60.0
        delta_kwh = (base_watts / 1000.0) * hours_fraction
        lifetime_kwh += delta_kwh
        cost = delta_kwh * cost_per_kwh

        rows.append(
            (
                current_time.strftime("%Y-%m-%d %H:%M:%S"),
                round(base_watts, 1),
                round(lifetime_kwh, 4),
                round(cost, 6),
            )
        )

    with conn:
        conn.executemany(
            """
            INSERT INTO electricity_usage (timestamp, current_power_w, lifetime_energy_kwh, calculated_cost)
            VALUES (?, ?, ?, ?);
            """,
            rows,
        )

    conn.close()
    print(f"Seeded {len(rows)} sample data points spanning {days} days.")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize SQLite database for Proxmox Power Tracker"
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("DB_PATH", "/data/energy_monitor.db"),
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--seed-sample",
        action="store_true",
        help="Seed synthetic data for testing UI before hardware arrives",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days of sample data to generate (default: 14)",
    )
    parser.add_argument(
        "--cost-per-kwh",
        type=float,
        default=float(os.getenv("COST_PER_KWH", "0.25")),
        help="Cost per kWh for sample data (default: 0.25)",
    )

    args = parser.parse_args()
    init_db(args.db_path)

    if args.seed_sample:
        seed_sample_data(args.db_path, days=args.days, cost_per_kwh=args.cost_per_kwh)


if __name__ == "__main__":
    main()
