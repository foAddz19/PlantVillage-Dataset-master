"""Shared image validation without importing a classifier or training stack."""
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

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


