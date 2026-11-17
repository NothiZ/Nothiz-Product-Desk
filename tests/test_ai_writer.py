import unittest
from unittest.mock import patch

import ai_writer


SECTIONS = {
    "ChineseTitle": "USB LED 牛仔帽",
    "ChineseDescription": "核心特点：原创设计，适用品牌兼容性好。\n材质：PVC 和 TPU，深圳市设计，广东风格。",
    "EnglishTitle": "USB LED Off-shoulder Canvas Hat",
    "EnglishDescription": "Key Features: Original design. Customer favorite.\nMaterial: PVC, TPU and bone china.",
}
PRODUCT = {"page_title_raw": "USB LED Canvas Hat PVC TPU", "description": "商品信息"}


class AiWriterTests(unittest.TestCase):
    def generate(self, sections, template="template1", dedupe=None):
        response = "\n".join(f"{label}:\n{value}" for label, value in sections.items())
        with patch.object(ai_writer, "call_llm", return_value=response):
            return ai_writer.generate_content(PRODUCT, template, dedupe)

    def test_section_labels_accept_markdown_and_both_colons(self):
        for label in ("ChineseTitle:", "**ChineseTitle:**", "**ChineseTitle**：", "### ChineseTitle："):
            with self.subTest(label=label):
                text = f"```text\n{label} 牛仔帽\nEnglishTitle: Denim Hat\n```"
                self.assertEqual(
                    ai_writer._extract_sections(text, ("ChineseTitle", "EnglishTitle")),
                    {"ChineseTitle": "牛仔帽", "EnglishTitle": "Denim Hat"},
                )

    def test_sensitive_filter_keeps_normal_sentences_and_field_values(self):
        for text in (
            "Original design for everyday use.", "Customer favorite for summer.",
            "Custom fit for comfort.", "Made in mind for comfort.", "适用品牌兼容性好",
            "Material: Original cotton.", "Custom fit: Flexible", "Origin of this design: Nature",
        ):
            with self.subTest(text=text):
                self.assertFalse(ai_writer._is_sensitive_line(text))

    def test_sensitive_filter_recognizes_only_explicit_field_names(self):
        for text in ("产地：中国", "- 原产地：中国", "**品牌：** 示例", "Model Number: X1", "Place of Origin：China", "Customization: Supported"):
            with self.subTest(text=text):
                self.assertTrue(ai_writer._is_sensitive_line(text))

    def test_titles_and_specs_preserve_product_words(self):
        result = self.generate(SECTIONS)
        self.assertEqual(result["title_cn"], SECTIONS["ChineseTitle"])
        self.assertEqual(result["title_en"], SECTIONS["EnglishTitle"])
        self.assertEqual(result["desc_cn"], SECTIONS["ChineseDescription"])
        self.assertEqual(result["desc_en"], SECTIONS["EnglishDescription"])

    def test_first_english_word_is_not_treated_as_a_brand(self):
        for title in ("Canvas shoulder bag", "Women summer hat", "USB LED PVC TPU"):
            with self.subTest(title=title), patch.dict(PRODUCT, page_title_raw=title):
                result = self.generate({**SECTIONS, "EnglishTitle": title})
                self.assertEqual(result["title_en"], title)

    def test_duplicate_titles_do_not_reintroduce_raw_url_marketing_words(self):
        dedupe = ai_writer.TitleDedupe()
        with patch.dict(PRODUCT, url_keywords=["批发", "wholesale", "OEM"]):
            first = self.generate(SECTIONS, dedupe=dedupe)
            second = self.generate(SECTIONS, dedupe=dedupe)
        self.assertEqual(second["title_cn"], first["title_cn"] + "2")
        self.assertEqual(second["title_en"], first["title_en"] + " 2")

    def test_technical_attribute_names_and_negative_values_are_preserved(self):
        result = self.generate({**SECTIONS,
            "ChineseDescription": "USB-C接口：USB 3.2\n工作温度：-5°C",
            "EnglishDescription": "USB-C Port: USB 3.2\nBluetooth 5.3: Supported\nTemperature: -5°C",
        })
        self.assertEqual(result["desc_cn"], "USB-C接口：USB 3.2\n工作温度：-5°C")
        self.assertEqual(result["desc_en"], "USB-C Port: USB 3.2\nBluetooth 5.3: Supported\nTemperature: -5°C")

    def test_template2_keeps_normal_intro_sentences(self):
        for intro in ("Original design for everyday use.", "Customer favorite for summer.", "Custom fit for comfort.", "Made in mind for comfort."):
            with self.subTest(intro=intro):
                sections = {**SECTIONS,
                    "ChineseDescription": "详情\n适用品牌兼容性好，深圳市设计，广东风格。\n规格\n材质：PVC 和 TPU",
                    "EnglishDescription": f"Details\n{intro}\nSpecifications\nMaterial: Bone china",
                }
                result = self.generate(sections, "template2")
                self.assertIn(intro, result["desc_en"])
                self.assertIn("适用品牌兼容性好，深圳市设计，广东风格。", result["desc_cn"])
                self.assertIn("Material: Bone china", result["desc_en"])

    def test_explicit_sensitive_fields_are_removed_in_both_templates(self):
        for template in ("template1", "template2"):
            with self.subTest(template=template):
                cn = "产地：中国\n型号：X1\n材质：PVC 和 TPU"
                en = "Place of Origin: China\nModel Number: X1\nMaterial: Original cotton"
                if template == "template2":
                    cn = "详情\nOriginal 是设计名称。\n规格\n" + cn
                    en = "Details\nCustomer favorite.\nSpecifications\n" + en
                result = self.generate({**SECTIONS, "ChineseDescription": cn, "EnglishDescription": en}, template)
                self.assertNotIn("产地：", result["desc_cn"])
                self.assertNotIn("型号：", result["desc_cn"])
                self.assertNotIn("Place of Origin:", result["desc_en"])
                self.assertNotIn("Model Number:", result["desc_en"])
                self.assertIn("Material: Original cotton", result["desc_en"])

    def test_missing_fields_raise_instead_of_returning_blank_results(self):
        for label in SECTIONS:
            for template in ("template1", "template2"):
                with self.subTest(label=label, template=template):
                    with self.assertRaisesRegex(ValueError, "AI"):
                        self.generate({**SECTIONS, label: ""}, template)

    def test_descriptions_empty_after_formatting_are_rejected(self):
        for description, template in (("不是参数列表", "template1"), ("材质：", "template1"), ("产地：中国", "template1"), ("详情\n规格", "template2"), ("详情\n规格\n产地：中国", "template2")):
            with self.subTest(description=description, template=template):
                with self.assertRaisesRegex(ValueError, "AI"):
                    self.generate({**SECTIONS, "ChineseDescription": description}, template)

    def test_markdown_only_values_are_not_valid_content(self):
        for marker in ("---", "**", "___"):
            for label, value, template in (("ChineseTitle", marker, "template1"), ("ChineseDescription", f"详情\n{marker}\n规格", "template2")):
                with self.subTest(marker=marker, label=label):
                    with self.assertRaisesRegex(ValueError, "AI"):
                        self.generate({**SECTIONS, label: value}, template)

    def test_invalid_content_does_not_reserve_titles_for_deduplication(self):
        dedupe = ai_writer.TitleDedupe()
        with self.assertRaises(ValueError):
            self.generate({**SECTIONS, "EnglishDescription": ""}, dedupe=dedupe)
        result = self.generate(SECTIONS, dedupe=dedupe)
        self.assertEqual(result["title_cn"], SECTIONS["ChineseTitle"])
        self.assertEqual(result["title_en"], SECTIONS["EnglishTitle"])

    def test_valid_template2_is_formatted_without_dropping_content(self):
        result = self.generate({**SECTIONS,
            "ChineseDescription": "**详情**\n适合日常使用。\n**规格**\n材质：棉布",
            "EnglishDescription": "**Details**\nSuitable for daily use.\n**Specifications**\nMaterial: Cotton",
        }, "template2")
        self.assertEqual(result["desc_cn"], "详情\n适合日常使用。\n\n规格\n材质：棉布")
        self.assertEqual(result["desc_en"], "Details\nSuitable for daily use.\n\nSpecifications\nMaterial: Cotton")


if __name__ == "__main__":
    unittest.main()
