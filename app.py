#!/usr/bin/env python3
"""Local browser application for the UI asset pipeline."""

from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image

from pipeline_tasks import CodexTaskStore


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CATALOG_PATH = ROOT / "workspace" / "project.json"
PROJECT_DESIGN_PATH = ROOT / "workspace" / "design-spec.json"
PROJECT_SETTINGS_PATH = ROOT / "workspace" / "settings.json"
ENV_PATH = ROOT / ".env"
SOURCE_FILES = {"index.html", "styles.css", "app.js"}
SAFE_FILE_ROOTS = {"reference", "regions", "assets", "artifacts", "src", "prompts", "runs", "pages"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GENERATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
PAGE_CONTEXT = threading.local()
TASK_STORE = CodexTaskStore(ROOT)
DEFAULT_SETTINGS = {
    "generationSize": "750x1334",
    "extractionSize": "1024x1024",
    "finalSize": "750x1334",
    "quality": "medium",
    "padding": 32,
    "threshold": 20,
    "codexMaxIterations": 8,
}
ELEMENT_TYPES = {"image-asset", "code-element", "complete-composite"}
PROCESSING_MODES = {"none", "complete-crop", "local-transparent", "ai-transparent", "background-repair"}
LEGACY_PROCESSING_MODES = {"ai": "ai-transparent", "background-script": "local-transparent"}


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


def vision_image_data_url(path: Path, quality: int = 82) -> tuple[str, dict]:
    """Create a same-size JPEG transport copy without modifying the reference PNG."""
    with Image.open(path) as source:
        rgba = source.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, "white")
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        output = io.BytesIO()
        flattened.save(output, format="JPEG", quality=quality, optimize=True, subsampling=0)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}", {
        "sourceBytes": path.stat().st_size,
        "transportBytes": len(output.getvalue()),
        "width": flattened.width,
        "height": flattened.height,
        "format": "jpeg",
        "quality": quality,
    }


def post_chat_completion(base_url: str, api_key: str, payload: dict, run_dir: Path, *, attempts: int = 3) -> bytes:
    from scripts.request_diagnostics import record
    retryable_statuses = {429, 500, 502, 503, 504, 524}
    active_payload = {**payload, "stream": True}
    removed_response_format = False
    for attempt in range(1, attempts + 1):
        logged_payload = json.loads(json.dumps(active_payload))
        for message in logged_payload.get("messages", []):
            if isinstance(message.get("content"), list):
                for item in message["content"]:
                    if item.get("type") == "image_url":
                        item["image_url"]["url"] = "[reference-image-data]"
        write_json(run_dir / f"request-attempt-{attempt}.json", logged_payload)
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(active_payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_type = response.headers.get_content_type() if hasattr(response.headers, 'get_content_type') else ''
                if content_type == 'text/event-stream':
                    chunks = []
                    with (run_dir / f"stream-attempt-{attempt}.jsonl").open('w', encoding='utf-8') as stream_log:
                        while True:
                            line = response.readline()
                            if not line:
                                break
                            text = line.decode('utf-8', errors='replace').strip()
                            if not text.startswith('data:'):
                                continue
                            data = text[5:].strip()
                            if data == '[DONE]':
                                break
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            stream_log.write(json.dumps(event, ensure_ascii=False) + '\n')
                            for choice in event.get('choices', []):
                                delta = choice.get('delta') or {}
                                value = delta.get('content', '')
                                if isinstance(value, list):
                                    value = ''.join(item.get('text', '') for item in value if isinstance(item, dict))
                                if value:
                                    chunks.append(str(value))
                    result = ''.join(chunks)
                    return json.dumps({'choices': [{'message': {'content': result}}]}, ensure_ascii=False).encode('utf-8')
                return response.read()
        except urllib.error.HTTPError as error:
            error_body = error.read()
            (run_dir / f"error-response-attempt-{attempt}.txt").write_bytes(error_body)
            readable = error_body.decode("utf-8", errors="replace").strip()
            if error.code == 400 and "response_format" in readable.lower() and not removed_response_format:
                active_payload = {key: value for key, value in active_payload.items() if key != "response_format"}
                removed_response_format = True
                continue
            if error.code in retryable_statuses and attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            if error.code == 524:
                failure = RuntimeError(f"文本模型中转站等待上游超时（HTTP 524），已尝试 {attempt} 次。请稍后重试，或在项目设置切换响应更快的文本模型；原提示词未覆盖。")
                record(failure, run_dir, stage='文本模型请求', url=request.full_url, elapsed=None, headers=error.headers, body=error_body)
                raise failure from error
            detail = re.sub(r"\s+", " ", readable)[:500]
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(f"文本模型网关返回 HTTP {error.code}{suffix}") from error
        except TimeoutError as error:
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            failure = RuntimeError(f"文本模型响应超时，已尝试 {attempt} 次，请稍后重试或更换文本模型")
            record(error, run_dir, stage='文本模型请求', url=request.full_url)
            failure.diagnostic = error.diagnostic
            raise failure from error
        except ConnectionResetError as error:
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            failure = RuntimeError(f"文本模型连接被远端关闭（WinError 10054），已尝试 {attempt} 次。请检查文本 API 地址、网关连接或稍后重试；原提示词未覆盖。")
            record(error, run_dir, stage='文本模型请求', url=request.full_url)
            failure.diagnostic = error.diagnostic
            raise failure from error
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, ConnectionResetError) or getattr(reason, 'winerror', None) == 10054:
                if attempt < attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                failure = RuntimeError(f"文本模型连接被远端关闭（WinError 10054），已尝试 {attempt} 次。请检查文本 API 地址、网关连接或稍后重试；原提示词未覆盖。")
                record(error, run_dir, stage='文本模型请求', url=request.full_url)
                failure.diagnostic = error.diagnostic
                raise failure from error
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            failure = RuntimeError(f"无法连接文本模型网关：{error.reason}")
            record(error, run_dir, stage='文本模型请求', url=request.full_url)
            failure.diagnostic = error.diagnostic
            raise failure from error
    raise RuntimeError("文本模型请求失败")


