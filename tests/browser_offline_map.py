"""Cold-browser offline maps: local tiles, temporary scans, no AI or USB hardware.

Run from the project virtual environment; --maps-dir defaults to ROOT/maps.
Every non-local network request is blocked and fails this regression check.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from leaflab.dataset import Catalog, ROOT
from tests.test_device import StubService


@contextmanager
def running_app(directory: Path, maps_dir: Path):
    catalog = directory / "catalog"
    catalog.mkdir()
    app = create_app(Catalog(catalog), StubService(), data_dir=directory / "data",
                     maps_dir=maps_dir)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def create_scan(page, base: str, latitude: float, longitude: float,
                source: str, note: str):
    payload = io.BytesIO()
    Image.new("RGB", (90, 64), (40, 110, 50)).save(payload, "PNG")
    token = page.locator('meta[name="app-token"]').get_attribute("content")
    headers = {"X-App-Token": token}
    response = page.request.post(base + "/api/device/analyze", headers=headers,
                                 multipart={
                                     "image": {"name": "offline-map.png",
                                               "mimeType": "image/png",
                                               "buffer": payload.getvalue()},
                                     "source": "upload", "latitude": str(latitude),
                                     "longitude": str(longitude),
                                     "location_source": source,
                                 })
    assert response.status == 201, response.text()
    row = response.json()
    saved = page.request.post(base + f"/api/device/scans/{row['id']}/save",
                              headers=headers, data={"severity": "mild", "note": note})
    assert saved.status == 200, saved.text()
    return saved.json()


def local_context(browser, base: str):
    context = browser.new_context(viewport={"width": 1280, "height": 960},
                                  service_workers="block")
    external = []
    errors = []
    tile_responses = []
    origin = urlparse(base)

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.scheme in ("data", "blob", "about"):
            route.continue_()
        elif (parsed.scheme, parsed.hostname, parsed.port) == (origin.scheme, origin.hostname, origin.port):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort("internetdisconnected")

    context.route("**/*", intercept)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: tile_responses.append({"url": response.url,
                                                               "status": response.status})
            if "/api/device/map/tiles/" in response.url else None)
    return context, page, external, errors, tile_responses


def assert_ready(page):
    try:
        expect(page.locator("#map-mode")).to_have_attribute("data-state", "ready", timeout=15000)
    except AssertionError:
        print("MAP DEBUG " + json.dumps(page.evaluate("({state:document.querySelector('#map-mode').dataset.state,center:device.map.getCenter(),zoom:device.map.getZoom(),size:device.map.getSize(),bounds:device.mapInfo.bounds})"),ensure_ascii=True),flush=True)
        raise
    expect(page.locator("#offline-map")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#map-notice")).to_be_hidden()


def test_package(browser, maps_dir: Path, output: Path, required_native_zoom=None):
    with tempfile.TemporaryDirectory(prefix="ai-plant-offline-map-") as temporary:
        with running_app(Path(temporary), maps_dir) as base:
            context, page, external, errors, tile_responses = local_context(browser, base)
            try:
                page.goto(base + "/device", wait_until="networkidle")
                response = page.request.get(base + "/api/device/map/status")
                assert response.status == 200, response.text()
                status = response.json()
                assert status["ready"], status
                if required_native_zoom is not None:
                    assert status["maxzoom"] == required_native_zoom, status
                west, south, east, north = status["bounds"]
                center = status.get("center") or [(west + east) / 2, (south + north) / 2,
                                                   min(status["maxzoom"], 16)]
                longitude, latitude = center[:2]
                note = '<img src=x onerror="window.mapXss=1"> & "offline"'
                first = create_scan(page, base, latitude, longitude, "simulated", note)
                create_scan(page, base, latitude + (north - south) / 10000,
                            longitude + (east - west) / 10000, "manual", "manual offline point")
                page.reload(wait_until="networkidle")
                leaflet_assets = page.locator('script[src*="leaflet"], link[href*="leaflet"]').evaluate_all(
                    "nodes => nodes.map(node => node.src || node.href)")
                assert len(leaflet_assets) >= 2, leaflet_assets
                assert all(url.startswith(base + "/static/") for url in leaflet_assets), leaflet_assets
                page.locator('[data-tab="map"]').click()
                assert_ready(page)
                expect(page.locator("#map-pack")).to_contain_text(status["name"])
                expect(page.locator("#map-list .map-record")).to_have_count(2)
                assert page.evaluate("device.markers.getLayers().length") == 2
                page.wait_for_function("document.querySelector('#field-map .leaflet-tile-loaded') !== null")
                assert any(row["status"] == 200 for row in tile_responses), tile_responses
                page.evaluate("id => device.markers.getLayers()[device.records.findIndex(row => row.id === id)].openPopup()", first["id"])
                expect(page.locator(".leaflet-popup-content")).to_contain_text(note)
                assert page.locator('.leaflet-popup-content img[src="x"]').count() == 0
                assert page.evaluate("window.mapXss === undefined")
                page.evaluate("device.map.closePopup()")
                page.locator("#map-filter").select_option("manual")
                expect(page.locator("#map-list .map-record")).to_have_count(1)
                assert page.evaluate("device.markers.getLayers().length") == 1
                page.locator("#map-filter").select_option("all")
                expect(page.locator("#map-list .map-record")).to_have_count(2)
                page.locator('[data-tab="history"]').click()
                expect(page.locator("#history-list .scan-card")).to_have_count(2)
                expect(page.locator("#history-list")).to_contain_text(note)
                page.locator('[data-tab="map"]').click()
                page.locator("#show-map-coverage").click()
                assert_ready(page)
                assert page.evaluate("bounds => device.map.getBounds().contains(L.latLngBounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]]))", status["bounds"])
                before = page.evaluate("({zoom: device.map.getZoom(), center: device.map.getCenter()})")
                page.evaluate("device.map.zoomIn(1, {animate: false})")
                assert page.evaluate("device.map.getZoom()") == min(before["zoom"] + 1, page.evaluate("device.map.getMaxZoom()"))
                page.evaluate("device.map.panBy([40, 0], {animate: false})")
                assert page.evaluate("device.map.getCenter().lng") != before["center"]["lng"]
                page.locator("#show-map-coverage").click()
                assert_ready(page)
                for width, height, label in ((1280, 960, "desktop"), (800, 480, "pi-screen"), (390, 844, "mobile")):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("device.map.invalidateSize({animate: false})")
                    page.locator("#show-map-coverage").click()
                    assert_ready(page)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                    expect(page.locator("#field-map")).to_be_visible()
                    page.screenshot(path=str(output / f"offline-map-{label}.png"), full_page=True)
                    if required_native_zoom is not None:
                        detail_lat,detail_lon=(16.053,103.652) if west<103.652<east and south<16.053<north else (latitude,longitude)
                        page.evaluate("p=>device.map.setView([p.lat,p.lon],p.zoom,{animate:false})",{"lat":detail_lat,"lon":detail_lon,"zoom":required_native_zoom})
                        assert_ready(page)
                        page.wait_for_function("z=>Array.from(document.querySelectorAll('#field-map .leaflet-tile-loaded')).some(image=>image.src.includes('/tiles/'+z+'/'))",arg=required_native_zoom)
                        assert page.evaluate("device.tiles.options.maxNativeZoom") == required_native_zoom
                        page.locator("#field-map").screenshot(path=str(output / f"offline-map-z{required_native_zoom}-{label}.png"))
                page.set_viewport_size({"width": 1280, "height": 960})
                page.locator("#show-map-coverage").click()
                assert_ready(page)
                for _ in range(3):
                    page.locator("#offline-map").click()
                    expect(page.locator("#map-mode")).to_have_attribute("data-state", "grid")
                    expect(page.locator("#offline-map")).to_have_attribute("aria-pressed", "false")
                    expect(page.locator("#map-notice")).to_be_hidden()
                    assert page.evaluate("device.markers.getLayers().length") == 2
                    page.locator("#offline-map").click()
                    assert_ready(page)
                outside_lat = min(84.0, north + max(.5, (north-south)/4))
                outside_lon = (west+east)/2
                assert outside_lat > north
                page.evaluate("p => device.map.setView([p.lat,p.lon], Math.min(16,device.map.getMaxZoom()), {animate:false})", {"lat":outside_lat,"lon":outside_lon})
                expect(page.locator("#map-mode")).to_have_attribute("data-state", "outside")
                expect(page.locator("#map-notice")).to_be_visible()
                assert page.evaluate("device.markers.getLayers().length") == 2
                assert abs(page.evaluate("device.map.getCenter().lat") - outside_lat) < .001
                page.screenshot(path=str(output / "offline-map-outside.png"), full_page=True)
                page.locator("#show-map-coverage").click()
                assert_ready(page)
                assert page.request.get(base + "/api/device/scans").json()["total"] == 2
                assert not external, external
                assert not errors, errors
                return {"package": status, "saved_scan_id": first["id"],
                        "native_detail_zoom": required_native_zoom,
                        "local_tile_200_responses": sum(row["status"] == 200 for row in tile_responses),
                        "external_requests": external, "page_errors": errors,
                        "checks": ["cold local assets and tiles", "map and history records", "escaped note",
                                   "location filters", "coverage/zoom/pan", "desktop/Pi/mobile layout",
                                   "grid toggle", "outside coverage keeps points"]}
            finally:
                context.close()


def test_tile_failure_retry(browser, maps_dir: Path, output: Path):
    with tempfile.TemporaryDirectory(prefix="ai-plant-map-retry-") as temporary:
        with running_app(Path(temporary), maps_dir) as base:
            context, page, external, errors, tile_responses = local_context(browser, base)
            try:
                page.goto(base + "/device", wait_until="networkidle")
                status = page.request.get(base + "/api/device/map/status").json()
                assert status["ready"], status
                west, south, east, north = status["bounds"]
                center = status.get("center") or [(west + east) / 2, (south + north) / 2]
                create_scan(page, base, center[1], center[0], "manual", "point survives tile failure")
                page.reload(wait_until="networkidle")
                pattern = "**/api/device/map/tiles/**"
                page.route(pattern, lambda route: route.fulfill(status=503, content_type="text/plain",
                                                                body="simulated local tile read failure"))
                page.locator('[data-tab="map"]').click()
                expect(page.locator("#map-mode")).to_have_attribute("data-state", "error")
                expect(page.locator("#map-notice")).to_be_visible()
                expect(page.locator("#retry-map")).to_be_visible()
                expect(page.locator("#map-list .map-record")).to_have_count(1)
                assert page.evaluate("device.markers.getLayers().length") == 1
                assert any(row["status"] == 503 for row in tile_responses), tile_responses
                page.screenshot(path=str(output / "offline-map-error.png"), full_page=True)
                # Recovery uses the real local MBTiles server, not a mocked image.
                page.unroute(pattern)
                page.locator("#retry-map").click()
                assert_ready(page)
                page.wait_for_function("document.querySelector('#field-map .leaflet-tile-loaded') !== null")
                expect(page.locator("#map-list .map-record")).to_have_count(1)
                assert any(row["status"] == 200 for row in tile_responses), tile_responses
                assert not external, external
                assert not errors, errors
                return {"simulated_tile_503": True, "recovered_real_local_tile_200": True,
                        "points": 1, "external_requests": external, "page_errors": errors}
            finally:
                context.close()


def test_missing_package(browser, output: Path):
    with tempfile.TemporaryDirectory(prefix="ai-plant-map-missing-") as temporary:
        directory = Path(temporary)
        maps = directory / "empty-maps"
        maps.mkdir()
        with running_app(directory, maps) as base:
            context, page, external, errors, tile_responses = local_context(browser, base)
            try:
                page.goto(base + "/device", wait_until="networkidle")
                status = page.request.get(base + "/api/device/map/status").json()
                assert status["ready"] is False, status
                create_scan(page, base, 16.053, 103.652, "manual", "point remains without a basemap")
                page.reload(wait_until="networkidle")
                page.locator('[data-tab="map"]').click()
                expect(page.locator("#map-mode")).to_have_attribute("data-state", "missing")
                expect(page.locator("#map-notice")).to_be_visible()
                expect(page.locator("#map-list .map-record")).to_have_count(1)
                assert page.evaluate("device.markers.getLayers().length") == 1
                page.locator("#map-list .map-record").click()
                expect(page.locator(".leaflet-popup-content")).to_contain_text("point remains without a basemap")
                page.locator("#retry-map").click()
                expect(page.locator("#map-mode")).to_have_attribute("data-state", "missing")
                expect(page.locator("#map-list .map-record")).to_have_count(1)
                page.screenshot(path=str(output / "offline-map-missing.png"), full_page=True)
                assert not external, external
                assert not errors, errors
                assert not tile_responses, tile_responses
                return {"ready": False, "points": 1, "external_requests": external,
                        "page_errors": errors, "checks": ["missing package notice", "saved points still usable", "retry safe"]}
            finally:
                context.close()


def test_high_resolution_tile(browser):
    from tests.test_offline_maps import make_map, tile
    with tempfile.TemporaryDirectory(prefix="ai-plant-map-512-") as temporary:
        directory=Path(temporary)
        maps=directory/"maps";maps.mkdir()
        make_map(maps/"current.mbtiles", overrides={"bounds":"90,0,180,66.5132604", "center":"135,40,2", "tile_size":"512"},
                 tiles=[(2,3,2,tile(size=512))])
        with running_app(directory,maps) as base:
            context,page,external,errors,responses=local_context(browser,base)
            try:
                page.goto(base+"/device",wait_until="networkidle")
                create_scan(page,base,40,135,"manual","512px geography check")
                page.reload(wait_until="networkidle")
                page.locator('[data-tab="map"]').click()
                assert_ready(page)
                assert page.evaluate("device.tiles.options.tileSize") == 256
                assert any('/2/3/1.png' in row['url'] and row['status']==200 for row in responses),responses
                assert not external,external
                assert not errors,errors
                return {"source_pixels":512,"logical_pixels":256,"correct_xyz_tile":True,"external_requests":external,"page_errors":errors}
            finally:context.close()


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps-dir", type=Path, default=ROOT / "maps")
    parser.add_argument("--expect-native-zoom",type=int)
    args = parser.parse_args()
    output = ROOT / "test-results"
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            report = {"ready_package": test_package(browser, args.maps_dir.resolve(), output, args.expect_native_zoom),
                      "tile_failure_retry": test_tile_failure_retry(browser, args.maps_dir.resolve(), output),
                      "missing_package": test_missing_package(browser, output),
                      "high_resolution_raster":test_high_resolution_tile(browser)}
        finally:
            browser.close()
    (output / "offline-map-browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: cold offline map assets/tiles, no external network, saved points/history/filter, escaped popup, coverage/zoom/pan, grid toggled three times, local tile503/retry to real tile200, missing/outside coverage, desktop/Pi/mobile; temporary data only")


if __name__ == "__main__":
    main()
