# การใช้โมเดลข้าว Paddy Doctor

**พิมพ์ใบผลตรวจ:** รองรับการส่ง ESC/POS ผ่าน USB พร้อมฟอนต์ไทย ดู [PRINTER_GUIDE_TH.md](PRINTER_GUIDE_TH.md) เมื่อย้ายไป Pi ให้ใช้แพ็กเกจ `dist/ai-plant-pi.zip` ซึ่งรวม `assets/fonts/`, `config/printer.example.json`, `printer_tools.py` และ `requirements-printer.txt` แล้ว การฝึกโมเดลตามคู่มือนี้ทำบน PC ที่เก็บโปรเจกต์ต้นฉบับ


ผลโมเดลที่ฝึกจาก ZIP บนเครื่องนี้และผลวัด RAM/เวลา อยู่ใน [PADDY_MODEL_REPORT_TH.md](PADDY_MODEL_REPORT_TH.md)

โปรเจกต์มี backend สองชุด แยกน้ำหนักและไลบรารีที่โหลด:

- `plantvillage`: ระบบเดิม Extra Trees สำหรับพืช 38 กลุ่ม ใช้ `models/classifier.joblib`
- `paddy`: MobileNetV2 + Global Average Pooling ส่งออก INT8 ใช้ `models/paddy/model.json` และไฟล์ `.tflite` ที่ JSON ระบุ รองรับชุดต้นฉบับ 13 คลาสและชุดแข่งขัน 10 คลาส โดยตรวจรายชื่อและลำดับให้ตรงกัน

โหมดข้าวไม่โหลด joblib, scikit-learn หรือ TensorFlow และไม่เลือกใช้โมเดล PlantVillage แทนเมื่อหาโมเดลข้าวไม่พบ หน้าเว็บจะแสดงว่ายังไม่พร้อมใช้

## ชุดข้อมูลบนเครื่องนี้

`D:\paddy-doctor-dataset-main` เป็น repository โค้ดและภาพประกอบ ไม่ใช่ชุดภาพฝึก

ชุดที่ผู้ใช้ให้เพิ่มคือ `D:\IOT\paddy-disease-classification.zip` ขนาดประมาณ 1.09 GB มี:

- ภาพติดป้ายกำกับ 10,407 ภาพใน `train_images` และ `train.csv`
- ภาพแข่งขัน 3,469 ภาพใน `test_images` ที่ไม่มีคำตอบจริง จึงใช้คำนวณความแม่นยำไม่ได้
- 10 คลาส: bacterial_leaf_blight, bacterial_leaf_streak, bacterial_panicle_blight, blast, brown_spot, dead_heart, downy_mildew, hispa, normal, tungro

แตกเฉพาะภาพติดป้ายกำกับและ CSV ไว้ที่ `datasets/paddy-competition` โดยเก็บ ZIP ต้นฉบับไว้ ตรวจพบภาพซ้ำแบบ SHA256 ตรงกัน 74 ภาพ จึงใช้ 10,333 ภาพที่ไม่ซ้ำ ข้อมูลนี้ไม่มีรหัสใบจริง จึงยังรับประกันการแยกต้นหรือใบเดียวกันจากภาพคนละมุมไม่ได้

`dead_heart` คือป้ายอาการยอดแห้งในชุดแข่งขัน ไม่ได้แปลงเป็น black/white/yellow stem borer เพราะข้อมูลนี้ไม่แยกสามกลุ่มนั้น ส่วนชุด 13 คลาสต้องมีคลาสครบตามต้นฉบับ รวม leaf_roller และสามชนิด stem borer ไม่มีการเติมน้ำหนักหรือคลาสที่ไม่ได้ฝึก

## เปิดโปรแกรมบน Windows

ดับเบิลคลิก `start_paddy.bat` เพื่อเปิดหน้าอุปกรณ์ในโหมดข้าว หรือใช้:

```powershell
.venv\Scripts\python.exe app.py --model paddy --inference-only --device --open-browser --dataset-dir datasets\paddy-competition
```

