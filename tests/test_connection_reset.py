import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from app import post_chat_completion
from scripts.request_diagnostics import diagnose


class ConnectionResetTest(unittest.TestCase):
    def test_reset_is_retried_and_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            error = urllib.error.URLError(ConnectionResetError(10054, '远程主机强迫关闭'))
            with patch('app.urllib.request.urlopen', side_effect=[error, error, error]), patch('app.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'WinError 10054.*3 次'):
                    post_chat_completion('http://test/v1', 'key', {'messages': []}, Path(directory))

    def test_diagnostic_distinguishes_reset_from_http_status(self):
        detail = diagnose(urllib.error.URLError(ConnectionResetError(10054, 'reset')), '文本模型请求')
        self.assertEqual(detail['origin'], '远端连接被关闭')
        self.assertIsNone(detail['httpStatus'])
