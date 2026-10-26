# OPERATIONS.md — RTL-SDR Web Monitor

คู่มือปฏิบัติการสำหรับติดตั้ง เริ่ม/หยุด/รีสตาร์ทระบบ และย้ายไปใช้งานบนเครื่องที่มี USB RTL-SDR

---

## 1. ภาพรวมสถาปัตยกรรม

ระบบมี 3 ส่วนประกอบหลัก:

```
┌──────────────┐    TCP:1234    ┌──────────────┐   HTTP/WS:8000   ┌────────────┐
│  USB RTL-SDR │ ◄──────────► │   rtl_tcp    │ ◄──────────────► │  เบราว์เซอร์ │
│  (Hardware)  │   raw IQ      │   (Server)   │   spectrum/audio │  (Frontend)│
└──────────────┘               └──────┬───────┘                  └────────────┘
                                      │ TCP:1234
                                      ▼
                               ┌──────────────┐         ┌──────────────┐
                               │  FastAPI     │ ◄─────► │   SQLite     │
                               │  (Backend)   │         │  rtl_sdr.db  │
                               │  uvicorn:8000│         └──────────────┘
                               └──────────────┘
```

| Component        | หน้าที่                                     | Default Port | ต้องมี USB? |
|------------------|--------------------------------------------|--------------|-------------|
| **rtl_tcp**      | อ่าน IQ data จาก USB RTL-SDR ส่งผ่าน TCP   | 1234         | ✅ ใช่      |
| **FastAPI/uvicorn** | REST API + WebSocket, demod, record, auth | 8000         | ❌ ไม่      |
| **SQLite**       | เก็บ users, recordings, bookmarks, keywords | — (ไฟล์)     | ❌ ไม่      |

> 💡 **สำคัญ**: rtl_tcp และ Backend ไม่จำเป็นต้องอยู่เครื่องเดียวกัน
> ตัวอย่าง: เสียบ USB RTL-SDR ที่ Raspberry Pi (รัน rtl_tcp) แล้วให้ Backend รันที่เครื่องอื่นเชื่อมผ่าน LAN

---

## 2. Prerequisites (System Dependencies)

### 2.1 สำหรับเครื่องที่รัน Backend (ไม่จำเป็นต้องมี USB)

- **Python 3.11+**
- **pip**, **venv**
- แพ็คเกจ build: `gcc`, `libffi-dev` (สำหรับ bcrypt/cryptography)

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip gcc libffi-dev
```

### 2.2 สำหรับเครื่องที่รัน rtl_tcp (ต้องมี USB RTL-SDR)

- **librtlsdr** (rtl-sdr ไดรเวอร์)
- โปรแกรม `rtl_tcp`

```bash
# Ubuntu/Debian
sudo apt install -y rtl-sdr librtlsdr-dev

# ตรวจว่าเห็นอุปกรณ์
lsusb | grep -i rtl
# ควรเห็นประมาณ: Realtek Semiconductor Corp. RTL2838UHIDIR
```

### 2.3 ไดรเวอร์ USB (udev rules)

เคอร์เนล Linux มักจะโหลด `dvb_usb_rtl28xxu` ครอบ RTL-SDR ไว้ ทำให้ rtl_tcp เปิดไม่ได้ ต้อง blacklist:

```bash
# สร้าง blacklist
sudo tee /etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

# Reload แล้วถอดเสียบ USB ใหม่
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null

# ทดสอบ
rtl_test -t
# ควรเห็น: "Found 1 device(s):\n  0:  Realtek, RTL2838UHIDIR, ..."
```

---

## 3. การติดตั้งเริ่มต้น (Fresh Install)

```bash
# 1. Clone repo
git clone https://github.com/superogira/rtl-sdr-web-monitor.git rtl-sdr-web
cd rtl-sdr-web

# 2. สร้าง venv + ติดตั้ง dependencies
python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. สร้างไฟล์ config
cp .env.example .env

