# พิมพ์ผลตรวจด้วย Xprinter XP-58IIH ผ่าน USB บน Raspberry Pi 5

เพิ่มปุ่ม **ดูใบผลตรวจ**, **พิมพ์ผล** และ **พิมพ์ซ้ำ** ในหน้า `/device` แล้ว ใช้ข้อมูลจากรายการที่บันทึกในฐานข้อมูลจริงเท่านั้น โปรแกรมไม่รับผลทำนายหรือคะแนนที่ส่งมาใหม่จากปุ่มพิมพ์

ใบพิมพ์มีชื่อ AI PLANT DEVICE, เวลาประเทศไทย UTC+07, รหัสรายการ, ผลเบื้องต้น, สามอันดับและคะแนนโมเดล, แหล่งพิกัดพร้อมป้ายจำลอง/ระบุเอง, ระดับอาการที่ผู้ตรวจระบุ และหมายเหตุ ผลที่ uncertain จะแสดงว่า **ยังระบุไม่ได้** และแสดงอันดับเป็นเพียงกลุ่มใกล้เคียง

## วิธีพิมพ์

1. เปิด `http://127.0.0.1:8765/device` บน Pi
2. ถ่าย/เลือกภาพ แล้วกดตรวจโรค
3. ใส่หมายเหตุและระดับอาการตามที่ผู้ตรวจประเมิน จากนั้นกด **บันทึกจุดตรวจ**
4. กด **ดูใบผลตรวจ** เพื่อตรวจรูปแบบ หรือ **พิมพ์ผล** เพื่อส่งไปเครื่องพิมพ์
5. รายการเก่าสามารถกดดูใบผลตรวจและพิมพ์ซ้ำในหน้าประวัติได้

ปุ่มพิมพ์จะเปิดเมื่อมีรายการที่บันทึกแล้วและตั้งค่า USB แล้ว การตั้งค่าไม่ได้ยืนยันว่าเครื่องมีไฟหรือกระดาษ: ระบบตรวจ USB เมื่อกดพิมพ์และแจ้งข้อผิดพลาดตามจริง

## ติดตั้งบน Pi

