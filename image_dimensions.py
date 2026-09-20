"""Shared image canvas dimension normalization."""

from __future__ import annotations

import re
from copy import deepcopy


SIZE_PATTERN = re.compile(r"^(\d+)\s*[x×]\s*(\d+)$", re.IGNORECASE)
PROMPT_SIZE_PATTERN = re.compile(r"(?<!\d)(\d{3,5})\s*[x×]\s*(\d{3,5})(?!\d)")


def round_up_to_16(value: int) -> int:
    """Return the smallest positive multiple of 16 that is at least value."""
    value = int(value)
    if value < 1:
        raise ValueError("image dimensions must be positive")
    return ((value + 15) // 16) * 16


def normalize_size(value: str) -> str:
    """Normalize a WIDTHxHEIGHT value for image API requests."""
    text = str(value or "").strip().lower().replace("×", "x")
    match = SIZE_PATTERN.fullmatch(text)
    if not match:
        raise ValueError("image size must use WIDTHxHEIGHT format")
    width, height = (round_up_to_16(int(item)) for item in match.groups())
    return f"{width}x{height}"


def normalize_canvas(canvas: dict) -> dict:
    """Normalize a design-spec canvas while preserving other canvas metadata."""
    result = dict(canvas)
    if "width" in result:
        result["width"] = round_up_to_16(result["width"])
    if "height" in result:
        result["height"] = round_up_to_16(result["height"])
    return result


def normalize_design_spec(spec: dict) -> dict:
    """Return a copy whose authoritative canvas is API-compatible."""
    result = deepcopy(spec)
    if isinstance(result.get("canvas"), dict):
        result["canvas"] = normalize_canvas(result["canvas"])
    return result


def normalize_prompt_dimensions(prompt: str) -> str:
    """Round explicit large image canvas pairs in a generated prompt."""
    def replace(match: re.Match[str]) -> str:
        width = round_up_to_16(int(match.group(1)))
        height = round_up_to_16(int(match.group(2)))
        return f"{width}x{height}"

    return PROMPT_SIZE_PATTERN.sub(replace, str(prompt or ""))
