import unittest
import json
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from scraper import _dump_sku, _extract_alibaba, _extract_taobao, _variant_cells


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


class TaobaoVariantTests(unittest.TestCase):
    def test_diagnostic_filename_does_not_use_path_characters_from_item_id(self):
        with patch("scraper.open", create=True) as opened, patch("scraper.os.makedirs"):
            _dump_sku("https://item.taobao.com/item.htm?id=../outside", "<html></html>")
        self.assertEqual(Path(opened.call_args.args[0]), Path("debug/sku_page.html"))

    def fixture(self, name):
        return (FIXTURE.parent / name).read_text(encoding="utf-8")

    def test_real_taobao_options_include_unselected_and_disabled_values(self):
        expected = {"颜色分类": ["浅绿色", "浅绿色弹力腰a字裙 s码", "芥末绿弹力腰 l码"], "尺码": ["均码"]}
        self.assertEqual(_extract_taobao(self.fixture("taobao_sku.html")).get("dims"), expected)

    def test_real_tmall_shoe_sizes_use_size_column_and_exclude_marketing_badges(self):
        dims = _extract_taobao(self.fixture("tmall_sku.html")).get("dims", {})
        self.assertEqual(dims.get("颜色分类"), ["40730303"])
        sizes = ["35.5", "36", "37", "37.5", "38", "38.5", "39", "40", "40.5", "41", "42", "42.5", "43", "44", "44.5", "45", "46", "47", "47.5"]
        self.assertEqual(dims.get("鞋码"), sizes)
        self.assertEqual(_variant_cells({"鞋码": sizes}), {"尺寸": ", ".join(sizes)})

    def test_current_dom_fallback_ignores_badges_and_body_measurements(self):
        soup = BeautifulSoup(self.fixture("taobao_sku.html"), "lxml")
        for script in soup.find_all("script"):
            script.decompose()
        dims = _extract_taobao(str(soup)).get("dims", {})
        self.assertEqual(dims.get("颜色分类"), ["浅绿色", "浅绿色弹力腰a字裙 s码", "芥末绿弹力腰 l码"])
        self.assertEqual(dims.get("尺码"), ["均码"])

    def test_embedded_skus_remain_complete_when_rendered_options_are_collapsed(self):
        soup = BeautifulSoup(self.fixture("tmall_sku.html"), "lxml")
        soup.select_one('[class*="skuWrapper--"]').decompose()
        dims = _extract_taobao(str(soup)).get("dims", {})
        self.assertEqual(len(dims.get("鞋码", [])), 19)

    def test_legacy_purchase_panel_keeps_other_dimensions_and_image_names(self):
        html = '''<div id="J_isku"><dl><dt>颜色分类</dt><dd><ul class="J_TSaleProp" data-property="颜色分类">
        <li data-value="1:1"><a title="黑色"><img alt="黑色"></a></li>
        <li data-value="1:2"><span>白色</span></li></ul></dd></dl>
        <dl><dt>容量</dt><dd><ul class="J_TSaleProp"><li data-value="2:1"><span>500ml</span></li></ul></dd></dl></div>
        <aside class="recommend"><ul class="J_TSaleProp" data-property="颜色分类"><li data-value="9:9">推荐红色</li></ul></aside>'''
        self.assertEqual(_extract_taobao(html).get("dims"), {"颜色分类": ["黑色", "白色"], "容量": ["500ml"]})

    def test_malformed_embedded_data_falls_back_to_current_purchase_dom(self):
        soup = BeautifulSoup(self.fixture("taobao_sku.html"), "lxml")
        soup.script.string = 'var a = window.__ICE_APP_CONTEXT__ || {}; var b = {broken;'
        self.assertEqual(_extract_taobao(str(soup)).get("dims", {}).get("尺码"), ["均码"])


if __name__ == "__main__":
    unittest.main()