# 4. แก้ .env (อย่างน้อย JWT_SECRET_KEY)
nano .env
#   - ตั้ง JWT_SECRET_KEY เป็นสตริงสุ่ม 32+ ตัวอักษร
#   - ตั้ง RTL_TCP_HOST / RTL_TCP_PORT ให้ตรงกับเครื่อง rtl_tcp

# 5. สร้าง directory สำหรับ DB + recordings
mkdir -p data data/recordings

# 6. สร้าง admin user
./venv/bin/python3 -m backend.cli createuser admin admin123 admin

# 7. ทดสอบรัน (ดู section ถัดไป)
```

---

## 4. คำสั่ง Start / Stop / Restart / Status

### Mode A: Manual (แนะนำสำหรับ dev / Raspberry Pi)

> สมมติ working directory = `/root/workspace/rtl-sdr-web`
> ปรับ path ตามจริงของเครื่องคุณ

#### Start — ขั้นตอนที่ต้องทำตามลำดับ

```bash
# ขั้นที่ (1) เริ่ม rtl_tcp ก่อน (ที่เครื่องเสียบ USB)
rtl_tcp -a 0.0.0.0 -p 1234 &
# -a 0.0.0.0 = รับจาก IP ใดก็ได้, ถ้าใช้สาย LAN ให้ใช้ IP ของเครื่อง
# ทดสอบ: telnet localhost 1234 ควรต่อได้

# ขั้นที่ (2) เริ่ม Backend
cd /root/workspace/rtl-sdr-web
nohup ./venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    > /tmp/uvicorn.log 2>&1 &
disown   # ป้องกันถูก kill เมื่อปิด terminal

# ขั้นที่ (3) ตรวจสอบว่ารันสำเร็จ
sleep 3
curl http://localhost:8000/api/health
# ควรได้: {"status":"ok",...}
```

#### Stop

```bash
# หยุด Backend
pkill -f "uvicorn backend.main"

# หยุด rtl_tcp
pkill -f "rtl_tcp"

# หยุดทั้งคู่ในคำสั่งเดียว
pkill -f "uvicorn backend.main"; pkill -f "rtl_tcp"
```

#### Restart (Backend เท่านั้น — เมื่อแก้ Python code)

```bash
pkill -f "uvicorn backend.main"
sleep 2
cd /root/workspace/rtl-sdr-web
nohup ./venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    > /tmp/uvicorn.log 2>&1 &
disown
```

> ⚠️ แก้ไฟล์ **CSS/JS/HTML** ไม่ต้อง restart — browser แค่ refresh (Ctrl+Shift+R)
> ต้อง restart เฉพาะเมื่อแก้ไฟล์ **Python (.py)**

#### Status / Health Check

```bash
# 1. ตรวจ process รันอยู่ไหม
pgrep -af "uvicorn backend.main"
pgrep -af "rtl_tcp"

# 2. ตรวจ HTTP health
curl -s http://localhost:8000/api/health | python3 -m json.tool

# 3. ตรวจสถานะ RTL-TCP + streaming
curl -s http://localhost:8000/api/sdr/status | python3 -m json.tool

# 4. ตรวจ audio pipeline (มี chunks ไหลไหม)
curl -s http://localhost:8000/api/audio/diag | python3 -m json.tool
#   "rtl_connected": true  → Backend ต่อ rtl_tcp สำเร็จ
#   "streaming": true      → กำลังอ่าน IQ + demod
#   "chunks_sent": N       → มี audio frame ส่งออกแล้ว N ชิ้น
#   "last_audio_ago": 0.12 → เสียงยังไหลอยู่ (เลขน้อย = สด)

# 5. ดู log แบบ real-time
tail -f /tmp/uvicorn.log
```

---

### Mode B: Docker Compose

> เหมาะสำหรับการ deploy บนเครื่องที่ติดตั้ง Docker แล้ว
> USB RTL-SDR ต้องอยู่ที่เครื่องเดียวกับ container

#### การติดตั้ง Docker + Docker Compose
```bash
# Ubuntu/Debian
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

