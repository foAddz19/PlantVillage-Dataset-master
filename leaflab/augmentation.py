"""Deterministic training augmentation and separately seeded camera stress tests.

These transformations simulate some capture changes, not real-camera validation.
Original dataset files are never modified.
"""
import hashlib
import io

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

AUGMENTATION_VERSION = 1
STRESS_PROFILES = ("lighting", "framing", "screen")


def image_rng(path, variant, namespace="train"):
    digest = hashlib.sha256(f"leaflab-aug1:{namespace}:{path}:{variant}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def camera_variant(image, rng, profile="mixed"):
    if profile not in {"mild", "mixed", *STRESS_PROFILES}:
        raise ValueError(f"Unknown augmentation profile: {profile}")
    image = ImageOps.fit(image, (192, 192), method=Image.Resampling.BILINEAR)
    mild = profile == "mild"
    if profile in {"mixed", "framing", "screen"}:
        # Reflection avoids teaching a class-specific border colour.
        pad = 36
        image = Image.fromarray(np.pad(np.asarray(image), ((pad, pad), (pad, pad), (0, 0)), mode="reflect"))
        angle = float(rng.uniform(-16, 16))
        image = image.rotate(angle, resample=Image.Resampling.BILINEAR)
        zoom = float(rng.uniform(.88, 1.12))
        side = 192 / zoom
        cx, cy = np.array(image.size) / 2 + rng.uniform(-9, 9, size=2)
        image = image.transform((192, 192), Image.Transform.EXTENT,
                                (cx-side/2, cy-side/2, cx+side/2, cy+side/2), Image.Resampling.BILINEAR)
        if profile == "mixed" and rng.random() < .5:
            image = ImageOps.mirror(image)
    if profile != "framing":
        image = ImageEnhance.Brightness(image).enhance(float(rng.uniform(.85, 1.15) if mild else rng.uniform(.7, 1.3)))
        image = ImageEnhance.Contrast(image).enhance(float(rng.uniform(.88, 1.12) if mild else rng.uniform(.8, 1.2)))
        image = ImageEnhance.Color(image).enhance(float(rng.uniform(.88, 1.12) if mild else rng.uniform(.75, 1.25)))
        pixels = np.asarray(image, dtype=np.float32)
        # Small channel gains approximate white-balance changes without recolouring diseases.
        pixels *= rng.uniform(.94 if mild else .88, 1.06 if mild else 1.12, size=(1, 1, 3))
        yy, xx = np.mgrid[:192, :192].astype(np.float32) / 191 - .5
        pixels *= (1 + rng.uniform(-.2, .2) * xx + rng.uniform(-.2, .2) * yy)[..., None]
        if profile in {"mixed", "screen"}:
            pixels *= (1 + .025 * np.sin(yy * rng.uniform(100, 220) + rng.uniform(0, 6)))[..., None]
            pixels += rng.normal(0, 1.5, size=pixels.shape)
        image = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
    if profile in {"mixed", "screen"}:
        resolution = int(rng.integers(115, 180))
        image = image.resize((resolution, resolution), Image.Resampling.BILINEAR).resize((192, 192), Image.Resampling.BILINEAR)
        image = image.filter(ImageFilter.GaussianBlur(float(rng.uniform(.2, .65))))
        with io.BytesIO() as buffer:
            image.save(buffer, format="JPEG", quality=int(rng.integers(76, 96)))
            buffer.seek(0)
            with Image.open(buffer) as compressed:
                image = compressed.convert("RGB")
    return image


def training_variants(image, path):
    # Only call this after assigning the source leaf to the training split.
    yield image
    yield camera_variant(image, image_rng(path, 0), "mild")
    yield camera_variant(image, image_rng(path, 1), "mixed")


def stress_variant(image, path, profile):
    return camera_variant(image, image_rng(path, profile, "held-out-stress"), profile)
