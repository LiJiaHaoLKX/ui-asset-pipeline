import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from scripts.split_transparent_objects import split_objects


class IconGroupingTest(unittest.TestCase):
    def test_order_outline_and_disconnected_lines_stay_one_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new('RGBA', (160, 80))
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 65, 70), outline='white', width=3)
            draw.rectangle((25, 27, 48, 30), fill='white')
            draw.rectangle((25, 43, 48, 46), fill='white')
            draw.rectangle((110, 20, 140, 55), fill='red')
            source = root / 'input.png'
            image.save(source)
            results = split_objects(source, root / 'out', 8, 0, 100, 0, 0, expected_count=2)
            self.assertEqual(len(results), 2)
            with Image.open(root / 'out' / results[0]['file']) as icon:
                self.assertEqual(icon.getpixel((16, 18))[3], 255)
                self.assertEqual(icon.getpixel((16, 34))[3], 255)
                self.assertEqual(icon.getpixel((16, 27))[3], 0)

    def test_mismatch_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new('RGBA', (100, 50))
            draw = ImageDraw.Draw(image)
            draw.rectangle((5, 5, 20, 20), fill='white')
            draw.rectangle((60, 5, 75, 20), fill='white')
            source = root / 'input.png'
            image.save(source)
            out = root / 'out'
            out.mkdir()
            old = out / 'object-001.png'
            old.write_bytes(b'previous-result')
            with self.assertRaisesRegex(ValueError, '红框有 1 个'):
                split_objects(source, out, 8, 0, 1, 0, 0, expected_count=1)
            self.assertEqual(old.read_bytes(), b'previous-result')
