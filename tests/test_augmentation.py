import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import joblib
import numpy as np
from PIL import Image
from sklearn.ensemble import ExtraTreesClassifier

from leaflab.augmentation import STRESS_PROFILES, stress_variant, training_variants
from leaflab.dataset import ROOT
from leaflab.model import train_model
from leaflab.ensemble import select_blend


class AugmentationTests(unittest.TestCase):
    def image(self):
        rng = np.random.default_rng(13)
        return Image.fromarray(rng.integers(20, 230, (96, 128, 3), dtype=np.uint8))

    def test_variants_reproducible_without_changing_source(self):
        original = self.image()
        before = original.tobytes()
        first = list(training_variants(original, "leaf-a"))
        again = list(training_variants(original, "leaf-a"))
        self.assertEqual(len(first), 3)
        self.assertEqual(original.tobytes(), before)
        self.assertEqual(first[0].tobytes(), before)
        for a, b in zip(first, again):
            self.assertEqual(a.tobytes(), b.tobytes())
        self.assertNotEqual(first[1].tobytes(), list(training_variants(original, "leaf-b"))[1].tobytes())
        self.assertTrue(all(v.mode == "RGB" for v in first))

    def test_stress_profiles_preserve_source_and_have_separate_seeds(self):
        original = self.image()
        before = original.tobytes()
        training = {v.tobytes() for v in training_variants(original, "leaf-a")}
        for profile in STRESS_PROFILES:
            variant = stress_variant(original, "leaf-a", profile)
            self.assertEqual(variant.size, (192, 192))
            self.assertNotIn(variant.tobytes(), training)
            self.assertEqual(variant.tobytes(), stress_variant(original, "leaf-a", profile).tobytes())
        self.assertEqual(original.tobytes(), before)

    def test_training_augments_only_train_leaves_and_saves_original_manifest(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            records = []
            for label in ("Apple___healthy", "Tomato___healthy"):
                for split in ("train", "test"):
                    name = f"{label}-{split}.png"
                    self.image().save(root / name)
                    records.append({"path": name, "label": label, "group": name, "split": split})
            class TinyCatalog:
                directory = root
                files = {"Apple___healthy": [], "Tomato___healthy": []}
                def split(self, size):
                    return ([r for r in records if r["split"] == "train"],
                            [r for r in records if r["split"] == "test"], {})
            def small_forest(**kwargs):
                return ExtraTreesClassifier(n_estimators=3, max_depth=3, n_jobs=1, random_state=42)
            (root / "model").mkdir()
            (root / "model" / "classifier.joblib").write_bytes(b"corrupt model to recover")
            # Run real extraction, training, stress evaluation, serialization and reload.
            with patch("leaflab.model.ExtraTreesClassifier", side_effect=small_forest):
                artifact = train_model(TinyCatalog(), root / "model", 150)
            self.assertEqual(artifact["metadata"]["train_images"], 2)
            self.assertEqual(artifact["metadata"]["training_samples"], 6)
            self.assertEqual(artifact["metadata"]["test_images"], 2)
            self.assertEqual(artifact["metadata"]["camera_stress"]["transformed_images"], 6)
            self.assertFalse(artifact["metadata"]["camera_stress"]["real_camera_validation"])
            self.assertEqual(artifact["manifest"], sorted(records, key=lambda r: r["split"] == "test"))
            saved = joblib.load(root / "model" / "classifier.joblib")
            self.assertEqual(saved["manifest"], artifact["manifest"])
            self.assertFalse((root / "model" / "training.lock").exists())

    def test_blend_selection_never_reads_excluded_train_or_test_leaves(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            excluded = [{"path": "must-not-read-train.png", "label": "Apple___healthy", "group": "used-train"},
                        {"path": "must-not-read-test.png", "label": "Tomato___healthy", "group": "used-test"}]
            unused = []
            for index, label in enumerate(("Apple___healthy", "Tomato___healthy")):
                self.image().save(root / f"unused-{index}.png")
                unused.append({"path": f"unused-{index}.png", "label": label, "group": f"unused-{index}"})
            catalog = SimpleNamespace(directory=root, split=lambda size: (excluded+unused, [], {}))
            classifier = SimpleNamespace(classes_=np.asarray(["Apple___healthy", "Tomato___healthy"]),
                                         predict_proba=lambda x: np.tile([.8, .2], (len(x), 1)))
            blend, metadata, records = select_blend(catalog, classifier, classifier, excluded)
            self.assertEqual({r["group"] for r in records}, {"unused-0", "unused-1"})
            self.assertEqual(metadata["source_images"], 2)
            self.assertEqual(metadata["augmented_weight"], 0)  # Prefer the simpler model on a tie.
            self.assertTrue(np.allclose(blend.predict_proba([[0]]).sum(axis=1), 1))


if __name__ == "__main__":
    unittest.main()
