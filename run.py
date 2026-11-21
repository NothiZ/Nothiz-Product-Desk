# -*- coding: utf-8 -*-
"""主入口：抓取（浏览器标签页并发）与 AI 生成（线程池）并行流水线，支持断点续跑。"""
import asyncio
import os
import re
import sys
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

import llm_client
from ai_writer import ContentGenerationError, TitleDedupe, find_review_terms, generate_content
from desktop_runtime import DEFAULT_PRICE_MULTIPLIER, parse_price_multiplier
from scraper import Scraper, validate_product_url

# ═══════════════════════════ 配置 ═══════════════════════════
PROVIDER = "deepseek"            # "deepseek" | "gemini"
DESC_TEMPLATE = "template2"      # "template1"（Key: Value 列表）| "template2"（Details + Specifications）
INPUT_FILE = "products.xlsx"
OUTPUT_FILE = "products_output.xlsx"
PRICE_MULTIPLIER = DEFAULT_PRICE_MULTIPLIER
RESUME = True                    # 已有输出文件时跳过已生成的行；想全部重跑：设为 False 或删除输出文件
SAVE_EVERY = 5                   # 每完成 N 条保存一次
HEADLESS = False                 # False 才能在淘宝弹出滑块时手动验证；纯阿里可设 True
SCRAPE_CONCURRENCY = {"alibaba": 2, "taobao": 1}   # 同时打开的标签页数；淘宝/天猫不建议调大
LLM_WORKERS = 3                  # 同时进行的 AI 请求数（Gemini 免费档另受 llm_client 的限速约束）
# ═════════════════════════════════════════════════════════════

AI_COLUMNS = {"AI中文标题": "title_cn", "AI英文标题": "title_en", "AI中文详情": "desc_cn", "AI英文详情": "desc_en"}
RESULT_COLS = ["采集价格", *AI_COLUMNS, "颜色", "尺寸", "其他规格", "需检查", "失败原因"]
_LISTING_AMOUNT = re.compile(
    r"(US\$|(?:CNY|CN)[¥￥]|[¥￥$€£]|[A-Z]{3})?\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def _detect_url_column(df: pd.DataFrame) -> str:
    for col in df.columns:
        if str(col).strip().lower() in {"链接", "商品链接", "网址", "地址", "url", "link"}:
            return col
    for col in df.columns:  # 兜底：前 10 个非空单元格里有 http 开头的
        if df[col].dropna().astype(str).head(10).str.strip().str.lower().str.startswith(("http://", "https://")).any():
            return col
    raise RuntimeError("未能识别出链接列：请确保有“链接/URL/Link”等列名，或该列内容以 http(s) 开头。")


def _load() -> tuple[pd.DataFrame, str]:
    df = pd.read_excel(INPUT_FILE)
    if "采集状态" in df.columns:
        df = df.loc[df["采集状态"].astype("string").str.strip().eq("已完成").fillna(False)].reset_index(drop=True)
    url_col = _detect_url_column(df)
    for c in RESULT_COLS:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].astype("string").fillna("")  # 避免 pandas 把空列推断成 float
    if RESUME and os.path.exists(OUTPUT_FILE):  # 按链接逐行复用上次结果：增删、改动、调整顺序都不影响
        old = pd.read_excel(OUTPUT_FILE)
        if url_col in old.columns and "AI中文标题" in old.columns:
            keep = old["AI中文标题"].astype("string").fillna("").str.strip() != ""
            if "失败原因" in old.columns:
                keep |= old["失败原因"].astype("string").fillna("").str.strip() != ""
            old = old[keep]
            prev = old.assign(_k=old[url_col].astype(str).str.strip()).drop_duplicates("_k").set_index("_k")
            keys = df[url_col].astype(str).str.strip()
            hit = keys.isin(prev.index)
            for c in (c for c in RESULT_COLS if c in prev.columns):
                df.loc[hit, c] = keys[hit].map(prev[c]).astype("string").fillna("")
            print(f"续跑：载入上次保存的 {int(hit.sum())} 行，失败或不完整的文案会重新处理。")
    # Review notes never decide whether a row failed or should be regenerated.
    for idx in df.index:
        content = {key: df.at[idx, col] for col, key in AI_COLUMNS.items()}
        df.at[idx, "需检查"] = "、".join(find_review_terms(content))
        if not any(value.strip() for value in content.values()) and not df.at[idx, "失败原因"].strip():
            continue
        try:
            validate_product_url(str(df.at[idx, url_col]).strip())
        except ValueError as error:
            df.at[idx, "失败原因"] = str(error)
            continue
        if not all(value.strip() for value in content.values()):
            df.at[idx, "失败原因"] = df.at[idx, "失败原因"] or "标题或详情不完整"
        else:
            df.at[idx, "失败原因"] = ""
    return df, url_col


