"""System sensor reading for the DeepCool display.

Ported from the free functions in main_cli.cpp (getCPUTemperature,
getCPUUsage, getGPUTemperature, getGPUUsage, getRAMUsage) — not from
mainwindow.cpp's versions of the same names, which are GUI-only placeholders
(e.g. `return 45.5f; // Placeholder` for CPU usage) that were never wired
into the CLI path this project actually runs.

Uses psutil where it does the same job the original hand-rolled from /proc
and /sys, which is most of this file: psutil.cpu_percent() replaces the
manual /proc/stat delta bookkeeping, and psutil.sensors_temperatures()
replaces walking /sys/class/hwmon by hand. Both produce the same values, with
less code carrying the risk of a transcription mistake.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import psutil

logger = logging.getLogger(__name__)

_NVIDIA_SMI = shutil.which("nvidia-smi")


def cpu_temperature(fahrenheit: bool = False) -> float:
    """Max of coretemp (Intel) or k10temp (AMD) readings; falls back to the
    kernel's own thermal zones if neither chip is exposed via hwmon.
    """
    celsius = 0.0
    try:
        for chip, entries in psutil.sensors_temperatures().items():
            if chip not in ("coretemp", "k10temp"):
                continue
            for entry in entries:
                if entry.current and entry.current < 150:
                    celsius = max(celsius, entry.current)
    except Exception as exc:  # pragma: no cover - platform-dependent
        logger.debug("sensors_temperatures() failed: %s", exc)

    if celsius <= 0:
        for zone in range(3):
            try:
                with open(f"/sys/class/thermal/thermal_zone{zone}/temp") as f:
                    reading = int(f.read().strip()) / 1000.0
                if 0 < reading < 150:
                    celsius = max(celsius, reading)
            except (OSError, ValueError):
                continue

    if celsius <= 0:
        return 0.0
    return celsius * 9.0 / 5.0 + 32.0 if fahrenheit else celsius


def cpu_usage() -> float:
    """Percent, averaged since the previous call. psutil keeps the delta
    state internally, replacing the original's manual prev_idle/prev_total
    bookkeeping against /proc/stat.
    """
    return psutil.cpu_percent(interval=None)


def gpu_temperature(fahrenheit: bool = False) -> float:
    """NVIDIA via nvidia-smi, else AMD via hwmon. No Intel iGPU path — the
    original didn't have a working one either (its intelTemp lookup used a
    literal glob string as a path and always fell through)."""
    if _NVIDIA_SMI:
        try:
            out = subprocess.run(
                [_NVIDIA_SMI, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                celsius = float(out.stdout.strip().splitlines()[0])
                if celsius > 0:
                    return celsius * 9.0 / 5.0 + 32.0 if fahrenheit else celsius
        except (subprocess.SubprocessError, ValueError) as exc:
            logger.debug("nvidia-smi temperature query failed: %s", exc)

    try:
        for chip, entries in psutil.sensors_temperatures().items():
            if chip not in ("amdgpu", "radeon"):
                continue
            for entry in entries:
                if entry.current and entry.current > 0:
                    celsius = entry.current
                    return celsius * 9.0 / 5.0 + 32.0 if fahrenheit else celsius
    except Exception as exc:  # pragma: no cover - platform-dependent
        logger.debug("AMD GPU temperature read failed: %s", exc)

    return 0.0


def gpu_usage() -> float:
    """NVIDIA via nvidia-smi, else AMD via the sysfs busy-percent file."""
    if _NVIDIA_SMI:
        try:
            out = subprocess.run(
                [_NVIDIA_SMI, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip().splitlines()[0])
        except (subprocess.SubprocessError, ValueError) as exc:
            logger.debug("nvidia-smi utilization query failed: %s", exc)

    try:
        with open("/sys/class/drm/card0/device/gpu_busy_percent") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def ram_usage() -> float:
    return psutil.virtual_memory().percent
