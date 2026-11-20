"""Download candidate images, build the offline wall, and import reviewed selections."""

import csv
import json
import re
from io import BytesIO
from urllib.parse import urlsplit

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from PIL import Image, ImageFilter, ImageOps, ImageStat

from desktop_runtime import IMAGE_MODES, resource_dir, save_workbook_atomic, workbook_write_lock, write_json
from probe_search import collect_search, search_url

_SIZED_IMAGE = re.compile(r"(\.(?:jpe?g|png|webp|avif))_\d+x\d+(?:[a-z0-9]*)\.(?:jpe?g|png|webp|avif)$", re.I)
_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "AVIF": "avif"}
_PIXEL_LIMIT = 25_000_000
_BYTE_LIMIT = 16 * 1024 * 1024
# Initial edge-variance cutoff, to be calibrated against reviewed product photos.
SHARPNESS_THRESHOLD = 100
_CSV_HEADERS = ["商品 ID", "链接", "标题"]
_RESULT_HEADERS = ["商品编号", "分类名", "商品 ID", "标题", "链接", "图片模式"]


def _trusted_url(url, domains):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return parsed.scheme == "https" and parsed.username is None and parsed.port in (None, 443) and any(
            host == domain or host.endswith(f".{domain}") for domain in domains)
    except ValueError:
        return False


def larger_image_url(url):
    parsed = urlsplit(url)
    return parsed._replace(path=_SIZED_IMAGE.sub(r"\1", parsed.path)).geturl()


def inspect_image(data, min_side=600):
    with Image.open(BytesIO(data)) as decoded:
        extension = _EXTENSIONS.get(decoded.format)
        if not extension or decoded.width * decoded.height > _PIXEL_LIMIT:
            raise ValueError("图片格式或尺寸不受支持")
        image = ImageOps.exif_transpose(decoded)
        width, height = image.size
        if min(width, height) < min_side:
            return extension, width, height, "分辨率低"
        gray = image.convert("L")
        gray.thumbnail((600, 600), Image.Resampling.LANCZOS)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        if min(edges.size) <= 2:
            return extension, width, height, "偏糊"
        interior = edges.crop((1, 1, edges.width - 1, edges.height - 1))
        quality = "清晰" if ImageStat.Stat(interior).var[0] >= SHARPNESS_THRESHOLD else "偏糊"
        return extension, width, height, quality


def image_responses(session, url):
    if not _trusted_url(url, ("alicdn.com", "alibaba.com")):
        return
    large = larger_image_url(url)
    for address in dict.fromkeys((large, url)):
        try:
            with session.get(address, stream=True, timeout=(5, 10), allow_redirects=False) as response:
                if response.status_code != 200:
                    continue
                data = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    data.extend(chunk)
                    if len(data) > _BYTE_LIMIT:
                        raise ValueError("图片文件过大")
            yield data, address
        except (requests.RequestException, OSError, ValueError):
            continue


def _download_image(session, url, product_id, folder):
    for data, address in image_responses(session, url):
        try:
            extension, width, height, quality = inspect_image(data)
        except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
            continue
        name = f"{product_id}.{extension}"
        (folder / name).write_bytes(data)
        if address == url and url != larger_image_url(url) and quality == "清晰":
            quality = "待复核"
        return {"image": f"candidates/{name}", "width": width, "height": height, "quality": quality}
    return {}


def _resolve_prefix(category, prefix):
    prefix = prefix.strip()
    if not prefix:
        initials = re.findall(r"\b([A-Za-z])[A-Za-z0-9]*", category)
        return "".join(initials[:8]).upper() or "SP"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,7}", prefix):
        raise ValueError("编号前缀须以字母开头，最多 8 位字母或数字")
    return prefix.upper()


