#!/usr/bin/env python3
"""Split a transparent PNG into an unknown number of independent objects."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def binary_runs(mask: Image.Image) -> list[list[tuple[int, int]]]:
    """Return inclusive foreground runs for every image row."""
    pixels = mask.load()
    width, height = mask.size
    rows: list[list[tuple[int, int]]] = []

    for y in range(height):
        row: list[tuple[int, int]] = []
        x = 0
        while x < width:
            while x < width and pixels[x, y] == 0:
                x += 1
            if x == width:
                break
            start = x
            while x + 1 < width and pixels[x + 1, y] != 0:
                x += 1
            row.append((start, x))
            x += 1
        rows.append(row)

    return rows


def label_runs(
    rows: list[list[tuple[int, int]]],
) -> tuple[list[tuple[int, int, int, int]], list[list[int]], list[int]]:
    """Label run-length encoded pixels using 8-connected components."""
    runs: list[tuple[int, int, int, int]] = []
    row_ids: list[list[int]] = []
    parent: list[int] = []

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    previous: list[int] = []
    for y, row in enumerate(rows):
        current: list[int] = []
        previous_cursor = 0

        for x0, x1 in row:
            run_id = len(runs)
            runs.append((y, x0, x1, run_id))
            parent.append(run_id)
            current.append(run_id)

            while (
                previous_cursor < len(previous)
                and runs[previous[previous_cursor]][2] < x0 - 1
            ):
                previous_cursor += 1

            cursor = previous_cursor
            while cursor < len(previous):
                previous_id = previous[cursor]
                _, previous_x0, previous_x1, _ = runs[previous_id]
                if previous_x0 > x1 + 1:
                    break
                if previous_x1 >= x0 - 1:
                    union(run_id, previous_id)
                cursor += 1

        row_ids.append(current)
        previous = current

    roots = [find(index) for index in range(len(runs))]
    return runs, row_ids, roots


def reading_order(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    """Order objects top-to-bottom, then left-to-right within overlapping rows."""
    rows: list[dict[str, object]] = []
    for item in sorted(objects, key=lambda value: value["bbox"][1]):
        left, top, right, bottom = item["bbox"]
        height = bottom - top
        best_row: dict[str, object] | None = None
        best_overlap = 0

        for row in rows:
            overlap = min(bottom, row["bottom"]) - max(top, row["top"])
            row_height = row["bottom"] - row["top"]
            if overlap > best_overlap and overlap >= min(height, row_height) / 2:
                best_row = row
                best_overlap = overlap

        if best_row is None:
            rows.append({"top": top, "bottom": bottom, "items": [item]})
        else:
            best_row["top"] = min(best_row["top"], top)
            best_row["bottom"] = max(best_row["bottom"], bottom)
            best_row["items"].append(item)

    ordered: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda value: value["top"]):
        ordered.extend(sorted(row["items"], key=lambda value: value["bbox"][0]))
    return ordered


def split_objects(
    source: Path,
    output_dir: Path,
    alpha_threshold: int,
    join_gap: int,
    min_area: int,
    padding: int,
    edge_halo: int,
    expected_count: int | None = None,
) -> list[dict[str, object]]:
    image = Image.open(source).convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] == 255:
        raise ValueError("Input has no transparent pixels; an alpha-background PNG is required.")

    foreground = alpha.point(lambda value: 255 if value >= alpha_threshold else 0)
    grouping_mask = foreground
    if join_gap:
        grouping_mask = foreground.filter(ImageFilter.MaxFilter(join_gap * 2 + 1))

    grouped_rows = binary_runs(grouping_mask)
    grouped_runs, grouped_row_ids, grouped_roots = label_runs(grouped_rows)
    original_rows = binary_runs(foreground)

    object_runs: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    object_area: dict[int, int] = defaultdict(int)

    for y, original_row in enumerate(original_rows):
        group_ids = grouped_row_ids[y]
        group_cursor = 0
        for x0, x1 in original_row:
            while grouped_runs[group_ids[group_cursor]][2] < x0:
                group_cursor += 1
            group_id = group_ids[group_cursor]
            _, group_x0, group_x1, _ = grouped_runs[group_id]
            if not (group_x0 <= x0 and group_x1 >= x1):
                raise RuntimeError("Could not map foreground pixels to a grouped component.")
            root = grouped_roots[group_id]
            object_runs[root].append((y, x0, x1))
            object_area[root] += x1 - x0 + 1

    width, height = image.size
    objects: list[dict[str, object]] = []
    for root, runs in object_runs.items():
        area = object_area[root]
        left = min(run[1] for run in runs)
        top = min(run[0] for run in runs)
        right = max(run[2] for run in runs) + 1
        bottom = max(run[0] for run in runs) + 1
        outer_margin = max(padding, edge_halo)
        crop_box = (
            max(0, left - outer_margin),
            max(0, top - outer_margin),
            min(width, right + outer_margin),
            min(height, bottom + outer_margin),
        )
        objects.append(
            {
                "root": root,
                "runs": runs,
                "area": area,
                "bbox": crop_box,
            }
        )

    if expected_count is not None:
        # Internal strokes enclosed by an icon outline belong to that icon.
        # Merge masks, not bounding-box pixels, so transparency is preserved.
        groups = []
        for item in sorted(objects, key=lambda obj: (obj['bbox'][2] - obj['bbox'][0]) * (obj['bbox'][3] - obj['bbox'][1]), reverse=True):
            left, top, right, bottom = item['bbox']
            owners = [group for group in groups if group['bbox'][0] <= left and group['bbox'][1] <= top and group['bbox'][2] >= right and group['bbox'][3] >= bottom]
            if owners:
                owner = min(owners, key=lambda obj: (obj['bbox'][2] - obj['bbox'][0]) * (obj['bbox'][3] - obj['bbox'][1]))
                owner['runs'].extend(item['runs'])
                owner['area'] += item['area']
            else:
                groups.append(item)
        objects = groups
    objects = reading_order([item for item in objects if item['area'] >= min_area])
    if expected_count is not None and len(objects) > expected_count and join_gap == 0:
        # Join narrowly separated strokes, preserving the original alpha pixels.
        return split_objects(source, output_dir, alpha_threshold, 4, min_area, padding, edge_halo, expected_count)
    if expected_count is not None and len(objects) != expected_count:
        raise ValueError(f"红框有 {expected_count} 个，但识别到 {len(objects)} 组素材。已停止拆分，请检查图标是否散开、合并或漏生成；不会将笔画碎片作为独立素材进入审核。")
    output_dir.mkdir(parents=True, exist_ok=True)
    for previous in output_dir.glob("object-*.png"):
        previous.unlink()
    manifest: list[dict[str, object]] = []

    for index, item in enumerate(objects, start=1):
        left, top, right, bottom = item["bbox"]
        cropped = image.crop((left, top, right, bottom))

        membership = Image.new("L", cropped.size, 0)
        draw = ImageDraw.Draw(membership)
        for y, x0, x1 in item["runs"]:
            draw.line((x0 - left, y - top, x1 - left, y - top), fill=255)
        if edge_halo:
            membership = membership.filter(ImageFilter.MaxFilter(edge_halo * 2 + 1))
        cropped.putalpha(ImageChops.multiply(cropped.getchannel("A"), membership))

        filename = f"object-{index:03d}.png"
        cropped.save(output_dir / filename, optimize=True)
        manifest.append(
            {
                "file": filename,
                "bbox": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                "foreground_area": item["area"],
            }
        )

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split disconnected objects from a transparent PNG into separate RGBA PNGs."
    )
    parser.add_argument("input", type=Path, help="Input PNG with a transparent background")
    parser.add_argument("output", type=Path, help="Directory for extracted PNG files")
    parser.add_argument("--expected-count", type=int, help="One complete asset per marked target; group enclosed strokes and reject count mismatches")
    parser.add_argument(
        "--alpha-threshold", type=int, default=8, choices=range(1, 256), metavar="1..255"
    )
    parser.add_argument(
        "--join-gap",
        type=int,
        default=0,
        help="Expand each core by this radius before grouping nearby pieces (default: 0)",
    )
    parser.add_argument(
        "--min-area", type=int, default=16, help="Ignore smaller foreground regions (default: 16)"
    )
    parser.add_argument(
        "--padding", type=int, default=4, help="Transparent padding around each output (default: 4)"
    )
    parser.add_argument(
        "--edge-halo",
        type=int,
        default=4,
        help="Preserve faint edge pixels this far outside the detected core (default: 4)",
    )
    args = parser.parse_args()
    if args.join_gap < 0 or args.min_area < 1 or args.padding < 0 or args.edge_halo < 0:
        parser.error(
            "--join-gap, --padding and --edge-halo must be >= 0; --min-area must be >= 1"
        )
    return args


def main() -> None:
    args = parse_args()
    results = split_objects(
        args.input,
        args.output,
        args.alpha_threshold,
        args.join_gap,
        args.min_area,
        args.padding,
        args.edge_halo,
        args.expected_count,
    )
    print(f"Extracted {len(results)} object(s) into: {args.output.resolve()}")


if __name__ == "__main__":
    main()
