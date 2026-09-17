import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import extract_regions as extraction


class ExtractionRetryTest(unittest.TestCase):
    def run_case(self, recover):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (20, 20), 'white').save(root / 'region.png')
            region = {'id': 'region', 'cropFile': 'region.png', 'targets': [{'id': 'one'}]}
            prompts = []

            def fake_api(prompt, crop, output, records, size, quality):
                prompts.append(prompt)
                records.mkdir(parents=True, exist_ok=True)
                mode = 'RGBA' if recover and len(prompts) > 1 else 'RGB'
                color = (255, 0, 0, 0) if mode == 'RGBA' else 'white'
                image = Image.new(mode, (20, 20), color)
                image.putpixel((10, 10), (255, 0, 0, 255) if mode == 'RGBA' else (255, 0, 0))
                image.save(output)

            with patch.object(extraction, 'call_edit_api', side_effect=fake_api), patch.object(extraction.time, 'sleep'):
                result = extraction.run_job(region, root, root / 'raw', root / 'runs', '1024x1024', 'medium', 2)
                self.assertIn('CORRECTION', prompts[1])
                if recover:
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(len(prompts), 2)
                    extraction.validate_transparency(root / 'raw' / 'region.png')
                    cached = extraction.run_job(region, root, root / 'raw', root / 'runs', '1024x1024', 'medium', 2)
                    self.assertEqual(cached['status'], 'skipped')
                    self.assertEqual(len(prompts), 2)
                else:
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(len(prompts), 3)
                    self.assertFalse((root / 'raw' / 'region.png').exists())
                    self.assertIn('透明通道', result['error'])

    def test_invalid_transparency_retries_then_caches_success(self):
        self.run_case(True)

    def test_invalid_transparency_stops_at_limit_without_publishing(self):
        self.run_case(False)
