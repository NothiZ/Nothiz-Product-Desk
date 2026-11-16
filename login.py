# -*- coding: utf-8 -*-
"""用法：python login.py taobao|tmall|alibaba（不带参数则询问）。登录态保存到 scraper.STATE_FILES 里对应的文件。"""
import sys
from contextlib import suppress

from scrapling.fetchers import StealthySession

from scraper import BROWSER_OPTIONS, STATE_FILES

_LOGIN_URLS = {  # 天猫登录页会自动跳转到淘宝统一账号登录
    "taobao": "https://login.taobao.com",
    "tmall": "https://login.tmall.com",
    "alibaba": "https://login.alibaba.com",
}


def login(site: str, confirm=None) -> None:
    state = STATE_FILES[site]
    completed = False
    prompt = confirm or input

    def finish_login(page):
        nonlocal completed
        prompt("\n请在浏览器中完成登录（淘宝/天猫推荐手机淘宝扫码），登录成功、页面跳转后回到此处按回车…")
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        page.wait_for_timeout(2000)
        if "login" in page.url and prompt(
            f"\n[警告] 当前 URL 仍含 'login'（{page.url}），可能未完成登录。确认保存？(y/n): "
        ).strip().lower() != "y":
            print("已取消，请重新运行脚本。")
            return
        completed = True

    with StealthySession(headless=False, timeout=60000, **BROWSER_OPTIONS) as session:
        print(f"正在打开 {site} 登录页…")
        session.fetch(_LOGIN_URLS[site], page_action=finish_login)
        if completed:
            session.save_state(state)
            print(f"\n✅ 登录状态已保存至 {state}")
    return completed


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else input(f"登录哪个站点？({'/'.join(STATE_FILES)}): ").strip()
    if site not in STATE_FILES:
        sys.exit(f"未知站点：{site!r}，可选：{', '.join(STATE_FILES)}")
    login(site)
