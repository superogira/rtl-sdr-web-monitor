# 🎧 Digital Voice Modes (P25 / DMR / NXDN / D-STAR / dPMR)

คู่มือการเปิดใช้งานและข้อจำกัดของโหมดถอดรหัสเสียงดิจิทัลใน RTL-SDR Web Monitor

> **สรุปสั้น**: ระบบรองรับ 5 โหมด — P25 Phase 1, DMR, NXDN, D-STAR, dPMR
> การถอดรหัสทำโดยโปรแกรมภายนอก **`dsd-neo`** (แยก process) ส่วนระบบนี้ทำหน้าที่
> แปลง IQ → baseband และเล่นเสียงที่ถอดได้

---

## 📋 สารบัญ
1. [ทำไมต้องใช้ dsd-neo (ไม่ใช้ mbelib-neo ตรง ๆ)](#1-ทำไมต้องใช้-dsd-neo)
2. [สถาปัตยกรรม](#2-สถาปัตยกรรม)
3. [ติดตั้ง dsd-neo](#3-ติดตั้ง-dsd-neo)
4. [ตั้งค่า](#4-ตั้งค่า)
5. [การใช้งาน](#5-การใช้งาน)
6. [การใช้ทรัพยากร](#6-การใช้ทรัพยากร)
7. [ข้อจำกัดที่สำคัญ](#7-ข้อจำกัดที่สำคัญ)
8. [การแก้ปัญหา](#8-การแก้ปัญหา)
9. [License และสิทธิบัตร](#9-license-และสิทธิบัตร)
10. [การติดตั้งบน production](#10-การติดตั้งบน-production-บันทึกจากการ-deploy-จริง)
11. [อ้างอิง](#11-อ้างอิง)

---

## 1. ทำไมต้องใช้ dsd-neo

`mbelib-neo` (และ mbelib ทุกสาย) เป็น **vocoder** เท่านั้น — รับ *bit ของ voice frame
ที่ซิงค์มาแล้ว* แล้วสังเคราะห์เป็น PCM:

```c
mbe_processImbe7200x4400Frame(aout, &result, imbe_fr, &cur_mp, &prev_mp, ...)
//                            ↑ char[8][23] = 184 bits ที่ต้องแยกมาให้ได้ก่อน
```

ทั้งหมดนี้ mbelib-neo **ไม่มีให้**:

| ขั้น | งาน | mbelib-neo |
|---|---|---|
| 1 | C4FM / GFSK / 4FSK demodulation | ❌ |
| 2 | Symbol timing recovery | ❌ |
| 3 | Frame sync (correlate sync word) | ❌ |
| 4 | FEC: Golay / Hamming / Trellis / RS + interleave | ❌ |
| 5 | Vocoder (IMBE / AMBE / AMBE+2) | ✅ |

ขั้น 1–4 คือส่วนที่ยากและใหญ่ที่สุด `dsd-neo` ให้มาครบและ link mbelib-neo ไว้ภายในแล้ว

### ทำไมเรียกผ่าน subprocess ไม่ link

| | subprocess pipe ✅ | ctypes / link |
|---|---|---|
| ต้องเขียน symbol sync + FEC เอง | ไม่ | **ต้อง** (~2–3 สัปดาห์) |
| License ของโปรเจกต์นี้ (MIT) | คงไว้ได้ (arm's-length) | **GPL ครอบทั้งงาน** |
| Process ตาย | ไม่ลาก server ล่ม | segfault = ทั้งระบบล่ม |
| อัปเดต decoder | เปลี่ยน binary | ต้อง rebuild |

---

## 2. สถาปัตยกรรม

```
IQ 2.048 MSPS (rtl_tcp)
 │
 ├─► SDRManager global path      → FM/AM/USB/LSB + recording + spectrum  (เดิม ไม่แตะ)
 │
 └─► AudioBroadcaster / Listener (มีอยู่แล้ว)
      │
      ├─ mode analog → StreamingDemodulator (เดิม)
      │
      └─ mode digital:
           StreamingDemodulator._process_digital()
             • channel LPF  (_digital_if_cutoff: 6.0k @12.5k, 3.6k @6.25k)
             • phase discriminator ( carried phase → no click )
             • NO de-emphasis, NO 300-3400 Hz voice BP   ← หัวใจ
             • decimate 2.048M → 32k (stride 64, exact)
             • resample 32k → 48k (3/2, มี overlap กันรอยต่อ)
                   ↓ float32 @ 48 kHz
           DigitalDecoder.feed()  → PCM16LE mono → stdin
                   ↓
             dsd-neo  -f1/-fs/-fn/-fd/-fm  -i - -s 48000 -o -
                   ↓ PCM16LE @ 8 kHz
           DigitalDecoder.read()  → float32
                   ↓
           AUD2 frame (header ระบุ 8000 Hz) → WebSocket → browser
                   ↓
           recording (WAV @ 8 kHz, duration ถูกต้อง)
```

### ทำไมต้องแยก "digital tap" จาก FM ปกติ

เส้นทาง FM สำหรับฟังเสียงมี 2 อย่างที่ **ทำลาย** สัญญาณดิจิทัล:

| ตัวกรอง | ผลกับเสียง analog | ผลกับสัญญาณดิจิทัล |
|---|---|---|
| De-emphasis 530 µs | ทำให้เสียงนุ่ม | **ปั้นรูป symbol → slicer ผิด** |
| Band-pass 300–3400 Hz | ตัด noise นอกย่านเสียง | **ตัด sidelobe ของ symbol** |

วัดจริง (test `test_digital_response_is_flat`): tap ดิจิทัลตอบสนองแบน
0.283 → 0.276 (300→4000 Hz ≈ 0.2 dB) ขณะที่เส้นทาง FM ตกจาก 0.28 → 0.08

---

## 3. ติดตั้ง dsd-neo

### วิธีที่แนะนำ — AppImage (ไม่ต้อง compile)

```bash
sudo mkdir -p /opt/dsd-neo && cd /opt/dsd-neo
sudo curl -L -o dsd-neo.AppImage \
  https://github.com/arancormonk/dsd-neo/releases/download/v2.11.4/dsd-neo-linux-x86_64-portable-v2.11.4.AppImage
sudo chmod +x dsd-neo.AppImage

# LXC/container/Docker มักไม่มี FUSE → แตกไฟล์แทน
sudo ./dsd-neo.AppImage --appimage-extract
# ใช้งาน: /opt/dsd-neo/squashfs-root/AppRun
```

### ตรวจสอบ

```bash
/opt/dsd-neo/squashfs-root/AppRun -h | head -5
# ควรเห็น "Usage: dsd-neo [options]"
```

### ทางเลือก — build จาก source (ได้ AVX2 dispatch เต็มที่)

```bash
sudo apt install -y build-essential cmake ninja-build libssl-dev \
  libsndfile1-dev libpulse-dev libncurses-dev
git clone https://github.com/arancormonk/mbelib-neo && cd mbelib-neo
cmake --preset dev-release -DMBELIB_ENABLE_NATIVE=OFF && cmake --build --preset dev-release -j
sudo cmake --install build/dev-release --prefix /usr/local
# แล้ว build dsd-neo ตาม docs/linux-installation.md ของ repo นั้น
```

> dsd-neo ต้องใช้ **mbelib-neo ≥ 2.6** (แพ็กเกจ CMake `mbe-neo`)

---

## 4. ตั้งค่า

เพิ่มใน `.env` (ไม่ commit):

```bash
# เปิด/ปิดโหมดดิจิทัล
DIGITAL_DECODE_ENABLED=true

# path ของ dsd-neo — เว้นว่าง = หาอัตโนมัติ
# (PATH → /opt/dsd-neo/squashfs-root/AppRun → /usr/local/bin → /usr/bin)
DSD_NEO_BIN=/opt/dsd-neo/squashfs-root/AppRun

# เพดาน process พร้อมกัน (1 process ต่อ 1 listener)
DIGITAL_DECODE_MAX_PROC=8

# จำนวนครั้งที่ยอม restart เมื่อ decoder ตาย (0 = ไม่จำกัด)
DIGITAL_DECODE_RESTART_MAX=5
```

**หมายเหตุ**: ถ้าตั้ง `DSD_NEO_BIN` ผิด path ระบบจะ **ไม่ fallback** ไปหาที่อื่น
(เพื่อให้เห็นปัญหา ไม่ใช่ใช้ binary เก่าเงียบ ๆ)

---

## 5. การใช้งาน

1. เปิด Dashboard → กดปุ่ม **`Digital ▾`** ที่แถบเลือกโหมด (ข้าง USB/LSB)
2. เลือกโหมด
3. ปุ่มจะเปลี่ยนเป็นชื่อโหมด (เช่น `P25P1 ▾`) สีเขียว = กำลังถอดรหัสอยู่
4. กด **🔈 ฟังสด**

Bandwidth ถูกตั้งอัตโนมัติตามโหมด (แก้ไม่ได้ เพราะ decoder ต้องการทั้งช่องสัญญาณ):

| โหมด | BW | Modulation | Vocoder |
|---|---|---|---|
| P25 Phase 1 | 12.5 kHz | C4FM | IMBE 7200x4400 |
| DMR | 12.5 kHz | 4FSK 2-slot TDMA | AMBE+2 |
| NXDN | 12.5 kHz | 4FSK | AMBE+2 |
| D-STAR | 6.25 kHz | GMSK | AMBE 3600x2400 |
| dPMR | 6.25 kHz | 4FSK | AMBE+2 |

### การบันทึกเสียง

- **อัดเสียงเอง (per-listener)**: ได้ไฟล์ WAV **8 kHz** ความยาวถูกต้อง
- **อัดอัตโนมัติ**: ใช้ squelch จาก **channel power** (ดิจิทัลไม่มี carrier ให้ gate แบบ analog)
- **Recording JOB (ตั้งเวลา)**: ⚠️ ยัง **ไม่** ถอดรหัส — บันทึกเป็น discriminator baseband
  (ดูข้อจำกัด #4)

---

## 6. การใช้ทรัพยากร

วัดจริงบน Xeon E5-2683 v3 @ 2.0 GHz (per 1 digital listener):

| องค์ประกอบ | CPU / 50 ms chunk | หมายเหตุ |
|---|---|---|
| Channel LPF + discriminator | ~8–12 ms | **ถูกกว่า** FM เดิม (~15 ms) เพราะไม่มี de-emphasis + BP |
| decimate 64:1 (integer stride) | ~1.5 ms | ทางเร็ว |
| resample 32k→48k | ~0.1 ms | ~60 taps |
| **dsd-neo process** | **~2–5 % ของ 1 core** | symbol rate แค่ 2.4–9.6 ksym/s |

| รวม | ค่า |
|---|---|
| CPU | **≈ เท่าหรือน้อยกว่า analog** |
| RAM | **+25–35 MB / listener** |
| Latency เพิ่ม | **+150–250 ms** (frame buffer + FEC + vocoder) |
| ขนาดติดตั้ง | 17 MB (AppImage) |

**Production 8 listeners** → +~250 MB RAM, +~25 % ของ 1 core (สบายมากบน 16 core / 12 GB)

---

## 7. ข้อจำกัดที่สำคัญ

| # | ข้อจำกัด | ผลกระทบ |
|---|---|---|
| 1 | **ไม่มี TETRA** | TETRA เป็น π/4-DQPSK — ข้อมูล phase หายไปถ้าผ่าน discriminator audio. ต้องใช้ IQ + 72 kHz ซึ่งชนกับ single-client rtl_tcp |
| 2 | **ไม่มี P25 Phase 2 / X2-TDMA / ProVoice / YSF / M17** | เพิ่มได้ในอนาคต (แก้ map เดียว) |
| 3 | **ถอดรหัสไม่ออกถ้าเข้ารหัส** (P25/DMR encrypted) | ต้องมี key; ระบบไม่รองรับ |
| 4 | **Recording JOB ยังไม่ decode** | ไฟล์ที่ได้เป็น baseband ต้องถอดภายหลัง (`dsd-neo -i file.wav`) |
| 5 | **Squelch ใช้ระดับสัญญาณเท่านั้น** | ดิจิทัลไม่มี carrier; บางช่วงสัญญาณอ่อนจะเงียบ |
| 6 | **ต้องมี SNR ~10 dB ขึ้นไป** | dsd-neo เป็น heuristic decoder — สัญญาณอ่อน = เงียบ/robot |
| 7 | **DMR เล่นช่องเดียว (slot A)** | dsd-neo ส่ง 2 slot แบบ stereo, ระบบเลือก left; per-slot selector ยังไม่มี |
| 8 | **`mode` column เป็น VARCHAR(3)** | SQLite ไม่ enforce (ใช้งานได้); ถ้าย้ายไป PostgreSQL ต้อง `ALTER COLUMN ... VARCHAR(16)` |
| 9 | **Latency เพิ่ม 150–250 ms** | เป็นธรรมชาติของ frame buffer + FEC + vocoder |

---

## 8. การแก้ปัญหา

### ไม่มีเสียงเลย

```bash
# 1. dsd-neo ติดตั้งถูกไหม
ls -l "$DSD_NEO_BIN" || ls -l /opt/dsd-neo/squashfs-root/AppRun

# 2. รันด้วยมือได้ไหม
/opt/dsd-neo/squashfs-root/AppRun -f1 -i - -s 48000 -o - --frontend none < /dev/null
# ควรได้ NOTICE: Frontend: none แล้ว exit 0

# 3. ดู log ของ server
journalctl -u rtl-sdr-web -n 100 --no-pager | grep -i 'digital decode'
```

### Dashboard ขึ้น "⚠ dsd-neo not installed" / "decoder stopped"

VFO row แสดงสถานะของ decoder โดยตรง (ชิปเล็ก ๆ ข้างมิเตอร์ SIG):

| ที่เห็น | ความหมาย | ต้องทำ |
|---|---|---|
| `ดิจิทัล · รอสัญญาณ` (จาง) | decoder ทำงานดี แค่ยังไม่มี burst | **ปกติ** — รอสัญญาณ |
| `⚠ dsd-neo not installed` | หาโปรแกรมไม่เจอ | ติดตั้ง หรือแก้ `DSD_NEO_BIN` |
| `⚠ dsd-neo not executable at …` | ตั้ง path ผิด | แก้ path ใน `.env` แล้ว restart |
| `⚠ decoder stopped` | เคยรันแล้วตาย | ดู log (บรรทัด `digital decode: ... exited`) |

### เจอ "dsd-neo not found"

ระบบ log ว่า:
```
digital decode: dsd-neo not found (set DSD_NEO_BIN); P25P1 will stay silent
```
→ ตั้ง `DSD_NEO_BIN` ใน `.env` แล้ว restart

### Decoder ตายบ่อย

```
digital decode: P25P1 exited (rc=1): ...
```
ข้อความหลัง `rc=` คือ stderr ของ dsd-neo เอง — บอกสาเหตุจริง
ถ้า restart ครบโควตาแล้วจะหยุด จนกว่าจะเปลี่ยนโหมด

### มีสัญญาณแต่ decode ไม่ออก

- **BW ผิด** → ระบบตั้งให้แล้ว แต่ถ้าตั้งเองต้องเป็น 12.5k / 6.25k
- **PPM คลาด** → ตั้ง Frequency correction (digital ทน ppm น้อยกว่า analog มาก)
- **สัญญาณอ่อน** → ต้องการ SNR ~10 dB
- **คนละ protocol** → ลองเปลี่ยนโหมด (เช่น NXDN96 vs NXDN48 ยังไม่แยก)

### ⚠ rtl_tcp รับ client ได้ครั้งละ 1 ตัว (สำคัญมาก)

`rtl_tcp` (โดยเฉพาะรุ่น `.exe` บน Windows) **รับ client พร้อมกันได้แค่ 1 ราย**
และไมส่ง FIN กลับตอน client ตัดการเชื่อมต่อ ทำให้ socket ของเราค้างที่
`FIN-WAIT-2` และ **ยึด slot เดียวไว้ตลอด** อาการที่เห็น:

```
TCP connected ... but no rtl_tcp header arrived within 10s
```

แก้โดยลด FIN timeout (ตั้งเป็นไฟล์ถาวรแล้วบน production):

```bash
cat /etc/sysctl.d/99-rtl-sdr-web.conf
# net.ipv4.tcp_fin_timeout = 15
sysctl -w net.ipv4.tcp_fin_timeout=15
```

**ข้อควรระวังตอนทดสอบ**: อย่าต่อ rtl_tcp ตรงจากสคริปต์อื่น (เช่น `python -c`
ที่เปิด `RtlTcpClient` เอง) ขณะที่ service รันอยู่ — มันจะแย่ง slot และทำให้
service เชื่อมต่อไม่ได้จนกว่า socket จะหมดอายุ ใช้วิธีทดสอบที่ไม่แตะ tuner แทน

### เสียงช้า / เร็วผิดปกติ

ตรวจว่า AUD2 frame รายงาน 8000 Hz — ถ้าเห็น 32000 Hz
แปลว่า listener ไม่ได้เข้าสาขาดิจิทัล (ดู `out_audio_rate`)

---

## 9. License และสิทธิบัตร

| Component | License | ผลกับโปรเจกต์นี้ |
|---|---|---|
| RTL-SDR Web Monitor | **MIT** | ไม่เปลี่ยน |
| dsd-neo | **GPL-3.0** | เรียกผ่าน subprocess — ไม่ link, ไม่ distribute รวม |
| mbelib-neo | GPL-2.0-or-later (+ ETSI tables ที่ยังไม่ชัด) | อยู่ใต้ dsd-neo |

**แนวทาง**: ระบบนี้ **ไม่แจกจ่าย** binary ของ dsd-neo — ผู้ใช้ติดตั้งเอง
จึงเป็นการใช้งานแยกโปรแกรม (arm's-length) ไม่ใช่การ link แบบ derivative work

### ⚠️ สิทธิบัตร

mbelib-neo ประกาศเองว่า:

> "Use and distribution of the vocoders may implicate patents. The project has
> not established third-party patent clearance; educational or research use is
> not asserted as an exemption."

IMBE/AMBE/AMBE+2 เป็นเทคโนโลยีของ **DVSI** และมีสิทธิบัตรในหลายประเทศ
**ผู้ deploy ต้องประเมินความเสี่ยงเอง** — โดยเฉพาะถ้าให้บริการสาธารณะ

---

## 10. การติดตั้งบน production (บันทึกจากการ deploy จริง)

ขั้นตอนที่ใช้จริงบน `192.168.1.219` (`/opt/rtl-sdr-web`, systemd, User=root):

```bash
# 1) decoder
mkdir -p /opt/dsd-neo && cd /opt/dsd-neo
curl -L -o dsd-neo.AppImage <release-url>
chmod +x dsd-neo.AppImage && ./dsd-neo.AppImage --appimage-extract
/opt/dsd-neo/squashfs-root/AppRun -h | head -2      # ต้องผ่านก่อนไปต่อ

# 2) config
cd /opt/rtl-sdr-web
cp .env .env.bak.$(date +%Y%m%d_%H%M%S)
# เพิ่ม DIGITAL_DECODE_* / DSD_NEO_BIN (ดูหัวข้อ 4)

# 3) code + tests ก่อน restart เสมอ
git pull --ff-only
./venv/bin/python -m unittest discover -s backend/tests -q   # ต้อง OK

# 4) restart
systemctl restart rtl-sdr-web && systemctl is-active rtl-sdr-web

# 5) ตรวจ
journalctl -u rtl-sdr-web --since "2 min ago" --no-pager | grep -i "digital\|error"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/dashboard
```

### สิ่งที่ตรวจแล้วบน production (2026-10-31)

| การตรวจ | ผล |
|---|---|
| test suite (รวม digital) | **148 tests OK** |
| dsd-neo decode IMBE จริง | 320 bytes → 160 samples @ 8 kHz, 201/320 ไบต์มีเสียง |
| spawn ครบ 5 โหมด | ✅ ~236 ms/โหมด |
| baseband → decoder (20 chunks) | 48,000 samples ป้อนเข้า, decoder ไม่ตาย |
| API `/sdr/mode` ทุกโหมด | 200 OK; `TETRA` → 400 พร้อมข้อความถูกต้อง |
| global digital guard | log เตือนครั้งเดียว, ไม่บันทึก noise |
| process leak หลังทดสอบ | 0 |
| memory / restarts | ~134 MB, NRestarts=0 |

### กับดักที่เจอตอน deploy

1. **การทดสอบของเราเองแย่ง rtl_tcp** — สคริปต์ที่เปิด `RtlTcpClient` ตรง
   ขณะ service รันอยู่ จะยึด single-client slot และทิ้ง socket ค้าง FIN-WAIT-2
   ทำให้ service เชื่อมไม่ได้ชั่วคราว → ใช้การทดสอบที่ไม่แตะ tuner
   (ป้อน IQ สังเคราะห์ / `-r` MBE file) และตั้ง `tcp_fin_timeout=15` ถาวร
2. **`tcp_fin_timeout` ไม่รอด reboot** — ต้องมีไฟล์ใน `/etc/sysctl.d/`
   ไม่ใช่แค่ `sysctl -w`
3. **remote rtl_tcp ที่บ้านล่ม** (connection refused) — แอปยัง retry ถูกต้อง
   ไม่ตาย แต่ decode จะไม่มีเสียงจนกว่า upstream จะกลับมา

---

## 11. อ้างอิง
- mbelib-neo: <https://github.com/arancormonk/mbelib-neo>
- dsd-neo: <https://github.com/arancormonk/dsd-neo>
- dsd-neo CLI reference: `docs/cli.md` ใน repo นั้น
- dsd-neo network audio (stdin/stdout format): `docs/network-audio.md`
- ETSI EN 300 395-2 (TETRA ACELP) — กรณีทำ TETRA ในอนาคต
- TIA-102.BABA (IMBE), TIA-102.BABA-1 (AMBE+2)