เมื่อเปิดโดยไม่ระบุ dataset จะยังวิเคราะห์ภาพอัปโหลดและกล้องได้ แต่ไม่มีคลังภาพตัวอย่าง ถ้ามีแอปเดิมเปิดอยู่บนพอร์ต 8765 ให้ปิดหน้าต่างเซิร์ฟเวอร์เดิม หรือเพิ่ม `--port 8766` เพื่อเปิดแยกกัน

ระบบเดิมยังเปิดด้วย `start.bat` หรือ `app.py --model plantvillage` ได้ น้ำหนักเดิมและประวัติการสแกนไม่ได้ถูกแปลงหรือแทนที่

## ติดตั้งและฝึกบน PC

ไลบรารีสำหรับเว็บอยู่ใน `.venv` และสำหรับฝึกอยู่ใน `.venv-training` อย่าฝึกบน Raspberry Pi RAM 2GB

```powershell
py -3.13 -m venv .venv-training
.venv-training\Scripts\python.exe -m pip install -r requirements-training.txt
.venv-training\Scripts\python.exe train_paddy.py --dataset datasets\paddy-competition --check-data
.venv-training\Scripts\python.exe train_paddy.py --dataset datasets\paddy-competition --epochs 15 --image-size 160 --batch-size 32 --fine-tune-epochs 4
```

ตัวฝึกรองรับ `root/class/*.jpg`, `root/train_images/class/*.jpg`, โฟลเดอร์ `train/validation/test` และ CSV ที่มี `image_id`/`filename`/`path` พร้อม `label` ถ้ามี `leaf_id`, `plant_id` หรือ `group` จะใช้รวมภาพก่อนแบ่งชุด มีการตรวจภาพซ้ำข้ามคลาสและข้ามชุด รวมถึงเส้นทางที่หลุดออกนอกโฟลเดอร์ข้อมูล

หากไม่มีชุด validation/test ที่ติดป้ายกำกับ จะกัน test ประมาณ 15% ต่อคลาส จากนั้นกัน validation ประมาณ 20% ของส่วนที่เหลือ ที่เหลือใช้ฝึก ภาพ test ไม่ใช้เลือก epoch หรือปรับ quantization ภาพจากกลุ่มใบเดียวกันจะอยู่ชุดเดียวกันเมื่อมีรหัสใบ

ตัวฝึกใช้ ImageNet backbone, Rescaling ในโมเดล, Global Average Pooling และ Dense ขนาดเล็ก ค่าเริ่มต้นคำนวณ embeddings ของภาพต้นฉบับและภาพเสริมหนึ่งมุมครั้งเดียว เพื่อฝึกหัวจำแนกบน CPU ได้เร็ว จากนั้นเลือก fine tune ได้ ค่า `--no-cache-features` ใช้ฝึกหัวด้วยภาพเสริมใหม่แต่ละรอบ โดยใช้เวลามากขึ้น

หลังฝึกจะประเมินทั้งโมเดล float และ INT8 บนชุดทดสอบเดียวกัน หน้าเว็บแสดงผลของ **ไฟล์ INT8 ที่ส่งออกจริง** ไม่ใช้คะแนนของ float แทน มีค่า confidence threshold เริ่มต้น 0.6 และระยะห่างอันดับ 1–2 ที่ 0.15 ซึ่งยังไม่ผ่านการปรับเทียบจากภาพภาคสนาม

ไฟล์ผลลัพธ์:

- `models/paddy/model.json`: ลำดับคลาส, ขนาดภาพ, dtype, quantization, SHA256, ผลทดสอบและรายการแบ่งชุด
- `models/paddy/paddy-<hash>.tflite`: น้ำหนัก fully integer สำหรับวิเคราะห์
- `models/paddy/paddy-<hash>.keras`: โมเดลแก้ไขได้บน PC ไม่ต้องนำไป Pi

Preprocessing: อ่านภาพพร้อม EXIF, แปลง RGB, ย่อแบบ bilinear/stretch เป็นขนาดใน JSON, ส่งค่า RGB ช่วง 0–255 ผ่าน input quantization; การแปลงเป็นช่วง −1 ถึง 1 อยู่ในโมเดล ห้ามหาร 255 ซ้ำก่อนส่งเข้าโมเดล

