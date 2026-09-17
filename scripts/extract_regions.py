#!/usr/bin/env python3
"""Extract transparent assets from manually cropped regions with bounded concurrency."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import uuid
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from gpt_image_api import call_edit_api, ImageDownloadError, _save_result, normalize_size


DEFAULT_PROMPT = """TASK: Faithful cutout of existing pixels, NOT UI design, icon generation, or creative restoration.
The input contains {target_count} target area(s) outlined by solid red annotation rectangles. Extract ONLY the existing asset visible INSIDE each rectangle onto a real transparent PNG background. The reference pixels and rectangle boundaries are authoritative.

STRICT CONTENT BOUNDARIES:
- Everything outside a red rectangle is context only and MUST NOT appear in its extracted asset. Do not import nearby navigation labels, captions, badges, or decorations, even if they are semantically related.
- Never add text. If a rectangle contains only a house, bag, or document icon, output ONLY that icon; do not add labels such as 首页, 商品, 订单, Home, Products, or Orders. Preserve text only when it is already visibly INSIDE that same rectangle. Never infer or complete unreadable text.
- Preserve exactly the existing shapes, silhouette, stroke thickness, line endings, corner radius, holes, number of strokes, small dots, proportions, colors, opacity, and internal spacing. A document icon must not become a clipboard; a two-line symbol must not gain a third line.
- Preserve the original rendering style. Do not turn flat artwork into 3D, beveled, embossed, glossy, metallic, or luminous artwork. Do not add shadows, highlights, outlines, frames, notches, corner accents, or decorative panels. Keep only effects that already exist in the source, with the same strength.

WHAT TO REMOVE AND WHAT TO KEEP:
- Remove the red annotation strokes and the surrounding page background. For a standalone icon, remove the page-colored surroundings, retaining the icon's existing strokes and any actual button surface or border inside its rectangle.
- If a rectangle encloses a complete button, card, banner, or carousel, keep that complete composite as ONE asset, including its existing surface, artwork, text, and decorations inside the rectangle. Do not strip its internal background or rebuild its contents.
- Do not extend content beyond the marked boundary or invent missing parts. Preserve visible existing edges, rounded corners and effects within the selected area.

FIDELITY BEFORE CLEANUP:
Only mild compression-noise reduction and edge cleanup are allowed, and only when they preserve the visible design. No creative sharpening, detail synthesis, added texture, thicker strokes, stronger contrast, recoloring, or upgraded materials. If a detail is uncertain or blurred, retain its original appearance instead of guessing. A slightly soft faithful cutout is preferable to a crisp redesigned icon.

OUTPUT:
EXACT GROUP COUNT: Return exactly {target_count} complete asset group(s): ONE red rectangle = ONE whole icon or composite. Count rectangles, NOT disconnected strokes or shapes. Do not output parts as separate assets or make an exploded view.
A document/order icon's outline and its inner horizontal lines belong to the SAME icon, in their original relative positions. A user icon's head and shoulders, a slider icon's bars and dots, and a button's surface and symbol must each remain one complete group. Transparent gaps inside a group are normal: do not join strokes with invented bridges, fill the gaps, or separate the pieces for display.
Use larger transparent spacing BETWEEN different groups than the internal gaps WITHIN each group. Preserve the original internal spacing and keep all parts of each marked icon together. Verify there are exactly {target_count} whole icons/composites before returning.
Keep the assets in the same reading order, separate and non-overlapping, with transparent gaps. Do not merge assets, add duplicates, add labels for identification, or split an existing composite. Preserve each asset's aspect ratio and relative internal geometry; never stretch or squash it.
Use actual PNG alpha=0 for the removed background and gaps. Never draw a checkerboard or flatten onto a solid background.
Before returning, compare each extracted asset with its marked source: remove any added text, shape, frame, highlight, shadow, or decoration. Do not output explanatory text.

