"""Build a portable Pi inference/USB-print package without training data or private scans."""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def build(output):
    config=json.loads((ROOT/'models/paddy/model.json').read_text(encoding='utf-8'))
    model=(ROOT/'models/paddy'/config['model_file']).resolve()
    if not model.is_relative_to(ROOT/'models'/'paddy') or hashlib.sha256(model.read_bytes()).hexdigest()!=config['sha256']:
        raise ValueError('Paddy model path/hash mismatch')
    required=['app.py','start_pi.sh','requirements-pi.txt','requirements-printer.txt','printer_tools.py',
              'benchmark_paddy.py','CARE_GUIDE_TH.md','PADDY_GUIDE_TH.md','PADDY_MODEL_REPORT_TH.md','PRINTER_GUIDE_TH.md',
              'config/printer.example.json','models/paddy/model.json',model.relative_to(ROOT).as_posix(),
              'leaflab/care.py','leaflab/care_knowledge.json','static/care.js','static/care.css']
    files={ROOT/name for name in required}
    for folder in ('leaflab','templates','static','assets/fonts'):
        files.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    for name in ('paddy-prediction.png','paddy-report.png','paddy-mobile.png','paddy-benchmark-windows.json','receipt-demo.png',
                 'care-guide-desktop.png','care-receipt.png'):
        optional=ROOT/'test-results'/name
        if optional.is_file():files.add(optional)
    if not all(p.is_file() for p in files):raise ValueError('Missing deployment files')
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(files):archive.write(path,'ai-plant/'+path.relative_to(ROOT).as_posix())
        archive.writestr('ai-plant/START_HERE_TH.txt', '''คัดลอกโฟลเดอร์ ai-plant ไปไว้ใน Home บน Raspberry Pi OS 64-bit
เปิด Terminal:
  cd ~/ai-plant
  sudo apt update
  sudo apt install -y python3-venv python3-pip libusb-1.0-0 libfreetype6 usbutils
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements-pi.txt
  sh start_pi.sh
เปิด Chromium บน Pi ที่ http://127.0.0.1:8765/device
ลบข้อมูลทีละรายการ: ประวัติ > ลบรายการนี้ > ตรวจภาพ/วันเวลา > ยืนยันลบรายการนี้ (ไม่มีปุ่มลบทั้งหมด)
คู่มือดูแลข้าว: เปิดแท็บคู่มือดูแล อ่าน CARE_GUIDE_TH.md สำหรับขอบเขตและแหล่งอ้างอิง
การติดตั้งเครื่องพิมพ์: อ่าน PRINTER_GUIDE_TH.md ก่อนสั่ง test-print
USB ID ต้องอ่านจากเครื่องจริงด้วย printer_tools.py configure-usb แล้วติดตั้งกฎ udev ตามคู่มือ
ใช้ตรวจโรค บันทึก แล้วพิมพ์ผล; ยังไม่ได้ทดสอบฮาร์ดแวร์ Pi/เครื่องพิมพ์จริง
''')
    os.replace(temporary,output)
    return len(files)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist'/'ai-plant-pi.zip')
    args=parser.parse_args();count=build(args.output)
    print(args.output.resolve());print('Files:',count+1,'Bytes:',args.output.stat().st_size)
