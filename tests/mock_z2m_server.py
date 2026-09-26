#!/usr/bin/env python3
"""
=============================================================================
Zigbee2MQTT Mock WebSocket Server (for testing & development)
=============================================================================
Simulates Zigbee2MQTT's event WebSocket at ws://localhost:8080/api.
Emits realistic IKEA INSPELNING smart plug telemetry (power, energy, voltage,
current) without requiring physical hardware.
=============================================================================
"""

import asyncio
import json
import random
import sys
import time

try:
    import websockets
except ImportError:
    print("websockets library required: pip install websockets", file=sys.stderr)
    sys.exit(1)

HOST = "0.0.0.0"
PORT = 8080
DEVICE_NAME = "proxmox_power_plug"


async def handler(websocket):
    print(f"[Mock Z2M] Client connected: {websocket.remote_address}")

    # Simulated starting baseline
    lifetime_energy = 145.250
    base_watts = 48.0

    try:
        while True:
            # Fluctuate power load realistically (40W to 85W)
            power_fluctuation = random.uniform(-3.0, 5.0)
            current_power = max(35.0, round(base_watts + power_fluctuation, 1))

            # Accumulate energy over a 3-second interval
            interval_hours = 3.0 / 3600.0
            energy_delta = (current_power / 1000.0) * interval_hours
            lifetime_energy += energy_delta

            # Voltage & Current
            voltage = round(random.uniform(229.0, 232.0), 1)
            current_amps = round(current_power / voltage, 2)

            payload = {
                "power": current_power,
                "energy": round(lifetime_energy, 4),
                "voltage": voltage,
                "current": current_amps,
                "state": "ON",
                "friendly_name": DEVICE_NAME,
            }

            # Wrap in standard Zigbee2MQTT WebSocket frame
            frame = {
                "topic": f"zigbee2mqtt/{DEVICE_NAME}",
                "payload": payload,
            }

            await websocket.send(json.dumps(frame))
            print(
                f"[Mock Z2M] Sent: {current_power}W | {lifetime_energy:.4f} kWh | {voltage}V"
            )

            await asyncio.sleep(3.0)

    except websockets.exceptions.ConnectionClosed:
        print(f"[Mock Z2M] Client disconnected: {websocket.remote_address}")


async def main():
    print(f"Starting Mock Zigbee2MQTT WebSocket Server on ws://{HOST}:{PORT}/api")
    print("Press Ctrl+C to stop.")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMock server stopped.")
