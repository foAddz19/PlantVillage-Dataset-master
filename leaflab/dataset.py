from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "PlantVillage-Dataset-master"
COLOR_DIR = DATASET / "raw" / "color"
EXTENSIONS = {".jpg", ".jpeg", ".png"}
CROPS = {
    "Apple": "แอปเปิล", "Blueberry": "บลูเบอร์รี",
    "Cherry_(including_sour)": "เชอร์รี", "Corn_(maize)": "ข้าวโพด",
    "Grape": "องุ่น", "Orange": "ส้ม", "Peach": "พีช",
    "Pepper,_bell": "พริกหวาน", "Potato": "มันฝรั่ง",
    "Raspberry": "ราสป์เบอร์รี", "Soybean": "ถั่วเหลือง",
    "Squash": "สควอช", "Strawberry": "สตรอว์เบอร์รี", "Tomato": "มะเขือเทศ",
}
DISEASES = {
    "healthy": "ใบปกติ", "Apple_scab": "โรคใบตกสะเก็ด",
    "Black_rot": "โรคเน่าดำ", "Cedar_apple_rust": "โรคราสนิมแอปเปิล",
    "Powdery_mildew": "โรคราแป้ง", "Cercospora_leaf_spot Gray_leaf_spot": "โรคใบจุดสีเทา",
    "Common_rust_": "โรคราสนิม", "Northern_Leaf_Blight": "โรคใบไหม้แผลใหญ่",
    "Esca_(Black_Measles)": "โรคเอสกา", "Leaf_blight_(Isariopsis_Leaf_Spot)": "โรคใบไหม้",
    "Haunglongbing_(Citrus_greening)": "โรคกรีนนิง", "Bacterial_spot": "โรคใบจุดแบคทีเรีย",
    "Early_blight": "โรคใบไหม้ระยะแรก", "Late_blight": "โรคใบไหม้ระยะปลาย",
    "Leaf_scorch": "โรคใบจุดไหม้", "Leaf_Mold": "โรคราใบ",
    "Septoria_leaf_spot": "โรคใบจุดเซปโทเรีย",
    "Spider_mites Two-spotted_spider_mite": "ความเสียหายจากไรแดงสองจุด",
    "Target_Spot": "โรคใบจุดวง", "Tomato_Yellow_Leaf_Curl_Virus": "โรคใบหงิกเหลือง",
    "Tomato_mosaic_virus": "โรคใบด่าง",
}


def label_info(label: str) -> dict:
    crop, disease = label.split("___", 1)
    return {"label": label, "crop": crop, "crop_th": CROPS.get(crop, crop),
            "disease": disease, "disease_th": DISEASES.get(disease, disease.replace("_", " ")),
            "healthy": disease == "healthy"}


def image_key(name: str) -> str:
    key = name.replace("_final_masked", "").split("___")[-1].split("copy")[0]
    for extension in (".jpg", ".png", ".jpeg"):
        if key.lower().endswith(extension):
            key = key[:-len(extension)]
    return key.strip().lower()


class Catalog:
    def __init__(self, directory: Path = COLOR_DIR, leaf_map_path: Path | None = None):
        self.directory = directory.resolve()
        self.files = {}
        if directory.exists():
            for folder in sorted(directory.iterdir()):
                if folder.is_dir() and "___" in folder.name:
                    self.files[folder.name] = sorted(
                        p.name for p in folder.iterdir() if p.suffix.lower() in EXTENSIONS)
        self.leaf_map_path = leaf_map_path or DATASET / "leaf_grouping" / "leaf-map.json"

    def summary(self) -> dict:
        classes = [{**label_info(label), "count": len(files)} for label, files in self.files.items()]
        return {"images": sum(c["count"] for c in classes), "class_count": len(classes),
                "crop_count": len({c["crop"] for c in classes}), "classes": classes}

    def path(self, label: str, index: int) -> Path:
        if label not in self.files or index < 0 or index >= len(self.files[label]):
            raise ValueError("ไม่พบภาพที่เลือก")
        return self.directory / label / self.files[label][index]

    def split(self, max_per_class: int = 300, seed: int = 42):
        """Split known physical leaves BEFORE sampling; use conservative fallback name groups."""
        leaf_map = json.loads(self.leaf_map_path.read_text(encoding="utf-8"))
        train, test = [], []
        known, total = 0, 0
        for label, names in self.files.items():
            groups = defaultdict(list)
            for name in names:
                key = image_key(name)
                suggestions = leaf_map.get(key, [])
                matching = sorted(s for s in suggestions if s.startswith(label + ":::"))
                # Merge ambiguous leaf IDs within a class conservatively below.
                group = matching[0] if matching else f"fallback:{label}:{key}"
                groups[group].append({"path": f"{label}/{name}", "label": label, "group": group})
                known += bool(matching)
                total += 1
            # Union ambiguous mapped IDs so no suggested physical leaf can cross the split.
            parent = {}
            def find(value):
                parent.setdefault(value, value)
                while parent[value] != value:
                    parent[value] = parent[parent[value]]
                    value = parent[value]
                return value
            for name in names:
                ids = sorted(s for s in leaf_map.get(image_key(name), []) if s.startswith(label + ":::"))
                for value in ids[1:]:
                    parent[find(value)] = find(ids[0])
            merged = defaultdict(list)
            for group, records in groups.items():
                canonical = find(group)
                for record in records:
                    record["group"] = canonical
                merged[canonical].extend(records)
            ids = sorted(merged)
            if len(ids) < 2:
                raise ValueError(f"ข้อมูลกลุ่มใบไม่พอสำหรับแบ่งชุดทดสอบ: {label}")
            class_seed = int(hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()[:12], 16)
            rng = random.Random(class_seed)
            rng.shuffle(ids)
            boundary = min(len(ids) - 1, max(1, round(len(ids) * .2)))
            held_out = [row for group in ids[:boundary] for row in merged[group]]
            training = [row for group in ids[boundary:] for row in merged[group]]
            rng.shuffle(training)
            rng.shuffle(held_out)
            train.extend(training[:max_per_class])
            test.extend(held_out[:max(20, min(100, max_per_class // 3))])
        return train, test, {"mapped_images": known, "total_images": total,
                             "fallback_images": total - known, "seed": seed}
