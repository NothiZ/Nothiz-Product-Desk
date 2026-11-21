import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import desktop_worker as worker


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.task=self.root/"job"/"task.json"
        self.task.parent.mkdir()
        self.payload={"kind":"login","site":"alibaba","data_dir":str(self.root/"data"),"settings":{}}
        self.task.write_text(json.dumps(self.payload),encoding="utf-8")

    def result(self):
        return json.loads((self.task.parent/"result.json").read_text(encoding="utf-8"))

    def test_stopped_login_does_not_open_browser(self):
        (self.task.parent/"stop").touch()
        with patch("login.login") as login:
            self.assertEqual(worker.run_worker(self.task),0)
        login.assert_not_called()
        self.assertEqual(self.result()["status"],"stopped")

    def test_worker_failure_writes_result_and_diagnostic_log(self):
        self.task.write_text("{broken",encoding="utf-8")
        self.assertNotEqual(worker.run_worker(self.task),0)
        self.assertEqual(self.result()["status"],"error")
        self.assertIn("Traceback",(self.task.parent/"worker.log").read_text(encoding="utf-8"))

    def test_gui_login_uses_confirmation_bridge(self):
        response=self.task.parent/"response-test-prompt.json"
        response.write_text(json.dumps({"answer":""}),encoding="utf-8")
        with patch("login.login",side_effect=lambda site,confirm: confirm("请登录后继续")), \
             patch.object(worker.uuid,"uuid4",return_value=type("Id",(),{"hex":"test-prompt"})()):
            self.assertEqual(worker.run_worker(self.task),0)
        self.assertEqual(self.result()["status"],"finished")
        log=(self.task.parent/"events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"type": "prompt"',log)

    def test_search_worker_writes_the_wall_path_to_its_result(self):
        self.payload.update(kind="search", settings={"search": "婴儿奶瓶消毒器"})
        self.task.write_text(json.dumps(self.payload), encoding="utf-8")
        wall = self.task.parent / "候选图片墙.html"

        def run_search(settings, folder, emit, prompt, stop_requested):
            self.assertEqual(settings, self.payload["settings"])
            self.assertEqual(folder, self.task.parent.resolve())
            self.assertFalse(stop_requested())
            (folder / "stop").touch()
            self.assertTrue(stop_requested())
            emit({"type": "progress", "total": 1, "done": 1, "failed": 0})
            return {"status": "stopped", "wall_file": str(wall)}

        with patch.dict("sys.modules", {"candidate_wall": SimpleNamespace(run_candidate_search=run_search)}):
            self.assertEqual(worker.run_worker(self.task), 0)
        self.assertEqual(self.result()["wall_file"], str(wall))

    def test_browser_selftest_handles_local_fixtures_without_relaxing_product_validation(self):
        import scraper
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        expected = {"颜色": ["其它", "粉色"]}

        async def fetch(url, **kwargs):
            await kwargs["page_action"](MagicMock())

        session.fetch = AsyncMock(side_effect=fetch)
        with patch.object(scraper, "AsyncStealthySession", return_value=session), \
             patch.object(scraper.Scraper, "_scrape", new=AsyncMock(return_value={"variant_dimensions": expected})):
            report = asyncio.run(worker._check_browser("<html></html>"))
        self.assertEqual(report, {"concurrent_pages": 2, "variants": expected})
        self.assertEqual(session.fetch.await_count, 2)
        with self.assertRaises(ValueError):
            scraper.validate_product_url("http://127.0.0.1/item.htm?id=1")


if __name__ == "__main__":
    unittest.main()
