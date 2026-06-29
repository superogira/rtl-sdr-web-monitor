<div align="center">

# 📡 RTL-SDR Web Monitor

**Real-time Software-Defined Radio monitoring system with web-based waterfall, multi-user live audio, speech-to-text, and scheduled recordings.**

English | [ภาษาไทย](#ภาษาไทย)

</div>

---

## 🌟 Features

### 📻 Core SDR
- **Web-based Waterfall** — Real-time spectrum display with configurable FFT, 2D waterfall, and 3D waterfall (Three.js)
- **Multi-Mode Demodulation** — FM (NBFM/WBFM), AM, USB, LSB
- **RTL-TCP Support** — Connect to any `rtl_tcp` server on your network (dongle not required on the server machine)
- **Multi-Device Registry** — Manage multiple RTL-TCP devices, switch active device on-the-fly
- **Auto-Connect & Stream** — 24/7 background operation, auto-retry on connection loss

### 🎧 Multi-Listener Audio (OpenWebRX-style)
- **Independent VFO per listener** — Each user can listen to a different frequency within the passband
- **Per-Listener Squelch** — Auto (adaptive) or manual squelch with hysteresis
- **Solo / Priority Mode** — Long-press mute button to set a listener as SOLO (only that VFO) or PRIORITY (dims others)
- **G.711 µ-law Audio** — Efficient bandwidth usage for live streaming

### 🎙️ Recording & Playback
- **Auto Recording** — Voice-activated (squelch-gated) recordings
- **Manual Recording** — Per-listener manual record button
- **Scheduled Recording Jobs** — Always-on or time-scheduled background recording with multi-frequency support
- **In-Browser Playback** — Listen to recordings directly, with transcript viewing
- **Retention Policies** — Auto-delete old recordings by count, age, or total duration (keep transcript option)

### 🤖 Speech-to-Text (STT)
- **Speechmatics** — Cloud-based high-quality transcription (Thai supported)
- **Azure Cognitive Services** — Microsoft cloud STT
- **Whisper Local** — Self-hosted faster-whisper-server (free, private, GPU-accelerated)
- **Auto-Transcribe** — Automatically transcribe new recordings
- **Audio Preprocessing** — Optionally downsample/compress audio before STT to save bandwidth

### 🔔 Alerts & Notifications
- **Keyword Alerting** — Define keywords (Thai/English) to trigger alerts when detected in transcriptions
- **Telegram Integration** — Send alerts to Telegram channels/groups with transcription and audio link
- **Keyword Management** — Case-sensitive options, active/inactive toggles

### 🔖 Bookmarks
- **Personal Bookmarks** — Save frequencies with name, mode, color, and description
- **Quick Navigation** — Click a bookmark to instantly tune to that frequency
- **Re-order & Toggle** — Drag to reorder, toggle visibility

### 👥 User System
- **JWT Authentication** — Access + refresh token auth
- **Role-Based Access** — Admin and User roles
- **Admin Panel** — User management (create, disable, delete)
- **User Timezone** — Per-user timezone preference for display

### ⚙️ Settings & Configuration
- **Web-based Settings UI** — Tabs for General, Display (Waterfall), SDR, Recording, STT, Telegram, Keywords, Users
- **VFO Passband Margin** — Configurable tuning range limit
- **Max Listeners Cap** — Limit concurrent listeners
- **Server Timezone** — Configurable timezone for job scheduler
- **Waterfall Display** — Configurable dB range, colors, FFT size

### 🖥️ Dashboard
- **Real-time Clock** — Live frequency display with 7-segment style
- **Signal Meter (S-Meter)** — Real-time signal strength indicator
- **Spectrum + Waterfall** — 2D spectrum with configurable overlap
- **3D Waterfall** — Three.js-powered 3D spectrum visualization
- **Active Recordings** — Live status of all manual/auto/scheduled recordings
- **Status Bar** — Connection status, SDR status, listener count, recording indicators

---

## 🏗️ Architecture

```
┌──────────────┐    TCP:1234    ┌──────────────┐   HTTP/WS:8000   ┌────────────┐
│  USB RTL-SDR │ ◄──────────► │   rtl_tcp    │ ◄──────────────► │   Browser  │
│  (Hardware)  │   raw IQ      │   (Server)   │   spectrum/audio │  (Frontend)│
└──────────────┘               └──────┬───────┘                  └────────────┘
                                      │ TCP:1234
                                      ▼
                               ┌──────────────┐         ┌──────────────┐
                               │  FastAPI      │ ◄─────► │   SQLite     │
                               │  (Backend)   │         │  rtl_sdr.db  │
                               │  uvicorn:8000│         └──────────────┘
```

### Tech Stack
| Component | Technology |
|-----------|-----------|
| **Backend** | Python 3.11+, FastAPI, Uvicorn |
| **Database** | SQLite + aiosqlite (SQLAlchemy async) |
| **Signal Processing** | NumPy, SciPy |
| **Auth** | JWT (python-jose) + bcrypt |
| **Frontend** | Vanilla JS, HTML5, CSS3 |
| **Spectrum/Waterfall** | Canvas 2D + Three.js 3D |
| **Audio Streaming** | WebSocket (G.711 µ-law AUD2 frames) |
| **Deployment** | Docker / Docker Compose |

---

## 📋 Prerequisites

### Backend Server (No USB required)
- Python 3.11+
- pip, venv
- Build packages: `gcc`, `libffi-dev`

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip gcc libffi-dev
```

### RTL-SDR Dongle Machine (rtl_tcp host)
- `librtlsdr`
- `rtl_tcp` program
- USB RTL-SDR dongle (RTL2832U based)

```bash
sudo apt install -y rtl-sdr librtlsdr-dev

# Blacklist kernel drivers that conflict
sudo tee /etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null

# Test
rtl_test -t
```

---

## 🚀 Installation

### Option 1: Manual Installation

```bash
# 1. Clone the repository
git clone https://github.com/superogira/rtl-sdr-web.git
cd rtl-sdr-web

# 2. Create virtual environment and install dependencies
python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your settings (at minimum: JWT_SECRET_KEY, RTL_TCP_HOST/PORT)

# 4. Create data directories
mkdir -p data data/recordings

# 5. Create admin user
./venv/bin/python3 -m backend.cli createsuperuser

# 6. Start the server
./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info
```

### Option 2: Docker

```bash
# 1. Clone and configure
git clone https://github.com/superogira/rtl-sdr-web.git
cd rtl-sdr-web
cp .env.example .env
# Edit .env with your settings

# 2. Build and run
docker compose up -d
```

Open your browser at **http://localhost:8000**

---

## ⚙️ Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `127.0.0.1` | rtl_tcp server hostname |
| `RTL_TCP_PORT` | `1234` | rtl_tcp port |
| `RTL_TCP_SAMPLE_RATE` | `2048000` | Sample rate (2.048 MSPS) |
| `RTL_TCP_CENTER_FREQ` | `145500000` | Initial frequency (145.5 MHz) |
| `RTL_TCP_GAIN` | `30` | Tuner gain (dB) |
| `JWT_SECRET_KEY` | (change me!) | JWT signing key (32+ chars) |
| `STT_PROVIDER` | `speechmatics` | STT provider: speechmatics / azure / whisper_local |
| `TELEGRAM_ENABLED` | `false` | Enable Telegram alerts |
| `AUTO_START_STREAMING` | `true` | Auto-connect on startup |

View `.env.example` for the complete list.

### CLI Tools

```bash
# Create admin user (interactive)
./venv/bin/python3 -m backend.cli createsuperuser

# Create user (non-interactive)
./venv/bin/python3 -m backend.cli createuser <username> <password> [admin]

# Reset password
./venv/bin/python3 -m backend.cli resetpassword <username>
```

---

## 🖥️ Usage

### First Login
1. Open **http://localhost:8000** in your browser
2. Log in with the admin credentials created during installation
3. You'll see the **Dashboard** with waterfall display

### Dashboard Controls
- **Mode Buttons** — FM / AM / USB / LSB
- **Bandwidth Buttons** — 1.8k / 2.4k / 3k / 6k / 12k / 25k / 50k / 200k
- **Frequency Control** — Click waterfall to set VFO, use input box for center frequency
- **Gain / PPM Sliders** — Adjust tuner parameters
- **Listen Button** — Toggle live audio
- **Record Button** — Manual recording per VFO
- **3D Toggle** — Switch between 2D and 3D waterfall views

### Recordings Page
- View all recordings with filters (frequency, mode, date, source)
- Play audio in-browser
- View STT transcripts with quality ratings
- Delete individual recordings

### Settings Page
- **General** — User timezone
- **Display** — Waterfall dB range, FFT size, colors
- **SDR** — Connection settings, device registry
- **Recording** — Auto-record, squelch, retention policies
- **STT** — Provider selection, API keys, language, audio format
- **Telegram** — Bot token, chat ID
- **Keywords** — Alert keywords management
- **Users** — User management (admin only)

### Recording Jobs
Admin users can set up scheduled recording jobs:
- **Always-on** — Continuous recording of specified frequencies
- **Scheduled** — Time-window based recording with day-of-week rules
- Each job supports multiple frequencies within a passband
- Jobs run independently from live listening

---

## 📖 Additional Documentation

- [`OPERATIONS.md`](./OPERATIONS.md) — Full operations guide (install, start/stop, migration, troubleshooting)
- [`HOW_TO_RESTART.md`](./HOW_TO_RESTART.md) — Quick restart reference
- [`docs/WHISPER_LOCAL_STT.md`](./docs/WHISPER_LOCAL_STT.md) — Setup guide for local Whisper STT

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| Can't connect to RTL-TCP | Check `rtl_tcp` is running: `ss -tlnp \| grep 1234` |
| No audio / waterfall | Ensure `AUTO_START_STREAMING=true` or click Connect + Stream |
| Audio stuttering | Check network latency to rtl_tcp; reduce sample rate |
| STT not working | Verify API keys in Settings → STT |
| SQLite errors | Check `data/` directory is writable |

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

---

## 📄 License

This project is open source. See the [LICENSE](LICENSE) file for details.
---

<div align="center">

---

# ภาษาไทย

</div>

# 📡 RTL-SDR Web Monitor — ภาษาไทย

**ระบบตรวจสอบคลื่นวิทยุ Software-Defined Radio แบบ Real-time พร้อม Waterfall ผ่านเว็บ, ฟังเสียงสดแบบหลายผู้ใช้, ถอดเสียงพูด, และตั้งเวลาอัดเสียง**

---

## 🌟 คุณสมบัติ

### 📻 ระบบ SDR หลัก
- **Waterfall บนเว็บ** — แสดงสเปกตรัมแบบ Real-time พร้อม FFT ที่ปรับค่าได้, 2D waterfall และ 3D waterfall (Three.js)
- **demod หลายโหมด** — FM (NBFM/WBFM), AM, USB, LSB
- **รองรับ RTL-TCP** — เชื่อมต่อไปยัง `rtl_tcp` server ไหนก็ได้บนเครือข่าย (ไม่ต้องเสียบ USB ที่เครื่อง Server)
- **ลงทะเบียนหลายอุปกรณ์** — จัดการ RTL-TCP หลายตัว สลับอุปกรณ์ที่ใช้งานได้ทันที
- **เชื่อมต่อและสตรีมอัตโนมัติ** — ทำงาน 24/7 พร้อมลองเชื่อมต่อใหม่หากหลุด

### 🎧 ฟังเสียงแบบหลายผู้ใช้ (OpenWebRX-style)
- **VFO แยกตามผู้ฟัง** — แต่ละคนฟังความถี่ต่างกันได้ภายใน passband เดียวกัน
- **Squelch แยกตามผู้ฟัง** — แบบ Auto (ปรับตาม noise floor) หรือ Manual พร้อม Hysteresis
- **โหมด SOLO / Priority** — กดปุ่ม mute ค้างเพื่อตั้ง VFO ใดให้เป็น SOLO (ฟังเฉพาะนั้น) หรือ PRIORITY (ลดเสียงอื่น)
- **G.711 µ-law Audio** — ใช้ bandwidth น้อยสำหรับการส่งเสียงสด

### 🎙️ อัดเสียงและเล่นซ้ำ
- **อัดอัตโนมัติ** — อัดเสียงเมื่อมีสัญญาณ (squelch-gated)
- **อัดด้วยตนเอง** — ปุ่มอัดแยกตามผู้ฟัง
- **ตั้งเวลาอัด (Recording Jobs)** — อัดแบบตลอดเวลาหรือตามตารางเวลา รองรับหลายความถี่
- **เล่นซ้ำในเบราว์เซอร์** — ฟังไฟล์อัดได้เลย พร้อมดูบทถอดเสียง
- **นโยบายเก็บไฟล์** — ลบไฟล์เก่าอัตโนมัติตามจำนวน อายุ หรือระยะเวลารวม

### 🤖 ถอดเสียงพูด (Speech-to-Text)
- **Speechmatics** — บริการ Cloud คุณภาพสูง (รองรับภาษาไทย)
- **Azure Cognitive Services** — บริการ Cloud ของ Microsoft
- **Whisper Local** — โฮสต์เองด้วย faster-whisper-server (ฟรี เป็นส่วนตัว ใช้ GPU เร่งได้)
- **ถอดเสียงอัตโนมัติ** — ถอดเสียงไฟล์บันทึกใหม่โดยอัตโนมัติ
- **ย่อขนาดก่อนส่ง** — ลดขนาดไฟล์เสียงก่อนส่ง STT เพื่อประหยัด bandwidth

### 🔔 การแจ้งเตือน
- **แจ้งเตือนคำสำคัญ** — กำหนดคำสำคัญ (ไทย/อังกฤษ) เมื่อระบบตรวจพบในบทถอดเสียง
- **แจ้งเตือนผ่าน Telegram** — ส่งแจ้งเตือนไปยัง Telegram พร้อมบทถอดเสียงและลิงก์ไฟล์เสียง
- **จัดการคำสำคัญ** — เลือกแยกตัวพิมพ์เล็ก/ใหญ่, เปิด/ปิดการใช้งาน

### 🔖 บุ๊กมาร์ก
- **บุ๊กมาร์กส่วนตัว** — บันทึกความถี่พร้อมชื่อ โหมด สี และคำอธิบาย
- **นำทางเร็ว** — คลิกบุ๊กมาร์กเพื่อปรับความถี่ทันที
- **เรียงลำดับและซ่อน** — ลากเรียงลำดับ, ซ่อน/แสดง

### 👥 ระบบผู้ใช้
- **ยืนยันตัวตนด้วย JWT** — ระบบ Access + Refresh token
- **สิทธิ์ตามบทบาท** — Admin และ User
- **จัดการผู้ใช้** — สร้าง, ปิดใช้งาน, ลบผู้ใช้
- **Timezone ส่วนตัว** — แต่ละผู้ใช้ตั้งเวลาโซนของตัวเอง

### ⚙️ การตั้งค่า
- **ตั้งค่าผ่านเว็บ** — แท็บทั่วไป, แสดงผล (Waterfall), SDR, อัดเสียง, STT, Telegram, คำสำคัญ, ผู้ใช้
- **VFO Passband Margin** — กำหนดขอบเขตการ tune
- **จำกัดผู้ฟังพร้อมกัน** — กำหนดจำนวนสูงสุด
- **Timezone ของเซิร์ฟเวอร์** — ตั้งค่าเวลาสำหรับ Job Scheduler
- **ปรับแต่ง Waterfall** — ช่วง dB, สี, ขนาด FFT

### 🖥️ แดชบอร์ด
- **นาฬิกาแบบ Real-time** — แสดงความถี่แบบ 7-segment
- **S-Meter** — แสดงระดับสัญญาณแบบ Real-time
- **สเปกตรัม + Waterfall** — 2D spectrum แบบปรับ overlap ได้
- **3D Waterfall** — แสดงสเปกตรัม 3 มิติด้วย Three.js
- **สถานะการอัด** — แสดงการอัดทั้งหมดที่กำลังทำงาน
- **แถบสถานะ** — สถานะเชื่อมต่อ, SDR, จำนวนผู้ฟัง, ตัวชี้การอัด

---

## 🏗️ สถาปัตยกรรม

```
┌──────────────┐    TCP:1234    ┌──────────────┐   HTTP/WS:8000   ┌────────────┐
│  USB RTL-SDR │ ◄──────────► │   rtl_tcp    │ ◄──────────────► │  เบราว์เซอร์ │
│  (ฮาร์ดแวร์)  │   raw IQ      │   (Server)   │   spectrum/audio │  (Frontend)│
└──────────────┘               └──────┬───────┘                  └────────────┘
                                      │ TCP:1234
                                      ▼
                               ┌──────────────┐         ┌──────────────┐
                               │  FastAPI      │ ◄─────► │   SQLite     │
                               │  (Backend)   │         │  rtl_sdr.db  │
                               │  uvicorn:8000│         └──────────────┘
```

### ส่วนประกอบของระบบ
| ส่วนประกอบ | เทคโนโลยี |
|-----------|-----------|
| **Backend** | Python 3.11+, FastAPI, Uvicorn |
| **ฐานข้อมูล** | SQLite + aiosqlite (SQLAlchemy async) |
| **ประมวลผลสัญญาณ** | NumPy, SciPy |
| **ระบบยืนยันตัวตน** | JWT (python-jose) + bcrypt |
| **Frontend** | Vanilla JS, HTML5, CSS3 |
| **สเปกตรัม/Waterfall** | Canvas 2D + Three.js 3D |
| **สตรีมเสียง** | WebSocket (G.711 µ-law AUD2) |
| **Deployment** | Docker / Docker Compose |

---

## 📋 ความต้องการของระบบ

### เซิร์ฟเวอร์ Backend (ไม่ต้องใช้ USB)
- Python 3.11+
- pip, venv
- แพ็คเกจ build: `gcc`, `libffi-dev`

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip gcc libffi-dev
```

### เครื่องที่เสียบ USB RTL-SDR (เครื่องรัน rtl_tcp)
- `librtlsdr` (ไดรเวอร์)
- โปรแกรม `rtl_tcp`
- USB RTL-SDR dongle (ตระกูล RTL2832U)

```bash
sudo apt install -y rtl-sdr librtlsdr-dev

# ปิดการใช้งานไดรเวอร์เคอร์เนลที่ขัดแย้ง
sudo tee /etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null

# ทดสอบ
rtl_test -t
```

---

## 🚀 การติดตั้ง

### วิธีที่ 1: ติดตั้งด้วยตนเอง

```bash
# 1. โคลนโปรเจกต์
git clone https://github.com/superogira/rtl-sdr-web.git
cd rtl-sdr-web

# 2. สร้าง Virtual Environment และติดตั้ง dependencies
python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. ตั้งค่า Environment
cp .env.example .env
# แก้ไข .env ด้วยค่าของคุณ (อย่างน้อย: JWT_SECRET_KEY, RTL_TCP_HOST/PORT)

# 4. สร้างโฟลเดอร์ข้อมูล
mkdir -p data data/recordings

# 5. สร้างผู้ดูแลระบบ
./venv/bin/python3 -m backend.cli createsuperuser

# 6. เริ่มเซิร์ฟเวอร์
./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info
```

### วิธีที่ 2: Docker

```bash
# 1. โคลนและตั้งค่า
git clone https://github.com/superogira/rtl-sdr-web.git
cd rtl-sdr-web
cp .env.example .env
# แก้ไข .env ด้วยค่าของคุณ

# 2. Build และรัน
docker compose up -d
```

เปิดเบราว์เซอร์ที่ **http://localhost:8000**
---

## ⚙️ การตั้งค่า

### ตัวแปร Environment (`.env`)

| ตัวแปร | ค่าเริ่มต้น | คำอธิบาย |
|--------|-----------|----------|
| `RTL_TCP_HOST` | `127.0.0.1` | ที่อยู่ของ rtl_tcp server |
| `RTL_TCP_PORT` | `1234` | พอร์ตของ rtl_tcp |
| `RTL_TCP_SAMPLE_RATE` | `2048000` | อัตราการสุ่มสัญญาณ (2.048 MSPS) |
| `RTL_TCP_CENTER_FREQ` | `145500000` | ความถี่เริ่มต้น (145.5 MHz) |
| `RTL_TCP_GAIN` | `30` | ระดับ Gain (dB) |
| `JWT_SECRET_KEY` | (ต้องเปลี่ยน!) | คีย์เซ็น JWT (อย่างน้อย 32 ตัวอักษร) |
| `STT_PROVIDER` | `speechmatics` | ผู้ให้บริการ STT: speechmatics / azure / whisper_local |
| `TELEGRAM_ENABLED` | `false` | เปิดใช้งานการแจ้งเตือน Telegram |
| `AUTO_START_STREAMING` | `true` | เชื่อมต่ออัตโนมัติเมื่อเริ่มเซิร์ฟเวอร์ |

ดู `.env.example` สำหรับรายการทั้งหมด

### คำสั่ง CLI

```bash
# สร้างผู้ดูแลระบบ (โต้ตอบ)
./venv/bin/python3 -m backend.cli createsuperuser

# สร้างผู้ใช้ (ไม่โต้ตอบ)
./venv/bin/python3 -m backend.cli createuser <username> <password> [admin]

# รีเซ็ตรหัสผ่าน
./venv/bin/python3 -m backend.cli resetpassword <username>
```

---

## 🖥️ การใช้งาน

### เข้าสู่ระบบครั้งแรก
1. เปิด **http://localhost:8000** ในเบราว์เซอร์
2. เข้าสู่ระบบด้วยบัญชีผู้ดูแลที่สร้างไว้ตอนติดตั้ง
3. จะพบ **แดชบอร์ด** พร้อม waterfall

### ปุ่มควบคุมบนแดชบอร์ด
- **ปุ่ม Mode** — FM / AM / USB / LSB
- **ปุ่ม Bandwidth** — 1.8k / 2.4k / 3k / 6k / 12k / 25k / 50k / 200k
- **ควบคุมความถี่** — คลิกที่ waterfall เพื่อตั้ง VFO, ใช้ช่องใส่ค่าสำหรับ Center Frequency
- **แถบเลื่อน Gain / PPM** — ปรับค่าตัวรับสัญญาณ
- **ปุ่มฟังสด** — เปิด/ปิดการฟังเสียงสด
- **ปุ่มอัด** — อัดเสียงแยกตาม VFO
- **สลับ 3D** — เปลี่ยนระหว่าง 2D และ 3D waterfall

### หน้าอัดเสียง
- ดูรายการอัดเสียงทั้งหมด พร้อมกรอง (ความถี่, โหมด, วันที่, แหล่ง)
- เล่นไฟล์เสียงในเบราว์เซอร์
- ดูบทถอดเสียง (transcript) พร้อมให้คะแนนคุณภาพ
- ลบไฟล์อัดเสียงทีละรายการ

### หน้าตั้งค่า
- **ทั่วไป** — Timezone ของผู้ใช้
- **แสดงผล** — ช่วง dB ของ waterfall, ขนาด FFT, สี
- **SDR** — การเชื่อมต่อ, การจัดการอุปกรณ์
- **อัดเสียง** — อัดอัตโนมัติ, squelch, นโยบายเก็บไฟล์
- **STT** — เลือกผู้ให้บริการ, API keys, ภาษา, รูปแบบไฟล์เสียง
- **Telegram** — Token บอท, Chat ID
- **คำสำคัญ** — จัดการคำสำคัญที่ต้องการแจ้งเตือน
- **ผู้ใช้** — จัดการผู้ใช้ (เฉพาะ Admin)

### ตั้งเวลาอัด (Recording Jobs)
ผู้ดูแลระบบสามารถตั้งเวลาอัดเสียงแบบอัตโนมัติ:
- **ตลอดเวลา** — อัดความถี่ที่กำหนดต่อเนื่อง
- **ตามตาราง** — อัดตามช่วงเวลาที่กำหนด พร้อมวันในสัปดาห์
- แต่ละ Job รองรับหลายความถี่ภายใน passband
- Jobs ทำงานแยกจากการฟังสด

---

## 📖 เอกสารเพิ่มเติม

- [`OPERATIONS.md`](./OPERATIONS.md) — คู่มือปฏิบัติการฉบับเต็ม (ติดตั้ง, เริ่ม/หยุด, migration, แก้ปัญหา)
- [`HOW_TO_RESTART.md`](./HOW_TO_RESTART.md) — คำสั่ง restart ฉบับย่อ
- [`docs/WHISPER_LOCAL_STT.md`](./docs/WHISPER_LOCAL_STT.md) — คู่มือติดตั้ง Whisper Local STT

---

## 🔧 การแก้ปัญหาเบื้องต้น

| ปัญหา | วิธีแก้ |
|-------|--------|
| เชื่อมต่อ RTL-TCP ไม่ได้ | ตรวจสอบว่า `rtl_tcp` กำลังรัน: `ss -tlnp \| grep 1234` |
| ไม่มีเสียง / waterfall | ตรวจสอบว่า `AUTO_START_STREAMING=true` หรือกด Connect + Stream |
| เสียงกระตุก | ตรวจสอบความหน่วงเครือข่ายไปยัง rtl_tcp; ลด sample rate |
| STT ไม่ทำงาน | ตรวจสอบ API keys ใน Settings → STT |
| ข้อผิดพลาด SQLite | ตรวจสอบว่าโฟลเดอร์ `data/` สามารถเขียนได้ |

---

## 🤝 การมีส่วนร่วม

เรายินดีรับ Pull Request! หากพบปัญหาหรือมีข้อเสนอแนะ สามารถเปิด Issue ได้ที่ GitHub

---

## 📄 ลิขสิทธิ์

โปรเจกต์นี้เป็นซอฟต์แวร์โอเพนซอร์ส ดูรายละเอียดในไฟล์ [LICENSE](LICENSE)

---

<div align="center">

**Made with ❤️ for the radio enthusiast community**

</div>