"""Real temporary MBTiles packages, isolated from all survey/model data."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from flask import Flask
from PIL import Image

from leaflab.offline_maps import OfflineMapError, import_mbtiles, inspect_mbtiles, offline_map_routes
from offline_map_tools import main


def tile(color="forestgreen", *, image_format="PNG", size=256):
    output = io.BytesIO()
    Image.new("RGB", (size, size), color).save(output, format=image_format)
    return output.getvalue()


def make_map(path, *, overrides=None, tiles=None, indexed=True):
    metadata = dict(name="Rice field test", format="png", bounds="103.5,16.0,103.8,16.2",
                    minzoom="2", maxzoom="2", center="103.65,16.1,2", scheme="tms", tile_size="256",
                    attribution='<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>')
    metadata.update(overrides or {})
    tiles = tiles if tiles is not None else [(2, 1, 0, tile("red")), (2, 1, 3, tile("blue"))]
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE metadata(name TEXT, value TEXT)")
        connection.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)")
        if indexed:
            connection.execute("CREATE UNIQUE INDEX tile_index ON tiles(zoom_level,tile_column,tile_row)")
        connection.executemany("INSERT INTO metadata VALUES (?,?)", list(metadata.items()))
        connection.executemany("INSERT INTO tiles VALUES (?,?,?,?)", tiles)
    return path


class OfflineMapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.maps = self.directory / "maps"
        self.maps.mkdir()
        self.path = self.maps / "current.mbtiles"
        app = Flask(__name__)
        app.register_blueprint(offline_map_routes(self.maps))
        self.client = app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_status_creates_no_database_or_directory(self):
        response = self.client.get("/api/device/map/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["code"], "missing")
        self.assertFalse(response.json["ready"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(list(self.maps.iterdir()), [])

    def test_status_has_safe_metadata_and_stable_version(self):
        make_map(self.path)
        first = self.client.get("/api/device/map/status").json
        self.assertTrue(first["ready"])
        self.assertEqual(first["bounds"], [103.5, 16.0, 103.8, 16.2])
        self.assertEqual(first["attribution"], {"text": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"})
        self.assertEqual(first["version"], self.client.get("/api/device/map/status").json["version"])
        self.assertNotIn(str(self.directory), json.dumps(first))

    def test_xyz_request_is_flipped_to_tms_and_payload_is_unchanged(self):
        red, blue = tile("red"), tile("blue")
        make_map(self.path, tiles=[(2, 1, 0, red), (2, 1, 3, blue)])
        upper = self.client.get("/api/device/map/tiles/2/1/0.png")
        lower = self.client.get("/api/device/map/tiles/2/1/3.png")
        self.assertEqual(upper.data, blue)
        self.assertEqual(lower.data, red)
        self.assertEqual(upper.mimetype, "image/png")
        self.assertEqual(upper.headers["X-Content-Type-Options"], "nosniff")
        cached = self.client.get("/api/device/map/tiles/2/1/0.png", headers={"If-None-Match": upper.headers["ETag"]})
        self.assertEqual(cached.status_code, 304)

    def test_jpeg_raster_has_actual_mime_even_with_png_route(self):
        payload = tile(image_format="JPEG")
        make_map(self.path, overrides={"format": "jpeg"}, tiles=[(2, 1, 3, payload)])
        response = self.client.get("/api/device/map/tiles/2/1/0.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, payload)

    def test_invalid_coordinates_missing_tiles_and_path_traversal_are_404(self):
        make_map(self.path)
        for suffix in ("2/1/2.png", "2/4/0.png", "2/0/4.png", "21/0/0.png", "1/0/0.png", "-1/0/0.png", "../../scans.sqlite3", "2/1/3.png/extra"):
            with self.subTest(suffix=suffix):
                response = self.client.get("/api/device/map/tiles/" + suffix)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Location", response.headers)

    def test_invalid_map_has_path_free_error_and_no_tile(self):
        self.path.write_text("not sqlite", encoding="utf-8")
        status = self.client.get("/api/device/map/status").json
        self.assertEqual(status["code"], "invalid")
        self.assertNotIn(str(self.directory), status["error"])
        self.assertEqual(self.client.get("/api/device/map/tiles/2/1/0.png").status_code, 404)

    def test_read_requests_never_change_database_or_create_sidecars(self):
        make_map(self.path)
        before, timestamp = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.client.get("/api/device/map/status")
        self.client.get("/api/device/map/tiles/2/1/0.png")
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(timestamp, self.path.stat().st_mtime_ns)
        self.assertEqual(list(self.maps.iterdir()), [self.path])

    def test_replacement_refreshes_cached_metadata_and_etag(self):
        make_map(self.path)
        before = self.client.get("/api/device/map/status").json
        old_etag = self.client.get("/api/device/map/tiles/2/1/0.png").headers["ETag"]
        incoming = make_map(self.directory / "incoming.mbtiles", overrides={"name": "Replacement"})
        import_mbtiles(incoming, self.path, replace=True)
        after = self.client.get("/api/device/map/status").json
        self.assertEqual(after["name"], "Replacement")
        self.assertNotEqual(before["version"], after["version"])
        self.assertEqual(self.client.get("/api/device/map/tiles/2/1/0.png", headers={"If-None-Match": old_etag}).status_code, 200)

    def test_rejects_vector_xyz_invalid_bounds_zoom_and_center(self):
        cases = [{"format": "pbf"}, {"format": "webp"}, {"scheme": "xyz"},
                 {"bounds": "nan,16,104,17"}, {"bounds": "103,16,104,86"}, {"bounds": "104,16,103,17"},
                 {"minzoom": "3", "maxzoom": "2"}, {"maxzoom": "21"}, {"center": "0,0,2"},
                 {"minzoom": "2.0"}, {"tile_size": "512"}]
        for index, override in enumerate(cases):
            with self.subTest(override=override):
                path = make_map(self.directory / f"invalid{index}.mbtiles", overrides=override)
                with self.assertRaises(OfflineMapError):
                    inspect_mbtiles(path, validate_tiles=True)

    def test_rejects_unindexed_lookup(self):
        make_map(self.path, indexed=False)
        with self.assertRaisesRegex(OfflineMapError, "index"):
            inspect_mbtiles(self.path)

    def test_full_validation_checks_every_tile(self):
        make_map(self.path, tiles=[(2, 1, 0, tile()), (2, 1, 3, b"not a real raster image")])
        self.assertTrue(inspect_mbtiles(self.path)["ready"])
        with self.assertRaises(OfflineMapError):
            inspect_mbtiles(self.path, validate_tiles=True)
        self.assertEqual(self.client.get("/api/device/map/tiles/2/1/0.png").status_code, 404)

    def test_full_validation_rejects_coordinate_types_ranges_and_dimension_mismatch(self):
        cases = [(2, 4, 0, tile()), (2, 1, -1, tile()), (2, 1, 3, tile(size=64)),
                 (2, 1, 3, tile(size=512)), (3, 1, 3, tile()), (2, 1, 3, tile(image_format="JPEG"))]
        for index, invalid in enumerate(cases):
            with self.subTest(tile=index):
                path = make_map(self.directory / f"badtile{index}.mbtiles", tiles=[(2, 1, 0, tile()), invalid])
                with self.assertRaises(OfflineMapError):
                    inspect_mbtiles(path, validate_tiles=True)

    def test_attribution_is_text_and_http_url_only(self):
        make_map(self.path, overrides={"attribution": '<script>alert(1)</script><img src=x onerror=bad>Map <a href="javascript:alert(1)">author</a>', "attribution_url": "file:///private"})
        attribution = inspect_mbtiles(self.path)["attribution"]
        self.assertEqual(attribution, {"text": "Map  author", "url": None})
        self.assertNotIn("<", attribution["text"])

    def test_metadata_duplicate_keys_rejected(self):
        make_map(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO metadata VALUES ('name','other')")
        with self.assertRaises(OfflineMapError):
            inspect_mbtiles(self.path)

    def test_import_validates_before_replacing_and_keeps_source_unchanged(self):
        make_map(self.path)
        before = self.path.read_bytes()
        invalid = make_map(self.directory / "broken.mbtiles", overrides={"format": "pbf"})
        with self.assertRaises(OfflineMapError):
            import_mbtiles(invalid, self.path, replace=True)
        self.assertEqual(self.path.read_bytes(), before)
        source = make_map(self.directory / "source.mbtiles", overrides={"name": "New map"})
        source_bytes = source.read_bytes()
        result = import_mbtiles(source, self.path, replace=True)
        self.assertEqual(result["tile_count"], 2)
        self.assertEqual(self.path.read_bytes(), source_bytes)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertFalse(list(self.maps.glob(".offline-map-*")))

    def test_import_no_clobber_same_path_and_wrong_extension_rejected(self):
        source = make_map(self.directory / "source.mbtiles")
        import_mbtiles(source, self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(OfflineMapError, "already exists"):
            import_mbtiles(source, self.path)
        with self.assertRaisesRegex(OfflineMapError, "different"):
            import_mbtiles(source, source, replace=True)
        with self.assertRaisesRegex(OfflineMapError, "extension"):
            import_mbtiles(source, self.directory / "scans.sqlite3", replace=True)
        self.assertEqual(before, self.path.read_bytes())

    def test_incomplete_wal_package_is_not_imported(self):
        source = make_map(self.directory / "source.mbtiles")
        Path(str(source) + "-wal").write_bytes(b"pending")
        with self.assertRaisesRegex(OfflineMapError, "self-contained"):
            import_mbtiles(source, self.path)
        self.assertFalse(self.path.exists())

    def test_failed_atomic_copy_keeps_existing_map(self):
        make_map(self.path)
        source = make_map(self.directory / "source.mbtiles")
        before = self.path.read_bytes()
        with mock.patch("leaflab.offline_maps.shutil.copyfileobj", side_effect=OSError("no space")):
            with self.assertRaises(OfflineMapError):
                import_mbtiles(source, self.path, replace=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(self.maps.glob(".offline-map-*")))

    def test_cli_inspect_import_and_error_use_no_network(self):
        source = make_map(self.directory / "source.mbtiles")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["inspect", str(source)]), 0)
        self.assertEqual(json.loads(output.getvalue())["tile_count"], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["import", str(source), "--output", str(self.path)]), 0)
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["import", str(source), "--output", str(self.path)]), 2)
        self.assertIn("already exists", output.getvalue())

    def test_windows_legacy_console_encoding_handles_thai_map_name(self):
        source = make_map(self.directory / "thai.mbtiles", overrides={"name": "แผนที่ทดสอบ"})
        command = Path(__file__).resolve().parents[1] / "offline_map_tools.py"
        environment = dict(os.environ, PYTHONIOENCODING="cp1252")
        result = subprocess.run([sys.executable, "-B", str(command), "inspect", str(source)],
                                capture_output=True, env=environment, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["name"], "แผนที่ทดสอบ")


if __name__ == "__main__":
    unittest.main()
