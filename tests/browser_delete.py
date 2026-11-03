"""Single-item deletion UX: real rice AI, temporary database, intercepted map tiles."""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect,sync_playwright
from werkzeug.serving import make_server
from app import create_app
from leaflab.dataset import ROOT
from leaflab.paddy import PaddyService
from leaflab.scans import ScanStore
from tests.browser_care import find_image


def main():
    service=PaddyService()
    assert service.status()['ready'],service.message
    try:
        image=find_image(service)
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp)
            app=create_app(service=service,model_backend='paddy',data_dir=data)
            server=make_server('127.0.0.1',0,app,threaded=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    page=browser.new_page(viewport=dict(width=1280,height=960))
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('https://tile.openstreetmap.org/**',lambda route:route.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#e4ecde"/></svg>'))
                    base=f'http://127.0.0.1:{server.server_port}'
                    page.goto(base+'/device',wait_until='networkidle')
                    for index in range(2):
                        page.locator('#device-file').set_input_files(str(image))
                        page.locator('#analyze-scan').click()
                        expect(page.locator('#result-state')).to_have_text('ตรวจแล้ว · ยังไม่บันทึก')
                        page.locator('#scan-note').fill(f'ข้อมูลทดสอบการลบเฉพาะรายการ {index+1} · ฐานข้อมูลชั่วคราว')
                        page.locator('#save-scan').click()
                        expect(page.locator('#save-scan')).to_have_text('✓ บันทึกแล้ว')
                    records=page.request.get(base+'/api/device/scans').json()['items']
                    target,keep=records
                    before_image=page.request.get(base+keep['image_url']).body()
                    page.locator('[data-tab="map"]').click()
                    expect(page.locator('#map-list .map-record')).to_have_count(2)
                    page.locator('[data-tab="history"]').click()
                    remove=page.locator(f'[data-delete-scan="{target["id"]}"]')
                    dialog=page.locator('#delete-dialog')
                    # Opening, Escape, focused Enter and explicit Cancel never issue a delete.
                    attempts=[];page.on('request',lambda request:attempts.append(request.url) if request.url.endswith('/delete') else None)
                    remove.click();expect(dialog).to_be_visible();expect(page.locator('#cancel-delete')).to_be_focused()
                    page.keyboard.press('Escape');expect(dialog).to_be_hidden()
                    remove.click();page.keyboard.press('Enter');expect(dialog).to_be_hidden()
                    remove.click();page.locator('#cancel-delete').click();expect(dialog).to_be_hidden()
                    assert not attempts,attempts
                    assert page.request.get(base+'/api/device/scans').json()['total']==2
                    # A failed request leaves a useful error and all data intact.
                    path=f'**/api/device/scans/{target["id"]}/delete'
                    page.route(path,lambda route:route.abort())
                    remove.click();page.locator('#confirm-delete').click()
                    expect(page.locator('#delete-error')).to_be_visible()
                    expect(page.locator('#cancel-delete')).to_be_enabled()
                    assert page.request.get(base+'/api/device/scans').json()['total']==2
                    page.locator('#cancel-delete').click();page.unroute(path)
                    remove.click()
                    expect(page.locator('#delete-title')).to_contain_text(target['prediction']['candidates'][0]['disease_th'])
                    expect(page.locator('#delete-id')).to_contain_text(target['id'][:12])
                    expect(page.locator('#delete-image')).to_have_js_property('naturalWidth', ImageWidth(image))
                    output=ROOT/'test-results';output.mkdir(exist_ok=True)
                    page.screenshot(path=str(output/'delete-confirm-desktop.png'))
                    page.set_viewport_size(dict(width=390,height=844))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert dialog.bounding_box()['height']<=844
                    page.screenshot(path=str(output/'delete-confirm-mobile.png'))
                    page.set_viewport_size(dict(width=800,height=480))
                    assert dialog.bounding_box()['height']<=480
                    page.locator('#cancel-delete').click()
                    # Hold the request: repeated actions/Escape cannot bypass the in-flight guard.
                    held=[];page.route(path,lambda route:held.append(route))
                    remove.click();page.locator('#confirm-delete').click()
                    expect(page.locator('#confirm-delete')).to_be_disabled()
                    expect(page.locator('#cancel-delete')).to_be_disabled()
                    page.keyboard.press('Escape');expect(dialog).to_be_visible()
                    assert len(held)==1
                    held[0].continue_();expect(dialog).to_be_hidden();page.unroute(path)
                    expect(page.locator('.scan-card')).to_have_count(1)
                    expect(page.locator('#saved-count')).to_have_text('1 จุดบันทึก')
                    assert page.request.get(base+'/api/device/scans').json()['items']==[keep]
                    assert page.request.get(base+keep['image_url']).body()==before_image
                    assert page.request.get(base+target['image_url']).status==409
                    assert page.evaluate('device.markers.getLayers().length')==1
                    page.locator('[data-tab="capture"]').click()
                    expect(page.locator('#device-result')).to_be_hidden();expect(page.locator('#save-scan')).to_be_disabled()
                    page.locator('[data-tab="map"]').click();expect(page.locator('#map-list .map-record')).to_have_count(1)
                    page.locator('[data-tab="history"]').click()
                    page.locator('[data-delete-scan]').click();page.locator('#confirm-delete').click()
                    expect(page.locator('.scan-card')).to_have_count(0)
                    expect(page.locator('#saved-count')).to_have_text('0 จุดบันทึก')
                    assert page.evaluate('device.markers.getLayers().length')==0
                    assert ScanStore(data).list()['total']==0
                    assert not errors,errors
                    browser.close()
                    print('PASS: Escape/Enter/Cancel preserve records; network error recoverable; exact ID confirmation; in-flight duplicate guard; other scan/image unchanged; map/history/active-result/exports sync; final empty state; desktop/mobile/800x480; all data temporary')
            finally:
                server.shutdown();thread.join()
    finally:
        service.close()


def ImageWidth(path):
    from PIL import Image
    with Image.open(path) as image:return image.width


if __name__=='__main__':main()
