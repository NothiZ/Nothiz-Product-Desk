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
        self.assertEqual((items[0]["width"], items[0]["height"], items[0]["quality"]), (800, 720, "偏糊"))
        self.assertTrue((self.folder / items[0]["image"]).is_file())

    def test_import_numbers_and_deduplicates_within_each_task(self):
        def job(name):
            folder = self.folder / "jobs" / name
            folder.mkdir(parents=True)
            manifest = folder / "candidate_manifest.json"
            manifest.write_text(json.dumps({"category": "奶瓶消毒器", "prefix": "NP",
                                            "items": self.records}), encoding="utf-8")
            return manifest, folder / "选品结果.xlsx"

        first_manifest, first_result = job("first")
        second_manifest, second_result = job("second")
        obsolete_master = self.folder / "选品结果.xlsx"
        obsolete_master.write_bytes(b"unrelated workbook")
        csv_path = self.folder / "选中结果.csv"

        def export(records):
            with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["商品 ID", "链接", "标题"])
                writer.writerows((item["id"], item["url"], "Edited") for item in records)

        export(self.records[:1])
        self.assertEqual(candidate_wall.import_selection(csv_path, first_manifest, first_result), ["NP01"])
        result = load_workbook(first_result)
        try:
            result.active["F1"] = "采集状态"
            result.active["F2"] = "已完成"
            result.save(first_result)
        finally:
            result.close()

        export(self.records + self.records[:1])
        first_before = first_result.read_bytes()
        with patch.object(Path, "replace", side_effect=PermissionError("Workbook in use")):
            with self.assertRaises(PermissionError):
                candidate_wall.import_selection(csv_path, first_manifest, first_result)
        self.assertEqual(first_result.read_bytes(), first_before)
        self.assertEqual(candidate_wall.import_selection(csv_path, first_manifest, first_result), ["NP02"])
        self.assertEqual(candidate_wall.import_selection(csv_path, first_manifest, first_result), [])
        result = load_workbook(first_result)
        try:
            self.assertEqual(result.active.max_row, 3)
            self.assertEqual(result.active["F2"].value, "已完成")
            self.assertEqual(result.active["D3"].value, "=2+2")
            self.assertEqual(result.active["D3"].data_type, "s")
        finally:
            result.close()
        export(self.records[:1])
        self.assertEqual(candidate_wall.import_selection(csv_path, second_manifest, second_result), ["NP01"])
        self.assertTrue(second_result.is_file())
        self.assertEqual(obsolete_master.read_bytes(), b"unrelated workbook")

    def test_import_rejects_a_url_that_was_not_in_the_wall(self):
        manifest = self.folder / "candidate_manifest.json"
        manifest.write_text(json.dumps({"category": "", "prefix": "SP",
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
