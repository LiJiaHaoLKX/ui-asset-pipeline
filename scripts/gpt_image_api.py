#!/usr/bin/env python3
"""Small OpenAI-compatible gpt-image client with immutable request records."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
import time
import ssl
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image


def read_image_config(env_file: Path | None = None) -> dict[str, str]:
    """Read one fresh snapshot; project settings override inherited process values."""
    env_file = env_file or Path(__file__).resolve().parents[1] / ".env"
    config = {key: value for key, value in os.environ.items() if key.startswith('GPT_IMAGE_')}
    if not env_file.exists():
        return config
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().startswith('GPT_IMAGE_'):
            config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def load_dotenv() -> None:
    """Compatibility helper for external scripts; internal callers use snapshots."""
    os.environ.update(read_image_config())


class ImageDownloadError(RuntimeError):
    """Generation succeeded, but downloading its result failed."""


def _recover_tls_download(url: str, output: Path) -> bool:
    """Use Windows' system TLS client on Python TLS EOF; never disable verification."""
    executable = shutil.which('curl.exe') if os.name == 'nt' else None
    if not executable:
        return False
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / 'image'
        result = subprocess.run([executable, '--fail', '--silent', '--location', '--proto', '=https',
                                 '--proto-redir', '=https', '--connect-timeout', '20', '--max-time', '120',
                                 '--output', str(candidate), url], capture_output=True, timeout=130)
        if result.returncode:
            return False
        with Image.open(candidate) as image:
            image.verify()
        shutil.copyfile(candidate, output)
        return True


