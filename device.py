"""Device detection for DeepCool digital coolers.

Ported from device.h/device.cpp (Qt/C++). Two device types exist on the wire:
HID (via /dev/hidraw*, read with a plain ioctl) and vendor-specific USB (via
libusb, for devices that don't present as a standard HID interface). Both are
scanned so this keeps parity with the original, even though this project's
own device uses the HID path.
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)

DEEPCOOL_VENDOR_ID = 0x3633
MYSTIQUE_PRODUCT_ID = 0x0009

# _IOR('H', 0x03, sizeof(struct hidraw_devinfo)); struct hidraw_devinfo is
# { __u32 bustype; __s16 vendor; __s16 product; } = 8 bytes. Computed, not
# guessed — a wrong ioctl number fails loudly (EINVAL), but getting the
# device's own vendor/product ID wrong here would misidentify hardware
# silently, so this is worth getting right rather than copying a number that
# "looks about right" from memory.
HIDIOCGRAWINFO = 0x80084803
_HIDRAW_DEVINFO_FMT = "=Ihh"  # bustype (u32), vendor (s16), product (s16)


class DeviceType(IntEnum):
    HID = 1
    USB_VENDOR = 2


# PID -> display name, transcribed from Device::getDeviceNameFromIds.
_DEVICE_NAMES: dict[int, str] = {
    0x0001: "DeepCool AK400 DIGITAL",
    0x0002: "DeepCool AK620 DIGITAL",
    0x0003: "DeepCool AK500 DIGITAL",
    0x0004: "DeepCool AK500S DIGITAL",
    0x0005: "DeepCool CH560 DIGITAL",
    0x0006: "DeepCool LS520/LS720 SE DIGITAL",
    0x0007: "DeepCool MORPHEUS",
    0x0008: "DeepCool AG400/AG620 DIGITAL",
    0x0009: "DeepCool MYSTIQUE 240/360",
    0x000A: "DeepCool LD240/LD360",
    0x000C: "DeepCool LP240/LP360",
    0x000D: "DeepCool LQ240/LQ360",
    0x000F: "DeepCool ASSASSIN IV VC VISION",
    0x0010: "DeepCool AK400 DIGITAL PRO",
    0x0011: "DeepCool AK500 DIGITAL PRO",
    0x0012: "DeepCool AK620 DIGITAL PRO",
    0x0013: "DeepCool CH170 DIGITAL",
    0x0015: "DeepCool CH360 DIGITAL",
    0x0016: "DeepCool CH270 DIGITAL",
    0x001B: "DeepCool CH690 DIGITAL",
}


def device_name_from_ids(vendor: int, product: int) -> str:
    if vendor == DEEPCOOL_VENDOR_ID:
        return _DEVICE_NAMES.get(product, f"DeepCool Device (PID: 0x{product:04x})")
    if vendor == 0x34D3 and product == 0x1100:
        return "DeepCool CH510 MESH DIGITAL"
    return f"Unknown Device (VID: 0x{vendor:04x}, PID: 0x{product:04x})"


@dataclass
class DeviceInfo:
    type: DeviceType
    vendor_id: int
    product_id: int
    device_path: str          # HID: /dev/hidrawX. USB: "usb:vid:pid".
    display_name: str
    bus_number: int = 0
    device_address: int = 0


def _is_deepcool(vendor: int, product: int) -> bool:
    return vendor == DEEPCOOL_VENDOR_ID or (vendor == 0x34D3 and product == 0x1100)


def scan_hid_devices() -> list[DeviceInfo]:
    """Scan /dev/hidraw* for a DeepCool device, matched by vendor/product ID.

    Vendor/product come from HIDIOCGRAWINFO, not from the device's reported
    name (hidraw devices don't reliably expose one) — same approach the
    original C++ takes.
    """
    devices: list[DeviceInfo] = []
    try:
        entries = sorted(e for e in os.listdir("/dev") if e.startswith("hidraw"))
    except OSError as exc:
        logger.debug("Could not list /dev for hidraw devices: %s", exc)
        return devices

    for entry in entries:
        path = f"/dev/{entry}"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug("Cannot open %s: %s", path, exc)
            continue

        try:
            buf = bytearray(8)
            fcntl.ioctl(fd, HIDIOCGRAWINFO, buf)
            _bustype, vendor_s, product_s = struct.unpack(_HIDRAW_DEVINFO_FMT, bytes(buf))
            # hidraw_devinfo's vendor/product are declared signed (__s16) in
            # the kernel header even though USB IDs are unsigned 16-bit
            # values; mask back to unsigned so 0x3633 round-trips as 0x3633
            # rather than coming back negative.
            vendor, product = vendor_s & 0xFFFF, product_s & 0xFFFF
        except OSError as exc:
            logger.debug("HIDIOCGRAWINFO failed on %s: %s", path, exc)
            os.close(fd)
            continue

        os.close(fd)

        if _is_deepcool(vendor, product):
            devices.append(
                DeviceInfo(
                    type=DeviceType.HID,
                    vendor_id=vendor,
                    product_id=product,
                    device_path=path,
                    display_name=device_name_from_ids(vendor, product),
                )
            )
            logger.debug("Found HID device: %s at %s", devices[-1].display_name, path)

    return devices


def scan_usb_devices() -> list[DeviceInfo]:
    """Scan for a DeepCool device via its vendor-specific USB interface.

    Requires pyusb; if it isn't installed, this scan is skipped rather than
    treated as fatal, since the HID path above covers this project's own
    hardware on its own.
    """
    devices: list[DeviceInfo] = []
    try:
        import usb.core
    except ImportError:
        logger.debug("pyusb not installed; skipping the USB-vendor scan (HID scan still runs).")
        return devices

    try:
        found = usb.core.find(find_all=True)
    except Exception as exc:  # pragma: no cover - depends on libusb/permissions
        logger.debug("libusb device enumeration failed: %s", exc)
        return devices

    seen: set[tuple[int, int]] = set()
    for dev in found:
        if not _is_deepcool(dev.idVendor, dev.idProduct):
            continue
        key = (dev.idVendor, dev.idProduct)
        if key in seen:
            continue
        seen.add(key)
        devices.append(
            DeviceInfo(
                type=DeviceType.USB_VENDOR,
                vendor_id=dev.idVendor,
                product_id=dev.idProduct,
                device_path=f"usb:{dev.idVendor:04x}:{dev.idProduct:04x}",
                display_name=device_name_from_ids(dev.idVendor, dev.idProduct),
                bus_number=dev.bus,
                device_address=dev.address,
            )
        )
    return devices


def detect_devices() -> list[DeviceInfo]:
    devices = scan_hid_devices()
    devices.extend(scan_usb_devices())
    return devices
