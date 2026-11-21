import unittest
from unittest.mock import Mock, patch

import probe_search


class SearchProbeTests(unittest.TestCase):
    def test_next_page_click_waits_for_target_page_and_previous_product_ids(self):
        page = Mock()

        probe_search.advance_page(page, 2, ["1601683518137"])

        page.locator.return_value.click.assert_called_once()
        wait_args = page.wait_for_function.call_args.kwargs["arg"]
        self.assertEqual(wait_args["number"], 2)
        self.assertEqual(wait_args["previous"], ["1601683518137"])
        self.assertEqual([call[0] for call in page.mock_calls][:3],
                         ["locator", "locator().click", "wait_for_function"])

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

    def test_collect_search_passes_each_page_to_the_caller(self):
        first = '<a href="/product-detail/Bottle_1601683518137.html" title="=2+2"><img src="https://s.alicdn.com/bottle.jpg"></a>'
        second = '<a href="/product-detail/Bottle_1600000000001.html" title="奶瓶消毒器"></a>'
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.return_value = "Alibaba Search"
        page.content.side_effect = [first, first + second]

        def fetch(_url, *, page_action):
            page_action(page)
            return Mock(status=200)

        record = Mock()
        with patch("desktop_runtime.configure_browser_path"), \
                patch("desktop_runtime.login_state_path", return_value=Mock(is_file=lambda: False)), \
                patch("scrapling.fetchers.StealthySession") as session:
            session.return_value.__enter__.return_value.fetch.side_effect = fetch
            probe_search.collect_search(page.url, 2, record)

        self.assertEqual([(call.args[0], [item["id"] for item in call.args[1]]) for call in record.call_args_list],
                         [(1, ["1601683518137"]), (2, ["1601683518137", "1600000000001"])])


if __name__ == "__main__":
    unittest.main()
