import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import scraper


class ScraperSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmall_scrape_returns_variants_through_the_existing_pipeline(self):
        page = MagicMock()
        page.url = "https://detail.tmall.com/item.htm?id=1057477467114"
        page.title = AsyncMock(return_value="测试运动鞋-tmall.com天猫")
        page.content = AsyncMock(return_value=(Path(__file__).parent / "fixtures/tmall_sku.html").read_text(encoding="utf-8"))
        page.wait_for_selector = AsyncMock()
        sc = scraper.Scraper(headless=True)
        with patch.object(sc, "_pass_checkpoint", new=AsyncMock(return_value=True)), \
             patch.object(sc, "_frames_text", new=AsyncMock(return_value="商品详情")):
            result = await sc._scrape(page, page.url, "taobao")
        self.assertEqual(result["variants"]["颜色"], "40730303")
        self.assertEqual(len(result["variant_dimensions"]["鞋码"]), 19)
        self.assertIn("47.5", result["variants"]["尺寸"])

    def make_session(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.load_state = AsyncMock()
        session.page = SimpleNamespace()
        async def fetch(url, **kwargs):
            await kwargs["page_action"](session.page)
        session.fetch = AsyncMock(side_effect=fetch)
        return session

    async def test_reuses_async_scrapling_session_and_loads_state_once(self):
        session = self.make_session()
        factory = MagicMock(return_value=session)
        expected = {"title": "Carrier", "variants": {"颜色": "其它, 粉色"}}
        with patch.object(scraper, "AsyncStealthySession", factory, create=True), \
             patch.object(scraper, "async_playwright", side_effect=AssertionError("Legacy browser must not launch"), create=True), \
             patch.object(scraper, "_state_file", return_value="alibaba_state.json"), \
             patch.object(scraper.Scraper, "_scrape", new=AsyncMock(return_value=expected)):
            async with scraper.Scraper(headless=True) as sc:
                self.assertEqual(await sc.scrape("https://www.alibaba.com/product-detail/carrier_1.html"), expected)
                self.assertEqual(await sc.scrape("https://www.alibaba.com/product-detail/carrier_2.html"), expected)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["max_pages"], 2)
        session.load_state.assert_awaited_once_with("alibaba_state.json")
        self.assertEqual(session.fetch.await_count, 2)
        session.__aexit__.assert_awaited_once()

    async def test_callback_failure_is_not_silently_reported_as_success(self):
        session = self.make_session()
        async def swallow_callback_error(url, **kwargs):
            try:
                await kwargs["page_action"](session.page)
            except Exception:
                pass  # Scrapling logs callback errors instead of raising them.
        session.fetch.side_effect = swallow_callback_error
        with patch.object(scraper, "AsyncStealthySession", return_value=session, create=True), \
             patch.object(scraper, "async_playwright", side_effect=AssertionError("Legacy browser must not launch"), create=True), \
             patch.object(scraper, "_state_file", return_value=None), \
             patch.object(scraper.Scraper, "_scrape", new=AsyncMock(side_effect=ValueError("broken SKU"))):
            async with scraper.Scraper(headless=True) as sc:
                with self.assertRaisesRegex(ValueError, "broken SKU"):
                    await sc.scrape("https://www.alibaba.com/product-detail/carrier_1.html")

    async def test_successful_fetch_retry_does_not_keep_previous_callback_error(self):
        session = self.make_session()
        async def retried_fetch(url, **kwargs):
            await kwargs["page_action"](session.page)
            await kwargs["page_action"](session.page)
        session.fetch.side_effect = retried_fetch
        expected = {"variants": {"颜色": "粉色"}}
        with patch.object(scraper, "AsyncStealthySession", return_value=session), \
             patch.object(scraper, "_state_file", return_value=None), \
             patch.object(scraper.Scraper, "_scrape", new=AsyncMock(side_effect=[ValueError("temporary"), expected])):
            async with scraper.Scraper(headless=True) as sc:
                self.assertEqual(await sc.scrape("https://www.alibaba.com/product-detail/carrier_1.html"), expected)

    async def test_expansion_waits_for_new_options_instead_of_existing_rows(self):
        def html(values):
            items = "".join(f'<span data-testid="color-sku-item">{v}</span>' for v in values)
            return f'<div data-testid="sku-list"><div data-testid="sku-list-title">Color:</div><div data-testid="sku-list-item">{items}</div></div>'
        page = MagicMock()
        page.locator.return_value.first.click = AsyncMock()
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(side_effect=[html(["Red", "+1"]), html(["Red", "Green"])])
        with patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.Scraper(headless=True)._expand_skus(page, {"Color": ["Red"]})
        self.assertEqual(result, {"Color": ["Red", "Green"]})

    async def test_taobao_and_tmall_keep_separate_login_sessions(self):
        taobao, tmall = self.make_session(), self.make_session()
        with patch.object(scraper, "AsyncStealthySession", side_effect=[taobao, tmall]):
            async with scraper.Scraper(headless=True) as sc:
                self.assertIs(await sc._session("taobao", "taobao_state.json"), taobao)
                self.assertIs(await sc._session("taobao", "tmall_auth.json"), tmall)
                self.assertIs(await sc._session("taobao", "taobao_state.json"), taobao)
        taobao.load_state.assert_awaited_once_with("taobao_state.json")
        tmall.load_state.assert_awaited_once_with("tmall_auth.json")
        taobao.__aexit__.assert_awaited_once()
        tmall.__aexit__.assert_awaited_once()

    async def test_invalid_state_closes_the_new_browser_immediately(self):
        session = self.make_session()
        session.load_state.side_effect = ValueError("invalid state")
        with patch.object(scraper, "AsyncStealthySession", return_value=session):
            async with scraper.Scraper(headless=True) as sc:
                with self.assertRaisesRegex(ValueError, "invalid state"):
                    await sc._session("alibaba", "broken_state.json")
                session.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
