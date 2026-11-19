"""Collect Alibaba Deep Search candidates and export them for Excel review."""

import argparse
import json
import re
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from candidate_workbook import write_candidates
from desktop_runtime import IMAGE_MODES

ORIGIN = "https://www.alibaba.com"
SEARCH_URL = f"{ORIGIN}/search/page"
PRODUCT_LINK = 'a[href*="/product-detail/"]'
CURRENT_PAGE = '.searchx-pagination [aria-current="page"]'
NEXT_PAGE = '.searchx-pagination > a:has(> span.pagination-item.next[aria-disabled="false"])'
_ID = re.compile(r"_(\d{8,})\.html$")
_CHECKPOINT = re.compile(r"captcha|punish|/login|login\.|sign[ -]?in|security verification|access denied|verify you are human|验证码|安全验证|请登录", re.I)
_BADGES = {"认证", "verified", "certified"}

# The page number can change before the client replaces the old product grid.
_RESULTS_READY = r"""
({number, previous, currentPage, productLink}) => {
    if (document.querySelector(currentPage)?.textContent.trim() !== String(number)) return false;
    const ids = [...new Set(Array.from(document.querySelectorAll(productLink),
        link => new URL(link.href).pathname.match(/_(\d{8,})\.html$/)?.[1]).filter(Boolean))];
    return ids.length > 0 && (ids.length !== previous.length || ids.some((id, i) => id !== previous[i]));
}
"""


def extract_items(html: str) -> list[dict]:
    """Pair public search thumbnails and headings by product ID."""
    items = {}
    for link in BeautifulSoup(html, "html.parser").select(PRODUCT_LINK):
        parsed = urlsplit(urljoin(ORIGIN, link["href"]))
        match = _ID.search(parsed.path)
        if not match:
            continue
        product_id = match[1]
        item = items.setdefault(product_id, {
            "id": product_id, "url": parsed._replace(query="", fragment="").geturl(), "title": "", "thumb": "",
        })
        img = link.find("img")
        heading = link.find(["h2", "h3", "h4"]) or link.find_parent(["h2", "h3", "h4"])
        text = " ".join(part for part in (heading or link).stripped_strings if part.casefold() not in _BADGES)
        if heading and text:
            item["title"] = text
        elif not item["title"]:
            for value in (link.get("title"), img.get("alt") if img else None, text):
                if value and value.strip() and value.strip().casefold() not in _BADGES:
                    item["title"] = value.strip()
                    break
        if img and not item["thumb"]:
            for attribute in ("data-src", "src"):
                value = (img.get(attribute) or "").strip()
                if value and urlsplit(urljoin(ORIGIN, value)).scheme in ("http", "https"):
                    item["thumb"] = urljoin(ORIGIN, value)
                    break
    return list(items.values())


def wait_for_results(page, number, previous):
    page.wait_for_function(_RESULTS_READY, arg={
        "number": number, "previous": previous, "currentPage": CURRENT_PAGE, "productLink": PRODUCT_LINK,
    }, timeout=25000).dispose()


def advance_page(page, number, previous):
    page.locator(NEXT_PAGE).click(timeout=10000)
    wait_for_results(page, number, previous)


def confirm_access(page, headless, confirm=None):
    if not _CHECKPOINT.search(f"{page.url} {page.title()}"):
        return False
    if headless:
        raise RuntimeError("遇到登录或验证码，请使用可见浏览器手动处理")
    page.bring_to_front()
    (confirm or input)("请在浏览器中手动完成登录或验证码，再继续。")
    if _CHECKPOINT.search(f"{page.url} {page.title()}"):
        raise RuntimeError("页面仍显示登录或验证码")
    return True


def search_url(search):
    search = search.strip()
    if not search:
        raise ValueError("请输入搜索关键词")
    target = urlsplit(search)
    if target.scheme:
        keyword = parse_qs(target.query).get("SearchText", [""])[0]
        if (target.scheme, target.netloc, target.path) != ("https", "www.alibaba.com", "/search/page") or not keyword:
            raise ValueError("请输入带 SearchText 的阿里巴巴国际站搜索页 URL")
        return search
    query = urlencode({"SearchScene": "proSearch", "SearchText": search, "pro": "true", "from": "pcHomeContent"})
    return f"{SEARCH_URL}?{query}"


def collect_search(url, pages, consume, *, headless=False, confirm=None, stop_requested=lambda: False):
    from patchright.sync_api import TimeoutError as BrowserTimeout
    from scrapling.fetchers import StealthySession
    from desktop_runtime import configure_browser_path, login_state_path
    from scraper import BROWSER_OPTIONS, STATE_FILES

    configure_browser_path()
    state = login_state_path(STATE_FILES["alibaba"])
    processed = False
    failure = None

    def collect(page):
        nonlocal processed, failure
        previous = []
        try:
            if stop_requested():
                return
            confirm_access(page, headless, confirm)
            for number in range(1, pages + 1):
                if stop_requested():
                    return
                try:
                    if number == 1:
                        wait_for_results(page, number, previous)
                    else:
                        advance_page(page, number, previous)
                except BrowserTimeout:
                    if not confirm_access(page, headless, confirm):
                        raise RuntimeError(f"等待第 {number} 页的页码和商品列表更新超时") from None
                    wait_for_results(page, number, previous)
                items = extract_items(page.content())
                if not items:
                    raise RuntimeError(f"第 {number} 页结果已更新，但未解析出任何商品")
                previous = [item["id"] for item in items]
                processed = True
                if consume(number, items) is False:
                    return
        except Exception as error:
            failure = error

    with StealthySession(headless=headless, timeout=30000, retries=1, solve_cloudflare=False, **BROWSER_OPTIONS) as session:
        if state.is_file():
            session.load_state(str(state))
        else:
            print("未找到阿里巴巴登录状态，将以访客身份打开搜索页")
        response = session.fetch(url, page_action=collect)
    if failure is not None:
        raise failure
    if not processed and not stop_requested():
        raise RuntimeError(f"搜索页未返回可采集结果，HTTP {response.status}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("search", help="搜索关键词或 https://www.alibaba.com/search/page 的完整地址")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--image-mode", choices=IMAGE_MODES, default=IMAGE_MODES[0], help="候选表的默认图片模式")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("页数必须大于 0")
    try:
        url = search_url(args.search)
    except ValueError as error:
        parser.error(str(error))
    Path("debug").mkdir(exist_ok=True)
    out = Path(mkdtemp(prefix="alibaba_search_", dir="debug"))
    candidates = {}

    def record(number, items):
        new = len({item["id"] for item in items} - candidates.keys())
        for item in items:
            candidates.setdefault(item["id"], item)
        (out / f"page_{number}.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"第 {number} 页 · {len(items)} 个商品，新增 {new}，重复 {len(items) - new}"
              f"，缺标题 {sum(not item['title'] for item in items)}，缺缩略图 {sum(not item['thumb'] for item in items)}")

    try:
        collect_search(url, args.pages, record, headless=args.headless)
    except Exception as error:
        raise SystemExit(str(error)) from error
    workbook = out / "候选商品.xlsx"
    write_candidates(workbook, candidates.values(), args.image_mode)
    print(f"共 {len(candidates)} 个不同商品。候选 Excel 已保存到 {workbook.resolve()}")


if __name__ == "__main__":
    main()
