import csv
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook
from PIL import Image

import candidate_wall


class CandidateWallTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.records = [
            {"id": item_id, "title": title,
             "url": f"https://www.alibaba.com/product-detail/Bottle_{item_id}.html",
             "thumb": "https://s.alicdn.com/kf/Bottle.png_300x300.png"}
            for item_id, title in (("1601683518137", "</script><img src=x>"), ("1600000000001", "=2+2"))
        ]

    def test_wall_prefers_large_image_and_escapes_embedded_titles(self):
        template = self.folder / "assets" / "candidate_wall.html"
        template.parent.mkdir()
        template.write_text('<script type="application/json">__CANDIDATE_DATA__</script>', encoding="utf-8")
        image = BytesIO()
        Image.new("RGB", (800, 720), "white").save(image, format="PNG")
        response = Mock(status_code=200)
        response.iter_content.return_value = [image.getvalue()]
        with patch.object(candidate_wall, "resource_dir", return_value=self.folder), \
                patch("candidate_wall.requests.Session") as session:
            active = session.return_value.__enter__.return_value
            active.get.return_value.__enter__.return_value = response
            wall, items = candidate_wall.write_candidate_wall(self.records[:1], self.folder, prefix="NP")
        self.assertEqual(active.get.call_args.args[0], "https://s.alicdn.com/kf/Bottle.png")
        html = wall.read_text(encoding="utf-8")
        self.assertIn(r"\u003c/script>", html)
        self.assertNotIn("</script><img", html)
        self.assertEqual((items[0]["width"], items[0]["height"], items[0]["quality"], items[0]["rejected"]),
                         (800, 720, "偏糊", False))
        self.assertTrue((self.folder / items[0]["image"]).is_file())

    def test_import_reuses_existing_numbering_and_ignores_edited_titles(self):
        manifest = self.folder / "candidate_manifest.json"
        manifest.write_text(json.dumps({"category": "奶瓶消毒器", "prefix": "NP", "image_mode": "常规产品图",
                                        "items": self.records}), encoding="utf-8")
        workbook = self.folder / "选品结果.xlsx"
        csv_path = self.folder / "选中结果.csv"

        def export(records):
            with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["商品 ID", "链接", "标题"])
                writer.writerows((item["id"], item["url"], "Edited") for item in records)

        export(self.records[:1])
        self.assertEqual(candidate_wall.import_selection(csv_path, manifest, workbook), ["NP01"])
        export(self.records + self.records[:1])
        self.assertEqual(candidate_wall.import_selection(csv_path, manifest, workbook), ["NP02"])
        with workbook.open("rb") as file:
            result = load_workbook(file)
            sheet = result.active
            self.assertEqual(sheet.max_row, 3)
            self.assertEqual(sheet["D3"].value, "=2+2")
            self.assertEqual(sheet["D3"].data_type, "s")
            result.close()

    def test_import_rejects_a_url_that_was_not_in_the_wall(self):
        manifest = self.folder / "candidate_manifest.json"
        manifest.write_text(json.dumps({"category": "", "prefix": "SP", "image_mode": "常规产品图",
                                        "items": self.records}), encoding="utf-8")
        csv_path = self.folder / "选中结果.csv"
        csv_path.write_text("商品 ID,链接,标题\n1601683518137,https://example.com/,Not a candidate\n", encoding="utf-8-sig")
        workbook = self.folder / "选品结果.xlsx"
        with self.assertRaisesRegex(ValueError, "当前候选"):
            candidate_wall.import_selection(csv_path, manifest, workbook)
        self.assertFalse(workbook.exists())

    def test_search_filters_titles_and_stops_at_the_run_limit(self):
        first, second = self.records
        hits = [{**first, "title": "Baby bottle bag"}, {**second, "title": "Baby bottle sterilizer"}]

        def scrape(_url, pages, record, **options):
            self.assertEqual(pages, 2)
            self.assertFalse(record(1, hits))

        settings = {"search": "bottle,sterilizer", "pages": 2, "limit": 1,
                    "required": "BOTTLE，奶瓶", "excluded": "bag"}
        wall_path = self.folder / "candidates.html"
        with patch.object(candidate_wall, "collect_search", side_effect=scrape) as collect, \
                patch.object(candidate_wall, "write_candidate_wall", return_value=(wall_path, [{"image": "valid.png"}])) as wall:
            result = candidate_wall.run_candidate_search(settings, self.folder, Mock(), Mock(), lambda: False)
        collect.assert_called_once()
        self.assertEqual(wall.call_args.args[0], [hits[1]])
        self.assertEqual((result["status"], result["done"], result["failed"]), ("finished", 1, 0))
