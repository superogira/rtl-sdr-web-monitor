# USB RTL-SDR Direct Access Setup

This guide covers setting up RTL-SDR Web Monitor to read IQ data **directly from a USB RTL-SDR dongle** — without needing a separate `rtl_tcp` server process.

> **Note:** The system supports **both** connection types simultaneously:
> - `rtl_tcp` — connect to a remote/local `rtl_tcp` server over TCP/IP (original method)
> - `usb` — read directly from a USB RTL-SDR dongle via `pyrtlsdr` (new method)

---

## 📋 Prerequisites

### Hardware
- RTL-SDR dongle (RTL2832U-based, e.g. R820T/R820T2 tuner)
- USB port on the server machine

### OS
- Ubuntu 22.04 / 24.04 LTS (or similar Debian-based Linux)
- Python 3.11+ (Ubuntu 24.04 ships Python 3.12, which works fine)

---

## 🔧 Step 1: Install System Packages

```bash
# RTL-SDR drivers + development libraries
sudo apt update
sudo apt install -y rtl-sdr librtlsdr-dev

# Python + build tools
sudo apt install -y python3-venv python3-pip gcc libffi-dev python3-dev
```

---

## 🔧 Step 2: Blacklist Conflicting Kernel Drivers

The Linux kernel may auto-load DVB-T drivers that conflict with RTL-SDR. Blacklist them:

```bash
sudo tee /etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null
```

---

## 🔧 Step 3: Set Up USB Permissions (udev rules)

Allow non-root users to access the USB dongle:

```bash
sudo tee /etc/udev/rules.d/rtl-sdr.rules <<'EOF'
# RTL-SDR USB devices (Realtek RTL2832U)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2830", GROUP="plugdev", MODE="0666"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger
```

> **Note:** For other RTL-SDR vendors (e.g. Nooelec), add their USB vendor/product IDs. Run `lsusb` to find your dongle's IDs.

---

## ✅ Step 4: Verify the Dongle

```bash
# List connected devices (look for RTL2838)
lsusb | grep -i rtl

# Test the dongle
rtl_test -t
```

Expected output:
```
Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000005

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 ...
```

---

## 🚀 Step 5: Install RTL-SDR Web Monitor

```bash
# Clone or copy the project
cd /home/youruser
# (If using git: git clone <repo> rtl-sdr-web)
cd rtl-sdr-web

# Create virtual environment
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env: set JWT_SECRET_KEY to a strong random value

# Create data directories
mkdir -p data data/recordings

# Create admin user
./venv/bin/python3 -m backend.cli createuser admin yourpassword admin

# Start the server
./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

---

## 📡 Step 6: Add a USB Device in the Web UI

1. Open **http://your-server-ip:8000** and log in
2. Go to **Settings → SDR**
3. Click **Add Device**
4. Select type: **USB (Direct)**
5. Fill in:
   - **Name**: e.g. "Roof Dongle"
   - **Device Index**: `0` (default = first dongle)
   - **Serial Number**: e.g. `00000005` (optional but recommended — more reliable than index)
   - **Gain**, **PPM**, **Waterfall dB range** as needed
6. **Save** → **Activate** the device

Or via API:

```bash
# Login and get token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"yourpassword"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create USB device
curl -s -X POST http://localhost:8000/api/sdr/devices \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name": "USB Dongle",
    "type": "usb",
    "config": {
      "device_index": 0,
      "serial": "00000005",
      "gain": 30,
      "freq_correction_ppm": 0,
      "waterfall_db_min": -110,
      "waterfall_db_max": -55
    }
  }'

# Activate it (replace DEVICE_ID)
curl -s -X POST http://localhost:8000/api/sdr/devices/DEVICE_ID/activate \
  -H "Authorization: Bearer $TOKEN"

# Start streaming
curl -s -X POST http://localhost:8000/api/sdr/connect \
  -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://localhost:8000/api/sdr/stream/start \
  -H "Authorization: Bearer $TOKEN"
```

---

## 🔍 How It Works

```
┌──────────────┐     USB      ┌──────────────────┐   HTTP/WS:8000   ┌────────────┐
│ USB RTL-SDR  │ ◄──────────► │  RTL-SDR Web     │ ◄──────────────► │  Browser   │
│  (Hardware)  │   raw IQ     │  (pyrtlsdr)      │   spectrum/audio │  (Frontend)│
└──────────────┘              └──────┬───────────┘                  └────────────┘
                                     │
                                     ▼
                              ┌──────────────┐
                              │   SQLite     │
                              │  rtl_sdr.db  │
                              └──────────────┘
```

### Architecture
- **`backend/sdr/rtl_usb.py`** — `RtlUsbClient` class: reads IQ samples directly from USB via `pyrtlsdr` (librtlsdr Python bindings)
- **`backend/sdr/connection_pool.py`** — Shared connection pool that works with both `RtlTcpClient` and `RtlUsbClient`
- Blocking USB reads run in a `ThreadPoolExecutor` so the asyncio event loop is never blocked
- The connection pool, priority arbitration, and IQ fan-out logic work identically for both `rtl_tcp` and `usb` devices

### Device Identification
- **Serial Number** (recommended): Each dongle has a unique serial in its EEPROM. Use this when you have multiple dongles.
- **Device Index**: Integer (`0`, `1`, `2`, ...). **Warning**: Index can change on reboot/re-plug. Serial is more stable.
- If both are provided, serial takes precedence.

---

## 🐳 Docker Notes

To use USB RTL-SDR inside Docker:

1. Uncomment the USB device passthrough in `docker-compose.yml`:
```yaml
services:
  rtl-sdr-web:
    # ...
    devices:
      - /dev/bus/usb:/dev/bus/usb
```

2. Install `librtlsdr` in the Dockerfile:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    rtl-sdr librtlsdr-dev && rm -rf /var/lib/apt/lists/*
```

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| `usb_open error -3` | Install udev rules (Step 3) or re-plug the dongle |
| `No module named 'pkg_resources'` | Install `setuptools<81`: `pip install "setuptools<81"` |
| `pyrtlsdr is not installed` | `pip install pyrtlsdr` + ensure `librtlsdr-dev` is installed |
| Device not found | Run `rtl_test -t` to verify; check `lsusb` output |
| Wrong device opens | Use serial number instead of index |
| PLL not locked warning | Normal at startup; resolves once frequency is set |
| Detached/Reattached kernel driver | Normal — pyrtlsdr auto-detaches the kernel DVB driver |

---

## 📖 Comparison: rtl_tcp vs USB (Direct)

| Feature | rtl_tcp | USB (Direct) |
|---------|---------|-------------|
| **Extra process** | Yes (`rtl_tcp` daemon) | No |
| **Network** | TCP/IP (remote capable) | USB only (local) |
| **Latency** | Slightly higher (TCP) | Lower (direct USB) |
| **Multi-machine** | ✅ Dongle on machine A, server on B | ❌ Dongle must be on the server |
| **Setup complexity** | Medium (run rtl_tcp) | Simple (just plug in) |
| **Docker** | Network connection | USB passthrough required |

Both modes are fully supported and can coexist — add multiple devices of different types and switch between them.