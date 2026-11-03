"""Real rice inference + browser/receipt QA. Temporary scans, simulated USB only."""
import io
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from leaflab.dataset import ROOT
from leaflab.images import read_image
from leaflab.paddy import PaddyService
from leaflab.printing import PrintService


def find_image(service, uncertain=False):
    rows=[row for row in service.artifact['manifest'] if row['split']=='test' and row['label']==('blast' if not uncertain else 'bacterial_leaf_blight')]
    for row in rows[:60]:
        path=ROOT/'datasets/paddy-competition'/row['path']
        result=service.predict(read_image(path))
        if result['uncertain']==uncertain and (uncertain or result['candidates'][0]['disease']=='blast'):
            return path
    raise AssertionError('No real image found for the requested case')


def main():
    service=PaddyService()
    assert service.status()['ready'],service.message
    try:
        selected=find_image(service)
        uncertain=find_image(service,uncertain=True)
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);config=directory/'printer.json'
            config.write_text(json.dumps(dict(enabled=True,vendor_id='0x1234',product_id='0xabcd',pause_ms=0)))
            jobs=[]
            printer=PrintService(directory,config,sender=lambda commands,config:jobs.append(commands))
            app=create_app(service=service,model_backend='paddy',data_dir=directory,printer_service=printer)
            server=make_server('127.0.0.1',0,app,threaded=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    page=browser.new_page(viewport=dict(width=1280,height=1000))
                    errors=[];external=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda request:external.append(request.url) if not request.url.startswith(('http://127.0.0.1:','blob:','data:')) else None)
                    page.route('https://**/*',lambda route:route.abort())
                    base=f'http://127.0.0.1:{server.server_port}'
                    page.goto(base+'/device#care',wait_until='networkidle')
                    expect(page.locator('.care-library-card')).to_have_count(10)
                    output=ROOT/'test-results';output.mkdir(exist_ok=True)
                    page.screenshot(path=str(output/'care-library-desktop.png'),full_page=True)
                    keys=['bacterial_leaf_blight','bacterial_leaf_streak','bacterial_panicle_blight','blast','brown_spot',
                          'dead_heart','downy_mildew','hispa','normal','tungro']
                    for key in keys:
                        page.locator('#care-search').fill(key)
                        expect(page.locator('.care-library-card')).to_have_count(1)
                        page.locator('.care-library-card button').click()
                        expect(page.locator('#care-dialog')).to_be_visible()
                        expect(page.locator('#care-dialog .care-status')).to_contain_text('คู่มืออ้างอิง')
                        assert page.locator('#care-dialog .care-sources a').count()>=1
                        page.locator('#care-dialog .care-dialog-heading button').click()
                    page.locator('#care-search').fill('โปร่งแสง')
                    page.locator('.care-library-card button').click()
                    page.screenshot(path=str(output/'care-guide-desktop.png'))
                    page.locator('#care-dialog .care-dialog-heading button').click()
                    page.locator('[data-tab="capture"]').click()
                    page.locator('#device-file').set_input_files(str(selected))
                    page.locator('#analyze-scan').click()
                    expect(page.locator('#result-care')).to_have_attribute('data-status','provisional')
                    expect(page.locator('#result-care h3')).to_have_text('โรคไหม้')
                    page.locator('#observer-severity').select_option('severe')
                    page.locator('#scan-note').fill('ทดสอบระบบคำแนะนำด้วยภาพชุดทดสอบ · ความรุนแรงตัวอย่าง · USB จำลอง ยังไม่ได้พิมพ์จริง')
                    page.locator('#save-scan').click()
                    expect(page.locator('#save-scan')).to_have_text('✓ บันทึกแล้ว')
                    expect(page.locator('#result-care .care-urgent')).to_be_visible()
                    page.screenshot(path=str(output/'care-result-desktop.png'),full_page=True)
                    page.locator('#preview-receipt').click()
                    expect(page.locator('#receipt-image')).to_have_js_property('naturalWidth',384)
                    response=page.request.get(base+page.locator('#receipt-image').get_attribute('src'))
                    image=Image.open(io.BytesIO(response.body()))
                    assert image.width==384 and image.mode=='1'
                    (output/'care-receipt.png').write_bytes(response.body())
                    page.locator('#close-receipt').click()
                    page.locator('#print-scan').click()
                    expect(page.locator('#device-message')).to_contain_text('ส่งใบผลตรวจ')
                    assert len(jobs)==1
                    page.locator('[data-tab="history"]').click()
                    page.locator('[data-care-scan]').click()
                    expect(page.locator('#care-dialog h2')).to_have_text('โรคไหม้')
                    expect(page.locator('#care-dialog .care-meta').filter(has_text='ฉบับที่เก็บพร้อมผลตรวจ')).to_have_count(1)
                    page.set_viewport_size(dict(width=800,height=480))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(output/'care-800x480.png'))
                    page.locator('#care-dialog .care-dialog-heading button').click()
                    page.set_viewport_size(dict(width=390,height=844))
                    page.locator('[data-tab="care"]').click()
                    page.locator('#care-search').fill('')
                    expect(page.locator('.care-library-card')).to_have_count(10)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    expect(page.locator('#device-message')).to_be_hidden(timeout=7000)
                    page.screenshot(path=str(output/'care-library-mobile.png'),full_page=True)
                    page.locator('[data-tab="capture"]').click()
                    page.locator('#device-file').set_input_files(str(uncertain))
                    page.locator('#analyze-scan').click()
                    expect(page.locator('#result-care')).to_have_attribute('data-status','uncertain')
                    page.locator('#result-care button').click()
                    expect(page.locator('#care-dialog h2')).to_have_text('คำแนะนำตรวจเพิ่มเติม')
                    expect(page.locator('#care-dialog .care-sources a')).to_have_count(0)
                    page.screenshot(path=str(output/'care-uncertain-mobile.png'))
                    page.locator('#care-dialog .care-dialog-heading button').click()
                    page.goto(base+'/',wait_until='networkidle')
                    file_input=page.locator('input[type="file"]').first
                    file_input.set_input_files(str(selected))
                    page.locator('#analyze-button').click()
                    expect(page.locator('#lab-care')).to_have_attribute('data-status','provisional')
                    assert not errors,errors
                    assert not external,external
                    browser.close()
                    print('PASS: all 10 offline guides; real rice analysis; uncertain gate; saved snapshot/history; severe warning; Thai 384-dot preview; simulated USB; 1280/800/390 layouts; lab integration; no external requests')
            finally:
                server.shutdown();thread.join()
    finally:
        service.close()


if __name__=='__main__':main()
