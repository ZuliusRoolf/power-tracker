# Proxmox Host Power & Electricity Cost Tracker

An ultra-lightweight, 24/7 power monitoring and cost calculation stack designed to run inside a resource-constrained Proxmox LXC container (1 CPU core, 512MB RAM).

---

### Project Intention: Paying Parents the Electric Bill

When hosting a home server (Proxmox VE) at your parents' house, determining fair reimbursement for electricity can be difficult. This project provides automated, verifiable, and tamper-resistant tracking of the server's exact power usage.

At the end of every month, you and your parents can open a clean web dashboard showing:
1. **Total Kilowatt-Hours (kWh)** consumed by the Proxmox machine.
2. **Exact Dollar Amount Owed** based on the household utility rate (e.g., `$0.25/kWh`).
3. **Daily and Monthly Charts** showing power fluctuations and idle vs. peak load.

---

## Architecture Overview

```
 [Wall Outlet]
       │
       ▼
 [IKEA INSPELNING Smart Plug] ────(Powers Proxmox Machine)────► [Proxmox VE Host]
       │
       │ Zigbee 3.0 Wireless Telemetry (Power in W, Cumulative Energy in kWh)
       ▼
 [SONOFF Zigbee 3.0 USB Dongle Plus]
       │
       │ Proxmox USB Passthrough (/dev/serial/by-id/...)
       ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      Proxmox LXC Container                             │
 │                  (Debian Slim / Alpine Linux, 512MB RAM)               │
 │                                                                        │
 │  ┌───────────────────────┐                                             │
 │  │      Mosquitto        │  Local MQTT broker (<2MB RAM)               │
 │  └──────────┬────────────┘                                             │
 │             │                                                          │
 │  ┌──────────▼────────────┐                                             │
 │  │      Zigbee2MQTT      │  Serial communication + Failsafe converter  │
 │  └──────────┬────────────┘  (Frontend WebSocket on :8080/api)          │
 │             │                                                          │
 │             │ ws://localhost:8080/api                                  │
 │             │                                                          │
 │  ┌──────────▼────────────┐                                             │
 │  │       monitor.py      │  Python daemon (<20MB RAM)                  │
 │  │                       │  • Offline-resilient cumulative delta math  │
 │  │                       │  • Calculates cost = delta * COST_PER_KWH   │
 │  └──────────┬────────────┘                                             │
 │             │                                                          │
 │             ▼ writes via SQLite WAL mode                               │
 │     /data/energy_monitor.db                                            │
 │             ▲                                                          │
 │             │ reads SQL Views (daily_costs, monthly_costs)             │
 │  ┌──────────┴────────────┐                                             │
 │  │       Datasette       │  Served on 0.0.0.0:8001                     │
 │  │   + datasette-plot    │  Interactive visual charts over Tailscale   │
 │  └───────────────────────┘                                             │
 └────────────────────────────────────────────────────────────────────────┘
       ▲
       │ Accessible via secure Tailscale tunnel / local LAN
 [Client Browser / Phone]
```

---

## ⚠️ Critical Rule & Failsafe Architecture

> **PRIMARY DIRECTIVE:** The IKEA INSPELNING plug supplies mains electrical power to the physical Proxmox VE host itself. It must **NEVER** receive a turn-off command (`state: OFF` or `state: TOGGLE`). An accidental power-off cuts power to the host, terminating the LXC container, all hosted VMs, and risking storage corruption.

This project implements a multi-layer defense against power cuts:

### 1. Zigbee Command Stripping (`inspelning-failsafe.js`)
In `zigbee2mqtt/inspelning-failsafe.js`, the standard `tz.on_off` converter is intercepted:
- Any payload containing `state: "OFF"`, `state: "TOGGLE"`, or `0` triggers a critical runtime exception and is dropped **before** any Zigbee packet is transmitted over the air.
- Only explicit `ON` commands are permitted.

### 2. Complete UI Toggle Removal
In the Zigbee2MQTT Frontend web interface:
- The `state` attribute is exposed with read-only access (`ea.STATE`) instead of read-write (`ea.STATE_SET`).
- The Z2M Web UI **does not render a toggle switch button** for this device. A misclick or tap on mobile cannot cut power to the host.

### 3. Automatic Power Recovery (`power_on_behavior: on`)
- If household mains power blips or trips the breaker, the plug's internal firmware memory is configured to automatically energize its relay as soon as power returns.

