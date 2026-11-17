# -*- coding: utf-8 -*-
"""异步抓取 Alibaba / 淘宝 / 天猫。
每页只取一次 HTML 快照，价格、详情、变体都在本地解析，避免几十次浏览器往返。"""
import asyncio
import json
import os
import random
import re
import time
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from patchright.async_api import TimeoutError as PWTimeout
from scrapling.fetchers import AsyncStealthySession

try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:
    _PARSER = "html.parser"
# Login and scraping must use the same browser settings and state filenames.
BROWSER_OPTIONS = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai", "google_search": False}
STATE_FILES = {"taobao": "taobao_state.json", "tmall": "tmall_auth.json", "alibaba": "alibaba_state.json"}
BLOCK_ALIBABA_ASSETS = True      # 阿里页拦截字体/媒体以加快加载（图片不拦：颜色选项是图片）；若发现被风控，改为 False
TAOBAO_DELAY = (1.5, 3.5)        # 淘宝/天猫每条之间的随机间隔（秒），降低风控概率

_COLOR_KEYS = ("颜色", "color", "colour")
_SIZE_KEYS = ("尺寸", "尺码", "size", "长度", "length", "大小")
_MONEY = re.compile(r"(US\$|CNY¥|CN¥|[¥￥$€£])\s*(\d[\d,]*(?:\.\d+)?)", re.I)
_SKU_REGIONS = '[data-module-name="module_sku"], [data-module-name="module_skuPanel"], [data-testid="pc-purchase-sku-options"]'
_SKU_ROWS = '[data-testid="sku-list"], [data-testid="pc-purchase-sku-option-attribute"]'
_ALI_DETAIL_SELECTORS = [
    'div[data-module-name="module_product_overview"]',
    'div[data-module-name="module_key_attributes"]',
    'div[data-module-name="module_attributes"]',
    'div[data-module-name="module_description"]',
    'div[data-module-name*="description"]',
    'div[data-module-name*="attribute"]',
    'div[data-module-name*="overview"]',
    '[data-testid*="attribute"]',
    '[data-testid*="overview"]',
]


# ── 标题 / URL / 文本过滤 ─────────────────────────────────────────────────────

def clean_page_title(raw_title: str) -> str:
    """去掉平台尾缀、噪音词和开头的【包邮】之类标签。"""
    t = (raw_title or "").strip()
    t = re.sub(r"(?i)[-_|]\s*(淘宝网|天猫精选|天猫|tmall(\.com)?|taobao(\.com)?|alibaba\.com)", "", t)
    t = re.sub(r"(?i)\s*on alibaba\.com", "", t)
    for kw in ("定制可供来样定做", "定制", "可供来样定做"):
        t = t.replace(kw, "")
    t = re.sub(r"(?i)\s*-\s*buy\s+.*$", "", t).strip()                      # Alibaba: "<主体> - Buy ... Product"
    t = re.sub(r"^(\s*[\[\【][^\]\】]{1,12}[\]\】]\s*)+", "", t)             # 开头的【包邮】【现货】…
    return re.sub(r"\s+", " ", t).strip()


def _extract_url_keywords(url: str, limit: int = 20) -> list[str]:
    if not url:
        return []

    try:
        parsed = urlparse(url)
        pieces = []

        for seg in (parsed.path or "").split("/"):
            seg = seg.strip()
            if seg:
                pieces.append(seg)

        qs = parse_qs(parsed.query or "")
        for k, vals in qs.items():
            if not vals:
                continue
            if str(k).lower() in {
                "id", "itemid", "spm", "skuid", "mi_id",
                "utm_source", "utm_medium", "utm_campaign",
                "utm_term", "utm_content", "xxc",
            }:
                continue
            pieces.extend([str(v) for v in vals if v is not None])

        raw = " ".join(pieces)
    except Exception:
        raw = url

    raw = re.sub(r"[%_+\-./=&#?]", " ", raw)
    tokens = re.split(r"\s+", raw)

    out = []
    seen = set()
    stop = {
        "item", "detail", "html", "htm", "www", "com",
        "taobao", "tmall", "product", "products", "index",
    }

    for t in tokens:
        t = t.strip()
        if not t:
            continue
        low = t.lower()
        if low in stop:
            continue
        if t.isdigit():
            continue
        if len(t) < 2 or len(t) > 24:
            continue

        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)
        if not cleaned:
            continue
        low2 = cleaned.lower()
        if low2 in stop or low2 in seen:
            continue

        seen.add(low2)
        out.append(cleaned)
        if len(out) >= limit:
            break

    return out


