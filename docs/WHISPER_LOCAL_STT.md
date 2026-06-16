# 🎙️ Whisper Local STT — คู่มือติดตั้งและใช้งาน

คู่มือสำหรับติดตั้งและใช้งานระบบถอดเสียงด้วย **Whisper Local** (faster-whisper-server) ที่รันบนเครือข่ายท้องถิ่น ไม่ต้องเสียเงินจ่ายรายเดือนเหมือนบริการ Cloud

---

## 📋 สารบัญ
1. [ภาพรวมระบบ](#-ภาพรวมระบบ)
2. [ความต้องการของระบบ](#-ความต้องการของระบบ)
3. [วิธีที่ 1: ติดตั้ง faster-whisper-server ด้วย Docker (แนะนำ)](#-วิธีที่-1-ติดตั้ง-faster-whisper-server-ด้วย-docker-แนะนำ)
4. [วิธีที่ 2: ติดตั้งด้วย pip](#-วิธีที่-2-ติดตั้งด้วย-pip)
5. [ตั้งค่าใน rtl-sdr-web](#-ตั้งค่าใน-rtl-sdr-web)
6. [ทดสอบการทำงาน](#-ทดสอบการทำงาน)
7. [การแก้ปัญหา (Troubleshooting)](#-การแก้ปัญหา-troubleshooting)
8. [เปรียบเทียบ Provider](#-เปรียบเทียบ-provider)

---

## 🔍 ภาพรวมระบบ

```
┌─────────────────────┐         ┌──────────────────────────┐
│   rtl-sdr-web       │  HTTP   │  faster-whisper-server   │
│   (เครื่องนี้)       │ ──────► │  (OpenAI-compatible API) │
│                     │  WAV    │  GPU/CPU                 │
│  backend (Python)   │  file   │  Port 8100               │
└─────────────────────┘         └──────────────────────────┘
     ส่งไฟล์เสียง                       ถอดเสียงแล้วส่งข้อความกลับ
```

- **faster-whisper-server** คือเซิร์ฟเวอร์ที่ใช้ [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 backend) เร็วกว่า openai-whisper 4 เท่า ใช้ memory น้อยกว่า
- เปิดบริการผ่าน **OpenAI-compatible REST API** (`/v1/audio/transcriptions` และ `/v1/audio/translations`) ทำให้ rtl-sdr-web เรียกใช้ได้ง่ายและส่ง `language` เพื่อบังคับภาษาได้
- รองรับ **GPU (CUDA)** และ **CPU** — แต่ GPU เร็วกว่ามาก

---

## 💻 ความต้องการของระบบ

### ฝั่ง rtl-sdr-web (เครื่องที่รัน backend)
| รายการ | ค่าต่ำสุด | แนะนำ |
|--------|-----------|-------|
| Python | 3.10+ | 3.11+ |
| RAM | 1 GB (ว่าง) | 2 GB |
| Python package | `httpx` (มีอยู่แล้วใน `requirements.txt`) | ไม่ต้องติดตั้งเพิ่ม |

### ฝั่ง faster-whisper-server (เครื่องที่รัน Whisper)
| รายการ | CPU Mode | GPU Mode (แนะนำ) |
|--------|----------|-------------------|
| CPU | 4 cores+ | — |
| GPU | — | NVIDIA GPU, CUDA 12+ |
| RAM | 4 GB (small model) | 8 GB+ |
| VRAM | — | 2 GB+ (small), 4 GB+ (medium) |
| Disk | 2 GB (models) | 2 GB (models) |

> 💡 **สามารถรันบนเครื่องเดียวกันได้** ถ้าเครื่องแรงพอ หรือรันคนละเครื่องก็ได้ (เชื่อมผ่าน IP ในวง LAN)

---

## 🐳 วิธีที่ 1: ติดตั้ง faster-whisper-server ด้วย Docker (แนะนำ)

### ขั้นตอนที่ 1: เตรียม Docker
```bash
# ตรวจสอบว่ามี Docker และ Docker Compose
docker --version
docker compose version

# ถ้ายังไม่มี ติดตั้ง:
# curl -fsSL https://get.docker.com | sh
```

### ขั้นตอนที่ 2: โคลนโปรเจกต์
```bash
cd /opt  # หรือโฟลเดอร์ที่ต้องการ
git clone https://github.com/fedirz/faster-whisper-server.git
cd faster-whisper-server
```

### ขั้นตอนที่ 3: ตั้งค่า environment
สร้างไฟล์ `.env`:
```bash
cat > .env << 'EOF'
# โหมด CPU (ใช้ได้ทุกเครื่อง)
WHISPER__INFERENCE_DEVICE=cpu
WHISPER__MODEL=Systran/faster-whisper-small

# ถ้ามี GPU NVIDIA ให้เปลี่ยนเป็น:
# WHISPER__INFERENCE_DEVICE=cuda
# WHISPER__MODEL=Systran/faster-whisper-medium
EOF
```

### ขั้นตอนที่ 4: รันด้วย Docker Compose
```bash
# รันแบบ background
docker compose up -d

# ดู log
docker compose logs -f

# ทดสอบว่าเปิดทำงานแล้ว (เปิดในเบราว์เซอร์)
# http://<IP>:8100
```

### ขั้นตอนที่ 5: ตั้งให้รันอัตโนมัติตอนบูตเครื่อง
```bash
docker compose up -d --restart unless-stopped
```

---

## 📦 วิธีที่ 2: ติดตั้งด้วย pip

> ⚠️ แนะนำให้ใช้ Docker มากกว่า แต่ถ้าต้องการติดตั้งด้วย pip ก็ทำได้

```bash
# สร้าง virtual environment
python3 -m venv venv
source venv/bin/activate

# ติดตั้ง
pip install faster-whisper-server

# รัน (CPU mode)
faster-whisper-server --host 0.0.0.0 --port 8100

# หรือระบุ GPU
# faster-whisper-server --host 0.0.0.0 --port 8100 --device cuda
```

---

## ⚙️ ตั้งค่าใน rtl-sdr-web

หลังจากติดตั้ง faster-whisper-server แล้ว ให้ตั้งค่าในระบบ rtl-sdr-web:

### ผ่านหน้าเว็บ (แนะนำ)
1. เข้าสู่ระบบด้วยบัญชี **admin**
2. ไปที่ **ตั้งค่าระบบ → ถอดเสียง**
3. เลือก **Provider** = `Whisper Local (API)`
4. กรอกข้อมูล:

| ฟิลด์ | คำอธิบาย | ตัวอย่าง |
|-------|----------|---------|
| **IP Address** | IP ของเครื่องที่รัน faster-whisper-server | `192.168.1.201` |
| **Port** | Port ของ API (default: 8100) | `8100` |
| **Model** | ชื่อโมเดล HuggingFace | `Systran/faster-whisper-small` |
| **Task** | `transcribe` = ถอดเสียงเป็นภาษาต้นฉบับ<br>`translate` = แปลเป็นภาษาอังกฤษ | `transcribe` |
| **Temperature** | 0.0 = แม่นยำที่สุด (greedy)<br>ค่าสูงขึ้น = สร้างสรรค์มากขึ้น | `0.0` |

5. กด **💾 บันทึก**

### ผ่านไฟล์ `.env` (fallback ถ้ายังไม่ได้ตั้งใน DB)
```env
STT_PROVIDER=whisper_local
WHISPER_LOCAL_HOST=192.168.1.201
WHISPER_LOCAL_PORT=8100
WHISPER_LOCAL_MODEL=Systran/faster-whisper-small
WHISPER_LOCAL_TASK=transcribe
WHISPER_LOCAL_TEMPERATURE=0.0
```

> 💡 ค่าที่ตั้งใน **หน้าเว็บ (Database)** จะมี priority สูงกว่า `.env`

---

## รายการโมเดลที่แนะนำ

| โมเดล | ขนาด | RAM/VRAM | ความเร็ว (CPU) | ความแม่นยำ | เหมาะกับ |
|-------|------|----------|----------------|------------|----------|
| `Systran/faster-whisper-tiny` | ~75 MB | ~1 GB | ⚡⚡⚡⚡⚡ | ⭐⭐ | ทดสอบระบบ, เสียงชัด |
| `Systran/faster-whisper-base` | ~145 MB | ~1 GB | ⚡⚡⚡⚡ | ⭐⭐⭐ | ใช้ทั่วไปบน CPU |
| `Systran/faster-whisper-small` | ~480 MB | ~2 GB | ⚡⚡⚡ | ⭐⭐⭐⭐ | **แนะนำ** สมดุลดี |
| `Systran/faster-whisper-medium` | ~1.5 GB | ~4 GB | ⚡⚡ | ⭐⭐⭐⭐⭐ | ต้องการความแม่นยำสูง |
| `Systran/faster-whisper-large-v3` | ~3 GB | ~6-8 GB | ⚡ (เฉพาะ GPU) | ⭐⭐⭐⭐⭐ | ความแม่นยำสูงสุด (ต้องมี GPU) |

> ดูรายการโมเดลทั้งหมดได้ที่ [HuggingFace - Systran](https://huggingface.co/Systran)

---

## 🧪 ทดสอบการทำงาน

### 1. ทดสอบว่า faster-whisper-server ทำงาน
```bash
# เปิดในเบราว์เซอร์
http://<IP>:8100

# หรือทดสอบด้วย curl
curl http://192.168.1.201:8100/ | head -5
# ควรได้: HTML page (Whisper Playground)
```

### 2. ทดสอบถอดเสียงผ่าน rtl-sdr-web
1. ไปที่หน้า **Recordings**
2. เลือกไฟล์ที่ต้องการถอดเสียง
3. กดปุ่ม **ถอดเสียง**
4. รอสักครู่ — ควรมีข้อความปรากฏขึ้นมา

### 3. เช็ค log หากมีปัญหา
```bash
# Log ของ rtl-sdr-web
journalctl -u rtl-sdr-web -f | grep -i whisper

# ควรเห็น:
# "Whisper Local transcript: 150 chars (host=192.168.1.201:8100, ...)"
```

---

## 🔧 การแก้ปัญหา (Troubleshooting)

### ❌ ปัญหา: "Whisper Local host not configured"
**สาเหตุ:** ยังไม่ได้ตั้งค่า IP Address
**แก้ไข:** ไปที่หน้า **ตั้งค่า → ถอดเสียง** และกรอก IP Address

---

### ❌ ปัญหา: "Connection refused" / "Connection timeout"
**สาเหตุ:** faster-whisper-server ไม่ได้รัน หรือ IP/Port ผิด
**แก้ไข:**
```bash
# 1. ตรวจสอบว่า server รันอยู่
curl http://<IP>:8100/

# 2. ตรวจสอบ firewall
sudo ufw allow 8100/tcp

# 3. ตรวจสอบว่า server ฟังบน 0.0.0.0 ไม่ใช่ 127.0.0.1
# ใน Docker compose ต้องมี ports: "8100:8100"
```

---

### ❌ ปัญหา: "Whisper Local host not configured" (ซ้ำ)
ถ้าตั้งค่าแล้วยังขึ้น ให้ล้าง cache ของ browser แล้วบันทึกใหม่อีกครั้ง

---

### ❌ ปัดหา: ถอดเสียงช้ามาก
**สาเหตุ:** ใช้ CPU หรือโมเดลใหญ่เกินไป
**แก้ไข:**
- ลองเปลี่ยนเป็นโมเดลเล็กลง (เช่น `tiny` หรือ `base`)
- ติดตั้ง GPU ถ้าเป็นไปได้
- ตรวจสอบว่า faster-whisper-server ใช้ CUDA (ดู log ตอน startup)

```bash
# ในฝั่ง faster-whisper-server ตรวจสอบ GPU
docker compose exec faster-whisper-server nvidia-smi
```

---

### ❌ ปัญหา: คุณภาพการถอดเสียงไม่ดี
**แก้ไข:**
- ลองเปลี่ยนโมเดลใหญ่ขึ้น (เช่น `medium` หรือ `large-v3`)
- ตั้ง `temperature=0.0` (greedy decoding = แม่นยำที่สุด)
- ตรวจสอบคุณภาพเสียงต้นฉบับ (สัญญาณ SDR, squelch level)

---

### ❌ ปัญหา: "HTTP 422" จาก Whisper Local API
**สาเหตุ:** ส่ง parameter ที่ server ไม่รู้จัก (เช่น format ผิด)
**แก้ไข:** ตรวจสอบ log ใน backend ดู error body — ปัจจุบันใช้ `response_format=json` ซึ่งรองรับโดย faster-whisper-server ทุกเวอร์ชันล่าสุด

### ❌ ปัญหา: ถอดเสียงผิดภาษา
**สาเหตุ:** model auto-detect ภาษาผิด
**แก้ไข:** ตั้งค่า **Language** ในหน้าถอดเสียงเป็นภาษาที่ต้องการ (เช่น `th`) — ระบบจะส่ง `language` parameter ไปบังคับภาษา

---

## 📊 เปรียบเทียบ Provider

| คุณสมบัติ | Whisper Local | Speechmatics | Azure Speech | Whisper (Local builtin) |
|----------|---------------|--------------|--------------|------------------------|
| **ราคา** | ✅ ฟรี | ❌ เสียเงิน | ❌ เสียเงิน | ✅ ฟรี |
| **ต้องติดตั้งเพิ่ม** | ติดตั้ง server แยก | ไม่ต้อง | ไม่ต้อง | ไม่ต้อง (แต่หนัก) |
| **ความเร็ว** | ⚡⚡⚡ (มี GPU) | ⚡⚡⚡⚡ (Cloud) | ⚡⚡⚡⚡ (Cloud) | ⚡ (ช้า) |
| **ความเร่งด่วน** | รันเองได้ | ต้องอินเทอร์เน็ต | ต้องอินเทอร์เน็ต | รันเองได้ |
| **ภาษาไทย** | ✅ (คุณภาพดี) | ✅ (ดีมาก) | ✅ (ดี) | ✅ (ปานกลาง) |
| **ใช้ GPU ของเครื่อง** | ✅ | ❌ | ❌ | ❌ (CPU only ใน built-in) |
| **เหมาะกับ** | ใช้ในองค์กร ไม่ออกเน็ต | คุณภาพสูง ไม่สนราคา | อยู่บน Azure แล้ว | ทดสอบ ไม่มี server |

---

## 📝 หมายเหตุ

- การตั้งค่าใน **Database** (ผ่านหน้าเว็บ) จะมี priority สูงกว่า `.env`
- หากลบค่าใน Database ระบบจะใช้ค่าจาก `.env` แทน
- การเรียก API ใช้ `httpx` แบบ async จึงไม่บล็อก event loop
- โมเดลจะถูกดาวน์โหลดอัตโนมัติครั้งแรกที่เรียกใช้ (เก็บใน `~/.cache/huggingface/` ในฝั่ง server)
- ดู API docs ได้ที่ `http://<IP>:8100/docs` (Swagger UI) หรือ `http://<IP>:8100/openapi.json`

---

_อัปเดตล่าสุด: 2025-06-16 (ใช้ OpenAI-compatible REST API)_
