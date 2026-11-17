"""Desktop settings, portable resources and the worker-process protocol."""
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

APP_TITLE = "产品详情生成"
EVENT_PREFIX = "@@PD_EVENT "
_PUBLIC_SETTINGS = ("provider", "model", "template", "input_file", "output_file", "resume", "show_browser")


def default_settings():
    return {"provider": "deepseek", "model": "deepseek-chat", "template": "template2",
            "input_file": "", "output_file": "", "resume": True, "show_browser": True,
            "remember_key": False, "api_keys": {},
            "models": {"deepseek": "deepseek-chat", "gemini": "gemini-2.5-flash"}}


def user_data_dir():
    override = os.getenv("PRODUCT_DETAILS_DATA_DIR")
    base = Path(override) if override else Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "ProductDetailsDesktop"
    base = base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def resource_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def login_state_path(filename):
    destination = user_data_dir() / filename
    # Explicit worker/test directories must not import unrelated credentials.
    if os.getenv("PRODUCT_DETAILS_DATA_DIR"):
        return destination
    legacy = resource_dir() / filename
    if legacy.resolve() != destination.resolve() and legacy.is_file():
        if not destination.exists():
            shutil.move(str(legacy), str(destination))
        elif legacy.read_bytes() == destination.read_bytes():
            legacy.unlink()
    return destination


def configure_browser_path():
    # A frozen release must use its bundled browser, independent of the developer's cache.
    browsers = resource_dir() / "browsers"
    if not browsers.is_dir() and not getattr(sys, "frozen", False):
        browsers = resource_dir() / "build-resources" / "browsers"
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    return browsers


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_settings():
    settings = default_settings()
    try:
        saved = json.loads((user_data_dir() / "settings.json").read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({k: saved[k] for k in settings if k in saved})
    except (OSError, ValueError):
        pass
    if settings["provider"] not in ("deepseek", "gemini"):
        settings["provider"] = "deepseek"
    if not isinstance(settings.get("models"), dict):
        settings["models"] = default_settings()["models"]
    if not settings.get("remember_key") or not isinstance(settings.get("api_keys"), dict):
        settings["api_keys"] = {}
    return settings


def save_settings(settings):
    saved = {k: settings.get(k, v) for k, v in default_settings().items()}
    if not saved["remember_key"]:
        saved["api_keys"] = {}
    write_json(user_data_dir() / "settings.json", saved)


def validate_generation(settings, api_key):
    normalized = {**default_settings(), **settings}
    if normalized["provider"] not in ("deepseek", "gemini"):
        raise ValueError("请选择 DeepSeek 或 Gemini。")
    if normalized["template"] not in ("template1", "template2"):
        raise ValueError("请选择有效的详情模板。")
    if not str(api_key).strip():
        raise ValueError("请先填写当前服务的 API Key。")
    if not str(normalized.get("model", "")).strip():
        raise ValueError("请填写模型名称。")
    if not str(normalized.get("input_file", "")).strip():
        raise ValueError("请选择输入 Excel 文件。")
    if not str(normalized.get("output_file", "")).strip():
        raise ValueError("请选择输出 Excel 文件。")
    source = Path(normalized["input_file"]).expanduser().resolve()
    output = Path(normalized["output_file"]).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        raise ValueError("输入文件不存在，或不是 .xlsx 文件。")
    if output.suffix.lower() != ".xlsx":
        raise ValueError("输出文件请使用 .xlsx 扩展名。")
    if os.path.normcase(str(source)) == os.path.normcase(str(output)) or (output.exists() and source.samefile(output)):
        raise ValueError("输入和输出不能是同一个文件，请另选输出位置。")
    normalized.update(input_file=str(source), output_file=str(output), model=str(normalized["model"]).strip())
    return normalized


@dataclass
class RunningTask:
    folder: Path
    process: object
    _offset: int = 0
    _pending: bytes = field(default=b"", repr=False)
    _event_offset: int = 0
    _event_pending: bytes = field(default=b"", repr=False)

    @property
    def log_path(self):
        return self.folder / "worker.log"

    @property
    def result_path(self):
        return self.folder / "result.json"

    def poll(self):
        return self.process.poll()

    def request_stop(self):
        (self.folder / "stop").touch()

    def respond(self, prompt_id, answer):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(prompt_id)):
            raise ValueError("无效的确认请求")
        write_json(self.folder / f"response-{prompt_id}.json", {"answer": str(answer)})

    def result(self):
        try:
            result = json.loads(self.result_path.read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    def _read_lines(self, filename, offset_attr, pending_attr):
        try:
            with (self.folder / filename).open("rb") as log:
                log.seek(getattr(self, offset_attr))
                chunk = log.read()
                setattr(self, offset_attr, getattr(self, offset_attr) + len(chunk))
        except FileNotFoundError:
            return []
        lines = (getattr(self, pending_attr) + chunk).split(b"\n")
        pending = lines.pop()
        if self.poll() is not None and pending:
            lines.append(pending)
            pending = b""
        setattr(self, pending_attr, pending)
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]

    def read_updates(self):
        updates = []
        for text in self._read_lines("worker.log", "_offset", "_pending"):
            if text.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(text[len(EVENT_PREFIX):])
                    if isinstance(event, dict) and isinstance(event.get("type"), str):
                        updates.append(event)
                        continue
                except ValueError:
                    pass
            if text:
                updates.append({"type": "log", "message": text})
        # A separate stream keeps progress framing independent of threaded print() calls.
        for text in self._read_lines("events.jsonl", "_event_offset", "_event_pending"):
            try:
                event = json.loads(text)
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    updates.append(event)
            except ValueError:
                updates.append({"type": "log", "message": "忽略了一条不完整的进度记录"})
        return updates


def launch_job(settings, *, kind="generate", api_key="", site="alibaba"):
    if kind not in ("generate", "login"):
        raise ValueError("未知任务类型")
    if site not in ("alibaba", "taobao", "tmall"):
        raise ValueError("未知登录站点")
    if kind == "generate":
        settings = validate_generation(settings, api_key)
    # Migrate before the worker receives its explicit data-directory override.
    for filename in ("alibaba_state.json", "taobao_state.json", "tmall_auth.json"):
        login_state_path(filename)
    folder = user_data_dir() / "jobs" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    task_file = folder / "task.json"
    # Never serialize keys into job files or the command line.
    payload = {"kind": kind, "site": site, "data_dir": str(user_data_dir()),
               "settings": {k: settings.get(k, default_settings()[k]) for k in _PUBLIC_SETTINGS}}
    write_json(task_file, payload)
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).with_name("desktop_app.py")))
    command.extend(["--worker", str(task_file)])
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PRODUCT_DETAILS_DATA_DIR=str(user_data_dir()))
    provider = settings.get("provider", "deepseek")
    if api_key:
        env[f"{provider.upper()}_API_KEY"] = str(api_key).strip()
    process = subprocess.Popen(command, cwd=str(user_data_dir()), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return RunningTask(folder, process)