ต่ออะแดปเตอร์ของเครื่องพิมพ์ ใส่กระดาษความร้อน 58 มม. และต่อ USB เข้ากับ Pi จากนั้นรันในโฟลเดอร์ `~/ai-plant`:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip libusb-1.0-0 libfreetype6 usbutils
cd ~/ai-plant
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-pi.txt
```

`requirements-pi.txt` รวม `requirements-printer.txt` แล้ว สร้าง venv ใหม่เฉพาะเมื่อยังไม่มีบน Pi หากมี venv Linux เดิมให้ใช้ติดตั้ง requirements เพิ่มได้ ของ Windows ต้องสร้างใหม่บน Pi

## ค้นหาเครื่องพิมพ์และตั้งค่าจาก USB จริง

```bash
lsusb
.venv/bin/python printer_tools.py list-usb
.venv/bin/python printer_tools.py configure-usb
```

`configure-usb` ต้องพบอุปกรณ์ USB Printer Class ที่มี Bulk OUT เพียงหนึ่งรายการ จะอ่าน vendor/product ID, interface และ endpoint จาก descriptor จริง แล้วสร้าง:

- `config/printer.json`: ตั้งค่าพิมพ์ของเครื่องนี้
- `runtime/99-ai-plant-printer.rules`: กฎสิทธิ์ USB ที่ใช้ ID ของเครื่องจริง

ไม่มีการใส่ USB ID เดาของ XP-58IIH เพราะล็อตหรือรุ่นย่อยอาจต่างกัน หากพบหลายรายการ ใช้ ID ที่แสดงจาก `list-usb` เลือกด้วย `configure-usb --vendor-id XXXX --product-id YYYY` โดยแทน XXXX/YYYY ตามเครื่อง หากเคยตั้งค่าแล้วและตั้งใจเปลี่ยน ให้เพิ่ม `--replace`

ถ้า `lsusb` เห็นเครื่อง แต่ `list-usb` ว่าง อุปกรณ์อาจใช้ vendor-specific interface ให้ตรวจ `lsusb -v -d XXXX:YYYY` แล้วตั้ง `interface` และ `out_endpoint` ใน `config/printer.json` จากข้อมูลจริง โดยใช้ `config/printer.example.json` เป็นต้นแบบ

## ให้สิทธิ์ USB โดยใช้กลุ่ม lp

หลัง `configure-usb` สำเร็จ:

```bash
sudo install -m 0644 runtime/99-ai-plant-printer.rules /etc/udev/rules.d/99-ai-plant-printer.rules
sudo usermod -aG lp "$USER"
sudo udevadm control --reload-rules
```

ออกจากระบบแล้วเข้าใหม่เพื่อรับกลุ่ม `lp` และถอดเสียบสาย USB ของเครื่องพิมพ์ใหม่ หรือรีบูต Pi หนึ่งครั้ง ผู้ใช้ที่รันเว็บต้องเป็นสมาชิกกลุ่ม `lp` ด้วย ถ้าภายหลังตั้งเป็น systemd service ให้ใช้ผู้ใช้เดียวกัน

ระบบใช้ libusb โดยตรงและจะยึด interface เฉพาะช่วงงานพิมพ์ หาก usblp จับ interface อยู่จะถอดและคืนให้หลังส่งงาน ไม่ควรส่งงานจาก CUPS/โปรแกรมอื่นพร้อมกับแอปนี้

## ทดลองใบตัวอย่างก่อนใช้งาน

สร้าง PNG โดยไม่ส่งเข้าเครื่องพิมพ์:

```bash
.venv/bin/python printer_tools.py preview-demo
```

ไฟล์อยู่ที่ `runtime/receipt-demo.png` มีคำว่า **ใบตัวอย่าง / TEST PRINT** และระบุว่าไม่ใช่ผลวิเคราะห์จริง

เมื่อต่อเครื่องและตั้งสิทธิ์แล้ว คำสั่งต่อไปนี้จะส่งพิมพ์กระดาษจริงหนึ่งใบ:

```bash
.venv/bin/python printer_tools.py test-print
```

จากนั้นเปิดแอป:

```bash
sh start_pi.sh
```

## ภาษาไทยและขนาดใบพิมพ์

เรนเดอร์ฟอนต์ Noto Sans Thai ด้วย HarfBuzz/FreeType เป็นภาพขาวดำ แล้วสร้าง ESC/POS raster จึงไม่ต้องเลือก code page ไทยของเครื่องพิมพ์ และไม่พึ่ง libraqm ใน Pillow

ค่าตั้งต้นเป็น 384 จุดสำหรับพื้นที่พิมพ์ของเครื่อง 58 มม. ส่งเป็นชิ้นละ 64 แถว พัก 100 ms ระหว่างชิ้นเพื่อช่วยควบคุม buffer; ปรับได้ใน JSON หากจำเป็น มี timeout USB และจำกัดเวลาส่งงาน ไม่มีคำสั่งเปิดลิ้นชักหรือสั่งตัดกระดาษอัตโนมัติ ให้ฉีกที่ tear bar

ต้องคัดลอก `assets/fonts/NotoSansThai.ttf` พร้อม `OFL.txt` ไปด้วย ฟอนต์นี้รองรับใบผลตรวจไทยและอังกฤษ ขนาดและความเข้มจริงต้องดูจากกระดาษของเครื่องที่ใช้งาน


### ตัวอักษรและการจัดวางฉบับปรับปรุง

ใช้ฟอนต์ไทยน้ำหนักกลางถึงหนา ขนาดหัวเรื่อง 26 จุดภาพ ผลหลัก 29 จุดภาพ เนื้อหา 22 จุดภาพ และข้อมูลประกอบ 19 จุดภาพ จัดหัวเรื่องกลางกระดาษ แยกหมวดด้วยเส้น และวางคะแนนชิดขวาเป็นคอลัมน์เดียวกัน ชื่ออาการยาวจะขึ้นบรรทัดในคอลัมน์ของตนโดยไม่ชนคะแนน

เรนเดอร์ glyph แบบ monochrome พร้อม font hinting ที่ความกว้างจริง 384 จุด ไม่มีการขยาย ย่อ หรือทำ dithering ในข้อมูลส่งพิมพ์ ส่วนหน้าตัวอย่างคงขนาดภาพจริงเมื่อพื้นที่พอ ใช้ BudouX Thai ขนาดเล็กช่วยเลือกจุดขึ้นบรรทัด และคงพยัญชนะ/สระ/วรรณยุกต์ใน grapheme เดียวกัน กรณีข้อความยาวเกินหนึ่งคอลัมน์จึงค่อยแบ่งตาม grapheme

หลังคัดลอกโค้ดรุ่นนี้ไป Pi ให้รัน `.venv/bin/python -m pip install -r requirements-pi.txt` เพื่อเพิ่ม BudouX แล้วรีสตาร์ตแอป ผลบนกระดาษยังขึ้นกับหัวพิมพ์ กระดาษ และความเข้มของเครื่องจริง จึงควรใช้ `printer_tools.py test-print` ตรวจหนึ่งใบก่อนใช้งาน

## ความหมายของสถานะงานพิมพ์

- `pending`: บันทึกว่าจะส่งงานแล้ว ก่อนเริ่มเขียน USB
- `sent`: USB รับข้อมูลครบ ไม่ใช่การยืนยันว่าเครื่องพิมพ์ออกกระดาษครบแล้ว
- `unknown`: เกิดข้อผิดพลาดระหว่างส่ง อาจออกกระดาษบางส่วน จึงไม่มีการลองส่งซ้ำอัตโนมัติ

เก็บสถานะไว้ที่ `data/print_jobs.sqlite3` คำขอ UUID เดิมจะไม่ถูกส่งพิมพ์ซ้ำ แม้รีสตาร์ตแอป การกด **พิมพ์ซ้ำ** โดยผู้ใช้เป็นงานใหม่โดยตั้งใจ หลัง error ให้ตรวจเครื่องและใบพิมพ์ก่อนกดอีกครั้ง

## ไฟล์ที่เพิ่มสำหรับย้ายไป Pi

- `leaflab/printing.py`, `leaflab/receipt.py` รวมอยู่ในโฟลเดอร์ `leaflab`
- `assets/fonts/` ทั้งโฟลเดอร์
- `requirements-printer.txt` คู่กับ `requirements-pi.txt`
- `printer_tools.py`
- `config/printer.example.json` แล้วสร้าง `printer.json` บน Pi ด้วย configure-usb
- `templates/device.html`, `static/device.js`, `static/device.css`, `app.py`, `leaflab/device.py` รุ่นที่แก้ไขแล้ว

ใช้แพ็กเกจ `dist/ai-plant-pi.zip` เพื่อคัดลอกทั้งชุดได้ ภายในมีโฟลเดอร์ `ai-plant` และโมเดลข้าวจริง รวมไฟล์ฟอนต์/คู่มือ แต่ไม่รวมชุดภาพฝึก, venv Windows, โมเดล PlantVillage หรือข้อมูลสแกนส่วนตัว

## การตรวจสอบที่ทำบน PC

ตรวจภาพใบตัวอย่างภาษาไทย, ถอด raster กลับเทียบ bitmap, จำลอง descriptor/partial USB writes/สิทธิ์ USB, การคืน kernel driver, งานซ้ำหลังรีสตาร์ต, ข้อผิดพลาดที่อาจพิมพ์ไปบางส่วน, API และหน้าเว็บจริง โดยใช้ USB transport จำลอง **ยังไม่ได้ทดสอบกับ XP-58IIH ที่ต่อ Pi จริง**

อ้างอิง: [XP-58IIH](https://www.xprintermexico.com/product-page/xp58iih), [python-escpos usage](https://python-escpos.readthedocs.io/en/latest/user/usage.html), [PyUSB tutorial](https://github.com/pyusb/pyusb/blob/master/docs/tutorial.rst), [Noto Sans Thai และใบอนุญาต](https://github.com/google/fonts/tree/main/ofl/notosansthai)


## คำแนะนำดูแลข้าว

เพิ่มคู่มือออฟไลน์ 10 กลุ่ม คำแนะนำหลังวิเคราะห์ เปิดย้อนหลัง และสรุปท้ายใบพิมพ์แล้ว อ่านวิธีใช้ ขอบเขต และแหล่งอ้างอิงใน [CARE_GUIDE_TH.md](CARE_GUIDE_TH.md) ผลที่ยังไม่ชัดได้รับคำแนะนำตรวจเพิ่มก่อนเลือกวิธีจัดการเฉพาะโรค
