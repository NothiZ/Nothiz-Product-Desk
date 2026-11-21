import unittest
from unittest.mock import Mock, patch

from patchright.sync_api import TimeoutError as BrowserTimeout

import probe_search


FIRST_PRODUCT = '<a href="/product-detail/Bottle_1601683518137.html" title="=2+2"><img src="https://s.alicdn.com/bottle.jpg"></a>'


class SearchProbeTests(unittest.TestCase):
    def collect(self, page, pages, record, **kwargs):
        def fetch(_url, *, page_action):
            page_action(page)
            return Mock(status=200)

        with patch("desktop_runtime.configure_browser_path"), \
                patch("desktop_runtime.login_state_path", return_value=Mock(is_file=lambda: False)), \
                patch("scrapling.fetchers.StealthySession") as session:
            session.return_value.__enter__.return_value.fetch.side_effect = fetch
            probe_search.collect_search(page.url, pages, record, **kwargs)

    def test_next_page_click_waits_for_target_page_and_previous_product_ids(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1

        probe_search.advance_page(page, 2, ["1601683518137"])

        page.locator.return_value.click.assert_called_once()
        wait_args = page.wait_for_function.call_args.kwargs["arg"]
        self.assertEqual(wait_args["number"], 2)
        self.assertEqual(wait_args["previous"], ["1601683518137"])
        actions = [call[0] for call in page.mock_calls]
        self.assertLess(actions.index("locator().click"), actions.index("wait_for_function"))

    def test_extract_items_uses_product_heading_instead_of_certification_badge(self):
        html = """
        <a href="//www.alibaba.com/product-detail/Bottle_1601683518137.html?spm=a">
            <img data-src="//s.alicdn.com/bottle.jpg" alt="认证">
        </a>
        <h2><a href="//www.alibaba.com/product-detail/Bottle_1601683518137.html?spm=b">
            <span>认证</span><span>婴儿奶瓶消毒器</span>
        </a></h2>
        """
        self.assertEqual(probe_search.extract_items(html), [{
            "id": "1601683518137",
            "url": "https://www.alibaba.com/product-detail/Bottle_1601683518137.html",
            "title": "婴儿奶瓶消毒器",
            "thumb": "https://s.alicdn.com/bottle.jpg",
        }])

    def test_collect_search_passes_each_available_page_to_the_caller(self):
        second = '<a href="/product-detail/Bottle_1600000000001.html" title="奶瓶消毒器"></a>'
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.return_value = "Alibaba Search"
        page.content.side_effect = [FIRST_PRODUCT, FIRST_PRODUCT + second]
        next_page = page.locator.return_value
        next_page.count.side_effect = [1, 0]
        next_page.click.side_effect = [None, BrowserTimeout("Next is disabled")]

        record = Mock()
        self.collect(page, 3, record)

        self.assertEqual([(call.args[0], [item["id"] for item in call.args[1]]) for call in record.call_args_list],
                         [(1, ["1601683518137"]), (2, ["1601683518137", "1600000000001"])])
        next_page.click.assert_called_once()

    def test_collect_search_keeps_single_page_with_a_two_page_limit(self):
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.return_value = "Alibaba Search"
        page.content.return_value = FIRST_PRODUCT
        next_page = page.locator.return_value
        next_page.count.return_value = 0
        next_page.click.side_effect = BrowserTimeout("No Next control")
        record = Mock()

        self.collect(page, 2, record)

        record.assert_called_once_with(1, probe_search.extract_items(FIRST_PRODUCT))
        next_page.click.assert_not_called()

    def test_collect_search_preserves_timeout_waiting_for_clicked_page(self):
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.return_value = "Alibaba Search"
        page.content.return_value = FIRST_PRODUCT
        page.locator.return_value.count.return_value = 1
        page.wait_for_function.side_effect = [Mock(), BrowserTimeout("Results did not update")]

        with self.assertRaisesRegex(RuntimeError, "等待第 2 页的页码和商品列表更新超时"):
            self.collect(page, 2, Mock())

    def test_collect_search_detects_captcha_when_next_control_disappears(self):
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.side_effect = ["Alibaba Search", "Captcha"]
        page.content.return_value = FIRST_PRODUCT
        next_page = page.locator.return_value
        next_page.count.return_value = 0
        next_page.click.side_effect = BrowserTimeout("No Next control")

        with self.assertRaisesRegex(RuntimeError, "遇到登录或验证码"):
            self.collect(page, 2, Mock(), headless=True)


if __name__ == "__main__":
    unittest.main()
