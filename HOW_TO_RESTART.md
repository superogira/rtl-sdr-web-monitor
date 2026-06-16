# 🚀 How to Restart & Check (Quick Reference)

> ไฟล์นี้สรุปคำสั่งสั้นๆ สำหรับ restart server และตรวจสอบสถานะ
> รายละเอียดเต็มอยู่ใน `OPERATIONS.md` section 4

---

## 📋 สรุปสภาพแวดล้อมปัจจุบัน

| รายการ | ค่า |
|---|---|
| **รูปแบบการรัน** | systemd service |
| **Service name** | `rtl-sdr-web.service` |
| **Path** | `/root/workspace/rtl-sdr-web` |
| **Python** | `/root/workspace/rtl-sdr-web/venv/bin/python3` |
| **Port** | `8000` |
| **การทำ Migration** | อัตโนมัติ (auto-add columns ใน `backend/database.py` `_run_migrations`) |

---

## 🔄 Restart Server (หลังแก้ Python code)

```bash
# Restart
sudo systemctl restart rtl-sdr-web

# ตรวจสถานะหลัง restart
systemctl is-active rtl-sdr-web
# ควรได้: active
```

> ⚠️ **แก้ไฟล์ CSS/JS/HTML** → ไม่ต้อง restart (refresh browser ด้วย Ctrl+Shift+R)
> ⚠️ **แก้ไฟล์ Python (.py)** → ต้อง restart

---

## 🩺 ตรวจสอบสถานะ (Health Check)

```bash
# 1. ตรวจว่า service รันอยู่ไหม
systemctl is-active rtl-sdr-web

# 2. HTTP health check
curl -s http://localhost:8000/api/health | python3 -m json.tool

# 3. ดู log แบบ real-time
journalctl -u rtl-sdr-web -f

# 4. ดู log 50 บรรทัดล่าสุด
journalctl -u rtl-sdr-web --no-pager -n 50

# 5. ตรวจ SDR status
curl -s http://localhost:8000/api/sdr/status | python3 -m json.tool
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
# Start (ถ้า service หยุดอยู่)
sudo systemctl start rtl-sdr-web

# Stop
sudo systemctl stop rtl-sdr-web

# Status แบบละเอียด
systemctl status rtl-sdr-web

# Enable (ให้ start อัตโนมัติตอน boot)
sudo systemctl enable rtl-sdr-web
```

---

## 🐛 ถ้ามีปัญหา

```bash
# Server crash / restart loop
journalctl -u rtl-sdr-web --no-pager -n 100

# ตรวจ syntax ก่อน restart
cd /root/workspace/rtl-sdr-web
./venv/bin/python3 -m py_compile backend/main.py

# ตรวจ port ชนกัน
ss -tlnp | grep 8000
```

---

_อัปเดตล่าสุด: 2025-06-16_