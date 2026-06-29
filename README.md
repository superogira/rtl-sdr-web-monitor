# RTL-SDR Monitor

[English](#english) · [ภาษาไทย](#ภาษาไทย)

---

<a id="english"></a>

# RTL-SDR Monitor (English)

A self-hosted web-based RTL-SDR receiver, recorder, and intelligent monitoring platform. Listen to live radio in your browser, schedule background recordings, auto-transcribe audio to text with Whisper, and get instant Telegram alerts when your keywords are detected — all from a clean, bilingual (Thai/English) web UI.

Built for shared-tuner, multi-user environments: one RTL-SDR dongle serves many listeners, each with their own VFO offset, bookmarks, and alert rules.

## ✨ Features

### Live Reception
- **Shared tuner architecture** — one RTL-SDR device serves multiple simultaneous listeners via WebSocket audio fan-out
- **Real-time audio streaming** over WebSocket with server-side demodulation
- **8 demodulation modes**: FM, WFM (wideband FM broadcast), NFM (narrow FM), AM, USB (upper sideband), LSB (lower sideband), CW (Morse), RAW (pass-through IQ)
- **3D waterfall display** rendered with WebGL
- **Per-listener VFO offset** — tune within the passband without retuning the tuner (configurable margin ratio 0.40–0.49)
- **Adjustable channel bandwidth** (1 kHz – 500 kHz), gain, frequency correction (PPM), and squelch
- **Max listeners cap** (1–128) to protect tuner load

### Recording & Scheduling
- **Manual recording** (admin-controlled toggle) of live sessions
- **Scheduled recording jobs** — cron-style schedule strings, start/stop times, always-on mode
- **Retention policies** — auto-delete old recordings by age (days), count, or total size (MB)
- **Auto Speech-to-Text** — every recording can be automatically transcribed on completion
- **Recording attribution** — each recording records which user/scheduled job created it

### Speech-to-Text (STT)
- **faster-whisper (local)** — fully offline transcription using Whisper models on your own GPU/CPU
- **OpenAI Whisper API** — cloud transcription
- **Microsoft Azure Speech**
- **Speechmatics**
- Auto audio preprocessing (resample to 16 kHz, format conversion)

### Keyword Alerting & Notifications
- **Rule-based alert engine** — define rules with keyword matching, keyword groups, regular expressions, and time-window schedules
- **Telegram notifications** — delivered to configured chat/channel IDs
- **Background worker** — polls a database-backed job queue every 2 seconds, evaluates rules against transcripts, and dispatches notifications
- **Alert history & statistics** — triage events, resend, test channels, and view analytics

### Authentication & Users
- **JWT authentication** with access and refresh tokens
- **Role-based access** — `admin` and `user` roles (admin-only settings & recording controls)
- **Per-user timezone** for correct local-time display
- **CLI user management** — create superusers and regular users from the terminal

### Bookmarks
- Save and organize favorite frequencies per user

### Internationalization
- **Bilingual UI** — Thai (default) and English, toggleable in the header

## 🏗 Architecture

| Layer | Technology |
|-------|-----------|
| Backend | Python · FastAPI · SQLAlchemy (async) · Pydantic |
| Database | SQLite (default) — configurable to PostgreSQL/MySQL via `DATABASE_URL` |
| Real-time | WebSockets (audio + spectrum fan-out) |
| DSP / Demod | NumPy-based streaming demodulator (FM/WFM/NFM/AM/SSB/CW/RAW) |
| STT | faster-whisper (local) / OpenAI / Azure / Speechmatics |
| Auth | JWT (python-jose) · bcrypt password hashing |
| Frontend | Vanilla HTML/CSS/JS · WebGL (waterfall) · no build step |
| SDR interface | `rtl_tcp` client (network) |

## 📋 Requirements

- **RTL-SDR dongle** (RTL2832U-based) and the `rtl-sdr` driver/toolkit installed
- A running **`rtl_tcp`** server your backend can reach (local or remote)
- **Python 3.10+**
- _(Optional)_ NVIDIA GPU for fast local Whisper transcription
- **Docker** + **Docker Compose** (for containerized deployment)

## 🚀 Installation

### Option A — Docker (recommended)

1. **Prepare `.env`**

   ```bash
   cp .env.example .env
   # edit .env — set JWT_SECRET_KEY, RTL_TCP_HOST/PORT, TELEGRAM_BOT_TOKEN, etc.
   ```

2. **Start the stack**

   ```bash
   docker-compose up -d
   ```

   The app is available at **http://localhost:8000**.

> The included `docker-compose.yml` maps the RTL-SDR USB device into the container and exposes port `8000`. An optional `rtl_tcp` sidecar service is included (commented out) for convenience.

### Option B — Manual (Python)

1. **Install system packages** (Debian/Ubuntu example)

   ```bash
   sudo apt-get update
   sudo apt-get install -y rtl-sdr librtlsdr-dev build-essential
   ```

2. **Install Python dependencies**

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment**

   ```bash
   cp .env.example .env
   # edit .env
   ```

4. **Run the server**

   ```bash
   uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

   The database initializes automatically on first startup (auto-runs migrations).

## ⚙️ Configuration

All settings are controlled via environment variables (see `.env.example`). The database initializes automatically on startup; there is no manual migration step.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./app.db` | Database connection string |
| `SERVER_HOST` | `0.0.0.0` | Bind host |
| `SERVER_PORT` | `8000` | Bind port |
| `JWT_SECRET_KEY` | _(required)_ | Secret used to sign JWT tokens — **set this to a strong random value** |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Access token lifetime |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |

### SDR / rtl_tcp

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `127.0.0.1` | Host of the `rtl_tcp` server |
| `RTL_TCP_PORT` | `1234` | Port of the `rtl_tcp` server |

> Device index, gain, PPM correction, antenna, and sample rate are configured at runtime via the web UI / API.

### Speech-to-Text

| Variable | Description |
|----------|-------------|
| `WHISPER_LOCAL_HOST` | Host of a local Whisper STT service (if used) |
| `WHISPER_LOCAL_PORT` | Port of a local Whisper STT service |
| `WHISPER_LOCAL_MODEL` | Whisper model size (e.g. `base`, `small`, `medium`, `large-v3`) |
| `AZURE_SPEECH_KEY` | Azure Speech resource key (if using Azure) |
| `SPEECHMATICS_API_KEY` | Speechmatics API key (if using Speechmatics) |

### Telegram Notifications

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ENABLED` | Enable/disable Telegram delivery (`true`/`false`) |

## 👤 First-run: Create an Admin User

There is no default account. Create your first admin from the CLI:

```bash
# Interactive
python -m backend.cli createsuperuser

# Non-interactive
python -m backend.cli createuser <username> <password> admin
```

Then log in at the web UI with those credentials.

### Other CLI commands

| Command | Description |
|---------|-------------|
| `python -m backend.cli createsuperuser` | Create an admin user (interactive) |
| `python -m backend.cli createuser <username> <password> [admin]` | Create a user |
| `python -m backend.cli resetpassword <username>` | Reset a user's password |
| `python -m backend.cli migrate-db` | Run database migrations manually |

## 🧭 Web UI Pages

| Route | Description |
|-------|-------------|
| `/login` | Login screen |
| `/dashboard` | Live reception console — frequency, mode, waterfall, audio, VFO, recording |
| `/recordings` | Browse, play, and read transcripts of past recordings |
| `/alert-rules` | Configure keyword rules, keyword groups, and Telegram channels |
| `/alerts` | Alert event history and triage |
| `/stats` | Alert analytics and charts |
| `/settings` | Admin settings (SDR, Recording, STT, Telegram, Keywords, Users tabs) |
| `/admin/users` | User management |

## 📡 API Highlights

- `GET /api/health` — health check
- `GET /api/sdr/status` — tuner state
- `POST /api/sdr/connect|disconnect` — connect/disconnect tuner
- `POST /api/sdr/frequency|sample-rate|gain|freq-correction|mode|channel-bw|squelch` — tuner control
- `POST /api/sdr/start-streaming|stop-streaming` — start/stop live audio
- `POST /api/sdr/start-recording|stop-recording` — manual recording (admin)
- `WS /ws` — control channel (commands + spectrum)
- `WS /ws/audio` — live audio stream
- `/api/recordings*` — recordings CRUD + transcripts
- `/api/recording-jobs*` — scheduled recording jobs
- `/api/devices*` — SDR device management
- `/api/alert-rules*`, `/api/alerts*`, `/api/alert-stats*` — alerting
- `/api/keyword-groups*`, `/api/keywords*`, `/api/channels*`, `/api/notif-groups*` — alert configuration
- `/api/auth/login|refresh|me` — authentication

## 🗂 Project Structure

```
.
├── backend/
│   ├── main.py              # FastAPI app, lifespan, WebSocket endpoints
│   ├── config.py            # Settings (env vars)
│   ├── database.py          # DB init + migrations
│   ├── cli.py               # User management CLI
│   ├── sdr/                 # Tuner control, rtl_tcp client, capture, job scheduler
│   ├── audio/               # Demodulation (FM/AM/SSB/CW/RAW), conversion, broadcaster
│   ├── alerts/              # Rule engine, worker, providers, router
│   ├── auth/                # JWT, dependencies, router
│   ├── models/              # SQLAlchemy models
│   ├── services/            # STT, Telegram, recording jobs, app settings
│   └── migrations/versions/ # Alembic-style migrations
├── frontend/
│   ├── index.html
│   ├── pages/               # dashboard, recordings, alerts, settings, ...
│   ├── css/  js/  assets/
├── docs/
│   └── WHISPER_LOCAL_STT.md
├── scripts/
│   └── reset_stuck_alerts.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## 📄 License

Released under the **MIT License**. See [LICENSE](LICENSE).

---

<a id="ภาษาไทย"></a>

# RTL-SDR Monitor (ภาษาไทย)

แพลตฟอร์มเว็บสำหรับรับสัญญาณ RTL-SDR แบบติดตั้งเอง (self-hosted) ที่รวมการรับฟัง บันทึก และเฝ้าระวังสัญญาณอัจฉริยะไว้ในที่เดียว รับฟังวิทยุสดในเบราว์เซอร์ ตั้งเวลาบันทึกเป็นพื้นหลัง แปลงเสียงเป็นข้อความอัตโนมัติด้วย Whisper และรับการแจ้งเตือนผ่าน Telegram ทันทีเมื่อตรวจพบคำค้นที่กำหนด ผ่านหน้าเว็บที่รองรับ 2 ภาษา (ไทย/อังกฤษ)

ออกแบบมาสำหรับการใช้งานแชร์ทูเนอร์แบบหลายผู้ใช้: อุปกรณ์ RTL-SDR เครื่องเดียวให้บริการผู้ฟังหลายคนพร้อมกัน แต่ละคนมี VFO offset บุ๊คมาร์ก และกฎการแจ้งเตือนของตัวเอง

## ✨ ความสามารถ

### การรับสัญญาณสด
- **สถาปัตยกรรมแชร์ทูเนอร์** — อุปกรณ์ RTL-SDR เครื่องเดียวให้บริการผู้ฟังหลายคนพร้อมกันผ่าน WebSocket
- **สตรีมเสียงเรียลไทม์** ผ่าน WebSocket พร้อมการดีมอดูเลตที่ฝั่งเซิร์ฟเวอร์
- **โหมดดีมอดูเลต 8 แบบ**: FM, WFM (FM ความถี่วิทยุกระจายเสียง), NFM (FM แคบ), AM, USB (ซ้าย), LSB (ขวา), CW (มอร์ส), RAW (IQ ดิบ)
- **จอภาพ Waterfall 3 มิติ** เรนเดอร์ด้วย WebGL
- **VFO offset รายบุคคล** — ปรับความถี่ภายในแบนด์โดยไม่ต้องเปลี่ยนความถี่หลักของทูเนอร์ (อัตราส่วน margin ปรับได้ 0.40–0.49)
- **ปรับความกว้างแชนเนล** (1 kHz – 500 kHz), ค่าขยาย (gain), การแก้ความถี่ (PPM), และ squelch
- **จำกัดจำนวนผู้ฟังสูงสุด** (1–128) เพื่อปกป้องโหลดของทูเนอร์

### การบันทึกและตั้งเวลา
- **บันทึกด้วยมือ** (สวิตช์ควบคุมโดยแอดมิน) สำหรับเซสชันสด
- **งานบันทึกตามตาราง** — สตริงตารางแบบ cron, เวลาเริ่ม/หยุด, โหมดทำงานตลอดเวลา
- **นโยบายการเก็บรักษา** — ลบบันทึกเก่าอัตโนมัติตามอายุ (วัน), จำนวน, หรือขนาดรวม (MB)
- **แปลงเสียงเป็นข้อความอัตโนมัติ** — ทุกการบันทึกสามารถแปลงเป็นข้อความได้ทันทีเมื่อบันทึกเสร็จ
- **การระบุแหล่งที่มาของการบันทึก** — บันทึกว่าสร้างโดยผู้ใช้หรืองานตามตารางใด

### แปลงเสียงเป็นข้อความ (STT)
- **faster-whisper (ในเครื่อง)** — แปลงเสียงออฟไลน์ทั้งหมดโดยใช้โมเดล Whisper บน GPU/CPU ของคุณเอง
- **OpenAI Whisper API** — แปลงเสียงบนคลาวด์
- **Microsoft Azure Speech**
- **Speechmatics**
- ประมวลผลเสียงอัตโนมัติ (resample เป็น 16 kHz, แปลงรูปแบบ)

### การแจ้งเตือนคำค้น
- **เอนจินการแจ้งเตือนแบบกฎ** — กำหนดกฎด้วยการจับคู่คำค้น, กลุ่มคำค้น, regular expression และตารางเวลา
- **การแจ้งเตือนผ่าน Telegram** — ส่งไปยัง chat/channel ID ที่กำหนด
- **Worker พื้นหลัง** — ตรวจคิวงานในฐานข้อมูลทุก 2 วินาที ประเมินกฎเทียบกับข้อความที่แปลงแล้ว และส่งการแจ้งเตือน
- **ประวัติและสถิติการแจ้งเตือน** — ตรวจสอบ event, ส่งซ้ำ, ทดสอบ channel, และดูสถิติ

### การยืนยันตัวตนและผู้ใช้
- **การยืนยันตัวด้วย JWT** พร้อม access token และ refresh token
- **สิทธิ์ตามบทบาท** — บทบาท `admin` และ `user` (ตั้งค่าและควบคุมการบันทึกสำหรับแอดมินเท่านั้น)
- **เขตเวลารายบุคคล** เพื่อแสดงเวลาท้องถิ่นที่ถูกต้อง
- **จัดการผู้ใช้ผ่าน CLI** — สร้าง superuser และผู้ใช้ทั่วไปจากเทอร์มินัล

### บุ๊คมาร์ก
- บันทึกและจัดระเบียบความถี่โปรดรายบุคคล

### การแปลภาษา
- **UI สองภาษา** — ไทย (ค่าเริ่มต้น) และอังกฤษ สลับได้ในส่วนหัว

## 🏗 สถาปัตยกรรม

| ชั้น | เทคโนโลยี |
|-------|-----------|
| Backend | Python · FastAPI · SQLAlchemy (async) · Pydantic |
| ฐานข้อมูล | SQLite (ค่าเริ่มต้น) — เปลี่ยนเป็น PostgreSQL/MySQL ได้ผ่าน `DATABASE_URL` |
| เรียลไทม์ | WebSocket (กระจายเสียง + สเปกตรัม) |
| DSP / ดีมอดูเลต | สตรีมมิ่งดีมอดูเลเตอร์บน NumPy (FM/WFM/NFM/AM/SSB/CW/RAW) |
| STT | faster-whisper (ในเครื่อง) / OpenAI / Azure / Speechmatics |
| การยืนยันตัว | JWT (python-jose) · แฮชรหัสผ่านด้วย bcrypt |
| Frontend | HTML/CSS/JS แบบดั้งเดิม · WebGL (waterfall) · ไม่มีขั้นตอน build |
| อินเทอร์เฟซ SDR | ไคลเอนต์ `rtl_tcp` (เครือข่าย) |

## 📋 ความต้องการของระบบ

- **อุปกรณ์ RTL-SDR** (ฐาน RTL2832U) พร้อมไดรเวอร์/ชุดเครื่องมือ `rtl-sdr` ที่ติดตั้งแล้ว
- เซิร์ฟเวอร์ **`rtl_tcp`** ที่ทำงานอยู่ซึ่ง backend เข้าถึงได้ (ในเครื่องหรือระยะไกล)
- **Python 3.10+**
- _(ทางเลือก)_ การ์ดจอ NVIDIA สำหรับการแปลงเสียง Whisper ในเครื่องแบบรวดเร็ว
- **Docker** + **Docker Compose** (สำหรับการปรับใช้แบบคอนเทนเนอร์)

## 🚀 การติดตั้ง

### ทางเลือก ก — Docker (แนะนำ)

1. **เตรียม `.env`**

   ```bash
   cp .env.example .env
   # แก้ไข .env — ตั้ง JWT_SECRET_KEY, RTL_TCP_HOST/PORT, TELEGRAM_BOT_TOKEN ฯลฯ
   ```

2. **เริ่มระบบ**

   ```bash
   docker-compose up -d
   ```

   เข้าใช้งานได้ที่ **http://localhost:8000**

> ไฟล์ `docker-compose.yml` ที่แนบมาจะ map อุปกรณ์ USB RTL-SDR เข้าสู่คอนเทนเนอร์และเปิดพอร์ต `8000` มีเซอร์วิส `rtl_tcp` แบบ sidecar (คอมเมนต์ไว้) ให้ใช้ตามความสะดวก

### ทางเลือก ข — ด้วยตนเอง (Python)

1. **ติดตั้งแพ็กเกจระบบ** (ตัวอย่าง Debian/Ubuntu)

   ```bash
   sudo apt-get update
   sudo apt-get install -y rtl-sdr librtlsdr-dev build-essential
   ```

2. **ติดตั้ง dependencies ของ Python**

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **ตั้งค่า environment**

   ```bash
   cp .env.example .env
   # แก้ไข .env
   ```

4. **รันเซิร์ฟเวอร์**

   ```bash
   uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

   ฐานข้อมูลจะถูกสร้างและ migrate อัตโนมัติเมื่อเริ่มต้นครั้งแรก

## ⚙️ การตั้งค่า

ค่าทั้งหมดควบคุมผ่านตัวแปร environment (ดู `.env.example`) ฐานข้อมูลจะตั้งค่าเริ่มต้นอัตโนมัติเมื่อเริ่มต้น ไม่ต้อง migrate ด้วยมือ

### หลัก

| ตัวแปร | ค่าเริ่มต้น | คำอธิบาย |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./app.db` | สตริงเชื่อมต่อฐานข้อมูล |
| `SERVER_HOST` | `0.0.0.0` | host ที่ bind |
| `SERVER_PORT` | `8000` | พอร์ตที่ bind |
| `JWT_SECRET_KEY` | _(จำเป็น)_ | secret สำหรับเซ็น JWT token — **ตั้งเป็นค่าสุ่มที่แข็งแกร่ง** |
| `JWT_ALGORITHM` | `HS256` | อัลกอริทึมเซ็น JWT |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | อายุ access token |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | อายุ refresh token |

### SDR / rtl_tcp

| ตัวแปร | ค่าเริ่มต้น | คำอธิบาย |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `127.0.0.1` | host ของเซิร์ฟเวอร์ `rtl_tcp` |
| `RTL_TCP_PORT` | `1234` | พอร์ตของเซิร์ฟเวอร์ `rtl_tcp` |

> device index, gain, PPM correction, antenna, และ sample rate ตั้งค่าขณะรันผ่านเว็บ UI / API

### แปลงเสียงเป็นข้อความ

| ตัวแปร | คำอธิบาย |
|----------|-------------|
| `WHISPER_LOCAL_HOST` | host ของบริการ Whisper STT ในเครื่อง (ถ้าใช้) |
| `WHISPER_LOCAL_PORT` | พอร์ตของบริการ Whisper STT ในเครื่อง |
| `WHISPER_LOCAL_MODEL` | ขนาดโมเดล Whisper (เช่น `base`, `small`, `medium`, `large-v3`) |
| `AZURE_SPEECH_KEY` | key ของ Azure Speech resource (ถ้าใช้ Azure) |
| `SPEECHMATICS_API_KEY` | API key ของ Speechmatics (ถ้าใช้ Speechmatics) |

### การแจ้งเตือน Telegram

| ตัวแปร | คำอธิบาย |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | token ของบอทจาก [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ENABLED` | เปิด/ปิดการส่ง Telegram (`true`/`false`) |

## 👤 การใช้งานครั้งแรก: สร้างผู้ใช้แอดมิน

ไม่มีบัญชีเริ่มต้น สร้างแอดมินคนแรกผ่าน CLI:

```bash
# แบบโต้ตอบ
python -m backend.cli createsuperuser

# แบบไม่โต้ตอบ
python -m backend.cli createuser <username> <password> admin
```

จากนั้นเข้าสู่ระบบที่เว็บ UI ด้วยข้อมูลดังกล่าว

### คำสั่ง CLI อื่นๆ

| คำสั่ง | คำอธิบาย |
|---------|-------------|
| `python -m backend.cli createsuperuser` | สร้างผู้ใช้แอดมิน (แบบโต้ตอบ) |
| `python -m backend.cli createuser <username> <password> [admin]` | สร้างผู้ใช้ |
| `python -m backend.cli resetpassword <username>` | รีเซ็ตรหัสผ่านผู้ใช้ |
| `python -m backend.cli migrate-db` | รัน migration ฐานข้อมูลด้วยมือ |

## 🧭 หน้าเว็บ UI

| เส้นทาง | คำอธิบาย |
|-------|-------------|
| `/login` | หน้าเข้าสู่ระบบ |
| `/dashboard` | คอนโซลรับสัญญาณสด — ความถี่, โหมด, waterfall, เสียง, VFO, การบันทึก |
| `/recordings` | เรียกดู, เล่น และอ่านข้อความที่แปลงแล้วของบันทึกย้อนหลัง |
| `/alert-rules` | ตั้งค่ากฎคำค้น, กลุ่มคำค้น, และ channel Telegram |
| `/alerts` | ประวัติและการตรวจสอบ event การแจ้งเตือน |
| `/stats` | สถิติและกราฟการแจ้งเตือน |
| `/settings` | ตั้งค่าแอดมิน (แท็บ SDR, Recording, STT, Telegram, Keywords, Users) |
| `/admin/users` | จัดการผู้ใช้ |

## 📡 API ที่สำคัญ

- `GET /api/health` — ตรวจสอบสถานะ
- `GET /api/sdr/status` — สถานะทูเนอร์
- `POST /api/sdr/connect|disconnect` — เชื่อมต่อ/ตัดการเชื่อมต่อทูเนอร์
- `POST /api/sdr/frequency|sample-rate|gain|freq-correction|mode|channel-bw|squelch` — ควบคุมทูเนอร์
- `POST /api/sdr/start-streaming|stop-streaming` — เริ่ม/หยุดเสียงสด
- `POST /api/sdr/start-recording|stop-recording` — บันทึกด้วยมือ (แอดมิน)
- `WS /ws` — channel ควบคุม (คำสั่ง + สเปกตรัม)
- `WS /ws/audio` — สตรีมเสียงสด
- `/api/recordings*` — CRUD บันทึก + ข้อความที่แปลง
- `/api/recording-jobs*` — งานบันทึกตามตาราง
- `/api/devices*` — จัดการอุปกรณ์ SDR
- `/api/alert-rules*`, `/api/alerts*`, `/api/alert-stats*` — การแจ้งเตือน
- `/api/keyword-groups*`, `/api/keywords*`, `/api/channels*`, `/api/notif-groups*` — การตั้งค่าการแจ้งเตือน
- `/api/auth/login|refresh|me` — การยืนยันตัว

## 🗂 โครงสร้างโปรเจกต์

```
.
├── backend/
│   ├── main.py              # แอป FastAPI, lifespan, endpoint WebSocket
│   ├── config.py            # การตั้งค่า (env vars)
│   ├── database.py          # ตั้งค่า DB + migration
│   ├── cli.py               # CLI จัดการผู้ใช้
│   ├── sdr/                 # ควบคุมทูเนอร์, ไคลเอนต์ rtl_tcp, capture, ตัวตั้งเวลางาน
│   ├── audio/               # ดีมอดูเลต (FM/AM/SSB/CW/RAW), แปลง, broadcaster
│   ├── alerts/              # เอนจินกฎ, worker, providers, router
│   ├── auth/                # JWT, dependencies, router
│   ├── models/              # โมเดล SQLAlchemy
│   ├── services/            # STT, Telegram, งานบันทึก, การตั้งค่าแอป
│   └── migrations/versions/ # migration แบบ Alembic
├── frontend/
│   ├── index.html
│   ├── pages/               # dashboard, recordings, alerts, settings, ...
│   ├── css/  js/  assets/
├── docs/
│   └── WHISPER_LOCAL_STT.md
├── scripts/
│   └── reset_stuck_alerts.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## 📄 ลิขสิทธิ์

เผยแพร่ภายใต้ **MIT License** ดูรายละเอียดที่ [LICENSE](LICENSE)