#!/usr/bin/env python3
"""MCP tools used by a manually created Codex session."""

from __future__ import annotations

import os
import secrets
import socket
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from pipeline_tasks import CodexTaskStore, write_json


ROOT = Path(os.environ.get("UI_PIPELINE_ROOT") or Path(__file__).resolve().parent).resolve()
STORE = CodexTaskStore(ROOT)
SESSION_CLAIMS: dict[str, str] = {}

mcp = MCPServer(
    "ui_asset_pipeline",
    instructions=(
        "Use these tools only for browser-created UI implementation and calibration tasks. "
        "Claim the exact task id supplied by the user, read its context and source revision, "
        "inspect the actual reference image and every approved asset image, classify each asset, "
        "write only the allowed source files, and submit the result for human review. "
        "For a persistent worker session, repeatedly call wait_for_next_task after each submission; "
        "an empty queue is a wait state, not a reason to end the session. "
        "Never read .env or API keys."
    ),
)


def claim_token(task_id: str) -> str:
    token = SESSION_CLAIMS.get(task_id)
    if not token:
        raise ValueError("当前 MCP 进程尚未领取此任务，请先调用 claim_task")
    return token


@mcp.tool()
def list_tasks(status: str = "queued") -> list[dict]:
    """List browser-created tasks. Pass an empty status to include every status."""
    return STORE.list_tasks(status=status or None)


@mcp.tool()
def wait_for_next_task(timeout_seconds: int = 50) -> dict:
    """Wait for a local task-created event, then return the oldest queued task. Repeat only for keepalive timeouts."""
    timeout = max(1, min(int(timeout_seconds), 55))
    deadline = time.monotonic() + timeout
    queued = STORE.list_tasks(status="queued")
    if queued:
        task = min(queued, key=lambda item: (item.get("createdAt", ""), item.get("id", "")))
        return {
            "task": task,
            "taskId": task["id"],
            "delivery": "already-queued",
            "next": "Call claim_task with this exact taskId, complete the task, submit it, then call wait_for_next_task again.",
            "keepAlive": True,
        }

    waiters_root = ROOT / "workspace" / ".mcp-waiters"
    waiters_root.mkdir(parents=True, exist_ok=True)
    registration = waiters_root / f"{os.getpid()}-{secrets.token_hex(8)}.json"
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    port = receiver.getsockname()[1]
    write_json(registration, {"pid": os.getpid(), "port": port, "createdAtEpoch": time.time()})
    try:
        # Close the race between the first queue read and callback registration.
        queued = STORE.list_tasks(status="queued")
        if queued:
            task = min(queued, key=lambda item: (item.get("createdAt", ""), item.get("id", "")))
            return {
                "task": task,
                "taskId": task["id"],
                "delivery": "registered-race-check",
                "next": "Call claim_task with this exact taskId, complete the task, submit it, then call wait_for_next_task again.",
                "keepAlive": True,
            }
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "task": None,
                    "delivery": "keepalive-timeout",
                    "next": "The event wait timed out only to keep the connection healthy. Call wait_for_next_task again unless the user explicitly told you to stop.",
                    "keepAlive": True,
                }
            receiver.settimeout(remaining)
            try:
                receiver.recvfrom(4096)
            except TimeoutError:
                continue
            queued = STORE.list_tasks(status="queued")
            if queued:
                task = min(queued, key=lambda item: (item.get("createdAt", ""), item.get("id", "")))
                return {
                    "task": task,
                    "taskId": task["id"],
                    "delivery": "local-event",
                    "next": "Call claim_task with this exact taskId, complete the task, submit it, then call wait_for_next_task again.",
                    "keepAlive": True,
                }
    finally:
        receiver.close()
        registration.unlink(missing_ok=True)


@mcp.tool()
def claim_task(task_id: str) -> dict:
    """Claim one exact task id for this MCP process before reading protected context or writing files."""
    task, token = STORE.claim_task(task_id)
    SESSION_CLAIMS[task_id] = token
    return {
        "task": task,
        "next": ["get_task_context", "get_reference_image", "list_asset_images"],
        "message": "任务已绑定到当前 MCP 会话。后续工具只需继续传 task_id。",
    }


@mcp.tool()
def get_task_context(task_id: str) -> dict:
    """Read the frozen design spec, page requirements, reference path, approved assets, and acceptance criteria."""
    return STORE.get_context(task_id, claim_token(task_id))


@mcp.tool()
def get_reference_image(task_id: str) -> Image:
    """Return the actual reference image pixels. This mandatory call is not equivalent to reading its path."""
    return Image(path=STORE.get_reference_image_path(task_id, claim_token(task_id)))


@mcp.tool()
def list_asset_images(task_id: str) -> dict:
    """List every approved asset that must be visually opened and classified before source can be written."""
    return STORE.list_asset_images(task_id, claim_token(task_id))


@mcp.tool()
def get_asset_image(task_id: str, asset_id: str) -> Image:
    """Return the actual pixels for one approved asset. Inspect all visible baked text and decoration."""
    return Image(path=STORE.get_asset_image_path(task_id, claim_token(task_id), asset_id))


@mcp.tool()
def record_asset_observation(
    task_id: str, asset_id: str, classification: str, contains_baked_text: bool, notes: str = ""
) -> dict:
    """Record visual classification after opening an asset: complete-composite, artwork-only, or mixed-or-uncertain."""
    return STORE.record_asset_observation(
        task_id, claim_token(task_id), asset_id, classification, contains_baked_text, notes
    )


@mcp.tool()
def get_visual_inspection_status(task_id: str) -> dict:
    """Show which mandatory reference/asset inspections remain before implementation source writes are allowed."""
    return STORE.visual_inspection_status(task_id, claim_token(task_id))


@mcp.tool()
def get_source_files(task_id: str) -> dict:
    """Read the current editable source bundle and its revision token."""
    return STORE.get_source_files(task_id, claim_token(task_id))


@mcp.tool()
def write_source_files(task_id: str, files: dict[str, str], expected_revision: str, note: str = "") -> dict:
    """Write a partial or complete source bundle with optimistic revision checking."""
    return STORE.write_source_files(task_id, claim_token(task_id), files, expected_revision, note)


@mcp.tool()
def create_comparison(task_id: str, note: str = "", threshold: int | None = None) -> dict:
    """Render the current calibration source and create immutable render, overlay, diff, and metrics artifacts."""
    return STORE.create_comparison(task_id, claim_token(task_id), note, threshold)


@mcp.tool()
def get_comparison(task_id: str, iteration: str = "latest") -> dict:
    """Return absolute artifact paths and metrics for one calibration iteration."""
    return STORE.get_comparison(task_id, claim_token(task_id), iteration)


@mcp.tool()
def submit_for_review(task_id: str, summary: str) -> dict:
    """Stop writing and submit the completed work to the browser for explicit human approval."""
    task = STORE.submit_for_review(task_id, claim_token(task_id), summary)
    SESSION_CLAIMS.pop(task_id, None)
    return task


@mcp.tool()
def report_task_error(task_id: str, error: str) -> dict:
    """Record an unrecoverable task error so the browser can display it."""
    task = STORE.report_error(task_id, claim_token(task_id), error)
    SESSION_CLAIMS.pop(task_id, None)
    return task


@mcp.tool()
def release_task(task_id: str) -> dict:
    """Release an unfinished claim so another Codex session can claim the task."""
    task = STORE.release_task(task_id, claim_token(task_id))
    SESSION_CLAIMS.pop(task_id, None)
    return task


if __name__ == "__main__":
    mcp.run(transport="stdio")