def write_candidate_wall(candidates, folder, *, category="", prefix="", image_mode=IMAGE_MODES[0],
                         emit=lambda _event: None, stop_requested=lambda: False):
    if image_mode not in IMAGE_MODES:
        raise ValueError("未识别的图片模式")
    prefix = _resolve_prefix(category, prefix)
    template = (resource_dir() / "assets" / "candidate_wall.html").read_text(encoding="utf-8")
    images = folder / "candidates"
    images.mkdir(parents=True, exist_ok=True)
    items = []
    downloaded = 0
    with requests.Session() as session:
        for number, candidate in enumerate(candidates, 1):
            product_id, url = candidate["id"], candidate["url"]
            if not re.fullmatch(r"[0-9]{8,24}", product_id) or not _trusted_url(url, ("alibaba.com",)):
                raise ValueError("候选商品编号或链接无效")
            item = {"id": product_id, "title": candidate["title"], "url": url, "image": "",
                    "width": None, "height": None, "quality": "未获取", "rejected": False}
            if not stop_requested():
                item.update(_download_image(session, candidate["thumb"], product_id, images))
            downloaded += bool(item["image"])
            items.append(item)
            emit({"type": "progress", "total": len(candidates), "done": downloaded, "failed": number - downloaded,
                  "message": f"已下载主图 {downloaded}/{len(candidates)}"})
    manifest = {"category": category, "prefix": prefix, "image_mode": image_mode, "items": items}
    # Escaping '<' prevents untrusted titles from closing the JSON script element.
    encoded = json.dumps(manifest, ensure_ascii=False).replace("<", r"\u003c")
    wall = folder / "candidates.html"
    write_json(folder / "candidate_manifest.json", manifest)
    wall.write_text(template.replace("__CANDIDATE_DATA__", encoded, 1), encoding="utf-8")
    return wall, items


def run_candidate_search(settings, folder, emit, prompt, stop_requested):
    def terms(value):
        return [part.strip() for part in re.split(r"[,，、]", value) if part.strip()]

    search = settings["search"].strip()
    keywords = [search] if search.startswith("https://") else terms(search)
    pages, limit = settings["pages"], settings["limit"]
    if not keywords or pages < 1 or limit < 1:
        raise ValueError("请填写搜索关键词、页数和候选上限")
    required = terms(settings.get("required", "").casefold())
    excluded = terms(settings.get("excluded", "").casefold())
    candidates = {}
    for keyword in keywords:
        if stop_requested():
            break

        def record(number, items):
            for item in items:
                title = item["title"].casefold()
                if any(word in title for word in excluded) or (required and not any(word in title for word in required)):
                    continue
                candidates.setdefault(item["id"], item)
                if len(candidates) == limit:
                    break
            emit({"type": "log", "message": f"{keyword} · 第 {number}/{pages} 页 · 抓到 {len(items)} 个 · 累计候选 {len(candidates)} 个"})
            return len(candidates) < limit

        try:
            collect_search(search_url(keyword), pages, record, headless=not settings.get("show_browser", True),
                           confirm=prompt, stop_requested=stop_requested)
        except KeyboardInterrupt:
            if not stop_requested():
                raise
            break
        if len(candidates) >= limit:
            break

    wall, items = write_candidate_wall(list(candidates.values()), folder, category=settings.get("category", ""),
                                       prefix=settings.get("prefix", ""), image_mode=settings.get("image_mode", IMAGE_MODES[0]),
                                       emit=emit, stop_requested=stop_requested)
    ready = sum(bool(item["image"]) for item in items)
    stopped = stop_requested()
    return {"status": "stopped" if stopped else "finished", "wall_file": str(wall), "total": len(items),
            "done": ready, "failed": len(items) - ready,
            "message": f"{'已停止' if stopped else '搜索完成'} · 候选 {len(items)} 个 · 主图已下载 {ready}/{len(items)}"}


def _result_sheet(workbook, *, task=False):
    sheet = workbook.active
    if list(next(sheet.values, ())) == []:
        sheet.title = "选品结果"
        sheet.append(_RESULT_HEADERS)
        sheet.freeze_panes = "A2"
    headers = [cell.value for cell in sheet[1]]
    if (headers[:len(_RESULT_HEADERS)] if task else headers) != _RESULT_HEADERS:
        raise ValueError("现有选品结果表的格式无法识别")
    return sheet


