import unittest
import json
from pathlib import Path

from bs4 import BeautifulSoup

from scraper import _extract_alibaba, _variant_cells


FIXTURE = Path(__file__).parent / "fixtures" / "alibaba_1601683518137.html"


class VariantExtractionTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")
        self.expected = {"颜色": ["其它", "粉色"]}

    def test_current_purchase_dom_includes_unselected_colors(self):
        soup = BeautifulSoup(self.html, "lxml")
        for script in soup.select("script"):
            script.decompose()
        self.assertEqual(_extract_alibaba(str(soup))["dims"], self.expected)

    def test_embedded_sku_ignores_recommendations_and_customization(self):
        soup = BeautifulSoup(self.html, "lxml")
        for row in soup.select("[data-testid=pc-purchase-sku-option-attribute]"):
            row.decompose()
        self.assertEqual(_extract_alibaba(str(soup))["dims"], self.expected)

    def test_complete_embedded_options_take_priority_over_dom(self):
        html = self.html.replace('aria-label="粉色"', 'aria-label="蓝色"').replace(">粉色<", ">蓝色<")
        self.assertEqual(_extract_alibaba(html)["dims"], self.expected)

    def test_legacy_image_options_are_deduplicated_and_skip_more_button(self):
        row = """
        <div data-module-name="module_sku"><div data-testid="sku-list">
          <div data-testid="sku-list-title">Color:</div>
          <div data-testid="sku-list-item">
            <div data-testid="color-sku-item"><img alt="Red"></div>
            <div data-testid="color-sku-item"><img alt="Green"></div>
            <div data-testid="color-sku-item">+8</div>
          </div>
        </div></div>"""
        self.assertEqual(_extract_alibaba(row + row)["dims"], {"Color": ["Red", "Green"]})

    def test_colors_and_sizes_keep_existing_excel_schema(self):
        self.assertEqual(_variant_cells({"Color": ["Red", "Green"], "Size": ["S", "M"], "Capacity": ["240ml"]}),
                         {"颜色": "Red, Green", "尺寸": "S, M", "其他规格": "Capacity: 240ml"})

    def test_customization_without_selectable_skus_is_not_a_variant(self):
        html = '<div data-module-name="module_sku_summary_other_customization">颜色：定制红、定制蓝</div>'
        self.assertEqual(_extract_alibaba(html)["dims"], {})

    def test_malformed_embedded_sku_falls_back_to_valid_dom(self):
        for sku in (None, [], {"skuAttrs": [None]}, {"skuAttrs": [{"name": "Color", "values": None}]}):
            with self.subTest(sku=sku):
                soup = BeautifulSoup(self.html, "lxml")
                soup.script.string = "window.detailData = " + json.dumps({"globalData": {"product": {"sku": sku}}}) + ";"
                self.assertEqual(_extract_alibaba(str(soup))["dims"], self.expected)


if __name__ == "__main__":
    unittest.main()
