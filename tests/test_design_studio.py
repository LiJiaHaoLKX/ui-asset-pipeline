import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from design_studio import DesignStudio


class DesignStudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.official = {'original': True}
        self.env = {'GPT_TEXT_BASE_URL': 'http://text.test/v1', 'GPT_TEXT_API_KEY': 'text-secret',
                    'GPT_TEXT_MODEL': 'vision-model', 'GPT_IMAGE_BASE_URL': 'http://image.test/v1',
                    'GPT_IMAGE_API_KEY': 'image-secret'}
        self.proposal = {'summary': '清晰易读', 'previewPrompt': 'A sample board', 'spec': {
            'canvas': {'width': 750, 'height': 1334}, 'colors': {'text': '#111111'},
            'typography': {'body': 28}, 'spacing': [8, 16], 'radii': {'card': 16},
            'components': {'button': {'height': 88}}, 'assetRules': ['Keep baked text']}}
        self.chat = Mock(side_effect=lambda *args: json.dumps({'choices': [{'message': {'content': json.dumps(self.proposal)}}]}))
        self.save_design = Mock(side_effect=lambda value: setattr(self, 'official', value))
        self.studio = DesignStudio(self.root, lambda: self.env, self.read, self.write,
                                  lambda: self.official, self.save_design, self.chat,
                                  lambda path: ('data:image/png;base64,fixture', {}))
        self.studio.save_brief({'brief': {'product': '游戏商城', 'canvas': '750x1334'}})
        buffer = io.BytesIO()
        Image.new('RGB', (12, 16), 'red').save(buffer, format='PNG')
        self.png = buffer.getvalue()

    @staticmethod
    def read(path, default):
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def generate(self):
        self.studio.generate({})
        return self.studio.current(self.studio.state())

    def preview(self, version):
        with patch('scripts.gpt_image_api._send', return_value=(self.png, 'image/png')) as send:
            self.studio.preview({'version': version['id']})
        return send

    def test_generation_sends_brief_and_references_without_adopting(self):
        self.studio.upload({'name': 'style.png', 'data': base64.b64encode(self.png).decode()})
        version = self.generate()
        payload = self.chat.call_args.args[2]
        content = payload['messages'][1]['content']
        self.assertEqual(json.loads(content[0]['text'])['brief']['product'], '游戏商城')
        self.assertEqual(content[1]['type'], 'image_url')
        self.assertEqual(version['inputRevision'], self.studio.state()['revision'])
        self.save_design.assert_not_called()

    def test_preview_uses_saved_config_and_never_logs_key(self):
        version = self.generate()
        send = self.preview(version)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'http://image.test/v1/images/generations')
        self.assertEqual(request.get_header('Authorization'), 'Bearer image-secret')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], 'gpt-image-2.5')
        self.assertEqual(payload['n'], 1)
        self.assertNotIn('image-secret', (send.call_args.args[1] / 'request.json').read_text())
        self.assertTrue(self.studio.current(self.studio.state())['previewId'])
        self.save_design.assert_not_called()

    def test_feedback_is_fresh_and_preserves_history_without_previous_context(self):
        first = self.generate()
        self.preview(first)
        self.studio.generate({'feedback': '文字大一点'})
        content = self.chat.call_args.args[2]['messages'][1]['content']
        request = json.loads(content[0]['text'])
        self.assertEqual(request['feedback'], '文字大一点')
        self.assertNotIn('previousSpec', request)
        self.assertEqual(len(content), 1)
        state = self.studio.state()
        self.assertEqual(len(state['versions']), 2)
        self.assertTrue(state['versions'][0]['previewId'])
        self.assertIsNone(self.studio.current(state)['previewId'])

    def test_failed_generation_keeps_draft_and_official(self):
        first = self.generate()
        self.proposal['summary'] = None
        with self.assertRaises(ValueError):
            self.studio.generate({})
        self.assertEqual(self.studio.state()['current'], first['id'])
        self.assertEqual(self.official, {'original': True})

    def test_adoption_requires_current_preview_and_unchanged_inputs(self):
        version = self.generate()
        with self.assertRaises(ValueError):
            self.studio.adopt({'version': version['id']})
        self.preview(version)
        with self.assertRaises(ValueError):
            self.studio.adopt({'version': 'other'})
        self.studio.save_brief({'brief': {'product': '新产品'}})
        with self.assertRaisesRegex(ValueError, '重新生成'):
            self.studio.adopt({'version': version['id']})
        with self.assertRaisesRegex(ValueError, '重新生成'):
            self.studio.preview({'version': version['id']})
        self.save_design.assert_not_called()

    def test_adoption_backs_up_and_preserves_page_files(self):
        sentinel = self.root / 'pages' / 'page-001' / 'state.json'
        self.write(sentinel, {'approved': True})
        version = self.generate()
        self.preview(version)
        self.studio.adopt({'version': version['id']})
        self.assertEqual(self.official, version['spec'])
        backup = next((self.studio.folder / 'adoptions').glob('*.json'))
        self.assertEqual(self.read(backup, {})['previous'], {'original': True})
        self.assertEqual(self.read(sentinel, {}), {'approved': True})

    def test_async_reserves_lock_and_records_errors(self):
        queued = []
        self.studio.start('generate', {'confirmed': True}, lambda kind, fn: queued.append(fn) or 'job')
        self.assertTrue(self.studio.public()['busy'])
        with self.assertRaises(ValueError):
            self.studio.start('generate', {'confirmed': True}, Mock())
        with self.assertRaises(ValueError):
            self.studio.save_brief({'brief': {}})
        self.chat.side_effect = RuntimeError('upstream unavailable')
        with self.assertRaises(RuntimeError):
            queued[0]()
        self.assertFalse(self.studio.public()['busy'])
        self.assertEqual(self.studio.public()['task']['status'], 'failed')
        self.assertIn('upstream unavailable', self.studio.public()['task']['message'])

    def test_upload_limit_removal_and_safe_path(self):
        body = {'data': base64.b64encode(self.png).decode()}
        for _ in range(5):
            self.studio.upload(body)
        with self.assertRaises(ValueError):
            self.studio.upload(body)
        identifier = self.studio.state()['references'][0]['id']
        self.studio.remove({'id': identifier})
        self.assertTrue(self.studio.image_path(identifier).exists())
        self.assertEqual(len(self.studio.state()['references']), 4)
        with self.assertRaises(ValueError):
            self.studio.image_path('../../.env')

    def test_interrupted_task_is_reported(self):
        self.write(self.studio.task_path, {'status': 'running', 'action': 'generate'})
        self.assertEqual(self.studio.public()['task']['status'], 'failed')

    def test_failed_preview_download_reuses_response_without_generation(self):
        version = self.generate()
        raw = json.dumps({'data': [{'url': 'https://cdn.example/image.png'}]}).encode()
        from scripts.gpt_image_api import ImageDownloadError
        def fail_download(body, content_type, output, run):
            (run / 'response.json').write_bytes(body)
            raise ImageDownloadError('download failed')
        with patch('scripts.gpt_image_api._send', return_value=(raw, 'application/json')), patch('scripts.gpt_image_api._save_result', side_effect=fail_download):
            with self.assertRaises(ImageDownloadError):
                self.studio.preview({'version': version['id']})
        def save_download(body, content_type, output, run):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self.png)
        with patch('scripts.gpt_image_api._send') as generate, patch('scripts.gpt_image_api._save_result', side_effect=save_download):
            self.studio.preview({'version': version['id']})
            generate.assert_not_called()
        self.assertTrue(self.studio.current(self.studio.state())['previewId'])


if __name__ == '__main__':
    unittest.main()
