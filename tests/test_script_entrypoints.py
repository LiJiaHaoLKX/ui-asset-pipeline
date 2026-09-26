import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ScriptEntrypointsTest(unittest.TestCase):
    def test_image_scripts_start_without_project_on_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            for script in ('extract_regions.py', 'repair_background_targets.py', 'gpt_image_api.py'):
                with self.subTest(script=script):
                    result = subprocess.run(
                        [sys.executable, '-E', str(root / 'scripts' / script), '--help'],
                        cwd=directory, capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)
