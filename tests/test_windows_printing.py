import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from leaflab.printing import PrintService, PrinterError, read_config, send_print
from leaflab.windows_printer import send_windows_raw
from printer_tools import configure_usb, configure_windows, demo_record


class WindowsConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.path=self.root/'printer.json'

    def test_windows_requires_an_explicit_queue_without_usb_ids(self):
        self.path.write_text(json.dumps(dict(enabled=True,transport='windows_raw',printer_name='XP-58')))
        config=read_config(self.path)
        self.assertEqual(config['printer_name'],'XP-58')
        for update in (dict(printer_name=None),dict(printer_name=''),dict(printer_name='  '),
                       dict(printer_name='XP\n58'),dict(transport='default')):
            self.path.write_text(json.dumps({**config,**update}))
            with self.subTest(update=update),self.assertRaises(PrinterError):read_config(self.path)

    def test_configure_selects_exact_observed_name_and_refuses_silent_overwrite(self):
        records=[dict(name='XP-58'),dict(name='EPSON')]
        with self.assertRaises(ValueError):configure_windows(records,self.path,'xp-58')
        self.assertFalse(self.path.exists())
        configure_windows(records,self.path,'XP-58')
        self.assertEqual(read_config(self.path)['transport'],'windows_raw')
        with self.assertRaises(ValueError):configure_windows(records,self.path,'EPSON')
        self.assertEqual(read_config(self.path)['printer_name'],'XP-58')
        configure_usb([dict(vendor_id='0x1234',product_id='0xabcd',interface=0,out_endpoint=1)],
                      self.path,replace=True,runtime_dir=self.root/'runtime')
        config=read_config(self.path)
        self.assertEqual(config['transport'],'usb');self.assertIsNone(config['printer_name'])

    def test_transport_dispatch_uses_only_selected_backend(self):
        with patch('leaflab.printing.send_usb') as usb,patch('leaflab.windows_printer.send_windows_raw') as windows:
            send_print([b'one'],dict(transport='usb'))
            usb.assert_called_once();windows.assert_not_called()
            send_print([b'two'],dict(transport='windows_raw',printer_name='XP-58'))
            windows.assert_called_once_with([b'two'],dict(transport='windows_raw',printer_name='XP-58'))

    @unittest.skipUnless(os.name=='nt','Windows file locking')
    def test_independent_services_cannot_hold_print_lock_together(self):
        first=PrintService(self.root/'data',self.path)
        second=PrintService(self.root/'data',self.path)
        with first.process_lock():
            with self.assertRaises(PrinterError) as error:
                with second.process_lock():self.fail('second service acquired print lock')
            self.assertEqual(error.exception.status,429)
        with second.process_lock():pass


class WindowsTransportTests(unittest.TestCase):
    def setUp(self):
        api=MagicMock();self.api=api
        constants=['PAUSED','ERROR','PAPER_JAM','PAPER_OUT','OFFLINE','NOT_AVAILABLE',
                   'DOOR_OPEN','USER_INTERVENTION','NO_TONER','OUT_OF_MEMORY']
        for index,name in enumerate(constants):setattr(api,'PRINTER_STATUS_'+name,1<<index)
        api.PRINTER_ATTRIBUTE_WORK_OFFLINE=1024
        api.GetPrinter.return_value=dict(Status=0,Attributes=0)
        api.OpenPrinter.return_value=73
        api.StartDocPrinter.return_value=904
        api.WritePrinter.side_effect=lambda handle,data:len(data)
        patcher=patch('leaflab.windows_printer._win32print',return_value=api)
        patcher.start();self.addCleanup(patcher.stop)
        self.config=dict(transport='windows_raw',printer_name='XP-58')

    def test_raw_document_preserves_all_bytes_with_partial_writes(self):
        self.api.WritePrinter.side_effect=[2,2,2]
        job=send_windows_raw([b'abcd',b'ef'],self.config)
        self.assertEqual(job,904)
        self.api.OpenPrinter.assert_called_once_with('XP-58')
        self.api.StartDocPrinter.assert_called_once_with(73,1,('AI PLANT DEVICE - receipt',None,'RAW'))
        self.assertEqual([call.args for call in self.api.WritePrinter.call_args_list],[(73,b'abcd'),(73,b'cd'),(73,b'ef')])
        self.api.EndDocPrinter.assert_called_once_with(73)
        self.api.AbortPrinter.assert_not_called();self.api.ClosePrinter.assert_called_once_with(73)
        self.api.GetDefaultPrinter.assert_not_called()

    def test_reported_paper_out_prevents_document_start(self):
        self.api.GetPrinter.return_value['Status']=self.api.PRINTER_STATUS_PAPER_OUT
        with self.assertRaises(PrinterError):send_windows_raw([b'x'],self.config)
        self.api.StartDocPrinter.assert_not_called();self.api.ClosePrinter.assert_called_once_with(73)

    def test_windows_work_offline_prevents_document_start(self):
        self.api.GetPrinter.return_value['Attributes']=self.api.PRINTER_ATTRIBUTE_WORK_OFFLINE
        with self.assertRaises(PrinterError):send_windows_raw([b'x'],self.config)
        self.api.StartDocPrinter.assert_not_called()

    def test_zero_write_aborts_document_and_closes_without_end_doc(self):
        self.api.WritePrinter.side_effect=lambda *args:0
        with self.assertRaises(PrinterError):send_windows_raw([b'abc'],self.config)
        self.api.EndDocPrinter.assert_not_called()
        self.api.AbortPrinter.assert_called_once_with(73);self.api.ClosePrinter.assert_called_once_with(73)

    def test_open_failure_does_not_close_an_unopened_handle(self):
        self.api.OpenPrinter.side_effect=OSError('unavailable')
        with self.assertRaises(PrinterError):send_windows_raw([b'abc'],self.config)
        self.api.ClosePrinter.assert_not_called();self.api.StartDocPrinter.assert_not_called()

    def test_failed_end_doc_is_unknown_and_not_automatically_retried(self):
        self.api.EndDocPrinter.side_effect=OSError('spooler disconnected')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'printer.json'
            config.write_text(json.dumps(dict(enabled=True,transport='windows_raw',printer_name='XP-58')))
            service=PrintService(root/'data',config);row=demo_record();request=str(uuid.uuid4())
            with self.assertLogs(level='ERROR'),self.assertRaises(PrinterError):service.print_scan(row,request)
            restarted=PrintService(root/'data',config)
            with self.assertRaises(PrinterError) as error:restarted.print_scan(row,request)
            self.assertEqual(error.exception.status,409)
            self.api.StartDocPrinter.assert_called_once()
            self.api.AbortPrinter.assert_called_once_with(73)
            with closing(sqlite3.connect(root/'data/print_jobs.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT status FROM print_jobs').fetchone()[0],'unknown')


if __name__=='__main__':unittest.main()
