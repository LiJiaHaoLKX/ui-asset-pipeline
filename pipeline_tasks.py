#!/usr/bin/env python3
"""Persistent Codex task exchange shared by the browser app and MCP server."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from image_dimensions import normalize_size
from urllib.parse import quote


SOURCE_FILES = ("index.html", "styles.css", "app.js")
ACTIVE_STATUSES = ("queued", "running", "awaiting-human-approval")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class SourceDocumentInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()
        self.charset = ""
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []

    def handle_decl(self, decl: str) -> None:
        if decl.lower().strip() == "doctype html":
            self.tags.add("!doctype")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag.lower())
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and attributes.get("charset"):
            self.charset = attributes["charset"].lower()
        if tag.lower() == "link" and "stylesheet" in attributes.get("rel", "").lower().split():
            self.stylesheets.append(attributes.get("href", ""))
        if tag.lower() == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"])


def validate_source_bundle(files: dict[str, str]) -> None:
    html = files.get("index.html", "")
    if not html.strip():
        return
    inspector = SourceDocumentInspector()
    inspector.feed(html)
    required = {"!doctype", "html", "head", "body"}
    missing = sorted(required - inspector.tags)
    if missing:
        raise ValueError(f"index.html 必须是完整 HTML 文档，缺少：{', '.join(missing)}")
    if inspector.charset not in {"utf-8", "utf8"}:
        raise ValueError("index.html 必须在 head 中声明 UTF-8 charset")
    if not any(Path(href.split("?", 1)[0]).name == "styles.css" and not href.startswith("/") for href in inspector.stylesheets):
        raise ValueError("index.html 必须使用相对路径引用 styles.css")
    if files.get("app.js", "").strip() and not any(Path(src.split("?", 1)[0]).name == "app.js" and not src.startswith("/") for src in inspector.scripts):
        raise ValueError("app.js 非空时，index.html 必须使用相对路径引用 app.js")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def notify_task_waiters(root: Path, task_id: str) -> int:
    """Wake locally registered MCP wait calls after a queued task is committed."""
    waiters_root = root.resolve() / "workspace" / ".mcp-waiters"
    if not waiters_root.is_dir():
        return 0
    payload = json.dumps({"event": "task-queued", "taskId": task_id}).encode("utf-8")
    notified = 0
    now = time.time()
    for registration in waiters_root.glob("*.json"):
        try:
            waiter = read_json(registration, {}) or {}
            if now - float(waiter.get("createdAtEpoch", 0)) > 120:
                registration.unlink(missing_ok=True)
                continue
            port = int(waiter.get("port", 0))
            if not 1 <= port <= 65535:
                raise ValueError("invalid waiter port")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as notifier:
                notifier.sendto(payload, ("127.0.0.1", port))
            notified += 1
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            registration.unlink(missing_ok=True)
    return notified


class CodexTaskStore:
    def __init__(self, root: Path, app_url: str | None = None):
        self.root = root.resolve()
        self.db_path = self.root / "workspace" / "tasks.db"
        self.app_url = (app_url or os.environ.get("UI_PIPELINE_APP_URL") or "http://127.0.0.1:8766").rstrip("/")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS codex_tasks (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL CHECK(type IN ('implementation', 'calibration')),
                    page_id TEXT NOT NULL,
                    page_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    claim_hash TEXT,
                    iteration_count INTEGER NOT NULL DEFAULT 0,
                    max_iterations INTEGER NOT NULL DEFAULT 8,
                    summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT,
                    context_path TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_codex_tasks_page_status
                    ON codex_tasks(page_id, status, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_codex_tasks_active_page
                    ON codex_tasks(page_id)
                    WHERE status IN ('queued', 'running', 'awaiting-human-approval');
                CREATE TABLE IF NOT EXISTS codex_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES codex_tasks(id)
                );
                CREATE TABLE IF NOT EXISTS codex_visual_inspections (
                    task_id TEXT NOT NULL,
                    image_kind TEXT NOT NULL CHECK(image_kind IN ('reference', 'asset')),
                    asset_id TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL DEFAULT '',
                    contains_baked_text INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    inspected_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, image_kind, asset_id),
                    FOREIGN KEY(task_id) REFERENCES codex_tasks(id)
                );
                """
            )

    def _page(self, page_id: str) -> dict:
        catalog = read_json(self.root / "workspace" / "project.json", {}) or {}
        page = next((item for item in catalog.get("pages", []) if item.get("id") == page_id), None)
        if not page:
            raise ValueError("页面不存在")
        return page

    def _page_root(self, page_id: str) -> Path:
        page = self._page(page_id)
        return self.root if page.get("storage") == "legacy" else self.root / "pages" / page_id

    def _task_row(self, task_id: str, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        if connection is not None:
            row = connection.execute("SELECT * FROM codex_tasks WHERE id = ?", (task_id,)).fetchone()
        else:
            with self._connection() as owned:
                row = owned.execute("SELECT * FROM codex_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Codex 任务不存在")
        return row

    @staticmethod
    def _public_task(row: sqlite3.Row, events: list[dict] | None = None) -> dict:
        task = {
            "id": row["id"],
            "type": row["type"],
            "pageId": row["page_id"],
            "pageName": row["page_name"],
            "status": row["status"],
            "sourceRevision": row["source_revision"],
            "iterationCount": row["iteration_count"],
            "maxIterations": row["max_iterations"],
            "summary": row["summary"],
            "error": row["error"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "claimedAt": row["claimed_at"],
            "completedAt": row["completed_at"],
        }
        if events is not None:
            task["events"] = events
        return task

    def _events(self, connection: sqlite3.Connection, task_id: str, limit: int = 12) -> list[dict]:
        rows = connection.execute(
            "SELECT event, message, details_json, created_at FROM codex_task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [
            {
                "event": row["event"],
                "message": row["message"],
                "details": json.loads(row["details_json"] or "{}"),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def _event(self, connection: sqlite3.Connection, task_id: str, event: str, message: str, details=None) -> None:
        connection.execute(
            "INSERT INTO codex_task_events(task_id, event, message, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, event, message, json.dumps(details or {}, ensure_ascii=False), now_iso()),
        )

    def source_bundle(self, page_id: str) -> dict:
        source_root = self._page_root(page_id) / "src"
        existence = {name: (source_root / name).is_file() for name in SOURCE_FILES}
        files = {name: (source_root / name).read_text(encoding="utf-8") if existence[name] else "" for name in SOURCE_FILES}
        digest = hashlib.sha256()
        for name in SOURCE_FILES:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(b"1" if existence[name] else b"0")
            digest.update(files[name].encode("utf-8"))
            digest.update(b"\0")
        return {"files": files, "revision": digest.hexdigest()[:20]}

    def _context(self, task_id: str, task_type: str, page: dict, source_revision: str, max_iterations: int) -> dict:
        page_root = self._page_root(page["id"])
        manifest_path = page_root / "assets" / "asset-manifest.json"
        manifest = read_json(manifest_path, {}) or {}
        approved_root = page_root / "assets" / "approved"
        assets = []
        for item in manifest.get("assets", []):
            asset_path = approved_root / str(item.get("file", ""))
            assets.append({**item, "absolutePath": str(asset_path.resolve()) if asset_path.is_file() else None})
        reference = page_root / "reference" / "reference-page.png"
        requirements_path = page_root / "prompts" / "reference-page.notes.txt"
        prompt_path = page_root / "prompts" / "reference-page.txt"
        relative_source = (page_root / "src" / "index.html").resolve().relative_to(self.root).as_posix()
        required_workflow = [
            "Read projectDesignSpec, pageRequirements, and aiGeneratedImagePrompt before making implementation decisions.",
            "Call get_reference_image to inspect the actual reference pixels, not only its file path.",
            "Call list_asset_images, then call get_asset_image and record_asset_observation for every approved asset.",
            "Verify each recorded elementType and processingMode against the pixels; record complete-composite, artwork-only, or mixed-or-uncertain for every raster asset.",
            "Read assetManifest.codeElements and implement those as code at their recorded placement and zIndex unless the reference contradicts the record.",
            "Only after all visual inputs are inspected, call get_source_files and write_source_files.",
        ]
        raster_rules = [
            "An approved asset may be a complete raster composite, not merely background artwork.",
            "When an asset visibly contains text, badges, buttons, labels, or decoration, preserve them in the raster image and do not recreate them in HTML/CSS.",
            "Implement text and controls in code only when they are structural UI and are not already baked into an approved asset.",
            "The AI-generated image prompt describes visual intent. It does not override what is visibly baked into approved raster assets.",
            "Use the approved activeVersion. Original and processed versions are audit references and must not be layered together.",
        ]
        acceptance = [
            "Inspect the actual reference image and every approved raster asset through MCP before writing source.",
            "Use approved raster assets according to their recorded visual classification and implement only remaining structural UI in code.",
            "Keep the fixed project canvas and preserve exact visible copy from the approved inputs.",
            "Write only index.html, styles.css, and app.js through MCP with revision checking.",
        ]
        if task_type == "calibration":
            acceptance.extend(
                [
                    "Inspect reference, render, overlay, diff, and metrics for every iteration.",
                    "Create a fresh comparison after each meaningful source adjustment.",
                    f"Stop after at most {max_iterations} comparison iterations and submit for human review.",
                ]
            )
        return {
            "taskId": task_id,
            "taskType": task_type,
            "requiredWorkflow": required_workflow,
            "rasterAssetRules": raster_rules,
            "projectDesignSpec": read_json(self.root / "workspace" / "design-spec.json", {}) or {},
            "pageRequirements": requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else "",
            "aiGeneratedImagePrompt": prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "",
            "projectRoot": str(self.root),
            "page": {"id": page["id"], "name": page["name"], "root": str(page_root.resolve())},
            # Compatibility aliases for tasks created by earlier browser versions.
            "designSpec": read_json(self.root / "workspace" / "design-spec.json", {}) or {},
            "settings": read_json(self.root / "workspace" / "settings.json", {}) or {},
            "requirements": requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else "",
            "imagePrompt": prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "",
            "referenceImage": str(reference.resolve()) if reference.is_file() else None,
            "assetManifestPath": str(manifest_path.resolve()) if manifest_path.is_file() else None,
            "assetManifest": {**manifest, "assets": assets},
            "allowedSourceFiles": list(SOURCE_FILES),
            "sourceRevision": source_revision,
            "previewUrl": f"{self.app_url}/files/{quote(relative_source)}",
            "acceptanceCriteria": acceptance,
        }

    def create_task(self, page_id: str, task_type: str, max_iterations: int = 8) -> dict:
        if task_type not in {"implementation", "calibration"}:
            raise ValueError("不支持的 Codex 任务类型")
        max_iterations = max(1, min(int(max_iterations), 20))
        page = self._page(page_id)
        page_root = self._page_root(page_id)
        if not (page_root / "reference" / "reference-page.png").is_file():
            raise ValueError("当前页面尚无参考图")
        if task_type == "implementation" and not (page_root / "assets" / "asset-manifest.json").is_file():
            raise ValueError("请先完成素材审核")
        if task_type == "calibration" and not all((page_root / "src" / name).is_file() for name in SOURCE_FILES):
            raise ValueError("请先完成并保存页面源码")
        with self._connection() as connection:
            active = connection.execute(
                "SELECT id FROM codex_tasks WHERE page_id = ? AND status IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1",
                (page_id, *ACTIVE_STATUSES),
            ).fetchone()
            if active:
                raise ValueError(f"当前页面已有未结束任务：{active['id']}")
        source_revision = self.source_bundle(page_id)["revision"]
        prefix = "impl" if task_type == "implementation" else "cal"
        task_id = f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        task_dir = page_root / "artifacts" / "codex" / "tasks" / task_id
        context_path = task_dir / "context.json"
        context = self._context(task_id, task_type, page, source_revision, max_iterations)
        write_json(context_path, context)
        stamp = now_iso()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO codex_tasks
                   (id, type, page_id, page_name, status, source_revision, max_iterations, created_at, updated_at, context_path)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (task_id, task_type, page_id, page["name"], source_revision, max_iterations, stamp, stamp, str(context_path)),
            )
            self._event(connection, task_id, "created", "浏览器创建了 Codex 任务")
            row = self._task_row(task_id, connection)
            task = self._public_task(row, self._events(connection, task_id))
        notify_task_waiters(self.root, task_id)
        return task

    def write_browser_source(self, page_id: str, files: dict, expected_revision: str) -> dict:
        if not isinstance(files, dict) or not files:
            raise ValueError("至少需要提供一个源码文件")
        unknown = set(files) - set(SOURCE_FILES)
        if unknown:
            raise ValueError(f"不允许写入源码文件：{', '.join(sorted(unknown))}")
        with self._connection() as connection:
            running = connection.execute(
                "SELECT id FROM codex_tasks WHERE page_id = ? AND status IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1",
                (page_id, *ACTIVE_STATUSES),
            ).fetchone()
            if running:
                raise ValueError(f"当前页面存在未结束任务 {running['id']}，请完成或取消任务后再从浏览器保存")
        current = self.source_bundle(page_id)
        if expected_revision != current["revision"]:
            raise RuntimeError(f"源码版本冲突，当前版本为 {current['revision']}，请重新加载源码")
        validate_source_bundle({**current["files"], **{name: str(content) for name, content in files.items()}})
        source_root = self._page_root(page_id) / "src"
        source_root.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            text = str(content)
            if len(text.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError(f"源码文件过大：{name}")
            temporary = source_root / f".{name}.{uuid.uuid4().hex}.tmp"
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(source_root / name)
        return {"ok": True, "revision": self.source_bundle(page_id)["revision"]}

    def list_tasks(self, page_id: str | None = None, status: str | None = None) -> list[dict]:
        clauses, values = [], []
        if page_id:
            clauses.append("page_id = ?")
            values.append(page_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM codex_tasks{where} ORDER BY created_at DESC", values).fetchall()
            return [self._public_task(row, self._events(connection, row["id"], 8)) for row in rows]

    def delete_page_tasks(self, page_id: str) -> int:
        self._page(page_id)
        with self._connection() as connection:
            rows = connection.execute("SELECT id FROM codex_tasks WHERE page_id = ?", (page_id,)).fetchall()
            task_ids = [row["id"] for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            connection.execute(f"DELETE FROM codex_visual_inspections WHERE task_id IN ({placeholders})", task_ids)
            connection.execute(f"DELETE FROM codex_task_events WHERE task_id IN ({placeholders})", task_ids)
            connection.execute(f"DELETE FROM codex_tasks WHERE id IN ({placeholders})", task_ids)
            return len(task_ids)

    def get_task(self, task_id: str, include_events: bool = True) -> dict:
        with self._connection() as connection:
            row = self._task_row(task_id, connection)
            return self._public_task(row, self._events(connection, task_id) if include_events else None)

    def claim_task(self, task_id: str) -> tuple[dict, str]:
        token = secrets.token_urlsafe(32)
        claim_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        stamp = now_iso()
        with self._connection() as connection:
            row = self._task_row(task_id, connection)
            if row["status"] != "queued":
                raise ValueError(f"任务当前状态为 {row['status']}，不能领取")
            connection.execute(
                "UPDATE codex_tasks SET status = 'running', claim_hash = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
                (claim_hash, stamp, stamp, task_id),
            )
            connection.execute("DELETE FROM codex_visual_inspections WHERE task_id = ?", (task_id,))
            self._event(connection, task_id, "claimed", "Codex 会话已领取任务")
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id)), token

    def _require_claim(self, connection: sqlite3.Connection, task_id: str, token: str) -> sqlite3.Row:
        row = self._task_row(task_id, connection)
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not row["claim_hash"] or not secrets.compare_digest(row["claim_hash"], actual):
            raise PermissionError("当前 MCP 会话没有此任务的写入权限")
        if row["status"] != "running":
            raise ValueError(f"任务当前状态为 {row['status']}，不能执行写操作")
        return row

    def get_context(self, task_id: str, token: str) -> dict:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            context = read_json(Path(row["context_path"]), {}) or {}
            context.setdefault("projectDesignSpec", context.get("designSpec", {}))
            context.setdefault("pageRequirements", context.get("requirements", ""))
            context.setdefault("aiGeneratedImagePrompt", context.get("imagePrompt", ""))
            context.setdefault("requiredWorkflow", [
                "Read projectDesignSpec, pageRequirements, and aiGeneratedImagePrompt before making implementation decisions.",
                "Call get_reference_image to inspect the actual reference pixels.",
                "Call list_asset_images, then get_asset_image and record_asset_observation for every approved asset.",
                "Only after all visual inputs are inspected and classified, call get_source_files and write_source_files.",
            ])
            context.setdefault("rasterAssetRules", [
                "If an approved asset contains text or other UI decoration, treat it as raster content and do not recreate that content in HTML/CSS.",
                "Only structural UI absent from approved assets should be implemented in code.",
            ])
            return context

    @staticmethod
    def _approved_assets(context: dict) -> list[dict]:
        return [
            asset for asset in context.get("assetManifest", {}).get("assets", [])
            if asset.get("approved", True) and asset.get("absolutePath")
        ]

    def list_asset_images(self, task_id: str, token: str) -> dict:
        context = self.get_context(task_id, token)
        assets = []
        for asset in self._approved_assets(context):
            target = asset.get("matchedSourceTarget") or {}
            assets.append({
                "id": asset.get("id"),
                "file": asset.get("file"),
                "pageBox": target.get("pageBox"),
                "placement": asset.get("placement"),
                "semanticDescription": asset.get("semanticDescription", ""),
                "containsBakedText": asset.get("containsBakedText"),
                "elementType": asset.get("elementType", "image-asset"),
                "processingMode": asset.get("processingMode", asset.get("extractionMethod")),
                "parent": asset.get("parent", ""),
                "zIndex": asset.get("zIndex"),
                "activeVersion": asset.get("activeVersion"),
                "classificationRequired": True,
            })
        return {
            "assets": assets,
            "requiredAction": "Open every asset with get_asset_image, then call record_asset_observation for it.",
            "bakedContentPolicy": "Text, badges, buttons, labels, and decoration visible inside a complete raster composite must not be recreated in HTML/CSS.",
        }

    def get_reference_image_path(self, task_id: str, token: str) -> Path:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            context = read_json(Path(row["context_path"]), {}) or {}
            path = Path(context.get("referenceImage") or "")
            if not path.is_file():
                raise ValueError("任务参考图不存在")
            connection.execute(
                "INSERT OR REPLACE INTO codex_visual_inspections(task_id, image_kind, asset_id, inspected_at) VALUES (?, 'reference', '', ?)",
                (task_id, now_iso()),
            )
            return path

    def get_asset_image_path(self, task_id: str, token: str, asset_id: str) -> Path:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            context = read_json(Path(row["context_path"]), {}) or {}
            asset = next((item for item in self._approved_assets(context) if item.get("id") == asset_id), None)
            if not asset:
                raise ValueError("当前任务没有这个已批准素材")
            path = Path(asset["absolutePath"])
            if not path.is_file():
                raise ValueError("素材图片不存在")
            connection.execute(
                "INSERT OR REPLACE INTO codex_visual_inspections(task_id, image_kind, asset_id, inspected_at) VALUES (?, 'asset', ?, ?)",
                (task_id, asset_id, now_iso()),
            )
            return path

    def record_asset_observation(
        self, task_id: str, token: str, asset_id: str, classification: str,
        contains_baked_text: bool, notes: str = "",
    ) -> dict:
        allowed = {"complete-composite", "artwork-only", "mixed-or-uncertain"}
        if classification not in allowed:
            raise ValueError(f"classification 必须是：{', '.join(sorted(allowed))}")
        with self._connection() as connection:
            self._require_claim(connection, task_id, token)
            inspected = connection.execute(
                "SELECT 1 FROM codex_visual_inspections WHERE task_id = ? AND image_kind = 'asset' AND asset_id = ?",
                (task_id, asset_id),
            ).fetchone()
            if not inspected:
                raise ValueError("请先调用 get_asset_image 实际查看该素材")
            connection.execute(
                "UPDATE codex_visual_inspections SET classification = ?, contains_baked_text = ?, notes = ?, inspected_at = ? WHERE task_id = ? AND image_kind = 'asset' AND asset_id = ?",
                (classification, int(contains_baked_text), notes.strip(), now_iso(), task_id, asset_id),
            )
            return {
                "assetId": asset_id,
                "classification": classification,
                "containsBakedText": contains_baked_text,
                "implementationRule": "Do not recreate baked text or decoration in HTML/CSS." if contains_baked_text else "Use this classification when deciding which structural UI remains to be coded.",
            }

    def visual_inspection_status(self, task_id: str, token: str) -> dict:
        context = self.get_context(task_id, token)
        expected = {str(asset.get("id")) for asset in self._approved_assets(context)}
        with self._connection() as connection:
            self._require_claim(connection, task_id, token)
            rows = connection.execute(
                "SELECT image_kind, asset_id, classification FROM codex_visual_inspections WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        reference_inspected = any(row["image_kind"] == "reference" for row in rows)
        inspected = {row["asset_id"] for row in rows if row["image_kind"] == "asset"}
        classified = {row["asset_id"] for row in rows if row["image_kind"] == "asset" and row["classification"]}
        return {
            "referenceInspected": reference_inspected,
            "missingAssetImages": sorted(expected - inspected),
            "missingAssetObservations": sorted(expected - classified),
            "readyToWrite": reference_inspected and expected <= classified,
        }

    def get_source_files(self, task_id: str, token: str) -> dict:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            return self.source_bundle(row["page_id"])

    def write_source_files(self, task_id: str, token: str, files: dict, expected_revision: str, note: str = "") -> dict:
        if not isinstance(files, dict) or not files:
            raise ValueError("至少需要提供一个源码文件")
        unknown = set(files) - set(SOURCE_FILES)
        if unknown:
            raise ValueError(f"不允许写入源码文件：{', '.join(sorted(unknown))}")
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            if row["type"] == "implementation":
                status = self.visual_inspection_status(task_id, token)
                if not status["readyToWrite"]:
                    raise ValueError(
                        "尚未完成强制视觉检查，不能写入源码。"
                        f" referenceInspected={status['referenceInspected']};"
                        f" missingAssetImages={status['missingAssetImages']};"
                        f" missingAssetObservations={status['missingAssetObservations']}"
                    )
            current = self.source_bundle(row["page_id"])
            if expected_revision != current["revision"]:
                raise RuntimeError(f"源码版本冲突，当前版本为 {current['revision']}，请重新读取后再修改")
            validate_source_bundle({**current["files"], **{name: str(content) for name, content in files.items()}})
            source_root = self._page_root(row["page_id"]) / "src"
            source_root.mkdir(parents=True, exist_ok=True)
            for name, content in files.items():
                text = str(content)
                if len(text.encode("utf-8")) > 2 * 1024 * 1024:
                    raise ValueError(f"源码文件过大：{name}")
                temporary = source_root / f".{name}.{uuid.uuid4().hex}.tmp"
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(source_root / name)
            updated = self.source_bundle(row["page_id"])
            stamp = now_iso()
            connection.execute(
                "UPDATE codex_tasks SET source_revision = ?, updated_at = ? WHERE id = ?",
                (updated["revision"], stamp, task_id),
            )
            self._event(
                connection,
                task_id,
                "source-written",
                note.strip() or f"Codex 更新了 {len(files)} 个源码文件",
                {"files": sorted(files), "revision": updated["revision"]},
            )
            return {"ok": True, "revision": updated["revision"], "files": sorted(files)}

    def _run(self, *parts: str, timeout: int = 180) -> str:
        completed = subprocess.run(list(parts), cwd=self.root, capture_output=True, text=True, timeout=timeout, check=False)
        output = "\n".join(value.strip() for value in (completed.stdout, completed.stderr) if value.strip())
        if completed.returncode:
            raise RuntimeError(output or f"Command failed with exit code {completed.returncode}")
        return output

    def create_comparison(self, task_id: str, token: str, note: str = "", threshold: int | None = None) -> dict:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            if row["type"] != "calibration":
                raise ValueError("只有校准任务可以创建对比迭代")
            if row["iteration_count"] >= row["max_iterations"]:
                raise ValueError("任务已达到最大校准轮数，请提交人工审核")
            page_root = self._page_root(row["page_id"])
            reference = page_root / "reference" / "reference-page.png"
            if not reference.is_file():
                raise ValueError("参考图不存在")
            iterations_root = page_root / "artifacts" / "iterations"
            identifiers = [int(item.name) for item in iterations_root.iterdir() if item.is_dir() and item.name.isdigit()] if iterations_root.exists() else []
            iteration = f"{max(identifiers, default=0) + 1:03d}"
            destination = iterations_root / iteration
            destination.mkdir(parents=True, exist_ok=False)
            settings = read_json(self.root / "workspace" / "settings.json", {}) or {}
            size = normalize_size(str(settings.get("finalSize", "752x1344")))
            width, height = (int(value) for value in size.split("x", 1))
            threshold_value = max(0, min(int(settings.get("threshold", 20) if threshold is None else threshold), 255))
            render = destination / "render.png"
            relative_source = (page_root / "src" / "index.html").resolve().relative_to(self.root).as_posix()
            source_url = f"{self.app_url}/files/{quote(relative_source)}"
            try:
                self._run(
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/render_page.ps1",
                    "-Url", source_url, "-Output", str(render), "-Width", str(width), "-Height", str(height), timeout=180,
                )
                output = self._run(
                    sys.executable, "scripts/compare_images.py", str(reference), str(render), str(destination),
                    "--threshold", str(threshold_value), timeout=180,
                )
                write_json(
                    destination / "adjustment.json",
                    {"note": note.strip(), "createdAt": now_iso(), "sourceRevision": self.source_bundle(row["page_id"])["revision"], "taskId": task_id},
                )
            except Exception as error:
                write_json(destination / "error.json", {"error": str(error), "createdAt": now_iso(), "taskId": task_id})
                raise
            stamp = now_iso()
            connection.execute(
                "UPDATE codex_tasks SET iteration_count = iteration_count + 1, updated_at = ? WHERE id = ?",
                (stamp, task_id),
            )
            self._event(connection, task_id, "comparison-created", f"生成对比迭代 {iteration}", {"iteration": iteration})
            result = self.get_comparison_files(row["page_id"], iteration)
            result["output"] = output
            return result

    def get_comparison(self, task_id: str, token: str, iteration: str = "latest") -> dict:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            return self.get_comparison_files(row["page_id"], iteration)

    def get_comparison_files(self, page_id: str, iteration: str = "latest") -> dict:
        root = self._page_root(page_id) / "artifacts" / "iterations"
        choices = sorted(item.name for item in root.iterdir() if item.is_dir() and item.name.isdigit()) if root.exists() else []
        selected = choices[-1] if iteration == "latest" and choices else iteration
        if not selected or selected == "latest" or selected not in choices:
            raise ValueError("对比迭代不存在")
        directory = root / selected
        files = {}
        for name in ("render.png", "overlay.png", "diff.png", "metrics.json", "adjustment.json"):
            path = directory / name
            files[name] = str(path.resolve()) if path.is_file() else None
        return {"iteration": selected, "files": files, "metrics": read_json(directory / "metrics.json", {}) or {}}

    def submit_for_review(self, task_id: str, token: str, summary: str) -> dict:
        with self._connection() as connection:
            row = self._require_claim(connection, task_id, token)
            stamp = now_iso()
            connection.execute(
                "UPDATE codex_tasks SET status = 'awaiting-human-approval', summary = ?, updated_at = ? WHERE id = ?",
                (summary.strip(), stamp, task_id),
            )
            self._event(connection, task_id, "submitted", "Codex 已提交人工审核", {"summary": summary.strip()})
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id))

    def report_error(self, task_id: str, token: str, error: str) -> dict:
        with self._connection() as connection:
            self._require_claim(connection, task_id, token)
            stamp = now_iso()
            connection.execute(
                "UPDATE codex_tasks SET status = 'failed', error = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (error.strip(), stamp, stamp, task_id),
            )
            self._event(connection, task_id, "failed", "Codex 报告任务失败", {"error": error.strip()})
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id))

    def release_task(self, task_id: str, token: str) -> dict:
        with self._connection() as connection:
            self._require_claim(connection, task_id, token)
            connection.execute(
                "UPDATE codex_tasks SET status = 'queued', claim_hash = NULL, claimed_at = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), task_id),
            )
            self._event(connection, task_id, "released", "Codex 会话释放了任务")
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id))

    def review_task(self, task_id: str, approved: bool, note: str = "") -> dict:
        with self._connection() as connection:
            row = self._task_row(task_id, connection)
            if row["status"] != "awaiting-human-approval":
                raise ValueError("任务当前不在等待人工审核状态")
            stamp = now_iso()
            if approved:
                connection.execute(
                    "UPDATE codex_tasks SET status = 'completed', claim_hash = NULL, summary = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                    (note.strip() or row["summary"], stamp, stamp, task_id),
                )
                self._event(connection, task_id, "approved", "用户在浏览器中批准了任务", {"note": note.strip()})
            else:
                connection.execute(
                    "UPDATE codex_tasks SET status = 'queued', claim_hash = NULL, claimed_at = NULL, summary = ?, updated_at = ? WHERE id = ?",
                    (note.strip(), stamp, task_id),
                )
                self._event(connection, task_id, "changes-requested", "用户要求 Codex 继续修改", {"note": note.strip()})
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id))

    def cancel_task(self, task_id: str) -> dict:
        with self._connection() as connection:
            row = self._task_row(task_id, connection)
            if row["status"] in TERMINAL_STATUSES:
                raise ValueError("任务已经结束")
            stamp = now_iso()
            connection.execute(
                "UPDATE codex_tasks SET status = 'cancelled', claim_hash = NULL, updated_at = ?, completed_at = ? WHERE id = ?",
                (stamp, stamp, task_id),
            )
            self._event(connection, task_id, "cancelled", "用户在浏览器中取消了任务")
            return self._public_task(self._task_row(task_id, connection), self._events(connection, task_id))
