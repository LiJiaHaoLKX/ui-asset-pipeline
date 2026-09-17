#!/usr/bin/env python3
"""Check gateway authentication and configured-model visibility without generating an image."""

from __future__ import annotations

import json
import os
import urllib.request

from gpt_image_api import read_image_config


def main() -> None:
    config = read_image_config()
    base_url = config["GPT_IMAGE_BASE_URL"].rstrip("/")
    api_key = config["GPT_IMAGE_API_KEY"]
    configured_model = config["GPT_IMAGE_MODEL"]
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = json.loads(response.read())
        request_id = response.headers.get("x-request-id")
    model_ids = {item.get("id") for item in document.get("data", [])}
    print(
        json.dumps(
            {
                "authenticated": True,
                "modelsReturned": len(model_ids),
                "configuredModel": configured_model,
                "configuredModelListed": configured_model in model_ids,
                "requestId": request_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
