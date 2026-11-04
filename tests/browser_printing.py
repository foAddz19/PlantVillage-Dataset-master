"""Real browser, real rice inference/receipt; USB sender is simulated, no paper printed."""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from leaflab.dataset import ROOT
from leaflab.paddy import PaddyService
from leaflab.printing import PrintService
from playwright.sync_api import expect,sync_playwright
from werkzeug.serving import make_server

service=PaddyService()
assert service.status()['ready'],service.message
with tempfile.TemporaryDirectory() as temporary:
    directory=Path(temporary)
    config=directory/'printer.json';config.write_text(json.dumps({'enabled':True,'vendor_id':'0x1234','product_id':'0xabcd'}))
    jobs=[]
    printer=PrintService(directory,config,sender=lambda commands,config:jobs.append(commands))
    app=create_app(service=service,model_backend='paddy',data_dir=directory,printer_service=printer)
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':1000})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/device',wait_until='networkidle')
            expect(page.locator('#printer-status')).to_contain_text('ตั้งค่าแล้ว')
            page.locator('[data-tab="capture"]').click()
            row=next(r for r in service.artifact['manifest'] if r['split']=='test')
            page.locator('#device-file').set_input_files(str(ROOT/'datasets'/'paddy-competition'/row['path']))
            page.locator('#analyze-scan').click()
            expect(page.locator('#result-state')).to_have_text('ตรวจแล้ว · ยังไม่บันทึก')
            expect(page.locator('#print-scan')).to_be_disabled()
            page.locator('#edit-observations').click()
            page.locator('#scan-note').fill('ทดสอบระบบพิมพ์ USB · ยังไม่ได้พิมพ์กระดาษจริง')
            page.locator('#close-observations').click()
            page.locator('#save-scan').click()
            expect(page.locator('#save-scan')).to_have_text('✓ บันทึกแล้ว')
            page.locator('#preview-receipt').click()
            expect(page.locator('#receipt-dialog')).to_be_visible()
            expect(page.locator('#receipt-image')).to_have_js_property('naturalWidth',384)
            output=ROOT/'test-results';output.mkdir(exist_ok=True)
            page.screenshot(path=str(output/'printer-preview-desktop.png'))
            page.locator('#close-receipt').click()
            page.locator('#print-scan').click()
            expect(page.locator('#device-message')).to_contain_text('ส่งใบผลตรวจไปเครื่องพิมพ์แล้ว')
            expect(page.locator('#print-scan')).to_have_text('พิมพ์ซ้ำ')
            assert len(jobs)==1
            page.locator('[data-tab="history"]').click()
            page.locator('.scan-actions').get_by_role('button',name='พิมพ์ซ้ำ',exact=True).click()
            expect(page.locator('.scan-actions button:last-child')).to_be_enabled()
            assert len(jobs)==2
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(output/'printer-history-mobile.png'),full_page=True)
            assert not errors,errors
            print('PASS: real rice analysis, save-before-print, Thai PNG preview, initial send, explicit history reprint, mobile layout; simulated USB transport only')
            browser.close()
    finally:
        server.shutdown();thread.join();service.close()
