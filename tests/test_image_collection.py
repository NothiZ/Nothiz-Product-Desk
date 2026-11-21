import json
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook, load_workbook
from PIL import Image, ImageDraw

from image_collection import _has_saved_images, collect_images, extract_images, page_status
from desktop_runtime import validate_image_settings


LINK = "https://www.alibaba.com/product-detail/Bottle_1601928018059.html"


def gallery(*urls):
    data = {"globalData": {"product": {"mediaItems": [
        {"bizType": "Product", "group": "photos", "type": "image", "imageUrl": {"big": url}} for url in urls
    ]}}}
    return f"<script>window.detailData = {json.dumps(data)};</script>"


class ImageCollectionTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_the_history_workbook_as_an_image_task(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "选品结果.xlsx"
            master.touch()
            with patch.dict(os.environ, {"PRODUCT_DETAILS_DATA_DIR": str(root)}):
                with self.assertRaisesRegex(ValueError, "总表"):
                    validate_image_settings({"input_file": str(master), "output_dir": str(root / "照片")})

    def test_extracts_product_gallery_and_filters_external_and_duplicate_images(self):
        first = "https://sc04.alicdn.com/kf/first.jpg"
        second = "https://sc04.alicdn.com/kf/second.jpg"
        third = "https://sc04.alicdn.com/kf/description.jpg"
        html = (gallery(first, first + "_120x120.jpg", second, "https://example.com/foreign.jpg")
                + f'<section data-module-name="module_structure_description"><img src="data:," data-src="{third}"></section>'
                + '<section data-module-name="module_recommend"><img src="https://sc04.alicdn.com/kf/other.jpg"></section>')
        self.assertEqual(extract_images(html, LINK), [first, second])
        self.assertEqual(extract_images(html, LINK, include_description=True), [first, second, third])

    def test_classifies_page_blocks_without_treating_login_links_as_a_block(self):
        html = gallery("//sc04.alicdn.com/kf/first.jpg") + "<body><a>Sign in</a></body>"
        self.assertEqual(page_status(LINK, "Bottle on Alibaba.com", html), "")
        self.assertEqual(page_status(LINK, "Security verification", "<body>Slide to verify</body>"), "需验证")
        self.assertEqual(page_status("https://login.alibaba.com/", "Alibaba.com", ""), "需登录")
        self.assertEqual(page_status(LINK, "Alibaba.com", "<body><h1>该商品已下架</h1></body>"), "已下架")

    def test_resume_requires_all_numbered_photos(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "01.jpg").touch()
            self.assertTrue(_has_saved_images(folder, "1 / 1"))
            self.assertFalse(_has_saved_images(folder, "2 / 2"))

    async def test_collects_only_task_rows_saves_photos_and_resumes_terminal_rows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_file = root / "选品结果.xlsx"
            output_dir = root / "图片"
            book = Workbook()
            book.active.append(["商品编号", "分类名", "链接"])
            book.active.append(["SP01", "奶瓶消毒器", LINK])
            book.active.append(["SP02", "奶瓶消毒器", LINK.replace("8059", "8058")])
            book.save(input_file)
            book.close()
            image = Image.new("RGB", (800, 800), "black")
            draw = ImageDraw.Draw(image)
            for x in range(0, 800, 8):
                draw.rectangle((x, 0, x + 3, 799), fill="white")
            data = BytesIO()
            image.save(data, format="PNG")
            picture_url = "https://sc04.alicdn.com/kf/photo.jpg"
            pages = [("", gallery(picture_url)), ("需登录", "")]
            settings = {"input_file": str(input_file), "output_dir": str(output_dir), "min_clear": 1, "show_browser": False}
            events = []
            with patch("image_collection.Scraper.fetch_product", new=AsyncMock(side_effect=pages)) as fetch, \
                    patch("image_collection.image_responses", return_value=iter([(data.getvalue(), picture_url)])):
                result = await collect_images(settings, events.append)
                self.assertEqual(fetch.await_count, 2)
            self.assertEqual((result["done"], result["failed"]), (1, 1))
            with Image.open(output_dir / "奶瓶消毒器" / "SP01" / "01.jpg") as saved:
                self.assertEqual(saved.format, "JPEG")
            book = load_workbook(input_file)
            try:
                self.assertEqual([cell.value for cell in book.active[1]][-3:], ["采集状态", "清晰图数", "说明"])
                self.assertEqual([book.active["D2"].value, book.active["E2"].value, book.active["E3"].value],
                                 ["常规产品图", "已完成", "需登录"])
            finally:
                book.close()
            with patch("image_collection.Scraper.fetch_product", new=AsyncMock(return_value=("需登录", ""))) as fetch:
                await collect_images(settings, events.append)
                self.assertEqual(fetch.await_args.args[0], LINK.replace("8059", "8058"))
                self.assertEqual(fetch.await_count, 1)
            photo = output_dir / "奶瓶消毒器" / "SP01" / "01.jpg"
            photo.unlink()
            with patch("image_collection.Scraper.fetch_product", new=AsyncMock(return_value=("需登录", ""))), \
                    patch("image_collection.image_responses", return_value=iter([(data.getvalue(), picture_url)])):
                await collect_images(settings)
            self.assertTrue(photo.is_file())

    async def test_unsuccessful_photo_folders_are_marked_and_recovered_on_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "选品结果.xlsx"
            output = root / "图片"
            book = Workbook()
            book.active.append(["商品编号", "分类名", "链接"])
            book.active.append(["JS04", "Jersey", LINK])
            book.save(path)
            book.close()
            normal = output / "Jersey" / "JS04"
            skipped = normal.with_name("JS04-跳过")
            settings = {"input_file": str(path), "output_dir": str(output), "show_browser": False}
            status = "已淘汰"

            async def download(_scraper, _session, row, *_args):
                row["folder"].mkdir(parents=True, exist_ok=True)
                (row["folder"] / "01.jpg").write_bytes(b"saved image")
                return status, "1 / 1", ""

            with patch("image_collection._collect_product", new=AsyncMock(side_effect=download)) as collect:
                await collect_images(settings)
                self.assertTrue((skipped / "01.jpg").is_file())
                self.assertFalse(normal.exists())

                skipped.rename(normal)
                await collect_images(settings)
                self.assertTrue((skipped / "01.jpg").is_file())
                self.assertFalse(normal.exists())
                self.assertEqual(collect.await_count, 1)

                status = "失败"
                await collect_images({**settings, "resume": False})
                self.assertTrue(skipped.is_dir())

                status = "已完成"
                await collect_images({**settings, "min_clear": 1})
                self.assertTrue((normal / "01.jpg").is_file())
                self.assertFalse(skipped.exists())

                skipped.mkdir()
                with self.assertRaisesRegex(ValueError, "同时存在"):
                    await collect_images(settings)
                self.assertTrue((normal / "01.jpg").is_file())
                self.assertTrue(skipped.is_dir())

    def test_details_input_keeps_only_completed_image_rows(self):
        import run

        with TemporaryDirectory() as directory:
            path = Path(directory) / "选品结果.xlsx"
            book = Workbook()
            book.active.append(["商品编号", "链接", "采集状态"])
            book.active.append(["SP01", LINK, "已完成"])
            book.active.append(["SP02", LINK, "已淘汰"])
            book.active.append(["SP03", LINK, None])
            book.save(path)
            book.close()
            with patch.object(run, "INPUT_FILE", str(path)), patch.object(run, "RESUME", False):
                selected, _ = run._load()
        self.assertEqual(selected["商品编号"].tolist(), ["SP01"])
