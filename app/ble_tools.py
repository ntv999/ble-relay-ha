#!/usr/bin/env python3
"""BLE scanner and pairing helper for the Home Assistant add-on."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

log = logging.getLogger(__name__)

DEFAULT_NAME_PREFIXES = ("GATE", "BARRIER", "GD")
GATE_SERVICE_UUID = "12345678-0001-0000-0000-000000000000"
BARRIER_SERVICE_UUID = "12345678-0002-0000-0000-000000000000"


@dataclass
class ScanResult:
    address: str
    name: str
    rssi: int
    service_uuids: list[str]
    device_type: str


def _device_type(name: str, service_uuids: list[str]) -> str:
    service_set = {uuid.lower() for uuid in service_uuids}
    upper_name = name.upper()

    if GATE_SERVICE_UUID in service_set or upper_name.startswith(("GATE", "GD")):
        return "gate"
    if BARRIER_SERVICE_UUID in service_set or upper_name.startswith("BARRIER"):
        return "barrier"
    return "unknown"


def _matches(name: str, service_uuids: list[str], prefixes: tuple[str, ...]) -> bool:
    if GATE_SERVICE_UUID in {uuid.lower() for uuid in service_uuids}:
        return True
    if BARRIER_SERVICE_UUID in {uuid.lower() for uuid in service_uuids}:
        return True
    return any(name.upper().startswith(prefix.upper()) for prefix in prefixes)


async def scan_devices(
    adapter: str,
    timeout: float,
    prefixes: tuple[str, ...] = DEFAULT_NAME_PREFIXES,
    show_all: bool = False,
) -> list[ScanResult]:
    scanner = BleakScanner(adapter=adapter)

    async with scanner:
        await asyncio.sleep(timeout)

    results: list[ScanResult] = []
    for device, adv in scanner.discovered_devices_and_advertisement_data.values():
        result = _scan_result(device, adv)
        if show_all or _matches(result.name, result.service_uuids, prefixes):
            results.append(result)

    results.sort(key=lambda item: item.rssi, reverse=True)
    return results


def _scan_result(device: BLEDevice, adv: AdvertisementData) -> ScanResult:
    name = adv.local_name or device.name or ""
    service_uuids = list(adv.service_uuids or [])
    return ScanResult(
        address=device.address,
        name=name or "(no name)",
        rssi=adv.rssi,
        service_uuids=service_uuids,
        device_type=_device_type(name, service_uuids),
    )


def print_scan_results(results: list[ScanResult]) -> None:
    if not results:
        log.warning("No BLE gate/barrier devices found")
        return

    log.info("Found %d BLE candidate(s):", len(results))
    for index, result in enumerate(results, start=1):
        log.info(
            "%d. name=%s address=%s rssi=%s type=%s services=%s",
            index,
            result.name,
            result.address,
            result.rssi,
            result.device_type,
            ",".join(result.service_uuids) or "-",
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
        device = await BleakScanner.find_device_by_name(
            name,
            timeout=timeout,
            adapter=adapter,
        )
        if not device:
            raise RuntimeError(f"Device named {name!r} not found")
        target = device.address

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
