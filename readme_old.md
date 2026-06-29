<div align="center">

# 📡 RTL-SDR Web Monitor

**Real-time Software-Defined Radio monitoring system with web-based waterfall, multi-user live audio, speech-to-text, and scheduled recordings.**

[English](#english) · [ภาษาไทย](#ภาษาไทย)

</div>
---

<a id="english"></a>

# RTL-SDR Monitor (English)

A self-hosted web-based RTL-SDR receiver, recorder, and intelligent monitoring platform. Listen to live radio in your browser, schedule background recordings, auto-transcribe audio to text with Whisper, and get instant Telegram alerts when your keywords are detected — all from a clean, bilingual (Thai/English) web UI.

Built for shared-tuner, multi-user environments: one RTL-SDR dongle serves many listeners, each with their own VFO offset, bookmarks.

## ✨ Features

### 📻 Core SDR
- **Shared tuner architecture** — one RTL-SDR device serves multiple simultaneous listeners via WebSocket audio fan-out
- **Real-time audio streaming** — over WebSocket with server-side demodulation
- **Multi-Mode Demodulation** — FM (NBFM/WBFM), AM, USB, LSB
- **RTL-TCP Support** — Connect to any `rtl_tcp` server on your network (dongle not required on the server machine)
- **Multi-Device Registry** — Manage multiple RTL-TCP devices, switch active device on-the-fly
- **Web-based Waterfall** — Real-time spectrum display with configurable FFT, 2D waterfall, and 3D waterfall (Three.js)

### 🎧 Multi-Listener Audio
- **Independent VFO per listener** — Each user can listen to a different frequency within the passband
- **Per-Listener Squelch** — Auto (adaptive) or manual squelch with hysteresis
- **Solo / Priority Mode** — Long-press mute button to set a listener as SOLO (only that VFO) or PRIORITY (dims others)
- **G.711 µ-law Audio** — Efficient bandwidth usage for live streaming

### 🎙️ Recording & Scheduling
- **Auto Recording** — Voice-activated (squelch-gated) recordings
- **Manual recording** — Per-listener manual record button
- **Scheduled recording jobs** — Always-on or time-scheduled background recording with multi-frequency support
- **In-Browser Playback** — Listen to recordings directly, with transcript viewing
- **Retention Policies** — Auto-delete old recordings by count, age, or total duration (keep transcript option)
- **Auto Speech-to-Text** — every recording can be automatically transcribed on completion

### 🤖 Speech-to-Text (STT)
- **Speechmatics** — Cloud-based high-quality transcription
- **Azure Cognitive Services** — Microsoft cloud STT
- **Whisper Local** — Self-hosted faster-whisper-server
- **Auto-Transcribe** — Automatically transcribe new recordings
- **Audio Preprocessing** — Optionally downsample/compress audio before STT to save bandwidth

### 🔔 Keyword Alerting & Notifications
- **Rule-based alert engine** — define rules with keyword matching, keyword groups and time-window schedules, active/inactive toggles
- **Telegram notifications** — delivered to configured chat/channel IDs, send alerts to Telegram channels/groups with transcription and audio link
- **Background worker** — polls a database-backed job queue every 2 seconds, evaluates rules against transcripts, and dispatches notifications
- **Alert history & statistics** — triage events, resend, test channels, and view analytics

### Authentication & Users
- **JWT authentication** with access and refresh tokens
- **Role-based access** — `admin` and `user` roles (admin-only settings & recording controls)
- **Per-user timezone** for correct local-time display
- **CLI user management** — create superusers and regular users from the terminal

### 🔖 Bookmarks
- **Personal Bookmarks** — Save frequencies with name, mode per user
- **Quick Navigation** — Click a bookmark to instantly tune to that frequency

### 🖥️ Dashboard
- **Real-time Clock** — Live frequency display with 7-segment style
- **Signal Meter (S-Meter)** — Real-time signal strength indicator
- **Spectrum + Waterfall** — 2D spectrum with configurable overlap
- **3D Waterfall** — Three.js-powered 3D spectrum visualization
- **Active Recordings** — Live status of all manual/auto/scheduled recordings
- **Status Bar** — Connection status, SDR status, listener count


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

### Internationalization
- **Bilingual UI** — Thai (default) and English, toggleable in the header

---

### 🏗 Architecture

```
┌──────────────┐    TCP:1234  ┌──────────────┐   HTTP/WS:8000   ┌────────────┐
│  USB RTL-SDR │ ◄──────────► │   rtl_tcp    │ ◄──────────────► │   Browser  │
│  (Hardware)  │   raw IQ     │   (Server)   │   spectrum/audio │  (Frontend)│
└──────────────┘              └──────┬───────┘                  └────────────┘
                                      │ TCP:1234
                                      ▼
                               ┌──────────────┐         ┌──────────────┐
                               │  FastAPI     │ ◄─────► │   SQLite     │
                               │  (Backend)   │         │  rtl_sdr.db  │
                               │  uvicorn:8000│         └──────────────┘
							   └──────────────┘
```

### Tech Stack
| Component | Technology |
|-----------|-----------|
| **Backend** | Python 3.11+, FastAPI, Uvicorn |
| **Database** | SQLite + aiosqlite (SQLAlchemy async) |
| **Signal Processing** | NumPy, SciPy |
| **Auth** | JWT (python-jose) + bcrypt |
| **STT** | faster-whisper (local) / Azure / Speechmatics |
| **Frontend** | Vanilla JS, HTML5, CSS3 |
| **Spectrum/Waterfall** | Canvas 2D + Three.js 3D |
| **Audio Streaming** | WebSocket (G.711 µ-law AUD2 frames) |
| **SDR interface** | rtl_tcp client (network) |
| **Deployment** | Docker / Docker Compose |

---

## 📋 Prerequisites

- **RTL-SDR dongle** (RTL2832U-based) and the `rtl-sdr` driver/toolkit installed
- A running **`rtl_tcp`** server your backend can reach (local or remote)

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
| `RTL_TCP_SAMPLE_RATE` | `2048000` | Sample rate (2.048 MSPS) |
| `RTL_TCP_CENTER_FREQ` | `145500000` | Initial frequency (145.5 MHz) |
| `RTL_TCP_GAIN` | `30` | Tuner gain (dB) |
| `AUTO_START_STREAMING` | `true` | Auto-connect on startup |
> Device index, gain, PPM correction, antenna, and sample rate are configured at runtime via the web UI / API.

### Speech-to-Text

| Variable | Description |
|----------|-------------|
| `STT_PROVIDER` | STT provider: speechmatics / azure / whisper_local |
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

---
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

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

---

## 📄 License

This project is open source. Released under the **MIT License**. See [LICENSE](LICENSE).

---
<div align="center">

**Made with ❤️ for the radio enthusiast community**

</div>