def fetch_gateway_models(base_url: str, api_key: str) -> list[str]:
    base_url = base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("图片 API 地址必须是有效的 HTTP 或 HTTPS 地址")
    if not api_key:
        raise ValueError("请填写 API Key，或先保存已有密钥")
    request = urllib.request.Request(
        base_url + "/models",
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"图片模型中转站返回 HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接图片模型中转站：{error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("图片模型中转站返回了无效的 JSON") from error

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("图片模型中转站响应中缺少 data 列表")
    models: list[str] = []
    seen: set[str] = set()
    for item in items:
        model_id = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    if not models:
        raise RuntimeError("图片模型中转站没有返回可选模型")
    return models


def normalize_target(target: dict, *, suggested: bool = False) -> dict:
    result = dict(target)
    processing_mode = result.get("processingMode") or result.get("extractionMode") or "ai-transparent"
    processing_mode = LEGACY_PROCESSING_MODES.get(str(processing_mode), str(processing_mode))
    element_type = str(result.get("elementType") or "")
    if element_type not in ELEMENT_TYPES:
        element_type = "complete-composite" if processing_mode in {"complete-crop", "background-repair"} else "image-asset"
    if element_type == "code-element":
        processing_mode = "none"
    elif processing_mode == "none":
        processing_mode = "complete-crop" if element_type == "complete-composite" else "local-transparent"
    result["elementType"] = element_type
    result["processingMode"] = processing_mode
    result["extractionMode"] = {
        "ai-transparent": "ai",
        "local-transparent": "background-script",
    }.get(processing_mode, processing_mode)
    result.setdefault("name", "")
    result.setdefault("parent", "")
    result.setdefault("zIndex", None)
    result.setdefault("approved", True)
    result.setdefault("backgroundTolerance", 34)
    result.setdefault("edgeFeather", 48)
    result.setdefault("suggestion", {"source": "ai" if suggested else "manual"})
    result.setdefault("reviewStatus", "needs-review" if suggested else "confirmed")
    return result


def normalize_regions_document(document: dict, *, suggested: bool = False) -> dict:
    result = dict(document or {})
    regions = []
    for region_index, source_region in enumerate(result.get("regions", []), start=1):
        region = dict(source_region)
        region.setdefault("id", f"region-{region_index:03d}")
        region.setdefault("purpose", "")
        region.setdefault("approved", True)
        region["targets"] = [
            {**normalize_target(target, suggested=suggested), "id": target.get("id") or f"target-{target_index:03d}"}
            for target_index, target in enumerate(region.get("targets", []), start=1)
        ]
        regions.append(region)
    result["regions"] = regions
    return result


def pending_raster_targets(document: dict) -> list[dict]:
    normalized = normalize_regions_document(document or {})
    return [
        target
        for region in normalized.get("regions", []) if region.get("approved", True)
        for target in region.get("targets", [])
        if target.get("approved", True)
        and target.get("elementType") in {"image-asset", "complete-composite"}
        and target.get("reviewStatus", "confirmed") != "confirmed"
    ]


def require_raster_targets_confirmed(root: Path) -> None:
    pending = pending_raster_targets(read_json(root / "regions" / "regions.json", {}) or {})
    if not pending:
        return
    labels = [target.get("name") or target.get("id") or "未命名元素" for target in pending]
    preview = "、".join(labels[:5]) + ("…" if len(labels) > 5 else "")
    raise ValueError(f"还有 {len(pending)} 个图片素材或完整复合图片未人工确认：{preview}。请返回区域标注逐项确认")


def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def write_env(updates: dict[str, str]) -> None:
    current = read_env()
    for key, value in updates.items():
        if value:
            if "\n" in value or "\r" in value:
                raise ValueError("Environment values cannot contain line breaks")
            current[key] = value
    ordered = ["GPT_IMAGE_BASE_URL", "GPT_IMAGE_API_KEY", "GPT_IMAGE_MODEL", "GPT_TEXT_BASE_URL", "GPT_TEXT_API_KEY", "GPT_TEXT_MODEL"]
    lines = [f"{key}={current.get(key, '')}" for key in ordered]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_catalog() -> dict:
    catalog = read_json(CATALOG_PATH)
    if catalog:
        return catalog
    stamp = now_iso()
    catalog = {"version": 2, "activePageId": "game-home", "pages": [{"id": "game-home", "name": "游戏首页", "storage": "legacy", "createdAt": stamp, "updatedAt": stamp}]}
    write_json(CATALOG_PATH, catalog)
    return catalog


def save_catalog(catalog: dict) -> None:
    catalog["updatedAt"] = now_iso()
    write_json(CATALOG_PATH, catalog)


def ensure_project_config() -> None:
    if not PROJECT_DESIGN_PATH.exists():
        source = ROOT / "design-spec.json"
        spec = read_json(source, {}) if source.exists() else read_json(ROOT / "design-spec.example.json", {})
        write_json(PROJECT_DESIGN_PATH, spec or {})
    if not PROJECT_SETTINGS_PATH.exists():
        legacy_state = read_json(ROOT / "workspace" / "state.json", {}) or {}
        legacy_settings = legacy_state.get("settings", {})
        settings = {**DEFAULT_SETTINGS, **legacy_settings}
        if "size" in legacy_settings and "generationSize" not in legacy_settings:
            settings["generationSize"] = legacy_settings["size"]
        settings.pop("size", None)
        write_json(PROJECT_SETTINGS_PATH, settings)
    state_paths = [ROOT / "workspace" / "state.json", *(ROOT / "pages").glob("*/state.json")]
    for path in state_paths:
        state = read_json(path, {}) or {}
        if "settings" in state:
            state.pop("settings", None)
            write_json(path, state)


def load_project_design() -> dict:
    ensure_project_config()
    return read_json(PROJECT_DESIGN_PATH, {}) or {}


def save_project_design(spec: dict) -> None:
    write_json(PROJECT_DESIGN_PATH, spec)
    write_json(ROOT / "design-spec.json", spec)


def load_project_settings() -> dict:
    ensure_project_config()
    stored = read_json(PROJECT_SETTINGS_PATH, {}) or {}
    return {**DEFAULT_SETTINGS, **stored}


def save_project_settings(settings: dict) -> None:
    write_json(PROJECT_SETTINGS_PATH, {**DEFAULT_SETTINGS, **settings})


def page_by_id(page_id: str | None = None) -> dict:
    catalog = ensure_catalog()
    target = page_id or catalog["activePageId"]
    page = next((item for item in catalog["pages"] if item["id"] == target), None)
    if not page:
        raise ValueError("页面不存在")
    return page


def public_page(page: dict) -> dict:
    return {key: page.get(key) for key in ("id", "name", "createdAt", "updatedAt")}


def root_for_page(page: dict) -> Path:
    return ROOT if page.get("storage") == "legacy" else ROOT / "pages" / page["id"]


def current_page() -> dict:
    return page_by_id(getattr(PAGE_CONTEXT, "page_id", None))


def data_root() -> Path:
    return root_for_page(current_page())


def state_path() -> Path:
    page = current_page()
    return ROOT / "workspace" / "state.json" if page.get("storage") == "legacy" else data_root() / "state.json"


def create_page(name: str, requested_id: str = "") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("页面名称不能为空")
    catalog = ensure_catalog()
    if requested_id:
        page_id = requested_id.strip().lower()
        if not NAME_PATTERN.fullmatch(page_id):
            raise ValueError("页面标识必须使用小写字母、数字和连字符")
    else:
        used = {item["id"] for item in catalog["pages"]}
        serial = 1
        while f"page-{serial:03d}" in used:
            serial += 1
        page_id = f"page-{serial:03d}"
    if any(item["id"] == page_id for item in catalog["pages"]):
        raise ValueError("页面标识已存在")
    root = ROOT / "pages" / page_id
    if root.exists():
        raise ValueError("页面目录已存在")
    for relative in ("prompts", "reference", "regions", "assets/approved", "artifacts/iterations", "runs", "src"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "reference-page.txt").write_text("", encoding="utf-8")
    (root / "prompts" / "reference-page.notes.txt").write_text("", encoding="utf-8")
    stamp = now_iso()
    page = {"id": page_id, "name": name, "storage": "page", "createdAt": stamp, "updatedAt": stamp}
    catalog["pages"].append(page)
    catalog["activePageId"] = page_id
    save_catalog(catalog)
    PAGE_CONTEXT.page_id = page_id
    save_state(load_state())
    return public_page(page)


def infer_phase() -> str:
    root = data_root()
    iterations = root / "artifacts" / "iterations"
    if iterations.exists() and any((item / "metrics.json").exists() for item in iterations.iterdir() if item.is_dir()):
        return "comparison-ready"
    if (root / "src" / "index.html").exists() and (root / "assets" / "asset-manifest.json").exists():
        return "page-generated"
    if (root / "assets" / "asset-manifest.json").exists():
        return "assets-approved"
    if (root / "regions" / "regions.json").exists():
        return "regions-marked"
    if (root / "reference" / "reference-page.png").exists():
        return "reference-approved"
    return "draft-reference"


def load_state() -> dict:
    state = read_json(state_path(), {}) or {}
    state.pop("settings", None)
    state.setdefault("phase", infer_phase())
    state.setdefault("referenceApproved", (data_root() / "reference" / "reference-page.png").exists())
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    state["updatedAt"] = now_iso()
    write_json(state_path(), state)


def update_phase(phase: str, detail: str = "") -> None:
    state = load_state()
    state["phase"] = phase
    state["history"].append({"phase": phase, "detail": detail, "at": now_iso()})
    state["history"] = state["history"][-50:]
    save_state(state)
    catalog = ensure_catalog()
    page = next(item for item in catalog["pages"] if item["id"] == current_page()["id"])
    page["updatedAt"] = now_iso()
    save_catalog(catalog)


def safe_project_path(relative: str, allowed_roots: set[str] | None = None) -> Path:
    clean = Path(unquote(relative.replace("\\", "/")))
    if clean.is_absolute() or ".." in clean.parts or not clean.parts:
        raise ValueError("Invalid project path")
    allowed = allowed_roots or SAFE_FILE_ROOTS
    if clean.parts[0] not in allowed or clean.parts[0].startswith("."):
        raise ValueError("Path is outside an allowed project directory")
    resolved = (ROOT / clean).resolve()
    if ROOT.resolve() not in resolved.parents:
        raise ValueError("Path escapes the project")
    return resolved


def command(*parts: str, timeout: int = 600) -> str:
    completed = subprocess.run(
        list(parts), cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    output = "\n".join(value.strip() for value in (completed.stdout, completed.stderr) if value.strip())
    if completed.returncode:
        raise RuntimeError(output or f"Command failed with exit code {completed.returncode}")
    return output


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in {"traceback"}}


def create_job(kind: str, task) -> str:
    job_id = uuid.uuid4().hex[:12]
    page = current_page().copy()
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "kind": kind, "pageId": page["id"], "pageName": page["name"], "status": "queued", "createdAt": now_iso(), "message": "等待执行"}

    def runner() -> None:
        PAGE_CONTEXT.page_id = page["id"]
        with JOBS_LOCK:
            JOBS[job_id].update(status="running", startedAt=now_iso(), message="正在执行")
        try:
            result = task() or {}
            with JOBS_LOCK:
                JOBS[job_id].update(status="completed", completedAt=now_iso(), message="执行完成", result=result)
        except Exception as error:
            from scripts.request_diagnostics import diagnose
            diagnostic = getattr(error, 'diagnostic', None) or diagnose(error, '任务执行（未记录更细阶段）')
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="failed", completedAt=now_iso(), message=str(error), traceback=traceback.format_exc(), diagnostic=diagnostic
                )
                write_json(root_for_page(page) / 'runs' / 'job-failures' / (job_id + '.json'), public_job(JOBS[job_id]))

    threading.Thread(target=runner, name=f"pipeline-{kind}-{job_id}", daemon=True).start()
    return job_id