#### แก้ docker-compose.yml ให้ใช้ USB

 uncomment ส่วน `devices:` และเพิ่ม rtl_tcp service:

```yaml
services:
  rtl-sdr-web:
    build: .
    container_name: rtl-sdr-monitor
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env
    devices:
      - /dev/bus/usb:/dev/bus/usb
    restart: unless-stopped
```

#### Start / Stop / Restart

```bash
# Start (build + run ครั้งแรกใช้เวลา 2-5 นาที)
docker compose up -d --build

# ดู log
docker compose logs -f rtl-sdr-web

# Restart
docker compose restart rtl-sdr-web

# Stop
docker compose down

# Rebuild หลังแก้ code
docker compose up -d --build
```

#### Status
```bash
docker compose ps
docker compose top
curl http://localhost:8000/api/health
```

---

## 5. การย้ายไปเครื่องใหม่ที่มี USB RTL-SDR

มี 2 topology ให้เลือก:

### Topology 1: ทุกอย่างในเครื่องเดียว (ง่ายสุด)

```
[เครื่อง Linux + USB RTL-SDR]
   ├─ rtl_tcp (127.0.0.1:1234)
   ├─ Backend uvicorn (0.0.0.0:8000)
   └─ เปิดเบราว์เซอร์ http://localhost:8000
```

#### ขั้นตอน

```bash
# === ที่เครื่องใหม่ ===

# 1. ติดตั้ง rtl-sdr (section 2.2, 2.3)
sudo apt install -y rtl-sdr python3.11 python3.11-venv gcc libffi-dev git
# สร้าง blacklist (section 2.3) แล้วถอดเสียบ USB ใหม่

# 2. Clone + ติดตั้ง Backend (section 3)
git clone https://github.com/superogira/rtl-sdr-web-monitor.git rtl-sdr-web
cd rtl-sdr-web
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env

# 3. แก้ .env — ใช้ localhost
#    RTL_TCP_HOST=127.0.0.1
#    RTL_TCP_PORT=1234
#    JWT_SECRET_KEY=<สุ่ม 32+ ตัวอักษร>
nano .env

# 4. สร้าง user admin
./venv/bin/python3 -m backend.cli createuser admin admin123 admin

# 5. เริ่ม rtl_tcp แล้วทดสอบก่อน
rtl_tcp -a 0.0.0.0 -p 1234 &
# ในอีก terminal ทดสอบ: rtl_test
#   ควรเห็น "Sampling at 2048000 S/s" โดยไม่มี error

# 6. เริ่ม Backend
nohup ./venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    > /tmp/uvicorn.log 2>&1 &
disown

# 7. เปิดเบราว์เซอร์
#    http://localhost:8000  (หรือ IP เครื่องนี้)
#    login: admin / admin123
#    กด "🔗 เชื่อมต่อ" → "▶ รับสัญญาณ" → "🔈 ฟังสด"
```

### Topology 2: แยกเครื่อง (RTL-SDR ที่ Pi, Backend ที่เครื่องอื่น)

```
[Raspberry Pi + USB]          [Server / Laptop]
  rtl_tcp                       Backend uvicorn
  192.168.1.50:1234   ────►     RTL_TCP_HOST=192.168.1.50
                                0.0.0.0:8000
                                       ▲
                                  เบราว์เซอร์
```

#### ขั้นตอน

```bash
# === ที่ Raspberry Pi ===
sudo apt install -y rtl-sdr
# ทำ blacklist (section 2.3)
rtl_tcp -a 0.0.0.0 -p 1234 &
# จด IP: เช่น 192.168.1.50

# === ที่เครื่อง Backend ===
git clone https://github.com/superogira/rtl-sdr-web-monitor.git rtl-sdr-web
cd rtl-sdr-web
python3.11 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env

# แก้ .env — ชี้ไป IP ของ Pi
#   RTL_TCP_HOST=192.168.1.50
#   RTL_TCP_PORT=1234
nano .env

./venv/bin/python3 -m backend.cli createuser admin admin123 admin

nohup ./venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    > /tmp/uvicorn.log 2>&1 &
disown

# ที่เครื่อง Backend ทดสอบการเชื่อมต่อไป Pi
curl -s http://localhost:8000/api/health
# แล้ว login ที่หน้าเว็บ กด "🔗 เชื่อมต่อ"
# ถ้า rtl_connected = true → สำเร็จ
```

