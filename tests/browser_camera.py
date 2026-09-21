"""Camera tests using a canvas-backed MediaStream; never opens real hardware."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "test-results"
OUTPUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8765"

INSTRUMENT = """(() => {
  window.cameraRequests = [];
  window.cameraTracks = [];
  // A canvas stream avoids OS camera drivers while exercising video.play/capture for real.
  navigator.mediaDevices.getUserMedia = async constraints => {
    window.cameraRequests.push(constraints);
    const image = new Image();
    image.src = '/api/image?label=Apple___Apple_scab&index=0';
    await image.decode();
    const canvas = document.createElement('canvas'); canvas.width=640; canvas.height=480;
    const ctx=canvas.getContext('2d');
    const draw=()=> {ctx.fillStyle='#ccc';ctx.fillRect(0,0,640,480);ctx.drawImage(image,128,48,384,384);};
    draw();
    const stream=canvas.captureStream(10);
    const timer=setInterval(draw,100);
    for (const track of stream.getTracks()) {
      window.cameraTracks.push(track);
      const stop=track.stop.bind(track);
      track.stop=()=> {clearInterval(timer);stop();};
    }
    return stream;
  };
  navigator.mediaDevices.enumerateDevices = async () => [
    {kind:'videoinput',deviceId:'fake-one',label:'Test camera 1'},
    {kind:'videoinput',deviceId:'fake-two',label:'Test camera 2'},
  ];
})()"""

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True, args=[
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    ])
    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    context.add_init_script(INSTRUMENT)
    page = context.new_page()
    errors = []
    active_requests, max_active = set(), [0]
    def began(request):
        if request.url == URL + "/api/predict":
            active_requests.add(request)
            max_active[0] = max(max_active[0], len(active_requests))
    page.on("request", began)
    page.on("requestfinished", lambda request: active_requests.discard(request))
    page.on("requestfailed", lambda request: active_requests.discard(request))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL, wait_until="networkidle")
    assert page.evaluate("window.cameraRequests.length") == 0, "Camera must not open on page load"
    expect(page.locator("#pill-text")).to_have_text("โมเดลพร้อมใช้งาน")
    page.get_by_role("button", name="ใช้กล้อง", exact=True).click()
    expect(page.locator("#camera-input-panel")).to_be_visible()
    assert page.evaluate("window.cameraRequests.length") == 0, "Selecting the tab must not request access"
    page.get_by_role("button", name="เปิดกล้อง", exact=True).click()
    expect(page.locator("#camera-live")).to_be_visible()
    expect(page.locator("#analyze-button")).to_be_enabled()
    assert page.locator("#camera-video").evaluate("video => video.videoWidth > 0")
    assert page.evaluate("window.cameraRequests.every(c => c.audio === false)")
    page.get_by_role("button", name="วิเคราะห์ภาพจากกล้อง ↗", exact=True).click()
    expect(page.locator("#camera-result-info")).to_be_visible()
    expect(page.locator("#candidates .candidate")).to_have_count(3)
    expect(page.locator("#camera-result-time")).to_contain_text("พิกเซล")
    with page.expect_download() as download:
        page.get_by_role("button", name="บันทึกผลวิเคราะห์ ↓").click()
    download.value.save_as(str(OUTPUT / "camera-result.json"))
    result = json.loads((OUTPUT / "camera-result.json").read_text(encoding="utf-8"))
    assert result["source"] == "camera" and result["captured_at"] and result["crop_size"] <= 640
    assert page.locator("#camera-result-image").evaluate("img => img.naturalWidth === img.naturalHeight")
    page.screenshot(path=str(OUTPUT / "camera-desktop.png"), full_page=True)

    # Repeated frames go through the real inference endpoint, never concurrently.
    page.locator("#camera-interval").select_option("2000")
    with page.expect_response("**/api/predict"):
        page.get_by_role("button", name="เริ่มวิเคราะห์ต่อเนื่อง", exact=True).click()
    with page.expect_response("**/api/predict", timeout=10000):
        expect(page.locator("#camera-auto")).to_have_text("หยุดวิเคราะห์ต่อเนื่อง")
    page.get_by_role("button", name="หยุดวิเคราะห์ต่อเนื่อง", exact=True).click()
    expect(page.locator("#camera-auto")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#camera-live")).to_be_visible()
    assert max_active[0] == 1, f"Overlapping camera requests: {max_active[0]}"

    # Closing a camera during an unfinished request suppresses the late result.
    pending = []
    page.route("**/api/predict", lambda route: pending.append(route))
    with page.expect_request("**/api/predict"):
        page.get_by_role("button", name="เริ่มวิเคราะห์ต่อเนื่อง", exact=True).click()
    page.get_by_role("button", name="ปิดกล้อง", exact=True).click()
    expect(page.locator("#camera-live")).to_be_hidden()
    expect(page.locator("#camera-auto")).to_have_attribute("aria-pressed", "false")
    assert page.evaluate("window.cameraTracks.every(t => t.readyState === 'ended')")
    old_timestamp = page.locator("#camera-result-time").inner_text()
    for route in pending:
        route.fulfill(json=result)
    page.unroute("**/api/predict")
    # Advance a known timer boundary to verify Stop cleared the scheduled loop.
    page.clock.install()
    page.clock.fast_forward(6000)
    expect(page.locator("#camera-result-time")).to_have_text(old_timestamp)
    expect(page.locator("#result-loading")).to_be_hidden()

    # Switching camera selection, input source, and navigation releases tracks.
    page.get_by_role("button", name="เปิดกล้อง", exact=True).click()
    expect(page.locator("#camera-live")).to_be_visible()
    page.locator("#camera-device").select_option("fake-two")
    expect(page.locator("#camera-live")).to_be_visible()
    assert page.evaluate("window.cameraTracks.filter(t => t.readyState === 'live').length") == 1
    page.get_by_role("button", name="เลือกไฟล์", exact=True).click()
    assert page.evaluate("window.cameraTracks.every(t => t.readyState === 'ended')")
    page.get_by_role("button", name="ใช้กล้อง", exact=True).click()
    page.get_by_role("button", name="เปิดกล้อง", exact=True).click()
    expect(page.locator("#camera-live")).to_be_visible()
    page.locator('nav [data-view="library"]').click()
    assert page.evaluate("window.cameraTracks.every(t => t.readyState === 'ended')")
    page.locator('nav [data-view="analyze"]').click()
    page.get_by_role("button", name="เปิดกล้อง", exact=True).click()
    expect(page.locator("#camera-live")).to_be_visible()
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, get:()=>true}); document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#camera-live")).to_be_hidden()
    assert page.evaluate("window.cameraTracks.every(t => t.readyState === 'ended')")
    page.evaluate("delete document.hidden")

    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(OUTPUT / "camera-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors, errors
    context.close()

    # Test browser/device error handling without granting any real device permission.
    for name, message in [("NotAllowedError", "ไม่ได้รับอนุญาต"), ("NotFoundError", "ไม่พบกล้อง"),
                          ("NotReadableError", "เปิดกล้องไม่ได้")]:
        failure = browser.new_context()
        failure.add_init_script(f"navigator.mediaDevices.getUserMedia = async () => {{ throw new DOMException('simulated', '{name}'); }};")
        tab = failure.new_page()
        tab.goto(URL, wait_until="networkidle")
        tab.get_by_role("button", name="ใช้กล้อง", exact=True).click()
        tab.get_by_role("button", name="เปิดกล้อง", exact=True).click()
        expect(tab.locator("#camera-message")).to_contain_text(message)
        expect(tab.locator("#camera-open")).to_be_enabled()
        expect(tab.locator("#analyze-button")).to_be_disabled()
        failure.close()

    # A permission dialog resolving after Cancel must release its eventual stream.
    late = browser.new_context()
    late.add_init_script("""window.lateTracks=[];
      navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { window.resolveCamera = () => {
        const c=document.createElement('canvas'); c.width=c.height=64;
        const stream=c.captureStream(5); window.lateTracks=stream.getTracks(); resolve(stream);
      }; });""")
    tab = late.new_page()
    tab.goto(URL, wait_until="networkidle")
    tab.get_by_role("button", name="ใช้กล้อง", exact=True).click()
    tab.get_by_role("button", name="เปิดกล้อง", exact=True).click()
    expect(tab.locator("#camera-stop")).to_have_text("ยกเลิกเปิดกล้อง")
    tab.get_by_role("button", name="ยกเลิกเปิดกล้อง", exact=True).click()
    tab.evaluate("async () => {window.resolveCamera(); await Promise.resolve();}")
    assert tab.evaluate("window.lateTracks.length && window.lateTracks.every(t => t.readyState === 'ended')")
    expect(tab.locator("#camera-live")).to_be_hidden()
    late.close()
    browser.close()
    print(json.dumps({"fake_camera_capture": "passed", "real_model_inference": "passed",
                      "continuous_frames": "passed", "max_concurrent_requests": max_active[0],
                      "cancel_inflight": "passed", "release_tracks": "passed", "permission_errors": "passed",
                      "late_permission_cancel": "passed", "mobile": "passed", "real_camera_used": False,
                      "javascript_errors": errors}, indent=2))
