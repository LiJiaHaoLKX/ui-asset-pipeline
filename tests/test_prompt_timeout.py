import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import generate_image_prompt


class PromptTimeoutTest(unittest.TestCase):
    def run_request(self, root, responses):
        env = {'GPT_TEXT_BASE_URL': 'http://test/v1', 'GPT_TEXT_API_KEY': 'test', 'GPT_TEXT_MODEL': 'test'}
        with patch('app.read_env', return_value=env), patch('app.data_root', return_value=root), patch('app.current_page', return_value={'name': 'test'}), patch('app.load_project_design', return_value={'colors': {'primary': '#fff'}}), patch('app.urllib.request.urlopen', side_effect=responses) as request, patch('app.time.sleep'):
            result = generate_image_prompt({'confirmed': True, 'notes': '测试页面'})
            return result, request.call_count

    @staticmethod
    def timeout():
        return urllib.error.HTTPError('http://test', 524, 'timeout', {}, io.BytesIO(b'<!DOCTYPE html>timeout'))

    def test_524_retries_then_saves_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'prompts').mkdir()
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {'content': 'new prompt'}}]}).encode()
            result, calls = self.run_request(root, [self.timeout(), response])
            self.assertEqual(calls, 2)
            self.assertEqual(result['prompt'], 'new prompt')
            self.assertEqual((root / 'prompts/reference-page.txt').read_text().strip(), 'new prompt')

    def test_524_exhaustion_keeps_previous_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'prompts').mkdir()
            target = root / 'prompts/reference-page.txt'
            target.write_text('original')
            with self.assertRaisesRegex(RuntimeError, 'HTTP 524.*3 次'):
                self.run_request(root, [self.timeout() for _ in range(3)])
            self.assertEqual(target.read_text(), 'original')
            self.assertEqual(len(list(root.glob('runs/gpt-text/*/request-attempt-*.json'))), 3)