def _filter_chinese_text(text: str, max_len: int = 1500) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).splitlines()]
    kept = [ln for ln in lines if ln and re.search(r"[\u4e00-\u9fff]", ln)]
    if not kept:
        kept = [ln for ln in lines if ln]
    out = "\n".join(kept)
    return out[:max_len]


def _filter_generic_text(text: str, max_len: int = 2500, max_line_len: int = 600) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).splitlines()]
    out = []
    seen = set()
    noise = [
        "Alibaba.com", "Get the app", "Sign in", "Sign up",
        "Chat now", "Send Inquiry", "Cookie", "Privacy",
        "All categories", "Request for Quotation",
    ]
    total = 0
    for ln in lines:
        if not ln:
            continue
        if any(n.lower() in ln.lower() for n in noise):
            continue
        if max_line_len and len(ln) > max_line_len:
            ln = ln[:max_line_len].rstrip()
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
        total += len(ln) + 1
        if total >= max_len:
            break
    return "\n".join(out)[:max_len]

# ── 价格 / 变体 / 详情解析（输入都是 HTML 字符串，纯函数，可单独测试）──────────────

def _amounts(text: str) -> list[tuple[str, float]]:
    out = []
    for cur, num in _MONEY.findall(text or ""):
        cur = cur.replace("￥", "¥")
        cur = {"CNY¥": "CN¥", "US$": "US$"}.get(cur.upper(), cur)
        out.append((cur, float(num.replace(",", ""))))
    return out


def _price_range(texts: list[str], limit: int | None = None) -> str:
    """只返回“币种+金额”（区间用 ' - ' 连接）；文本里没有金额就返回空串，绝不回传原文。"""
    found = [a for t in texts for a in _amounts(t)][:limit]
    if not found:
        return ""
    cur = "US$" if any(c == "US$" for c, _ in found) else found[0][0]
    nums = [n for c, n in found if c == cur]
    fmt = lambda x: f"{cur}{int(x) if x == int(x) else x}"
    lo, hi = min(nums), max(nums)
    return fmt(lo) if lo == hi else f"{fmt(lo)} - {fmt(hi)}"


def _snippet(text: str, anchors: list[str], max_len: int) -> str:
    low = text.lower()
    pos = next((p for p in (low.find(a.lower()) for a in anchors) if p != -1), None)
    return text[:max_len] if pos is None else text[max(0, pos - 200): max(0, pos - 200) + max_len]


def _sku_json(soup) -> dict[str, list[str]]:
    """Read only the current product's SKU data, excluding recommendations and customization."""
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        text = script.string or ""
        match = re.search(r"window\.detailData\s*=\s*", text)
        if not match:
            continue
        try:
            data, _ = decoder.raw_decode(text, match.end())
            sku = data["globalData"]["product"]["sku"]
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(sku, dict):
            continue
        out = {}
        props = sku.get("skuAttrs") or sku.get("skuSummaryAttrs") or []
        for prop in props if isinstance(props, list) else []:
            if not isinstance(prop, dict) or not isinstance(prop.get("values"), list):
                continue
            name = str(prop.get("name") or "").strip()
            vals = [str(v.get("name") or v.get("value") or "").strip()
                    for v in prop.get("values", []) if isinstance(v, dict)]
            if name and any(vals):
                out[name] = list(dict.fromkeys(v for v in vals if v))
        if out:
            return out
    return {}


