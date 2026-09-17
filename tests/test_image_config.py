import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.gpt_image_api import read_image_config, call_generate_api, call_edit_api


class ImageConfigTest(unittest.TestCase):
    def test_saved_values_override_old_process_without_mutating_it(self):
        old = {'GPT_IMAGE_BASE_URL': 'http://old/v1', 'GPT_IMAGE_API_KEY': 'old-key', 'GPT_IMAGE_MODEL': 'old-model'}
        text = 'GPT_IMAGE_BASE_URL=https://new/v1\nGPT_IMAGE_API_KEY=new-key\nGPT_IMAGE_MODEL=new-model'
        with patch.dict(os.environ, old), patch.object(Path, 'exists', return_value=True), patch.object(Path, 'read_text', return_value=text):
            first = read_image_config()
            self.assertEqual(first['GPT_IMAGE_BASE_URL'], 'https://new/v1')
            self.assertEqual(first['GPT_IMAGE_API_KEY'], 'new-key')
            self.assertEqual(os.environ['GPT_IMAGE_API_KEY'], 'old-key')
            with patch.object(Path, 'read_text', return_value=text.replace('new', 'changed')):
                self.assertEqual(read_image_config()['GPT_IMAGE_API_KEY'], 'changed-key')
            self.assertEqual(first['GPT_IMAGE_API_KEY'], 'new-key')

    def test_generate_and_edit_use_same_fresh_address_key_model(self):
        config = {'GPT_IMAGE_BASE_URL': 'https://new/v1', 'GPT_IMAGE_API_KEY': 'new-key', 'GPT_IMAGE_MODEL': 'new-model'}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch('scripts.gpt_image_api.read_image_config', return_value=config), patch('scripts.gpt_image_api._send', return_value=(b'', 'image/png')) as send, patch('scripts.gpt_image_api._save_result'), patch('scripts.gpt_image_api.normalize_size'), patch('scripts.gpt_image_api._multipart', return_value=(b'', 'multipart/form-data')), patch('builtins.print'):
                call_generate_api('prompt', folder / 'out.png', folder / 'generate', '750x1334', 'medium', 1)
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, 'https://new/v1/images/generations')
                self.assertEqual(request.get_header('Authorization'), 'Bearer new-key')
                self.assertEqual(json.loads(request.data)['model'], 'new-model')
                call_edit_api('prompt', folder / 'in.png', folder / 'out.png', folder / 'edit', '1024x1024', 'medium')
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, 'https://new/v1/images/edits')
                self.assertEqual(request.get_header('Authorization'), 'Bearer new-key')
