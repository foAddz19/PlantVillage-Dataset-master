"""Configure a USB ESC/POS printer, preview Thai receipts, or explicitly test-print."""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from leaflab.dataset import ROOT
from leaflab.paddy_labels import rice_info
from leaflab.printing import PrintService, PrinterError, usb_id


def demo_record():
    now = datetime.now(timezone.utc).isoformat()
    return dict(id='TEST-'+uuid.uuid4().hex, captured_at=now, saved_at=now, source='sample',
        prediction=dict(candidates=[{**rice_info('blast'),'score':.55},{**rice_info('brown_spot'),'score':.30},
            {**rice_info('normal'),'score':.15}], uncertain=True, model_id='DEMO-NOT-REAL'),
        latitude=16.053, longitude=103.652, location_source='simulated', severity='unassessed',
        note='ใบตัวอย่างทดสอบเครื่องพิมพ์ ไม่ใช่ผลวิเคราะห์จริง · แปลงข้าว ทดสอบสระและวรรณยุกต์ น้ำ ใบ ข้าว เกษตร')


def discover_usb():
    import usb.core
    import usb.util
    from usb.backend.libusb1 import get_backend
    backend=get_backend()
    if backend is None: raise PrinterError('ไม่พบ libusb ให้ติดตั้ง sudo apt install libusb-1.0-0 usbutils')
    records=[]
    for device in usb.core.find(find_all=True, backend=backend):
        try:
            for config in device:
                for interface in config:
                    if interface.bInterfaceClass != 7 or interface.bAlternateSetting != 0: continue
                    for endpoint in interface:
                        if usb.util.endpoint_direction(endpoint.bEndpointAddress)==usb.util.ENDPOINT_OUT and usb.util.endpoint_type(endpoint.bmAttributes)==usb.util.ENDPOINT_TYPE_BULK:
                            records.append(dict(vendor_id=f'0x{device.idVendor:04x}',product_id=f'0x{device.idProduct:04x}',
                                interface=interface.bInterfaceNumber,out_endpoint=endpoint.bEndpointAddress,
                                bus=device.bus,address=device.address))
        finally: usb.util.dispose_resources(device)
    return records


def configure_usb(records, destination, vendor=None, product=None, replace=False, runtime_dir=None):
    if (vendor is None)!=(product is None): raise ValueError('ระบุ --vendor-id และ --product-id คู่กัน')
    if vendor is not None:
        records=[r for r in records if usb_id(r['vendor_id'])==usb_id(vendor) and usb_id(r['product_id'])==usb_id(product)]
    if len(records)!=1: raise ValueError('ต้องพบ USB printer interface เพียงหนึ่งรายการ ให้ตรวจสาย/ไฟ หรือเลือก --vendor-id และ --product-id จาก list-usb')
    selected=records[0]
    if destination.exists() and not replace: raise ValueError('มี config อยู่แล้ว ใช้ --replace เฉพาะเมื่อต้องการตั้งค่าใหม่')
    config=json.loads((ROOT/'config'/'printer.example.json').read_text(encoding='utf-8'))
    config.update({k:selected[k] for k in ('vendor_id','product_id','interface','out_endpoint')},enabled=True)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.tmp');temporary.write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8');os.replace(temporary,destination)
    vendor_hex=f'{usb_id(selected["vendor_id"]):04x}';product_hex=f'{usb_id(selected["product_id"]):04x}'
    rule=f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="{vendor_hex}", ATTR{{idProduct}}=="{product_hex}", MODE="0660", GROUP="lp"\n'
    rule_path=Path(runtime_dir or ROOT/'runtime')/'99-ai-plant-printer.rules';rule_path.parent.mkdir(parents=True,exist_ok=True);rule_path.write_text(rule,encoding='ascii')
    return rule_path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config'/'printer.json')
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('list-usb')
    configure=commands.add_parser('configure-usb')
    configure.add_argument('--vendor-id');configure.add_argument('--product-id');configure.add_argument('--replace',action='store_true')
    preview=commands.add_parser('preview-demo');preview.add_argument('--output',type=Path,default=ROOT/'runtime'/'receipt-demo.png')
    commands.add_parser('test-print')
    args=parser.parse_args()
    try:
        if args.action=='list-usb':
            print(json.dumps(discover_usb(),indent=2));return
        if args.action=='configure-usb':
            rule=configure_usb(discover_usb(),args.config,args.vendor_id,args.product_id,args.replace)
            print('Saved:',args.config);print('udev rule:',rule);print('Install the rule and add your user to lp as described in PRINTER_GUIDE_TH.md');return
        service=PrintService(ROOT/'data',args.config)
        row=demo_record()
        if args.action=='preview-demo':
            args.output.parent.mkdir(parents=True,exist_ok=True);service.preview(row).save(args.output);print(args.output);return
        print(json.dumps(service.print_scan(row,str(uuid.uuid4())),ensure_ascii=False,indent=2))
    except (PrinterError,ValueError,ImportError,OSError) as exc:
        parser.exit(2,str(exc)+'\n')


if __name__=='__main__':main()
