import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from scripts.extract_uniform_background import extract_targets


class UniformBackgroundExtractionTest(unittest.TestCase):
    def test_removes_only_edge_connected_background_and_records_trimmed_placement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            image = Image.new("RGB", (60, 50), (245, 245, 245))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 15, 39, 34), fill=(20, 90, 180))
            draw.rectangle((27, 22, 32, 27), fill=(245, 245, 245))
            image.save(reference)
            regions = {
                "canvas": {"width": 60, "height": 50},
                "regions": [{
                    "id": "region-001", "x": 10, "y": 5, "width": 40, "height": 40, "approved": True,
                    "targets": [{
                        "id": "target-001", "x": 5, "y": 5, "width": 30, "height": 30,
                        "approved": True, "extractionMode": "background-script",
                        "backgroundTolerance": 12, "edgeFeather": 0,
                    }],
                }],
            }
            regions_path = root / "regions.json"
            regions_path.write_text(json.dumps(regions), encoding="utf-8")

            manifest = extract_targets(reference, regions_path, root / "output")

            self.assertEqual(len(manifest["assets"]), 1)
            asset = manifest["assets"][0]
            self.assertEqual(asset["extractionMethod"], "background-script")
            self.assertEqual(asset["placement"]["bbox"], {"x": 18, "y": 13, "width": 24, "height": 24})
            with Image.open(root / "output" / asset["file"]) as extracted:
                self.assertEqual(extracted.mode, "RGBA")
                self.assertEqual(extracted.getpixel((0, 0))[3], 0)
                # The white hole is enclosed by the blue object, so it must remain opaque.
                self.assertEqual(extracted.getpixel((12, 12))[3], 255)

    def test_ignores_ai_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            Image.new("RGB", (20, 20), "white").save(reference)
            regions_path = root / "regions.json"
            regions_path.write_text(json.dumps({
                "canvas": {"width": 20, "height": 20},
                "regions": [{"id": "region-001", "x": 0, "y": 0, "width": 20, "height": 20, "targets": [
                    {"id": "target-001", "x": 2, "y": 2, "width": 10, "height": 10, "extractionMode": "ai"}
                ]}],
            }), encoding="utf-8")
            manifest = extract_targets(reference, regions_path, root / "output")
            self.assertEqual(manifest["assets"], [])

    def test_complete_crop_and_code_element_keep_distinct_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            Image.new("RGB", (40, 30), (30, 60, 90)).save(reference)
            regions_path = root / "regions.json"
            regions_path.write_text(json.dumps({
                "canvas": {"width": 40, "height": 30},
                "regions": [{"id": "region-001", "x": 0, "y": 0, "width": 40, "height": 30, "targets": [
                    {"id": "target-001", "x": 2, "y": 3, "width": 20, "height": 10, "name": "hero-banner", "elementType": "complete-composite", "processingMode": "complete-crop", "reviewStatus": "confirmed"},
                    {"id": "target-002", "x": 24, "y": 4, "width": 12, "height": 8, "name": "login-button", "elementType": "code-element", "processingMode": "none", "reviewStatus": "confirmed", "zIndex": 3},
                ]}],
            }), encoding="utf-8")

            manifest = extract_targets(reference, regions_path, root / "output")

            self.assertEqual(len(manifest["assets"]), 1)
            self.assertEqual(len(manifest["codeElements"]), 1)
            asset = manifest["assets"][0]
            self.assertEqual(asset["elementType"], "complete-composite")
            self.assertEqual(asset["processingMode"], "complete-crop")
            self.assertEqual(asset["placement"]["bbox"], {"x": 2, "y": 3, "width": 20, "height": 10})
            self.assertEqual([version["kind"] for version in asset["versions"]], ["source-crop", "complete-crop"])
            self.assertTrue((root / "output" / asset["versions"][0]["file"]).is_file())
            self.assertTrue((root / "output" / asset["versions"][1]["file"]).is_file())
            self.assertEqual(manifest["codeElements"][0]["placement"]["bbox"], {"x": 24, "y": 4, "width": 12, "height": 8})

    def test_unconfirmed_ai_suggestion_is_not_processed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            Image.new("RGB", (20, 20), "white").save(reference)
            regions_path = root / "regions.json"
            regions_path.write_text(json.dumps({
                "canvas": {"width": 20, "height": 20},
                "regions": [{"id": "region-001", "x": 0, "y": 0, "width": 20, "height": 20, "targets": [
                    {"id": "target-001", "x": 2, "y": 2, "width": 10, "height": 10, "elementType": "image-asset", "processingMode": "local-transparent", "reviewStatus": "needs-review"}
                ]}],
            }), encoding="utf-8")
            manifest = extract_targets(reference, regions_path, root / "output")
            self.assertEqual(manifest["assets"], [])
            self.assertEqual(manifest["codeElements"], [])


if __name__ == "__main__":
    unittest.main()
