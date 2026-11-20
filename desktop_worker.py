"""Hidden worker entry points and offline checks for the frozen desktop release."""
import asyncio
import contextlib
import json
import os
import platform
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from desktop_runtime import configure_browser_path, resource_dir, write_json


async def _generate(settings, folder, emit):
    import run

    run.PROVIDER = settings["provider"]
    run.DESC_TEMPLATE = settings["template"]
    run.INPUT_FILE = settings["input_file"]
    run.OUTPUT_FILE = settings["output_file"]
    run.RESUME = bool(settings["resume"])
    run.HEADLESS = not bool(settings["show_browser"])
    Path(run.OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)
    stop_event = asyncio.Event()
    if (folder / "stop").exists():
        stop_event.set()

    async def watch_stop():
        while not stop_event.is_set():
            if (folder / "stop").exists():
                stop_event.set()
                return
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(watch_stop())
    try:
        return await run.main(progress=emit, stop_event=stop_event)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def run_worker(task_file):
    task_file = Path(task_file).resolve()
    folder = task_file.parent
    folder.mkdir(parents=True, exist_ok=True)
    result = {"type": "finished", "status": "error", "total": 0, "done": 0, "failed": 0,
              "output_file": "", "message": "任务未完成"}
    previous_cwd = Path.cwd()
    previous_data = os.getenv("PRODUCT_DETAILS_DATA_DIR")
    model_env, old_model = None, None
    with (folder / "worker.log").open("a", encoding="utf-8", buffering=1) as log, \
            (folder / "events.jsonl").open("a", encoding="utf-8", buffering=1) as events:
        # Windowed executables have no stdout; redirect before importing API/browser modules.
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            def emit(event):
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
                events.flush()

            def prompt(message):
                prompt_id = uuid.uuid4().hex
                response = folder / f"response-{prompt_id}.json"
                emit({"type": "prompt", "id": prompt_id, "message": message})
                while True:
                    if (folder / "stop").exists():
                        raise KeyboardInterrupt
                    if response.exists():
                        return str(json.loads(response.read_text(encoding="utf-8"))["answer"])
                    time.sleep(0.15)

            try:
                payload = json.loads(task_file.read_text(encoding="utf-8"))
                data_dir = Path(payload["data_dir"]).resolve()
                data_dir.mkdir(parents=True, exist_ok=True)
                os.environ["PRODUCT_DETAILS_DATA_DIR"] = str(data_dir)
                os.chdir(data_dir)
                configure_browser_path()
                if (folder / "stop").exists():
                    raise KeyboardInterrupt
                if payload["kind"] == "login":
                    import login
                    saved = login.login(payload["site"], confirm=prompt)
                    result.update(status="finished", message="登录状态已保存" if saved else "登录已取消，未保存状态")
                elif payload["kind"] == "generate":
                    settings = payload["settings"]
                    model_env = settings["provider"].upper() + "_MODEL"
                    old_model = os.getenv(model_env)
                    os.environ[model_env] = settings["model"]
                    result.update(asyncio.run(_generate(settings, folder, emit)))
                    result["message"] = (f"{'已停止并保存' if result['status'] == 'stopped' else '处理完成'}："
                                         f"成功 {result['done']}，失败 {result['failed']}")
                elif payload["kind"] == "search":
                    from candidate_wall import run_candidate_search

                    result.update(run_candidate_search(payload["settings"], folder, emit, prompt,
                                                       lambda: (folder / "stop").exists()))
                elif payload["kind"] == "images":
                    from image_collection import collect_images

                    result.update(asyncio.run(collect_images(payload["settings"], emit, prompt,
                                                             lambda: (folder / "stop").exists())))
                else:
                    raise ValueError("未知任务类型")
            except KeyboardInterrupt:
                result.update(status="stopped", message="已停止")
            except BaseException as exc:
                result.update(status="error", message=str(exc) or type(exc).__name__)
                traceback.print_exc()
            finally:
                os.chdir(previous_cwd)
                if previous_data is None:
                    os.environ.pop("PRODUCT_DETAILS_DATA_DIR", None)
                else:
                    os.environ["PRODUCT_DETAILS_DATA_DIR"] = previous_data
                if model_env:
                    if old_model is None:
                        os.environ.pop(model_env, None)
                    else:
                        os.environ[model_env] = old_model
                write_json(folder / "result.json", result)
                emit(result)
    return 1 if result["status"] == "error" else 0


async def _check_browser(html):
    from scraper import Scraper

    body = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/alibaba.com/product-detail/selftest_1601683518137.html"
        async with Scraper(headless=True, concurrency={"alibaba": 2}) as sc:
            # Local fixtures exercise the browser and extractor without changing public URL validation.
            session = await sc._session("alibaba", None)

            async def check_page(index):
                result = None
                page_url = url + f"?n={index}"

                async def extract(page):
                    nonlocal result
                    result = await sc._scrape(page, page_url, "alibaba")

                await session.fetch(page_url, page_action=extract)
                return result

            products = await asyncio.gather(check_page(1), check_page(2))
        expected = {"颜色": ["其它", "粉色"]}
        if not all(p and p["variant_dimensions"] == expected for p in products):
            raise AssertionError("离线浏览器采集的变体与样本不一致")
        return {"concurrent_pages": len(products), "variants": expected}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def self_test(report_file):
    """Check the actual frozen GUI, XLSX libraries and bundled browser without external services."""
    report_file = Path(report_file).resolve()
    report_file.parent.mkdir(parents=True, exist_ok=True)
    state_dir = report_file.parent / ("selftest-data-" + uuid.uuid4().hex)
    state_dir.mkdir()
    previous_cwd = Path.cwd()
    os.environ["PRODUCT_DETAILS_DATA_DIR"] = str(state_dir)
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "platform": platform.platform()}
    started = time.monotonic()
    with report_file.with_suffix(".log").open("w", encoding="utf-8", buffering=1) as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                os.chdir(state_dir)
                browsers = configure_browser_path()
                report["browser_path"] = str(browsers)
                if not browsers.is_dir():
                    raise FileNotFoundError("未找到随程序附带的浏览器")
                import certifi
                import ssl
                ssl.create_default_context(cafile=certifi.where())
                report["tls"] = "ok"
                from desktop_app import DesktopApp
                app = DesktopApp()
                app.withdraw()
                app.update_idletasks()
                app.destroy()
                report["gui"] = "ok"
                fixture = resource_dir() / "selftest" / "alibaba_1601683518137.html"
                if not fixture.exists():
                    fixture = Path(__file__).parent / "tests" / "fixtures" / fixture.name
                html = fixture.read_text(encoding="utf-8").replace(
                    "<body>", '<body><div data-module-name="module_price">US$1.00</div>'
                    '<div data-module-name="module_description">Offline product description</div>')
                report["browser"] = asyncio.run(_check_browser(html))
                import pandas as pd
                workbook = state_dir / "output-check.xlsx"
                pd.DataFrame({"颜色": ["其它, 粉色"]}).to_excel(workbook, index=False)
                if pd.read_excel(workbook).loc[0, "颜色"] != "其它, 粉色":
                    raise AssertionError("Excel 读写检查失败")
                report.update(ok=True, excel="ok", external_api_calls=0)
            except BaseException as exc:
                traceback.print_exc()
                report["error"] = str(exc) or type(exc).__name__
            finally:
                os.chdir(previous_cwd)
                report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                write_json(report_file, report)
    return 0 if report["ok"] else 1
