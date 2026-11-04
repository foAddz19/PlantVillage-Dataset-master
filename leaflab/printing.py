"""Explicit USB/Windows printing and persistent request deduplication."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .dataset import ROOT


class PrinterError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def usb_id(value):
    if type(value) is int: result = value
    elif isinstance(value, str): result = int(value, 16)
    else: raise ValueError('ต้องระบุ USB vendor_id และ product_id จาก lsusb')
    if not 1 <= result <= 65535: raise ValueError('USB ID ไม่ถูกต้อง')
    return result


def read_config(path):
    config = dict(enabled=False, width_dots=384, timeout_ms=5000, fragment_height=64, pause_ms=100, feed_lines=4,
                  serial_number=None, interface=None, out_endpoint=None, transport='usb', printer_name=None)
    if not Path(path).exists(): return config
    try:
        supplied = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(supplied, dict): raise ValueError('config ต้องเป็น JSON object')
        config.update(supplied)
        if type(config['enabled']) is not bool: raise ValueError('enabled ต้องเป็น true/false')
        for name, low, high in (('width_dots',192,576),('timeout_ms',1000,15000),('fragment_height',16,128),('pause_ms',0,500),('feed_lines',2,8)):
            if type(config[name]) is not int or not low <= config[name] <= high: raise ValueError('ค่าตั้งเครื่องพิมพ์ไม่ถูกต้อง: '+name)
        if config['width_dots'] % 8: raise ValueError('width_dots ต้องหาร 8 ลงตัว')
        for name, maximum in (('interface',255),('out_endpoint',127)):
            if config[name] is not None and (type(config[name]) is not int or not 0 <= config[name] <= maximum):
                raise ValueError('ค่าตั้ง USB ไม่ถูกต้อง: '+name)
        if config['serial_number'] is not None and not isinstance(config['serial_number'],str): raise ValueError('serial_number ต้องเป็นข้อความ')
        if config['transport'] not in ('usb', 'windows_raw'):
            raise ValueError('transport ต้องเป็น usb หรือ windows_raw')
        if config['enabled']:
            if config['transport'] == 'usb':
                config['vendor_id'] = usb_id(config.get('vendor_id'))
                config['product_id'] = usb_id(config.get('product_id'))
            else:
                name = config['printer_name']
                if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
                    raise ValueError('ต้องระบุ printer_name ของคิว Windows ที่ติดตั้งไว้')
        return config
    except (ValueError, OSError, TypeError) as exc:
        raise PrinterError('ตั้งค่าเครื่องพิมพ์ไม่ถูกต้อง: '+str(exc)) from exc


def send_usb(commands, config):
    """Claim the configured USB printer only for this job; discover descriptors."""
    import usb.core
    import usb.util
    from usb.backend.libusb1 import get_backend

    backend = get_backend()
    if backend is None: raise PrinterError('ไม่พบ libusb บน Pi ให้ติดตั้ง libusb-1.0-0')
    matches = list(usb.core.find(find_all=True, idVendor=config['vendor_id'], idProduct=config['product_id'], backend=backend))
    if config['serial_number']:
        matches = [dev for dev in matches if dev.serial_number == config['serial_number']]
    if not matches: raise PrinterError('ไม่พบเครื่องพิมพ์ USB ที่ตั้งค่าไว้ กรุณาตรวจไฟ สาย และ USB ID')
    if len(matches) != 1: raise PrinterError('พบเครื่องพิมพ์ USB ID เดียวกันหลายเครื่อง ให้ระบุ serial_number')
    dev = matches[0]
    interface = None
    detached = claimed = False
    try:
        try: active = dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration(); active = dev.get_active_configuration()
        outputs = []
        for item in active:
            if config['interface'] is not None and item.bInterfaceNumber != config['interface']: continue
            if config['interface'] is None and item.bInterfaceClass != 7: continue
            if item.bAlternateSetting != 0: continue
            for endpoint in item:
                if usb.util.endpoint_direction(endpoint.bEndpointAddress) != usb.util.ENDPOINT_OUT: continue
                if usb.util.endpoint_type(endpoint.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK: continue
                if config['out_endpoint'] is not None and endpoint.bEndpointAddress != config['out_endpoint']: continue
                outputs.append((item.bInterfaceNumber, endpoint.bEndpointAddress))
        if len(outputs) != 1:
            raise PrinterError('หา USB printer interface/Bulk OUT ไม่ได้ ให้ตรวจ lsusb -v และกำหนด interface/out_endpoint')
        interface, endpoint = outputs[0]
        try:
            if dev.is_kernel_driver_active(interface):
                dev.detach_kernel_driver(interface); detached = True
        except NotImplementedError: pass
        usb.util.claim_interface(dev, interface); claimed = True
        deadline = time.monotonic() + 60
        for command in commands:
            position = 0
            while position < len(command):
                if time.monotonic() > deadline: raise PrinterError('งานพิมพ์ใช้เวลานานเกินกำหนด', 504)
                written = dev.write(endpoint, command[position:], timeout=config['timeout_ms'])
                if not written: raise PrinterError('USB ไม่รับข้อมูลพิมพ์')
                position += written
            if command.startswith(b'\x1dv0'):
                time.sleep(config['pause_ms'] / 1000)
    except usb.core.USBError as exc:
        if getattr(exc, 'errno', None) in (13, 1):
            raise PrinterError('ไม่มีสิทธิ์ใช้ USB printer ให้ตั้ง udev และกลุ่ม lp ตามคู่มือ') from exc
        raise PrinterError('ส่งข้อมูลผ่าน USB ไม่สำเร็จ กรุณาตรวจสาย กระดาษ และไฟเครื่องพิมพ์') from exc
    finally:
        if claimed:
            try: usb.util.release_interface(dev, interface)
            except usb.core.USBError: logging.warning('Cannot release printer interface', exc_info=True)
        if detached:
            try: dev.attach_kernel_driver(interface)
            except (usb.core.USBError, NotImplementedError): logging.warning('Cannot reattach printer kernel driver', exc_info=True)
        usb.util.dispose_resources(dev)


def send_print(commands, config):
    if config['transport'] == 'windows_raw':
        from .windows_printer import send_windows_raw
        return send_windows_raw(commands, config)
    return send_usb(commands, config)


class PrintService:
    def __init__(self, directory, config_path=None, sender=None):
        self.directory = Path(directory)
        self.config_path = Path(config_path or os.environ.get('LEAFLAB_PRINTER_CONFIG') or ROOT/'config'/'printer.json')
        self.sender = sender or send_print
        self.lock = threading.Lock()

    def status(self):
        try:
            config = read_config(self.config_path)
            configured = config['enabled']
            return dict(configured=configured, busy=self.lock.locked(), connection_verified=False,
                        message=('ตั้งค่าแล้ว · ตรวจคิว Windows เมื่อกดพิมพ์' if config['transport'] == 'windows_raw' else 'ตั้งค่าแล้ว · ตรวจ USB เมื่อกดพิมพ์') if configured else 'ยังไม่ได้ตั้งค่าเครื่องพิมพ์')
        except PrinterError as exc:
            return dict(configured=False, busy=self.lock.locked(), connection_verified=False, message=str(exc))

    def preview(self, row):
        from .receipt import render_receipt
        try: return render_receipt(row, read_config(self.config_path)['width_dots'])
        except (ImportError, OSError) as exc:
            raise PrinterError('สร้างใบผลตรวจไม่ได้ ให้ติดตั้ง requirements-printer.txt และคัดลอก assets/fonts') from exc

    def database(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory/'print_jobs.sqlite3', timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS print_jobs (request_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, status TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        db.commit()
        return closing(db)

    @contextmanager
    def process_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory/'printer.lock').open('a+b') as handle:
            if os.name == 'posix':
                import fcntl
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc: raise PrinterError('เครื่องพิมพ์กำลังทำงาน กรุณารอสักครู่',429) from exc
            elif os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'\0'); handle.flush()
                handle.seek(0)
                try: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc: raise PrinterError('เครื่องพิมพ์กำลังทำงาน กรุณารอสักครู่',429) from exc
            try: yield
            finally:
                if os.name == 'posix': fcntl.flock(handle, fcntl.LOCK_UN)
                elif os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def print_scan(self, row, request_id):
        from .receipt import escpos_commands
        if not row.get('saved_at'): raise PrinterError('กรุณาบันทึกจุดตรวจก่อนพิมพ์ผล',409)
        try: request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc: raise ValueError('request_id ของงานพิมพ์ต้องเป็น UUID') from exc
        if not self.lock.acquire(blocking=False): raise PrinterError('เครื่องพิมพ์กำลังทำงาน กรุณารอสักครู่',429)
        try:
            with self.process_lock(), self.database() as db:
                old = db.execute('SELECT * FROM print_jobs WHERE request_id=?',(request_id,)).fetchone()
                if old:
                    if old['scan_id'] != row['id']: raise PrinterError('request_id นี้ใช้กับผลตรวจอื่นแล้ว',409)
                    if old['status'] != 'sent': raise PrinterError('งานนี้เคยเริ่มส่งแล้ว แต่ยืนยันไม่ได้ว่าพิมพ์ครบ กรุณาตรวจเครื่องก่อนสั่งพิมพ์ซ้ำเป็นงานใหม่',409)
                    return dict(request_id=request_id, scan_id=row['id'], status='sent', duplicate=True,
                                message='งานนี้ส่งไปเครื่องพิมพ์แล้ว ไม่ส่งซ้ำ')
                config = read_config(self.config_path)
                if not config['enabled']: raise PrinterError('ยังไม่ได้ตั้งค่าเครื่องพิมพ์ ดู PRINTER_GUIDE_TH.md')
                commands = escpos_commands(self.preview(row), config['fragment_height'], config['feed_lines'])
                digest = hashlib.sha256(b''.join(commands)).hexdigest()
                db.execute('INSERT INTO print_jobs (request_id,scan_id,status,receipt_sha256) VALUES (?,?,?,?)',
                           (request_id,row['id'],'pending',digest)); db.commit()
                try:
                    self.sender(commands,config)
                except Exception as exc:
                    db.execute('UPDATE print_jobs SET status=? WHERE request_id=?',('unknown',request_id)); db.commit()
                    logging.exception('Printer job %s failed',request_id)
                    message = str(exc) if isinstance(exc,PrinterError) else 'เชื่อมต่อเครื่องพิมพ์ไม่ได้'
                    raise PrinterError(message+' อาจพิมพ์ไปบางส่วนแล้ว กรุณาตรวจเครื่องก่อนกดพิมพ์ซ้ำ',
                                       exc.status if isinstance(exc,PrinterError) else 503) from exc
                db.execute('UPDATE print_jobs SET status=? WHERE request_id=?',('sent',request_id)); db.commit()
                return dict(request_id=request_id, scan_id=row['id'], status='sent', duplicate=False,
                            message='ส่งใบผลตรวจไปเครื่องพิมพ์แล้ว กรุณาตรวจใบพิมพ์ที่เครื่อง')
        finally: self.lock.release()
