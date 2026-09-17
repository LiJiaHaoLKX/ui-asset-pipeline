import io
import json
import tempfile
import unittest
import urllib.error
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.gpt_image_api import _save_result, ImageDownloadError


class ImageDownloadTest(unittest.TestCase):
    def test_tls_eof_uses_verified_system_download_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error = urllib.error.URLError(ssl.SSLEOFError(8, 'unexpected EOF'))
            with patch('scripts.gpt_image_api.urllib.request.urlopen', side_effect=error), patch('scripts.gpt_image_api._recover_tls_download', return_value=True) as fallback:
                _save_result(b'{"data":[{"url":"https://cdn.example/image.png"}]}', 'application/json', root / 'image.png', root)
                fallback.assert_called_once_with('https://cdn.example/image.png', root / 'image.png')

    def test_download_has_user_agent_but_no_api_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'image-data'
            with patch('scripts.gpt_image_api.urllib.request.urlopen', return_value=response) as opener:
                _save_result(json.dumps({'data': [{'url': 'https://cdn.example/image.png'}]}).encode(), 'application/json', root / 'image.png', root)
            request = opener.call_args.args[0]
            self.assertEqual(request.get_header('User-agent'), 'Mozilla/5.0')
            self.assertIsNone(request.get_header('Authorization'))
            self.assertEqual((root / 'image.png').read_bytes(), b'image-data')

    def test_download_error_is_distinct_from_generation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = urllib.error.HTTPError('https://cdn.example/image.png', 403, 'Forbidden', {}, io.BytesIO(b'error code: 1010'))
            with patch('scripts.gpt_image_api.urllib.request.urlopen', side_effect=failure):
                with self.assertRaisesRegex(ImageDownloadError, '生成成功.*403'):
                    _save_result(b'{"data":[{"url":"https://cdn.example/image.png"}]}', 'application/json', root / 'image.png', root)
            self.assertTrue((root / 'response.json').exists())
