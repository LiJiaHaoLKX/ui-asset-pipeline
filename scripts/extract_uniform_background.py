#!/usr/bin/env python3
"""Extract individually marked assets by removing an edge-connected uniform background."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

from PIL import Image


LOCAL_MODE = "local-transparent"
COMPLETE_CROP_MODE = "complete-crop"


def processing_mode(target: dict) -> str:
    value = target.get("processingMode") or target.get("extractionMode") or "ai-transparent"
    return {"ai": "ai-transparent", "background-script": LOCAL_MODE}.get(value, value)


def dominant_edge_color(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    edge = []
    for x in range(width):
        edge.extend((rgb.getpixel((x, 0)), rgb.getpixel((x, height - 1))))
    for y in range(1, max(1, height - 1)):
        edge.extend((rgb.getpixel((0, y)), rgb.getpixel((width - 1, y))))
    buckets = Counter(tuple(channel // 8 for channel in pixel) for pixel in edge)
    dominant = buckets.most_common(1)[0][0]
    members = [pixel for pixel in edge if tuple(channel // 8 for channel in pixel) == dominant]
    return tuple(round(sum(pixel[index] for pixel in members) / len(members)) for index in range(3))


def remove_edge_background(image: Image.Image, tolerance: int, feather: int) -> tuple[Image.Image, tuple[int, int, int]]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    background = dominant_edge_color(rgba)
    outer = tolerance + feather
    pixels = rgba.load()
    connected: set[tuple[int, int]] = set()
    pending: deque[tuple[int, int]] = deque()

    def distance(x: int, y: int) -> float:
        red, green, blue, _ = pixels[x, y]
        return math.sqrt((red - background[0]) ** 2 + (green - background[1]) ** 2 + (blue - background[2]) ** 2)

    for x in range(width):
        pending.extend(((x, 0), (x, height - 1)))
    for y in range(1, height - 1):
        pending.extend(((0, y), (width - 1, y)))

    while pending:
        x, y = pending.popleft()
        if (x, y) in connected or distance(x, y) > outer:
            continue
        connected.add((x, y))
        if x: pending.append((x - 1, y))
        if x + 1 < width: pending.append((x + 1, y))
        if y: pending.append((x, y - 1))
        if y + 1 < height: pending.append((x, y + 1))

    for x, y in connected:
        red, green, blue, alpha = pixels[x, y]
        color_distance = distance(x, y)
        if feather and color_distance > tolerance:
            alpha = round(alpha * min(1.0, (color_distance - tolerance) / feather))
        else:
            alpha = 0
        pixels[x, y] = (red, green, blue, alpha)
    return rgba, background


def extract_targets(reference: Path, regions_file: Path, output_root: Path) -> dict:
    image = Image.open(reference).convert("RGBA")
    document = json.loads(regions_file.read_text(encoding="utf-8"))
    canvas = document.get("canvas", {})
    if (canvas.get("width"), canvas.get("height")) != image.size:
        raise ValueError("标注画布尺寸与参考图不一致")

    script_root = output_root / "local"
    originals_root = output_root / "originals"
    script_root.mkdir(parents=True, exist_ok=True)
    originals_root.mkdir(parents=True, exist_ok=True)
    run_tag = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    assets = []
    code_elements = []
    for region in document.get("regions", []):
        if not region.get("approved", True):
            continue
        for target in region.get("targets", []):
            mode = processing_mode(target)
            if not target.get("approved", True) or target.get("reviewStatus", "confirmed") != "confirmed":
                continue
            page_x = int(region["x"]) + int(target["x"])
            page_y = int(region["y"]) + int(target["y"])
            width, height = int(target["width"]), int(target["height"])
            if width <= 0 or height <= 0 or page_x < 0 or page_y < 0 or page_x + width > image.width or page_y + height > image.height:
                raise ValueError(f"素材 {region['id']}/{target['id']} 超出参考图")
            tolerance = max(0, min(int(target.get("backgroundTolerance", 34)), 255))
            feather = max(0, min(int(target.get("edgeFeather", 48)), 64))
            cropped = image.crop((page_x, page_y, page_x + width, page_y + height))
            base_id = f"{region['id']}-{target['id']}"
            original_filename = f"{base_id}.png"
            original_path = originals_root / original_filename
            if not original_path.exists():
                cropped.save(original_path, optimize=True)
            page_box = {"x": page_x, "y": page_y, "width": width, "height": height}
            if target.get("elementType") == "code-element" or mode == "none":
                code_elements.append({
                    "id": f"code-{base_id}", "sourceRegion": region["id"], "sourceTarget": target["id"],
                    "name": target.get("name", ""), "semanticDescription": target.get("purpose", ""),
                    "elementType": "code-element", "processingMode": "none", "parent": target.get("parent", ""),
                    "zIndex": target.get("zIndex"), "placement": {"status": "confirmed-from-red-box", "bbox": page_box},
                    "suggestion": target.get("suggestion", {"source": "manual"}), "approved": False,
                    "reviewStatus": "pending-human-approval",
                })
                continue
            if mode not in {LOCAL_MODE, COMPLETE_CROP_MODE}:
                continue
            background = None
            if mode == LOCAL_MODE:
                extracted, background = remove_edge_background(cropped, tolerance, feather)
            else:
                extracted = cropped
            alpha_box = extracted.getchannel("A").getbbox()
            if not alpha_box:
                raise ValueError(f"素材 {region['id']}/{target['id']} 被完全移除，请降低颜色容差")
            pad = 2
            left = max(0, alpha_box[0] - pad)
            top = max(0, alpha_box[1] - pad)
            right = min(width, alpha_box[2] + pad)
            bottom = min(height, alpha_box[3] + pad)
            if mode == COMPLETE_CROP_MODE:
                left, top, right, bottom = 0, 0, width, height
            result = extracted.crop((left, top, right, bottom))
            filename = f"{base_id}-{run_tag}.png"
            result.save(script_root / filename, optimize=True)
            result_page_box = {"x": page_x + left, "y": page_y + top, "width": right - left, "height": bottom - top}
            source_target = {**target, "pageBox": {"x": page_x, "y": page_y, "width": width, "height": height}}
            assets.append({
                "id": f"local-{region['id']}-{target['id']}",
                "file": f"local/{filename}",
                "sourceRegion": region["id"],
                "sourceTarget": target["id"],
                "sourceMarkedBox": {"x": region["x"], "y": region["y"], "width": region["width"], "height": region["height"]},
                "sourceTargets": [source_target],
                "matchedSourceTarget": source_target,
                "extractedImageBox": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                "backgroundColor": {"r": background[0], "g": background[1], "b": background[2]} if background else None,
                "backgroundTolerance": tolerance,
                "edgeFeather": feather,
                "extractionMethod": "background-script" if mode == LOCAL_MODE else COMPLETE_CROP_MODE,
                "processingMode": mode,
                "elementType": target.get("elementType", "complete-composite" if mode == COMPLETE_CROP_MODE else "image-asset"),
                "name": target.get("name", ""),
                "role": target.get("elementType", "image-asset"),
                "parent": target.get("parent", ""),
                "zIndex": target.get("zIndex"),
                "placement": {"status": "derived-from-local-processing", "bbox": result_page_box},
                "versions": [
                    {"id": "original", "kind": "source-crop", "file": f"originals/{original_filename}"},
                    {"id": f"processed-{run_tag}", "kind": mode, "file": f"local/{filename}"},
                ],
                "activeVersion": f"processed-{run_tag}",
                "processingHistory": [{
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": mode,
                    "inputVersion": "original", "outputVersion": f"processed-{run_tag}",
                    "parameters": {"backgroundTolerance": tolerance, "edgeFeather": feather} if mode == LOCAL_MODE else {},
                }],
                "approved": False,
                "reviewStatus": "pending-human-approval",
            })
    return {"canvas": document["canvas"], "assets": assets, "codeElements": code_elements}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("regions", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = extract_targets(args.reference, args.regions, args.output)
    (args.output / "asset-manifest.script.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Processed {len(manifest['assets'])} local asset(s) and {len(manifest['codeElements'])} code element(s)")


if __name__ == "__main__":
    main()
