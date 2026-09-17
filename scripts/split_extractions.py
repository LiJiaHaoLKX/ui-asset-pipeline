#!/usr/bin/env python3
"""Split every transparent API result and create a draft asset manifest."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image


def processing_mode(target: dict) -> str:
    value = target.get("processingMode") or target.get("extractionMode") or "ai-transparent"
    return {"ai": "ai-transparent", "background-script": "local-transparent"}.get(value, value)


def is_ai_transparent_target(target: dict) -> bool:
    return target.get("approved", True) and target.get("reviewStatus", "confirmed") == "confirmed" and processing_mode(target) == "ai-transparent"


def targets_in_reading_order(targets: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for target in sorted(targets, key=lambda item: item["markedCropBox"]["y"]):
        box = target["markedCropBox"]
        top, bottom = box["y"], box["y"] + box["height"]
        best_row = None
        best_overlap = 0
        for row in rows:
            overlap = min(bottom, row["bottom"]) - max(top, row["top"])
            if overlap > best_overlap and overlap >= min(box["height"], row["bottom"] - row["top"]) / 2:
                best_row, best_overlap = row, overlap
        if best_row is None:
            rows.append({"top": top, "bottom": bottom, "items": [target]})
        else:
            best_row["top"] = min(best_row["top"], top)
            best_row["bottom"] = max(best_row["bottom"], bottom)
            best_row["items"].append(target)
    ordered: list[dict] = []
    for row in sorted(rows, key=lambda item: item["top"]):
        ordered.extend(sorted(row["items"], key=lambda item: item["markedCropBox"]["x"]))
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("regions", type=Path, help="regions.normalized.json")
    parser.add_argument("raw", type=Path, help="directory containing region-*.png API outputs")
    parser.add_argument("output", type=Path, help="directory for split assets")
    parser.add_argument(
        "--split-script",
        type=Path,
        default=Path(__file__).resolve().parent / "split_transparent_objects.py",
    )
    parser.add_argument("--join-gap", type=int, default=0)
    parser.add_argument("--min-area", type=int, default=256, help="ignore Alpha fragments smaller than this")
    args = parser.parse_args()

    document = json.loads(args.regions.read_text(encoding="utf-8"))
    crops = args.regions.parent
    results_path = args.raw / "extraction-results.json"
    statuses = {item['id']: item for item in json.loads(results_path.read_text(encoding='utf-8'))} if results_path.exists() else {}
    args.output.mkdir(parents=True, exist_ok=True)
    assets = []
    serial = 1
    for region in document.get("regions", []):
        approved_targets = [target for target in region.get("targets", []) if is_ai_transparent_target(target)]
        if not approved_targets:
            continue
        region_id = region["id"]
        if region_id in statuses and statuses[region_id].get('status') not in {'completed', 'skipped'}:
            print(f"跳过 {region_id}：该区域提取未成功，请先重试提取")
            continue
        source = args.raw / f"{region_id}.png"
        if not source.exists():
            continue
        destination = args.output / region_id
        command = [sys.executable, str(args.split_script), str(source), str(destination)]
        if args.join_gap:
            command.extend(["--join-gap", str(args.join_gap)])
        command.extend(["--min-area", str(args.min_area), "--expected-count", str(len(approved_targets))])
        subprocess.run(command, check=True)
        local_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        target_matches: dict[str, dict] = {}
        if len(approved_targets) == len(local_manifest):
            source_order = targets_in_reading_order(approved_targets)
            target_matches = {
                local["file"]: target for local, target in zip(local_manifest, source_order)
            }
        for local in local_manifest:
            asset_id = f"asset-{serial:03d}"
            matched_target = target_matches.get(local["file"])
            originals_dir = args.output / "originals"
            originals_dir.mkdir(parents=True, exist_ok=True)
            original_filename = f"{region_id}-{matched_target.get('id') if matched_target else asset_id}.png"
            original_path = originals_dir / original_filename
            if not original_path.exists():
                with Image.open(crops / region["cropFile"]) as source_crop:
                    if matched_target:
                        box = matched_target["markedCropBox"]
                        original = source_crop.crop((box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]))
                    else:
                        original = source_crop.copy()
                    original.save(original_path, optimize=True)
            assets.append(
                {
                    "id": asset_id,
                    "file": f"{region_id}/{local['file']}",
                    "sourceRegion": region_id,
                    "sourceMarkedBox": {
                        "x": region["x"], "y": region["y"], "width": region["width"], "height": region["height"]
                    },
                    "sourceCropBox": region["cropBox"],
                    "sourceTargets": [
                        {
                            **target,
                            "pageBox": {
                                "x": region["x"] + target["x"],
                                "y": region["y"] + target["y"],
                                "width": target["width"],
                                "height": target["height"],
                            },
                        }
                        for target in region.get("targets", [])
                        if is_ai_transparent_target(target)
                    ],
                    "matchedSourceTarget": matched_target,
                    "sourceTarget": matched_target.get("id") if matched_target else None,
                    "extractedImageBox": local["bbox"],
                    "extractionMethod": "ai",
                    "processingMode": "ai-transparent",
                    "elementType": (matched_target or {}).get("elementType", "image-asset"),
                    "name": (matched_target or {}).get("name", ""),
                    "role": "image-asset",
                    "parent": (matched_target or {}).get("parent", ""),
                    "zIndex": (matched_target or {}).get("zIndex"),
                    "placement": {"status": "needs-codex-review", "bbox": None},
                    "versions": [
                        {"id": "original", "kind": "source-crop", "file": f"originals/{original_filename}"},
                        {"id": "processed", "kind": "ai-transparent", "file": f"{region_id}/{local['file']}"},
                    ],
                    "activeVersion": "processed",
                    "processingHistory": [{"mode": "ai-transparent", "inputVersion": "original", "outputVersion": "processed"}],
                    "approved": False,
                    "reviewStatus": "pending-human-approval",
                }
            )
            serial += 1

    script_manifest_path = args.output / "asset-manifest.script.json"
    script_manifest = json.loads(script_manifest_path.read_text(encoding="utf-8")) if script_manifest_path.exists() else {}
    repair_manifest_path = args.output / "asset-manifest.repair.json"
    repair_manifest = json.loads(repair_manifest_path.read_text(encoding="utf-8")) if repair_manifest_path.exists() else {}
    manifest = {
        "canvas": document["canvas"],
        "coordinateNotice": "API output may rescale or recenter assets. Do not map extractedImageBox directly to page coordinates.",
        "assets": script_manifest.get("assets", []) + repair_manifest.get("assets", []) + assets,
        "codeElements": script_manifest.get("codeElements", []),
    }
    (args.output / "asset-manifest.draft.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Created {len(assets)} draft asset record(s)")


if __name__ == "__main__":
    main()