### ย้ายข้อมูลเก่า (ถ้าต้องการ)

```bash
# Backup จากเครื่องเดิม
cd /root/workspace/rtl-sdr-web
tar czf sdr-backup.tar.gz data/

# Copy ไปเครื่องใหม่
scp sdr-backup.tar.gz user@new-host:~/rtl-sdr-web/

# Restore ที่เครื่องใหม่
cd ~/rtl-sdr-web
tar xzf sdr-backup.tar.gz
# หมายเหตุ: DB มี password hash ของเดิม ไม่ต้อง createuser ใหม่
```

---

## 6. สรุปสถานการณ์ทั่วไป (Cheatsheet)

| สถานการณ์                     | คำสั่ง                                                                 |
|-------------------------------|------------------------------------------------------------------------|
| เริ่มระบบครั้งแรก             | ทำตาม section 3 → 5                                                    |
| แก้ Python code → รีสตาร์ท    | `pkill -f "uvicorn backend.main"; sleep 2; nohup ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info > /tmp/uvicorn.log 2>&1 & disown` |
| แก้ JS/CSS/HTML               | ไม่ต้อง restart — refresh browser (Ctrl+Shift+R)                       |
| USB หลุด → เสียบใหม่          | รีสตาร์ท rtl_tcp แล้วกด "🔗 เชื่อมต่อ" ใหม่ในเว็บ                       |
| ดูสถานะทุกอย่าง              | `curl -s localhost:8000/api/audio/diag \| python3 -m json.tool`        |
| ดู log                        | `tail -f /tmp/uvicorn.log`                                             |
| ลืม password admin           | `./venv/bin/python3 -c "import sqlite3,bcrypt; ..."` (ดู AI_CONTEXT.md) |
| รีเซ็ต DB ทำใหม่             | `rm data/rtl_sdr.db && ./venv/bin/python3 -m backend.cli createuser admin admin123 admin` |

---

## 7. Troubleshooting

### "Connection refused" ตอนกดเชื่อมต่อ

```bash
# เช็ค rtl_tcp รันอยู่ไหม
pgrep -af rtl_tcp
# ถ้าไม่มี → เริ่ม: rtl_tcp -a 0.0.0.0 -p 1234 &

# เช็ค port ตรงกับ .env ไหม
grep RTL_TCP .env
curl -s localhost:8000/api/audio/diag | python3 -m json.tool
#   ดู rtl_connected: false → แปลว่า Backend ต่อ rtl_tcp ไม่ได้
```

### "usb_open error -6" หรือ "Device or resource busy"

อุปกรณ์ USB ถูกเคอร์เนลจองไว้ ทำ blacklist (section 2.3):

```bash
sudo modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830
# ถอดเสียบ USB ใหม่
# ทดสอบ: rtl_test -t
```

### มี spectrum แต่ไม่มีเสียง / chunks_sent ไม่เพิ่ม

```bash
# 1. ตรวจ signal level อาจต่ำกว่า squelch
curl -s localhost:8000/api/audio/diag | python3 -m json.tool
#   signal_level_db ต้อง > SQUELCH_LEVEL (-70 default)

# 2. ลองลด squelch ในเว็บ (slider ฝั่งซ้ายล่าง) หรือ set ใน .env
#    SQUELCH_LEVEL=-100

# 3. ปรับ gain สูงขึ้นในเว็บ (slider GAIN)
```

### WebSocket สลับแดงเขียว (flap)

