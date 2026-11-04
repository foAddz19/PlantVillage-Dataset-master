"""Windows RAW spooler output; keep the receipt's native ESC/POS bytes intact."""
from __future__ import annotations

import logging
import os
import time

from .printing import PrinterError


def _win32print():
    if os.name != 'nt':
        raise PrinterError('windows_raw ใช้ได้บน Windows เท่านั้น บน Pi ให้ตั้งค่า configure-usb')
    try:
        import win32print
    except ImportError as exc:
        raise PrinterError('กรุณาติดตั้ง requirements-printer.txt ใน virtual environment ของแอป') from exc
    return win32print


def list_windows_printers():
    api = _win32print()
    try:
        return [dict(name=row['pPrinterName'], driver=row['pDriverName'], port=row['pPortName'],
                     status=row['Status'], jobs=row['cJobs'])
                for row in api.EnumPrinters(api.PRINTER_ENUM_LOCAL | api.PRINTER_ENUM_CONNECTIONS, None, 2)]
    except Exception as exc:
        raise PrinterError('อ่านรายชื่อเครื่องพิมพ์ Windows ไม่สำเร็จ กรุณาตรวจบริการ Print Spooler') from exc


def send_windows_raw(commands, config):
    """Send one RAW document to an explicitly configured queue, never the default."""
    api = _win32print()
    handle = None
    document_started = False
    try:
        handle = api.OpenPrinter(config['printer_name'])
        info = api.GetPrinter(handle, 2)
        # The driver can report zero even when the hardware cannot be verified.
        blocked = (api.PRINTER_STATUS_PAUSED | api.PRINTER_STATUS_ERROR |
                   api.PRINTER_STATUS_PAPER_JAM | api.PRINTER_STATUS_PAPER_OUT |
                   api.PRINTER_STATUS_OFFLINE | api.PRINTER_STATUS_NOT_AVAILABLE |
                   api.PRINTER_STATUS_DOOR_OPEN | api.PRINTER_STATUS_USER_INTERVENTION |
                   api.PRINTER_STATUS_NO_TONER | api.PRINTER_STATUS_OUT_OF_MEMORY)
        if info['Status'] & blocked or info['Attributes'] & api.PRINTER_ATTRIBUTE_WORK_OFFLINE:
            raise PrinterError('คิว Windows แจ้งว่าเครื่องพิมพ์หยุดทำงาน ออฟไลน์ หรือมีข้อผิดพลาด กรุณาตรวจไฟ กระดาษ และสาย USB')
        job_id = api.StartDocPrinter(handle, 1, ('AI PLANT DEVICE - receipt', None, 'RAW'))
        document_started = True
        api.StartPagePrinter(handle)
        # Windows calls are synchronous; this deadline is checked between calls.
        deadline = time.monotonic() + 60
        for command in commands:
            position = 0
            while position < len(command):
                if time.monotonic() > deadline:
                    raise PrinterError('Windows รับข้อมูลงานพิมพ์นานเกินกำหนด', 504)
                remaining = command[position:]
                written = api.WritePrinter(handle, remaining)
                if not isinstance(written, int) or not 0 < written <= len(remaining):
                    raise PrinterError('Windows รับข้อมูลงานพิมพ์ไม่ครบ')
                position += written
        api.EndPagePrinter(handle)
        api.EndDocPrinter(handle)
        document_started = False
        return job_id
    except PrinterError:
        raise
    except Exception as exc:
        raise PrinterError('ส่งงานไปคิว Windows ไม่สำเร็จ กรุณาตรวจชื่อเครื่องพิมพ์และ Print Spooler') from exc
    finally:
        if handle is not None:
            if document_started:
                try:
                    api.AbortPrinter(handle)
                except Exception:
                    logging.warning('Cannot abort Windows printer document', exc_info=True)
            try:
                api.ClosePrinter(handle)
            except Exception:
                logging.warning('Cannot close Windows printer handle', exc_info=True)
