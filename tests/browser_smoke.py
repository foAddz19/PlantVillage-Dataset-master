"""Run against an already-started app: python tests/browser_smoke.py."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "test-results"
OUTPUT.mkdir(exist_ok=True)
manifest = json.loads((ROOT / "models" / "split_manifest.json").read_text(encoding="utf-8"))
held_out = next(row for row in manifest if row["split"] == "test")
image_path = ROOT / "PlantVillage-Dataset-master" / "raw" / "color" / held_out["path"]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
    page = context.new_page()
    errors, external = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: external.append(request.url)
            if request.url.startswith("http") and not request.url.startswith("http://127.0.0.1:8765/") else None)
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    expect(page.locator("#pill-text")).to_have_text("โมเดลพร้อมใช้งาน")
    expect(page.locator("#quick-samples .sample-card")).to_have_count(6)
    expect(page.locator("#analyze-button")).to_be_disabled()
    page.screenshot(path=str(OUTPUT / "desktop.png"), full_page=True)
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="เลือกหรือลากภาพใบพืช").click()
    chooser.value.set_files(str(image_path))
    expect(page.locator("#preview")).to_be_visible()
    expect(page.locator("#analyze-button")).to_be_enabled()
    page.get_by_role("button", name="วิเคราะห์ภาพ ↗", exact=True).click()
    expect(page.locator("#result-tag")).to_have_text("วิเคราะห์เสร็จแล้ว")
    expect(page.locator("#candidates .candidate")).to_have_count(3)
    expect(page.locator("#sample-note")).to_be_hidden()
    page.screenshot(path=str(OUTPUT / "prediction.png"), full_page=True)
    with page.expect_download() as download:
        page.get_by_role("button", name="บันทึกผลวิเคราะห์ ↓").click()
    download.value.save_as(str(OUTPUT / "prediction.json"))
    saved = json.loads((OUTPUT / "prediction.json").read_text(encoding="utf-8"))
    assert saved["source"] == "upload" and len(saved["candidates"]) == 3
    page.get_by_role("button", name="ล้างภาพ", exact=True).click()
    expect(page.locator("#analyze-button")).to_be_disabled()
    # Dataset examples use the same model and label their train/test membership.
    sample = page.locator("#quick-samples button").filter(has=page.get_by_text("แอปเปิล", exact=True))
    expect(sample).to_have_count(1)
    sample.click()
    expect(page.locator("#preview")).to_be_visible()
    page.get_by_role("button", name="วิเคราะห์ภาพ ↗", exact=True).click()
    expect(page.locator("#sample-note")).to_be_visible()
    expect(page.locator("#sample-note")).to_contain_text("ป้ายกำกับจริง:")
    page.locator('nav [data-view="library"]').click()
    expect(page.locator("#library-grid .sample-card")).to_have_count(12)
    page.locator("#class-filter").select_option("Apple___Apple_scab")
    expect(page.locator("#library-count")).to_have_text("630 รายการ")
    page.get_by_role("button", name="ถัดไป →", exact=True).click()
    expect(page.locator("#page-number")).to_have_text("2 / 53")
    page.screenshot(path=str(OUTPUT / "library.png"), full_page=True)
    page.locator('nav [data-view="model"]').click()
    expect(page.locator("#report-body tr")).to_have_count(38)
    expect(page.locator("#model-metrics")).to_contain_text("82.9%")
    page.screenshot(path=str(OUTPUT / "model.png"), full_page=True)
    # A server-rejected file must leave the UI usable for another upload.
    page.locator('nav [data-view="analyze"]').click()
    page.get_by_role("button", name="ล้างภาพ", exact=True).click()
    page.locator("#image-input").set_input_files({"name": "bad.jpg", "mimeType": "image/jpeg", "buffer": b"not an image"})
    page.get_by_role("button", name="วิเคราะห์ภาพ ↗", exact=True).click()
    expect(page.locator("#result-tag")).to_have_text("วิเคราะห์ไม่สำเร็จ")
    expect(page.locator("#analyze-button")).to_be_enabled()
    page.get_by_role("button", name="ล้างภาพ", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="networkidle")
    expect(page.locator("#quick-samples .sample-card")).to_have_count(6)
    page.screenshot(path=str(OUTPUT / "mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile overflow"
    assert not errors, errors
    assert not external, external
    print(json.dumps({"browser": "Edge", "upload": "passed", "prediction": "passed", "download": "passed",
                      "sample": "passed", "library_pagination": "passed", "model_report": "passed",
                      "invalid_image": "passed", "mobile": "passed", "console_errors": errors,
                      "external_requests": external}, indent=2))
    browser.close()