Region purpose (identification only; it does not authorize additions or redesign): {purpose}
Target notes (subordinate to the source pixels and strict content boundaries above):
{target_instructions}
"""


def processing_mode(target: dict) -> str:
    value = target.get("processingMode") or target.get("extractionMode") or "ai-transparent"
    return {"ai": "ai-transparent", "background-script": "local-transparent"}.get(value, value)


def is_ai_transparent_target(target: dict) -> bool:
    return (
        target.get("approved", True)
        and target.get("reviewStatus", "confirmed") == "confirmed"
        and processing_mode(target) == "ai-transparent"
    )


class TransparencyError(ValueError):
    pass


def validate_transparency(path: Path) -> None:
    with Image.open(path) as image:
        if "A" not in image.getbands() and "transparency" not in image.info:
            raise TransparencyError("模型返回图片没有透明通道；棋盘格背景不等于真实透明")
        if image.convert("RGBA").getchannel("A").getextrema()[0] == 255:
            raise TransparencyError("模型返回图片完全不透明，无法拆分透明素材")


def retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {429, 500, 502, 503, 504}
    return isinstance(error, (TransparencyError, urllib.error.URLError, TimeoutError, ConnectionError))


def run_job(
    region: dict,
    crops_dir: Path,
    output_dir: Path,
    runs_dir: Path,
    size: str,
    quality: str,
    retries: int,
) -> dict:
    region_id = region["id"]
    targets = [target for target in region.get("targets", []) if is_ai_transparent_target(target)]
    if not targets:
        raise ValueError(f"Region {region_id} has no approved red-box targets")
    crop = crops_dir / region.get("markedCropFile", region["cropFile"])
    output = output_dir / f"{region_id}.png"
    record_dir = runs_dir / region_id
    prompt = DEFAULT_PROMPT.format(
        target_count=len(targets),
        purpose=region.get("purpose") or "extract exactly the content enclosed by each red rectangle",
        target_instructions="\n".join(
            f"- {target.get('id', f'target-{index:03d}')}: "
            f"{target.get('purpose') or 'preserve all visible content inside this red rectangle as one asset'}"
            for index, target in enumerate(targets, start=1)
        ),
    )
    status_path = record_dir / "status.json"
    fingerprint = hashlib.sha256(
        crop.read_bytes()
        + prompt.encode("utf-8")
        + json.dumps({"size": size, "quality": quality}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if output.exists():
        try:
            previous_status = json.loads(status_path.read_text(encoding="utf-8"))
            if previous_status.get("fingerprint") == fingerprint:
                validate_transparency(output)
                return {"id": region_id, "status": "skipped", "output": str(output), "fingerprint": fingerprint}
        except Exception:
            pass

    request_prompt = prompt + "\nOutput real PNG alpha transparency. Outside the assets, pixels must have alpha=0. Never draw a checkerboard, white/gray grid, or any simulated transparency background."
    # Resume downloading an already generated result without paying for generation again.
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if previous.get("fingerprint") == fingerprint and previous.get("downloadResponse"):
        response_path = Path(previous["downloadResponse"])
        candidate = response_path.parent / "result.png"
        try:
            _save_result(response_path.read_bytes(), "application/json", candidate, response_path.parent)
            normalize_size(candidate, size, response_path.parent)
            validate_transparency(candidate)
            shutil.copy2(candidate, output)
            result = {"id": region_id, "status": "completed", "output": str(output), "fingerprint": fingerprint}
            status_path.write_text(json.dumps(result), encoding="utf-8")
            return result
        except ImageDownloadError as exc:
            return {**previous, "error": str(exc)}
        except TransparencyError:
            pass
    run_dir = record_dir / ("run-" + uuid.uuid4().hex)
    for attempt in range(1, retries + 2):
        try:
            attempt_dir = run_dir / f"attempt-{attempt}"
            candidate = attempt_dir / "result.png"
            call_edit_api(request_prompt, crop, candidate, attempt_dir, size, quality)
            validate_transparency(candidate)
            output.parent.mkdir(parents=True, exist_ok=True)
            # Only validated results enter the directory consumed by splitting.
            shutil.copy2(candidate, output)
            result = {
                "id": region_id,
                "status": "completed",
                "attempt": attempt,
                "output": str(output),
                "fingerprint": fingerprint,
            }
            record_dir.mkdir(parents=True, exist_ok=True)
            status_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        except Exception as exc:
            if isinstance(exc, TransparencyError):
                request_prompt = prompt + "\nCORRECTION: The previous output was rejected because its background was opaque. Return RGBA PNG with actual alpha=0 outside the assets and in their gaps. Do NOT paint a checkerboard or flatten onto any background. Preserve all content inside each target."
            if attempt > retries or not retryable(exc):
                result = {"id": region_id, "status": "failed", "attempt": attempt, "error": str(exc)}
                if isinstance(exc, ImageDownloadError):
                    result.update(fingerprint=fingerprint, downloadResponse=str(attempt_dir / "response.json"))
                record_dir.mkdir(parents=True, exist_ok=True)
                status_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                return result
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("regions", type=Path, help="regions.normalized.json produced by crop_regions.py")
    parser.add_argument("crops", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("runs", type=Path)
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="parallel workers; 0 means one worker per approved crop (default: 0)",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="medium")
    parser.add_argument("--dry-run", action="store_true", help="write the job plan without calling the API")
    args = parser.parse_args()
    if args.workers < 0 or args.retries < 0:
        parser.error("--workers and --retries must be >= 0")

    document = json.loads(args.regions.read_text(encoding="utf-8"))
    regions = [region for region in document.get("regions", []) if region.get("approved", True) and any(
        is_ai_transparent_target(target)
        for target in region.get("targets", [])
    )]
    missing_targets = [region["id"] for region in regions if not any(
        is_ai_transparent_target(target) for target in region.get("targets", [])
    )]
    if missing_targets:
        parser.error(f"approved regions must contain at least one approved red-box target: {', '.join(missing_targets)}")
    actual_workers = len(regions) if args.workers == 0 else args.workers
    args.output.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        plan = [
            {
                "id": region["id"],
                "input": str((args.crops / region.get("markedCropFile", region["cropFile"])).resolve()),
                "output": str((args.output / f"{region['id']}.png").resolve()),
                "purpose": region.get("purpose", ""),
                "targetCount": len([target for target in region.get("targets", []) if is_ai_transparent_target(target)]),
            }
            for region in regions
        ]
        (args.output / "extraction-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"Planned {len(plan)} extraction job(s) with {actual_workers} concurrent worker(s); "
            "API was not called"
        )
        return
    results = []
    if not regions:
        (args.output / "extraction-results.json").write_text("[]\n", encoding="utf-8")
        print("No approved regions to extract")
        return
    print(f"Extracting {len(regions)} crop(s) with concurrency={actual_workers}")
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = [
            executor.submit(run_job, region, args.crops, args.output, args.runs, args.size, args.quality, args.retries)
            for region in regions
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result['id']}: {result['status']}" + (f" — {result['error']}" if result.get("error") else ""))
    results.sort(key=lambda item: item["id"])
    (args.output / "extraction-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(result["status"] == "failed" for result in results):
        failures = [f"{result['id']}：{result['error']}" for result in results if result['status'] == 'failed']
        raise SystemExit("透明素材提取失败：" + "；".join(failures))


if __name__ == "__main__":
    main()