1. ดู backend log: `tail -f /tmp/uvicorn.log | grep "🔊"`
2. ดู DevTools Console (F12) ใน browser — มีข้อความ `🔊 [audio]` แสดง state
3. ตรวจว่า rtl_connected และ streaming เป็น true: `curl -s localhost:8000/api/audio/diag`
4. ดู `last_audio_ago` ถ้าเลขใหญ่ = audio ไม่ไหล ให้ restart Backend
5. ถ้ายัง flap ใน Docker → อาจเป็น proxy timeout ตั้งค่า reverse proxy ให้ allow WebSocket upgrade

### ไม่ได้ยินเสียง (แม้ chunks_sent เพิ่ม)

1. ที่เบราว์เซอร์ → F12 → Console → กด "🔈 ฟังสด"
2. ดู `🔊 [audio] frame #1: XXX bytes` — ถ้ามี = frame ถึง client แล้ว
3. ดู audio-status overlay (ขวาบน) `Nf | Nu | N.NNNs`:
   - `f` เพิ่ม = frames มาถึง
   - `u` เพิ่ม = underrun (เสียงกระตุก)
4. ถ้า f เพิ่มแต่ไม่ได้ยิน → ปัญหาที่ Web Audio API:
   - คลิกหน้าเว็บก่อน (autoplay policy)
   - เช็ค volume slider ในเว็บ
   - ดูว่า browser block AudioContext (ดู icon ใน address bar)

### รีเซ็ตทุกอย่างกลับเริ่มต้น

```bash
cd /root/workspace/rtl-sdr-web
pkill -f "uvicorn backend.main"
pkill -f "rtl_tcp"
rm -f data/rtl_sdr.db
rm -rf data/recordings/*
./venv/bin/python3 -m backend.cli createuser admin admin123 admin
# restart ตาม section 4
```

---

## 8. ระบบถอดเสียง (Speech-to-Text / STT)

ระบบถอดเสียงแปลงเสียงวิทยุเป็นข้อความ รองรับหลาย provider:

### 8.1 Provider ที่รองรับ

