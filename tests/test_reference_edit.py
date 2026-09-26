import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app
from scripts.gpt_image_api import _multipart


class ReferenceEditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'reference').mkdir()
        Image.new('RGB', (32, 32), 'red').save(self.root / 'reference/reference-page.png')
        uploaded = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(uploaded, format='PNG')
        self.encoded = base64.b64encode(uploaded.getvalue()).decode()

    def run_edit(self, include_original):
        calls = []

        def fake_edit(prompt, images, output, run_dir, *args, **kwargs):
            calls.append((prompt, images, run_dir))
            Image.new('RGB', (32, 32), 'green').save(output)

        with patch.object(app, 'data_root', return_value=self.root), patch.object(app, 'load_project_settings', return_value={'generationSize': '1024x1024', 'quality': 'medium'}), patch.object(app, 'read_env', return_value={}), patch.object(app, 'update_phase'), patch.object(app, 'image_info', return_value={}), patch.object(app, 'file_url', return_value=''), patch('scripts.gpt_image_api.call_edit_api', side_effect=fake_edit):
            app.edit_reference({'confirmed': True, 'prompt': 'Change the button', 'image': self.encoded, 'includeOriginal': include_original})
        return calls[0]

    def test_two_images_have_fixed_order_and_roles(self):
        prompt, images, _ = self.run_edit(True)
        self.assertEqual([path.name for path in images], ['image-1-original.png', 'image-2-uploaded.png'])
        self.assertIn('Image 1 is the generated original UI page', prompt)
        self.assertIn('Image 2 is the uploaded reference image', prompt)
        with Image.open(self.root / 'reference/reference-page.png') as result:
            self.assertEqual(result.getpixel((0, 0)), (0, 128, 0))

    def test_unchecked_sends_only_upload(self):
        prompt, images, _ = self.run_edit(False)
        self.assertEqual([path.name for path in images], ['image-1-uploaded.png'])
        self.assertIn('Image 1 is the uploaded reference image', prompt)
        self.assertNotIn('Image 2', prompt)

    def test_failure_preserves_current_reference(self):
        original = (self.root / 'reference/reference-page.png').read_bytes()
        with patch.object(app, 'data_root', return_value=self.root), patch.object(app, 'load_project_settings', return_value={'generationSize': '1024x1024', 'quality': 'medium'}), patch.object(app, 'read_env', return_value={}), patch('scripts.gpt_image_api.call_edit_api', side_effect=RuntimeError('upstream failed')):
            with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
                app.edit_reference({'confirmed': True, 'prompt': 'Change', 'image': self.encoded})
        self.assertEqual((self.root / 'reference/reference-page.png').read_bytes(), original)

    def test_multipart_keeps_file_order(self):
        first = self.root / 'first.png'
        second = self.root / 'second.png'
        first.write_bytes(b'FIRST')
        second.write_bytes(b'SECOND')
        body, _ = _multipart({'prompt': 'edit'}, 'image[]', [first, second])
        self.assertLess(body.index(b'filename="first.png"'), body.index(b'filename="second.png"'))
        self.assertEqual(body.count(b'name="image[]"'), 2)


if __name__ == '__main__':
    unittest.main()
