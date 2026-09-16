# 🌊 DeepCool MYSTIQUE CLI (Python)

> Headless Linux controller for the DeepCool MYSTIQUE 360's LCD screen — a Python port of the original C++/Qt tool, for a server that has no display of its own to run a GUI on.

A command-line daemon that reads CPU/GPU temperature, usage, and RAM from the host and pushes them to the AIO cooler's built-in 2.8" LCD over USB, on a fixed interval, with no desktop environment required.

<p align="center">
  <img alt="Stack" src="https://img.shields.io/badge/Stack-Python%20%2B%20psutil%20%2B%20pyusb-blue?style=for-the-badge">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge">
  <img alt="Status" src="https://img.shields.io/badge/Status-Untested%20on%20hardware-orange?style=for-the-badge">
</p>

---

## 📦 How it works

A small Python daemon opens the cooler's vendor-specific USB interface, runs its cold-boot initialization sequence, and then loops: read sensors, build a 48-byte packet, send it, sleep, repeat.

```
Host system
├── deepcool_cli.py     ←  entry point: CLI parsing, main loop
│     ├── device.py     →  finds the cooler on the USB bus
│     ├── protocol.py   →  packet format, checksum, display update
│     └── sensors.py    →  CPU/GPU temp & usage, RAM usage
└── DeepCool MYSTIQUE 360 (USB, vendor ID 0x3633)
      └── 2.8" LCD, driven entirely by what this daemon sends it
```

No network port, no web UI — this only ever talks to the one USB device and the host's own `/proc`, `/sys`, and `nvidia-smi`.

---

## 🧰 What's under the hood

