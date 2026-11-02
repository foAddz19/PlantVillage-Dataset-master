import io
import json
import re
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import create_app
from leaflab.dataset import Catalog
from leaflab.printing import PrintService, PrinterError, read_config, send_usb
from leaflab.receipt import ThaiFont, escpos_commands, render_receipt, receipt_layout, wrap_text
from printer_tools import configure_usb, demo_record
from tests.test_device import StubService


class ReceiptTests(unittest.TestCase):
    def test_thai_english_glyphs_and_grapheme_wrapping(self):
        font=ThaiFont(22)
        text='ข้าวน้ำเกษตรใบไม้'*10
        lines=list(wrap_text(text,font,360))
        self.assertEqual(''.join(lines),text)
        self.assertTrue(all(font.width(line)<=360 for line in lines))
        self.assertTrue(all(glyph[0]!=0 for glyph in font.glyphs('AI ข้าว น้ำ ใบไม้ 100%')))

    def test_native_glyphs_are_binary_and_bold_weight_is_applied(self):
        from PIL import Image
        counts=[]
        for weight in (400,700):
            font=ThaiFont(22,weight=weight)
            image=Image.new('L',(340,font.height+8),255)
            font.draw(image,'ข้าว น้ำ ใบไม้ AI 58mm',8,0)
            pixels=image.tobytes()
            self.assertLessEqual(set(pixels),{0,255})
            counts.append(pixels.count(0))
        self.assertGreater(counts[1],counts[0])

    def test_receipt_columns_fit_long_disease_names_and_notes(self):
        row=demo_record()
        row['prediction']['uncertain']=False
        row['prediction']['candidates'][0]['disease_th']='ความเสียหายจากหนอนกอ (Yellow stem borer)'
        row['note']='แปลงข้าว ทดสอบสระและวรรณยุกต์ น้ำ ใบไม้ '*12
        for width in (192,320,384,576):
            with self.subTest(width=width):
                fonts,layout,height=receipt_layout(row,width)
                self.assertLessEqual(height,4112)
                for item in layout:
                    if item['kind']!='text':continue
                    advance=fonts[item['style']].width(item['text'])
                    self.assertLessEqual(advance,item['max_width']+.01,item)
                    self.assertGreaterEqual(item['x'],0,item)
                    self.assertLessEqual(item['x']+advance,width,item)

    def test_receipt_uses_saved_thai_time_and_keeps_uncertain_result_explicit(self):
        row=demo_record();row['captured_at']='2026-11-01T20:30:00+00:00'
        _,layout,_=receipt_layout(row)
        texts=[item['text'] for item in layout if item['kind']=='text']
        self.assertIn('02/11/2026',texts)
        self.assertIn('03:30:00 น.',texts)
        self.assertEqual([i['text'] for i in layout if i.get('style')=='result'],['ยังระบุไม่ได้'])

    def test_raster_commands_reconstruct_the_actual_monochrome_receipt(self):
        image=render_receipt(demo_record())
        self.assertEqual(image.width,384)
        self.assertEqual(image.mode,'1')
        commands=escpos_commands(image)
        self.assertEqual(commands[:2],[b'\x1b@',b'\x1ba\x00'])
        self.assertEqual(commands[-1],b'\n'*4)
        position=0
        for command in commands[2:-1]:
            self.assertEqual(command[:4],b'\x1dv0\x00')
            width_bytes=int.from_bytes(command[4:6],'little')
            height=int.from_bytes(command[6:8],'little')
            self.assertEqual(width_bytes,48)
            self.assertLessEqual(height,64)
            original=image.crop((0,position,384,position+height)).tobytes()
            self.assertEqual(command[8:],bytes(value^255 for value in original))
            self.assertEqual(len(command[8:]),width_bytes*height)
            position+=height
        self.assertEqual(position,image.height)


class PrintingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.config=self.root/'printer.json'
        self.config.write_text(json.dumps({'enabled':True,'vendor_id':'0x1234','product_id':'0xabcd','pause_ms':0}))
        self.calls=[]
        self.service=PrintService(self.root/'data',self.config,sender=lambda commands,config:self.calls.append(commands))
        self.row=demo_record()

    def test_configuration_rejects_missing_ids_and_invalid_width(self):
        for data in ({'enabled':True},{'enabled':True,'vendor_id':True,'product_id':1},{'width_dots':385}, {'timeout_ms':0}):
            self.config.write_text(json.dumps(data))
            with self.subTest(data=data),self.assertRaises(PrinterError): read_config(self.config)

    def test_disabled_printer_does_not_send_and_preview_still_works(self):
        self.config.write_text('{"enabled":false}')
        self.assertFalse(self.service.status()['configured'])
        self.assertEqual(self.service.preview(self.row).width,384)
        with self.assertRaises(PrinterError):self.service.print_scan(self.row,str(uuid.uuid4()))
        self.assertFalse(self.calls)

    def test_retry_after_restart_does_not_print_a_second_copy(self):
        job=str(uuid.uuid4())
        self.assertFalse(self.service.print_scan(self.row,job)['duplicate'])
        restarted=PrintService(self.root/'data',self.config,sender=lambda *args:self.fail('Duplicate physical job'))
        self.assertTrue(restarted.print_scan(self.row,job)['duplicate'])
        self.assertEqual(len(self.calls),1)
        other={**self.row,'id':'another-scan'}
        with self.assertRaises(PrinterError):restarted.print_scan(other,job)

    def test_usb_failure_is_persisted_as_unknown_and_never_automatically_retried(self):
        attempts=[]
        def fail(commands,config):attempts.append(1);raise PrinterError('USB timeout')
        self.service.sender=fail;job=str(uuid.uuid4())
        with self.assertLogs(level='ERROR'),self.assertRaises(PrinterError):self.service.print_scan(self.row,job)
        with self.assertRaises(PrinterError) as error:self.service.print_scan(self.row,job)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(len(attempts),1)
        with closing(sqlite3.connect(self.root/'data'/'print_jobs.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT status FROM print_jobs').fetchone()[0],'unknown')

    def test_job_is_recorded_before_any_physical_write(self):
        def send(commands,config):
            with closing(sqlite3.connect(self.root/'data'/'print_jobs.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT status FROM print_jobs').fetchone()[0],'pending')
        self.service.sender=send
        self.service.print_scan(self.row,str(uuid.uuid4()))

    def test_concurrent_print_is_rejected(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def send(*args):entered.set();release.wait(5)
        self.service.sender=send
        def run():
            try:self.service.print_scan(self.row,str(uuid.uuid4()))
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(PrinterError) as error:self.service.print_scan(self.row,str(uuid.uuid4()))
            self.assertEqual(error.exception.status,429)
        finally:release.set();thread.join(5)
        self.assertFalse(errors)

    def test_api_requires_saved_result_token_and_cannot_replace_prediction(self):
        app=create_app(Catalog(self.root),StubService(),data_dir=self.root/'scans',printer_service=self.service)
        client=app.test_client();token=re.search(r'name="app-token" content="([^"]+)"',client.get('/device').text).group(1)
        headers={'X-App-Token':token}
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(64,64),(30,100,40)).save(image,'PNG')
        row=client.post('/api/device/analyze',headers=headers,data={'image':(io.BytesIO(image.getvalue()),'leaf.png'),
            'latitude':'16.053','longitude':'103.652','location_source':'simulated'}).json
        path=f'/api/device/scans/{row["id"]}/print'
        self.assertEqual(client.post(path,json={'request_id':str(uuid.uuid4())}).status_code,403)
        self.assertEqual(client.post(path,headers=headers,json={'request_id':str(uuid.uuid4())}).status_code,409)
        self.assertEqual(client.post(path,headers=headers,json={'request_id':'invalid'}).status_code,409)
        client.post(f'/api/device/scans/{row["id"]}/save',headers=headers,json={'note':'ต้นข้าวทดสอบ','severity':'mild'})
        self.assertEqual(client.post(path,headers=headers,json={'request_id':'invalid'}).status_code,400)
        self.assertEqual(client.post(path,headers=headers,json={'request_id':str(uuid.uuid4()),'prediction':{}}).status_code,400)
        receipt=client.get(f'/api/device/scans/{row["id"]}/receipt.png')
        self.assertEqual(receipt.status_code,200);self.assertEqual(receipt.mimetype,'image/png')
        job=str(uuid.uuid4())
        self.assertEqual(client.post(path,headers=headers,json={'request_id':job}).json['status'],'sent')
        self.assertTrue(client.post(path,headers=headers,json={'request_id':job}).json['duplicate'])
        self.assertEqual(len(self.calls),1)

    def test_configuration_is_derived_from_observed_usb_descriptors(self):
        self.config.unlink()
        observed=[dict(vendor_id='0x4321',product_id='0xdcba',interface=2,out_endpoint=3)]
        rule=configure_usb(observed,self.config,runtime_dir=self.root/'runtime')
        config=read_config(self.config)
        self.assertEqual((config['vendor_id'],config['product_id'],config['interface'],config['out_endpoint']),(0x4321,0xdcba,2,3))
        self.assertIn('ATTR{idVendor}=="4321"',rule.read_text())
        with self.assertRaises(ValueError):configure_usb(observed,self.config,runtime_dir=self.root/'runtime')


class UsbTransportTests(unittest.TestCase):
    def setup_usb(self):
        class Interface(list):
            bInterfaceNumber=2;bInterfaceClass=7;bAlternateSetting=0
        interface=Interface([SimpleNamespace(bEndpointAddress=3,bmAttributes=2)])
        device=MagicMock();device.get_active_configuration.return_value=[interface]
        device.is_kernel_driver_active.return_value=True
        patches=[patch('usb.backend.libusb1.get_backend',return_value=object()),patch('usb.core.find',return_value=iter([device])),
                 patch('usb.util.claim_interface'),patch('usb.util.release_interface'),patch('usb.util.dispose_resources')]
        for item in patches:item.start();self.addCleanup(item.stop)
        return device

    def test_real_descriptor_endpoint_partial_write_and_driver_restoration(self):
        device=self.setup_usb();device.write.side_effect=[2,2,2]
        send_usb([b'abcd',b'ef'],dict(vendor_id=0x1234,product_id=0xabcd,serial_number=None,interface=None,out_endpoint=None,timeout_ms=5000,pause_ms=0))
        self.assertEqual([call.args for call in device.write.call_args_list],[(3,b'abcd'),(3,b'cd'),(3,b'ef')])
        device.detach_kernel_driver.assert_called_once_with(2);device.attach_kernel_driver.assert_called_once_with(2)

    def test_usb_permission_failure_restores_driver(self):
        import usb.core
        device=self.setup_usb();device.write.side_effect=usb.core.USBError('access denied',errno=13)
        with self.assertRaises(PrinterError) as error:
            send_usb([b'abcd'],dict(vendor_id=1,product_id=2,serial_number=None,interface=None,out_endpoint=None,timeout_ms=5000,pause_ms=0))
        self.assertIn('udev',str(error.exception));device.attach_kernel_driver.assert_called_once_with(2)


if __name__=='__main__':unittest.main()
