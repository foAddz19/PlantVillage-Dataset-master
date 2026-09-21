from __future__ import annotations

import json
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.metrics import accuracy_score, balanced_accuracy_score, classification_report

from .dataset import ROOT, Catalog, label_info

MODEL_DIR = ROOT / "models"
FEATURE_VERSION = 1
Image.MAX_IMAGE_PIXELS = 25_000_000


def read_image(source) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("รองรับภาพ JPG, PNG และ WebP เท่านั้น")
                if min(original.size) < 32:
                    raise ValueError("ภาพเล็กเกินไป กรุณาใช้ภาพอย่างน้อย 32 × 32 พิกเซล")
                original.load()
                image = ImageOps.exif_transpose(original)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba)
                return image.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("อ่านภาพไม่ได้ หรือภาพมีขนาดเกิน 25 ล้านพิกเซล") from exc


def features(image: Image.Image) -> np.ndarray:
    small = ImageOps.fit(image, (96, 96), method=Image.Resampling.BILINEAR)
    rgb = np.asarray(small, dtype=np.float32) / 255.
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32) / 255.
    out = []
    for matrix in (rgb, hsv):
        for region in (matrix, matrix[16:80, 16:80]):
            for channel in range(3):
                histogram, _ = np.histogram(region[:, :, channel], bins=16, range=(0, 1))
                out.extend(histogram / region.shape[0] / region.shape[1])
        cells = matrix.reshape(4, 24, 4, 24, 3).transpose(0, 2, 1, 3, 4)
        out.extend(cells.mean(axis=(2, 3)).ravel())
        out.extend(cells.std(axis=(2, 3)).ravel())
    gray = rgb @ np.array([.299, .587, .114], dtype=np.float32)
    gy, gx = np.gradient(gray)
    magnitude = np.hypot(gx, gy)
    angle = np.mod(np.arctan2(gy, gx), np.pi)
    bins = np.minimum((angle * 9 / np.pi).astype(int), 8)
    for y in range(0, 96, 24):
        for x in range(0, 96, 24):
            hist = np.bincount(bins[y:y+24, x:x+24].ravel(),
                               weights=magnitude[y:y+24, x:x+24].ravel(), minlength=9)
            out.extend(hist / (np.linalg.norm(hist) + 1e-6))
    center = gray[1:-1, 1:-1]
    lbp = np.zeros_like(center, dtype=np.uint8)
    for bit, (dy, dx) in enumerate(((-1,-1),(-1,0),(-1,1),(0,1),(1,1),(1,0),(1,-1),(0,-1))):
        lbp |= ((gray[1+dy:95+dy, 1+dx:95+dx] >= center).astype(np.uint8) << bit)
    out.extend(np.bincount(lbp.ravel(), minlength=256) / lbp.size)
    out.extend(np.asarray(small.resize((12, 12)), dtype=np.float32).ravel() / 255.)
    return np.asarray(out, dtype=np.float32)


