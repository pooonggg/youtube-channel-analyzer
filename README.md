# YouTube Channel Analyzer — คลังคลิปน่าดู

ดึงคลิปและคอมเมนต์จาก YouTube Data API v3 (ฟรี 10,000 units/วัน) เก็บใน SQLite
ให้ Claude อ่านคอมเมนต์แล้วให้คะแนน และแสดงเป็นคลังคลิปแบบ YouTube พร้อมตัวกรอง การเรียง 2 ชั้น
และปุ่มสถานะ ดูแล้ว / สนใจ / ไม่สนใจ

## รันหน้าเว็บ

```
C:\Users\joker\anaconda3\envs\STT\python.exe -m streamlit run app.py
```

ต้องมี `.streamlit/secrets.toml` ที่มี `YOUTUBE_API_KEY` (ดู `secrets.toml.example`)

## ดึงข้อมูล (หน้าเว็บแถบซ้าย หรือ CLI)

```
python ingest.py video <ลิงก์คลิป>            # คลิปเดียว + คอมเมนต์ทั้งหมด
python ingest.py channel <ลิงก์ช่อง>          # เฉพาะคลิปใหม่ของช่อง + คอมเมนต์
python ingest.py refresh                      # อัปเดตยอดวิวเฉพาะคลิปที่ สนใจ/ดูแล้ว
python ingest.py quota                        # โควตาที่ใช้วันนี้
```

กฎประหยัดโควตา: ดึงช่องซ้ำจะหยุดเมื่อเจอคลิปที่มีอยู่แล้ว, คอมเมนต์ดึงครั้งเดียวต่อคลิป
(`--recheck-comments` ดึงใหม่เมื่อจำนวนเพิ่ม ≥10%), refresh แตะเฉพาะคลิปที่ทำเครื่องหมายไว้

## ขั้นตอนให้คะแนน (Claude)

1. `python ingest.py export-scoring` → ไฟล์ `data/to_score/<เวลา>.md` (คลิปที่ยังไม่มีคะแนน + คอมเมนต์ ไม่มีชื่อผู้คอมเมนต์)
2. Claude เขียน `data/scores/<ชื่อ>.json`:
   `[{"video_id": "...", "score": 0-100, "summary": "markdown", "reasons": "ข้อความสั้น"}]`
3. `python ingest.py import-scores data/scores/<ชื่อ>.json` → ขึ้นหน้าเว็บทันที (กดรีเฟรช)

## โครงสร้าง

- `yt_api.py` เรียก API (นับโควตาทุก request)
- `db.py` schema SQLite: channels, videos, comments, scores, user_status, quota_log
- `ingest.py` ดึงข้อมูล / export / import คะแนน
- `app.py` หน้าเว็บ Streamlit
- `data/` ฐานข้อมูลและไฟล์คะแนน (ไม่ commit — มีชื่อผู้คอมเมนต์)

## ทดสอบ

```
C:\Users\joker\anaconda3\envs\STT\python.exe -m pytest -q tests
```

## หน้าเว็บสาธารณะ (GitHub Pages)

`python build_site.py` สร้าง `docs/index.html` เป็นหน้าเว็บไฟล์เดียว (ไม่ต้องมีเซิร์ฟเวอร์) จากฐานข้อมูล
แล้ว commit + push → GitHub Pages เสิร์ฟจากโฟลเดอร์ `docs/` ของ branch `main`
หน้าเว็บนี้มีแค่ข้อมูลคลิปและบทสรุปของ Claude (ไม่มีคอมเมนต์ดิบหรือชื่อผู้คอมเมนต์)
สถานะ ดูแล้ว/สนใจ/ไม่สนใจ บนหน้าสาธารณะเก็บในเบราว์เซอร์ของผู้ชมแต่ละคน
