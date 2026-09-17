#!/usr/bin/env python3
"""Crop manually marked regions from a reference image using regions.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw


def processing_mode(target: dict) -> str:
    value = target.get("processingMode") or target.get("extractionMode") or "ai-transparent"
    return {"ai": "ai-transparent", "background-script": "local-transparent"}.get(value, value)


def crop_regions(source: Path, regions_file: Path, output_dir: Path, padding: int) -> None:
    image = Image.open(source).convert("RGBA")
    document = json.loads(regions_file.read_text(encoding="utf-8"))
    canvas = document.get("canvas", {})
    expected_size = (canvas.get("width"), canvas.get("height"))
    if expected_size != (None, None) and expected_size != image.size:
        raise ValueError(f"regions canvas {expected_size} does not match image size {image.size}")

    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    for region in document.get("regions", []):
        region_id = str(region["id"])
        x, y = int(region["x"]), int(region["y"])
        width, height = int(region["width"]), int(region["height"])
        if width <= 0 or height <= 0:
            raise ValueError(f"Region {region_id} has non-positive dimensions")

        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(image.width, x + width + padding)
        bottom = min(image.height, y + height + padding)
        filename = f"{region_id}.png"
        marked_filename = f"{region_id}.marked.png"
        cropped = image.crop((left, top, right, bottom))
        cropped.save(output_dir / filename, optimize=True)
        targets = []
        marked = cropped.copy()
        draw = ImageDraw.Draw(marked)
        for target_index, target in enumerate(region.get("targets", []), start=1):
            target_id = str(target.get("id") or f"target-{target_index:03d}")
            tx, ty = int(target["x"]), int(target["y"])
            tw, th = int(target["width"]), int(target["height"])
            if tw <= 0 or th <= 0 or tx < 0 or ty < 0 or tx + tw > width or ty + th > height:
                raise ValueError(f"Target {region_id}/{target_id} is outside its crop region")
            local_box = {
                "x": tx + x - left,
                "y": ty + y - top,
                "width": tw,
                "height": th,
            }
            targets.append({**target, "id": target_id, "markedCropBox": local_box})
            if processing_mode(target) != "ai-transparent" or target.get("reviewStatus", "confirmed") != "confirmed":
                continue
            x1, y1 = local_box["x"], local_box["y"]
            x2, y2 = x1 + tw - 1, y1 + th - 1
            draw.rectangle((x1, y1, x2, y2), outline=(255, 24, 55, 255), width=5)
        marked.save(output_dir / marked_filename, optimize=True)
        normalized.append(
            {
                **region,
                "cropFile": filename,
                "markedCropFile": marked_filename,
                "cropBox": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                "targets": targets,
            }
        )

    (output_dir / "regions.normalized.json").write_text(
        json.dumps({"canvas": {"width": image.width, "height": image.height}, "regions": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Cropped {len(normalized)} region(s) into {output_dir.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("regions", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--padding", type=int, default=0)
    args = parser.parse_args()
    if args.padding < 0:
        parser.error("--padding must be >= 0")
    crop_regions(args.source, args.regions, args.output, args.padding)


if __name__ == "__main__":
    main()
