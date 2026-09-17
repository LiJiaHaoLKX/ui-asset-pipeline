import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from pipeline_tasks import CodexTaskStore


class MCPProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_handshake_and_read_only_call(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            page = project / "pages" / "page-001"
            for directory in (project / "workspace", page / "reference", page / "assets" / "approved", page / "prompts", page / "src"):
                directory.mkdir(parents=True, exist_ok=True)
            (project / "workspace" / "project.json").write_text(
                json.dumps({"activePageId": "page-001", "pages": [{"id": "page-001", "name": "测试", "storage": "page"}]}),
                encoding="utf-8",
            )
            (project / "workspace" / "design-spec.json").write_text('{"canvas":{"width":750,"height":1334}}', encoding="utf-8")
            (project / "workspace" / "settings.json").write_text('{"finalSize":"750x1334"}', encoding="utf-8")
            (page / "prompts" / "reference-page.notes.txt").write_text("页面需求", encoding="utf-8")
            (page / "prompts" / "reference-page.txt").write_text("AI 提示词", encoding="utf-8")
            Image.new("RGB", (10, 10), "white").save(page / "reference" / "reference-page.png")
            Image.new("RGBA", (10, 10), "red").save(page / "assets" / "approved" / "hero.png")
            (page / "assets" / "asset-manifest.json").write_text(
                json.dumps({"assets": [{"id": "asset-001", "file": "hero.png", "approved": True}]}), encoding="utf-8"
            )
            store = CodexTaskStore(project)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "mcp_server.py")],
                env={**os.environ, "UI_PIPELINE_ROOT": str(project)},
            )
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertEqual(
                        names,
                        {
                            "list_tasks", "wait_for_next_task", "claim_task", "get_task_context", "get_reference_image",
                            "list_asset_images", "get_asset_image", "record_asset_observation",
                            "get_visual_inspection_status", "get_source_files", "write_source_files",
                            "create_comparison", "get_comparison", "submit_for_review",
                            "report_task_error", "release_task",
                        },
                    )
                    started = time.monotonic()
                    waiting_call = asyncio.create_task(
                        session.call_tool("wait_for_next_task", {"timeout_seconds": 5})
                    )
                    await asyncio.sleep(0.2)
                    task = await asyncio.to_thread(store.create_task, "page-001", "implementation")
                    waiting = await asyncio.wait_for(waiting_call, timeout=2)
                    self.assertFalse(waiting.is_error)
                    self.assertIn(task["id"], waiting.content[0].text)
                    self.assertIn("local-event", waiting.content[0].text)
                    self.assertLess(time.monotonic() - started, 2)
                    await session.call_tool("claim_task", {"task_id": task["id"]})
                    context = await session.call_tool("get_task_context", {"task_id": task["id"]})
                    self.assertFalse(context.is_error)
                    reference = await session.call_tool("get_reference_image", {"task_id": task["id"]})
                    self.assertEqual(reference.content[0].type, "image")
                    assets = await session.call_tool("list_asset_images", {"task_id": task["id"]})
                    self.assertFalse(assets.is_error)
                    asset = await session.call_tool("get_asset_image", {"task_id": task["id"], "asset_id": "asset-001"})
                    self.assertEqual(asset.content[0].type, "image")
                    observation = await session.call_tool(
                        "record_asset_observation",
                        {
                            "task_id": task["id"], "asset_id": "asset-001",
                            "classification": "complete-composite", "contains_baked_text": True,
                            "notes": "文字已在图片内",
                        },
                    )
                    self.assertFalse(observation.is_error)
                    status = await session.call_tool("get_visual_inspection_status", {"task_id": task["id"]})
                    self.assertFalse(status.is_error)


if __name__ == "__main__":
    unittest.main()