def _sku_lists(soup) -> dict[str, list[str]]:
    """Read current and legacy selectable options, including unselected/image-only values."""
    out: dict[str, list[str]] = {}
    for row in soup.select(_SKU_ROWS):
        current = row.get("data-testid") == "pc-purchase-sku-option-attribute"
        title = row.find("div", recursive=False) if current else row.select_one('[data-testid="sku-list-title"]')
        box = row if current else row.select_one('[data-testid="sku-list-item"]')
        name = re.split(r"[：:]", title.get_text(" ", strip=True))[0].strip() if title else ""
        if not name or not box:
            continue
        vals = []
        selector = '[data-testid="pc-purchase-sku-option"]' if current else '[data-testid$="sku-item"]'
        for item in box.select(selector):
            img = item.select_one("img[alt]")
            vals.append((item.get("aria-label") or (img.get("alt") if img else "") or item.get_text(" ", strip=True)).strip())
        vals = list(dict.fromkeys(v for v in vals if v and not re.fullmatch(r"\+\d+", v)))
        if len(vals) > len(out.get(name, [])):
            out[name] = vals
    return out


def _variant_cells(dims: dict[str, list[str]]) -> dict[str, str]:
    """维度 -> Excel 列：颜色 / 尺寸（含长度、尺码）/ 其他规格；选项之间用英文逗号。"""
    groups: dict[str, list[tuple[str, list[str]]]] = {}
    for name, vals in dims.items():
        low = name.lower()
        col = "颜色" if any(k in low for k in _COLOR_KEYS) else "尺寸" if any(k in low for k in _SIZE_KEYS) else "其他规格"
        groups.setdefault(col, []).append((name, vals))
    return {
        col: ", ".join(items[0][1]) if col != "其他规格" and len(items) == 1
        else "; ".join(f"{n}: {', '.join(v)}" for n, v in items)
        for col, items in groups.items()
    }


def _extract_alibaba(html: str) -> dict:
    soup = BeautifulSoup(html, _PARSER)
    items = soup.select('div[data-module-name="module_price"] [data-testid="ladder-price"] .price-item')[:10]
    price = (_price_range([(i.select_one("span") or i).get_text("", strip=True) for i in items])
             or _price_range([i.get_text(" ", strip=True) for i in items]))
    if not price:
        amt, cur = (soup.select_one(f'meta[property="og:price:{k}"]') for k in ("amount", "currency"))
        if amt and amt.get("content"):
            price = f"{(cur.get('content') if cur else '') or ''}{amt['content']}".strip()
    if not price:
        mod = soup.select_one('div[data-module-name="module_price"]')
        price = _price_range([mod.get_text(" ", strip=True)], limit=4) if mod else ""

    parts: list[str] = []
    for sel in _ALI_DETAIL_SELECTORS:
        parts += [t for t in (n.get_text("\n", strip=True) for n in soup.select(sel)[:2]) if t]
        if sum(map(len, parts)) > 6500:
            break
    body = soup.body.get_text("\n", strip=True) if soup.body else ""
    sku = soup.select(_SKU_REGIONS)
    embedded = _sku_json(soup)
    h1 = soup.select_one("h1")
    return {
        "price": price,
        "props": "\n".join(parts) or _snippet(body, ["Key attributes", "Attributes", "Product Description",
                                                      "Packaging and delivery", "Overview"], 4500),
        "h1": h1.get_text(strip=True) if h1 else "",
        # Embedded SKU arrays remain complete when the rendered panel is collapsed.
        "dims": {**_sku_lists(soup), **embedded},
        "has_more": not embedded and any(n.find(string=re.compile(r"^\s*\+\d+\s*$")) for n in sku),
    }


def _extract_taobao(html: str) -> dict:
    soup = BeautifulSoup(html, _PARSER)
    price = ""
    for node in soup.select('.tm-price, [class*="price" i]')[:40]:
        t = node.get_text(" ", strip=True)
        if len(t) <= 40:  # 过长的一定是容器，不是价格节点
            price = _price_range([t]) or ("¥" + t if re.fullmatch(r"\d+(?:\.\d{1,2})?", t) else "")
            if price:
                break
    props = "\n".join(n.get_text("\n", strip=True) for n in soup.select(".tm-propertylist, .tb-property"))
    return {"price": price, "props": props}


async def _block_assets(route):
    if route.request.resource_type in ("media", "font"):
        await route.abort()
    else:
        await route.continue_()


# ── 浏览器会话 ────────────────────────────────────────────────────────────────