| Provider | รายละเอียด | ต้องติดตั้งเพิ่ม? |
|----------|-----------|------------------|
| **Speechmatics** (Cloud) | API แม่นยำ รองรับไทย ต้องมี API Key และอินเทอร์เน็ต | ไม่ (มี `httpx` ใน `requirements.txt`) |
| **Whisper Local** (API) | faster-whisper-server บนเครือข่ายท้องถิ่น ฟรี รองรับ GPU/CPU | ต้องติดตั้ง server แยก (ดู `docs/WHISPER_LOCAL_STT.md`) |
| **Azure** Cognitive Services Speech | บริการ Cloud ของ Microsoft รองรับไทย | ไม่ (มี SDK ใน `requirements.txt`) |
| **Groq Whisper** (Cloud) | OpenAI-compatible API เร็วมาก รองรับไทย | ไม่ (มี `httpx` ใน `requirements.txt`) |
| **MaxPlus AI** (Cloud) | OpenAI-compatible API ([api.maxplus-ai.cc](https://maxplus-ai.cc/docs/api)) เลือกโมเดลได้จาก `GET /v1/models` เช่น `whisper-large-v3`, `grok-stt` (คีย์รูปแบบ `ccsk-…`) | ไม่ (มี `httpx` ใน `requirements.txt`) |

### 8.2 การตั้งค่า STT (ผ่านหน้าเว็บ)

1. ไปที่ **⚙️ ตั้งค่า → 📝 ถอดเสียง**
2. เลือก Provider (Speechmatics / Whisper Local / Azure / Groq / MaxPlus AI)
3. สำหรับ Speechmatics:
   - ใส่ API Key (สมัครได้ที่ https://speechmatics.com)
   - เลือก Language: ไทย / English / Auto
   - **Operating Point**: `standard` (เร็ว ประหยัดเครดิต) หรือ `enhanced` (แม่นยำ ช้า)
   - **Diarization**: `none` (ไม่แยก) / `speaker` (แยกผู้พูด) / `channel` (แยกช่องสัญญาณ — เหมาะกับวิทยุสื่อสาร)
   - **Enable Entities**: ดึง named entities (ชื่อคน/สถานที่/องค์กร)
4. สำหรับ **MaxPlus AI**:
   - ใส่ API Key (สร้างได้จาก dashboard ของ MaxPlus AI — รูปแบบ `ccsk-…`)
   - เลือก **Model** จากรายการที่ดึงมาแบบสดผ่าน `GET /v1/models` (กดปุ่ม **🔄 โหลดโมเดลจาก API** เพื่อรีเฟรช) เช่น `whisper-large-v3` หรือ `grok-stt`
   - ไฟล์เสียงต้องไม่เกิน 25 MB — แนะนำเปิดตัวเลือกบีบอัด MP3 สำหรับไฟล์ยาว
5. **ถอดเสียงอัตโนมัติ**: เปิดเพื่อถอดทุกครั้งที่อัดเสร็จ หรือปิดเพื่อกดถอดเองในหน้า Recordings
6. กด **💾 บันทึก**

> 💡 ค่าทั้งหมดเก็บในฐานข้อมูล (ตาราง `app_settings`) ใช้ร่วมกันสำหรับผู้ใช้ทุกคน

### 8.3 การใช้งาน

- **ถอดอัตโนมัติ**: เปิดใน Settings → STT → ระบบจะถอดทุก recording ที่อัดเสร็จอัตโนมัติ (แสดงสถานะ `🔄AUTO`)
- **ถอดเอง**: ไปที่หน้า **📋 รายการบันทึก** → กดปุ่ม `📝 ถอดเสียง` ที่รายการใดก็ได้
- ดูผลลัพธ์: transcript จะแสดงในหน้า Recordings พร้อม badge บอก provider เช่น `[speechmatics]`
- ถ้าถอดล้มเหลว: จะแสดง `⚠️ <error message>` สีแดง

### 8.4 การติดตั้ง Whisper Local (Optional)

> ดูคู่มือฉบับสมบูรณ์ได้ที่ [`docs/WHISPER_LOCAL_STT.md`](docs/WHISPER_LOCAL_STT.md)

```bash
# รัน faster-whisper-server ด้วย Docker (แนะนำ) หรือดูวิธี pip ในคู่มือ
docker compose up -d
```

### 8.5 การตรวจสอบ STT ผ่าน API

```bash
# ดูการตั้งค่า STT ปัจจุบัน
curl -s http://localhost:8000/api/settings/stt \
  -H "Authorization: Bearer <token>" | python3 -m json.tool

# ถอดเสียง recording ใดก็ได้ด้วย API
curl -s -X POST http://localhost:8000/api/recordings/<recording_id>/transcribe \
  -H "Authorization: Bearer <token>" | python3 -m json.tool
```

### 8.6 Troubleshooting STT

| ปัญหา | สาเหตุ / แก้ไข |
|-------|---------------|
| "Speechmatics API key not configured" | ยังไม่ได้ใส่ API Key → ไปตั้งค่าที่หน้า Settings |
| "Job rejected/failed" | ไฟล์เสียงเงียบเกินไป หรือ format ไม่รองรับ → ลองอัดใหม่ให้นานขึ้น |
| "Polling timed out" | ใช้เวลาถอดเกิน 5 นาที → ไฟล์ยาวเกินไป หรือ Speechmatics ไม่ตอบ |
| "Whisper Local host not configured" | ยังไม่ได้ตั้งค่า IP → ไปที่หน้า Settings → STT |
| ถอดเสียงไม่ตรง | ลองเปลี่ยน operating_point=enhanced หรือตรวจสอบ language setting |

---

## 8.7 บัญชีทดสอบ (Demo Accounts)

> ⚠️ **เฉพาะการทดสอบเท่านั้น** — อย่าใช้ใน production ระบบจริง
> ลบออกก่อน deploy ด้วยคำสั่งใน section นี้

| Username | Password | Role | สิทธิ์ |
|----------|----------|------|-------|
| `demo_admin` | `demo1234` | ADMIN | ปรับ center freq, sample rate, ทุกฟีเจอร์ |
| `demo_user` | `demo1234` | USER | คลิก waterfall ย้าย VFO, ฟังสด, บันทึก, ดูรายการ |

### สร้างบัญชีทดสอบ

```bash
cd /root/workspace/rtl-sdr-web
./venv/bin/python3 -m backend.cli createuser demo_admin demo1234 admin
./venv/bin/python3 -m backend.cli createuser demo_user demo1234 user
```

### แนวทางการทดสอบ VFO (ฟีเจอร์ใหม่)

1. **Login เป็น `demo_user`** → สังเกตว่าปุ่ม tune ± / กล่องใส่ความถี่ **หายไป**
2. **คลิกที่ waterfall** → จะมีเส้นประสีแดง + เครื่องหมาย ▼ ตามตำแหน่งที่คลิก
3. **สังเกตว่า waterfall ไม่เลื่อน** (ต่างจาก admin ที่เปลี่ยน center freq)
4. **Login เป็น `demo_admin`** → ปุ่ม tune กลับมา + ใส่ความถี่ได้ (เลื่อน center freq จริง)

### ลบบัญชีทดสอบ (ก่อน production)

```bash
./venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('data/rtl_sdr.db')
conn.execute(\"DELETE FROM users WHERE username IN ('demo_admin','demo_user')\")
conn.commit()
print('Demo accounts deleted:', conn.total_changes)
conn.close()
"
```

---

## 9. Environment Variables (อ้างอิง)

| Variable                    | Default                  | ความหมาย                              |
|-----------------------------|--------------------------|---------------------------------------|
| `RTL_TCP_HOST`              | `127.0.0.1`              | IP/hostname ของ rtl_tcp              |
| `RTL_TCP_PORT`              | `1234`                   | TCP port ของ rtl_tcp                 |
| `RTL_TCP_SAMPLE_RATE`       | `2048000`                | Sample rate (2.048 MSPS = เสถียร)     |
| `RTL_TCP_CENTER_FREQ`       | `145500000`              | ความถี่เริ่มต้น (145.5 MHz = วิทยุสมัคร)|
| `RTL_TCP_GAIN`              | `30`                     | Gain (dB)                             |
| `SQUELCH_LEVEL`             | `-70.0`                  | threshold เปิด/ปิด record (dB)        |
| `AUDIO_SAMPLE_RATE`         | `8000`                   | เสียง PCM rate (Hz)                   |
| `JWT_SECRET_KEY`            | (ต้องเปลี่ยน!)           | signing key สำหรับ token              |
| `RECORDINGS_DIR`            | `./frontend/assets/audio`| path เก็บไฟล์บันทึก                   |
| `TELEGRAM_ENABLED`          | `false`                  | เปิดแจ้งเตือน Telegram                |

> 📖 ดู `.env.example` สำหรับรายการครบถ้วน

---

## 10. โครงสร้างไฟล์สำคัญ

```
rtl-sdr-web/
├── .env                  ← แก้ config ที่นี่
├── data/
│   ├── rtl_sdr.db        ← SQLite database
│   └── recordings/       ← ไฟล์เสียง
├── backend/
│   ├── main.py           ← FastAPI app + WebSocket endpoints
│   ├── config.py         ← อ่าน .env
│   └── sdr/
│       ├── rtl_tcp.py    ← client สำหรับ rtl_tcp
│       └── manager.py    ← streaming + recording logic
├── frontend/
│   ├── css/app.css
│   └── js/
│       ├── audio.js      ← live audio player
│       ├── app.js        ← dashboard logic
│       └── websocket.js  ← spectrum/status WS
└── venv/                 ← Python virtualenv
```

---

สร้างโดย: OPERATIONS.md