### 4. Physical Switch Protection
- **Recommendation:** Place a strip of electrical tape or a small 3D-printed clip over the physical push-button on the side of the IKEA INSPELNING plug to prevent anyone from accidentally pressing it while cleaning or moving cables.

---

## Hardware Requirements

| Component | Description | Notes |
| :--- | :--- | :--- |
| **Smart Plug** | IKEA INSPELNING (E2206) | Zigbee 3.0 smart plug with active power (W) & cumulative energy (kWh) |
| **Zigbee Adapter** | SONOFF Zigbee 3.0 USB Dongle Plus | Model "P" (CC2652P) or Model "E" (EFR32MG21) |
| **Host** | Proxmox VE Server | Supplies power through the IKEA plug |
| **LXC Container** | Debian 12 Slim or Alpine Linux 3.19+ | 1 CPU core, 512MB RAM, 4GB disk |

---

## Proxmox Host Configuration (USB Passthrough)

To allow the LXC container to communicate with the SONOFF USB Dongle:

### Method A: Proxmox VE 8+ GUI (Recommended & Easiest)
Proxmox VE 8 introduced native Device Passthrough right in the web interface:
1. Open the Proxmox VE Web GUI.
2. Select your LXC container -> **Resources**.
3. Click **Add** -> **Device Passthrough**.
4. In **Device Path**, enter the by-id serial path:
   `/dev/serial/by-id/usb-SONOFF_SONOFF_Dongle_Lite_MG21_308af7379da0ef11a1dfaaa361ce3355-if00-port0`
5. Click **Add** and restart the container (`pct restart <CONTAINER_ID>`).
That's it! Proxmox automatically handles cgroup permissions and device node mounting for you.

---

### Method B: Manual Configuration (Proxmox VE 7 or CLI)
If configuring via the Proxmox host CLI or using older versions:
1. On the Proxmox VE host shell, find the dongle:
   ```bash
   ls -l /dev/serial/by-id/
   ```
2. Check device major/minor numbers:
   ```bash
   ls -l /dev/ttyACM0   # typically c 166:0 (Dongle-E / MG21)
   # or
   ls -l /dev/ttyUSB0   # typically c 188:0 (Dongle-P)
   ```
3. Edit the LXC configuration on Proxmox host (`/etc/pve/lxc/<CONTAINER_ID>.conf`):
   ```ini
   # For Dongle-E / MG21 (c 166:*):
   lxc.cgroup2.devices.allow: c 166:* rwm
   lxc.mount.entry: /dev/serial/by-id/usb-SONOFF_SONOFF_Dongle_Lite_MG21_308af7379da0ef11a1dfaaa361ce3355-if00-port0 dev/serial/by-id/usb-SONOFF_SONOFF_Dongle_Lite_MG21_308af7379da0ef11a1dfaaa361ce3355-if00-port0 none bind,optional,create=file
   ```
4. Restart the container:
   ```bash
   pct restart <CONTAINER_ID>
   ```

---

## Quick Start / Automated Installation

Run the turnkey `install.sh` script inside your LXC container. It automatically:
- Detects the OS (Debian Slim or Alpine Linux) and init system (`systemd` or `openrc`).
- Scans for the SONOFF USB dongle.
- Installs minimal system packages (`mosquitto`, `sqlite3`, `python3-venv`).
- Sets up a dedicated Python virtualenv (`/opt/power-tracker/venv`).
- Installs `websockets`, `datasette`, and `datasette-plot`.
- Initializes `/data/energy_monitor.db` with schema and pre-built analytical views.
- Deploys the failsafe converter and registers auto-starting background services.

```bash
# Clone the repository
git clone https://github.com/your-username/power-tracker.git /opt/power-tracker
cd /opt/power-tracker

# Run the installer
chmod +x install.sh
sudo ./install.sh
```

---

## Configuration (`/etc/power-monitor/config.env`)

Edit `/etc/power-monitor/config.env` to match your local setup:

```ini
# Cost per kWh from your parents' electric bill (e.g., $0.25)
COST_PER_KWH=0.25

# Path to SQLite database
DB_PATH=/data/energy_monitor.db

# Zigbee2MQTT Event WebSocket
Z2M_WS_URL=ws://localhost:8080/api

# Friendly name assigned to IKEA INSPELNING in Zigbee2MQTT
DEVICE_FRIENDLY_NAME=proxmox_power_plug

# Power deadband (in Watts) to throttle redundant writes
MIN_POWER_CHANGE_W=1.0

# Heartbeat record interval (in seconds)
MAX_INTERVAL_SECONDS=60

# Log level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO
```