def _dump_sku(url: str, html: str) -> None:
    """Save enough page evidence to diagnose missing variants locally."""
    soup = BeautifulSoup(html, _PARSER)
    mods = soup.select(_SKU_REGIONS)
    m = re.search(r"_(\d+)\.html", url)
    path = os.path.join("debug", f"sku_{m.group(1) if m else 'page'}.html")
    os.makedirs("debug", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(map(str, mods)) if any(n.get_text(strip=True) for n in mods) else html)
    print(f"⚠ 没读到变体，已保存 {path}")


def validate_product_url(url: str) -> str:
    """Validate the actual host and return its supported login site."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        invalid = (parsed.scheme not in ("http", "https") or not host
                   or parsed.username is not None or parsed.password is not None or parsed.port == 0)
    except ValueError:
        raise ValueError("商品链接格式无效，请使用完整的 http(s) 商品链接。") from None
    if invalid:
        raise ValueError("商品链接格式无效，请使用不含账号密码的完整 http(s) 链接。")
    # Match domain boundaries; platform names in a path or query prove nothing.
    for site, domain in (("alibaba", "alibaba.com"), ("taobao", "taobao.com"), ("tmall", "tmall.com")):
        if host == domain or host.endswith("." + domain):
            return site
    raise ValueError(f"不支持的商品域名：{host}；仅支持 Alibaba.com、淘宝和天猫，不支持 1688。")


def _platform(url: str) -> str:
    return "alibaba" if validate_product_url(url) == "alibaba" else "taobao"


def _state_file(url: str) -> str | None:
    site = validate_product_url(url)
    name = STATE_FILES[site]
    return name if os.path.exists(name) else None


class Scraper:
    """Reuse Scrapling browser sessions per login state, with platform concurrency limits."""

    def __init__(self, headless: bool = False, concurrency: dict | None = None, captcha_wait: int = 120,
                 stop_event=None):
        self.headless, self.captcha_wait = headless, captcha_wait
        self.stop_event = stop_event
        conc = {"alibaba": 2, "taobao": 1, **(concurrency or {})}
        self._concurrency = conc
        self._sems = {k: asyncio.Semaphore(v) for k, v in conc.items()}
        self._sessions: dict = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._stack.__aexit__(*exc)
        self._sessions.clear()

    async def _session(self, plat: str, state: str | None):
        async with self._lock:
            key = (plat, state)
            if key not in self._sessions:
                # A corrupt state must close its browser before the next row retries it.
                async with AsyncExitStack() as pending:
                    session = await pending.enter_async_context(AsyncStealthySession(
                        headless=self.headless, max_pages=self._concurrency[plat], timeout=60000,
                        **BROWSER_OPTIONS))
                    if state:
                        await session.load_state(state)
                    else:
                        print(f"⚠ 未找到 {plat} 登录状态文件，将以未登录身份访问")
                    self._stack.push_async_exit(pending.pop_all())
                self._sessions[key] = session
            return self._sessions[key]

    async def _pass_checkpoint(self, page) -> bool:
        """淘宝/天猫遇到登录页或风控页：有头模式下等你手动完成验证，无头模式直接放弃。"""
        blocked = lambda: "login" in page.url or "sec.taobao.com" in page.url
        if not blocked():
            return True
        if self.headless:
            print("未登录或触发风控，无法抓取该商品页。")
            return False
        print(f"⚠ 触发登录/滑块验证，请在浏览器中手动完成（最多等 {self.captcha_wait} 秒）…")
        await page.bring_to_front()
        deadline = time.monotonic() + self.captcha_wait
        while time.monotonic() < deadline:
            if self.stop_event is not None and self.stop_event.is_set():
                return False
            if not blocked():
                await page.wait_for_load_state("load")
                return True
            await asyncio.sleep(1)
        print("等待验证超时，跳过该商品。")
        return False

    async def _expand_skus(self, page, dims: dict) -> dict:
        """摘要里有“+N”说明选项被折叠：点“立即选择”展开，再读一次完整列表。"""
        try:
            await page.locator('[data-testid="sku-action"], [data-testid="pc-purchase-sku-edit"]').first.click(timeout=2500)
            # Existing summary rows satisfy selector waits before the expanded list renders.
            deadline = time.monotonic() + 3
            more = {}
            while time.monotonic() < deadline:
                more = _sku_lists(BeautifulSoup(await page.content(), _PARSER))
                if any(len(v) > len(dims.get(k, [])) for k, v in more.items()):
                    break
                await asyncio.sleep(0.15)
        except Exception as e:
            print(f"⚠ 变体有“+N”折叠项，展开失败：{type(e).__name__}")
            return dims
        merged = {k: max(dims.get(k, []), v, key=len) for k, v in {**dims, **more}.items()}
        if merged == dims:
            print("⚠ 变体有“+N”折叠项，但展开后没读到更多选项，结果可能不完整")
        return merged

    async def _frames_text(self, page) -> str:
        """淘宝详情：优先 desc_ifr，否则取第一个文字超过 100 字的 frame。"""
        fr = page.frame(name="desc_ifr")
        if fr:
            try:
                t = (await fr.inner_text("body", timeout=3000)).strip()
                if t:
                    return t
            except Exception:
                pass
        for fr in page.frames[:10]:
            try:
                t = (await fr.inner_text("body", timeout=1500)).strip()
                if len(t) > 100:
                    return t
            except Exception:
                pass
        return ""

    async def scrape(self, url: str) -> dict | None:
        plat = _platform(url)
        async with self._sems[plat]:
            # Queued pages must not start a browser request after a desktop stop.
            if self.stop_event is not None and self.stop_event.is_set():
                return None
            if plat == "taobao":
                await asyncio.sleep(random.uniform(*TAOBAO_DELAY))
                if self.stop_event is not None and self.stop_event.is_set():
                    return None
            session = await self._session(plat, _state_file(url))
            result, error, called = None, None, False

            async def setup(page):
                if plat == "alibaba" and BLOCK_ALIBABA_ASSETS:
                    # Scrapling pools tabs; replace our route instead of accumulating handlers.
                    await page.unroute("**/*", _block_assets)
                    await page.route("**/*", _block_assets)

            async def extract(page):
                nonlocal result, error, called
                called = True
                result, error = None, None
                try:
                    result = await self._scrape(page, url, plat)
                except Exception as e:
                    # Scrapling logs callback exceptions, so propagate extraction errors ourselves.
                    error = e

            try:
                await session.fetch(url, page_setup=setup, page_action=extract)
                if error:
                    raise error
                if not called:
                    raise RuntimeError("Scrapling 未执行商品提取回调")
                return result
            except (PWTimeout, TimeoutError):
                print("页面打开超时")
                return None

    async def _scrape(self, page, url: str, plat: str) -> dict | None:
        ali = plat == "alibaba"
        if not ali and not await self._pass_checkpoint(page):
            return None
        final_url, raw_title = page.url or url, await page.title()
        print("当前 URL:", final_url, "| 页面标题:", raw_title)
        if ali:
            for sel, ms in (('[data-module-name="module_price"], [data-testid="pc-purchase-price"]', 10000),
                            (_SKU_ROWS, 3000),
                            (None, 0),
                            ('[data-module-name*="attribute"], [data-module-name*="description"]', 4000)):
                try:
                    if sel:
                        await page.wait_for_selector(sel, timeout=ms)
                    else:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")  # 触发懒加载模块
                except PWTimeout:
                    pass

        html = await page.content()
        if ali:
            d = await asyncio.to_thread(_extract_alibaba, html)
            dims = await self._expand_skus(page, d["dims"]) if d["has_more"] else d["dims"]
            detail = ""
        else:
            d = await asyncio.to_thread(_extract_taobao, html)
            dims, detail = {}, await self._frames_text(page)

        cells = _variant_cells(dims)
        print("变体:", cells or "未读到")
        if ali and not dims:
            _dump_sku(url, html)
        title = clean_page_title(raw_title) or d.get("h1", "")
        print("清洗后标题:", title)
        if not title:
            print("标题为空，抓取失败。")
            return None

        raw = (d["props"] + "\n" + detail).strip()[: 12000 if ali else 6000]
        return {
            "title": title,
            "price": d["price"],
            "description": _filter_generic_text(raw, 3200, 800) if ali else _filter_chinese_text(raw, 2500),
            "description_raw": raw,
            "url_keywords": _extract_url_keywords(final_url),
            "final_url": final_url,
            "page_title_raw": raw_title,
            "variants": cells,
            "variant_dimensions": dims,
        }
