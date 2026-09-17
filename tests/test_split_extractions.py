import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


class SplitExtractionsTest(unittest.TestCase):
    def test_uses_regions_parent_for_original_crops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crops = root / "crops"
            raw = root / "raw"
            output = root / "split"
            crops.mkdir()
            raw.mkdir()

            Image.new("RGBA", (40, 20), "white").save(crops / "region-001.png")
            extracted = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
            draw = ImageDraw.Draw(extracted)
            draw.rectangle((2, 2, 10, 12), fill="red")
            draw.rectangle((24, 3, 34, 14), fill="blue")
            extracted.save(raw / "region-001.png")

            target_base = {
                "approved": True,
                "reviewStatus": "confirmed",
                "processingMode": "ai-transparent",
                "elementType": "image-asset",
                "y": 2,
                "height": 11,
            }
            document = {
                "canvas": {"width": 40, "height": 20},
                "regions": [{
                    "id": "region-001",
                    "x": 0,
                    "y": 0,
                    "width": 40,
                    "height": 20,
                    "cropFile": "region-001.png",
                    "cropBox": {"x": 0, "y": 0, "width": 40, "height": 20},
                    "targets": [
                        {**target_base, "id": "target-001", "x": 2, "width": 9, "markedCropBox": {"x": 2, "y": 2, "width": 9, "height": 11}},
                        {**target_base, "id": "target-002", "x": 24, "width": 11, "markedCropBox": {"x": 24, "y": 3, "width": 11, "height": 12}},
                    ],
                }],
            }
            regions = crops / "regions.normalized.json"
            regions.write_text(json.dumps(document), encoding="utf-8")

            script = Path(__file__).resolve().parents[1] / "scripts" / "split_extractions.py"
            completed = subprocess.run(
                [sys.executable, str(script), str(regions), str(raw), str(output), "--min-area", "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads((output / "asset-manifest.draft.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["assets"]), 2)
            self.assertTrue((output / "originals" / "region-001-target-001.png").is_file())
            self.assertTrue((output / "originals" / "region-001-target-002.png").is_file())


if __name__ == "__main__":
    unittest.main()
