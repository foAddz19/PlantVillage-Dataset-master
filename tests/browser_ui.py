"""Real Edge + unchanged rice model. Isolated scans; simulated camera/USB only."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import create_app
from leaflab.images import read_image
from leaflab.paddy import PaddyService
from leaflab.printing import PrintService
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from tests.browser_care import find_image

FAKE_CAMERA="""window.uiTracks=[];
navigator.mediaDevices.getUserMedia=async()=>{
 const image=new Image();image.src='/static/ui/rice-specimen.jpg';await image.decode();
 const canvas=document.createElement('canvas');canvas.width=640;canvas.height=480;
 const ctx=canvas.getContext('2d');ctx.fillStyle='#bbb';ctx.fillRect(0,0,640,480);ctx.drawImage(image,140,0,360,480);
 const stream=canvas.captureStream(2);window.uiTracks.push(...stream.getTracks());return stream;
};"""

def main():
    output=ROOT/'test-results/ui';output.mkdir(exist_ok=True)
    service=PaddyService();assert service.status()['ready'],service.message
    confirmed=find_image(service)
    uncertain=ROOT/'datasets/paddy-competition/train_images/normal/100002.jpg'
    assert service.predict(read_image(uncertain))['uncertain']
    report=dict(generated_at=datetime.now(timezone.utc).isoformat(),viewports={},javascript_errors=[],external_requests=[],checks={})
    with tempfile.TemporaryDirectory(prefix='ui-qa-',dir=ROOT/'runtime') as temporary:
        directory=Path(temporary);assert directory.resolve().is_relative_to(ROOT/'runtime')
        config=directory/'printer.json';config.write_text(json.dumps(dict(enabled=True,vendor_id='0x1234',product_id='0xabcd',pause_ms=0)))
        physical=[]
        printer=PrintService(directory,config,sender=lambda commands,config:physical.append(commands))
        app=create_app(service=service,model_backend='paddy',data_dir=directory,printer_service=printer)
        server=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                context=browser.new_context(viewport=dict(width=800,height=480),reduced_motion='reduce')
                context.add_init_script(FAKE_CAMERA)
                page=context.new_page();analysis_requests=[]
                page.on('pageerror',lambda error:report['javascript_errors'].append(str(error)))
                page.on('request',lambda request:report['external_requests'].append(request.url) if request.url.startswith(('http:','https:')) and not request.url.startswith(base+'/') else None)
                page.on('request',lambda request:analysis_requests.append(request.url) if request.url.endswith('/api/device/analyze') and request.method=='POST' else None)
                page.route('https://**/*',lambda route:route.abort())
                page.goto(base+'/device',wait_until='networkidle')
                expect(page.locator('#ai-status')).to_have_text('AI พร้อมใช้งาน')
                expect(page.locator('#screen-home')).to_be_visible()
                expect(page.locator('#gps-status')).to_contain_text('ยังไม่มีข้อมูล GPS จริง')
                expect(page.locator('#battery-status')).to_contain_text('ยังไม่มีตัวอ่าน UPS')
                assert page.locator('input#battery').count()==0
                page.locator('#start-scan').click();page.locator('#open-camera').click()
                expect(page.locator('#device-video')).to_be_visible()
                expect(page.locator('#analyze-scan')).to_be_disabled()
                assert not analysis_requests
                page.locator('#capture-photo').click()
                expect(page.locator('#device-preview')).to_be_visible()
                expect(page.locator('#device-video')).to_be_hidden()
                expect(page.locator('#analyze-scan')).to_be_enabled()
                assert not analysis_requests,'Photo capture must never analyze automatically'
                assert page.evaluate('uiTracks.length>0 && uiTracks.every(t=>t.readyState==="ended")')
                frozen_sha=page.evaluate("async()=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await device.blob.arrayBuffer()))).map(b=>b.toString(16).padStart(2,'0')).join('')")
                pending=[];page.route('**/api/device/analyze',lambda route:pending.append(route))
                with page.expect_request('**/api/device/analyze'):page.locator('#analyze-scan').click()
                expect(page.locator('#analyze-scan')).to_be_disabled()
                expect(page.locator('#analysis-overlay')).to_be_visible()
                page.locator('#analyze-scan').evaluate('(button)=>button.click()')
                assert len(pending)==1 and len(analysis_requests)==1
                with page.expect_response('**/api/device/analyze') as response:pending[0].continue_()
                camera_row=response.value.json();assert response.value.status==201
                expect(page.locator('#screen-result')).to_be_visible()
                payload=page.request.get(base+camera_row['image_url']).body()
                assert hashlib.sha256(payload).hexdigest()==frozen_sha
                assert camera_row['source']=='camera'
                assert camera_row['prediction']==service.predict(read_image(io.BytesIO(payload)))
                page.unroute('**/api/device/analyze')
                report['checks']['manual_frozen_capture_no_auto_inference']=True
                report['checks']['exact_frozen_frame_and_real_model_scores']=True
                report['checks']['no_duplicate_requests_while_busy']=True

                # Real confident result; note/coordinates are user supplied, never GPS claims.
                page.locator('[data-tab="settings"]').click()
                page.locator('#location-source').select_option('manual')
                page.locator('#latitude').fill('16.053');page.locator('#longitude').fill('103.652');page.locator('#longitude').press('Tab')
                page.locator('[data-tab="capture"]').click()
                page.locator('#device-file').set_input_files(str(confirmed))
                with page.expect_response('**/api/device/analyze') as response:page.locator('#analyze-scan').click()
                row=response.value.json();assert not row['prediction']['uncertain']
                expect(page.locator('#screen-result')).to_be_visible()
                expect(page.locator('#result-confidence')).to_have_text(f"{row['prediction']['candidates'][0]['score']*100:.1f}%")
                for index,candidate in enumerate(row['prediction']['candidates']):
                    expect(page.locator('.rank-row').nth(index).locator('span').last).to_have_text(f"{candidate['score']*100:.1f}%")
                expect(page.locator('#print-scan')).to_be_disabled()
                page.locator('#result-detail').click();expect(page.locator('#care-dialog')).to_be_visible()
                assert page.locator('#care-dialog .care-sources a').count()>=1
                page.locator('#care-dialog .care-dialog-heading button').click()
                page.locator('#edit-observations').click()
                hostile='แปลงทดสอบ <img src=x onerror="window.UI_XSS=1">'
                page.locator('#scan-note').fill(hostile);page.locator('#observer-severity').select_option('mild')
                page.locator('#close-observations').click()
                with page.expect_response('**/save') as saved:page.locator('#save-scan').click()
                saved_row=saved.value.json()
                assert saved_row['prediction']==row['prediction'] and saved_row['latitude']==row['latitude']
                expect(page.locator('#save-scan')).to_have_text('✓ บันทึกแล้ว')
                page.locator('#preview-receipt').click()
                expect(page.locator('#receipt-image')).to_have_js_property('naturalWidth',384)
                page.screenshot(path=str(output/'receipt-800.png'),animations='disabled')
                page.locator('#close-receipt').click();page.locator('#print-scan').click()
                expect(page.locator('#print-scan')).to_have_text('พิมพ์ซ้ำ');assert len(physical)==1
                page.locator('[data-tab="history"]').click()
                expect(page.locator('.scan-card')).to_have_count(1)
                expect(page.locator('.scan-card p')).to_have_text(hostile)
                assert page.locator('.scan-card p img').count()==0 and page.evaluate('window.UI_XSS===undefined')
                page.locator('[data-print-scan]').click();expect(page.locator('[data-print-scan]')).to_be_enabled();assert len(physical)==2
                page.locator('[data-view-scan]').click();expect(page.locator('#screen-result')).to_be_visible()
                expect(page.locator('#analyzed-frame')).to_have_attribute('src',row['image_url'])
                expect(page.locator('#result-location')).to_contain_text('พิกัดระบุเอง')
                report['checks']['save_snapshot_history_reopen_xss_escaped']=True
                report['checks']['native_thai_receipt_and_explicit_reprint_mock_transport']=True

                # Capture viewport screenshots with a real confident prediction.
                for width,height,label in [(1440,900,'desktop'),(800,480,'800'),(390,844,'mobile')]:
                    page.set_viewport_size(dict(width=width,height=height))
                    result={}
                    for screen in ['home','capture','result','history','map','care','settings']:
                        if screen=='result':
                            page.locator('[data-tab="history"]').click();page.locator('[data-view-scan]').first.click()
                        else:page.locator(f'[data-tab="{screen}"]').click()
                        expect(page.locator(f'#screen-{screen}')).to_be_visible()
                        if screen=='map':
                            page.locator('#show-map-coverage').click()
                            expect(page.locator('#map-mode')).to_have_attribute('data-state','ready',timeout=15000)
                        if screen=='care':expect(page.locator('.care-library-card')).to_have_count(10)
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(label,screen,'horizontal overflow')
                        dimensions=page.evaluate("""()=>{const main=document.querySelector('main'),screen=document.querySelector('.screen:not([hidden])');return {mainClient:main.clientHeight,mainScroll:main.scrollHeight,screenClient:screen.clientHeight,screenScroll:screen.scrollHeight};}""")
                        if label!='mobile' and screen in ['home','capture','result']:
                            assert dimensions['mainScroll']<=dimensions['mainClient']+1,(label,screen,dimensions)
                            assert dimensions['screenScroll']<=dimensions['screenClient']+1,(label,screen,dimensions)
                            actions=page.locator('.capture-actionbar' if screen=='capture' else '.result-actionbar' if screen=='result' else '#start-scan').bounding_box()
                            assert actions['y']+actions['height']<=height-24,(label,screen,actions)
                            if screen=='result':
                                clipping=page.evaluate("""()=>{const parent=document.querySelector('.advice-brief').getBoundingClientRect();return Array.from(document.querySelectorAll('.advice-brief p')).filter(p=>p.getBoundingClientRect().bottom>parent.bottom+1).map(p=>p.id)}""")
                                assert not clipping,(label,screen,'clipped guidance',clipping)
                        small=page.evaluate("""()=>Array.from(document.querySelectorAll('button,label.button')).filter(b=>b.getClientRects().length && !b.closest('dialog:not([open])')).map(b=>({text:b.textContent.trim(),h:b.getBoundingClientRect().height,w:b.getBoundingClientRect().width})).filter(b=>b.h<43.9 || b.w<43.9)""")
                        assert not small,(label,screen,small)
                        page.screenshot(path=str(output/f'{screen}-{label}.png'),animations='disabled',style='#device-message{visibility:hidden!important}')
                        result[screen]=dimensions
                    report['viewports'][label]=result
                page.set_viewport_size(dict(width=800,height=480))
                page.locator('[data-tab="care"]').click()
                keys=['bacterial_leaf_blight','bacterial_leaf_streak','bacterial_panicle_blight','blast','brown_spot','dead_heart','downy_mildew','hispa','normal','tungro']
                for key in keys:
                    page.locator('#care-search').fill(key);expect(page.locator('.care-library-card')).to_have_count(1)
                    assert page.locator('.care-english').inner_text().strip()
                    page.locator('.care-library-card button').click();expect(page.locator('#care-dialog')).to_be_visible()
                    assert page.locator('#care-dialog .care-sources a').count()>=1
                    page.locator('#care-dialog .care-dialog-heading button').click()
                page.locator('#care-search').fill('')
                report['checks']['all_10_existing_guides_english_and_sources']=True

                # Real inconclusive image: cautious wording, unchanged scores, generic guidance.
                page.locator('[data-tab="capture"]').click();page.locator('#device-file').set_input_files(str(uncertain))
                with page.expect_response('**/api/device/analyze') as response:page.locator('#analyze-scan').click()
                unsure=response.value.json();assert unsure['prediction']['uncertain']
                expect(page.locator('#result-disease')).to_have_text('ยังไม่สามารถระบุโรคได้อย่างมั่นใจ')
                expect(page.locator('#result-confidence')).to_have_text(f"{unsure['prediction']['candidates'][0]['score']*100:.1f}%")
                page.screenshot(path=str(output/'uncertain-800.png'),animations='disabled',style='#device-message{visibility:hidden!important}')
                page.locator('#save-scan').click();expect(page.locator('#save-scan')).to_have_text('✓ บันทึกแล้ว')
                page.locator('[data-tab="history"]').click();expect(page.locator('.scan-card')).to_have_count(2)
                delete=page.locator(f'[data-delete-scan="{row["id"]}"]')
                delete.click();expect(page.locator('#delete-dialog')).to_be_visible()
                page.screenshot(path=str(output/'delete-800.png'),animations='disabled',style='#device-message{visibility:hidden!important}')
                page.locator('#cancel-delete').click();expect(page.locator('.scan-card')).to_have_count(2)
                delete.click();page.locator('#confirm-delete').click();expect(page.locator('.scan-card')).to_have_count(1)
                kept=page.request.get(base+'/api/device/scans').json();assert kept['items'][0]['id']==unsure['id']
                assert page.request.get(base+'/api/device/export/geojson').json()['features'][0]['properties']['id']==unsure['id']
                page.locator('[data-tab="map"]').click();expect(page.locator('#map-count')).to_have_text('แสดง 1 จุดบันทึก')
                page.locator('#map-filter').select_option('gps');expect(page.locator('#map-count')).to_have_text('แสดง 0 จุดบันทึก')
                page.locator('#map-filter').select_option('all')
                report['checks']['uncertain_wording_and_original_scores']=True
                report['checks']['delete_cancel_and_exact_item_history_map_export_sync']=True
                report['checks']['real_local_mbtiles_and_location_provenance']=True

                page.locator('[data-tab="home"]').click();page.locator('#theme-toggle').click()
                expect(page.locator('html')).to_have_attribute('data-theme','dark')
                page.screenshot(path=str(output/'home-dark-800.png'),animations='disabled',style='#device-message{visibility:hidden!important}')
                page.reload(wait_until='networkidle');expect(page.locator('html')).to_have_attribute('data-theme','dark')
                expect(page.locator('#home-total')).to_have_text('1')
                page.locator('[data-tab="history"]').click();page.locator('[data-view-scan]').click()
                page.screenshot(path=str(output/'result-dark-800.png'),animations='disabled',style='#device-message{visibility:hidden!important}')
                report['checks']['dark_mode_and_saved_history_survive_reload']=True
                page.locator('#theme-toggle').click()
                page.route('**/api/status',lambda route:route.fulfill(json=dict(ready=False)))
                page.evaluate('status()');expect(page.locator('#ai-status')).to_have_text('โมเดลยังไม่พร้อม')
                page.locator('[data-tab="capture"]').click();page.locator('#device-file').set_input_files(str(confirmed))
                expect(page.locator('#analyze-scan')).to_be_disabled()
                report['checks']['model_unavailable_disables_analysis']=True
                page.unroute('**/api/status')
                context.close()
                for error_name,fragment in [('NotAllowedError','ไม่ได้รับอนุญาต'),('NotFoundError','ไม่พบกล้อง')]:
                    failure=browser.new_context(viewport=dict(width=800,height=480),reduced_motion='reduce')
                    failure.add_init_script(f"navigator.mediaDevices.getUserMedia=async()=>{{throw new DOMException('test','{error_name}')}}")
                    tab=failure.new_page();tab.goto(base+'/device#capture',wait_until='networkidle');tab.locator('#open-camera').click()
                    expect(tab.locator('#device-message')).to_contain_text(fragment)
                    expect(tab.locator('#analyze-scan')).to_be_disabled();failure.close()
                report['checks']['camera_permission_and_missing_device_errors']=True
                assert not report['javascript_errors'],report['javascript_errors']
                assert not report['external_requests'],report['external_requests']
                assert len(physical)==2
                report.update(real_model_inference=True,real_mbtiles=True,physical_paper_printed=False,real_camera_used=False,
                              actual_gps_connected=False,ups_reader_present=False,mocked_print_jobs=len(physical),tests_use_temporary_data=True)
                browser.close()
        finally:
            server.shutdown();thread.join();service.close()
    (output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(checks=report['checks'],viewports=list(report['viewports']),javascript_errors=report['javascript_errors'],external_requests=report['external_requests']),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
