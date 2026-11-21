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
    async def test_shared_output_rejects_a_different_input_even_with_the_same_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "first" / "products.xlsx", root / "second" / "products.xlsx"
            target = root / "products_详情.xlsx"
            for source in (first, second):
                source.parent.mkdir()
                pd.DataFrame({"链接": ["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source, index=False)
            with patch.multiple(run, INPUT_FILE=str(first), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK") as probe, \
                 patch.object(run, "generate_content", return_value={
                     "title_cn": "帽子", "title_en": "Hat", "desc_cn": "材质：棉布", "desc_en": "Material: Cotton",
                 }):
                self.assertEqual((await run.main())["done"], 1)
                original = target.read_bytes()
                probe.reset_mock()
                with patch.object(run, "INPUT_FILE", str(second)):
                    for resume in (False, True):
                        with self.subTest(resume=resume), patch.object(run, "RESUME", resume):
                            with self.assertRaisesRegex(RuntimeError, "当前输入文件"):
                                await run.main()
                probe.assert_not_called()
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual((await run.main())["done"], 1)

    async def test_existing_unmarked_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "input.xlsx", Path(folder) / "output.xlsx"
            rows = pd.DataFrame({"链接": ["https://www.alibaba.com/product-detail/test_1.html"]})
            for path in (source, target):
                rows.to_excel(path, index=False)
            original = target.read_bytes()
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK") as probe:
                with self.assertRaisesRegex(RuntimeError, "当前输入文件"):
                    await run.main()
            probe.assert_not_called()
            self.assertEqual(target.read_bytes(), original)

    async def test_concurrent_main_cannot_claim_the_same_output_before_the_first_save(self):
        with tempfile.TemporaryDirectory() as folder:
            source, other, target = (Path(folder) / name for name in ("input.xlsx", "other.xlsx", "output.xlsx"))
            for path in (source, other):
                pd.DataFrame({"链接": []}).to_excel(path, index=False)
            entered, release = asyncio.Event(), asyncio.Event()

            class PausedScraper(FakeScraper):
                async def __aenter__(self):
                    entered.set()
                    await release.wait()
                    return self

            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", PausedScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK") as probe:
                first = asyncio.create_task(run.main())
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    self.assertFalse(target.exists())
                    with patch.object(run, "INPUT_FILE", str(other)):
                        with self.assertRaisesRegex(OSError, "详情输出"):
                            await asyncio.wait_for(run.main(), timeout=1)
                finally:
                    release.set()
                    self.assertEqual((await first)["status"], "finished")
            probe.assert_called_once()

    async def test_locked_output_uses_a_numbered_backup_when_another_workbook_occupies_the_first(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source, target, occupied = (root / name for name in ("input.xlsx", "RESULT.XLSX", "RESULT_backup.XLSX"))
            pd.DataFrame({"链接": []}).to_excel(source, index=False)
            pd.DataFrame({"数据": ["另一任务"]}).to_excel(occupied, engine="openpyxl", index=False)
            original = occupied.read_bytes()
            replace = Path.replace

            def replace_unless_locked(path, destination):
                if destination == target:
                    raise PermissionError("Excel locked the primary output")
                return replace(path, destination)

            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(Path, "replace", autospec=True, side_effect=replace_unless_locked):
                result = await run.main()
            backup = Path(result["output_file"])
            self.assertEqual(backup, target.with_name("RESULT_backup_2.XLSX"))
            self.assertIn("链接", pd.read_excel(backup).columns)
            self.assertEqual(occupied.read_bytes(), original)

    def test_listing_price_is_exported_next_to_price_with_currency_and_rounding(self):
        rows = pd.DataFrame({"采集价格": ["US$10", "CN¥20 - CN¥30", "US$0.10", "USD1,200.40", "100", "面议", ""],
                             "上架价格": ["旧价格"] * 7, "AI中文标题": ["测试"] * 7})
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "output.xlsx"
            run._save(rows, target, "test-input", final=True)
            output = pd.read_excel(target, dtype=str, keep_default_na=False)
        self.assertEqual(list(output.columns), ["采集价格", "上架价格", "AI中文标题"])
        self.assertEqual(output["采集价格"].tolist(), rows["采集价格"].tolist())
        self.assertEqual(output["上架价格"].tolist(),
                         ["US$12.50", "CN¥25.00 - CN¥37.50", "US$0.13", "USD1500.50", "125.00", "", ""])

    async def test_resume_does_not_regenerate_complete_flagged_text(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://www.alibaba.com/product-detail/test_1.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            previous = pd.DataFrame({"链接":[url], "采集价格":["US$10"], "上架价格":["US$12.50"],
                                     "AI中文标题":["淘宝帽子"], "AI英文标题":["Taobao Hat"],
                                     "AI中文详情":["支持OEM定制"], "AI英文详情":["Supports wholesale orders."]})
            run._write_output(previous, target, run._source_id(source.resolve()))
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True), \
                 patch.object(run, "PRICE_MULTIPLIER", "1.50"), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(FakeScraper, "scrape", new=AsyncMock()) as fetch, \
                 patch.object(run, "generate_content") as generate:
                result = await run.main()
            self.assertEqual((result["total"], result["done"], result["failed"]), (0, 0, 0))
            fetch.assert_not_awaited()
            generate.assert_not_called()
            output = pd.read_excel(target).iloc[0]
            self.assertEqual(output["AI中文标题"], "淘宝帽子")
            self.assertEqual(output["上架价格"], "US$15.00")
            self.assertIn("OEM", output.get("需检查", ""))
            self.assertTrue(pd.isna(output["失败原因"]))

    async def test_failed_drafts_are_checkpointed_and_survive_an_empty_resume_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            urls = [f"https://www.alibaba.com/product-detail/test_{i}.html" for i in range(5)]
            pd.DataFrame({"链接":urls}).to_excel(source,index=False)
            response = "ChineseTitle: 普通帽子\nChineseDescription: 材质：棉布\nEnglishTitle: Hat\nEnglishDescription:"
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False, DESC_TEMPLATE="template1", SAVE_EVERY=5), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(ai_writer, "call_llm", return_value=response) as model, \
                 patch.object(run, "_save", wraps=run._save) as save:
                result = await run.main()
            self.assertEqual((result["done"], result["failed"]), (0, 5))
            self.assertEqual(model.call_count, 5)
            self.assertEqual([call.kwargs.get("final", False) for call in save.call_args_list], [False, True])
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True, DESC_TEMPLATE="template1"), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(ai_writer, "call_llm", return_value=""):
                result = await run.main()
            self.assertEqual((result["done"], result["failed"]), (0, 5))
            output = pd.read_excel(target)
            self.assertTrue(output["AI中文标题"].eq("普通帽子").all())
            self.assertTrue(output["失败原因"].notna().all())

    async def test_resume_retries_failed_draft_and_clears_failure_after_success(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://www.alibaba.com/product-detail/test_1.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            previous = pd.DataFrame({"链接":[url], "AI中文标题":["待补全帽子"], "AI英文标题":["Hat"],
                                     "AI中文详情":["材质：棉布"], "AI英文详情":[""],
                                     "失败原因":["上次缺少英文详情"]})
            run._write_output(previous, target, run._source_id(source.resolve()))
            response = "ChineseTitle: 棉布帽子\nChineseDescription: 材质：棉布\nEnglishTitle: Hat\nEnglishDescription: Material: Cotton"
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True, DESC_TEMPLATE="template1"), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(ai_writer, "call_llm", return_value=response):
                result = await run.main()
            self.assertEqual((result["total"], result["done"], result["failed"]), (1, 1, 0))
            output = pd.read_excel(target).iloc[0]
            self.assertEqual(output["AI英文标题"], "Hat")
            self.assertTrue(pd.isna(output["失败原因"]))

    async def test_flagged_content_is_exported_once_with_notes_and_counts_as_success(self):
        for template in ("template1", "template2"):
            with self.subTest(template=template), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
                pd.DataFrame({"链接":["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source,index=False)
                cn = "核心特点：支持OEM定制，欢迎批发，天猫旗舰店正品。"
                en = "Key Features: Supports logo customization and wholesale orders."
                if template == "template2":
                    cn = "详情\n支持OEM定制，欢迎批发，天猫旗舰店正品。\n规格\n材质：棉布"
                    en = "Details\nSupports logo customization and wholesale orders.\nSpecifications\nMaterial: Cotton"
                response = f"ChineseTitle: 淘宝 LOGO 牛仔帽\nChineseDescription:\n{cn}\nEnglishTitle: Taobao LOGO Denim Hat\nEnglishDescription:\n{en}"
                with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False, DESC_TEMPLATE=template), \
                     patch.object(run, "Scraper", FakeScraper), \
                     patch.object(run.llm_client, "call_llm", return_value="OK"), \
                     patch.object(ai_writer, "call_llm", return_value=response) as model:
                    result = await run.main()
                self.assertEqual((result["done"], result["failed"]), (1, 0))
                self.assertEqual(model.call_count, 1)
                output = pd.read_excel(target).iloc[0]
                self.assertEqual(output["AI中文标题"], "淘宝 LOGO 牛仔帽")
                self.assertIn("支持OEM定制，欢迎批发，天猫旗舰店正品。", output["AI中文详情"])
                self.assertIn("Supports logo customization and wholesale orders.", output["AI英文详情"])
                for term in ("OEM", "淘宝", "天猫", "wholesale", "LOGO"):
                    self.assertIn(term, output.get("需检查", ""))
                self.assertTrue(pd.isna(output["失败原因"]))

    async def test_missing_field_is_a_true_failure_with_partial_content_and_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            pd.DataFrame({"链接":["https://www.alibaba.com/product-detail/test_1.html"]}).to_excel(source,index=False)
            response = "ChineseTitle: 天猫最终帽子\nChineseDescription: 材质：牛仔布\nEnglishTitle: Final Hat\nEnglishDescription:"
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=False, DESC_TEMPLATE="template1"), \
                 patch.object(run, "Scraper", FakeScraper), \
                 patch.object(run.llm_client, "call_llm", return_value="OK"), \
                 patch.object(ai_writer, "call_llm", return_value=response) as model:
                result = await run.main()
            self.assertEqual((result["done"], result["failed"]), (0, 1))
            self.assertEqual(model.call_count, 1)
            output = pd.read_excel(target).iloc[0]
            self.assertEqual(output["AI中文标题"], "天猫最终帽子")
            self.assertEqual(output["AI英文标题"], "Final Hat")
            self.assertEqual(output["AI中文详情"], "材质：牛仔布")
            self.assertTrue(pd.isna(output["AI英文详情"]))
            self.assertEqual(output["颜色"], "Pink")
            self.assertIn("EnglishDescription", output.get("失败原因", ""))
            self.assertIn("天猫", output.get("需检查", ""))

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
            output = pd.read_excel(target)
            self.assertTrue(output[list(run.AI_COLUMNS)].isna().all().all())
            self.assertIn("域名", output.iloc[0].get("失败原因", ""))

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
            self.assertTrue(output[list(run.AI_COLUMNS)].isna().all().all())
            self.assertIn("缺少有效内容", output.iloc[0].get("失败原因", ""))

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
            self.assertEqual(Path(result["output_file"]), target.resolve())
            self.assertTrue(any(e["done"] == 1 and e["total"] == 1 for e in events))
            output=pd.read_excel(target)
            self.assertEqual(output.loc[0,"颜色"],"Pink")
            self.assertEqual(output.loc[0,"AI英文标题"],"Test Title")
            self.assertTrue(pd.isna(output.loc[0,"需检查"]))
            self.assertTrue(pd.isna(output.loc[0,"失败原因"]))

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
        with patch.object(login,"StealthySession",return_value=session), patch.object(login,"login_state_path",return_value=Path("alibaba_state.json"),create=True):
            self.assertIs(login.login("alibaba",confirm=confirm), True)
        confirm.assert_called_once()
        session.save_state.assert_called_once_with("alibaba_state.json")

    def test_cancelled_login_returns_false_without_saving_state(self):
        page = MagicMock()
        page.url = "https://login.alibaba.com/"
        session = MagicMock()
        session.__enter__.return_value = session
        session.fetch.side_effect = lambda url, **kw: kw["page_action"](page)
        with patch.object(login, "StealthySession", return_value=session), patch.object(login,"login_state_path",return_value=Path("alibaba_state.json"),create=True):
            self.assertIs(login.login("alibaba", confirm=lambda _: "n"), False)
        session.save_state.assert_not_called()

class ContentReviewResumeTests(unittest.TestCase):
    def test_unsupported_url_remains_a_true_failure_even_with_complete_text(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://detail.1688.com/offer/123.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            pd.DataFrame({"链接":[url], "AI中文标题":["帽子"], "AI英文标题":["Hat"],
                          "AI中文详情":["材质：棉布"], "AI英文详情":["Material: Cotton"],
                          "失败原因":["不支持的商品域名"]}).to_excel(target,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True):
                loaded, _ = run._load()
            self.assertIn("域名", loaded.loc[0,"失败原因"])

    def test_incomplete_saved_results_are_queued_again(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://www.alibaba.com/product-detail/test_1.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            pd.DataFrame({"链接":[url], "AI中文标题":["普通帽子"], "AI英文标题":["Hat"],
                          "AI中文详情":["材质：棉布"], "AI英文详情":[" "]}).to_excel(target,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True):
                loaded, _ = run._load()
            self.assertEqual(loaded.loc[0,"AI中文标题"], "普通帽子")
            self.assertTrue(loaded.loc[0].get("失败原因", ""))

    def test_complete_results_recompute_review_notes(self):
        for has_note in (False, True):
            with self.subTest(has_note=has_note), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
                urls = ["https://www.alibaba.com/product-detail/test_1.html", "https://www.alibaba.com/product-detail/test_2.html"]
                pd.DataFrame({"链接":urls}).to_excel(source,index=False)
                old = pd.DataFrame({"链接":urls, "AI中文标题":["淘宝 LOGO 帽子", "普通帽子"],
                    "AI英文标题":["Taobao Hat", "Original Hat"], "AI中文详情":["欢迎批发", "适用品牌兼容性好"],
                    "AI英文详情":["Supports wholesale orders.", "Custom fit for comfort."]})
                if has_note:
                    old["需检查"] = ["过期标记", "过期标记"]
                old.to_excel(target,index=False)
                with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True):
                    loaded, _ = run._load()
                self.assertIn("淘宝", loaded.loc[0,"需检查"])
                self.assertEqual(loaded.loc[1,"需检查"], "")
                self.assertEqual(loaded.loc[0,"AI中文标题"], "淘宝 LOGO 帽子")
                self.assertEqual(loaded.loc[0].get("失败原因", "缺少列"), "")
                self.assertEqual(loaded.loc[1,"AI中文标题"], "普通帽子")

    def test_manually_corrected_draft_is_kept_and_stale_failure_reason_is_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://www.alibaba.com/product-detail/test_1.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            pd.DataFrame({"链接":[url], "AI中文标题":["手工修好的帽子"], "AI英文标题":["Manually Edited Hat"],
                          "AI中文详情":["材质：牛仔布"], "AI英文详情":["Material: Denim"],
                          "失败原因":["上次包含淘宝"]}).to_excel(target,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True):
                loaded, _ = run._load()
            self.assertEqual(loaded.loc[0,"AI中文标题"], "手工修好的帽子")
            self.assertEqual(loaded.loc[0].get("失败原因", "缺少列"), "")

    def test_partial_failed_draft_without_chinese_title_survives_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/"input.xlsx", Path(folder)/"output.xlsx"
            url = "https://www.alibaba.com/product-detail/test_1.html"
            pd.DataFrame({"链接":[url]}).to_excel(source,index=False)
            pd.DataFrame({"链接":[url], "AI中文标题":[""], "AI英文标题":["Final Draft Hat"],
                          "AI中文详情":["材质：棉布"], "AI英文详情":["Material: Cotton"],
                          "失败原因":["缺少中文标题"]}).to_excel(target,index=False)
            with patch.multiple(run, INPUT_FILE=str(source), OUTPUT_FILE=str(target), RESUME=True):
                loaded, _ = run._load()
            self.assertEqual(loaded.loc[0,"AI英文标题"], "Final Draft Hat")
            self.assertTrue(loaded.loc[0,"失败原因"])


if __name__ == "__main__":
    unittest.main()
