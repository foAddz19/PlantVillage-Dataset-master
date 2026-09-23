"""End-to-end PC simulator test with real AI, temporary SQLite, fake camera and no external map traffic."""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from waitress import create_server

from app import create_app

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "test-results"
OUTPUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8766"
FAKE_CAMERA = """window.deviceTracks=[];
navigator.mediaDevices.getUserMedia=async()=>{
  const image=new Image();image.src='/api/image?label=Apple___Apple_scab&index=0';await image.decode();
  const canvas=document.createElement('canvas');canvas.width=640;canvas.height=480;
  const ctx=canvas.getContext('2d');const draw=()=>{ctx.fillStyle='#ccc';ctx.fillRect(0,0,640,480);ctx.drawImage(image,80,0,480,480);};draw();
  const stream=canvas.captureStream(10),timer=setInterval(draw,100);
  for(const track of stream.getTracks()){window.deviceTracks.push(track);const stop=track.stop.bind(track);track.stop=()=>{clearInterval(timer);stop();};}
  return stream;
};"""

with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
    server = create_server(create_app(data_dir=Path(temporary)), host="127.0.0.1", port=8766, threads=2)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            context.add_init_script(FAKE_CAMERA)
            page = context.new_page()
            errors, external = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: external.append(request.url) if request.url.startswith("http") and not request.url.startswith(URL) else None)
            page.goto(URL+"/device", wait_until="networkidle")
            expect(page.locator("#ai-status")).to_have_text("● AI พร้อมตรวจ")
            expect(page.locator("#analyze-scan")).to_be_disabled()
            page.locator("#sample-image").click()
            expect(page.locator("#device-preview")).to_be_visible()
            with page.expect_response("**/api/device/analyze") as response:
                page.locator("#analyze-scan").click()
            original = response.value.json()
            assert response.value.status == 201 and original["source"] == "sample"
            expect(page.locator("#device-result")).to_be_visible()
            page.locator('[data-tab="settings"]').click()
            page.locator("#latitude").fill("17.0")
            page.locator("#latitude").press("Tab")
            page.locator("#toggle-led").click()
            expect(page.locator("#led-status")).to_have_text("ไฟจำลอง: เปิด")
            page.locator('[data-tab="capture"]').click()
            page.locator("#scan-note").fill("แปลงทดลอง <script>alert(1)</script>")
            with page.expect_response("**/save") as saved_response:
                page.locator("#save-scan").click()
            saved = saved_response.value.json()
            assert saved["latitude"] == original["latitude"] and saved["latitude"] != 17.0
            expect(page.locator("#save-scan")).to_be_disabled()
            expect(page.locator("#saved-count")).to_have_text("1 จุดบันทึก")
            page.screenshot(path=str(OUTPUT/"device-desktop.png"), full_page=True)
            page.reload(wait_until="networkidle")
            page.locator('[data-tab="history"]').click()
            expect(page.locator(".scan-card")).to_have_count(1)
            expect(page.locator(".scan-card p")).to_have_text("แปลงทดลอง <script>alert(1)</script>")
            with page.expect_download() as download:
                page.get_by_role("link", name="GeoJSON ↓", exact=True).click()
            path = OUTPUT/"device-scans.geojson"
            download.value.save_as(str(path))
            features=json.loads(path.read_text(encoding="utf-8"))["features"]
            assert len(features)==1 and features[0]["geometry"]["coordinates"]==[original["longitude"],original["latitude"]]
            page.locator('[data-tab="map"]').click()
            expect(page.locator("#map-count")).to_have_text("แสดง 1 จุดบันทึก")
            page.locator(".map-record").click()
            expect(page.locator(".leaflet-popup-content")).to_contain_text("แปลงทดลอง <script>alert(1)</script>")
            page.locator("#map-filter").select_option("manual")
            expect(page.locator("#map-count")).to_have_text("แสดง 0 จุดบันทึก")
            page.locator("#map-filter").select_option("all")
            page.screenshot(path=str(OUTPUT/"device-map.png"), full_page=True)
            assert not external, external
            # Exercise online-map toggle with intercepted tile requests, never contact OSM in automated tests.
            page.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(status=503, body="test offline"))
            page.locator("#online-map").click()
            expect(page.locator("#online-map")).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".leaflet-control-attribution")).to_contain_text("OpenStreetMap")
            page.locator("#online-map").click()
            expect(page.locator("#online-map")).to_have_attribute("aria-pressed", "false")
            page.locator('[data-tab="capture"]').click()
            page.locator("#open-camera").click()
            expect(page.locator("#device-video")).to_be_visible()
            with page.expect_response("**/api/device/analyze") as captured_response:
                page.locator("#analyze-scan").click()
            captured = captured_response.value.json()
            assert captured["source"] == "camera" and len(captured["prediction"]["candidates"]) == 3
            expect(page.locator("#device-result")).to_be_visible()
            page.locator("#observer-severity").select_option("severe")
            page.locator("#save-scan").click()
            expect(page.locator("#saved-count")).to_have_text("2 จุดบันทึก")
            page.locator('[data-tab="map"]').click()
            assert page.evaluate("window.deviceTracks.every(t=>t.readyState==='ended')")
            expect(page.locator('path[fill="#d04f46"]')).to_have_count(1)
            page.set_viewport_size({"width": 800, "height": 480})
            page.locator('[data-tab="capture"]').click()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert page.locator("#analyze-scan").bounding_box()["height"] >= 44
            page.screenshot(path=str(OUTPUT/"device-800x480.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(OUTPUT/"device-mobile.png"), full_page=True)
            assert not errors, errors
            context.close()
            browser.close()
        print(json.dumps({"real_ai": "passed", "capture_save_map": "passed", "immutable_location": "passed",
                          "persistent_history": "passed", "geojson": "passed", "manual_severity": "passed",
                          "fake_camera": "passed", "offline_map": "passed", "xss_escaped": "passed",
                          "touch_800x480": "passed", "mobile": "passed", "real_hardware_used": False}, indent=2))
    finally:
        server.close()
        server.task_dispatcher.shutdown()
        thread.join(timeout=5)
