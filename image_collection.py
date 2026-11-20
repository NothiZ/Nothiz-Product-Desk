"""Collect and check product photos for one selected-results workbook."""

import argparse
import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from patchright.async_api import TimeoutError as PageTimeout
from PIL import Image, ImageOps
import requests

from candidate_wall import _trusted_url, image_responses, inspect_image, larger_image_url
from desktop_runtime import IMAGE_MODES, configure_browser_path, save_workbook_atomic, validate_image_settings, workbook_write_lock
from scraper import Scraper, product_payloads, validate_product_url

STATUS_HEADERS = ("采集状态", "清晰图数", "说明")
COMPLETE = "已完成"
REJECTED = "已淘汰"
_FINAL = (COMPLETE, REJECTED)
_GALLERY = '[data-module-name="module_productImage"], [data-module-name="module_main_image"]'
_DESCRIPTION = '[data-module-name="module_description"], [data-module-name="module_structure_description"]'
_STATUS_NOTES = {
    "需登录": "需要重新登录阿里巴巴，登录后可重试。",
    "需验证": "请在显示浏览器时手动完成验证，再继续采集。",
    "已下架": "商品详情页已下架或不存在，请核对链接。",
    "超时": "打开详情页超时，请检查网络后重试。",
    "拒绝访问": "平台拒绝访问，请检查浏览器能否正常打开该商品。",
}


def _image_url(value, page_url):
    if not isinstance(value, str) or not value.strip():
        return ""
    url = urljoin(page_url, value.strip())
    if not _trusted_url(url, ("alicdn.com", "alibaba.com")):
        return ""
    return urlsplit(url)._replace(fragment="").geturl()


def _dom_images(soup, region, page_url):
    for tag in soup.select(f":is({region}) img"):
        identity = " ".join(tag.get("class", []) + [tag.get("alt", "")]).casefold()
        if any(word in identity for word in ("avatar", "widget-icon", "find similar", "placeholder")):
            continue
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            if url := _image_url(tag.get(attr), page_url):
                yield url
                break


def extract_images(html, page_url, *, include_description=False):
    soup = BeautifulSoup(html, "lxml")
    gallery = []
    for product in product_payloads(soup):
        for item in product.get("mediaItems") or ():
            if not isinstance(item, dict) or item.get("type") != "image" or item.get("group") != "photos":
                continue
            if item.get("bizType") not in (None, "Product"):
                continue
            sizes = item.get("imageUrl")
            if isinstance(sizes, dict):
                for name in ("big", "small", "normal", "thumb"):
                    if url := _image_url(sizes.get(name), page_url):
                        gallery.append(url)
                        break
        if gallery:
            break
    if not gallery:
        gallery.extend(_dom_images(soup, _GALLERY, page_url))
    if include_description:
        gallery.extend(_dom_images(soup, _DESCRIPTION, page_url))
    unique = {}
    for url in gallery:
        key = urlsplit(larger_image_url(url))._replace(query="", fragment="").geturl()
        unique.setdefault(key, url)
    return list(unique.values())


def page_status(url, title, html):
    parsed = urlsplit(url)
    location = ((parsed.hostname or "") + parsed.path + parsed.query).lower()
    title = " ".join(title.casefold().split())
    if any(term in location for term in ("captcha", "_____tmd_____", "sec.alibaba.com")) or any(
        term in title for term in ("captcha", "security verification", "安全验证", "验证码", "滑块验证")
    ):
        return "需验证"
    if "login.alibaba.com" in location or "/login" in location or title.startswith(
        ("sign in", "log in", "请登录", "登录")
    ):
        return "需登录"
    if "localStorage.x5referer" in html and "login.alibaba.com" in html and "window.detailData" not in html:
        return "需登录"
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.get_text(" ", strip=True).casefold().split())[:3500]
    if any(term in title for term in ("access denied", "forbidden", "拒绝访问")):
        return "拒绝访问"
    if next(product_payloads(soup), None) is None and not soup.select_one(_GALLERY):
        if any(term in text for term in ("商品不存在", "商品已下架", "product is no longer available",
                                        "product is not available", "page not found")):
            return "已下架"
        if any(term in text for term in ("slide to verify", "complete the security verification", "请完成安全验证", "拖动滑块")):
            return "需验证"
        if any(term in text for term in ("please sign in to continue", "请先登录后继续")):
            return "需登录"
        if any(term in text for term in ("access denied", "拒绝访问")):
            return "拒绝访问"
    return ""