def file_url(path: Path) -> str:
    return "/files/" + path.resolve().relative_to(ROOT.resolve()).as_posix()


def image_info(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with Image.open(path) as image:
            return {"url": file_url(path), "width": image.width, "height": image.height, "modified": path.stat().st_mtime_ns}
    except OSError:
        return None


def list_crops() -> list[dict]:
    root = data_root()
    normalized = normalize_regions_document(read_json(root / "regions" / "crops-web" / "regions.normalized.json", {}) or {})
    result = []
    for region in normalized.get("regions", []):
        clean = root / "regions" / "crops-web" / region["cropFile"]
        marked = root / "regions" / "crops-web" / region["markedCropFile"]
        targets = [
            target for target in region.get("targets", [])
            if target.get("approved", True) and target.get("reviewStatus", "confirmed") == "confirmed"
        ]
        counts = {
            mode: len([target for target in targets if target.get("processingMode") == mode])
            for mode in PROCESSING_MODES
        }
        result.append({
            "id": region["id"], "purpose": region.get("purpose", ""), "targetCount": len(targets),
            "aiTargetCount": counts["ai-transparent"], "scriptTargetCount": counts["local-transparent"],
            "completeCropCount": counts["complete-crop"], "backgroundRepairCount": counts["background-repair"],
            "codeElementCount": counts["none"], "processingCounts": counts,
            "cleanUrl": file_url(clean), "markedUrl": file_url(marked),
        })
    return result


def directory_has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def recrop_impact(root: Path, page_id: str, codex_tasks: list[dict] | None = None) -> dict:
    tasks = codex_tasks if codex_tasks is not None else TASK_STORE.list_tasks(page_id=page_id)
    return {
        "hasExistingCrops": (root / "regions" / "crops-web" / "regions.normalized.json").is_file(),
        "assetReview": directory_has_files(root / "assets"),
        "pageImplementation": directory_has_files(root / "src") or any(task.get("type") == "implementation" for task in tasks),
        "comparisonCalibration": directory_has_files(root / "artifacts" / "iterations") or any(task.get("type") == "calibration" for task in tasks),
        "codexTaskCount": len(tasks),
    }


def clear_recrop_downstream(root: Path) -> None:
    for name in ("assets", "src", "artifacts"):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
    for relative in ("assets/approved", "src", "artifacts/iterations"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def assets_summary() -> dict:
    root = data_root()
    draft_path = root / "assets" / "split-web" / "asset-manifest.draft.json"
    review_path = root / "assets" / "asset-manifest.json"
    manifest_path = draft_path if draft_path.exists() else review_path
    document = read_json(manifest_path, {}) or {}
    reviewed = read_json(review_path, {}) or {}
    reviewed_assets = {item.get("id"): item for item in reviewed.get("assets", [])}
    reviewed_code = {item.get("id"): item for item in reviewed.get("codeElements", [])}
    base = manifest_path.parent
    assets = []
    for asset in document.get("assets", []):
        if manifest_path == draft_path and asset.get("id") in reviewed_assets:
            decision = reviewed_assets[asset["id"]]
            asset = {**asset, **{key: decision.get(key) for key in (
                "name", "parent", "zIndex", "placement", "semanticDescription", "approved", "reviewStatus",
            )}}
        path = base / asset.get("file", "")
        if not path.exists() and asset.get("name"):
            path = root / "assets" / "approved" / f"{asset['name']}.png"
        item = {key: asset.get(key) for key in (
            "id", "file", "name", "role", "elementType", "processingMode", "parent", "zIndex",
            "placement", "approved", "reviewStatus", "semanticDescription", "sourceRegion", "sourceTarget",
            "extractionMethod", "backgroundColor", "backgroundTolerance", "edgeFeather", "activeVersion",
            "processingHistory", "matchedSourceTarget",
        )}
        if not item.get("processingMode"):
            item["processingMode"] = LEGACY_PROCESSING_MODES.get(str(item.get("extractionMethod") or ""), item.get("extractionMethod") or "ai-transparent")
        if not item.get("elementType"):
            item["elementType"] = "complete-composite" if item["processingMode"] in {"complete-crop", "background-repair"} else "image-asset"
        item["url"] = file_url(path) if path.exists() else None
        versions = []
        for version in asset.get("versions", []):
            version_path = base / str(version.get("file", ""))
            if not version_path.exists():
                version_path = root / "assets" / "split-web" / str(version.get("file", ""))
            versions.append({**version, "url": file_url(version_path) if version_path.exists() else None})
        item["versions"] = versions
        assets.append(item)
    return {
        "source": manifest_path.relative_to(root).as_posix() if manifest_path.exists() else None,
        "assets": assets,
        "codeElements": [
            {**item, **({key: reviewed_code[item.get("id")].get(key) for key in (
                "name", "parent", "zIndex", "placement", "semanticDescription", "approved", "reviewStatus",
            )} if item.get("id") in reviewed_code else {})}
            for item in document.get("codeElements", [])
        ],
    }


def approve_asset_manifest(root: Path, draft_path: Path, body: dict) -> dict:
    draft = read_json(draft_path)
    if not draft:
        raise ValueError("没有待审核素材")
    decisions = {item["id"]: item for item in body.get("assets", []) if item.get("id")}
    approved_candidates = [item for item in decisions.values() if item.get("approved")]
    approved_code_candidates = [item for item in body.get("codeElements", []) if item.get("approved")]
    approved_names = [str(item.get("name", "")).strip() for item in approved_candidates + approved_code_candidates]
    duplicate_names = sorted({name for name in approved_names if approved_names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"元素名称不能重复：{', '.join(duplicate_names)}")

    approved_dir = root / "assets" / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    approved = []
    for asset in draft.get("assets", []):
        decision = decisions.get(asset.get("id"), {})
        if not decision.get("approved"):
            continue
        name = str(decision.get("name", "")).strip()
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"素材 {asset.get('id', '未命名')} 需要有效的 kebab-case 名称")
        source = draft_path.parent / str(asset.get("file", ""))
        if not source.is_file():
            raise ValueError(f"素材源文件不存在：{asset.get('file', '')}")
        destination = approved_dir / f"{name}.png"
        shutil.copy2(source, destination)
        target = asset.get("matchedSourceTarget")
        bbox = decision.get("bbox") or (target or {}).get("pageBox")
        approved.append({
            **asset,
            "name": name,
            "file": destination.name,
            "parent": str(decision.get("parent", "")),
            "zIndex": decision.get("zIndex"),
            "placement": {"status": "confirmed-from-browser-review", "bbox": bbox},
            "semanticDescription": str(decision.get("description", "")) or (target or {}).get("purpose", ""),
            "approved": True,
            "reviewStatus": "approved",
        })

    code_elements = []
    code_source = body.get("codeElements", draft.get("codeElements", []))
    for item in code_source:
        if not item.get("approved"):
            continue
        name = str(item.get("name", "")).strip()
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("代码元素需要有效的 kebab-case 名称")
        code_elements.append({**item, "name": name, "approved": True, "reviewStatus": "approved"})
    manifest = {**draft, "assets": approved, "codeElements": code_elements, "reviewedAt": now_iso()}
    write_json(root / "assets" / "asset-manifest.json", manifest)
    return {"manifest": manifest, "approvedCount": len(approved), "codeElementCount": len(code_elements)}


def iterations_summary() -> list[dict]:
    root = data_root() / "artifacts" / "iterations"
    result = []
    if not root.exists():
        return result
    for directory in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        metrics = read_json(directory / "metrics.json", {}) or {}
        result.append({"id": directory.name, "metrics": metrics, "renderUrl": file_url(directory / "render.png") if (directory / "render.png").exists() else None, "overlayUrl": file_url(directory / "overlay.png") if (directory / "overlay.png").exists() else None, "diffUrl": file_url(directory / "diff.png") if (directory / "diff.png").exists() else None})
    return result


def state_payload() -> dict:
    env = read_env()
    state = load_state()
    root = data_root()
    catalog = ensure_catalog()
    page = current_page()
    regions = normalize_regions_document(read_json(root / "regions" / "regions.json", {}) or {})
    all_targets = [target for region in regions.get("regions", []) if region.get("approved", True) for target in region.get("targets", []) if target.get("approved", True)]
    confirmed_targets = [target for target in all_targets if target.get("reviewStatus", "confirmed") == "confirmed"]
    pending_targets = [target for target in all_targets if target.get("reviewStatus", "confirmed") != "confirmed"]
    with JOBS_LOCK:
        jobs = [public_job(item.copy()) for item in JOBS.values() if item.get("pageId") == page["id"]]
    known_jobs = {job['id'] for job in jobs}
    for path in (root / 'runs' / 'job-failures').glob('*.json'):
        saved = read_json(path, {})
        if saved.get('id') and saved['id'] not in known_jobs:
            jobs.append(saved)
    codex_tasks = TASK_STORE.list_tasks(page_id=page["id"])
    return {
        **state,
        "settings": load_project_settings(),
        "project": {"activePageId": page["id"], "activePage": public_page(page), "fileBase": "" if page.get("storage") == "legacy" else f"pages/{page['id']}/", "pages": [public_page(item) for item in catalog["pages"]]},
        "config": {
            "image": {"baseUrl": env.get("GPT_IMAGE_BASE_URL", "http://154.12.91.166:3000/v1"), "model": env.get("GPT_IMAGE_MODEL", "gpt-image-2"), "apiKeyConfigured": bool(env.get("GPT_IMAGE_API_KEY"))},
            "text": {"baseUrl": env.get("GPT_TEXT_BASE_URL", "http://154.12.91.166:3000/v1"), "model": env.get("GPT_TEXT_MODEL", ""), "apiKeyConfigured": bool(env.get("GPT_TEXT_API_KEY"))},
        },
        "reference": image_info(root / "reference" / "reference-page.png"),
        "regionCount": len(regions.get("regions", [])),
        "targetCount": sum(len(region.get("targets", [])) for region in regions.get("regions", [])),
        "elementCounts": {
            element_type: sum(
                1 for region in regions.get("regions", []) for target in region.get("targets", [])
                if target.get("elementType") == element_type
            ) for element_type in ELEMENT_TYPES
        },
        "processingCounts": {
            mode: sum(1 for target in confirmed_targets if target.get("processingMode") == mode)
            for mode in PROCESSING_MODES
        },
        "pendingProcessingCounts": {
            mode: sum(1 for target in pending_targets if target.get("processingMode") == mode)
            for mode in PROCESSING_MODES
        },
        "regions": regions,
        "crops": list_crops(),
        "assets": assets_summary(),
        "iterations": iterations_summary(),
        "sourceReady": all((root / "src" / name).exists() for name in SOURCE_FILES),
        "sourceRevision": TASK_STORE.source_bundle(page["id"])["revision"],
        "recropImpact": recrop_impact(root, page["id"], codex_tasks),
        "codexTasks": codex_tasks,
        "jobs": sorted(jobs, key=lambda item: item["createdAt"], reverse=True),
    }


def generate_reference(body: dict) -> dict:
    if not body.get("confirmed"):
        raise ValueError("生成参考图会调用付费 API，请先确认")
    from scripts.gpt_image_api import call_generate_api

    settings = load_project_settings()
    root = data_root()
    prompt_path = root / "prompts" / "reference-page.txt"
    prompt = body.get("prompt") or prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("请先使用文本模型生成图片提示词")
    run_id = "reference-web-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    output = root / "reference" / "reference-page.png"
    with GENERATE_LOCK:
        call_generate_api(prompt, output, root / "runs" / "gpt-image" / run_id, settings["generationSize"], settings["quality"], 1, normalized_size=settings["finalSize"])
    update_phase("draft-reference", "生成了新的参考图，等待审核")
    return {"reference": image_info(output)}


def generate_image_prompt(body: dict) -> dict:
    if not body.get("confirmed"):
        raise ValueError("生成提示词会调用文本模型 API，请先确认")
    env = read_env()
    base_url = env.get("GPT_TEXT_BASE_URL", "").rstrip("/")
    api_key = env.get("GPT_TEXT_API_KEY")
    model = env.get("GPT_TEXT_MODEL")
    if not base_url or not api_key or not model:
        raise ValueError("请先配置文本 API 地址、密钥和模型")
    root = data_root()
    spec = load_project_design()
    notes = str(body.get("notes", "")).strip()
    system_prompt = (ROOT / 'prompts' / 'page-image-system.txt').read_text(encoding='utf-8')
    user_prompt = f"Page name: {current_page()['name']}\nDesign specification JSON:\n{json.dumps(spec, ensure_ascii=False, indent=2)}"
    if notes:
        user_prompt += f"\nAdditional direction:\n{notes}"
    payload = {"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "temperature": 0.3, "max_tokens": 3000}
    run_dir = root / "runs" / "gpt-text" / ("prompt-" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "request.json", payload)
    response_body = post_chat_completion(base_url, api_key, payload, run_dir)
    document = json.loads(response_body)
    (run_dir / "response.json").write_bytes(response_body)
    choices = document.get("choices") or []
    if not choices:
        raise RuntimeError("文本 API 未返回 choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    prompt = str(content or "").strip()
    if not prompt:
        raise RuntimeError("文本模型返回了空提示词")
    (root / "prompts" / "reference-page.txt").write_text(prompt + "\n", encoding="utf-8")
    return {"prompt": prompt}


def generate_region_suggestions(body: dict) -> dict:
    if not body.get("confirmed"):
        raise ValueError("AI 元素建议会调用文本模型 API，请先确认")
    env = read_env()
    base_url = env.get("GPT_TEXT_BASE_URL", "").rstrip("/")
    api_key = env.get("GPT_TEXT_API_KEY")
    model = env.get("GPT_TEXT_MODEL")
    if not base_url or not api_key or not model:
        raise ValueError("请先配置可看图的文本模型 API")
    root = data_root()
    reference_path = root / "reference" / "reference-page.png"
    if not reference_path.is_file():
        raise ValueError("当前页面还没有参考图")
    with Image.open(reference_path) as image:
        width, height = image.size
    existing = normalize_regions_document(body.get("regions") or {"canvas": {"width": width, "height": height}, "regions": []})
    image_url, transport = vision_image_data_url(reference_path)
    system_prompt = (
        "You are a senior UI production asset planner. Analyze the screenshot and return JSON only. "
        "Classify visible elements as image-asset, code-element, or complete-composite. "
        "Recommend exactly one processingMode: local-transparent, ai-transparent, complete-crop, background-repair, or none. "
        "Use none only for code-element. Use complete-crop for banners or composites whose text and decoration must remain baked. "
        "Use background-repair only for a reusable illustrated background that is visibly covered by foreground UI. "
        "Coordinates must be integer pixels in the original screenshot. Suggestions are candidates for human review, not final decisions."
    )
    user_text = (
        f"Canvas: {width}x{height}. Existing human crop regions may be reused and must not be moved: "
        f"{json.dumps(existing.get('regions', []), ensure_ascii=False)}\n"
        "Return {\"regions\":[{\"id\":\"region-001\",\"x\":0,\"y\":0,\"width\":100,\"height\":100,"
        "\"purpose\":\"...\",\"targets\":[{\"x\":0,\"y\":0,\"width\":50,\"height\":50,"
        "\"name\":\"semantic-kebab-name\",\"purpose\":\"...\",\"elementType\":\"image-asset\","
        "\"processingMode\":\"ai-transparent\",\"parent\":\"\",\"zIndex\":1,\"confidence\":0.9}]}]}. "
        "Target coordinates are local to their parent region. When existing crop regions are supplied, return those same regions and add target suggestions inside them."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
            ]},
        ],
        "temperature": 0.1,
        "max_tokens": 5000,
        "response_format": {"type": "json_object"},
    }
    run_dir = root / "runs" / "gpt-text" / ("element-plan-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "transport.json", transport)
    response_body = post_chat_completion(base_url, api_key, payload, run_dir)
    (run_dir / "response.json").write_bytes(response_body)
    response_document = json.loads(response_body)
    content = ((response_document.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    if isinstance(content, list):
        content = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.IGNORECASE)
    suggestions = normalize_regions_document(json.loads(cleaned), suggested=True)
    suggestions["canvas"] = {"width": width, "height": height}
    for region in suggestions.get("regions", []):
        region["x"] = max(0, min(int(region.get("x", 0)), width - 1))
        region["y"] = max(0, min(int(region.get("y", 0)), height - 1))
        region["width"] = max(1, min(int(region.get("width", 1)), width - region["x"]))
        region["height"] = max(1, min(int(region.get("height", 1)), height - region["y"]))
        for target in region.get("targets", []):
            target["x"] = max(0, min(int(target.get("x", 0)), region["width"] - 1))
            target["y"] = max(0, min(int(target.get("y", 0)), region["height"] - 1))
            target["width"] = max(1, min(int(target.get("width", 1)), region["width"] - target["x"]))
            target["height"] = max(1, min(int(target.get("height", 1)), region["height"] - target["y"]))
    write_json(run_dir / "suggestions.json", suggestions)
    return {"suggestions": suggestions, "run": str(run_dir.relative_to(root).as_posix())}


def run_extract(body: dict) -> dict:
    if not body.get("confirmed"):
        raise ValueError("素材提取会并发调用付费 API，请先确认")
    settings = load_project_settings()
    root = data_root()
    crops = root / "regions" / "crops-web"
    output = command(
        sys.executable, "scripts/extract_regions.py", str(crops / "regions.normalized.json"), str(crops), str(root / "assets" / "raw-web"), str(root / "runs" / "gpt-image" / "assets-web"),
        "--workers", "0", "--retries", "2", "--size", settings["extractionSize"], "--quality", settings["quality"], timeout=1800
    )
    update_phase("extraction-review", "素材提取完成")
    return {"output": output}


def run_background_repair(body: dict) -> dict:
    if not body.get("confirmed"):
        raise ValueError("背景补全会并发调用付费 API，请先确认")
    settings = load_project_settings()
    root = data_root()
    output = command(
        sys.executable, "scripts/repair_background_targets.py",
        str(root / "reference" / "reference-page.png"), str(root / "regions" / "regions.json"),
        str(root / "assets" / "split-web"), str(root / "runs" / "gpt-image" / "background-repair-web"),
        "--workers", "0", "--retries", "2", "--size", settings["extractionSize"],
        "--quality", settings["quality"], timeout=1800,
    )
    update_phase("extraction-review", "背景补全完成")
    return {"output": output}


def run_render_compare(server_port: int, body: dict) -> dict:
    page_root = data_root()
    root = page_root / "artifacts" / "iterations"
    identifiers = [int(item.name) for item in root.iterdir() if item.is_dir() and item.name.isdigit()] if root.exists() else []
    iteration = f"{(max(identifiers, default=0) + 1):03d}"
    destination = root / iteration
    destination.mkdir(parents=True, exist_ok=False)
    settings = load_project_settings()
    width, height = (int(value) for value in settings["finalSize"].split("x", 1))
    render = destination / "render.png"
    source_url = f"http://127.0.0.1:{server_port}{file_url(page_root / 'src' / 'index.html')}"
    command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/render_page.ps1", "-Url", source_url, "-Output", str(render), "-Width", str(width), "-Height", str(height), timeout=120)
    output = command(sys.executable, "scripts/compare_images.py", str(page_root / "reference" / "reference-page.png"), str(render), str(destination), "--threshold", str(int(body.get("threshold", settings["threshold"]))))
    note = str(body.get("note", "")).strip()
    write_json(destination / "adjustment.json", {"note": note, "createdAt": now_iso()})
    update_phase("comparison-ready", f"完成对比迭代 {iteration}")
    return {"iteration": iteration, "output": output}


from design_studio import DesignStudio

DESIGN_STUDIO = DesignStudio(ROOT, read_env, read_json, write_json, load_project_design, save_project_design, post_chat_completion, vision_image_data_url, GENERATE_LOCK)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "UIAssetPipeline/2.0"

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def send_json(self, value, status=HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def validate_local_request(self, write: bool = False) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            raise PermissionError("Only localhost requests are allowed")
        if write:
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin and origin not in allowed:
                raise PermissionError("Cross-origin writes are not allowed")

    def send_error_json(self, error: Exception, status=HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": str(error)}, status)

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 25 * 1024 * 1024:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def serve_file(self, path: Path, cache: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.validate_local_request()
            PAGE_CONTEXT.page_id = ensure_catalog()["activePageId"]
            root = data_root()
            if parsed.path in {"/", "/index.html"}:
                self.serve_file(WEB_ROOT / "index.html")
            elif parsed.path.startswith("/web/"):
                relative = parsed.path.removeprefix("/web/")
                if ".." in Path(relative).parts:
                    raise ValueError("Invalid web path")
                self.serve_file(WEB_ROOT / relative)
            elif parsed.path.startswith("/files/"):
                self.serve_file(safe_project_path(parsed.path.removeprefix("/files/")), cache=True)
            elif parsed.path == "/api/state":
                self.send_json(state_payload())
            elif parsed.path == '/api/design-studio':
                self.send_json(DESIGN_STUDIO.public())
            elif parsed.path.startswith('/api/design-studio/image/'):
                self.serve_file(DESIGN_STUDIO.image_path(parsed.path.rsplit('/', 1)[-1]))
            elif parsed.path.startswith("/api/jobs/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    self.send_json(public_job(job.copy()) if job else {"error": "任务不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            elif parsed.path == "/api/design":
                prompt_path = root / "prompts" / "reference-page.txt"
                notes_path = root / "prompts" / "reference-page.notes.txt"
                fallback = root / "prompts" / "reference-game-home.txt"
                self.send_json({"spec": load_project_design(), "prompt": prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else fallback.read_text(encoding="utf-8") if fallback.exists() else "", "notes": notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""})
            elif parsed.path == "/api/source":
                self.send_json(TASK_STORE.source_bundle(current_page()["id"]))
            elif parsed.path.startswith("/api/codex-tasks/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 3:
                    raise ValueError("无效的 Codex 任务路径")
                self.send_json(TASK_STORE.get_task(parts[2]))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.send_error_json(error)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.validate_local_request(write=True)
            body = self.body_json()
            PAGE_CONTEXT.page_id = ensure_catalog()["activePageId"]
            root = data_root()
            if parsed.path == '/api/design-studio/brief':
                self.send_json(DESIGN_STUDIO.save_brief(body))
            elif parsed.path == '/api/design-studio/upload':
                self.send_json(DESIGN_STUDIO.upload(body))
            elif parsed.path == '/api/design-studio/remove':
                self.send_json(DESIGN_STUDIO.remove(body))
            elif parsed.path in {'/api/design-studio/generate', '/api/design-studio/preview'}:
                self.send_json({'jobId': DESIGN_STUDIO.start(parsed.path.rsplit('/', 1)[-1], body, create_job)}, HTTPStatus.ACCEPTED)
            elif parsed.path == '/api/design-studio/adopt':
                self.send_json(DESIGN_STUDIO.adopt(body))
            elif parsed.path == "/api/pages":
                self.send_json({"page": create_page(str(body.get("name", "")), str(body.get("id", "")))}, HTTPStatus.CREATED)
            elif parsed.path == "/api/pages/select":
                page = page_by_id(str(body.get("id", "")))
                catalog = ensure_catalog(); catalog["activePageId"] = page["id"]; save_catalog(catalog)
                self.send_json({"page": public_page(page)})
            elif parsed.path == "/api/pages/rename":
                name = str(body.get("name", "")).strip()
                if not name: raise ValueError("页面名称不能为空")
                catalog = ensure_catalog(); page = next(item for item in catalog["pages"] if item["id"] == current_page()["id"])
                page["name"] = name; page["updatedAt"] = now_iso(); save_catalog(catalog)
                self.send_json({"page": public_page(page)})
            elif parsed.path == "/api/config":
                image_base = str(body.get("imageBaseUrl", "")).strip().rstrip("/")
                text_base = str(body.get("textBaseUrl", "")).strip().rstrip("/")
                if image_base and not image_base.startswith(("http://", "https://")):
                    raise ValueError("图片 API 地址必须使用 HTTP 或 HTTPS")
                if text_base and not text_base.startswith(("http://", "https://")):
                    raise ValueError("文本 API 地址必须使用 HTTP 或 HTTPS")
                write_env({"GPT_IMAGE_BASE_URL": image_base, "GPT_IMAGE_MODEL": str(body.get("imageModel", "")).strip(), "GPT_IMAGE_API_KEY": str(body.get("imageApiKey", "")).strip(), "GPT_TEXT_BASE_URL": text_base, "GPT_TEXT_MODEL": str(body.get("textModel", "")).strip(), "GPT_TEXT_API_KEY": str(body.get("textApiKey", "")).strip()})
                settings = load_project_settings()
                for key in ("generationSize", "extractionSize", "finalSize", "quality"):
                    if body.get(key): settings[key] = str(body[key])
                for key in ("padding", "threshold", "codexMaxIterations"):
                    if body.get(key) is not None: settings[key] = int(body[key])
                save_project_settings(settings)
                self.send_json({"ok": True})
            elif parsed.path == "/api/models/image":
                env = read_env()
                base_url = str(body.get("baseUrl", "")).strip() or env.get("GPT_IMAGE_BASE_URL", "")
                api_key = str(body.get("apiKey", "")).strip() or env.get("GPT_IMAGE_API_KEY", "")
                models = fetch_gateway_models(base_url, api_key)
                self.send_json({"models": models, "count": len(models)})
            elif parsed.path in {"/api/check-api", "/api/check-text-api"}:
                api_kind = "IMAGE" if parsed.path == "/api/check-api" else "TEXT"
                def check():
                    env = read_env()
                    base_url = env.get(f"GPT_{api_kind}_BASE_URL", "").rstrip("/")
                    api_key = env.get(f"GPT_{api_kind}_API_KEY")
                    model = env.get(f"GPT_{api_kind}_MODEL")
                    if not base_url or not api_key or not model: raise ValueError("请先保存完整的 API 配置")
                    request = urllib.request.Request(base_url + "/models", headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"})
                    with urllib.request.urlopen(request, timeout=30) as response:
                        data = json.loads(response.read())
                    models = {item.get("id") for item in data.get("data", [])}
                    return {"authenticated": True, "configuredModel": model, "configuredModelListed": model in models, "modelsReturned": len(models)}
                self.send_json({"jobId": create_job(f"check-{api_kind.lower()}-api", check)}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/design":
                spec = body.get("spec")
                if spec is not None:
                    if not isinstance(spec, dict): raise ValueError("设计规范必须是 JSON 对象")
                    save_project_design(spec)
                if spec is None and not any(key in body for key in ("prompt", "notes")):
                    raise ValueError("没有可保存的设计数据")
                if "prompt" in body: (root / "prompts" / "reference-page.txt").write_text(str(body.get("prompt", "")), encoding="utf-8")
                if "notes" in body: (root / "prompts" / "reference-page.notes.txt").write_text(str(body.get("notes", "")), encoding="utf-8")
                self.send_json({"ok": True})
            elif parsed.path == "/api/prompt/generate":
                self.send_json({"jobId": create_job("generate-image-prompt", lambda: generate_image_prompt(body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/regions/suggest":
                self.send_json({"jobId": create_job("suggest-ui-elements", lambda: generate_region_suggestions(body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/generate":
                self.send_json({"jobId": create_job("generate-reference", lambda: generate_reference(body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/reference/upload":
                encoded = str(body.get("data", ""))
                if "," in encoded: encoded = encoded.split(",", 1)[1]
                data = base64.b64decode(encoded, validate=True)
                if len(data) > 20 * 1024 * 1024: raise ValueError("图片不能超过 20 MB")
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                output = root / "reference" / "reference-page.png"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                update_phase("draft-reference", "上传了新的参考图")
                self.send_json({"reference": image_info(output)})
            elif parsed.path == "/api/reference/approve":
                if not (root / "reference" / "reference-page.png").exists(): raise ValueError("没有可审核的参考图")
                state = load_state(); state["referenceApproved"] = bool(body.get("approved", True)); save_state(state)
                update_phase("reference-approved" if state["referenceApproved"] else "draft-reference", "参考图审核状态已更新")
                self.send_json({"ok": True})
            elif parsed.path == "/api/regions":
                document = normalize_regions_document(body)
                canvas = document.get("canvas", {}); regions = document.get("regions", [])
                if not isinstance(regions, list) or not canvas.get("width") or not canvas.get("height"): raise ValueError("标注数据格式错误")
                for region in regions:
                    if not region.get("targets"): raise ValueError(f"{region.get('id', '未命名区域')} 至少需要一个红框")
                    for target in region["targets"]:
                        if target.get("elementType") not in ELEMENT_TYPES:
                            raise ValueError("不支持的元素类型")
                        if target.get("processingMode") not in PROCESSING_MODES:
                            raise ValueError("不支持的处理方式")
                        if target.get("elementType") == "code-element" and target.get("processingMode") != "none":
                            raise ValueError("代码元素的处理方式必须是不生成图片")
                write_json(root / "regions" / "regions.json", document)
                update_phase("regions-marked", f"保存 {len(regions)} 个切割区域")
                self.send_json({"ok": True})
            elif parsed.path == "/api/crop":
                from scripts.crop_regions import crop_regions
                require_raster_targets_confirmed(root)
                page_id = current_page()["id"]
                impact = recrop_impact(root, page_id)
                if impact["hasExistingCrops"] and not body.get("confirmedRecrop"):
                    raise ValueError("当前页面已经生成过裁剪，请确认重新裁剪及清理下游数据")
                with JOBS_LOCK:
                    active_jobs = [job for job in JOBS.values() if job.get("pageId") == page_id and job.get("status") in {"queued", "running"}]
                if active_jobs:
                    raise ValueError("当前页面仍有后台任务运行，请等待任务结束后再重新裁剪")
                settings = load_project_settings(); output = root / "regions" / "crops-web"
                staging = root / "regions" / f".crops-web-{uuid.uuid4().hex[:8]}"
                try:
                    crop_regions(root / "reference" / "reference-page.png", root / "regions" / "regions.json", staging, int(body.get("padding", settings["padding"])))
                    if impact["hasExistingCrops"]:
                        TASK_STORE.delete_page_tasks(page_id)
                        clear_recrop_downstream(root)
                    if output.exists():
                        shutil.rmtree(output)
                    staging.replace(output)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
                update_phase("crops-created", "重新裁剪并清理下游数据" if impact["hasExistingCrops"] else "裁剪完成")
                self.send_json({"crops": list_crops()})
            elif parsed.path == "/api/script-extract":
                from scripts.extract_uniform_background import extract_targets
                require_raster_targets_confirmed(root)
                regions_path = root / "regions" / "regions.json"
                if not regions_path.exists(): raise ValueError("请先保存区域标注")
                output_root = root / "assets" / "split-web"
                script_manifest = extract_targets(root / "reference" / "reference-page.png", regions_path, output_root)
                write_json(output_root / "asset-manifest.script.json", script_manifest)
                current_draft = read_json(output_root / "asset-manifest.draft.json", {}) or {}
                ai_assets = [item for item in current_draft.get("assets", []) if item.get("processingMode") not in {"local-transparent", "complete-crop"} and item.get("extractionMethod") != "background-script"]
                write_json(output_root / "asset-manifest.draft.json", {
                    "canvas": script_manifest["canvas"],
                    "coordinateNotice": "Script assets preserve page coordinates; AI assets may be rescaled or recentered.",
                    "assets": script_manifest["assets"] + ai_assets,
                    "codeElements": script_manifest.get("codeElements", []),
                })
                update_phase("extraction-review", f"本地处理生成 {len(script_manifest['assets'])} 张素材")
                self.send_json({"ok": True, "assetCount": len(script_manifest["assets"]), "assets": assets_summary()})
            elif parsed.path == "/api/extract":
                require_raster_targets_confirmed(root)
                self.send_json({"jobId": create_job("extract-assets", lambda: run_extract(body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/background-repair":
                require_raster_targets_confirmed(root)
                self.send_json({"jobId": create_job("repair-backgrounds", lambda: run_background_repair(body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/split":
                raw = root / "assets" / "raw-web"
                if not (raw / "extraction-results.json").exists(): raise ValueError("请先完成素材提取")
                output = command(sys.executable, "scripts/split_extractions.py", str(root / "regions" / "crops-web" / "regions.normalized.json"), str(raw), str(root / "assets" / "split-web"), "--join-gap", str(int(body.get("joinGap", 0))), "--min-area", str(int(body.get("minArea", 256))))
                update_phase("extraction-review", "拆分透明素材完成")
                self.send_json({"ok": True, "output": output})
            elif parsed.path == "/api/assets/approve":
                draft_path = root / "assets" / "split-web" / "asset-manifest.draft.json"
                result = approve_asset_manifest(root, draft_path, body)
                update_phase("assets-approved", f"批准 {result['approvedCount']} 张素材")
                self.send_json({"ok": True, "approvedCount": result["approvedCount"], "codeElementCount": result["codeElementCount"]})
            elif parsed.path == "/api/source":
                files = body.get("files", {})
                result = TASK_STORE.write_browser_source(current_page()["id"], files, str(body.get("expectedRevision", "")))
                update_phase("page-generated", "浏览器保存了页面源码")
                self.send_json(result)
            elif parsed.path == "/api/codex-tasks":
                task_type = str(body.get("type", ""))
                task = TASK_STORE.create_task(current_page()["id"], task_type, int(body.get("maxIterations", 8)))
                self.send_json({"task": task}, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/codex-tasks/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("无效的 Codex 任务路径")
                task_id, action = parts[2], parts[3]
                if action == "review":
                    previous = TASK_STORE.get_task(task_id, include_events=False)
                    approved = bool(body.get("approved"))
                    task = TASK_STORE.review_task(task_id, approved, str(body.get("note", "")))
                    if approved:
                        if previous["type"] == "implementation":
                            update_phase("page-generated", f"批准 Codex 页面实现任务 {task_id}")
                        else:
                            update_phase("completed", f"批准 Codex 校准任务 {task_id}")
                    self.send_json({"task": task})
                elif action == "cancel":
                    self.send_json({"task": TASK_STORE.cancel_task(task_id)})
                else:
                    raise ValueError("不支持的 Codex 任务操作")
            elif parsed.path == "/api/render-compare":
                port = self.server.server_port
                self.send_json({"jobId": create_job("render-compare", lambda: run_render_compare(port, body))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/complete":
                update_phase("completed", str(body.get("note", "项目已完成")))
                self.send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except urllib.error.HTTPError as error:
            self.send_error_json(RuntimeError(f"API 返回 HTTP {error.code}"), HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            self.send_error_json(error)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local multi-page UI asset pipeline")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    os.chdir(ROOT)
    ensure_catalog()
    ensure_project_config()
    PAGE_CONTEXT.page_id = ensure_catalog()["activePageId"]
    save_state(load_state())
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"UI Asset Pipeline: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
