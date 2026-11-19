"""Write a reviewable Excel workbook for one completed search run."""

from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from desktop_runtime import IMAGE_MODES

_REVIEW_CHOICES = ("待选", "入选", "跳过")
_HEADERS = ("选品结果", "图片模式", "商品 ID", "标题", "链接", "缩略图链接")


def write_candidates(path, items, image_mode=IMAGE_MODES[0]):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "候选商品"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "C2"
    sheet.append(_HEADERS)
    sheet.row_dimensions[1].height = 26
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="25485A")
        cell.font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for column, width in zip("ABCDEF", (15, 18, 19, 72, 32, 32)):
        sheet.column_dimensions[column].width = width

    for item in items:
        title = ILLEGAL_CHARACTERS_RE.sub("", item["title"])
        sheet.append((_REVIEW_CHOICES[0], image_mode, item["id"], title or None,
                      item["url"] or None, item["thumb"] or None))
        row = sheet[sheet.max_row]
        sheet.row_dimensions[sheet.max_row].height = 34
        for cell in row:
            if cell.value is not None:
                # Titles and IDs are literal text even when a source title starts with '='.
                cell.data_type = "s"
            cell.font = Font(name="Arial", size=10, color="253443")
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=cell.column == 4)
        for cell in row[:2]:
            cell.fill = PatternFill("solid", fgColor="FFF2D5")
        row[2].number_format = "@"
        for cell, domains in zip(row[4:], (("alibaba.com",), ("alicdn.com", "alibaba.com"))):
            cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            if cell.value:
                url = urlsplit(cell.value)
                host = url.hostname or ""
                if url.scheme in ("http", "https") and url.username is None and any(
                        host == domain or host.endswith(f".{domain}") for domain in domains):
                    cell.hyperlink = cell.value
                    cell.font = Font(name="Arial", size=10, color="166286", underline="single")

    sheet.auto_filter.ref = sheet.dimensions
    if sheet.max_row > 1:
        for column, choices in (("A", _REVIEW_CHOICES), ("B", IMAGE_MODES)):
            validation = DataValidation(type="list", formula1='"' + ",".join(choices) + '"',
                                        showErrorMessage=True, error="请选择下拉列表中的值。")
            sheet.add_data_validation(validation)
            validation.add(f"{column}2:{column}{sheet.max_row}")

    temporary = path.with_suffix(".tmp")
    try:
        workbook.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
