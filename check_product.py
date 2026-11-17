# -*- coding: utf-8 -*-
"""验证单条商品的真实变体；不调用 AI，也不读写批量输出表。"""
import argparse
import asyncio
import json
from pathlib import Path

from scraper import Scraper, validate_product_url


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="商品详情链接")
    parser.add_argument("--headless", action="store_true", help="隐藏浏览器窗口")
    parser.add_argument("--output", default="debug/product_check.json", help="单条采集结果 JSON 路径")
    args = parser.parse_args()
    try:
        validate_product_url(args.url)
    except ValueError as error:
        parser.error(str(error))
    async with Scraper(headless=args.headless) as sc:
        product = await sc.scrape(args.url)
    if not product:
        raise SystemExit("商品采集失败")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(product, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"采集结果：{path}")
    for name, values in product["variant_dimensions"].items():
        print(f"{name}（{len(values)} 项）：{', '.join(values)}")
    if not product["variant_dimensions"]:
        raise SystemExit("未提取到可选变体；请检查 debug/ 下的页面证据")


if __name__ == "__main__":
    asyncio.run(main())
