# 🚀 How to Restart & Check (Quick Reference)

> ไฟล์นี้สรุปคำสั่งสั้นๆ สำหรับ restart server และตรวจสอบสถานะ
> รายละเอียดเต็มอยู่ใน `OPERATIONS.md`

---

## 📋 สรุปสภาพแวดล้อมปัจจุบัน

| รายการ | ค่า |
|---|---|
| **รูปแบบการรัน** | uvicorn process (nohup, background) — ไม่ใช่ systemd |
| **Path** | `/root/workspace/rtl-sdr-web` |
| **Python** | `/root/workspace/rtl-sdr-web/venv/bin/python3` |
| **Port** | `8000` |
| **Log file** | `/var/log/rtl-sdr-web/uvicorn.log` |
| **การทำ Migration** | อัตโนมัติ (auto-add columns ใน `backend/database.py` `_run_migrations`) |

---

## 🔄 Restart Server (หลังแก้ Python code)

เพราะรันเป็น uvicorn process ตรงๆ (ไม่มี systemd service) ต้อง kill + start เอง

### วิธีที่ 1: คำสั่งเดียว (แนะนำ)

```bash
cd /root/workspace/rtl-sdr-web

# หา PID ของ uvicorn เดิม แล้ว kill + restart ในบรรทัดเดียว
OLD_PID=$(ss -tlnp | grep ':8000' | grep -oP 'pid=\K[0-9]+' | head -1)
echo "Old pid: $OLD_PID"
[ -n "$OLD_PID" ] && kill "$OLD_PID" && sleep 2
( kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" )  # force kill ถ้ายังไม่ตาย

mkdir -p /var/log/rtl-sdr-web
nohup ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info \
  > /var/log/rtl-sdr-web/uvicorn.log 2>&1 &

sleep 3
curl -s http://localhost:8000/api/health   # ควรได้: {"status":"ok",...}
```

### วิธีที่ 2: ทีละขั้น

```bash
# 1) หา PID ปัจจุบัน
ss -tlnp | grep ':8000'
# ผลลัพธ์จะมี pid=XXXX เช่น: users:(("uvicorn",pid=196008,fd=15))

# 2) ฆ่า process เดิม
kill 196008            # แทนด้วย PID จริง
sleep 2
kill -0 196008 2>/dev/null && kill -9 196008   # force kill ถ้ายังค้าง

# 3) เริ่ม server ใหม่ (background + redirect log)
cd /root/workspace/rtl-sdr-web
mkdir -p /var/log/rtl-sdr-web
nohup ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info \
  > /var/log/rtl-sdr-web/uvicorn.log 2>&1 &

# 4) ตรวจสถานะ
sleep 3
curl -s http://localhost:8000/api/health
```

> ⚠️ **แก้ไฟล์ CSS/JS/HTML** → ไม่ต้อง restart (refresh browser ด้วย Ctrl+Shift+R; อย่าลืม bump `?v=N` ใน `<script src=...>` / `<link href=...>`)
> ⚠️ **แก้ไฟล์ Python (.py)** → ต้อง restart (ตามวิธีข้างบน)

---

## 🩺 ตรวจสอบสถานะ (Health Check)

```bash
# 1. ตรวจว่า service รันอยู่ไหม (ดู process + port)
ps aux | grep uvicorn | grep -v grep
ss -tlnp | grep 8000

# 2. HTTP health check
curl -s http://localhost:8000/api/health

# 3. ดู log แบบ real-time
tail -f /var/log/rtl-sdr-web/uvicorn.log

# 4. ดู log 50 บรรทัดล่าสุด
tail -n 50 /var/log/rtl-sdr-web/uvicorn.log

# 5. ตรวจ SDR status
curl -s http://localhost:8000/api/sdr/status
```

---

## 🗄️ Database Migration

ระบบใช้ **manual migration** (ไม่ใช่ alembic CLI):
- ฟังก์ชัน `_run_migrations()` ใน `backend/database.py` จะรันอัตโนมัติทุกครั้งที่ server start
- ต้องเพิ่ม column ใหม่ใน list `migrations` (line ~57) ทุกครั้งที่เพิ่ม field
- มี migration file ใน `backend/migrations/versions/` เป็นเอกสารอ้างอิง

**เมื่อเพิ่ม column ใหม่:**
1. เพิ่มใน `backend/models/recording.py` (SQLAlchemy model)
2. เพิ่มใน `backend/database.py` → `_run_migrations()` list
3. (optional) สร้าง migration file ใน `backend/migrations/versions/` เป็นเอกสาร
4. Restart server → column จะถูกเพิ่มอัตโนมัติ

---

## 🔧 คำสั่งทั่วไป

```bash
# หาก server หยุดอยู่ → start ใหม่
cd /root/workspace/rtl-sdr-web
mkdir -p /var/log/rtl-sdr-web
nohup ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info \
  > /var/log/rtl-sdr-web/uvicorn.log 2>&1 &

# หยุด server
kill $(ss -tlnp | grep ':8000' | grep -oP 'pid=\K[0-9]+' | head -1)

# ดู process ทั้งหมดที่เกี่ยวข้อง
ps aux | grep -E "uvicorn|rtl-sdr" | grep -v grep
```

---

## 🐛 ถ้ามีปัญหา

```bash
# Server crash / restart loop → ดู log
tail -n 100 /var/log/rtl-sdr-web/uvicorn.log

# ตรวจ syntax ก่อน restart
cd /root/workspace/rtl-sdr-web
./venv/bin/python3 -m py_compile backend/main.py

# ตรวจ port ชนกัน
ss -tlnp | grep 8000

# ถ้า port ไม่ปล่อยหลัง kill → หาและ force kill
fuser -k 8000/tcp
```

---

## 📝 หมายเหตุ

- ก่อนหน้านี้เคยใช้ `systemd` (`rtl-sdr-web.service`) แต่ในสภาพแวดล้อมปัจจุบันไม่ได้ติดตั้ง service นี้ไว้ (รันเป็น uvicorn process ตรงๆ แทน)
- ถ้าต้องการใช้ systemd อีกครั้ง ให้สร้าง unit file ที่ `/etc/systemd/system/rtl-sdr-web.service` แล้ว `systemctl daemon-reload && systemctl enable --now rtl-sdr-web`

---

_อัปเดตล่าสุด: 2026-06-18_