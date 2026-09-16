"""DeepCool MYSTIQUE 240/360 protocol implementation.

Ported from deepcooldevice.cpp/h. This is the protocol-critical file: every
byte offset, payload, and the checksum algorithm are transcribed to match the
original exactly, not re-derived from PROTOCOL.md's prose description. Where
the two disagree (see the note on `_encode_ghz_decimal` below), the working
C++ implementation is treated as ground truth, since that is the code that
has actually been driving a real display; the .md is documentation that can
drift out of sync with it.

A note on scope, carried over from the C++ version: `init_machine_info_mode`
calls into libusb unconditionally, with no check for whether the device was
opened via the HID path or the vendor-specific USB path. PROTOCOL.md states
the device's interface class is Vendor Specific (0xFF), not a standard HID
class — which means the HID scan in device.py most likely never finds this
particular cooler at all, and the only path that has ever actually been
exercised is the libusb one. That asymmetry is preserved here rather than
"fixed", since fixing it would mean inventing a HID init sequence with
nothing to verify it against.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import IntEnum

from device import DeviceInfo, DeviceType, device_name_from_ids

logger = logging.getLogger(__name__)

PACKET_SIZE = 48
HEADER = bytes([0xAA, 0x2E])
FOOTER = b"HIDC"  # 0x48 0x49 0x44 0x43

CMD_UPDATE_DISPLAY = 0x01
CMD_CONFIG = 0x02
CMD_STATUS_REQUEST = 0x10

# Bulk endpoints on the vendor-specific interface. 0x01/0x81 handle init and
# configuration; 0x02/0x82 handle the recurring display-data writes.
EP_INIT_OUT, EP_INIT_IN = 0x01, 0x81
EP_DATA_OUT, EP_DATA_IN = 0x02, 0x82


class DisplayMode(IntEnum):
    CPU_INFO = 0        # CPU temp main, CPU GHz bottom
    GPU_INFO = 1        # CPU temp main, GPU temp bottom
    SYSTEM_OVERVIEW = 2
    CUSTOM = 3
    GPU_FOCUS = 4        # GPU temp main, CPU temp bottom


class ScreenRotation(IntEnum):
    R0 = 0
    R90 = 1
    R180 = 2
    R270 = 3


@dataclass
class SystemData:
    cpu_temp: float
    cpu_usage: float
    gpu_temp: float
    gpu_usage: float
    ram_usage: float
    use_fahrenheit: bool = False


def build_packet(command: int, payload: bytes = b"") -> bytes:
    """Assemble one 48-byte packet: header, command, payload, footer, checksum."""
    packet = bytearray(PACKET_SIZE)
    packet[0:2] = HEADER
    packet[2] = command
    if payload and len(payload) <= 39:
        packet[3 : 3 + len(payload)] = payload
    packet[42:46] = FOOTER

    checksum = sum(packet[0:46]) & 0xFFFF  # quint16 wraps; mirror that explicitly
    packet[46] = checksum & 0xFF
    packet[47] = (checksum >> 8) & 0xFF
    return bytes(packet)


def _encode_ghz_decimal(ghz: float, whole: int) -> int:
    """Reproduces `displayDecimal` from updateDisplay() exactly.

    PROTOCOL.md describes offsets 24-25 as "MHz value, little-endian, e.g.
    50 14 = 5200 MHz" — but the shipped C++ computes
    `(quint16)((ghz - whole) * 100) * 100`, which for 5.20 GHz gives
    20 * 100 = 2000, not 5200. The two disagree; this function matches the
    code that has actually been running, not the prose description.
    """
    return int((ghz - whole) * 100) * 100


class DeepCoolDevice:
    """Talks to a DeepCool MYSTIQUE over its vendor-specific USB interface."""

    def __init__(self) -> None:
        self._usb_dev = None  # a pyusb usb.core.Device once open() succeeds
        self.device_info: DeviceInfo | None = None
        self.device_name = ""
        self.current_mode = DisplayMode.CPU_INFO
        self.current_rotation = ScreenRotation.R0

    # -- lifecycle -----------------------------------------------------------

    def is_open(self) -> bool:
        return self._usb_dev is not None

    def open(self, info: DeviceInfo) -> bool:
        if self.is_open():
            self.close()

        if info.type != DeviceType.USB_VENDOR:
            # See the module docstring: the HID path is carried for parity
            # with the original class design, but nothing here has an
            # implementation for it, matching upstream's own behavior for
            # this device.
            logger.error(
                "Opening via the HID path is not implemented (upstream doesn't "
                "either for this device — see the module docstring)."
            )
            return False

        try:
            import usb.core
            import usb.util
        except ImportError:
            logger.error("pyusb is required to open a device (pip install pyusb).")
            return False

        dev = usb.core.find(idVendor=info.vendor_id, idProduct=info.product_id)
        if dev is None:
            return False

        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass  # No kernel driver attached, or platform doesn't support querying it.

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # Already configured; harmless to continue.

        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as exc:
            logger.error("Could not claim the USB interface: %s", exc)
            return False

        self._usb_dev = dev
        self.device_info = info
        self.device_name = device_name_from_ids(info.vendor_id, info.product_id)
        return True

    def close(self) -> None:
        if self._usb_dev is not None:
            try:
                import usb.util

                usb.util.release_interface(self._usb_dev, 0)
                usb.util.dispose_resources(self._usb_dev)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.debug("Error while releasing the USB device: %s", exc)
        self._usb_dev = None

    # -- low-level transfer ----------------------------------------------------

    def _bulk_write(self, endpoint: int, data: bytes) -> bool:
        try:
            self._usb_dev.write(endpoint, data, timeout=1000)
            return True
        except Exception as exc:
            logger.debug("Bulk write to endpoint 0x%02x failed: %s", endpoint, exc)
            return False

    def _bulk_read(self, endpoint: int, length: int) -> bytes:
        try:
            data = self._usb_dev.read(endpoint, length, timeout=1000)
            return bytes(data)
        except Exception as exc:
            logger.debug("Bulk read from endpoint 0x%02x failed: %s", endpoint, exc)
            return b""

    def _send_init_packet(self, command: int, payload: bytes = b"") -> bytes:
        """One request/response round trip on the init endpoint pair (0x01/0x81)."""
        packet = build_packet(command, payload)
        self._bulk_write(EP_INIT_OUT, packet)
        return self._bulk_read(EP_INIT_IN, 64)

    # -- high-level commands -----------------------------------------------------

    def send_status_request(self) -> bool:
        if not self.is_open():
            return False
        if not self._bulk_write(EP_DATA_OUT, build_packet(CMD_STATUS_REQUEST)):
            return False
        self._bulk_read(EP_DATA_IN, 64)
        return True

    def init_machine_info_mode(self) -> bool:
        """Cold-boot init sequence. Required once before the display responds
        to data updates; a warm reboot may not need it, but running it again
        is harmless.
        """
        if not self.is_open():
            return False

        logger.debug("Sending initialization sequence (captured from Windows)...")

        self._send_init_packet(0x12)  # device info
        time.sleep(0.01)

        rotation_payload = bytes([0x01, 0x00, self.current_rotation & 0x03, 0x01, 0x24])
        self._send_init_packet(CMD_CONFIG, rotation_payload)
        time.sleep(0.01)

        self._send_init_packet(0x03, bytes.fromhex("01"))
        self._send_init_packet(0x04, bytes.fromhex("05000001"))
        self._send_init_packet(0x07, bytes.fromhex("0002"))
        self._send_init_packet(0x08, bytes.fromhex("0004"))
        self._send_init_packet(0x05, bytes.fromhex("0101"))
        self._send_init_packet(0x0B)
        self._send_init_packet(0x06, bytes.fromhex("01"))
        time.sleep(0.01)

        self._send_init_packet(0x15, bytes.fromhex("2d2d"))
        self._send_init_packet(0x16, bytes.fromhex("2d2d"))
        self._send_init_packet(0x17, bytes.fromhex("2d2d"))
        time.sleep(0.01)

        resp = self._send_init_packet(0x0A, bytes.fromhex("ea07020202272100"))
        logger.debug("Mode switch response: %s", resp[:10].hex())
        time.sleep(0.1)

        resp = self._send_init_packet(CMD_STATUS_REQUEST)
        logger.debug("Status response: %s", resp[:10].hex())

        success = len(resp) >= 6 and resp[5] == 0xFF
        if success:
            logger.debug("Init succeeded.")
        return success

    def set_display_mode(self, mode: DisplayMode) -> bool:
        self.current_mode = mode
        return True

    def set_rotation(self, rotation: ScreenRotation) -> bool:
        """Sent via CMD_CONFIG on the init endpoint, same as during init but
        with the caller's chosen rotation instead of the constructor default.
        """
        if not self.is_open():
            return False

        payload = bytes([0x01, 0x00, rotation & 0x03, 0x01, 0x24])
        packet = build_packet(CMD_CONFIG, payload)

        if not self._bulk_write(EP_INIT_OUT, packet):
            return False
        self._bulk_read(EP_INIT_IN, 64)

        self.current_rotation = rotation
        logger.debug("Screen rotation set to %d degrees", rotation * 90)
        return True

    def update_display(self, data: SystemData) -> bool:
        if not self.is_open():
            return False

        # A status request precedes every display update in the original;
        # kept even though its response is unused, to match the device's
        # expected request pattern.
        if not self._bulk_write(EP_DATA_OUT, build_packet(CMD_STATUS_REQUEST)):
            return False
        self._bulk_read(EP_DATA_IN, 48)

        cpu_temp = int(max(0.0, min(data.cpu_temp, 127.0)))
        cpu_usage = int(max(0.0, min(data.cpu_usage, 100.0)))
        gpu_temp = int(max(0.0, min(data.gpu_temp, 127.0)))

        main_temp, bottom_temp = cpu_temp, gpu_temp
        if self.current_mode == DisplayMode.GPU_FOCUS:
            main_temp, bottom_temp = gpu_temp, cpu_temp

        mem_usage = round(max(0.0, min(data.ram_usage, 100.0)))

        cpu_mhz = _current_cpu_mhz()

        if self.current_mode == DisplayMode.GPU_INFO:
            display_integer, display_decimal = gpu_temp, 0
        elif self.current_mode == DisplayMode.GPU_FOCUS:
            display_integer, display_decimal = bottom_temp, 0
        else:
            ghz = cpu_mhz / 1000.0
            display_integer = int(ghz)
            display_decimal = _encode_ghz_decimal(ghz, display_integer)

        byte9 = mem_usage
        byte11 = 0x05 if gpu_temp < 35 else (0x06 if gpu_temp < 38 else 0x09)
        byte17 = 0x06
        byte23 = 0
        if self.current_mode not in (DisplayMode.GPU_INFO, DisplayMode.GPU_FOCUS):
            byte23 = (cpu_mhz % 1000) // 10
            if byte23 < 10:
                byte23 = 0x0A

        packet = bytearray(PACKET_SIZE)
        packet[0:2] = HEADER
        packet[2] = CMD_UPDATE_DISPLAY
        packet[3] = main_temp & 0xFF
        packet[6] = cpu_usage & 0xFF
        packet[9] = byte9 & 0xFF
        packet[11] = byte11 & 0xFF
        packet[12] = 0x03
        packet[14] = gpu_temp & 0xFF
        packet[15] = 0x05
        packet[17] = byte17 & 0xFF
        packet[18] = 0x0C
        packet[20] = 0x07
        packet[21] = display_integer & 0xFF
        packet[23] = byte23 & 0xFF
        packet[24] = display_decimal & 0xFF
        packet[25] = (display_decimal >> 8) & 0xFF
        packet[27] = display_decimal & 0xFF
        packet[28] = (display_decimal >> 8) & 0xFF
        packet[42:46] = FOOTER

        checksum = sum(packet[0:46]) & 0xFFFF
        packet[46] = checksum & 0xFF
        packet[47] = (checksum >> 8) & 0xFF

        if not self._bulk_write(EP_DATA_OUT, bytes(packet)):
            return False
        self._bulk_read(EP_DATA_IN, 48)
        return True


def _current_cpu_mhz() -> int:
    """Max scaling_cur_freq across all cores, in MHz. Falls back to 3500."""
    import pathlib

    cpu_root = pathlib.Path("/sys/devices/system/cpu")
    best = 0
    if cpu_root.is_dir():
        for entry in cpu_root.iterdir():
            if not (entry.name.startswith("cpu") and entry.name[3:].isdigit()):
                continue
            freq_path = entry / "cpufreq" / "scaling_cur_freq"
            try:
                khz = int(freq_path.read_text().strip())
            except (OSError, ValueError):
                continue
            best = max(best, khz // 1000)
    return best or 3500
