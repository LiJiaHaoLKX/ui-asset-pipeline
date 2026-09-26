import unittest

from app import parse_model_json


class ModelJsonTest(unittest.TestCase):
    def test_parses_json_with_explanation_and_trailing_text(self):
        self.assertEqual(parse_model_json('Here is the result:\n{"regions": []}\nDone.'), {"regions": []})

    def test_parses_json_code_fence(self):
        self.assertEqual(parse_model_json('```json\n{"regions": []}\n```'), {"regions": []})


if __name__ == "__main__":
    unittest.main()
