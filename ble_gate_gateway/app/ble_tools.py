#!/usr/bin/env python3
"""BLE scanner and pairing helper for the Home Assistant add-on."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

log = logging.getLogger(__name__)

DEFAULT_NAME_PREFIXES = ("GATE", "BARRIER", "GD")
GATE_SERVICE_UUID = "12345678-0001-0000-0000-000000000000"
BARRIER_SERVICE_UUID = "12345678-0002-0000-0000-000000000000"
TEST_COMPANY_ID = 0xFFFF
MANUF_TYPE_GATE = 0x01
MANUF_TYPE_BARRIER = 0x02


@dataclass
class ScanResult:
    address: str
    name: str
    rssi: int
    service_uuids: list[str]
    device_type: str
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    seen_count: int = 0


def _device_type(
    name: str,
    service_uuids: list[str],
    manufacturer_data: dict[int, bytes] | None = None,
) -> str:
    service_set = {uuid.lower() for uuid in service_uuids}
    upper_name = name.upper()
    manuf_type = _manufacturer_device_type(manufacturer_data or {})

    if GATE_SERVICE_UUID in service_set or upper_name.startswith(("GATE", "GD")):
        return "gate"
    if BARRIER_SERVICE_UUID in service_set or upper_name.startswith("BARRIER"):
        return "barrier"
    if manuf_type == MANUF_TYPE_GATE:
        return "gate"
    if manuf_type == MANUF_TYPE_BARRIER:
        return "barrier"
    return "unknown"


def _manufacturer_device_type(manufacturer_data: dict[int, bytes]) -> int | None:
    payload = manufacturer_data.get(TEST_COMPANY_ID, b"")
    if len(payload) >= 2:
        return payload[1]

    # Defensive fallback for scanners that expose the raw manufacturer payload
    # including the company id bytes.
    for data in manufacturer_data.values():
        if len(data) >= 4 and data[0] == 0xFF and data[1] == 0xFF:
            return data[3]

    return None


def _matches(
    name: str,
    service_uuids: list[str],
    manufacturer_data: dict[int, bytes],
    prefixes: tuple[str, ...],
) -> bool:
    if GATE_SERVICE_UUID in {uuid.lower() for uuid in service_uuids}:
        return True
    if BARRIER_SERVICE_UUID in {uuid.lower() for uuid in service_uuids}:
        return True
    if _manufacturer_device_type(manufacturer_data) in (
        MANUF_TYPE_GATE,
        MANUF_TYPE_BARRIER,
    ):
        return True
    return any(name.upper().startswith(prefix.upper()) for prefix in prefixes)


async def scan_devices(
    adapter: str,
    timeout: float,
    prefixes: tuple[str, ...] = DEFAULT_NAME_PREFIXES,
    show_all: bool = False,
) -> list[ScanResult]:
    results_by_addr: dict[str, ScanResult] = {}

    def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        result = _scan_result(device, adv)
        previous = results_by_addr.get(result.address)
        if previous:
            _merge_result(previous, result)
        else:
            results_by_addr[result.address] = result

    scanner = _new_scanner(adapter, on_detect)
    log.info("Active scan started on %s for %.1f s", adapter, timeout)

    async with scanner:
        await asyncio.sleep(timeout)

    results = [
        result for result in results_by_addr.values()
        if show_all or _matches(
            result.name,
            result.service_uuids,
            result.manufacturer_data,
            prefixes,
        )
    ]

    results.sort(key=lambda item: item.rssi, reverse=True)
    return results


def _new_scanner(adapter: str, callback) -> BleakScanner:
    try:
        return BleakScanner(callback, adapter=adapter, scanning_mode="active")
    except TypeError:
        return BleakScanner(callback, adapter=adapter)


def _scan_result(device: BLEDevice, adv: AdvertisementData) -> ScanResult:
    name = adv.local_name or device.name or ""
    service_uuids = list(adv.service_uuids or [])
    manufacturer_data = dict(adv.manufacturer_data or {})
    device_type = _device_type(name, service_uuids, manufacturer_data)
    if not name and device_type == "gate":
        name = "GATE-01"
    elif not name and device_type == "barrier":
        name = "BARRIER-01"

    return ScanResult(
        address=device.address,
        name=name or "(no name)",
        rssi=adv.rssi,
        service_uuids=service_uuids,
        device_type=device_type,
        manufacturer_data=manufacturer_data,
        seen_count=1,
    )


def _merge_result(current: ScanResult, update: ScanResult) -> None:
    current.rssi = max(current.rssi, update.rssi)
    current.seen_count += update.seen_count

    if current.name == "(no name)" and update.name != "(no name)":
        current.name = update.name

    service_set = {uuid.lower(): uuid for uuid in current.service_uuids}
    for uuid in update.service_uuids:
        service_set.setdefault(uuid.lower(), uuid)
    current.service_uuids = list(service_set.values())

    current.manufacturer_data.update(update.manufacturer_data)
    current.device_type = _device_type(
        current.name,
        current.service_uuids,
        current.manufacturer_data,
    )


def print_scan_results(results: list[ScanResult]) -> None:
    if not results:
        log.warning("No BLE gate/barrier devices found")
        return

    log.info("Found %d BLE candidate(s):", len(results))
    for index, result in enumerate(results, start=1):
        log.info(
            "%d. name=%s address=%s rssi=%s type=%s seen=%d services=%s manuf=%s",
            index,
            result.name,
            result.address,
            result.rssi,
            result.device_type,
            result.seen_count,
            ",".join(result.service_uuids) or "-",
            _format_manufacturer_data(result.manufacturer_data),
        )
        safe_id = _safe_id(result.name, index)
        safe_name = result.name if result.name != "(no name)" else f"BLE Gate {index}"
        log.info(
            "   devices YAML: [{id: \"%s\", name: \"%s\", type: \"%s\", "
            "address: \"%s\", discover_name: \"%s\"}]",
            safe_id,
            safe_name,
            result.device_type if result.device_type != "unknown" else "gate",
            result.address,
            "" if result.name == "(no name)" else result.name,
        )


def _format_manufacturer_data(manufacturer_data: dict[int, bytes]) -> str:
    if not manufacturer_data:
        return "-"

    return ",".join(
        f"0x{company_id:04x}:{payload.hex() or '-'}"
        for company_id, payload in sorted(manufacturer_data.items())
    )


def _safe_id(name: str, index: int) -> str:
    chars = []
    for char in name.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    value = "".join(chars).strip("_")
    return value or f"ble_gate_{index}"


async def pair_device(adapter: str, address: str, name: str, timeout: float) -> None:
    target = address
    if not target and name:
        log.info("Pair target address is empty, scanning for name '%s'", name)
        results = await scan_devices(adapter, timeout, show_all=True)
        target_result = next(
            (result for result in results if result.name == name),
            None,
        )
        if not target_result:
            raise RuntimeError(f"Device named {name!r} not found")
        target = target_result.address

    if not target:
        raise RuntimeError("Pair mode needs pair_address or pair_name")

    log.info("Connecting to %s for pairing", target)
    async with BleakClient(target, adapter=adapter, timeout=timeout) as client:
        if client.is_connected:
            log.info("Connected to %s", target)

        paired = False
        try:
            paired = await client.pair()
        except BleakError as exc:
            log.warning("BlueZ pair() returned an error: %s", exc)

        if paired:
            log.info("Pairing completed for %s", target)
        else:
            log.info("Pairing command finished; if already bonded, this is OK")


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("bleak").setLevel(logging.WARNING)


async def main() -> None:
    parser = argparse.ArgumentParser(description="BLE scan/pair helper")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--timeout", type=float, default=15)

    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--all", action="store_true")

    pair_parser = sub.add_parser("pair")
    pair_parser.add_argument("--address", default="")
    pair_parser.add_argument("--name", default="")

    args = parser.parse_args()
    _setup_logging()

    if args.command == "scan":
        results = await scan_devices(args.adapter, args.timeout, show_all=args.all)
        print_scan_results(results)
    elif args.command == "pair":
        await pair_device(args.adapter, args.address, args.name, args.timeout)


if __name__ == "__main__":
    asyncio.run(main())
