#!/usr/bin/env bash
# ==============================================================================
# Zigbee2MQTT Automated Installer for Proxmox LXC
# ==============================================================================
# Installs Node.js, clones Zigbee2MQTT to /opt/zigbee2mqtt, copies the
# failsafe configuration, and registers the system service.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
Z2M_DIR="/opt/zigbee2mqtt"
Z2M_DATA="${Z2M_DIR}/data"

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR] This script must be run as root (or via sudo).${NC}"
    exit 1
fi

echo -e "${BOLD}${BLUE}=== Installing Zigbee2MQTT & Dependencies ===${NC}"

# Detect OS
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

# 1. Install Node.js, npm, git
echo -e "${BLUE}[INFO] Installing Node.js, npm, git, and build tools...${NC}"
case "$DISTRO" in
    debian|ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends curl ca-certificates gnupg git make g++ gcc mosquitto

        # Zigbee2MQTT requires Node.js >= 20.x for ES Module support (srvx)
        NODE_MAJOR=$(node -v 2>/dev/null | cut -d'.' -f1 | tr -d 'v' || echo "0")
        if [[ "$NODE_MAJOR" -lt 20 ]]; then
            echo -e "${BLUE}[INFO] Upgrading Node.js to Node.js 20 LTS (required by modern Zigbee2MQTT)...${NC}"
            curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
            apt-get install -y --no-install-recommends nodejs
        fi
        ;;
    alpine)
        apk update
        apk add --no-cache nodejs npm git make g++ gcc mosquitto
        ;;
    *)
        echo -e "${YELLOW}[WARNING] Unrecognized distro. Ensure nodejs and git are installed.${NC}"
        ;;
esac

# 2. Check Node version
NODE_VER=$(node -v 2>/dev/null || echo "none")
echo -e "${GREEN}[SUCCESS] Node.js version: ${NODE_VER}${NC}"

# 3. Clone or update Zigbee2MQTT
if [[ ! -d "$Z2M_DIR" ]]; then
    echo -e "${BLUE}[INFO] Cloning Zigbee2MQTT into ${Z2M_DIR}...${NC}"
    git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git "$Z2M_DIR"
else
    echo -e "${BLUE}[INFO] Zigbee2MQTT already exists at ${Z2M_DIR}.${NC}"
fi

# 4. Enable pnpm and build dependencies
echo -e "${BLUE}[INFO] Enabling pnpm (Zigbee2MQTT's official package manager)...${NC}"
corepack enable 2>/dev/null || npm install -g pnpm

echo -e "${BLUE}[INFO] Installing Zigbee2MQTT packages via pnpm (this may take 1-2 minutes)...${NC}"
cd "$Z2M_DIR"
if command -v pnpm >/dev/null 2>&1; then
    pnpm install --frozen-lockfile || pnpm install
else
    npm install --no-audit --no-fund
fi

# 5. Copy configuration and failsafe converter
mkdir -p "$Z2M_DATA"

echo -e "${BLUE}[INFO] Deploying failsafe files into ${Z2M_DATA}...${NC}"
cp "${SCRIPT_DIR}/zigbee2mqtt/inspelning-failsafe.js" "${Z2M_DATA}/"

if [[ ! -f "${Z2M_DATA}/configuration.yaml" ]]; then
    cp "${SCRIPT_DIR}/zigbee2mqtt/configuration.yaml" "${Z2M_DATA}/configuration.yaml"
    echo -e "${GREEN}[SUCCESS] Copied default configuration.yaml to ${Z2M_DATA}/configuration.yaml${NC}"
else
    echo -e "${YELLOW}[INFO] Existing configuration.yaml found; ensuring failsafe converter is referenced.${NC}"
    if ! grep -q "inspelning-failsafe.js" "${Z2M_DATA}/configuration.yaml"; then
        cat << 'EOF' >> "${Z2M_DATA}/configuration.yaml"

# Failsafe converter for IKEA INSPELNING
external_converters:
  - inspelning-failsafe.js
EOF
    fi
fi

# 6. Service registration
echo -e "${BLUE}[INFO] Registering and starting Zigbee2MQTT service (${INIT_SYS})...${NC}"

if [[ "$INIT_SYS" == "systemd" ]]; then
    # Ensure mosquitto is running
    systemctl enable --now mosquitto.service || true

    cp "${SCRIPT_DIR}/systemd/zigbee2mqtt.service" /etc/systemd/system/
    NODE_BIN="$(command -v node || echo /usr/bin/node)"
    sed -i "s|/usr/bin/node|${NODE_BIN}|g" /etc/systemd/system/zigbee2mqtt.service
    systemctl daemon-reload
    systemctl enable --now zigbee2mqtt.service
    echo -e "${GREEN}[SUCCESS] zigbee2mqtt.service enabled and started.${NC}"

elif [[ "$INIT_SYS" == "openrc" ]]; then
    rc-update add mosquitto default || true
    rc-service mosquitto restart || true

    cp "${SCRIPT_DIR}/openrc/zigbee2mqtt" /etc/init.d/zigbee2mqtt
    chmod +x /etc/init.d/zigbee2mqtt
    rc-update add zigbee2mqtt default
    rc-service zigbee2mqtt restart
    echo -e "${GREEN}[SUCCESS] OpenRC zigbee2mqtt service registered and started.${NC}"
fi

echo ""
echo -e "${BOLD}${GREEN}========================================================================${NC}"
echo -e "${BOLD}${GREEN} Zigbee2MQTT is now installed and running!${NC}"
echo -e "${BOLD}${GREEN}========================================================================${NC}"
echo ""
echo -e "  ${BOLD}Web UI:${NC}    http://<CONTAINER_IP>:8080"
echo -e "  ${BOLD}Logs:${NC}      journalctl -u zigbee2mqtt.service -f"
echo ""
