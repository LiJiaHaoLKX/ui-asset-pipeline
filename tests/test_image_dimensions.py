import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_dimensions import normalize_prompt_dimensions, normalize_size
from scripts.gpt_image_api import call_generate_api


class ImageDimensionsTest(unittest.TestCase):
    def test_rounds_canvas_dimensions_up_to_multiple_of_16(self):
        self.assertEqual(normalize_size('750x1334'), '752x1344')
        self.assertEqual(normalize_size('1024x1536'), '1024x1536')

    def test_rounds_explicit_prompt_canvas_dimensions(self):
        self.assertEqual(normalize_prompt_dimensions('Canvas: 750 × 1334.'), 'Canvas: 752x1344.')

    def test_generate_request_uses_normalized_size_and_prompt(self):
        config = {'GPT_IMAGE_BASE_URL': 'https://image.test/v1', 'GPT_IMAGE_API_KEY': 'key', 'GPT_IMAGE_MODEL': 'model'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('scripts.gpt_image_api.read_image_config', return_value=config), \
                    patch('scripts.gpt_image_api._send', return_value=(b'', 'image/png')), \
                    patch('scripts.gpt_image_api._save_result'), patch('scripts.gpt_image_api.normalize_size'), \
                    patch('builtins.print'):
                call_generate_api('Canvas: 750x1334', root / 'out.png', root / 'run', '750x1334', 'medium', 1)
            request = json.loads((root / 'run' / 'request.json').read_text(encoding='utf-8'))
            self.assertEqual(request['size'], '752x1344')
            self.assertIn('752x1344', request['prompt'])


if __name__ == '__main__':
    unittest.main()