---

## Offline & Reboot Resilience Math

The IKEA INSPELNING hardware maintains a non-volatile hardware register for **lifetime cumulative energy** in kilowatt-hours (kWh).

### How the Math Works:
1. When `monitor.py` starts, it queries SQLite for the last recorded `lifetime_energy_kwh`.
2. When the plug reports cumulative energy ($E_{curr}$):
   - **Initial Run:** Stored as baseline ($E_{prev} = E_{curr}$), `cost = $0.00`.
   - **Normal Operation:** $\Delta E = E_{curr} - E_{prev}$, $\text{cost} = \Delta E \times \text{COST\_PER\_KWH}$.
   - **Container / Daemon Offline Gap:** If Proxmox reboots or `monitor.py` is stopped for 3 days while the server continues running, the plug continues accumulating energy. When `monitor.py` restarts, it calculates the delta across the entire offline window. **Zero energy or cost is lost.**
   - **Hardware Reset Guard:** If the plug is factory reset or swapped ($E_{curr} < E_{prev}$), the script logs a warning, establishes a new baseline, and prevents negative billing.

---

## Database Schema & Pre-Built Views

The SQLite database at `/data/energy_monitor.db` uses WAL mode (`PRAGMA journal_mode=WAL`) to allow concurrent reads from Datasette while `monitor.py` writes.

### Table: `electricity_usage`
| Column | Type | Description |
| :--- | :--- | :--- |
| `timestamp` | DATETIME | Reading time (`CURRENT_TIMESTAMP`) |
| `current_power_w` | REAL | Instantaneous power draw in Watts |
| `lifetime_energy_kwh` | REAL | Cumulative hardware meter reading in kWh |
| `calculated_cost` | REAL | Financial cost incurred since previous reading |

### Pre-Built SQL Views
1. **`daily_costs`**:
   Aggregates total cost, total energy used, average power draw, and peak power draw for each calendar day:
   ```sql
   SELECT day, total_cost, energy_kwh, avg_power_w, peak_power_w FROM daily_costs;
   ```
2. **`monthly_costs`**:
   Aggregates totals per month for long-term tracking.
3. **`parents_bill_summary`**:
   Formatted billing report showing exact dollar amount owed for each monthly cycle:
   ```sql
   SELECT billing_month, amount_owed_dollars, kwh_consumed, avg_load_watts FROM parents_bill_summary;
   ```

---

## Datasette Web UI & Graphing

Datasette serves the SQLite database on port `8001` over HTTP, bound to `0.0.0.0`.

### Tailscale Remote Access
Because the LXC container connects to your secure Tailscale network:
- Open your browser to: `http://<tailscale-ip>:8001`
- Or via local network: `http://<lxc-ip>:8001`

### Automatic Graphing with `datasette-plot`
When you click on the **`daily_costs`** or **`monthly_costs`** views, `datasette-plot` automatically renders an interactive bar chart of your electricity usage and financial costs.

---

## Service Management

### Debian / Ubuntu (systemd)
```bash
# Check status
systemctl status power-monitor.service
systemctl status power-datasette.service

# View live logs
journalctl -u power-monitor.service -f

# Restart services
sudo systemctl restart power-monitor.service
```

### Alpine Linux (OpenRC)
```bash
# Check status
rc-service power-monitor status
rc-service power-datasette status

# View logs
tail -f /var/log/power-monitor.log

# Restart services
rc-service power-monitor restart
```

---

## Testing & Verification (Without Hardware)

You can verify the entire pipeline before your physical plug and dongle arrive:

### 1. Run Unit Tests
```bash
python3 -m unittest discover tests
```

### 2. Seed Synthetic Data
Generate 14 days of realistic Proxmox server power draw:
```bash
python3 init_db.py --seed-sample --days 14
```

### 3. Launch Mock Zigbee2MQTT Server
In one terminal, start the mock WebSocket server:
```bash
python3 tests/mock_z2m_server.py
```
In another terminal, run `monitor.py`:
```bash
python3 monitor.py
```
Watch real-time power fluctuations and energy deltas stream into `/data/energy_monitor.db`!

---

## License

MIT License. Designed for home labbers everywhere sharing power with family.
