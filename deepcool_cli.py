#!/usr/bin/env python3
"""DeepCool MYSTIQUE 240/360 CLI — headless display controller for Linux.

Ported from main_cli.cpp. The Qt event loop, QCommandLineParser, and
QTimer served no purpose Python's own standard library doesn't already
cover for a CLI-only tool — argparse for options, a plain sleep loop for
the update cycle, and the `signal` module for Ctrl+C/SIGTERM — so this
version has no Qt dependency at all, and the systemd unit no longer needs
the `QT_QPA_PLATFORM=offscreen` workaround the original required.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

import device
import sensors
from protocol import DeepCoolDevice, DisplayMode, ScreenRotation, SystemData

logger = logging.getLogger("deepcool-cli")

_running = True


def _handle_signal(signum: int, _frame) -> None:
    global _running
    print(f"\nReceived signal {signum}, shutting down...")
    _running = False


def _daemonize() -> None:
    """Classic double-fork daemonization, kept for parity with the original
    and for anyone running this by hand outside systemd. Not needed when
    started via the provided unit file — Type=simple already has systemd
    supervise the foreground process directly, which is the more common
    pattern under systemd and avoids a background process with no unit to
    hold onto it.
    """
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)


def _list_devices() -> None:
    print("Scanning for DeepCool devices...\n")
    devices = device.detect_devices()

    if not devices:
        print("No DeepCool devices found.")
        print("\nTroubleshooting:")
        print("  1. Make sure the device is connected via USB")
        print("  2. Run with sudo or set up udev rules")
        print("  3. Check 'lsusb' for vendor ID 3633")
        return

    print(f"Found {len(devices)} device(s):\n")
    for i, d in enumerate(devices):
        type_str = "HID" if d.type == device.DeviceType.HID else "USB"
        print(f"  [{i}] {d.display_name}")
        print(f"      Type: {type_str}")
        print(f"      Path: {d.device_path}")
        print(f"      VID:PID: {d.vendor_id:04x}:{d.product_id:04x}")
        if d.type == device.DeviceType.USB_VENDOR:
            print(f"      Bus: {d.bus_number}, Address: {d.device_address}")
        print()


def _parse_display_mode(mode: str) -> DisplayMode:
    m = mode.lower()
    return {
        "cpu": DisplayMode.CPU_INFO, "0": DisplayMode.CPU_INFO,
        "gpu": DisplayMode.GPU_INFO, "1": DisplayMode.GPU_INFO,
        "gpu_focus": DisplayMode.GPU_FOCUS, "4": DisplayMode.GPU_FOCUS,
        "system": DisplayMode.SYSTEM_OVERVIEW, "2": DisplayMode.SYSTEM_OVERVIEW,
        "custom": DisplayMode.CUSTOM, "3": DisplayMode.CUSTOM,
    }.get(m, DisplayMode.CPU_INFO)


def _parse_rotation(degrees: int) -> ScreenRotation:
    return {90: ScreenRotation.R90, 180: ScreenRotation.R180, 270: ScreenRotation.R270}.get(
        degrees, ScreenRotation.R0
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deepcool-cli",
        description="DeepCool Digital Controller — CLI Version. "
        "Control DeepCool digital cooling devices from the command line.",
    )
    parser.add_argument("-l", "--list", action="store_true", help="List available devices and exit")
    parser.add_argument(
        "-d", "--device", default="0",
        help="Device path (/dev/hidrawX) or index (0, 1, ...)",
    )
    parser.add_argument("-i", "--interval", type=int, default=1000, help="Update interval in milliseconds")
    parser.add_argument("-m", "--mode", default="cpu", help="Display mode: cpu, gpu, gpu_focus")
    parser.add_argument("-f", "--fahrenheit", action="store_true", help="Use Fahrenheit instead of Celsius")
    parser.add_argument("-V", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("-D", "--daemon", action="store_true", help="Run as daemon (fork to background)")
    parser.add_argument("-r", "--rotate", type=int, default=0, help="Screen rotation: 0, 90, 180, 270 (degrees)")
    return parser


def main() -> int:
    global _running

    args = build_arg_parser().parse_args()

    if args.list:
        _list_devices()
        return 0

    if args.daemon:
        _daemonize()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_signal)

    if os.geteuid() != 0:
        print("Warning: Not running as root. Device access may fail.")
        print("Run with sudo or set up udev rules for non-root access.\n")

    devices = device.detect_devices()
    if not devices:
        print("No DeepCool devices found. Use -l to list devices.", file=sys.stderr)
        return 1

    try:
        idx = int(args.device)
        selected = devices[idx] if 0 <= idx < len(devices) else None
    except ValueError:
        selected = next((d for d in devices if d.device_path == args.device), None)

    if selected is None:
        selected = devices[0]
        logger.debug("Device %r not found, using first device", args.device)

    print(f"Selected device: {selected.display_name}")
    print(f"Device path: {selected.device_path}")

    dc = DeepCoolDevice()
    if not dc.open(selected):
        print(f"Failed to open device: {selected.device_path}", file=sys.stderr)
        print("Make sure you have permission to access the device.", file=sys.stderr)
        print("Try running with sudo or setting up udev rules.", file=sys.stderr)
        return 1

    print("Device opened successfully.")

    interval_ms = max(100, min(args.interval, 10000))
    display_mode = _parse_display_mode(args.mode)
    rotate_deg = args.rotate
    rotation = _parse_rotation(rotate_deg)

    dc.set_display_mode(display_mode)

    print("Initializing device...")
    if dc.init_machine_info_mode():
        print("Device initialized successfully.")
    else:
        print("Warning: Device init returned false, display may not update.")

    if rotate_deg != 0:
        if dc.set_rotation(rotation):
            print(f"Screen rotation set to {rotate_deg}\u00b0")
        else:
            print("Warning: Failed to set screen rotation.")

    print(f"Update interval: {interval_ms} ms")
    print(f"Display mode: {args.mode}")
    print(f"Temperature unit: {'Fahrenheit' if args.fahrenheit else 'Celsius'}")
    if rotate_deg != 0:
        print(f"Rotation: {rotate_deg}\u00b0")
    print()

    sensors.cpu_usage()  # first call establishes the baseline for the delta

    print("Starting monitoring... (Press Ctrl+C to stop)\n")

    interval_s = interval_ms / 1000.0
    try:
        while _running:
            data = SystemData(
                cpu_temp=sensors.cpu_temperature(args.fahrenheit),
                cpu_usage=sensors.cpu_usage(),
                gpu_temp=sensors.gpu_temperature(args.fahrenheit),
                gpu_usage=sensors.gpu_usage(),
                ram_usage=sensors.ram_usage(),
                use_fahrenheit=args.fahrenheit,
            )
            success = dc.update_display(data)

            unit = "F" if args.fahrenheit else "C"
            logger.debug(
                "CPU: %.1f%s (%.1f%%) | GPU: %.1f%s (%.1f%%) | RAM: %.1f%% | %s",
                data.cpu_temp, unit, data.cpu_usage,
                data.gpu_temp, unit, data.gpu_usage,
                data.ram_usage, "OK" if success else "FAIL",
            )
            time.sleep(interval_s)
    finally:
        dc.close()
        print("\nDevice closed. Goodbye!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
