import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import scraper


SUPPORTED = (
    ("https://www.alibaba.com/product-detail/test_1.html", "alibaba", "alibaba_state.json"),
    ("https://WWW.ALIBABA.COM/product-detail/test_1.html", "alibaba", "alibaba_state.json"),
    ("https://alibaba.com/product-detail/test_1.html", "alibaba", "alibaba_state.json"),
    ("https://item.taobao.com/item.htm?id=1", "taobao", "taobao_state.json"),
    ("https://detail.tmall.com/item.htm?id=1", "taobao", "tmall_auth.json"),
)
UNSUPPORTED = (
    "https://detail.1688.com/offer/123.html", "https://example.com/product/123",
    "https://m.tb.cn/h.example", "https://detail.tmall.hk/item.htm?id=123",
    "https://example.com/alibaba.com/product-detail/test_1.html",
    "https://alibaba.com.example.com/product-detail/test_1.html",
    "https://fakealibaba.com/product-detail/test_1.html",
    "https://example.com/?url=https://item.taobao.com/item.htm?id=1",
    "https://taobao.com@example.com/item.htm?id=1", "https://example.com/#tmall.com",
    "ftp://www.alibaba.com/product-detail/test_1.html", "www.alibaba.com/product-detail/test_1.html",
    "https://[broken", "https://www.alibaba.com:invalid/product-detail/test_1.html",
)


class ProductUrlTests(unittest.TestCase):
    def test_supported_domains_preserve_platform_and_login_state_selection(self):
        for url, platform, state in SUPPORTED:
            with self.subTest(url=url), patch.object(scraper.os.path, "exists", return_value=True), \
                 patch.object(scraper, "login_state_path", side_effect=lambda filename: Path("C:/local-login") / filename, create=True):
                self.assertEqual(scraper._platform(url), platform)
                self.assertEqual(scraper._state_file(url), str(Path("C:/local-login") / state))

    def test_unsupported_and_malformed_urls_are_rejected(self):
        for url in UNSUPPORTED:
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "链接|域名"):
                    scraper._platform(url)
                with self.assertRaisesRegex(ValueError, "链接|域名"):
                    scraper._state_file(url)


class ProductUrlEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_url_is_rejected_before_browser_session_creation(self):
        async with scraper.Scraper(headless=True) as sc:
            with patch.object(sc, "_session", new=AsyncMock(side_effect=AssertionError("Unsupported link must not start a browser"))) as session:
                with self.assertRaises(ValueError):
                    await sc.scrape("https://detail.1688.com/offer/123.html")
                session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
