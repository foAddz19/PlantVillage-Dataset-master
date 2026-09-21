"""Blend already trained clean/augmented models; select weights on unused TRAIN leaves."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

from leaflab.dataset import Catalog
from leaflab.ensemble import select_blend
from leaflab.model import evaluate_camera_stress, features, prediction_metrics, read_image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean-model", type=Path, required=True)
    parser.add_argument("--augmented-model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    clean = joblib.load(args.clean_model)
    augmented = joblib.load(args.augmented_model)
    if clean["metadata"]["feature_version"] != augmented["metadata"]["feature_version"]:
        raise ValueError("Feature versions must match")
    if clean["manifest"] != augmented["manifest"]:
        raise ValueError("This comparison requires identical train/test manifests")
    catalog = Catalog()
    classifier, validation, records = select_blend(
        catalog, clean["model"], augmented["model"], augmented["manifest"],
        lambda profile: print(json.dumps({"stage": "validation", "profile": profile}), flush=True))
    print(json.dumps({"stage": "selected", "weight": validation["augmented_weight"],
                      "source_images": validation.get("source_images")}), flush=True)
    test = [r for r in augmented["manifest"] if r["split"] == "test"]
    vectors = np.asarray([features(read_image(catalog.directory / r["path"])) for r in test], dtype=np.float32)
    metrics = prediction_metrics(classifier, vectors, np.asarray([r["label"] for r in test]))
    stress = evaluate_camera_stress(catalog, test, {"blended": classifier},
        lambda profile: print(json.dumps({"stage": "test", "profile": profile}), flush=True))["blended"]
    metadata = {**augmented["metadata"], **metrics, "camera_stress": stress, "blend_validation": validation,
                "created_at": datetime.now(timezone.utc).isoformat(), "training_version": 3,
                "seconds": round(augmented["metadata"]["seconds"] + time.monotonic()-started, 1),
                "algorithm": "Extra Trees · ผสานภาพต้นฉบับและภาพจำลองกล้อง"}
    artifact = {**augmented, "model": classifier, "metadata": metadata, "blend_validation_manifest": records}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    temporary = args.output_dir / "classifier.tmp"
    joblib.dump(artifact, temporary, compress=3)
    os.replace(temporary, args.output_dir / "classifier.joblib")
    for name, value in (("metrics", metadata), ("split_manifest", artifact["manifest"]), ("blend_validation_manifest", records)):
        (args.output_dir / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"clean": metadata["accuracy"], "camera": stress["mean_accuracy"],
                      "weight": validation["augmented_weight"]}), flush=True)


if __name__ == "__main__":
    main()