| Component | Library / tool |
|-----------|----------------|
| USB communication | [`pyusb`](https://github.com/pyusb/pyusb) (libusb bindings) |
| Sensor readings | [`psutil`](https://psutil.readthedocs.io) |
| GPU metrics | `nvidia-smi` (subprocess, CSV output) — NVIDIA only; the AMD path (`gpu_busy_percent` over sysfs) is carried over from the original but unverified on real AMD hardware |
| Service management | `systemd` |
| Device permissions | `udev` rule, no root required |

---

## 🔀 Ported from C++/Qt

This started as [mymymy1303/qt-deepcool](https://github.com/mymymy1303/qt-deepcool),
a C++/Qt6 tool built specifically for the MYSTIQUE 360. This is a from-scratch
Python rewrite, not a wrapper around the original binary — every packet
offset and the checksum algorithm were transcribed from the C++ source and
verified against independent reference calculations before being treated as
correct (see the tests referenced in `protocol.py`'s docstring). Dropping Qt
entirely removes the heaviest part of the original's dependency chain (a full
Qt6 build toolchain) and the `QT_QPA_PLATFORM=offscreen` workaround its
systemd unit needed to stop Qt from looking for a display that was never
going to be there on a headless server.

**What did *not* carry over:** the GUI (`mainwindow.cpp`/`.h` in the
original) — this project is CLI-only, matching how it's actually run in
practice. Two of the GUI's own sensor functions (`getCPUUsage`,
`getGPUTemperature`, `getGPUUsage`) were unfinished placeholders in the
original (`return 45.5f; // Placeholder`) and were never part of the working
CLI path to begin with; this port is based on the CLI's own, complete sensor
implementations in `main_cli.cpp`, not the GUI's.

**A protocol documentation fix:** `PROTOCOL.md` described bytes 24-25 as an
"MHz value" with a worked example that didn't match what the shipped C++
code actually computes there. The description now matches the real formula
— see the note in `PROTOCOL.md` for the specifics.

---

## ⚠️ Status: not yet verified against real hardware

Every part of this port that could be tested without the physical cooler
has been: packet construction, the checksum algorithm, byte offsets in the
display-update packet (including the GHz-decimal encoding and its
mode-dependent CPU/GPU swap), argument parsing, and the full startup →
init → update-loop → shutdown sequence, driven end-to-end against a
simulated device. None of that reaches actual USB hardware. **The first
real test is running this against the cooler itself** — if the screen
doesn't update, or updates with garbled values, that's the next thing to
debug, not a sign the port is fundamentally wrong (the tested parts above
substantially narrow down where a remaining bug could be).

---

## 📋 Prerequisites

- Linux (developed against Ubuntu 24.04, matching the original)
- Python 3.10+
- `libusb-1.0` installed at the system level (`pyusb` binds to it; on Debian/Ubuntu: `sudo apt install libusb-1.0-0`)
- A DeepCool MYSTIQUE 240/360 connected via USB (VID `0x3633`, PID `0x0009`)

---

## 🚀 Quick start

### Install

```bash
# Clone / copy the project
sudo cp -r deepcool-display-controller/ /opt/deepcool-display-controller
cd /opt/deepcool-display-controller

# Create a virtual environment
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### udev rule (run without root)

```bash
sudo cp 99-deepcool.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

sudo useradd -r -s /usr/sbin/nologin -G plugdev deepcool
```

### Run manually

```bash
source venv/bin/activate

# List detected devices
sudo python3 deepcool_cli.py --list

# Run with CPU mode
python3 deepcool_cli.py --mode cpu --interval 1000
```

### Install as a systemd service (auto-start on boot)

```bash
sudo cp deepcool-cli.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deepcool-cli

# Check status
sudo systemctl status deepcool-cli
journalctl -u deepcool-cli -f
```

---

## ⚙️ Configuration

There's no environment-variable configuration here — this isn't a network
service with a deployment-time config, it's a single command with flags:

| Option | Default | Description |
|--------|---------|-------------|
| `-l, --list` | — | List available devices and exit |
| `-d, --device <path>` | `0` | Device path or index |
| `-i, --interval <ms>` | `1000` | Update interval in milliseconds (clamped 100-10000) |
| `-m, --mode <mode>` | `cpu` | Display mode: `cpu`, `gpu`, `gpu_focus` |
| `-r, --rotate <deg>` | `0` | Screen rotation: `0`, `90`, `180`, `270` |
| `-f, --fahrenheit` | off | Use Fahrenheit instead of Celsius |
| `-V, --verbose` | off | Log each update cycle's readings |
| `-D, --daemon` | off | Fork to background (not needed under systemd — see below) |

To change these for the systemd service, edit the `ExecStart` line in
`deepcool-cli.service` and run `sudo systemctl daemon-reload && sudo systemctl restart deepcool-cli`.

---

## 📁 Project structure

```
deepcool-display-controller/
├── deepcool_cli.py         # Entry point: CLI parsing, main loop
├── device.py                # USB device detection
├── protocol.py               # Packet format, checksum, display commands
├── sensors.py                 # CPU/GPU temp & usage, RAM usage
├── 99-deepcool.rules         # udev rule for non-root device access
├── deepcool-cli.service      # systemd unit
├── PROTOCOL.md                # USB protocol documentation
├── requirements.txt
└── README.md
```

---

## 🐳 Why this isn't Dockerized

Every other self-hosted project in this collection runs in Docker. This one
doesn't, deliberately: it exists to hold an exclusive handle on one specific
USB device and speak a raw binary protocol to it, which is the kind of job
container isolation doesn't add protection to — the whole point of the
program is to reach past that boundary to the hardware. It also touches the
kernel's own USB subsystem directly (detaching whatever kernel driver has
the interface claimed), which is more predictable to run close to the
kernel than through an extra layer of abstraction. A native systemd service
with a scoped-down `udev` rule is the more correct tool for this particular
job, not a gap to fill in later.

---

## ⚠️ Limitations

- **NVIDIA GPU metrics only actually verified.** The AMD path is carried
  over from the original and has not been tested against real AMD hardware.
- **No Intel iGPU support.** The original had a non-functional stub for
  this (a literal glob string passed where a real path was expected, never
  opened); this port leaves it out rather than keep dead code.
- **The HID device path exists but has no working implementation.** See
  `protocol.py`'s module docstring — the MYSTIQUE's USB interface is
  vendor-specific, not standard HID, so this project's own hardware is
  reached exclusively through the vendor USB path.
- **Requires a cold-boot init sequence.** After a full power cycle, the
  cooler shows its boot logo and ignores display data until initialized;
  this happens automatically on every start of this program.

---

## 🛣️ Roadmap

- [ ] Verify against real hardware and fix whatever the first real run finds
- [ ] AMD GPU support, tested rather than carried over untested
- [ ] Config file, if the CLI flags stop being enough

---

## 📜 License

MIT — see `LICENSE` file. Derived from
[mymymy1303/qt-deepcool](https://github.com/mymymy1303/qt-deepcool) (also
MIT), which was itself inspired by
[Qt-deepcool-digital](https://gitlab.com/public_projects5782906/Qt-deepcool-digital).

## 👤 Author

**Cassiano Ribeiro Carneiro** — [@cassianorcarneiro](https://github.com/cassianorcarneiro)

---

### 🤖 AI Assistance Disclosure

The C++-to-Python port, protocol verification, and documentation in this
project were developed in collaboration with [Claude](https://www.anthropic.com/claude)
by Anthropic. All project direction, requirements, and intellectual
authorship remain the work of the repository author and are governed by the
project's license.

---

> *For the AIO cooler that insists on showing you a number, even when there's no monitor around to read it off of.*
