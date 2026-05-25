"""BLE GATT client for nRF52840 gate/barrier device.

Handles scanning, connecting, STATE notifications, CMD writes, and
automatic reconnection with exponential back-off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

log = logging.getLogger(__name__)

# ── GATT UUIDs (from firmware service.c) ────────────────────────────────────
GATE_SERVICE_UUID    = "12345678-0001-0000-0000-000000000000"
BARRIER_SERVICE_UUID = "12345678-0002-0000-0000-000000000000"
STATE_UUID           = "12345678-0010-0000-0000-000000000000"
CMD_UUID             = "12345678-0020-0000-0000-000000000000"
CONFIG_UUID          = "12345678-0030-0000-0000-000000000000"

# ── State byte → readable name ────────────────────────────────────────────────
STATE_CLOSED  = 0x00
STATE_OPEN    = 0x01
STATE_MOVING  = 0x02
STATE_ERROR   = 0x03
STATE_UNKNOWN = 0xFF

STATE_NAMES = {
    STATE_CLOSED:  "closed",
    STATE_OPEN:    "open",
    STATE_MOVING:  "moving",
    STATE_ERROR:   "error",
    STATE_UNKNOWN: "unknown",
}

# ── Command bytes ─────────────────────────────────────────────────────────────
CMD_OPEN   = 0x01
CMD_CLOSE  = 0x02
CMD_STOP   = 0x03
CMD_TOGGLE = 0x04


StateCallback = Callable[[str, int], Awaitable[None]]
"""Async callback(device_id: str, state_byte: int) called on every STATE notification."""


class BleClient:
    """Manages one BLE connection to a single gate/barrier device."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        address: str,
        discover_name: str,
        adapter: str,
        scan_timeout: float,
        reconnect_delay_min: float,
        reconnect_delay_max: float,
        pair_on_connect: bool,
        on_state: StateCallback,
        on_connected: Callable[[str], Awaitable[None]],
        on_disconnected: Callable[[str], Awaitable[None]],
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.address = address
        self.discover_name = discover_name
        self.adapter = adapter
        self.scan_timeout = scan_timeout
        self.reconnect_delay_min = reconnect_delay_min
        self.reconnect_delay_max = reconnect_delay_max
        self.pair_on_connect = pair_on_connect
        self._on_state = on_state
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._client: BleakClient | None = None
        self._cmd_queue: asyncio.Queue[int] = asyncio.Queue()
        self._stop_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect → run → reconnect loop.  Runs until stop() is called."""
        delay = self.reconnect_delay_min
        while not self._stop_event.is_set():
            try:
                address = await self._resolve_address()
                if address is None:
                    log.warning("[%s] Device not found in scan, retrying in %ds",
                                self.device_id, delay)
                    await self._interruptible_sleep(delay)
                    delay = min(delay * 2, self.reconnect_delay_max)
                    continue

                log.info("[%s] Connecting to %s", self.device_id, address)
                async with BleakClient(
                    address,
                    adapter=self.adapter,
                    disconnected_callback=self._on_bleak_disconnect,
                ) as client:
                    self._client = client
                    delay = self.reconnect_delay_min  # reset backoff on success
                    if self.pair_on_connect:
                        await self._pair(client)
                    await self._setup_notifications(client)
                    await self._on_connected(self.device_id)
                    log.info("[%s] Connected and subscribed to STATE notifications",
                             self.device_id)
                    await self._run_session(client)

            except BleakError as exc:
                log.warning("[%s] BLE error: %s", self.device_id, exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("[%s] Unexpected error: %s", self.device_id, exc)
            finally:
                self._client = None
                await self._on_disconnected(self.device_id)

            if not self._stop_event.is_set():
                log.info("[%s] Reconnecting in %ds", self.device_id, delay)
                await self._interruptible_sleep(delay)
                delay = min(delay * 2, self.reconnect_delay_max)

    async def send_cmd(self, cmd_byte: int) -> None:
        """Queue a command byte to be written to the CMD characteristic."""
        await self._cmd_queue.put(cmd_byte)

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._stop_event.set()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _resolve_address(self) -> str | None:
        """Return MAC address: from config if set, or by scanning for the device name."""
        if self.address:
            return self.address

        log.info("[%s] Scanning for '%s' (adapter=%s, timeout=%ds)",
                 self.device_id, self.discover_name, self.adapter, self.scan_timeout)
        device = await BleakScanner.find_device_by_name(
            self.discover_name,
            timeout=self.scan_timeout,
            adapter=self.adapter,
        )
        if device:
            rssi = device.rssi if hasattr(device, "rssi") else "n/a"
            log.info("[%s] Found '%s' at %s  RSSI=%s dBm",
                     self.device_id, self.discover_name, device.address, rssi)
            return device.address
        return None

    async def scan_rssi(self) -> int | None:
        """One-shot RSSI measurement.  Returns dBm or None if device not found.

        Call this periodically from a distance-test script to log signal strength
        at each physical position without making a full GATT connection.

        Example usage (standalone script):
            from ble_client import BleClient
            client = BleClient(...)
            rssi = await client.scan_rssi()
            print(f"RSSI at 10 m: {rssi} dBm")
        """
        scanner = BleakScanner(adapter=self.adapter)
        target_name = self.discover_name
        target_addr = self.address.upper() if self.address else ""

        found_rssi: int | None = None

        async with scanner:
            await asyncio.sleep(3.0)   # passive scan window
            for dev, adv_data in scanner.discovered_devices_and_advertisement_data.values():
                match_addr = target_addr and dev.address.upper() == target_addr
                match_name = (not target_addr) and (dev.name == target_name)
                if match_addr or match_name:
                    found_rssi = adv_data.rssi
                    break

        return found_rssi

    async def _setup_notifications(self, client: BleakClient) -> None:
        await client.start_notify(STATE_UUID, self._state_notification_handler)

    async def _pair(self, client: BleakClient) -> None:
        try:
            paired = await client.pair()
            if paired:
                log.info("[%s] Pairing completed", self.device_id)
            else:
                log.info("[%s] Pairing command finished or device already bonded",
                         self.device_id)
        except BleakError as exc:
            log.warning("[%s] Pairing failed: %s", self.device_id, exc)

    def _state_notification_handler(self, _handle: int, data: bytearray) -> None:
        if not data:
            return
        state_byte = data[0]
        log.debug("[%s] STATE notify: 0x%02X (%s)",
                  self.device_id, state_byte, STATE_NAMES.get(state_byte, "?"))
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._on_state(self.device_id, state_byte))
        )

    async def _run_session(self, client: BleakClient) -> None:
        """Process outgoing CMD writes until the connection drops."""
        disconnect_event = asyncio.Event()

        # Bleak calls disconnected_callback from another thread; bridge to asyncio.
        original_cb = client.disconnected_callback

        def _disconnected(c: BleakClient) -> None:
            asyncio.get_event_loop().call_soon_threadsafe(disconnect_event.set)
            if original_cb:
                original_cb(c)

        client.set_disconnected_callback(_disconnected)

        while not disconnect_event.is_set() and not self._stop_event.is_set():
            try:
                cmd_byte = await asyncio.wait_for(self._cmd_queue.get(), timeout=1.0)
                await self._write_cmd(client, cmd_byte)
            except asyncio.TimeoutError:
                pass  # just loop back and check events

    async def _write_cmd(self, client: BleakClient, cmd_byte: int) -> None:
        try:
            await client.write_gatt_char(CMD_UUID, bytes([cmd_byte]), response=False)
            log.info("[%s] CMD written: 0x%02X", self.device_id, cmd_byte)
        except BleakError as exc:
            log.warning("[%s] CMD write failed: %s", self.device_id, exc)

    def _on_bleak_disconnect(self, _client: BleakClient) -> None:
        log.info("[%s] BLE disconnected", self.device_id)

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
