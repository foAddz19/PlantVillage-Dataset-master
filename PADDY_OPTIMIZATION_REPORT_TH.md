# ผล Optimize โมเดลข้าว AI Plant Device — 3 พฤศจิกายน 2569

โมเดล INT8 ที่แอปใช้งานได้ **88.64% บนชุด test เดิม 1,549 ภาพ** ผ่านเป้าหมาย 80% และติดตั้งใน `models/paddy` แล้ว เพิ่มจากรุ่นเดิม 12.65 จุดเปอร์เซ็นต์ โดยคง MobileNetV2 alpha 0.5, input RGB 160×160 และ 719,034 พารามิเตอร์เท่าเดิม

## เปรียบเทียบผลจริง

| รายการ | รุ่นเดิม | หลัง Optimize |
|---|---:|---:|
| ทำนายอันดับแรกถูก | 1,177 / 1,549 | 1,373 / 1,549 |
| INT8 test accuracy | 75.98% | **88.64%** |
| INT8 test top-3 | 94.06% | 97.74% |
| INT8 test balanced accuracy | 75.84% | 88.05% |
| Float test accuracy | 77.79% | 89.22% |
| ขนาด TFLite | 1,005,784 ไบต์ | 1,005,784 ไบต์ |
| Median resize + inference บน Windows | 3.16 ms | 3.17 ms |
| RAM สูงสุดของโปรเซส benchmark | 55.59 MiB | 55.55 MiB |

INT8 ต่างจาก float รุ่นใหม่ 0.58 จุดเปอร์เซ็นต์; macro F1 ของ INT8 เท่ากับ 0.8842 เวลาและ RAM แทบเท่ารุ่นเดิมในการวัดครั้งนี้ ไม่ได้เพิ่มขนาดภาพ จำนวนโมเดล หรือทำ inference หลายรอบต่อคำขอ

Benchmark เป็นโปรเซสใหม่ทั้งสองรุ่นบน Windows-11-10.0.26200-SP0, Python 3.13.13, ภาพเดียวกัน 100 รอบ จำกัด CPU affinity 2 logical CPUs และ BelowNormal priority รวม resize/quantization/inference/จัดผล ไม่รวมถอดรหัสภาพ กล้อง Flask Chromium และระบบปฏิบัติการ **ยังไม่ได้วัด Raspberry Pi 5 จริง** ตัวเลขนี้จึงไม่ใช่ RAM ทั้งเครื่องหรือความเร็วบน Pi และไม่ควรตีความส่วนต่างเล็กน้อยเป็นการเร็วขึ้น/ประหยัด RAM อย่างมีนัยสำคัญ

## วิธีปรับโมเดลโดยจำกัดภาระเครื่อง

- เก็บน้ำหนักเดิมและ metadata ไว้ที่ `models/backups/paddy-before-optimize-20261103/`
- ใช้ manifest เดิม: train 7,027, validation 1,757 และ test 1,549 ภาพ รวม 10,333 ภาพหลังตัด SHA256 ซ้ำ 74 ภาพจาก 10,407 ภาพติดป้ายกำกับ ไม่ใช้ภาพแข่งขัน 3,469 ภาพที่ไม่มีเฉลย
- ฝึกต่อจาก `.keras` เดิม เพิ่มส่วนที่ฝึกได้เป็น 60 ชั้นท้ายของ backbone โดยหยุด BatchNormalization ไว้ ใช้ Adam เริ่ม learning rate 0.00003 ลดตาม validation loss, batch 8 และจบครบ 12 epochs
- เสริมภาพเฉพาะ train: horizontal flip, rotation 0.06 และ contrast 0.12 โหลดภาพแบบ streaming, prefetch 1, จำกัด 2 logical CPUs/priority ต่ำกว่าปกติ ฝึกบน PC แล้วใช้เฉพาะ INT8 บน Pi
- เลือก checkpoint ด้วย validation เท่านั้น ทั้ง best-accuracy และ best-loss ได้ checkpoint epoch 12 และ SHA เดียวกัน: float validation 89.19%, INT8 validation 88.39%
- Quantization calibration ใช้ภาพ train 512 ภาพแบ่งใกล้เคียงกันครบ 10 กลุ่ม ไม่ใช้ validation/test; ใช้ input/output uint8 และ INT8 operators
- หลังเลือกแล้วตรวจ test ของผู้ชนะผ่าน `PaddyService` / LiteRT CompiledModel CPU ตัวเดียวกับแอป ตรวจ SHA ทุกภาพและยืนยันผล float ของ checkpoint เดียวกัน
- ใช้เวลาช่วงฝึก/ประเมิน validation/ส่งออกประมาณ 29.7 นาที (ไม่นับการเตรียมเริ่มต้น) RAM peak ตลอดงาน TensorFlow รวม export 801.43 MiB; ไม่ใช่ RAM ตอนใช้งานบน Pi

Frozen manifest SHA256: `6d4f3d4f3386dc20eabfa35ed6972f6a3a75205af9fecc87d1b55a22bfcae5e5`

รุ่นเดิมเคยรายงานผลบน test นี้แล้ว รอบนี้เป็นการยืนยัน benchmark เดิม ไม่ใช่ชุดทดสอบภาคสนามใหม่ ไม่ใช้ผล test เลือก checkpoint หรือปรับค่ารอบนี้ ข้อมูลไม่มีรหัสใบ/ต้นจริง จึงยังรับประกันไม่ได้ว่าใบเดียวกันจากคนละมุมไม่ข้ามชุด คะแนนโมเดลยังไม่ผ่าน calibration เป็นโอกาสยืนยันโรค และยังไม่ได้ทดสอบกล้องในแปลงจริง

