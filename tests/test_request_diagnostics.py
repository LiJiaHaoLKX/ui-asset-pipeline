import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from scripts.gpt_image_api import _send
from scripts.request_diagnostics import diagnose


class DiagnosticTest(unittest.TestCase):
    def test_http_error_records_evidence_without_credentials(self):
        headers = Message()
        headers['content-type'] = 'application/json'
        headers['x-request-id'] = 'request-123'
        error = urllib.error.HTTPError('http://relay', 524, 'timeout', headers, io.BytesIO(b'{"error":"bad_response_status_code"}'))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            request = urllib.request.Request('http://relay/v1/images/generations?token=secret', headers={'Authorization': 'Bearer private-key'})
            with patch('scripts.gpt_image_api.urllib.request.urlopen', side_effect=error):
                with self.assertRaises(urllib.error.HTTPError):
                    _send(request, folder, 180)
            detail = json.loads((folder / 'diagnostic.json').read_text(encoding='utf-8'))
            self.assertEqual(detail['httpStatus'], 524)
            self.assertEqual(detail['stage'], '图片 API 请求')
            self.assertEqual(detail['responseHeaders']['x-request-id'], 'request-123')
            self.assertNotIn('secret', json.dumps(detail))
            self.assertNotIn('private-key', json.dumps(detail))
            self.assertEqual(error.diagnostic, detail)

    def test_timeout_is_not_reported_as_remote_status(self):
        detail = diagnose(TimeoutError(), '图片 API 连接或等待响应')
        self.assertIsNone(detail['httpStatus'])
        self.assertEqual(detail['origin'], '本地等待或网络连接失败')

    def test_uninstrumented_failure_is_unknown(self):
        detail = diagnose(RuntimeError('HTTP 524'), '任务执行（未记录更细阶段）')
        self.assertEqual(detail['origin'], '来源待确认')
