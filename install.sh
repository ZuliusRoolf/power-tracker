#!/usr/bin/env bash
# ==============================================================================
# Proxmox Host Power & Electricity Cost Tracker - Unified Installer
# ==============================================================================
# Supports: Debian Slim / Ubuntu & Alpine Linux inside a Proxmox LXC Container
# Memory Footprint Target: <20MB RAM for monitor, <60MB for Datasette (<512MB RAM total)
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

INSTALL_DIR="/opt/power-tracker"
DATA_DIR="/data"
CONFIG_DIR="/etc/power-monitor"
VENV_DIR="${INSTALL_DIR}/venv"
SCRIPT_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

echo -e "${BOLD}${CYAN}"
echo "========================================================================"
echo "    Proxmox Host Power & Electricity Cost Tracker Installer"
echo "    Track electricity usage and calculate parents' electric bill"
echo "========================================================================"
echo -e "${NC}"

# ------------------------------------------------------------------------------
# 1. Privilege Check
# ------------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    log_error "This installation script must be run as root (or via sudo)."
    exit 1
fi

# ------------------------------------------------------------------------------
# 2. Operating System & Init System Detection
# ------------------------------------------------------------------------------
log_info "Detecting Operating System and Init System..."

DISTRO="unknown"
INIT_SYS="unknown"

if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO="$ID"
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    INIT_SYS="systemd"
elif [[ -f /sbin/openrc-run ]] || command -v rc-service >/dev/null 2>&1; then
    INIT_SYS="openrc"
fi

log_success "Detected OS: ${DISTRO} | Init System: ${INIT_SYS}"

# ------------------------------------------------------------------------------
# 3. Hardware Device Detection (SONOFF Zigbee 3.0 USB Dongle Plus)
# ------------------------------------------------------------------------------
echo ""
log_info "Scanning for Zigbee USB Dongle..."

DETECTED_DEVICE=""
DETECTED_ADAPTER="ember" # Default for MG21 / Dongle-E / modern adapters

