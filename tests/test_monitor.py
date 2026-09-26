#!/usr/bin/env python3
"""
=============================================================================
Unit Tests for Proxmox Power & Electricity Cost Tracker
=============================================================================
Tests WebSocket message parsing, database schema, offline delta math,
deadband filtering, and SQL aggregation views.
=============================================================================
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

# Import monitor modules
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from monitor import (
    parse_z2m_message,
    get_db_connection,
    init_database,
    get_last_lifetime_energy,
    record_usage,
    PowerMonitor,
)


class TestMessageParser(unittest.TestCase):
    def test_parse_valid_dict_payload(self):
        msg = json.dumps({
            "topic": "zigbee2mqtt/proxmox_power_plug",
            "payload": {
                "power": 42.5,
                "energy": 12.345,
                "voltage": 230.0,
                "current": 0.18,
                "state": "ON"
            }
        })
        res = parse_z2m_message(msg, "proxmox_power_plug")
        self.assertIsNotNone(res)
        power, energy = res
        self.assertEqual(power, 42.5)
        self.assertEqual(energy, 12.345)

    def test_parse_serialized_json_string_payload(self):
        inner = json.dumps({"power": 55.0, "energy": 20.0})
        msg = json.dumps({
            "topic": "zigbee2mqtt/proxmox_power_plug",
            "payload": inner
        })
        res = parse_z2m_message(msg, "proxmox_power_plug")
        self.assertIsNotNone(res)
        power, energy = res
        self.assertEqual(power, 55.0)
        self.assertEqual(energy, 20.0)

    def test_parse_direct_payload(self):
        msg = json.dumps({"power": 60.1, "energy": 33.2})
        # If target device is empty or matches payload
        res = parse_z2m_message(msg, "")
        self.assertIsNotNone(res)
        power, energy = res
        self.assertEqual(power, 60.1)
        self.assertEqual(energy, 33.2)

    def test_device_filter_rejection(self):
        msg = json.dumps({
            "topic": "zigbee2mqtt/living_room_lamp",
            "payload": {"power": 10.0, "energy": 5.0}
        })
        res = parse_z2m_message(msg, "proxmox_power_plug")
        self.assertIsNone(res)

    def test_missing_power_or_energy_ignored(self):
        msg = json.dumps({
            "topic": "zigbee2mqtt/proxmox_power_plug",
            "payload": {"state": "ON", "voltage": 230}
        })
        res = parse_z2m_message(msg, "proxmox_power_plug")
        self.assertIsNone(res)

    def test_malformed_json_ignored(self):
        res = parse_z2m_message("not-json", "proxmox_power_plug")
        self.assertIsNone(res)


class TestDatabaseAndOfflineMath(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_energy.db")
        self.conn = get_db_connection(self.db_path)
        init_database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_initial_baseline_reading(self):
        monitor = PowerMonitor(
            db_path=self.db_path,
            ws_url="ws://localhost:8080/api",
            cost_per_kwh=0.25,
            target_device="proxmox_power_plug"
        )
        monitor.conn = self.conn

        # First reading: 50.0W, 100.0 kWh
        monitor.process_telemetry(power_w=50.0, lifetime_kwh=100.0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT current_power_w, lifetime_energy_kwh, calculated_cost FROM electricity_usage")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 50.0)
        self.assertEqual(rows[0][1], 100.0)
        # First reading has 0 cost (baseline)
        self.assertEqual(rows[0][2], 0.0)

    def test_subsequent_reading_cost_calculation(self):
        monitor = PowerMonitor(
            db_path=self.db_path,
            ws_url="ws://localhost:8080/api",
            cost_per_kwh=0.25,
            target_device="proxmox_power_plug"
        )
        monitor.conn = self.conn

        # Baseline
        monitor.process_telemetry(power_w=50.0, lifetime_kwh=100.0)
        # Advance energy by 2.0 kWh
        monitor.process_telemetry(power_w=52.0, lifetime_kwh=102.0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT lifetime_energy_kwh, calculated_cost FROM electricity_usage ORDER BY id ASC")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 2)
        # Delta = 102.0 - 100.0 = 2.0 kWh
        # Cost = 2.0 * 0.25 = 0.50
        self.assertEqual(rows[1][0], 102.0)
        self.assertAlmostEqual(rows[1][1], 0.50, places=4)

    def test_offline_gap_calculation(self):
        """
        Simulate the monitor being shut down for 3 days while Proxmox runs.
        The plug accumulates 15.0 kWh while offline.
        When monitor comes back online, the full delta must be recorded.
        """
        # Seed an old row from 3 days ago
        old_time = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            self.conn.execute(
                "INSERT INTO electricity_usage (timestamp, current_power_w, lifetime_energy_kwh, calculated_cost) "
                "VALUES (?, 45.0, 50.0, 0.0)",
                (old_time,)
            )

        monitor = PowerMonitor(
            db_path=self.db_path,
            ws_url="ws://localhost:8080/api",
            cost_per_kwh=0.30,
            target_device="proxmox_power_plug"
        )
        monitor.conn = self.conn

        # Monitor boots up now, plug reports 65.0 kWh (50 + 15 kWh while offline)
        monitor.process_telemetry(power_w=48.0, lifetime_kwh=65.0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT lifetime_energy_kwh, calculated_cost FROM electricity_usage ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        self.assertEqual(row[0], 65.0)
        # Delta = 65.0 - 50.0 = 15.0 kWh. Cost = 15.0 * 0.30 = $4.50
        self.assertAlmostEqual(row[1], 4.50, places=4)

    def test_energy_rollback_reset_protection(self):
        """
        If the smart plug is reset or swapped, lifetime energy might drop from 100 to 5.
        The system must not generate negative costs.
        """
        monitor = PowerMonitor(
            db_path=self.db_path,
            ws_url="ws://localhost:8080/api",
            cost_per_kwh=0.25,
            target_device="proxmox_power_plug"
        )
        monitor.conn = self.conn

        monitor.process_telemetry(power_w=50.0, lifetime_kwh=100.0)
        # Sudden drop in counter
        monitor.process_telemetry(power_w=50.0, lifetime_kwh=5.0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT lifetime_energy_kwh, calculated_cost FROM electricity_usage ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        self.assertEqual(row[0], 5.0)
        self.assertEqual(row[1], 0.0)  # Cost must be 0, not negative!


class TestSqlViews(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_views.db")
        self.conn = get_db_connection(self.db_path)
        init_database(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_daily_and_monthly_views(self):
        # Insert 3 days of readings
        day1 = "2026-09-01 12:00:00"
        day2 = "2026-09-02 12:00:00"
        day3 = "2026-09-03 12:00:00"

        with self.conn:
            self.conn.execute("INSERT INTO electricity_usage VALUES (1, ?, 40.0, 10.0, 0.0)", (day1,))
            self.conn.execute("INSERT INTO electricity_usage VALUES (2, ?, 50.0, 12.0, 0.50)", (day1,))
            self.conn.execute("INSERT INTO electricity_usage VALUES (3, ?, 60.0, 15.0, 0.75)", (day2,))
            self.conn.execute("INSERT INTO electricity_usage VALUES (4, ?, 55.0, 18.0, 0.75)", (day3,))

        cursor = self.conn.cursor()

        # Check daily_costs view
        cursor.execute("SELECT day, total_cost, energy_kwh FROM daily_costs ORDER BY day ASC")
        days = cursor.fetchall()
        self.assertEqual(len(days), 3)
        # Day 1: total cost 0.50
        self.assertEqual(days[0][0], "2026-09-01")
        self.assertAlmostEqual(days[0][1], 0.50, places=2)
        # Day 2: total cost 0.75
        self.assertEqual(days[1][0], "2026-09-02")
        self.assertAlmostEqual(days[1][1], 0.75, places=2)

        # Check monthly_costs view
        cursor.execute("SELECT month, total_cost FROM monthly_costs")
        months = cursor.fetchall()
        self.assertEqual(len(months), 1)
        self.assertEqual(months[0][0], "2026-09")
        # Total month cost = 0.50 + 0.75 + 0.75 = 2.00
        self.assertAlmostEqual(months[0][1], 2.00, places=2)


if __name__ == "__main__":
    unittest.main()