def _selection_rows(sheet):
    columns = {str(value).strip(): index for index, value in enumerate(next(sheet.values, ()), 1) if value is not None}
    if not {"商品编号", "分类名", "链接"}.issubset(columns):
        raise ValueError("请选择任务目录中的选品结果表，表中须含「商品编号」「分类名」「链接」。")
    rows = []
    for index, cells in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
        def value(header):
            return str(cells[columns[header] - 1] or "").strip() if header in columns else ""

        if not any(cells):
            continue
        rows.append({"row": index, "code": value("商品编号"), "category": value("分类名"), "url": value("链接"),
                     "mode": value("图片模式"), "status": value("采集状态"), "clear": value("清晰图数"), "note": value("说明")})
    return columns, rows


def read_selection(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return _selection_rows(workbook.active)[1]
    finally:
        workbook.close()


def _folder_name(value, fallback):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:80].rstrip(" .") or fallback
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", clean.split(".")[0], re.I):
        clean = "_" + clean
    return clean


async def _read_page(page, url, _platform, *, confirm, include_description):
    async def snapshot():
        html = await page.content()
        final_url = page.url or url
        status = page_status(final_url, await page.title(), html)
        if not status and not _trusted_url(final_url, ("alibaba.com",)):
            status = "拒绝访问"
        return status, html

    state, html = await snapshot()
    if state in ("需登录", "需验证") and confirm is not None:
        await page.bring_to_front()

        def wait_for_confirmation():
            try:
                confirm("请在浏览器中完成阿里巴巴登录或验证，返回后点击「继续采集」。")
                return True
            except KeyboardInterrupt:
                return False

        if await asyncio.to_thread(wait_for_confirmation):
            state, html = await snapshot()
            if not state and urlsplit(page.url).path != urlsplit(url).path:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                state, html = await snapshot()
    if state:
        return state, html
    if include_description:
        try:
            await page.locator(_DESCRIPTION).first.scroll_into_view_if_needed(timeout=4000)
            await page.locator(f":is({_DESCRIPTION}) img").first.wait_for(state="attached", timeout=3000)
        except PageTimeout:
            pass
        state, html = await snapshot()
    return state, html