## Raspberry Pi 5 RAM 2GB

ใช้ Raspberry Pi OS 64-bit และ Python ที่มี wheel ของ LiteRT รองรับ แนะนำเริ่มด้วย Python 3.11–3.13 ใน virtual environment ใหม่:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-pi.txt
sh start_pi.sh
```

คัดลอก `model.json` และ `.tflite` ที่อ้างอิงมาใน `models/paddy` ไม่ต้องคัดลอกชุดภาพ, `.keras`, `.venv-training` หรือโมเดล joblib เดิม `.venv` ของ Windows ใช้บน Linux ไม่ได้

โหมดข้าวประมวลผลทีละคำขอผ่าน inference lock ปิดการฝึกจากเว็บ และใช้หน่วยความจำเฉพาะโมเดล LiteRT ขนาดเล็กกับภาพปัจจุบัน อย่างไรก็ตาม ขนาดไฟล์โมเดลไม่ใช่ขนาด RAM ทั้งโปรเซส และยังต้องวัด latency/RAM จริงบน Pi พร้อมกล้อง เบราว์เซอร์ และระบบปฏิบัติการ

คำขอวิเคราะห์ซ้อนกันจะได้ HTTP 429 ก่อนเริ่มถอดรหัสภาพที่สอง เพื่อลด RAM ทั้งจากโมเดลและภาพ ระหว่างใช้หลายแท็บให้รอคำขอแรกเสร็จก่อน ลองวัดบน Pi ด้วย:

```bash
.venv/bin/python benchmark_paddy.py --image /path/to/rice.jpg --iterations 30
```

ค่านี้รวมการย่อภาพและ inference ในโปรเซส Python เดียว แต่ไม่รวมกล้อง Flask เบราว์เซอร์ หรือ RAM ของ OS บน PC สามารถเพิ่ม `--evaluate --dataset-dir datasets/paddy-competition` เพื่อตรวจผล INT8 กับรายการ test ใน manifest ทั้งหมดได้

โมเดล PlantVillage เดิมแม้ไฟล์บีบอัดประมาณ 280 MiB แต่ข้อมูลต้นไม้เมื่อโหลดใช้ราว 1.8 GiB และเคยวัดยอดการโหลดสูงกว่า 3 GiB บน PC จึงไม่เหมาะกับ Pi 2GB ควรเปิดด้วย `--model paddy` เสมอ

กล้องขณะนี้ใช้ browser getUserMedia ผ่าน localhost ต้องมี Chromium และกล้องที่ระบบมองเห็น ยังไม่ได้เพิ่ม Picamera2, GPIO LED, GPS จริง หรือวงจรวัดแบตเตอรี่ แผนที่ภายนอกต้องใช้อินเทอร์เน็ต ส่วน inference ไม่ต้องใช้อินเทอร์เน็ตเมื่อมีไฟล์โมเดลแล้ว

## ทดสอบ

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
# ทดสอบ LiteRT กับโมเดลจริงเพิ่ม:
$env:PADDY_TEST_MODEL_DIR = (Resolve-Path models\paddy).Path
.venv\Scripts\python.exe -m unittest tests.test_paddy -v
```

`tests/export_paddy_fixture.py` สร้างโมเดลสังเคราะห์เพื่อทดสอบการส่งออกในโฟลเดอร์ชั่วคราวเท่านั้น ไม่ใช่โมเดลตรวจโรคและห้ามนำไปใช้อ้างความแม่นยำ

อ้างอิง: [Paddy Doctor repository](https://github.com/paddydoc/paddy-doctor-dataset), [ชุดแข่งขัน 10 คลาส](https://www.kaggle.com/competitions/paddy-disease-classification/data), [LiteRT Python CPU](https://developers.google.com/edge/litert/next/python), [integer quantization](https://developers.google.com/edge/litert/conversion/tensorflow/quantization/post_training_integer_quant)