def _save_result(body: bytes, content_type: str, output: Path, record_dir: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if content_type.startswith("image/"):
        output.write_bytes(body)
        return
    (record_dir / "response.json").write_bytes(body)
    response_json = json.loads(body)
    items = response_json.get("data") or []
    if not items:
        raise RuntimeError(f"Image API returned no data: {response_json}")
    first = items[0]
    if first.get("b64_json"):
        output.write_bytes(base64.b64decode(first["b64_json"]))
    elif first.get("url"):
        request = urllib.request.Request(first["url"], headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/png,image/*;q=0.9,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(request, timeout=180) as image_response:
                output.write_bytes(image_response.read())
        except urllib.error.HTTPError as error:
            raise ImageDownloadError(f"图片生成成功，但下载图片返回 HTTP {error.code}；生成响应已保留，可重试下载") from error
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, 'reason', error)
            if isinstance(reason, ssl.SSLEOFError):
                try:
                    if _recover_tls_download(first['url'], output):
                        return
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            raise ImageDownloadError("图片生成成功，但下载连接失败；生成响应已保留，可重试下载") from error
    else:
        raise RuntimeError("Image API response has neither b64_json nor url.")


def fit_reference_size(output: Path, requested_size: str, record_dir: Path) -> None:
    """Keep every source pixel in the reference composition; never crop a UI."""
    with Image.open(output) as source:
        source.save(record_dir / 'original-response.png', format='PNG')
        actual = source.size
        if requested_size == 'auto':
            return
        expected = tuple(int(n) for n in requested_size.lower().split('x'))
        if actual == expected:
            return
        ratio = min(expected[0] / actual[0], expected[1] / actual[1], 1)
        fitted = (max(1, round(actual[0] * ratio)), max(1, round(actual[1] * ratio)))
        offset = ((expected[0] - fitted[0]) // 2, (expected[1] - fitted[1]) // 2)
        canvas = Image.new('RGBA', expected, (255, 255, 255, 255))
        canvas.alpha_composite(source.convert('RGBA').resize(fitted, Image.Resampling.LANCZOS), offset)
        canvas.convert('RGB').save(output, format='PNG')
    (record_dir / 'normalization.json').write_text(json.dumps({
        'requested': {'width': expected[0], 'height': expected[1]},
        'received': {'width': actual[0], 'height': actual[1]},
        'method': 'contain without cropping or upscaling; white padding',
        'offset': list(offset), 'fitted': list(fitted),
        'warning': '返回尺寸与目标不符，已等比缩放补白，未裁切；原图保存在 original-response.png'
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_size(output: Path, requested_size: str, record_dir: Path) -> None:
    if requested_size == "auto" or "x" not in requested_size:
        return
    expected = tuple(int(value) for value in requested_size.lower().split("x", 1))
    with Image.open(output) as image:
        actual = image.size
        if actual == expected:
            return
        source_ratio = actual[0] / actual[1]
        target_ratio = expected[0] / expected[1]
        crop_box = (0, 0, actual[0], actual[1])
        if abs(source_ratio - target_ratio) > 0.0001:
            if source_ratio > target_ratio:
                crop_width = round(actual[1] * target_ratio)
                left = (actual[0] - crop_width) // 2
                crop_box = (left, 0, left + crop_width, actual[1])
            else:
                crop_height = round(actual[0] / target_ratio)
                top = (actual[1] - crop_height) // 2
                crop_box = (0, top, actual[0], top + crop_height)
        normalized = image.crop(crop_box).resize(expected, Image.Resampling.LANCZOS)
        normalized.save(output, format="PNG", optimize=True)
    (record_dir / "normalization.json").write_text(
        json.dumps(
            {
                "reason": "gateway image dimensions differed from requested size",
                "requested": {"width": expected[0], "height": expected[1]},
                "received": {"width": actual[0], "height": actual[1]},
                "cropBox": {
                    "left": crop_box[0],
                    "top": crop_box[1],
                    "right": crop_box[2],
                    "bottom": crop_box[3],
                },
                "method": "center crop to target aspect ratio, then Pillow LANCZOS resize",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _send(request: urllib.request.Request, record_dir: Path, timeout: int) -> tuple[bytes, str]:
    try:
        from scripts.request_diagnostics import record
    except ModuleNotFoundError:
        from request_diagnostics import record
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
            metadata = {
                "status": response.status,
                "contentType": content_type,
                "requestId": response.headers.get("x-request-id"),
            }
    except urllib.error.HTTPError as error:
        error_body = error.read()
        record(error, record_dir, stage='图片 API 请求', url=request.full_url, elapsed=time.monotonic() - started, headers=error.headers, body=error_body)
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "error-response.txt").write_bytes(error_body)
        (record_dir / "response-meta.json").write_text(
            json.dumps(
                {
                    "status": error.code,
                    "contentType": error.headers.get_content_type(),
                    "requestId": error.headers.get("x-request-id"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    except (urllib.error.URLError, TimeoutError) as error:
        record(error, record_dir, stage='图片 API 连接或等待响应', url=request.full_url, elapsed=time.monotonic() - started)
        raise
    (record_dir / "response-meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return body, content_type


def call_generate_api(
    prompt: str,
    output: Path,
    record_dir: Path,
    size: str,
    quality: str,
    n: int,
    normalize_output_size: bool = False,
    normalized_size: str | None = None,
) -> None:
    config = read_image_config()
    base_url = config.get("GPT_IMAGE_BASE_URL", "http://154.12.91.166:3000/v1").rstrip("/")
    api_key = config.get("GPT_IMAGE_API_KEY")
    model = config.get("GPT_IMAGE_MODEL")
    if not api_key or not model:
        raise RuntimeError("Set GPT_IMAGE_API_KEY and GPT_IMAGE_MODEL before calling the API.")

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "output_format": "png",
        "n": n,
    }
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "request.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    request = urllib.request.Request(
        f"{base_url}/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    body, content_type = _send(request, record_dir, 180)
    stage = '解析生成响应、下载或保存图片'
    try:
        _save_result(body, content_type, output, record_dir)
        # Always retain the exact downloaded result before optional CLI-only normalization.
        if output.is_file() and not (record_dir / 'original-response.png').exists():
            import shutil
            shutil.copyfile(output, record_dir / 'original-response.png')
        stage = '本地图片尺寸处理'
        if normalized_size:
            fit_reference_size(output, normalized_size, record_dir)
        elif normalize_output_size:
            normalize_size(output, size, record_dir)
    except Exception as error:
        try:
            from scripts.request_diagnostics import record
        except ModuleNotFoundError:
            from request_diagnostics import record
        cause = error.__cause__ or error
        detail = record(cause, record_dir, stage=stage)
        error.diagnostic = detail
        raise
    print(output)


def _multipart(fields: dict[str, str], file_field: str, image_path: Path) -> tuple[bytes, str]:
    boundary = f"----ui-asset-pipeline-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{image_path.name}"\r\n'.encode(),
            b"Content-Type: image/png\r\n\r\n",
            image_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def call_edit_api(
    prompt: str,
    image: Path,
    output: Path,
    record_dir: Path,
    size: str,
    quality: str,
    transparent_background: bool = True,
) -> None:
    config = read_image_config()
    api_key = config.get("GPT_IMAGE_API_KEY")
    model = config.get("GPT_IMAGE_MODEL")
    if not api_key or not model:
        raise RuntimeError("Set GPT_IMAGE_API_KEY and GPT_IMAGE_MODEL before calling the API.")
    base_url = config.get("GPT_IMAGE_BASE_URL", "http://154.12.91.166:3000/v1").rstrip("/")
    edit_url = f"{base_url}/images/edits"
    fields = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": "1",
        "output_format": "png",
    }
    if transparent_background:
        fields["background"] = "transparent"
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "request.json").write_text(
        json.dumps({**fields, "endpoint": edit_url, "image": str(image)}, indent=2), encoding="utf-8"
    )
    body, content_type = _multipart(fields, "image[]", image)
    request = urllib.request.Request(
        edit_url,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": content_type},
        method="POST",
    )
    response_body, response_type = _send(request, record_dir, 300)
    _save_result(response_body, response_type, output, record_dir)
    normalize_size(output, size, record_dir)
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="medium")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument(
        "--normalize-size",
        action="store_true",
        help="resize a gateway result to the requested canvas and record the normalization",
    )
    parser.add_argument(
        "--normalize-to",
        metavar="WIDTHxHEIGHT",
        help="center-crop and resize the API result to a separate final canvas size",
    )
    args = parser.parse_args()
    if bool(args.prompt) == bool(args.prompt_file):
        parser.error("provide exactly one of --prompt or --prompt-file")
    if args.n != 1:
        parser.error("this client currently requires --n 1 so no returned image is silently discarded")
    prompt = args.prompt if args.prompt else args.prompt_file.read_text(encoding="utf-8")
    try:
        call_generate_api(
            prompt,
            args.output,
            args.record_dir,
            args.size,
            args.quality,
            args.n,
            args.normalize_size,
            args.normalize_to,
        )
    except Exception as exc:
        print(f"gpt-image request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
