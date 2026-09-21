"""Combine a clean-image model and an augmented model using separate validation leaves."""
import hashlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .augmentation import STRESS_PROFILES, camera_variant, image_rng


class BlendedClassifier:
    def __init__(self, clean_model, augmented_model, augmented_weight):
        if not np.array_equal(clean_model.classes_, augmented_model.classes_):
            raise ValueError("Cannot blend models with different class ordering")
        if not 0 <= augmented_weight <= 1:
            raise ValueError("Invalid model weight")
        self.clean_model = clean_model
        self.augmented_model = augmented_model
        self.augmented_weight = float(augmented_weight)
        self.classes_ = clean_model.classes_

    def predict_proba(self, vectors):
        clean = self.clean_model.predict_proba(vectors)
        if self.augmented_weight == 0:
            return clean
        augmented = self.augmented_model.predict_proba(vectors)
        return (1-self.augmented_weight)*clean + self.augmented_weight*augmented


def select_blend(catalog, clean_model, augmented_model, excluded_records, progress=None):
    # Import here because the feature extractor is also used by the trainer.
    from .model import features, read_image

    excluded = {r["group"] for r in excluded_records}
    # Catalog.split uses a stable group partition regardless of the sampling cap.
    # Only unused TRAIN-partition groups can calibrate; test groups stay untouched.
    pool, _, _ = catalog.split(100_000)
    records, counts, seen = [], {}, set()
    for record in sorted(pool, key=lambda r: hashlib.sha256(("blend-validation:"+r["path"]).encode()).digest()):
        label, group = record["label"], record["group"]
        if group in excluded or group in seen or counts.get(label, 0) >= 40:
            continue
        records.append(record)
        seen.add(group)
        counts[label] = counts.get(label, 0)+1
    if not records:
        return BlendedClassifier(clean_model, augmented_model, 0), {"reason": "no_unused_validation_groups", "augmented_weight": 0}, []

    labels = np.asarray([r["label"] for r in records])
    probabilities = {}
    for profile in ("clean", *STRESS_PROFILES):
        def extract(record):
            image = read_image(catalog.directory / record["path"])
            if profile != "clean":
                image = camera_variant(image, image_rng(record["path"], profile, "blend-validation"), profile)
            return features(image)
        with ThreadPoolExecutor(max_workers=2) as executor:
            vectors = np.asarray(list(executor.map(extract, records)), dtype=np.float32)
        probabilities[profile] = (clean_model.predict_proba(vectors), augmented_model.predict_proba(vectors))
        if progress:
            progress(profile)

    trials = []
    for weight in (0., .2, .35, .5, .65, .8, 1.):
        accuracies = {}
        for profile, (clean, augmented) in probabilities.items():
            scores = (1-weight)*clean + weight*augmented
            predicted = clean_model.classes_[scores.argmax(axis=1)]
            accuracies[profile] = float(np.mean(predicted == labels))
            if profile == "clean":
                balanced = float(np.mean([np.mean(predicted[labels == label] == label) for label in np.unique(labels)]))
                top3 = float(np.mean(np.any(clean_model.classes_[np.argsort(scores, axis=1)[:, -3:]] == labels[:, None], axis=1)))
        trials.append({"augmented_weight": weight, "clean_accuracy": accuracies["clean"],
                       "clean_balanced_accuracy": balanced, "clean_top3_accuracy": top3,
                       "stress_accuracy": float(np.mean([accuracies[p] for p in STRESS_PROFILES])),
                       "profiles": accuracies})
    baseline = trials[0]
    eligible = [trial for trial in trials if trial["clean_accuracy"] >= baseline["clean_accuracy"]-.005
                and trial["clean_balanced_accuracy"] >= baseline["clean_balanced_accuracy"]-.01
                and trial["clean_top3_accuracy"] >= baseline["clean_top3_accuracy"]-.005]
    chosen = max(eligible, key=lambda trial: (trial["stress_accuracy"], -trial["augmented_weight"]))
    metadata = {"augmented_weight": chosen["augmented_weight"], "source_images": len(records),
                "class_counts": counts, "group_overlap": 0, "trials": trials,
                "selection": "maximize simulated-camera accuracy; clean accuracy/top3 loss <= 0.5 percentage points on separate validation groups",
                "real_camera_validation": False}
    return BlendedClassifier(clean_model, augmented_model, chosen["augmented_weight"]), metadata, records
