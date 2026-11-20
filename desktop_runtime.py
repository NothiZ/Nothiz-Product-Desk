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
from tempfile import TemporaryFile

APP_TITLE = "产品详情生成"
EVENT_PREFIX = "@@PD_EVENT "
_PUBLIC_SETTINGS = ("provider", "model", "template", "input_file", "output_file", "resume", "show_browser")
IMAGE_MODES = ("常规产品图", "平铺图")
_DATA_DIR_NAME = "ProductDetailsDesktop"


def default_search_settings():
    return {"search": "婴儿奶瓶消毒器", "pages": 2, "limit": 60, "required": "", "excluded": "",
            "category": "", "prefix": "", "image_mode": IMAGE_MODES[0], "show_browser": True}


def default_settings():
    return {"provider": "deepseek", "model": "deepseek-chat", "template": "template2",
            "input_file": "", "output_file": "", "resume": True, "show_browser": True,
            "remember_key": False, "api_keys": {},
            "models": {"deepseek": "deepseek-chat", "gemini": "gemini-2.5-flash"},
            "candidate_search": default_search_settings(), "last_candidate_wall": ""}


def _default_user_data_dir():
    local = Path(os.getenv("LOCALAPPDATA") or Path.home()).expanduser().resolve()
    return local / _DATA_DIR_NAME


def _data_location_file():
    return _default_user_data_dir().with_name(f"{_DATA_DIR_NAME}-location.json")


def _configured_user_data_dir():
    override = os.getenv("PRODUCT_DETAILS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    try:
        saved = json.loads(_data_location_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise ValueError("已保存的数据目录配置无法读取。") from error
    if not isinstance(saved, str) or not Path(saved).is_absolute():
        raise ValueError("已保存的数据目录必须是绝对路径。")
    return Path(saved)


def needs_user_data_setup():
    if _configured_user_data_dir() is not None:
        return False
    legacy = _default_user_data_dir()
    return not legacy.is_dir() or not any(legacy.iterdir())


def configure_user_data_dir(parent):
    parent = Path(parent).expanduser().resolve()
    if not parent.is_dir():
        raise ValueError("请选择一个已有文件夹。")
    destination = parent if parent.name.casefold() == _DATA_DIR_NAME.casefold() else parent / _DATA_DIR_NAME
    destination.mkdir(exist_ok=True)
    with TemporaryFile(dir=destination):
        pass
    write_json(_data_location_file(), str(destination))
    return destination


def user_data_dir():
    base = _configured_user_data_dir() or _default_user_data_dir()
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


def validate_candidate_search(settings):
    normalized = {name: settings.get(name, value) for name, value in default_search_settings().items()}
    for name in ("search", "required", "excluded", "category", "prefix", "image_mode"):
        normalized[name] = str(normalized[name]).strip()
    if not any(term.strip() for term in re.split(r"[,，]", normalized["search"])):
        raise ValueError("请填写搜索关键词。")
    for name, label in (("pages", "搜索页数"), ("limit", "候选上限")):
        try:
            value = int(str(normalized[name]))
        except ValueError:
            value = 0
        if value < 1:
            raise ValueError(f"{label}请填写大于 0 的整数。")
        normalized[name] = value
    prefix = normalized["prefix"]
    if prefix and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,7}", prefix):
        raise ValueError("编号前缀须以英文字母开头，最多 8 个英文字母或数字。")
    normalized["prefix"] = prefix.upper()
    if normalized["image_mode"] not in IMAGE_MODES:
        raise ValueError("请选择有效的图片模式。")
    normalized["show_browser"] = bool(normalized["show_browser"])
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
    if kind not in ("generate", "login", "search"):
        raise ValueError("未知任务类型")
    if site not in ("alibaba", "taobao", "tmall"):
        raise ValueError("未知登录站点")
    if kind == "generate":
        settings = validate_generation(settings, api_key)
    public_settings = (validate_candidate_search(settings) if kind == "search" else
                       {k: settings.get(k, default_settings()[k]) for k in _PUBLIC_SETTINGS})
    # Migrate before the worker receives its explicit data-directory override.
    for filename in ("alibaba_state.json", "taobao_state.json", "tmall_auth.json"):
        login_state_path(filename)
    data_dir = user_data_dir()
    folder = data_dir / "jobs" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    task_file = folder / "task.json"
    # Never serialize keys into job files or the command line.
    payload = {"kind": kind, "site": site, "data_dir": str(data_dir), "settings": public_settings}
    write_json(task_file, payload)
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).with_name("desktop_app.py")))
    command.extend(["--worker", str(task_file)])
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PRODUCT_DETAILS_DATA_DIR=str(data_dir))
    provider = settings.get("provider", "deepseek")
    if kind == "generate" and api_key:
        env[f"{provider.upper()}_API_KEY"] = str(api_key).strip()
    process = subprocess.Popen(command, cwd=str(data_dir), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return RunningTask(folder, process)