## ผลรายกลุ่มของโมเดล INT8

| กลุ่ม | ถูก / ภาพ | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| bacterial_leaf_blight | 59 / 71 | 92.19% | 83.10% | 87.41% |
| bacterial_leaf_streak | 52 / 57 | 91.23% | 91.23% | 91.23% |
| bacterial_panicle_blight | 45 / 50 | 95.74% | 90.00% | 92.78% |
| blast | 238 / 259 | 82.64% | 91.89% | 87.02% |
| brown_spot | 118 / 143 | 87.41% | 82.52% | 84.89% |
| dead_heart | 198 / 214 | 99.00% | 92.52% | 95.65% |
| downy_mildew | 77 / 93 | 77.00% | 82.80% | 79.79% |
| hispa | 217 / 238 | 87.85% | 91.18% | 89.48% |
| normal | 223 / 262 | 96.12% | 85.11% | 90.28% |
| tungro | 146 / 162 | 81.56% | 90.12% | 85.63% |

ใบขอบแห้ง (`bacterial_leaf_blight`) recall เพิ่มจาก 38/71 (53.52%) เป็น 59/71 (83.10%) ส่วน `downy_mildew` ยังมี F1 ต่ำที่สุดใน 10 กลุ่ม ควรเก็บภาพภาคสนามที่ผู้เชี่ยวชาญยืนยันเพิ่มในรอบถัดไป

คู่ที่ยังสับสนมากที่สุด:

- `normal` → `hispa`: 15 ภาพ
- `tungro` → `blast`: 11 ภาพ
- `normal` → `blast`: 10 ภาพ
- `brown_spot` → `blast`: 9 ภาพ
- `hispa` → `tungro`: 7 ภาพ

ใบปกติที่ถูกทำนายเป็นโรค/แมลงยังเกิดได้ ผล AI จึงใช้คัดกรองร่วมกับการดูต้นและแปลงจริง คู่มือดูแลและเกณฑ์ผลไม่ชัดเจนยังทำงานตามเดิม

## ไฟล์และการใช้งาน

- Config: `models/paddy/model.json`
- น้ำหนักใช้งาน: `models/paddy/paddy-b22b66717baa6dfc.tflite`
- น้ำหนักฝึกต่อบน PC: `models/paddy/paddy-b22b66717baa6dfc.keras` (ไม่รวมในชุด Pi)
- SHA256: `b22b66717baa6dfcc70c843af5265bd175794291c0f8d78d3b81a980b2bf026c`
- [ผล test / confusion matrix](test-results/paddy-optimized-test.json), [validation](test-results/paddy-optimized-validation.json), [float test](test-results/paddy-optimized-float-test.json)
- [benchmark รุ่นใหม่](test-results/paddy-optimized-benchmark-windows.json), [benchmark รุ่นเดิม](test-results/paddy-baseline-benchmark-windows.json), [การเลือก checkpoint](test-results/paddy-optimization-selection.json), [ประวัติฝึก](test-results/paddy-optimization-history.json)
- `optimize_paddy.py`: ฝึกต่อแบบจำกัด CPU และสร้าง candidate ซึ่ง headline เป็น validation ยังไม่พร้อมแทนรุ่นจริง ต้องตรวจ test และปรับ metadata ให้ตรงก่อนใช้
- `evaluate_paddy_candidate.py`: ตรวจ frozen manifest/SHA และประเมิน candidate ด้วย runtime ของแอป ใช้ validation เลือกและ test ยืนยันครั้งสุดท้าย
- [ชุดสำหรับ Raspberry Pi](dist/ai-plant-pi.zip): แตกเป็นโฟลเดอร์ `ai-plant/` ใช้ `model.json` พร้อม `.tflite` ที่อ้างถึง ไม่ต้องย้ายชุดฝึก `.keras` หรือ `.venv-training` และไม่ต้องติดตั้ง TensorFlow

PC: ปิดแอปเดิมด้วย `stop.bat` แล้วเปิด `start_paddy.bat` เพื่อโหลดน้ำหนักใหม่ บน Pi ใช้ `sh start_pi.sh` และวัดซ้ำด้วย `.venv/bin/python benchmark_paddy.py --image /path/to/rice.jpg --iterations 30` หากอัปเดตเครื่องที่มีข้อมูลแล้ว ให้คงโฟลเดอร์ `data/` และการตั้งค่าเครื่องพิมพ์ของเครื่องนั้นไว้

## การตรวจสอบ

ผ่าน 64 tests ใน `tests.test_optimization`, `tests.test_paddy`, `tests.test_device`, `tests.test_care`, `tests.test_printing` และ `tests.test_delete` รวมการโหลด INT8 จริง การอัปโหลด/บันทึก และการป้องกัน hash/tensor/ลำดับ label ผิด ใช้ฐานข้อมูลชั่วคราวทั้งหมด ไม่รันโมเดล PlantVillage ขนาดใหญ่

Edge headless ผ่านตัวอย่างภาพ ทำนาย รายงาน 10 กลุ่ม ปิดฝึกผ่านเว็บ และหน้า mobile โดยไม่มี JavaScript error ภาพรายงาน `test-results/paddy-*.png` สร้างใหม่ด้วยโมเดลที่เลือก ขั้นตอนจัด ZIP และตรวจชุดที่แตกแล้วบันทึกใน `test-results/paddy-optimization-release.json`