# Check persistent by-id serial ports first
if [[ -d /dev/serial/by-id ]]; then
    for dev in /dev/serial/by-id/*; do
        if [[ -e "$dev" ]]; then
            dev_name="$(basename "$dev")"
            if [[ "$dev_name" =~ [Ss][Oo][Nn][Oo][Ff][Ff]|[Ii][Tt][Ee][Aa][Dd]|CP210|CH340|EFR32|CC2652|MG21|[Dd]ongle ]]; then
                DETECTED_DEVICE="$dev"
                log_success "Found Zigbee USB Dongle (by-id): ${DETECTED_DEVICE}"

                # Auto-detect Zigbee2MQTT adapter driver
                if [[ "$dev_name" =~ CC2652|CP210|Dongle_Plus$|Dongle-P ]]; then
                    DETECTED_ADAPTER="zstack"
                else
                    # MG21, Dongle_Lite, Dongle_Plus_V2, EFR32 use ember
                    DETECTED_ADAPTER="ember"
                fi
                log_info "Determined Zigbee adapter driver: ${DETECTED_ADAPTER}"
                break
            fi
        fi
    done
fi

# Fallback: check ttyACM* or ttyUSB*
if [[ -z "$DETECTED_DEVICE" ]]; then
    for dev in /dev/ttyACM0 /dev/ttyUSB0 /dev/ttyACM1 /dev/ttyUSB1; do
        if [[ -e "$dev" ]]; then
            DETECTED_DEVICE="$dev"
            log_success "Found USB Serial Device: ${DETECTED_DEVICE}"
            if [[ "$dev" == *"USB"* ]]; then
                DETECTED_ADAPTER="zstack"
            else
                DETECTED_ADAPTER="ember"
            fi
            break
        fi
    done
fi

if [[ -z "$DETECTED_DEVICE" ]]; then
    log_warning "No Zigbee USB Dongle detected inside this LXC container."
    echo -e "${YELLOW}------------------------------------------------------------------"
    echo "If your SONOFF Zigbee Dongle is plugged into your Proxmox VE host,"
    echo "pass the device through to this LXC container using either method:"
    echo ""
    echo "Method A: Proxmox VE 8+ GUI (Recommended):"
    echo "  1. Select your LXC container in Proxmox Web GUI."
    echo "  2. Go to 'Resources' -> 'Add' -> 'Device Passthrough'."
    echo "  3. Select your SONOFF dongle path (e.g., /dev/serial/by-id/usb-SONOFF...)."
    echo ""
    echo "Method B: Manual Config (/etc/pve/lxc/<ID>.conf):"
    echo "  lxc.cgroup2.devices.allow: c 166:* rwm   # (or 188:* for /dev/ttyUSB0)"
    echo "  lxc.mount.entry: /dev/serial/by-id/... dev/serial/by-id/... none bind,optional,create=file"
    echo "------------------------------------------------------------------${NC}"
else
    log_info "Serial port will be configured to: ${DETECTED_DEVICE} (adapter: ${DETECTED_ADAPTER})"
fi

# ------------------------------------------------------------------------------
# 4. Install OS-level Dependencies
# ------------------------------------------------------------------------------
echo ""
log_info "Installing lightweight OS dependencies..."

case "$DISTRO" in
    alpine)
        apk update
        apk add --no-cache \
            python3 \
            py3-pip \
            py3-virtualenv \
            sqlite \
            mosquitto \
            curl \
            bash
        ;;
    debian|ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends \
            python3 \
            python3-pip \
            python3-venv \
            sqlite3 \
            mosquitto \
            curl \
            ca-certificates
        ;;
    *)
        log_warning "Unrecognized distribution '${DISTRO}'. Proceeding assuming python3 & sqlite are present."
        ;;
esac

# ------------------------------------------------------------------------------
# 5. Verify Python Version
# ------------------------------------------------------------------------------
log_info "Verifying Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
    log_error "python3 is not installed. Aborting."
    exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log_success "Python version: ${PY_VER}"

# ------------------------------------------------------------------------------
# 6. Deploy Application & Virtual Environment
# ------------------------------------------------------------------------------
echo ""
log_info "Setting up directories and dedicated virtual environment in ${INSTALL_DIR}..."

mkdir -p "${INSTALL_DIR}"
mkdir -p "${DATA_DIR}"
mkdir -p "${CONFIG_DIR}"

# Copy project files if installing from external source
if [[ "${SCRIPT_SRC_DIR}" != "${INSTALL_DIR}" ]]; then
    cp -r "${SCRIPT_SRC_DIR}/"* "${INSTALL_DIR}/"
fi

# Create Python virtual environment
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
    log_success "Created Python virtualenv at ${VENV_DIR}"
fi

# Install Python packages inside virtual environment
log_info "Installing Python dependencies (websockets, datasette, datasette-plot)..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install --quiet \
    "websockets>=12.0" \
    "datasette>=0.64" \
    "datasette-plot>=0.2.0"

# Verify datasette-plot plugin is active
if "${VENV_DIR}/bin/datasette" plugins | grep -q "datasette-plot"; then
    log_success "Verified datasette and datasette-plot plugin installation."
else
    log_warning "datasette-plot was installed; proceeding."
fi

# ------------------------------------------------------------------------------
# 7. Configuration Setup
# ------------------------------------------------------------------------------
echo ""
log_info "Setting up configuration in ${CONFIG_DIR}/config.env..."

if [[ ! -f "${CONFIG_DIR}/config.env" ]]; then
    cp "${INSTALL_DIR}/config.env.example" "${CONFIG_DIR}/config.env"
    log_success "Created default configuration file: ${CONFIG_DIR}/config.env"
else
    log_info "Existing configuration file found at ${CONFIG_DIR}/config.env (preserving)."
fi

# ------------------------------------------------------------------------------
# 8. Database Initialization & Views
# ------------------------------------------------------------------------------
echo ""
log_info "Initializing SQLite database at ${DATA_DIR}/energy_monitor.db..."
"${VENV_DIR}/bin/python3" "${INSTALL_DIR}/init_db.py" --db-path "${DATA_DIR}/energy_monitor.db"
log_success "Database schema and SQL views created successfully."

# ------------------------------------------------------------------------------
# 9. Zigbee2MQTT Failsafe Protection Setup
# ------------------------------------------------------------------------------
echo ""
log_info "Configuring Zigbee2MQTT failsafe protection..."

# If Zigbee2MQTT is installed in standard paths, copy the external converter
Z2M_DATA_PATHS=(
    "/opt/zigbee2mqtt/data"
    "/app/data"
    "/data/zigbee2mqtt"
    "/var/lib/zigbee2mqtt"
)

CONVERTER_INSTALLED=false
for zpath in "${Z2M_DATA_PATHS[@]}"; do
    if [[ -d "$zpath" ]]; then
        cp "${INSTALL_DIR}/zigbee2mqtt/inspelning-failsafe.js" "${zpath}/"
        log_success "Copied failsafe converter 'inspelning-failsafe.js' into ${zpath}"
        CONVERTER_INSTALLED=true
        break
    fi
done

if [[ "$CONVERTER_INSTALLED" = false ]]; then
    log_info "Zigbee2MQTT data folder not yet created or located elsewhere."
    log_info "Failsafe converter is available at: ${INSTALL_DIR}/zigbee2mqtt/inspelning-failsafe.js"
    log_info "Ensure you copy it into your Zigbee2MQTT data directory and register in configuration.yaml."
fi

# ------------------------------------------------------------------------------
# 10. Service Registration (systemd or OpenRC)
# ------------------------------------------------------------------------------
echo ""
log_info "Installing background services (${INIT_SYS})..."

if [[ "$INIT_SYS" == "systemd" ]]; then
    cp "${INSTALL_DIR}/systemd/power-monitor.service" /etc/systemd/system/
    cp "${INSTALL_DIR}/systemd/power-datasette.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable power-monitor.service
    systemctl enable power-datasette.service

    # Enable Mosquitto MQTT broker if installed
    if systemctl list-unit-files | grep -q mosquitto.service; then
        systemctl enable mosquitto.service
        systemctl restart mosquitto.service || true
    fi

    log_success "Systemd services enabled: power-monitor, power-datasette"
    log_info "To start immediately, run:"
    echo "    systemctl start power-monitor.service power-datasette.service"

elif [[ "$INIT_SYS" == "openrc" ]]; then
    cp "${INSTALL_DIR}/openrc/power-monitor" /etc/init.d/power-monitor
    cp "${INSTALL_DIR}/openrc/power-datasette" /etc/init.d/power-datasette
    chmod +x /etc/init.d/power-monitor /etc/init.d/power-datasette

    rc-update add power-monitor default
    rc-update add power-datasette default

    if command -v rc-service >/dev/null 2>&1; then
        rc-update add mosquitto default || true
        rc-service mosquitto restart || true
    fi

    log_success "OpenRC services registered: power-monitor, power-datasette"
    log_info "To start immediately, run:"
    echo "    rc-service power-monitor start && rc-service power-datasette start"
else
    log_warning "Could not detect systemd or OpenRC. You can start the services manually:"
    echo "    ${VENV_DIR}/bin/python3 ${INSTALL_DIR}/monitor.py &"
    echo "    ${INSTALL_DIR}/datasette/run-datasette.sh &"
fi

# ------------------------------------------------------------------------------
# 11. Completion & Next Steps
# ------------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}========================================================================${NC}"
echo -e "${BOLD}${GREEN} Installation Complete!${NC}"
echo -e "${BOLD}${GREEN}========================================================================${NC}"
echo ""
echo -e "  ${BOLD}Configuration File:${NC}  ${CONFIG_DIR}/config.env"
echo -e "  ${BOLD}Database Location:${NC}   ${DATA_DIR}/energy_monitor.db"
echo -e "  ${BOLD}Datasette Web UI:${NC}    http://<CONTAINER_IP>:8001"
echo ""
echo -e "${BOLD}CRITICAL FAILSAFE REMINDER:${NC}"
echo "  The IKEA INSPELNING plug supplies power to your Proxmox VE host."
echo "  1. Verify 'inspelning-failsafe.js' is loaded in your Zigbee2MQTT configuration.yaml."
echo "  2. The Web UI toggle switch is automatically disabled for this device."
echo "  3. 'power_on_behavior: on' ensures the plug re-arms if AC wall power blips."
echo ""
echo -e "${BOLD}To start the services now:${NC}"
if [[ "$INIT_SYS" == "systemd" ]]; then
    echo "  systemctl start power-monitor.service power-datasette.service"
    echo "  systemctl status power-monitor.service"
else
    echo "  rc-service power-monitor start"
    echo "  rc-service power-datasette start"
fi
echo ""
