#!/usr/bin/env python3
"""BLE → MQTT gateway entry point.

Loads config.yaml, starts one BleClient per device in parallel,
and runs an MqttClient that bridges state/command between HA and BLE.

Usage:
    python3 gateway.py [--config path/to/config.yaml]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from ble_client import BleClient
from ha_discovery import DeviceConfig
from mqtt_client import CommandCallback, MqttClient, MqttConfig

log = logging.getLogger(__name__)


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(path: str) -> tuple[MqttConfig, dict, list[DeviceConfig]]:
    with open(path) as f:
        raw = yaml.safe_load(f)

    mqtt_raw = raw.get("mqtt", {})
    mqtt_cfg = MqttConfig(
        host=mqtt_raw.get("host", "localhost"),
        port=int(mqtt_raw.get("port", 1883)),
        username=mqtt_raw.get("username", ""),
        password=mqtt_raw.get("password", ""),
        client_id=mqtt_raw.get("client_id", "ble_ha_gateway"),
        keepalive=int(mqtt_raw.get("keepalive", 60)),
    )

    ble_raw = raw.get("ble", {})

    devices: list[DeviceConfig] = []
    for d in raw.get("devices", []):
        devices.append(DeviceConfig(
            id=d["id"],
            name=d["name"],
            device_type=d.get("type", "gate"),
            address=d.get("address", ""),
            discover_name=d.get("discover_name", ""),
        ))

    return mqtt_cfg, ble_raw, devices


# ── Per-device coroutine ──────────────────────────────────────────────────────

async def run_device(
    dev: DeviceConfig,
    ble_cfg: dict,
    mqtt: MqttClient,
    ble_clients: dict[str, BleClient],
) -> None:
    """Manage one BLE device: connect, forward state to MQTT, forward MQTT cmds to BLE."""

    async def on_state(device_id: str, state_byte: int) -> None:
        await mqtt.publish_state(device_id, state_byte)

    async def on_connected(device_id: str) -> None:
        await mqtt.set_online(device_id)

    async def on_disconnected(device_id: str) -> None:
        await mqtt.set_offline(device_id)

    client = BleClient(
        device_id=dev.id,
        device_name=dev.name,
        address=dev.address,
        discover_name=dev.discover_name,
        adapter=ble_cfg.get("adapter", "hci0"),
        scan_timeout=float(ble_cfg.get("scan_timeout", 15)),
        reconnect_delay_min=float(ble_cfg.get("reconnect_delay_min", 5)),
        reconnect_delay_max=float(ble_cfg.get("reconnect_delay_max", 60)),
        pair_on_connect=bool(ble_cfg.get("pair_on_connect", False)),
        on_state=on_state,
        on_connected=on_connected,
        on_disconnected=on_disconnected,
    )
    ble_clients[dev.id] = client

    # Wait until MQTT is ready before attempting BLE (avoids lost state messages)
    await mqtt.wait_ready()

    await client.run()


# ── MQTT command dispatcher ───────────────────────────────────────────────────

def make_command_handler(ble_clients: dict[str, BleClient]) -> CommandCallback:
    async def on_command(device_id: str, cmd_byte: int) -> None:
        client = ble_clients.get(device_id)
        if client:
            await client.send_cmd(cmd_byte)
        else:
            log.warning("Command for unknown device_id: %s", device_id)
    return on_command


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(config_path: str) -> None:
    mqtt_cfg, ble_cfg, devices = load_config(config_path)

    if not devices:
        log.error("No devices configured in %s", config_path)
        sys.exit(1)

    log.info("Starting BLE-MQTT gateway for %d device(s)", len(devices))

    ble_clients: dict[str, BleClient] = {}

    mqtt = MqttClient(
        config=mqtt_cfg,
        devices=devices,
        on_command=make_command_handler(ble_clients),
    )

    loop = asyncio.get_running_loop()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("Signal %d received, shutting down", signum)
        for client in ble_clients.values():
            client.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(signal.SIGINT,  lambda: _shutdown(signal.SIGINT, None))
    loop.add_signal_handler(signal.SIGTERM, lambda: _shutdown(signal.SIGTERM, None))

    async with asyncio.TaskGroup() as tg:
        tg.create_task(mqtt.run(), name="mqtt")
        for dev in devices:
            tg.create_task(
                run_device(dev, ble_cfg, mqtt, ble_clients),
                name=f"ble_{dev.id}",
            )


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from bleak internals
    logging.getLogger("bleak").setLevel(logging.WARNING)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLE → MQTT gateway for Home Assistant")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml (default: config.yaml next to this script)",
    )
    args = parser.parse_args()

    _setup_logging()

    try:
        asyncio.run(main(args.config))
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Gateway stopped")
