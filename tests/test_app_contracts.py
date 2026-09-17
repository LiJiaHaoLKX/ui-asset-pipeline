import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from app import approve_asset_manifest, clear_recrop_downstream, fetch_gateway_models, normalize_regions_document, normalize_target, pending_raster_targets, post_chat_completion, recrop_impact, vision_image_data_url
from scripts.repair_background_targets import fit_to_source_aspect, merge_repair_versions


class AppContractTest(unittest.TestCase):
    def test_recrop_impact_and_cleanup_preserve_upstream_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("regions/crops-web/regions.normalized.json", "assets/asset-manifest.json", "src/index.html", "artifacts/iterations/001/metrics.json", "reference/reference-page.png", "prompts/reference-page.txt"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            tasks = [{"type": "implementation"}, {"type": "calibration"}]
            impact = recrop_impact(root, "page-001", tasks)
            self.assertTrue(all(impact[key] for key in ("hasExistingCrops", "assetReview", "pageImplementation", "comparisonCalibration")))
            self.assertEqual(impact["codexTaskCount"], 2)

            clear_recrop_downstream(root)
            self.assertTrue((root / "reference" / "reference-page.png").is_file())
            self.assertTrue((root / "prompts" / "reference-page.txt").is_file())
            self.assertTrue((root / "regions" / "crops-web" / "regions.normalized.json").is_file())
            self.assertFalse((root / "assets" / "asset-manifest.json").exists())
            self.assertFalse((root / "src" / "index.html").exists())
            self.assertFalse((root / "artifacts" / "iterations" / "001").exists())

    def test_fetch_gateway_models_preserves_order_and_removes_duplicates(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"data": [
            {"id": "gpt-image-2"}, {"id": "gpt-image-2"}, {"id": "image-model-next"}, {"id": ""},
        ]}).encode()
        with patch("app.urllib.request.urlopen", return_value=response) as mocked:
            models = fetch_gateway_models("https://gateway.example/v1/", "secret-key")
        self.assertEqual(models, ["gpt-image-2", "image-model-next"])
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")

    def test_fetch_gateway_models_allows_http_gateway(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":[{"id":"image-model"}]}'
        with patch("app.urllib.request.urlopen", return_value=response):
            self.assertEqual(fetch_gateway_models("http://gateway.example/v1", "secret-key"), ["image-model"])

    def test_fetch_gateway_models_rejects_invalid_response(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"object":"list"}'
        with patch("app.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "缺少 data 列表"):
                fetch_gateway_models("https://gateway.example/v1", "secret-key")

    def test_pending_gate_only_blocks_raster_elements(self):
        pending = pending_raster_targets({"regions": [{"targets": [
            {"id": "image", "elementType": "image-asset", "processingMode": "complete-crop", "reviewStatus": "needs-review"},
            {"id": "composite", "elementType": "complete-composite", "processingMode": "complete-crop", "reviewStatus": "needs-review"},
            {"id": "code", "elementType": "code-element", "processingMode": "none", "reviewStatus": "needs-review"},
        ]}]})
        self.assertEqual([item["id"] for item in pending], ["image", "composite"])

    def test_background_repair_result_is_cropped_to_aspect_not_stretched(self):
        square = Image.new("RGB", (100, 100), "red")
        for y in range(25, 75):
            for x in range(100):
                square.putpixel((x, y), (0, 120, 255))
        result = fit_to_source_aspect(square, (200, 100))
        self.assertEqual(result.size, (200, 100))
        self.assertEqual(result.getpixel((100, 50)), (0, 120, 255))
        self.assertNotEqual(result.getpixel((100, 0)), (255, 0, 0))

    def test_background_repair_regeneration_keeps_previous_versions(self):
        previous = {
            "id": "repair-001", "activeVersion": "processed-old",
            "versions": [{"id": "original", "file": "original.png"}, {"id": "processed-old", "file": "old.png"}],
            "processingHistory": [{"outputVersion": "processed-old"}],
        }
        current = {
            "id": "repair-001", "activeVersion": "processed-new",
            "versions": [{"id": "original", "file": "original.png"}, {"id": "processed-new", "file": "new.png"}],
            "processingHistory": [{"outputVersion": "processed-new"}],
        }
        merged = merge_repair_versions(previous, current)
        self.assertEqual([item["id"] for item in merged["versions"]], ["original", "processed-old", "processed-new"])
        self.assertEqual(merged["activeVersion"], "processed-new")
        self.assertEqual(len(merged["processingHistory"]), 2)

    def test_vision_transport_preserves_dimensions_and_reduces_png_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "reference.png"
            image = Image.effect_noise((750, 1334), 80).convert("RGB")
            image.save(source)
            data_url, metadata = vision_image_data_url(source)
            self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
            self.assertEqual((metadata["width"], metadata["height"]), (750, 1334))
            self.assertLess(metadata["transportBytes"], metadata["sourceBytes"])

    def test_chat_request_retries_503_and_redacts_image_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            payload = {
                "model": "vision-model",
                "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,SECRET"}}]}],
                "response_format": {"type": "json_object"},
            }
            unavailable = urllib.error.HTTPError(
                "http://example/v1/chat/completions", 503, "Unavailable", {}, io.BytesIO(b'{"error":"upstream busy"}')
            )
            success = MagicMock()
            success.__enter__.return_value.read.return_value = b'{"choices":[]}'
            with patch("app.urllib.request.urlopen", side_effect=[unavailable, success]), patch("app.time.sleep"):
                response = post_chat_completion("http://example/v1", "secret-key", payload, run_dir)
            self.assertEqual(response, b'{"choices":[]}')
            self.assertIn("upstream busy", (run_dir / "error-response-attempt-1.txt").read_text())
            logged = (run_dir / "request-attempt-1.json").read_text()
            self.assertNotIn("SECRET", logged)
            self.assertNotIn("secret-key", logged)

    def test_chat_completion_reads_sse_stream_and_reassembles_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            class Headers:
                def get_content_type(self): return "text/event-stream"
            class StreamResponse:
                headers = Headers()
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def readline(self):
                    if not hasattr(self, "lines"):
                        self.lines = iter([
                            b'data: {"choices":[{"delta":{"content":"{"}}]}\n\n',
                            b'data: {"choices":[{"delta":{"content":"\\"spec\\":\\"ok}"}}]}\n\n',
                            b'data: [DONE]\n\n',
                        ])
                    return next(self.lines, b'')
            payload = {"model": "vision-model", "messages": [{"role": "user", "content": "go"}]}
            with patch("app.urllib.request.urlopen", return_value=StreamResponse()):
                response = post_chat_completion("http://example/v1", "secret-key", payload, Path(temporary))
            document = json.loads(response)
            self.assertEqual(document["choices"][0]["message"]["content"], '{"spec":"ok}')
            sent = json.loads((Path(temporary) / "request-attempt-1.json").read_text())
            self.assertTrue(sent["stream"])

    def test_normalizes_legacy_modes_and_ai_review_gate(self):
        local = normalize_target({"extractionMode": "background-script"})
        self.assertEqual(local["elementType"], "image-asset")
        self.assertEqual(local["processingMode"], "local-transparent")
        self.assertEqual(local["backgroundTolerance"], 34)
        self.assertEqual(local["edgeFeather"], 48)

        suggested = normalize_regions_document({
            "regions": [{"targets": [{"extractionMode": "ai"}]}],
        }, suggested=True)
        target = suggested["regions"][0]["targets"][0]
        self.assertEqual(target["processingMode"], "ai-transparent")
        self.assertEqual(target["reviewStatus"], "needs-review")
        self.assertEqual(target["suggestion"]["source"], "ai")

    def test_forces_code_elements_to_no_image_processing(self):
        target = normalize_target({"elementType": "code-element", "processingMode": "ai-transparent"})
        self.assertEqual(target["processingMode"], "none")

    def test_approval_keeps_versions_and_code_element_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "assets" / "split-web"
            source = split / "local" / "hero-v2.png"
            source.parent.mkdir(parents=True)
            Image.new("RGBA", (12, 8), "red").save(source)
            original = split / "originals" / "hero.png"
            original.parent.mkdir()
            Image.new("RGB", (12, 8), "white").save(original)
            draft_path = split / "asset-manifest.draft.json"
            draft = {
                "assets": [{
                    "id": "asset-001",
                    "file": "local/hero-v2.png",
                    "elementType": "image-asset",
                    "processingMode": "local-transparent",
                    "versions": [
                        {"id": "original", "file": "originals/hero.png"},
                        {"id": "processed-002", "file": "local/hero-v2.png"},
                    ],
                    "activeVersion": "processed-002",
                    "matchedSourceTarget": {"purpose": "主视觉", "pageBox": {"x": 2, "y": 3, "width": 12, "height": 8}},
                }],
                "codeElements": [{"id": "code-001", "elementType": "code-element"}],
            }
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            result = approve_asset_manifest(root, draft_path, {
                "assets": [{"id": "asset-001", "approved": True, "name": "hero-art", "parent": "hero", "zIndex": 2, "bbox": {"x": 2, "y": 3, "width": 12, "height": 8}}],
                "codeElements": [{"id": "code-001", "approved": True, "name": "hero-title", "parent": "hero", "zIndex": 3, "placement": {"bbox": {"x": 4, "y": 5, "width": 20, "height": 6}}}],
            })

            self.assertEqual(result["approvedCount"], 1)
            self.assertEqual(result["codeElementCount"], 1)
            manifest = result["manifest"]
            self.assertEqual(manifest["assets"][0]["activeVersion"], "processed-002")
            self.assertEqual(len(manifest["assets"][0]["versions"]), 2)
            self.assertEqual(manifest["assets"][0]["placement"]["bbox"]["x"], 2)
            self.assertEqual(manifest["codeElements"][0]["placement"]["bbox"]["x"], 4)
            self.assertTrue((root / "assets" / "approved" / "hero-art.png").is_file())

    def test_rejects_duplicate_names_and_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft_path = root / "assets" / "split-web" / "asset-manifest.draft.json"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text(json.dumps({
                "assets": [{"id": "asset-001", "file": "missing.png"}],
                "codeElements": [{"id": "code-001"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不能重复"):
                approve_asset_manifest(root, draft_path, {
                    "assets": [{"id": "asset-001", "approved": True, "name": "same-name"}],
                    "codeElements": [{"id": "code-001", "approved": True, "name": "same-name"}],
                })
            with self.assertRaisesRegex(ValueError, "源文件不存在"):
                approve_asset_manifest(root, draft_path, {
                    "assets": [{"id": "asset-001", "approved": True, "name": "hero-image"}],
                    "codeElements": [],
                })


if __name__ == "__main__":
    unittest.main()
