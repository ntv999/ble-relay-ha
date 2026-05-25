"""Home Assistant MQTT Discovery payload builder.

Generates the JSON config messages that HA MQTT integration uses to
auto-create 'cover' entities (one per gate/barrier device).

Discovery spec: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── MQTT topic helpers ────────────────────────────────────────────────────────

def state_topic(device_id: str) -> str:
    return f"ble_gate/{device_id}/state"

def command_topic(device_id: str) -> str:
    return f"ble_gate/{device_id}/set"

def availability_topic(device_id: str) -> str:
    return f"ble_gate/{device_id}/availability"

def discovery_topic(device_id: str) -> str:
    return f"homeassistant/cover/{device_id}/config"


# ── State / command payload strings ──────────────────────────────────────────

# Firmware state byte → MQTT state string published by gateway
STATE_MAP: dict[int, str] = {
    0x00: "closed",
    0x01: "open",
    0x02: "opening",
    0x03: "unknown",  # error — availability will go offline simultaneously
    0xFF: "unknown",
}

# HA cover command payload → firmware CMD byte
COMMAND_MAP: dict[str, int] = {
    "OPEN":   0x01,
    "CLOSE":  0x02,
    "STOP":   0x03,
}

AVAILABILITY_ONLINE  = "online"
AVAILABILITY_OFFLINE = "offline"


# ── Discovery payload builder ─────────────────────────────────────────────────

@dataclass
class DeviceConfig:
    id: str
    name: str
    device_type: str          # "gate" | "barrier"
    address: str = ""
    discover_name: str = ""
    fw_version: str = "1.0.0"
    manufacturer: str = "BLE Gate Project"

    @property
    def model(self) -> str:
        return self.discover_name or self.device_type.upper() + "-01"

    @property
    def unique_id(self) -> str:
        return f"ble_gate_{self.id}"


def build_discovery_payload(dev: DeviceConfig) -> dict[str, Any]:
    """Return the MQTT Discovery config dict for a cover entity."""
    return {
        "name": dev.name,
        "unique_id": dev.unique_id,
        # HA device_class "gate" shows a gate icon; "garage" also valid for barriers
        "device_class": dev.device_type,
        "state_topic": state_topic(dev.id),
        "command_topic": command_topic(dev.id),
        "availability_topic": availability_topic(dev.id),
        "payload_available": AVAILABILITY_ONLINE,
        "payload_not_available": AVAILABILITY_OFFLINE,
        "payload_open": "OPEN",
        "payload_close": "CLOSE",
        "payload_stop": "STOP",
        "state_open": "open",
        "state_closed": "closed",
        "state_opening": "opening",
        "state_closing": "closing",
        # Retain so the entity state survives HA restarts
        "retain": False,
        "qos": 1,
        "device": {
            "identifiers": [dev.unique_id],
            "name": dev.name,
            "manufacturer": dev.manufacturer,
            "model": dev.model,
            "sw_version": dev.fw_version,
        },
    }


def build_discovery_json(dev: DeviceConfig) -> str:
    return json.dumps(build_discovery_payload(dev))


def ha_state_string(state_byte: int) -> str:
    """Convert a raw firmware STATE byte to the HA MQTT state string."""
    return STATE_MAP.get(state_byte, "unknown")


def ha_cmd_byte(payload: str) -> int | None:
    """Convert an HA MQTT command payload string to a firmware CMD byte.

    Returns None if the payload is not recognised.
    """
    return COMMAND_MAP.get(payload.upper())
