"""Generate ~400px JPEG thumbnails of event hero images for the gallery.

Reads full-resolution images from data/<city>-for-agents/images/ and
writes web-sized thumbnails to docs/<city>/thumbs/<event_id>.jpg.
Idempotent: existing thumbs are skipped unless --refresh.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .city import CityConfig, active_city


THUMB_MAX_DIM = 400
JPEG_QUALITY = 80


def _images_dir(city: CityConfig) -> Path:
    return Path(city.dataset_path) / "images"


def _thumbs_dir(city: CityConfig) -> Path:
    return Path("docs") / city.slug / "thumbs"


def _thumb_path(city: CityConfig, event_id: str) -> Path:
    return _thumbs_dir(city) / f"{event_id}.jpg"


def _resize_one(src: Path, dst: Path) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
            im = background
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.LANCZOS)
        im.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)


def build_thumbnails(
    *,
    city: CityConfig | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    city = city or active_city()
    src_dir = _images_dir(city)
    if not src_dir.is_dir():
        raise FileNotFoundError(
            f"Missing {src_dir}. Run the dataset's scripts/enrich.py to fetch images first."
        )

    dst_dir = _thumbs_dir(city)
    dst_dir.mkdir(parents=True, exist_ok=True)

    counts = {"total": 0, "generated": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []

    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        if src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        counts["total"] += 1

        event_id = src.stem
        dst = _thumb_path(city, event_id)
        if dst.is_file() and not refresh:
            counts["skipped"] += 1
            continue

        try:
            _resize_one(src, dst)
            counts["generated"] += 1
        except Exception as exc:
            counts["failed"] += 1
            failures.append(f"{src.name}: {exc}")

    return {
        "city": city.slug,
        "thumbs_dir": str(dst_dir),
        "counts": counts,
        "failures": failures[:20],
    }


def thumb_relative_url(city: CityConfig, event_id: str) -> str:
    """Path used inside the gallery HTML (relative to docs/)."""
    return f"./{city.slug}/thumbs/{event_id}.jpg"


def thumb_exists(city: CityConfig, event_id: str) -> bool:
    return _thumb_path(city, event_id).is_file()
