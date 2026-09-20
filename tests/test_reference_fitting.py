import tempfile
import unittest
from pathlib import Path
from PIL import Image
from scripts.gpt_image_api import fit_reference_size


class ReferenceFittingTest(unittest.TestCase):
    def test_reference_generation_path_does_not_normalize(self):
        import app
        from unittest.mock import patch
        with patch('scripts.gpt_image_api.call_generate_api') as generate, patch.object(app, 'GENERATE_LOCK'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'prompts').mkdir(); (root / 'reference').mkdir()
                (root / 'prompts/reference-page.txt').write_text('prompt')
                with patch.object(app, 'data_root', return_value=root), patch.object(app, 'load_project_settings', return_value={'generationSize': '750x1334', 'quality': 'high'}), patch.object(app, 'load_state', return_value={}), patch.object(app, 'update_phase'), patch.object(app, 'file_url', side_effect=lambda p: str(p)), patch.object(app, 'image_info', return_value={'width': 1024, 'height': 1024}), patch.object(app, 'read_json', side_effect=lambda p, d=None: d), patch.object(app, 'read_env', return_value={}):
                    app.generate_reference({'confirmed': True, 'prompt': 'prompt'})
            self.assertNotIn('normalized_size', generate.call_args.kwargs)

    def test_square_preserves_left_and_right_edges_on_portrait_canvas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new('RGB', (100, 100), 'white')
            image.paste('red', (0, 0, 10, 100))
            image.paste('blue', (90, 0, 100, 100))
            output = root / 'reference.png'
            image.save(output)
            fit_reference_size(output, '100x180', root)
            with Image.open(output) as result:
                self.assertEqual(result.size, (100, 180))
                self.assertEqual(result.getpixel((0, 90)), (255, 0, 0))
                self.assertEqual(result.getpixel((99, 90)), (0, 0, 255))
                self.assertEqual(result.getpixel((50, 0)), (255, 255, 255))
            with Image.open(root / 'original-response.png') as original:
                self.assertEqual(original.tobytes(), image.tobytes())
