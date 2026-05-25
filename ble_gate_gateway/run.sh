#!/usr/bin/with-contenv bashio

# ── Read options from HA Supervisor (set via Add-on UI) ─────────────────────
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USERNAME=$(bashio::config 'mqtt_username')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')
BLE_ADAPTER=$(bashio::config 'ble_adapter')
BLE_SCAN_TIMEOUT=$(bashio::config 'ble_scan_timeout')
MODE=$(bashio::config 'mode' 'gateway')
SCAN_SHOW_ALL=$(bashio::config 'scan_show_all' 'false')
PAIR_ADDRESS=$(bashio::config 'pair_address' '')
PAIR_NAME=$(bashio::config 'pair_name' '')
LOG_LEVEL=$(bashio::config 'log_level' 'info')

# ── Bluetooth adapter probe ──────────────────────────────────────────────────
try_bring_up_adapter() {
    local adapter="$1"
    local index="${adapter#hci}"

    if command -v hciconfig >/dev/null 2>&1; then
        hciconfig "${adapter}" up && return 0
        bashio::log.warning "hciconfig cannot control ${adapter}; will rely on host BlueZ"
    fi

    if command -v btmgmt >/dev/null 2>&1; then
        btmgmt --index "${index}" power on && return 0
        bashio::log.warning "btmgmt cannot control ${adapter}; will rely on host BlueZ"
    fi

    if command -v bluetoothctl >/dev/null 2>&1; then
        bluetoothctl power on && return 0
        bashio::log.warning "bluetoothctl power on failed; will rely on host BlueZ"
    fi

    return 1
}

bashio::log.info "Preparing BLE adapter ${BLE_ADAPTER}..."
if ! try_bring_up_adapter "${BLE_ADAPTER}"; then
    bashio::log.warning "Adapter power-up skipped/failed; continuing via host D-Bus"
fi

if [ "${MODE}" = "scan" ]; then
    bashio::log.info "Scanning BLE devices on ${BLE_ADAPTER} for ${BLE_SCAN_TIMEOUT}s..."
    SCAN_ALL_FLAG=""
    if bashio::var.true "${SCAN_SHOW_ALL}"; then
        SCAN_ALL_FLAG="--all"
    fi
    LOG_LEVEL="${LOG_LEVEL^^}" exec python3 /app/ble_tools.py \
        --adapter "${BLE_ADAPTER}" \
        --timeout "${BLE_SCAN_TIMEOUT}" \
        scan ${SCAN_ALL_FLAG}
fi

if [ "${MODE}" = "pair" ]; then
    bashio::log.info "Pairing BLE device on ${BLE_ADAPTER}..."
    LOG_LEVEL="${LOG_LEVEL^^}" exec python3 /app/ble_tools.py \
        --adapter "${BLE_ADAPTER}" \
        --timeout "${BLE_SCAN_TIMEOUT}" \
        pair \
        --address "${PAIR_ADDRESS}" \
        --name "${PAIR_NAME}"
fi

# ── Build config.yaml from Supervisor options ────────────────────────────────
CONFIG_FILE="/tmp/gateway_config.yaml"
python3 - <<PYEOF
import json, yaml, os

options_file = "/data/options.json"
with open(options_file) as f:
    opts = json.load(f)

config = {
    "mqtt": {
        "host": opts.get("mqtt_host", "core-mosquitto"),
        "port": int(opts.get("mqtt_port", 1883)),
        "username": opts.get("mqtt_username", ""),
        "password": opts.get("mqtt_password", ""),
        "client_id": "ble_ha_gateway",
        "keepalive": 60,
    },
    "ble": {
        "adapter": opts.get("ble_adapter", "hci1"),
        "scan_timeout": int(opts.get("ble_scan_timeout", 15)),
        "reconnect_delay_min": 5,
        "reconnect_delay_max": 60,
        "pair_on_connect": bool(opts.get("pair_on_connect", False)),
    },
    "devices": opts.get("devices", []),
}

with open("${CONFIG_FILE}", "w") as f:
    yaml.dump(config, f)

print("Config written to ${CONFIG_FILE}")
PYEOF

# ── Start gateway ─────────────────────────────────────────────────────────────
bashio::log.info "Starting BLE-MQTT gateway (log level: ${LOG_LEVEL^^})"
LOG_LEVEL="${LOG_LEVEL^^}" exec python3 /app/gateway.py --config "${CONFIG_FILE}"