def train_model(catalog: Catalog, model_dir=MODEL_DIR, max_per_class=300, progress=None):
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    lock_path = model_dir / "training.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("มีการฝึกโมเดลอยู่แล้ว หากโปรแกรมเคยปิดผิดปกติ ให้ปิดโปรแกรมแล้วลบ models/training.lock") from exc
    os.close(descriptor)
    started = time.time()
    def report(stage, percent, message):
        if progress:
            progress({"stage": stage, "percent": percent, "message": message})
    try:
        report("splitting", 1, "กำลังแบ่งข้อมูลตามกลุ่มใบ")
        train, test, split_info = catalog.split(max_per_class)
        if not train or not test:
            raise ValueError("ไม่พบข้อมูลภาพสำหรับฝึกโมเดล")
        train_groups = {r["group"] for r in train}
        test_groups = {r["group"] for r in test}
        if train_groups & test_groups:
            raise ValueError("ตรวจพบกลุ่มใบซ้ำระหว่างชุดฝึกและชุดทดสอบ")
        records = train + test
        vectors, valid_records, skipped = [], [], []
        def extract(record):
            try:
                return features(read_image(catalog.directory / record["path"])), record, None
            except ValueError as exc:
                return None, record, str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for index, (vector, record, error) in enumerate(pool.map(extract, records)):
                if error:
                    skipped.append({"path": record["path"], "error": error})
                else:
                    vectors.append(vector)
                    valid_records.append({**record, "split": "train" if index < len(train) else "test"})
                if index % 100 == 0 or index == len(records) - 1:
                    report("features", 5 + int(65 * (index+1)/len(records)),
                           f"เตรียมภาพ {index+1:,} / {len(records):,}")
        X = np.asarray(vectors, dtype=np.float32)
        y = np.asarray([r["label"] for r in valid_records])
        training_mask = np.array([r["split"] == "train" for r in valid_records])
        expected = set(catalog.files)
        if set(y[training_mask]) != expected or set(y[~training_mask]) != expected:
            raise ValueError("ภาพที่อ่านได้ไม่ครอบคลุมทุกกลุ่มในชุดฝึกและชุดทดสอบ")
        report("fitting", 73, "กำลังฝึกโมเดลจำแนกภาพ")
        classifier = ExtraTreesClassifier(n_estimators=180, max_depth=28, min_samples_leaf=2,
                                          class_weight="balanced", n_jobs=4, random_state=42)
        classifier.fit(X[training_mask], y[training_mask])
        report("evaluating", 92, "กำลังวัดผลกับภาพที่ไม่ได้ใช้ฝึก")
        predicted = classifier.predict(X[~training_mask])
        metrics = classification_report(y[~training_mask], predicted, output_dict=True, zero_division=0)
        probabilities = classifier.predict_proba(X[~training_mask])
        top3 = classifier.classes_[np.argsort(probabilities, axis=1)[:, -3:]]
        metadata = {
            "feature_version": FEATURE_VERSION, "algorithm": "Extra Trees · สีและลักษณะพื้นผิว",
            "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time()-started, 1),
            "train_images": int(training_mask.sum()), "test_images": int((~training_mask).sum()),
            "classes": len(classifier.classes_), "accuracy": float(accuracy_score(y[~training_mask], predicted)),
            "balanced_accuracy": float(balanced_accuracy_score(y[~training_mask], predicted)),
            "top3_accuracy": float(np.mean(np.any(top3 == y[~training_mask, None], axis=1))),
            "group_overlap": 0, "max_per_class": max_per_class, "split_info": split_info,
            "report": metrics, "skipped_images": len(skipped), "seed": 42,
        }
        artifact = {"model": classifier, "metadata": metadata, "manifest": valid_records, "skipped": skipped}
        temporary = model_dir / "classifier.tmp"
        joblib.dump(artifact, temporary, compress=3)
        os.replace(temporary, model_dir / "classifier.joblib")
        (model_dir / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (model_dir / "split_manifest.json").write_text(json.dumps(valid_records, ensure_ascii=False), encoding="utf-8")
        report("ready", 100, "โมเดลพร้อมใช้งาน")
        return artifact
    finally:
        lock_path.unlink(missing_ok=True)


class ModelService:
    def __init__(self, catalog, model_dir=MODEL_DIR):
        self.catalog = catalog
        self.model_dir = Path(model_dir)
        self.lock = threading.Lock()
        self.artifact = None
        self.state = {"running": False, "stage": "idle", "percent": 0, "message": "ยังไม่มีโมเดล"}
        path = self.model_dir / "classifier.joblib"
        if path.exists():
            try:
                loaded = joblib.load(path)
                if loaded["metadata"]["feature_version"] != FEATURE_VERSION:
                    raise ValueError("feature version mismatch")
                self.artifact = loaded
                self.state.update(stage="ready", percent=100, message="โมเดลพร้อมใช้งาน")
            except Exception:
                self.state.update(stage="error", message="โหลดโมเดลเดิมไม่ได้ กรุณาฝึกโมเดลใหม่")

    def status(self):
        with self.lock:
            return {**self.state, "ready": self.artifact is not None,
                    "metadata": self.artifact["metadata"] if self.artifact else None}

    def start_training(self, max_per_class=300):
        with self.lock:
            if self.state["running"]:
                raise ValueError("กำลังฝึกโมเดลอยู่แล้ว")
            self.state = {"running": True, "stage": "starting", "percent": 0, "message": "เริ่มฝึกโมเดล"}
        def update(state):
            with self.lock:
                self.state.update(state)
        def run():
            try:
                artifact = train_model(self.catalog, self.model_dir, max_per_class, update)
                with self.lock:
                    self.artifact = artifact
            except Exception as exc:
                import logging
                logging.exception("Training failed")
                update({"stage": "error", "message": str(exc) if isinstance(exc, ValueError)
                        else "ฝึกโมเดลไม่สำเร็จ กรุณาตรวจพื้นที่ว่างและดูรายละเอียดในหน้าต่างโปรแกรม"})
            finally:
                update({"running": False})
        threading.Thread(target=run, daemon=True, name="model-training").start()

    def predict(self, image):
        with self.lock:
            artifact = self.artifact
        if artifact is None:
            raise LookupError("กรุณาฝึกโมเดลก่อนเริ่มวิเคราะห์ภาพ")
        scores = artifact["model"].predict_proba(features(image)[None, :])[0]
        indices = np.argsort(scores)[-3:][::-1]
        candidates = [{**label_info(str(artifact["model"].classes_[i])), "score": round(float(scores[i]), 5)}
                      for i in indices]
        uncertain = float(scores[indices[0]]) < .55 or float(scores[indices[0]]-scores[indices[1]]) < .15
        return {"candidates": candidates, "uncertain": bool(uncertain),
                "model_created_at": artifact["metadata"]["created_at"],
                "note": "คะแนนโมเดลไม่ใช่โอกาสยืนยันโรค โมเดลจะเลือกจาก 38 กลุ่มที่รู้จัก แม้ภาพจะไม่ใช่ใบพืช"}
