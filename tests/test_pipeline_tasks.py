import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pipeline_tasks import CodexTaskStore


VALID_HTML = '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="styles.css"></head><body>ok</body></html>'


class CodexTaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "workspace").mkdir()
        (self.root / "pages" / "page-001" / "reference").mkdir(parents=True)
        (self.root / "pages" / "page-001" / "assets" / "approved").mkdir(parents=True)
        (self.root / "pages" / "page-001" / "prompts").mkdir(parents=True)
        (self.root / "pages" / "page-001" / "src").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        self.write_json(
            self.root / "workspace" / "project.json",
            {"activePageId": "page-001", "pages": [{"id": "page-001", "name": "测试页面", "storage": "page"}]},
        )
        self.write_json(self.root / "workspace" / "design-spec.json", {"canvas": {"width": 750, "height": 1334}})
        self.write_json(self.root / "workspace" / "settings.json", {"finalSize": "750x1334", "threshold": 20})
        Image.new("RGB", (10, 10), "white").save(self.root / "pages" / "page-001" / "reference" / "reference-page.png")
        self.write_json(self.root / "pages" / "page-001" / "assets" / "asset-manifest.json", {"assets": []})
        (self.root / "pages" / "page-001" / "prompts" / "reference-page.notes.txt").write_text("页面需求", encoding="utf-8")
        (self.root / "pages" / "page-001" / "prompts" / "reference-page.txt").write_text("AI 图片提示词", encoding="utf-8")
        self.store = CodexTaskStore(self.root, "http://127.0.0.1:8766")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write_json(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_claim_write_revision_and_human_review(self):
        task = self.store.create_task("page-001", "implementation")
        claimed, token = self.store.claim_task(task["id"])
        self.assertEqual(claimed["status"], "running")
        context = self.store.get_context(task["id"], token)
        self.assertEqual(context["page"]["id"], "page-001")
        self.assertEqual(context["projectDesignSpec"]["canvas"]["width"], 750)
        self.assertEqual(context["aiGeneratedImagePrompt"], "AI 图片提示词")
        self.assertNotIn("apiKey", json.dumps(context))

        self.store.get_reference_image_path(task["id"], token)

        source = self.store.get_source_files(task["id"], token)
        written = self.store.write_source_files(
            task["id"], token, {"index.html": VALID_HTML}, source["revision"], "初次实现"
        )
        self.assertNotEqual(written["revision"], source["revision"])
        with self.assertRaises(RuntimeError):
            self.store.write_source_files(task["id"], token, {"styles.css": "body{}"}, source["revision"])

        submitted = self.store.submit_for_review(task["id"], token, "实现完成")
        self.assertEqual(submitted["status"], "awaiting-human-approval")
        completed = self.store.review_task(task["id"], True, "通过")
        self.assertEqual(completed["status"], "completed")

    def test_one_active_task_and_browser_write_lock(self):
        task = self.store.create_task("page-001", "implementation")
        with self.assertRaises(ValueError):
            self.store.create_task("page-001", "implementation")
        _, token = self.store.claim_task(task["id"])
        source = self.store.source_bundle("page-001")
        with self.assertRaises(ValueError):
            self.store.write_browser_source("page-001", {"index.html": "blocked"}, source["revision"])
        released = self.store.release_task(task["id"], token)
        self.assertEqual(released["status"], "queued")

    def test_delete_page_tasks_removes_tasks_events_and_inspections(self):
        task = self.store.create_task("page-001", "implementation")
        _, token = self.store.claim_task(task["id"])
        self.store.get_reference_image_path(task["id"], token)
        self.assertEqual(self.store.delete_page_tasks("page-001"), 1)
        self.assertEqual(self.store.list_tasks(page_id="page-001"), [])
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.store.get_task(task["id"])

    def test_calibration_requires_source_and_exposes_existing_iteration(self):
        with self.assertRaises(ValueError):
            self.store.create_task("page-001", "calibration")
        source = self.store.source_bundle("page-001")
        self.store.write_browser_source(
            "page-001",
            {"index.html": VALID_HTML, "styles.css": "", "app.js": ""},
            source["revision"],
        )
        implementation = self.store.create_task("page-001", "implementation")
        _, token = self.store.claim_task(implementation["id"])
        self.store.submit_for_review(implementation["id"], token, "done")
        self.store.review_task(implementation["id"], True)
        task = self.store.create_task("page-001", "calibration")
        self.assertEqual(task["maxIterations"], 8)

    def test_rejects_html_without_stylesheet_reference(self):
        task = self.store.create_task("page-001", "implementation")
        _, token = self.store.claim_task(task["id"])
        source = self.store.get_source_files(task["id"], token)
        self.store.get_reference_image_path(task["id"], token)
        with self.assertRaisesRegex(ValueError, "styles.css"):
            self.store.write_source_files(
                task["id"], token,
                {"index.html": '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>'},
                source["revision"],
            )

    def test_implementation_write_requires_actual_visual_inspection_and_classification(self):
        asset_path = self.root / "pages" / "page-001" / "assets" / "approved" / "hero.png"
        Image.new("RGBA", (12, 12), "red").save(asset_path)
        self.write_json(
            self.root / "pages" / "page-001" / "assets" / "asset-manifest.json",
            {"assets": [{"id": "asset-001", "file": "hero.png", "approved": True}]},
        )
        task = self.store.create_task("page-001", "implementation")
        _, token = self.store.claim_task(task["id"])
        source = self.store.get_source_files(task["id"], token)

        with self.assertRaisesRegex(ValueError, "强制视觉检查"):
            self.store.write_source_files(task["id"], token, {"index.html": VALID_HTML}, source["revision"])

        self.store.get_reference_image_path(task["id"], token)
        self.store.get_asset_image_path(task["id"], token, "asset-001")
        with self.assertRaisesRegex(ValueError, "missingAssetObservations"):
            self.store.write_source_files(task["id"], token, {"index.html": VALID_HTML}, source["revision"])

        observation = self.store.record_asset_observation(
            task["id"], token, "asset-001", "complete-composite", True, "文字已经烘焙在轮播图中"
        )
        self.assertTrue(observation["containsBakedText"])
        self.assertTrue(self.store.visual_inspection_status(task["id"], token)["readyToWrite"])
        written = self.store.write_source_files(task["id"], token, {"index.html": VALID_HTML}, source["revision"])
        self.assertTrue(written["ok"])


if __name__ == "__main__":
    unittest.main()