def _save_result_rows(workbook, sheet, rows, path):
    if not rows:
        return
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            cell.data_type = "s"
    sheet.auto_filter.ref = sheet.dimensions
    save_workbook_atomic(workbook, path)


def _update_task_workbook(path, rows):
    with workbook_write_lock(path):
        workbook = load_workbook(path) if path.exists() else Workbook()
        try:
            sheet = _result_sheet(workbook, task=True)
            existing_codes = {row[0] for row in sheet.iter_rows(min_row=2, values_only=True)}
            _save_result_rows(workbook, sheet, [row for row in rows if row[0] not in existing_codes], path)
        finally:
            workbook.close()


def import_selection(csv_path, manifest_path, workbook_path, *, task_workbook_path=None):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    category = manifest["category"]
    prefix = _resolve_prefix(category, manifest["prefix"])
    if manifest["image_mode"] not in IMAGE_MODES:
        raise ValueError("候选批次的图片模式无效")
    known = {item["id"]: item for item in manifest["items"]}
    selected = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != _CSV_HEADERS:
            raise ValueError("请选择图片墙导出的选中结果 CSV")
        for row in reader:
            item = known.get(row["商品 ID"])
            if not item or row["链接"] != item["url"] or not _trusted_url(item["url"], ("alibaba.com",)):
                raise ValueError("CSV 包含不属于当前候选批次的商品或链接")
            selected[item["id"]] = item
    if not selected:
        raise ValueError("CSV 中没有选中商品")
    if task_workbook_path is not None and task_workbook_path.resolve() == workbook_path.resolve():
        raise ValueError("任务选品表与总表必须位于不同文件夹")

    # Lock the read/number/save cycle across windows so they cannot allocate the same code.
    with workbook_write_lock(workbook_path):
        workbook = load_workbook(workbook_path) if workbook_path.exists() else Workbook()
        try:
            sheet = _result_sheet(workbook)
            existing = list(sheet.iter_rows(min_row=2, values_only=True))
            reviewed = {(row[1] or "", str(row[2])) for row in existing}
            last = max((int(match[1]) for row in existing
                        if (match := re.fullmatch(rf"{prefix}(\d+)", str(row[0]), flags=re.I))), default=0)
            added = []
            for product_id, item in selected.items():
                if (category, product_id) in reviewed:
                    continue
                last += 1
                code = f"{prefix}{last:02d}"
                title = ILLEGAL_CHARACTERS_RE.sub("", item["title"])
                added.append((code, category, product_id, title, item["url"], manifest["image_mode"]))

            task_codes = set()
            if task_workbook_path is not None:
                folder = task_workbook_path.parent.resolve()
                root = workbook_path.parent.resolve()
                task_key = str(folder.relative_to(root) if folder.is_relative_to(root) else folder)
                index_name = "_选品任务"
                index = workbook[index_name] if index_name in workbook.sheetnames else None
                if added:
                    if index is None:
                        index = workbook.create_sheet(index_name)
                        index.sheet_state = "hidden"
                    # Save the source task with each code so a retry can repair a failed task export.
                    for row in added:
                        index.append((task_key, row[0]))
                        index.cell(index.max_row, 1).data_type = "s"
                if index is not None:
                    task_codes = {code for key, code in index.values if key == task_key}

            _save_result_rows(workbook, sheet, added, workbook_path)
            if task_codes:
                task_rows = [row for row in existing + added if row[0] in task_codes]
                try:
                    _update_task_workbook(task_workbook_path, task_rows)
                except OSError as error:
                    raise OSError(f"总表记录已保留，任务表写入失败。请关闭任务表后重新导入。\n{error}") from error
            return [row[0] for row in added]
        finally:
            workbook.close()