def _save_images(session, urls, folder, label, limit, min_side):
    saved = clear = 0
    seen = set()
    for url in urls[:limit]:
        for data, _ in image_responses(session, url):
            try:
                _, width, height, quality = inspect_image(data, min_side)
                if min(width, height) <= 2:
                    continue
                digest = hashlib.sha256(data).digest()
                if digest in seen:
                    break
                with Image.open(BytesIO(data)) as decoded:
                    image = ImageOps.exif_transpose(decoded)
                    if "A" in image.getbands() or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        background = Image.new("RGB", rgba.size, "white")
                        background.paste(rgba, mask=rgba.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{label}_{saved + 1:02d}.jpg"
            temporary = path.with_suffix(".jpg.tmp")
            try:
                image.save(temporary, "JPEG", quality=94)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            seen.add(digest)
            saved += 1
            clear += quality == "清晰"
            break
    return clear, saved


def _has_saved_images(folder, label, score):
    _, separator, count = score.partition("/")
    count = count.strip()
    if not separator or not re.fullmatch(r"[1-8]", count):
        return False
    return all((folder / f"{label}_{number:02d}.jpg").is_file() for number in range(1, int(count) + 1))


async def collect_images(settings, emit=lambda _event: None, confirm=None, stop_requested=lambda: False):
    settings = validate_image_settings(settings)
    workbook_path = Path(settings["input_file"])
    output = Path(settings["output_dir"])
    flat = settings["mode"] == IMAGE_MODES[1]
    label, limit = ("候选", 8) if flat else ("主图", 5)
    counts = {"done": 0, "rejected": 0, "failed": 0}
    configure_browser_path()

    def update(row, status, clear="", note=""):
        row.update(status=status, clear=clear, note=note)
        emit({"type": "image_row", **{key: row[key] for key in ("row", "code", "status", "clear", "note")}})

    with workbook_write_lock(workbook_path), requests.Session() as session:
        workbook = load_workbook(workbook_path)
        try:
            sheet = workbook.active
            columns, rows = _selection_rows(sheet)
            if not rows:
                raise ValueError("选品结果表没有需要采集的商品。")
            folders = {}
            for row in rows:
                code = _folder_name(row["code"], "")
                folder = (output / _folder_name(row["category"], "未分类") / code).resolve()
                key = str(folder).casefold()
                if not code or folder == output or not folder.is_relative_to(output) or key in folders:
                    raise ValueError(f"第 {row['row']} 行商品编号为空、目录无效，或商品目录重复。")
                folders[key] = row["code"]
                row["folder"] = folder
            for header in STATUS_HEADERS:
                if header not in columns:
                    columns[header] = sheet.max_column + 1
                    sheet.cell(1, columns[header], header)
            if sheet.auto_filter.ref:
                sheet.auto_filter.ref = sheet.dimensions
            # Check workbook writability before fetching pages or creating image files.
            save_workbook_atomic(workbook, workbook_path)
            async with Scraper(headless=not settings["show_browser"]) as scraper:
                for row in rows:
                    if stop_requested():
                        break
                    folder = row["folder"]
                    if settings["resume"] and row["status"] in _FINAL and _has_saved_images(folder, label, row["clear"]):
                        status = row["status"]
                        update(row, status, row["clear"], row["note"])
                    else:
                        update(row, "采集中", note="正在读取商品详情及主图。")
                        status, score, note = await _collect_product(
                            scraper, session, row, settings, workbook_path, flat, label, limit, confirm
                        )
                        for header, value in zip(STATUS_HEADERS, (status, score, note)):
                            sheet.cell(row["row"], columns[header], value)
                        save_workbook_atomic(workbook, workbook_path)
                        update(row, status, score, note)
                    counts["done" if status == COMPLETE else "rejected" if status == REJECTED else "failed"] += 1
                    emit({"type": "progress", "total": len(rows), **counts, "message": row["code"]})
        finally:
            workbook.close()
    stopped = stop_requested()
    return {"status": "stopped" if stopped else "finished", "total": len(rows), **counts,
            "image_workbook": str(workbook_path), "image_output": str(output),
            "message": f"{'已停止并保存' if stopped else '图片采集完成'}："
                       f"达标 {counts['done']}，淘汰 {counts['rejected']}，失败 {counts['failed']}"}


async def _collect_product(scraper, session, row, settings, workbook_path, flat, label, limit, confirm):
    url = row["url"]
    try:
        if validate_product_url(url) != "alibaba" or urlsplit(url).scheme != "https" or not urlsplit(url).path.startswith("/product-detail/"):
            raise ValueError("图片采集目前支持阿里巴巴国际站的 HTTPS 商品详情链接。")
        key = hashlib.sha256(f"{settings['mode']}\n{url}".encode()).hexdigest()[:24]
        snapshot = workbook_path.parent / "页面快照" / f"{key}.html"
        if settings["save_snapshot"] and snapshot.is_file():
            status, html = "", snapshot.read_text(encoding="utf-8")
        else:
            async def read_page(page, link, platform):
                return await _read_page(page, link, platform, confirm=confirm if settings["show_browser"] else None,
                                        include_description=flat)

            status, html = await scraper.fetch_product(url, read_page)
        if status:
            return status, "", _STATUS_NOTES[status]
        urls = extract_images(html, url, include_description=flat)
        if not urls:
            return "失败", "", "没识别到商品主图，请检查详情页是否能正常加载。"
        if settings["save_snapshot"] and not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(html, encoding="utf-8")
        session.headers["Referer"] = url
        clear, saved = await asyncio.to_thread(_save_images, session, urls, row["folder"], label, limit, settings["min_side"])
    except (PageTimeout, TimeoutError):
        return "超时", "", _STATUS_NOTES["超时"]
    except Exception as error:
        print(f"{row['code']} 图片采集失败：{error}")
        return "失败", "", f"{type(error).__name__}：{str(error)[:150]}"
    if not saved:
        return "失败", "0 / 0", "没有成功下载可读取的产品图，请检查网络后重试。"
    score = f"{clear} / {saved}"
    if clear < settings["min_clear"]:
        return REJECTED, score, f"清晰图不足，至少需要 {settings['min_clear']} 张。"
    return COMPLETE, score, f"已保存 {saved} 张{label}。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", help="选品结果 Excel")
    parser.add_argument("output_dir", help="图片保存目录")
    parser.add_argument("--mode", choices=IMAGE_MODES, default=IMAGE_MODES[0])
    parser.add_argument("--min-clear", type=int)
    parser.add_argument("--min-side", type=int, default=600)
    parser.add_argument("--headless", dest="show_browser", action="store_false")
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--no-snapshots", dest="save_snapshot", action="store_false")
    settings = {key: value for key, value in vars(parser.parse_args()).items() if value is not None}
    try:
        result = asyncio.run(collect_images(settings, confirm=input))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(result["message"])


if __name__ == "__main__":
    main()
