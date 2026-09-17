#!/usr/bin/env python3
"""Copy split assets to stable semantic filenames for human review."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft_manifest", type=Path)
    parser.add_argument("naming", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    draft = json.loads(args.draft_manifest.read_text(encoding="utf-8"))
    naming = json.loads(args.naming.read_text(encoding="utf-8"))
    source_root = args.draft_manifest.parent
    args.output.mkdir(parents=True, exist_ok=True)
    reviewed = []
    for asset in draft["assets"]:
        source_file = asset["file"]
        if source_file not in naming:
            raise ValueError(f"No semantic name for {source_file}")
        name = naming[source_file]
        destination_name = f"{name}.png"
        shutil.copy2(source_root / source_file, args.output / destination_name)
        target = asset.get("matchedSourceTarget")
        reviewed.append(
            {
                **asset,
                "name": name,
                "file": destination_name,
                "semanticDescription": target.get("purpose") if target else name.replace("-", " "),
                "reviewStatus": "pending-human-approval",
                "approved": False,
            }
        )
    manifest = {**draft, "assets": reviewed}
    (args.output / "review-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Prepared {len(reviewed)} named asset(s) for review")


if __name__ == "__main__":
    main()
