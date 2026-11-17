import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import ai_writer
import login
import run
import scraper


class FakeScraper:
    def __init__(self, **kwargs):
        self.stop_event = kwargs.get("stop_event")
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def scrape(self, url):
        return {"title":"Test", "price":"US$1", "variants":{"颜色":"Pink"}}


class DesktopPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_nonempty_links_fail_while_blank_rows_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["www.alibaba.com/product-detail/test_1.html", "", "ftp://item.taobao.com/item.htm?id=1"]}).to_excel(source,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(FakeScraper, "scrape", new=AsyncMock()) as fetch:
                result = await run.main()
            self.assertEqual((result["total"], result["done"], result["failed"]), (2, 0, 2))
            fetch.assert_not_awaited()

    async def test_unsupported_link_counts_as_failure_without_scraping_or_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["https://detail.1688.com/offer/123.html"]}).to_excel(source,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(FakeScraper, "scrape", new=AsyncMock(return_value={"title":"Test", "price":"US$1", "variants":{}})) as fetch, \
                 patch.object(run, "generate_content", return_value={"title_cn":"测试", "title_en":"Test", "desc_cn":"详情", "desc_en":"Details"}) as generate:
                result = await run.main()
            self.assertEqual((result["done"], result["failed"]), (0, 1))
            fetch.assert_not_awaited()
            generate.assert_not_called()
            self.assertTrue(pd.read_excel(target)[run.RESULT_COLS].isna().all().all())

    async def test_empty_ai_content_counts_as_failure_without_writing_product_results(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source,index=False)
            events = []
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(ai_writer, "call_llm", return_value=""):
                result = await run.main(progress=events.append)
            self.assertEqual((result["done"], result["failed"]), (0, 1))
            self.assertEqual((events[-1]["done"], events[-1]["failed"]), (0, 1))
            output = pd.read_excel(target)
            self.assertTrue(output[run.RESULT_COLS].isna().all().all())

    async def test_progress_and_return_value_use_existing_excel_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source,index=False)
            ai = {"title_cn":"测试标题","title_en":"Test Title","desc_cn":"测试详情","desc_en":"Test details"}
            events=[]
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(run, "generate_content", return_value=ai):
                result = await run.main(progress=events.append, stop_event=asyncio.Event())
            self.assertEqual((result["done"],result["failed"]), (1,0))
            self.assertEqual(result["output_file"], str(target))
            self.assertTrue(any(e["done"] == 1 and e["total"] == 1 for e in events))
            output=pd.read_excel(target)
            self.assertEqual(output.loc[0,"颜色"],"Pink")
            self.assertEqual(output.loc[0,"AI英文标题"],"Test Title")

    async def test_pre_requested_stop_does_not_start_scraping(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source,index=False)
            stop=asyncio.Event()
            stop.set()
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(FakeScraper, "scrape", new=AsyncMock()) as fetch:
                result=await run.main(progress=lambda e:None,stop_event=stop)
            fetch.assert_not_awaited()
            self.assertEqual(result["status"],"stopped")
            self.assertEqual((result["done"],result["failed"]),(0,0))
            self.assertTrue(target.exists())

    async def test_queued_scrape_exits_before_browser_start_when_stopped(self):
        stop=asyncio.Event()
        sc=scraper.Scraper(headless=True,concurrency={"alibaba":1},stop_event=stop)
        await sc._sems["alibaba"].acquire()
        with patch.object(sc,"_session",new=AsyncMock()) as session:
            pending=asyncio.create_task(sc.scrape("https://www.alibaba.com/product-detail/test_1.html"))
            await asyncio.sleep(0)
            stop.set()
            sc._sems["alibaba"].release()
            self.assertIsNone(await pending)
            session.assert_not_awaited()


class DesktopLoginTests(unittest.TestCase):
    def test_login_accepts_gui_confirmation_callback_and_saves_state(self):
        page=MagicMock()
        page.url="https://www.alibaba.com/"
        session=MagicMock()
        session.__enter__.return_value=session
        session.fetch.side_effect=lambda url,**kw: kw["page_action"](page)
        confirm=MagicMock(return_value="")
        with patch.object(login,"StealthySession",return_value=session):
            self.assertIs(login.login("alibaba",confirm=confirm), True)
        confirm.assert_called_once()
        session.save_state.assert_called_once_with("alibaba_state.json")

    def test_cancelled_login_returns_false_without_saving_state(self):
        page = MagicMock()
        page.url = "https://login.alibaba.com/"
        session = MagicMock()
        session.__enter__.return_value = session
        session.fetch.side_effect = lambda url, **kw: kw["page_action"](page)
        with patch.object(login, "StealthySession", return_value=session):
            self.assertIs(login.login("alibaba", confirm=lambda _: "n"), False)
        session.save_state.assert_not_called()

    def test_excel_lock_reports_actual_backup_path(self):
        df=MagicMock()
        df.to_excel.side_effect=[PermissionError("locked"),None]
        with patch.object(run,"OUTPUT_FILE","result.xlsx"):
            self.assertEqual(run._save(df,final=True),"result_backup.xlsx")

    def test_backup_handles_uppercase_extension_and_dotted_directory(self):
        df=MagicMock()
        df.to_excel.side_effect=[PermissionError("locked"),None]
        with patch.object(run,"OUTPUT_FILE","data.xlsx/RESULT.XLSX"):
            self.assertEqual(Path(run._save(df,final=True)),Path("data.xlsx/RESULT_backup.XLSX"))


if __name__ == "__main__":
    unittest.main()
