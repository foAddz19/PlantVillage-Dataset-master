# ผลโมเดลข้าว Paddy Doctor — 3 พฤศจิกายน 2569

โมเดล INT8 รุ่นปัจจุบันได้ **88.64% บน test เดิม 1,549 ภาพ** สูงกว่าเป้าหมาย 80% และรุ่นก่อนที่ได้ 75.98% โดยคงขนาดโมเดลและภาพนำเข้าเท่าเดิม เปิดด้วย `start_paddy.bat` ดูรายละเอียดใน [รายงาน Optimize](PADDY_OPTIMIZATION_REPORT_TH.md) และวิธีติดตั้ง/ย้ายไป Pi ใน [คู่มือข้าว](PADDY_GUIDE_TH.md)

## ผลที่ตรวจสอบแล้ว

| รุ่น / ชุดประเมิน | อันดับแรกถูกต้อง | อยู่ใน 3 อันดับ | Balanced accuracy |
|---|---:|---:|---:|
| INT8 รุ่นก่อน / test เดิม | 75.98% (1,177/1,549) | 94.06% | 75.84% |
| Float รุ่นใหม่ / test เดิม | 89.22% | 97.93% | 88.01% |
| **INT8 ที่แอปใช้ / test เดิม** | **88.64% (1,373/1,549)** | **97.74%** | **88.05%** |
| INT8 รุ่นใหม่ / validation | 88.39% (1,553/1,757) | 97.15% | 87.55% |

INT8 ตอบ test ถูกเพิ่ม 196 ภาพ หรือ **12.65 จุดเปอร์เซ็นต์** และมี macro F1 88.42% การแปลงจาก float เป็น INT8 ลด accuracy ประมาณ 0.58 จุดเปอร์เซ็นต์ ผล INT8 ตรวจด้วย `PaddyService` / LiteRT CompiledModel CPU ที่แอปใช้งานจริง

เลือก checkpoint ด้วย validation หลังแปลง INT8 ก่อนตรวจ test ของรุ่นที่เลือก ใช้ manifest เดิมและตรวจ SHA256 ภาพครบ 1,549 ภาพ ชุด test นี้เคยใช้รายงานรุ่นเดิมแล้ว จึงเป็นการยืนยันบน benchmark เดิม ไม่ใช่ชุดทดสอบภาคสนามใหม่ที่ไม่เคยเปิดดู

แหล่งผลจริง: [INT8 test](test-results/paddy-optimized-test.json), [INT8 validation](test-results/paddy-optimized-validation.json), [float test](test-results/paddy-optimized-float-test.json)

## ข้อมูลและวิธีฝึก

- ใช้ `D:\IOT\paddy-disease-classification.zip`: ชุดแข่งขัน **10 กลุ่ม** รวม `dead_heart` ไม่ใช่ไฟล์น้ำหนักสำหรับชุดต้นฉบับ 13 กลุ่ม
- ภาพติดป้ายกำกับ 10,407 ภาพ ตัดภาพซ้ำ SHA256 ตรงกัน 74 ภาพ เหลือ 10,333 ภาพ: train 7,027, validation 1,757, test 1,549 ภาพ ไม่ใช้ภาพแข่งขัน 3,469 ภาพที่ไม่มีเฉลยอ้างความแม่นยำ
- MobileNetV2 alpha 0.5, RGB 160×160, Global Average Pooling, Dropout และ Dense 10 กลุ่ม รวม **719,034 พารามิเตอร์**
- รุ่นเดิมเริ่มจาก ImageNet ฝึกหัว 15 epochs และ fine tune 4 epochs ครั้งนี้ฝึกต่ออีก 12 epochs โดยเปิด 60 ชั้นท้ายของ backbone แต่คง BatchNormalization ให้หยุดเรียนรู้
- Adam เริ่มที่ 0.00003, batch 8, ลด learning rate ตาม validation loss และเก็บ checkpoint ตาม validation accuracy/loss ทั้งสองเกณฑ์เลือกรุ่น epoch 12 และส่งออกได้ SHA256 เดียวกัน
- Quantization calibration ใช้ภาพ train 512 ภาพ แบ่งต่อกลุ่ม 51–52 ภาพ ไม่มีภาพ validation/test อยู่ใน calibration
- ฝึกบน PC จำกัด 2 logical CPUs และ priority ต่ำกว่าปกติ ไม่มีการฝึกบน Pi และไม่มี ensemble หรือการทำนายหลายรอบเพิ่มในขั้นตอนใช้งาน

Manifest SHA256: `6d4f3d4f3386dc20eabfa35ed6972f6a3a75205af9fecc87d1b55a22bfcae5e5`

## ผลแยกกลุ่มของ INT8 รุ่นปัจจุบันบน test

