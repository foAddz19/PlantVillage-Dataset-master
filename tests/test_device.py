import io
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import create_app
from leaflab.dataset import Catalog, ROOT, label_info
from leaflab.scans import ScanStore


class StubService:
    def status(self):
        return {"ready": True, "running": False}

    def predict(self, image):
        return {"candidates": [{**label_info("Apple___healthy"), "score": .8}],
                "uncertain": False, "model_created_at": "test-model"}


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = Catalog(self.root)
        self.service = StubService()
        self.start_app()
        image = io.BytesIO()
        Image.new("RGB", (90, 64), (40, 110, 50)).save(image, "PNG")
        self.payload = image.getvalue()

    def start_app(self):
        self.app = create_app(self.catalog, self.service, data_dir=self.root / "data")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        page = self.client.get("/device")
        self.headers = {"X-App-Token": re.search(r'name="app-token" content="([^"]+)"', page.text).group(1)}

    def analyze(self, **overrides):
        data = {"image": (io.BytesIO(self.payload), "camera.png"), "source": "camera",
                "latitude": "16.053", "longitude": "103.652", "location_source": "simulated", **overrides}
        return self.client.post("/api/device/analyze", data=data, headers=self.headers)

    def test_capture_save_restart_and_exact_frame(self):
        response = self.analyze()
        self.assertEqual(response.status_code, 201)
        row = response.json
        self.assertIsNone(row["saved_at"])
        self.assertEqual(self.client.get("/api/device/scans").json["total"], 0)
        self.assertEqual(self.client.get(row["image_url"]).data, self.payload)
        saved = self.client.post(f"/api/device/scans/{row['id']}/save", headers=self.headers,
                                 json={"severity": "mild", "note": "แปลง A", "latitude": 0, "prediction": {"fake": True}}).json
        self.assertEqual(saved["latitude"], 16.053)
        self.assertEqual(saved["prediction"], row["prediction"])
        self.assertEqual(saved["severity"], "mild")
        # Repeated saves must neither duplicate nor silently overwrite the first saved record.
        self.client.post(f"/api/device/scans/{row['id']}/save", headers=self.headers, json={"note": "changed"})
        self.start_app()
        history = self.client.get("/api/device/scans").json
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["note"], "แปลง A")

    def test_validation_and_token(self):
        self.assertEqual(self.client.post("/api/device/analyze").status_code, 403)
        for fields in ({"latitude": "NaN"}, {"latitude": "91"}, {"longitude": "181"},
                       {"location_source": "gps"}, {"source": "fake"}, {"latitude": ""},
                       {"image": (io.BytesIO(b"broken"), "image.png")}):
            with self.subTest(fields=str(fields)):
                self.assertEqual(self.analyze(**fields).status_code, 400)
        row = self.analyze().json
        for fields in ({"severity": "invented"}, {"severity": []}, {"note": "x"*501}, {"note": []}):
            self.assertEqual(self.client.post(f"/api/device/scans/{row['id']}/save", headers=self.headers,json=fields).status_code,400)
        self.assertEqual(self.client.get("/api/device/scans/not-an-id").status_code, 409)

    def test_exports_only_saved_and_longitude_latitude_order(self):
        first = self.analyze().json
        self.analyze(latitude="0", longitude="0")  # Unsaved drafts must never appear in exports.
        self.client.post(f"/api/device/scans/{first['id']}/save", headers=self.headers,json={"note": "=1+1"})
        response = self.client.get("/api/device/export/geojson")
        data = response.json
        self.assertEqual(len(data["features"]), 1)
        feature = data["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [103.652, 16.053])
        self.assertEqual(feature["properties"]["location_source"], "simulated")
        csv = self.client.get("/api/device/export/csv")
        self.assertIn("'=1+1", csv.text)
        self.assertTrue(csv.data.startswith(b"\xef\xbb\xbf"))

    def test_offline_map_cache_headers_survive_application_response_policy(self):
        from tests.test_offline_maps import make_map
        maps = self.root / "maps"
        maps.mkdir()
        make_map(maps / "current.mbtiles")
        app = create_app(self.catalog, self.service, data_dir=self.root / "data", maps_dir=maps)
        client = app.test_client()
        status = client.get("/api/device/map/status")
        self.assertTrue(status.json["ready"])
        self.assertEqual(status.headers["Cache-Control"], "no-store")
        image = client.get("/api/device/map/tiles/2/1/0.png")
        self.assertEqual(image.status_code, 200)
        self.assertIn("must-revalidate", image.headers["Cache-Control"])
        cached = client.get("/api/device/map/tiles/2/1/0.png", headers={"If-None-Match": image.headers["ETag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.data, b"")
        self.assertEqual(client.get("/api/device/scans").headers["Cache-Control"], "no-store")

    def test_offline_map_never_allows_external_tile_images(self):
        for path in ("/device", "/"):
            page = self.client.get(path)
            self.assertNotIn("https://", page.headers["Content-Security-Policy"])
            self.assertIn("img-src 'self' blob: data:", page.headers["Content-Security-Policy"])
            self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        self.assertIn('id="offline-map"', self.client.get("/device").text)
        self.assertNotIn('id="online-map"', self.client.get("/device").text)


if __name__ == "__main__":
    unittest.main()
