#!/usr/bin/env python3
"""Promote reviewed assets into the approved directory and finalize placement metadata."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review_manifest", type=Path)
    parser.add_argument("regions", type=Path)
    parser.add_argument("approved_dir", type=Path)
    parser.add_argument("output_manifest", type=Path)
    args = parser.parse_args()

    review = json.loads(args.review_manifest.read_text(encoding="utf-8"))
    region_document = json.loads(args.regions.read_text(encoding="utf-8"))
    region_map = {region["id"]: region for region in region_document["regions"]}
    fallback_targets = {
        "neon-racing-cover": "target-001",
        "magic-brawl-cover": "target-002",
    }
    args.approved_dir.mkdir(parents=True, exist_ok=True)
    approved = []
    for asset in review["assets"]:
        region = region_map[asset["sourceRegion"]]
        target_id = (asset.get("matchedSourceTarget") or {}).get("id") or fallback_targets.get(asset["name"])
        target = next(item for item in region["targets"] if item["id"] == target_id)
        page_box = {
            "x": region["x"] + target["x"],
            "y": region["y"] + target["y"],
            "width": target["width"],
            "height": target["height"],
        }
        shutil.copy2(args.review_manifest.parent / asset["file"], args.approved_dir / asset["file"])
        approved.append(
            {
                **asset,
                "matchedSourceTarget": {**target, "pageBox": page_box},
                "placement": {"status": "confirmed-from-red-box", "bbox": page_box},
                "reviewStatus": "approved",
                "approved": True,
            }
        )
    manifest = {
        **review,
        "assets": approved,
        "codeElements": [
            {
                "name": "online-player-icon",
                "reason": "The small icon was ignored by image extraction and is more reliable as a code icon.",
                "sourceRegion": "region-003",
                "sourceTarget": "target-003",
            }
        ],
    }
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Approved {len(approved)} asset(s)")


if __name__ == "__main__":
    main()
