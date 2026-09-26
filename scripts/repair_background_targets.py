#!/usr/bin/env python3
"""Repair marked reusable backgrounds with the configured image edit API."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PIL import Image

import sys

# Direct script execution must resolve shared modules in the project root.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from image_dimensions import normalize_size as normalize_request_size

try:
    from .gpt_image_api import call_edit_api
except ImportError:  # Direct script execution.
    from gpt_image_api import call_edit_api


def mode_of(target: dict) -> str:
    value = target.get("processingMode") or target.get("extractionMode") or "ai-transparent"
    return {"ai": "ai-transparent", "background-script": "local-transparent"}.get(value, value)


def retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {429, 500, 502, 503, 504}
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def fit_to_source_aspect(image: Image.Image, source_size: tuple[int, int]) -> Image.Image:
    """Center-crop to the source aspect ratio before resizing, so objects are never stretched."""
    source_width, source_height = source_size
    target_ratio = source_width / source_height
    width, height = image.size
    result_ratio = width / height
    if result_ratio > target_ratio:
        crop_width = max(1, round(height * target_ratio))
        left = (width - crop_width) // 2
        box = (left, 0, left + crop_width, height)
    else:
        crop_height = max(1, round(width / target_ratio))
        top = (height - crop_height) // 2
        box = (0, top, width, top + crop_height)
    return image.crop(box).resize(source_size, Image.Resampling.LANCZOS)


def merge_repair_versions(previous: dict | None, current: dict) -> dict:
    if not previous:
        return current
    versions = []
    seen = set()
    for version in previous.get("versions", []) + current.get("versions", []):
        identity = (version.get("id"), version.get("file"))
        if identity in seen:
            continue
        seen.add(identity)
        versions.append(version)
    return {
        **previous,
        **current,
        "versions": versions,
        "processingHistory": previous.get("processingHistory", []) + current.get("processingHistory", []),
    }


def repair_one(job: dict, output_root: Path, runs_root: Path, size: str, quality: str, retries: int) -> dict:
    size = normalize_request_size(size)
    region, target, source = job["region"], job["target"], job["source"]
    base_id = f"{region['id']}-{target['id']}"
    originals = output_root / "originals"
    processed = output_root / "background-repair"
    originals.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    original_file = originals / f"{base_id}.png"
    if not original_file.exists():
        source.save(original_file, optimize=True)
    run_tag = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    processed_file = processed / f"{base_id}-{run_tag}.png"
    prompt = (
        "Restore this marked UI background as one complete reusable opaque image. "
        "Remove only foreground interface elements that visibly obstruct the underlying artwork, then reconstruct the hidden texture, illustration, lighting, borders and decoration from surrounding context. "
        "Preserve integrated artwork, artistic text, branding and all content that belongs to the background. "
        "Keep every person, character, object and decoration at the exact original proportions. Never stretch, squash, widen or shorten anything. "
        "Preserve the source aspect ratio and framing inside the output canvas; center the original composition if the API canvas has a different ratio. "
        "Do not redesign, recolor, crop important content, add new objects or change the composition. Avoid seams, duplicated controls and rectangular repair patches. "
        f"Target instruction: {target.get('purpose') or 'complete the reusable background behind foreground UI'}."
    )
    for attempt in range(1, retries + 2):
        try:
            call_edit_api(prompt, original_file, processed_file, runs_root / base_id / f"attempt-{attempt}", size, quality, transparent_background=False)
            with Image.open(processed_file) as result:
                api_result_size = result.size
                restored = fit_to_source_aspect(result.convert("RGBA"), source.size)
                restored.save(processed_file, optimize=True)
            page_box = {
                "x": int(region["x"]) + int(target["x"]), "y": int(region["y"]) + int(target["y"]),
                "width": int(target["width"]), "height": int(target["height"]),
            }
            version_id = f"processed-{run_tag}"
            return {
                "id": f"repair-{base_id}", "file": f"background-repair/{processed_file.name}",
                "sourceRegion": region["id"], "sourceTarget": target["id"],
                "matchedSourceTarget": {**target, "pageBox": page_box},
                "sourceMarkedBox": {key: region[key] for key in ("x", "y", "width", "height")},
                "elementType": target.get("elementType", "complete-composite"), "role": "complete-composite",
                "processingMode": "background-repair", "extractionMethod": "background-repair",
                "name": target.get("name", ""), "parent": target.get("parent", ""), "zIndex": target.get("zIndex"),
                "placement": {"status": "confirmed-from-red-box", "bbox": page_box},
                "versions": [
                    {"id": "original", "kind": "source-crop", "file": f"originals/{original_file.name}"},
                    {"id": version_id, "kind": "background-repair", "file": f"background-repair/{processed_file.name}"},
                ],
                "activeVersion": version_id,
                "processingHistory": [{
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": "background-repair",
                    "inputVersion": "original", "outputVersion": version_id, "parameters": {
                        "prompt": target.get("purpose", ""), "apiResultSize": list(api_result_size),
                        "outputSize": list(source.size), "aspectPolicy": "center-crop-no-stretch",
                    },
                }],
                "approved": False, "reviewStatus": "pending-human-approval",
            }
        except Exception as error:
            if attempt > retries or not retryable(error):
                raise
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("regions", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("runs", type=Path)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="medium")
    args = parser.parse_args()
    document = json.loads(args.regions.read_text(encoding="utf-8"))
    reference = Image.open(args.reference).convert("RGBA")
    jobs = []
    for region in document.get("regions", []):
        if not region.get("approved", True):
            continue
        for target in region.get("targets", []):
            if not target.get("approved", True) or target.get("reviewStatus", "confirmed") != "confirmed" or mode_of(target) != "background-repair":
                continue
            left = int(region["x"]) + int(target["x"])
            top = int(region["y"]) + int(target["y"])
            right = left + int(target["width"])
            bottom = top + int(target["height"])
            jobs.append({"region": region, "target": target, "source": reference.crop((left, top, right, bottom))})
    if not jobs:
        raise ValueError("没有已确认的背景补全目标")
    worker_count = len(jobs) if args.workers == 0 else max(1, args.workers)
    results = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(repair_one, job, args.output, args.runs, args.size, args.quality, args.retries) for job in jobs]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["id"])
    repair_manifest = {"canvas": document["canvas"], "assets": results}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "asset-manifest.repair.json").write_text(json.dumps(repair_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    draft_path = args.output / "asset-manifest.draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {"canvas": document["canvas"], "assets": [], "codeElements": []}
    previous_repairs = {asset.get("id"): asset for asset in draft.get("assets", []) if asset.get("processingMode") == "background-repair"}
    retained = [asset for asset in draft.get("assets", []) if asset.get("processingMode") != "background-repair"]
    results = [merge_repair_versions(previous_repairs.get(result.get("id")), result) for result in results]
    draft["assets"] = retained + results
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Repaired {len(results)} background target(s) with concurrency={worker_count}")


if __name__ == "__main__":
    main()
