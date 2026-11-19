from contextlib import chdir, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

import probe_search


class SearchProbeTests(unittest.TestCase):
    def test_next_page_click_waits_for_target_page_and_previous_product_ids(self):
        advance_page = getattr(probe_search, "advance_page", None)
        self.assertIsNotNone(advance_page, "The probe needs to advance inside the current tab")
        page = Mock()

        advance_page(page, 2, ["1601683518137"])

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

    def test_cli_exports_unique_candidates_with_editable_image_modes(self):
        first = '<a href="/product-detail/Bottle_1601683518137.html" title="=2+2"><img src="https://s.alicdn.com/bottle.jpg"></a>'
        second = '<a href="/product-detail/Bottle_1600000000001.html" title="奶瓶消毒器"></a>'
        page = Mock(url="https://www.alibaba.com/search/page?SearchText=test")
        page.title.return_value = "Alibaba Search"
        page.content.side_effect = [first, first + second]

        def fetch(_url, *, page_action):
            page_action(page)
            return Mock(status=200)

        argv = ["probe_search.py", "婴儿奶瓶消毒器", "--image-mode", "平铺图"]
        with TemporaryDirectory() as directory, chdir(directory), redirect_stdout(StringIO()), \
                patch("sys.argv", argv), patch("desktop_runtime.configure_browser_path"), \
                patch("desktop_runtime.login_state_path", return_value=Path("unused_state.json")), \
                patch("scrapling.fetchers.StealthySession") as session:
            session.return_value.__enter__.return_value.fetch.side_effect = fetch
            try:
                probe_search.main()
            except SystemExit as error:
                self.fail(f"Candidate export exited with {error.code}")
            outputs = list(Path("debug").glob("*/候选商品.xlsx"))
            self.assertEqual(len(outputs), 1)
            workbook = load_workbook(outputs[0])
            self.addCleanup(workbook.close)
            sheet = workbook["候选商品"]
            self.assertEqual([cell.value for cell in sheet[1]],
                             ["选品结果", "图片模式", "商品 ID", "标题", "链接", "缩略图链接"])
            self.assertEqual(sheet.max_row, 3)
            self.assertEqual([sheet[f"C{row}"].value for row in (2, 3)], ["1601683518137", "1600000000001"])
            self.assertEqual(sheet["C2"].number_format, "@")
            self.assertEqual((sheet["A2"].value, sheet["B2"].value), ("待选", "平铺图"))
            self.assertEqual((sheet["D2"].value, sheet["D2"].data_type), ("=2+2", "s"))
            self.assertEqual(sheet["E2"].hyperlink.target, sheet["E2"].value)
            self.assertEqual(sheet["F2"].hyperlink.target, "https://s.alicdn.com/bottle.jpg")
            self.assertIsNone(sheet["F3"].value)
            self.assertEqual(sheet.auto_filter.ref, "A1:F3")
            self.assertEqual({str(rule.sqref): rule.formula1 for rule in sheet.data_validations.dataValidation},
                             {"A2:A3": '"待选,入选,跳过"', "B2:B3": '"常规产品图,平铺图"'})


if __name__ == "__main__":
    unittest.main()
