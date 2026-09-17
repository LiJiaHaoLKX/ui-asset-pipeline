#!/usr/bin/env python3
"""Render a compact checkerboard contact sheet from an asset review manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--columns", type=int, default=4)
    args = parser.parse_args()
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    assets = document["assets"]
    cell_width, image_height, label_height, gap = 300, 230, 54, 16
    rows = (len(assets) + args.columns - 1) // args.columns
    sheet = Image.new("RGB", (gap + args.columns * (cell_width + gap), gap + rows * (image_height + label_height + gap)), "#101820")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    for index, asset in enumerate(assets):
        column, row = index % args.columns, index // args.columns
        left = gap + column * (cell_width + gap)
        top = gap + row * (image_height + label_height + gap)
        checker = Image.new("RGB", (cell_width, image_height), "#d8dde2")
        checker_draw = ImageDraw.Draw(checker)
        block = 20
        for y in range(0, image_height, block):
            for x in range(0, cell_width, block):
                if (x // block + y // block) % 2:
                    checker_draw.rectangle((x, y, x + block - 1, y + block - 1), fill="#aeb7bf")
        with Image.open(args.manifest.parent / asset["file"]).convert("RGBA") as image:
            image.thumbnail((cell_width - 20, image_height - 20), Image.Resampling.LANCZOS)
            checker.paste(image, ((cell_width - image.width) // 2, (image_height - image.height) // 2), image)
        sheet.paste(checker, (left, top))
        draw.text((left + 8, top + image_height + 9), f"{index + 1:02d}  {asset['name']}", fill="#f2f6f8", font=font)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output, optimize=True)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