def _listing_price(price, multiplier: Decimal) -> str:
    parts = re.split(r"\s*(?:[-–—~～]|至)\s*", str(price).strip())
    if len(parts) > 2:
        return ""
    amounts = []
    currencies = set()
    for part in parts:
        match = _LISTING_AMOUNT.fullmatch(part)
        if match is None:
            return ""
        currency, amount = match.groups()
        if currency:
            currencies.add(currency.upper())
        amounts.append(Decimal(amount.replace(",", "")))
    if len(currencies) > 1:
        return ""
    currency = currencies.pop() if currencies else ""
    try:
        return " - ".join(f"{currency}{(amount * multiplier).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"
                          for amount in amounts)
    except DecimalException:
        return ""


def _save(df: pd.DataFrame, final: bool = False) -> str | None:
    if "采集价格" in df.columns:
        multiplier = parse_price_multiplier(PRICE_MULTIPLIER)
        df = df.drop(columns=["上架价格"], errors="ignore")
        df.insert(df.columns.get_loc("采集价格") + 1, "上架价格",
                  df["采集价格"].map(lambda price: _listing_price(price, multiplier)))
    try:
        df.to_excel(OUTPUT_FILE, index=False)
        return OUTPUT_FILE
    except PermissionError:
        print(f"⚠ 无法写入 {OUTPUT_FILE}（被 Excel 占用？）。" + ("" if not final else "已改存备份文件。"))
        if final:
            target = Path(OUTPUT_FILE)
            backup = str(target.with_name(target.stem + "_backup" + target.suffix))
            df.to_excel(backup, index=False)
            return backup
    return None


def _fill_row(df: pd.DataFrame, idx, product: dict, ai: dict) -> None:
    row = {"采集价格": product["price"], **{col: ai[key] for col, key in AI_COLUMNS.items()},
           **{c: product["variants"].get(c, "") for c in ("颜色", "尺寸", "其他规格")},
           "需检查": "、".join(find_review_terms(ai)), "失败原因": ""}
    for col, v in row.items():
        df.at[idx, col] = v


async def main(progress=None, stop_event=None) -> dict:
    llm_client.set_provider(PROVIDER)
    try:
        await asyncio.to_thread(llm_client.call_llm, "只回复 OK")
    except Exception as e:
        sys.exit(f"❌ AI 接口不可用：{e}")
    df, url_col = _load()
    todo = [(i, str(u).strip()) for i, u in df[url_col].items()
            if pd.notna(u) and str(u).strip()
            and (not df.at[i, "AI中文标题"].strip() or df.at[i, "失败原因"].strip())]
    print(f"共 {len(df)} 行，待处理 {len(todo)} 条。")
    accepted = df["失败原因"].eq("")
    dedupe = TitleDedupe(df.loc[accepted, "AI中文标题"], df.loc[accepted, "AI英文标题"])
    llm_sem, done, failed = asyncio.Semaphore(LLM_WORKERS), 0, 0

    def report():
        if progress:
            progress({"type": "progress", "total": len(todo), "done": done, "failed": failed})
        # Failed drafts need the same checkpoints as accepted results.
        processed = done + failed
        if processed and processed % SAVE_EVERY == 0:
            if _save(df):
                print(f"  → 已保存（本次处理 {processed} 条）")

    def stopped():
        return stop_event is not None and stop_event.is_set()

    report()

    async def handle(sc: Scraper, idx, url: str) -> None:
        nonlocal done, failed
        if stopped():
            return
        print(f"\n===== 第 {idx} 行 =====\n链接: {url}")
        try:
            validate_product_url(url)
            product = await sc.scrape(url)
            if not product:
                if not stopped():
                    failed += 1
                    df.at[idx, "失败原因"] = "商品采集失败，未获得可用于生成的商品数据。"
                    report()
                return
            async with llm_sem:  # 抓取不等 AI：这里排队时，浏览器已经在抓下一条
                if stopped():
                    return
                ai = await asyncio.to_thread(generate_content, product, DESC_TEMPLATE, dedupe)
        except Exception as e:
            if isinstance(e, ContentGenerationError):
                draft = e.content if any(e.content.values()) else {key: df.at[idx, col] for col, key in AI_COLUMNS.items()}
                _fill_row(df, idx, product, draft)
            df.at[idx, "失败原因"] = f"{type(e).__name__}: {e}"
            failed += 1
            print(f"第 {idx} 行失败：{type(e).__name__}: {e}")
            report()
            return
        _fill_row(df, idx, product, ai)
        done += 1
        report()

    try:
        async with Scraper(headless=HEADLESS, concurrency=SCRAPE_CONCURRENCY, stop_event=stop_event) as sc:
            await asyncio.gather(*(handle(sc, i, u) for i, u in todo))
    finally:  # 即使 Ctrl+C 中断也保存已完成的部分
        saved_path = _save(df, final=True)
    status = "stopped" if stopped() else "finished"
    print(f"\n{'已停止' if stopped() else '完成'}：成功 {done} 条，失败 {failed} 条，已写入 {saved_path}")
    return {"status": status, "total": len(todo), "done": done, "failed": failed, "output_file": saved_path}


if __name__ == "__main__":
    asyncio.run(main())
