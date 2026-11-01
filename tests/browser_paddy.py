"""Self-contained Edge smoke test; scan storage always lives in a temporary folder."""
import argparse
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from leaflab.dataset import ROOT
from leaflab.paddy import PaddyService
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

parser = argparse.ArgumentParser()
parser.add_argument('--model-dir', type=Path, default=ROOT / 'models' / 'paddy')
parser.add_argument('--dataset-dir', type=Path, default=ROOT / 'datasets' / 'paddy-competition')
args = parser.parse_args()
service = PaddyService(model_dir=args.model_dir)
assert service.status()['ready'], service.message
with tempfile.TemporaryDirectory() as directory:
    app = create_app(service=service, model_backend='paddy', dataset_dir=args.dataset_dir, data_dir=Path(directory))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            expect(page.locator('#pill-text')).to_have_text('โมเดลพร้อมใช้งาน')
            expect(page.locator('#quick-samples .sample-card')).to_have_count(6)
            page.locator('#quick-samples button').first.click()
            page.locator('#analyze-button').click()
            expect(page.locator('#candidates .candidate')).to_have_count(3)
            expect(page.locator('#sample-note')).to_contain_text('ป้ายกำกับจริง:')
            output = ROOT / 'test-results'
            output.mkdir(exist_ok=True)
            page.screenshot(path=str(output / 'paddy-prediction.png'), full_page=True)
            page.locator('nav [data-view="model"]').click()
            expect(page.locator('#training-panel')).to_be_hidden()
            expect(page.locator('#report-body tr')).to_have_count(len(service.config['labels']))
            expect(page.locator('#model-runtime-note')).to_contain_text('INT8')
            page.screenshot(path=str(output / 'paddy-report.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.reload(wait_until='networkidle')
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(output / 'paddy-mobile.png'), full_page=True)
            assert not errors, errors
            print('PASS: rice examples, prediction, report, disabled web training, mobile layout, no JavaScript errors')
            browser.close()
    finally:
        server.shutdown()
        thread.join()
        service.close()
