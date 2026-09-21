import io
import json
import re
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from werkzeug.test import EnvironBuilder

import numpy as np
from PIL import Image

from app import create_app
from leaflab.dataset import Catalog, ROOT, image_key
from leaflab.model import ModelService, features, read_image, train_model


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.service = ModelService(cls.catalog)
        cls.app = create_app(cls.catalog, cls.service)
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        html = cls.client.get("/").get_data(as_text=True)
        cls.token = re.search(r'name="app-token" content="([^"]+)"', html).group(1)
        cls.headers = {"X-App-Token": cls.token}

    def test_catalog_counts_and_static_assets(self):
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["images"], 54305)
        self.assertEqual(response.json["class_count"], 38)
        self.assertEqual(response.headers["Permissions-Policy"], "camera=(self), microphone=()")
        self.assertIn("media-src 'self' blob:", response.headers["Content-Security-Policy"])
        for asset in ("/static/app.js", "/static/app.css", "/static/camera.js", "/static/camera.css", "/static/leaf.svg"):
            with self.subTest(asset=asset):
                response = self.client.get(asset)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_grouped_split_is_disjoint_reproducible_and_capped(self):
        train, test, _ = self.catalog.split(150)
        self.assertFalse({r["group"] for r in train} & {r["group"] for r in test})
        self.assertFalse({r["path"] for r in train} & {r["path"] for r in test})
        self.assertEqual({r["label"] for r in train}, set(self.catalog.files))
        self.assertEqual({r["label"] for r in test}, set(self.catalog.files))
        self.assertEqual((train, test), self.catalog.split(150)[:2])
        # Training size choices keep the same group partition, preventing test-to-train leakage.
        larger_train, larger_test, _ = self.catalog.split(300)
        self.assertFalse({r["group"] for r in larger_train} & {r["group"] for r in test})
        self.assertFalse({r["group"] for r in train} & {r["group"] for r in larger_test})

    def test_normalizes_copy_and_masked_image_names(self):
        self.assertEqual(image_key("uuid___RS_HL 1234.JPG"), "rs_hl 1234")
        self.assertEqual(image_key("uuid___RS_HL 1234 copy 2_final_masked.jpg"), "rs_hl 1234")

    def test_saved_training_manifest_and_metrics(self):
        self.assertIsNotNone(self.service.artifact, "Run train.py first for integration tests")
        artifact = self.service.artifact
        manifest = artifact["manifest"]
        train = [row for row in manifest if row["split"] == "train"]
        test = [row for row in manifest if row["split"] == "test"]
        self.assertFalse({r["group"] for r in train} & {r["group"] for r in test})
        self.assertEqual(len(train), artifact["metadata"]["train_images"])
        self.assertEqual(len(test), artifact["metadata"]["test_images"])
        self.assertGreater(artifact["metadata"]["accuracy"], .5)
        validation = artifact.get("blend_validation_manifest", [])
        if validation:
            groups = {r["group"] for r in validation}
            self.assertFalse(groups & {r["group"] for r in train + test})
            self.assertEqual(len(groups), len(validation))
            self.assertEqual(len(validation), artifact["metadata"]["blend_validation"]["source_images"])

    def test_upload_predict_uses_image_pixels_not_filename(self):
        record = next(r for r in self.service.artifact["manifest"] if r["split"] == "test")
        payload = (self.catalog.directory / record["path"]).read_bytes()
        predictions = []
        for name in ("leaf.jpg", "Tomato___healthy.jpg"):
            response = self.client.post("/api/predict", headers=self.headers,
                                        data={"image": (io.BytesIO(payload), name)})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(len(response.json["candidates"]), 3)
            self.assertTrue(all(0 <= c["score"] <= 1 for c in response.json["candidates"]))
            predictions.append(response.json["candidates"])
        self.assertEqual(predictions[0], predictions[1])

    def test_rejects_corrupt_missing_and_oversized_uploads(self):
        self.assertEqual(self.client.post("/api/predict", headers=self.headers).status_code, 400)
        response = self.client.post("/api/predict", headers=self.headers,
                                    data={"image": (io.BytesIO(b"not an image"), "fake.jpg")})
        self.assertEqual(response.status_code, 400)
        with closing(EnvironBuilder(path="/api/predict", method="POST", headers=self.headers,
                                   data={"image": (io.BytesIO(b"x" * (8*1024*1024+1)), "big.jpg")})) as builder:
            environ = builder.get_environ()
            with closing(environ["wsgi.input"]):
                response = self.client.open(environ)
                self.assertEqual(response.status_code, 413)
                response.close()

    def test_image_limits_and_features(self):
        with self.assertRaises(ValueError):
            payload = io.BytesIO(); Image.new("RGB", (8, 8)).save(payload, "PNG"); payload.seek(0)
            read_image(payload)
        with self.assertRaises(ValueError):
            payload = io.BytesIO(); Image.new("RGB", (64, 64)).save(payload, "GIF"); payload.seek(0)
            read_image(payload)
        gray = Image.new("L", (64, 80), 90)
        rgba = Image.new("RGBA", (80, 64), (40, 120, 30, 128))
        for image in (gray, rgba):
            payload = io.BytesIO(); image.save(payload, "PNG"); payload.seek(0)
            decoded = read_image(payload)
            vector = features(decoded)
            self.assertEqual(decoded.mode, "RGB")
            self.assertEqual(vector.shape, (1216,))
            self.assertTrue(np.isfinite(vector).all())

    def test_example_membership_and_invalid_paths(self):
        data = self.client.get("/api/examples").json
        self.assertEqual(len(data["items"]), 12)
        image = self.client.get(data["items"][0]["url"])
        self.assertEqual(image.mimetype, "image/jpeg")
        image.close()
        row = data["items"][0]
        response = self.client.post("/api/predict-example", headers=self.headers,
                                    json={"label": row["label"], "index": row["index"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["actual_label"], row["label"])
        self.assertEqual(response.json["split"], row["split"])
        for label, index in (("../../app.py", 0), (row["label"], -1), (row["label"], "bad")):
            response = self.client.get("/api/image", query_string={"label": label, "index": index})
            self.assertEqual(response.status_code, 400)

    def test_local_post_protection_and_train_size(self):
        self.assertEqual(self.client.post("/api/train", json={"samples_per_class": 150}).status_code, 403)
        self.assertEqual(self.client.post("/api/train", headers=self.headers,
                                         json={"samples_per_class": 999999}).status_code, 400)
        with patch.object(self.service, "start_training") as mocked:
            response = self.client.post("/api/train", headers=self.headers, json={"samples_per_class": 150})
            self.assertEqual(response.status_code, 202)
            mocked.assert_called_once_with(150)

    def test_absent_and_corrupt_model_are_recoverable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            service = ModelService(self.catalog, temporary)
            self.assertFalse(service.status()["ready"])
            with self.assertRaises(LookupError):
                service.predict(Image.new("RGB", (64, 64)))
            (Path(temporary) / "classifier.joblib").write_bytes(b"broken")
            self.assertEqual(ModelService(self.catalog, temporary).status()["stage"], "error")

    def test_training_lock_and_failed_retrain_preserve_existing_model(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            lock = Path(temporary) / "training.lock"
            lock.write_text("locked")
            with self.assertRaises(ValueError):
                train_model(self.catalog, temporary, 150)
            self.assertTrue(lock.exists())
            lock.unlink()
            previous = b"existing model"
            artifact = Path(temporary) / "classifier.joblib"
            artifact.write_bytes(previous)
            with patch.object(self.catalog, "split", side_effect=ValueError("invalid split")):
                with self.assertRaises(ValueError):
                    train_model(self.catalog, temporary, 150)
            self.assertFalse(lock.exists())
            self.assertEqual(artifact.read_bytes(), previous)

    def test_background_training_keeps_old_model_and_prevents_parallel_runs(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            service = ModelService(self.catalog, temporary)
            previous = {"metadata": {"created_at": "previous"}}
            replacement = {"metadata": {"created_at": "replacement"}}
            service.artifact = previous
            started, release = threading.Event(), threading.Event()
            original_status = service.status
            def simulated_train(catalog, model_dir, size, progress):
                progress({"stage": "features", "percent": 25, "message": "processing"})
                started.set()
                release.wait(5)
                return replacement
            with patch("leaflab.model.train_model", side_effect=simulated_train):
                service.start_training(150)
                self.assertTrue(started.wait(5))
                self.assertTrue(original_status()["ready"])
                self.assertEqual(original_status()["percent"], 25)
                self.assertIs(service.artifact, previous)
                with self.assertRaises(ValueError):
                    service.start_training(150)
                # Join only this service's worker, without sleeps or polling.
                workers = [thread for thread in threading.enumerate() if thread.name == "model-training"]
                release.set()
                for thread in workers:
                    thread.join(5)
                self.assertIs(service.artifact, replacement)
                self.assertFalse(original_status()["running"])


if __name__ == "__main__":
    unittest.main()