| กลุ่ม | Precision | Recall | F1 | จำนวน test |
|---|---:|---:|---:|---:|
| โรคขอบใบแห้ง | 92.19% | 83.10% | 87.41% | 71 |
| โรคใบขีดโปร่งแสง | 91.23% | 91.23% | 91.23% | 57 |
| โรครวงไหม้จากแบคทีเรีย | 95.74% | 90.00% | 92.78% | 50 |
| โรคไหม้ | 82.64% | 91.89% | 87.02% | 259 |
| โรคใบจุดสีน้ำตาล | 87.41% | 82.52% | 84.89% | 143 |
| อาการยอดแห้ง (Dead heart) | 99.00% | 92.52% | 95.65% | 214 |
| โรคราน้ำค้าง | 77.00% | 82.80% | 79.79% | 93 |
| ความเสียหายจากแมลง Hispa | 87.85% | 91.18% | 89.48% | 238 |
| ใบปกติ | 96.12% | 85.11% | 90.28% | 262 |
| โรคใบสีส้ม (ทังโกร) | 81.56% | 90.12% | 85.63% | 162 |

ยังมีผลผิด 176 ภาพ คู่ที่สับสนมาก ได้แก่ ใบปกติ → Hispa 15 ภาพ, ทังโกร → โรคไหม้ 11 ภาพ และใบปกติ → โรคไหม้ 10 ภาพ จึงยังต้องตรวจอาการประกอบก่อนเลือกวิธีจัดการ

## ขนาดและภาระตอนใช้งาน

วัดด้วยวิธีเดียวกันบน Windows 11 / Python 3.13.13 ในโปรเซสใหม่ แยกรุ่นละ 100 ครั้งหลัง warm-up จำกัด CPU affinity 2 logical CPUs และ priority BelowNormal:

| รายการ | รุ่นก่อน | รุ่นปัจจุบัน |
|---|---:|---:|
| ขนาด `.tflite` | 1,005,784 bytes | 1,005,784 bytes |
| Median resize + quantization + inference | 3.16 ms | 3.17 ms |
| RAM โปรเซสปัจจุบัน | 54.70 MiB | 54.66 MiB |
| RAM โปรเซสสูงสุด | 55.59 MiB | 55.55 MiB |

ขนาดไฟล์เท่าเดิมและค่าที่วัดได้ใกล้เคียงกัน ความต่างเล็กน้อยของเวลา/RAM ไม่ใช่หลักฐานว่าเร็วขึ้นหรือประหยัด RAM มากขึ้น Input เป็น `uint8 [1,160,160,3]`, scale 1.0, zero point 0 รับ RGB 0–255 และ normalize ภายในโมเดล

ผลวัดรวมการย่อภาพ quantization และ inference แต่ไม่รวมการอ่านไฟล์ภาพ กล้อง Flask Chromium หรือระบบปฏิบัติการ **ยังไม่มีผลวัด Raspberry Pi 5 จริง** ใช้ `benchmark_paddy.py` วัดซ้ำบนบอร์ดพร้อมอุปกรณ์ที่ใช้งานจริง รุ่นข้าวไม่โหลด TensorFlow/scikit-learn/joblib ปิดการฝึกผ่านเว็บ และปฏิเสธคำขอวิเคราะห์ซ้อนก่อนถอดรหัสภาพที่สอง

ผลวัดต้นฉบับ: [รุ่นเดิม](test-results/paddy-baseline-benchmark-windows.json), [รุ่นปัจจุบัน](test-results/paddy-optimized-benchmark-windows.json)

## ไฟล์ใช้งานและข้อจำกัด

- `models/paddy/model.json`: ลำดับกลุ่ม tensor contract, SHA256, metadata ผล test และ manifest
- `models/paddy/paddy-b22b66717baa6dfc.tflite`: น้ำหนักใช้งานจริงบน PC/Pi
- `models/paddy/paddy-b22b66717baa6dfc.keras`: โมเดลแก้ไขได้บน PC ไม่ต้องนำไป Pi
- `models/backups/paddy-before-optimize-20261103`: รุ่นเดิมสำหรับย้อนกลับ
- `optimize_paddy.py` และ `evaluate_paddy_candidate.py`: ฝึกต่อแบบจำกัด CPU และประเมินตาม manifest ที่ตรวจสอบได้

SHA256 รุ่นปัจจุบัน: `b22b66717baa6dfcc70c843af5265bd175794291c0f8d78d3b81a980b2bf026c`

ข้อมูลไม่มีรหัสใบ/ต้นจริง แม้ไม่มี SHA256 หรือ group เดียวกันข้ามชุด ก็ยังรับประกันไม่ได้ว่าภาพคนละมุมเป็นคนละใบ คะแนนต่อภาพยังไม่ปรับเทียบเป็นโอกาสยืนยันโรค และยังไม่มีการยืนยันกับกล้อง/แปลงของผู้ใช้ ระบบอาจเลือกกลุ่มโรคที่รู้จักแม้ภาพไม่ใช่ข้าว

ตรวจระบบผ่าน **64 tests** รวมโมเดลจริงและการป้องกันแก้ manifest/calibration ที่ปะปนชุดข้อมูล ตรวจหน้าเว็บ Edge รุ่นข้าวใหม่ผ่าน desktop/mobile โดยไม่พบ JavaScript error ดู [หน้ารายงาน](test-results/paddy-report.png) และรายละเอียดใน [รายงาน Optimize](PADDY_OPTIMIZATION_REPORT_TH.md) ยังไม่ได้ทดสอบฮาร์ดแวร์ Pi หรือ USB printer จริง

การเปลี่ยนโมเดลไม่ต้องแก้ประวัติสำรวจใน `data/` และโมเดล PlantVillage เดิมยังแยกอยู่ต่างหาก
