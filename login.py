# -*- coding: utf-8 -*-
"""登录 Alibaba 或淘宝/天猫；所有登录态统一保存到窗口版用户数据目录。"""
import sys
from contextlib import suppress

from scrapling.fetchers import StealthySession

from scraper import BROWSER_OPTIONS, STATE_FILES
from desktop_runtime import login_state_path

_LOGIN_URLS = {
    "taobao": "https://login.taobao.com",
    "alibaba": "https://login.alibaba.com",
}


def login(site: str, confirm=None) -> bool:
    site = "taobao" if site == "tmall" else site
    sites = ("taobao", "tmall") if site == "taobao" else (site,)
    label = "淘宝 / 天猫" if site == "taobao" else "阿里巴巴"
    completed = False
    prompt = confirm or input

    def finish_login(page):
        nonlocal completed
        prompt(f"\n请在浏览器中完成{label}登录，登录成功、页面跳转后回到此处按回车…")
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        page.wait_for_timeout(2000)
        if "login" in page.url and prompt(
            f"\n[警告] 当前 URL 仍含 'login'（{page.url}），可能未完成登录。确认保存？(y/n): "
        ).strip().lower() != "y":
            print("已取消，请重新运行脚本。")
            return
        completed = True

    def finish_tmall(page):
        nonlocal completed
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        page.wait_for_timeout(2000)
        if "login" in page.url:
            completed = False
            finish_login(page)

    with StealthySession(headless=False, timeout=60000, **BROWSER_OPTIONS) as session:
        print(f"正在打开{label}登录页…")
        session.fetch(_LOGIN_URLS[site], page_action=finish_login)
        if completed and site == "taobao":
            # Reopening Tmall's login portal can prompt again despite shared Taobao auth.
            session.fetch("https://www.tmall.com/", page_action=finish_tmall)
        if completed:
            for selected in sites:
                state = str(login_state_path(STATE_FILES[selected]))
                session.save_state(state)
                print(f"\n✅ 登录状态已保存至 {state}")
    return completed


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else input(f"登录哪个站点？({'/'.join(STATE_FILES)}): ").strip()
    if site not in STATE_FILES:
        sys.exit(f"未知站点：{site!r}，可选：{', '.join(STATE_FILES)}")
    login(site)
