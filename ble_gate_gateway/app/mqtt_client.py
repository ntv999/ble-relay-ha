"""Async MQTT client wrapper around aiomqtt.

Responsibilities:
  - Publish HA MQTT Discovery payloads on startup
  - Publish STATE strings and availability for each device
  - Subscribe to command topics and forward commands to BLE clients
  - Reconnect automatically (aiomqtt handles the transport; we wrap it)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

import aiomqtt

from ha_discovery import (
    DeviceConfig,
    build_discovery_json,
    discovery_topic,
    state_topic,
    availability_topic,
    command_topic,
    ha_state_string,
    ha_cmd_byte,
    AVAILABILITY_ONLINE,
    AVAILABILITY_OFFLINE,
)

log = logging.getLogger(__name__)

CommandCallback = Callable[[str, int], Awaitable[None]]
"""Async callback(device_id: str, cmd_byte: int) called when HA sends a command."""


@dataclass
class MqttConfig:
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "ble_ha_gateway"
    keepalive: int = 60


class MqttClient:
    """Manages the MQTT connection and all topic publish/subscribe logic."""

    def __init__(
        self,
        config: MqttConfig,
        devices: list[DeviceConfig],
        on_command: CommandCallback,
    ) -> None:
        self._config = config
        self._devices = {dev.id: dev for dev in devices}
        self._on_command = on_command
        self._client: aiomqtt.Client | None = None
        self._publish_queue: asyncio.Queue[tuple[str, str, int, bool]] = asyncio.Queue()
        self._ready = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect to broker, publish discoveries, and process messages until cancelled."""
        cfg = self._config
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=cfg.host,
                    port=cfg.port,
                    username=cfg.username or None,
                    password=cfg.password or None,
                    identifier=cfg.client_id,
                    keepalive=cfg.keepalive,
                    will=None,
                ) as client:
                    self._client = client
                    log.info("MQTT connected to %s:%d", cfg.host, cfg.port)

                    await self._publish_discoveries(client)
                    await self._subscribe_command_topics(client)
                    self._ready.set()

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._message_loop(client))
                        tg.create_task(self._publish_loop(client))

            except aiomqtt.MqttError as exc:
                self._ready.clear()
                self._client = None
                log.warning("MQTT connection lost: %s — retrying in 10s", exc)
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return

    async def wait_ready(self) -> None:
        await self._ready.wait()

    # ── Public publish helpers ─────────────────────────────────────────────────

    async def publish_state(self, device_id: str, state_byte: int) -> None:
        payload = ha_state_string(state_byte)
        await self._enqueue(state_topic(device_id), payload, qos=1, retain=False)

    async def set_online(self, device_id: str) -> None:
        await self._enqueue(availability_topic(device_id), AVAILABILITY_ONLINE, qos=1, retain=True)

    async def set_offline(self, device_id: str) -> None:
        await self._enqueue(availability_topic(device_id), AVAILABILITY_OFFLINE, qos=1, retain=True)

    async def set_all_offline(self) -> None:
        for device_id in self._devices:
            await self.set_offline(device_id)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _publish_discoveries(self, client: aiomqtt.Client) -> None:
        for dev in self._devices.values():
            topic = discovery_topic(dev.id)
            payload = build_discovery_json(dev)
            await client.publish(topic, payload, qos=1, retain=True)
            log.info("Published discovery for '%s' → %s", dev.name, topic)

    async def _subscribe_command_topics(self, client: aiomqtt.Client) -> None:
        for device_id in self._devices:
            topic = command_topic(device_id)
            await client.subscribe(topic, qos=1)
            log.info("Subscribed to command topic: %s", topic)

    async def _message_loop(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            await self._handle_message(str(message.topic), message.payload)

    async def _handle_message(self, topic: str, payload: bytes | str) -> None:
        text = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
        # Match topic to device_id
        for device_id in self._devices:
            if topic == command_topic(device_id):
                cmd_byte = ha_cmd_byte(text)
                if cmd_byte is not None:
                    log.info("[%s] HA command: '%s' → 0x%02X", device_id, text, cmd_byte)
                    await self._on_command(device_id, cmd_byte)
                else:
                    log.warning("[%s] Unknown command payload: '%s'", device_id, text)
                return

    async def _enqueue(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        await self._publish_queue.put((topic, payload, qos, retain))

    async def _publish_loop(self, client: aiomqtt.Client) -> None:
        while True:
            topic, payload, qos, retain = await self._publish_queue.get()
            try:
                await client.publish(topic, payload, qos=qos, retain=retain)
                log.debug("MQTT publish %s = %s", topic, payload)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT publish failed (%s): %s", topic, exc)
