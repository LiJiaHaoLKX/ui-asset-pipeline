#!/usr/bin/env python3
"""Generate stable overlay/difference artifacts for Codex visual review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("render", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--threshold", type=int, default=20)
    args = parser.parse_args()
    if not 0 <= args.threshold <= 255:
        parser.error("--threshold must be between 0 and 255")

    reference = Image.open(args.reference).convert("RGBA")
    render = Image.open(args.render).convert("RGBA")
    if reference.size != render.size:
        raise ValueError(f"image sizes differ: reference={reference.size}, render={render.size}")
    args.output.mkdir(parents=True, exist_ok=True)

    Image.blend(render, reference, 0.5).save(args.output / "overlay.png", optimize=True)
    difference = ImageChops.difference(reference.convert("RGB"), render.convert("RGB"))
    ImageEnhance.Contrast(difference).enhance(2.0).save(args.output / "diff.png", optimize=True)

    grayscale = difference.convert("L")
    mask = grayscale.point(lambda value: 255 if value >= args.threshold else 0)
    mask.save(args.output / "diff-mask.png", optimize=True)
    histogram = grayscale.histogram()
    total_pixels = reference.width * reference.height
    changed_pixels = sum(histogram[args.threshold:])
    mean_error = sum(value * count for value, count in enumerate(histogram)) / total_pixels
    bbox = mask.getbbox()
    metrics = {
        "width": reference.width,
        "height": reference.height,
        "threshold": args.threshold,
        "changedPixels": changed_pixels,
        "changedRatio": changed_pixels / total_pixels,
        "meanAbsoluteLumaError": mean_error,
        "differenceBounds": None if bbox is None else {"x": bbox[0], "y": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]},
    }
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (args.output / "iteration.json").write_text(
        json.dumps(
            {
                "status": "ready-for-codex-review",
                "reference": str(args.reference.resolve()),
                "render": str(args.render.resolve()),
                "overlay": str((args.output / "overlay.png").resolve()),
                "diff": str((args.output / "diff.png").resolve()),
                "metrics": str((args.output / "metrics.json").resolve()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
