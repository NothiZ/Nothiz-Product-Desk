"""Desktop settings, portable resources and the worker-process protocol."""
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import count
from pathlib import Path
from tempfile import TemporaryFile

APP_TITLE = "产品详情生成"
DEFAULT_PRICE_MULTIPLIER = "1.25"
_PUBLIC_SETTINGS = ("provider", "model", "template", "input_file", "output_file", "resume", "show_browser",
                    "price_multiplier")
IMAGE_MODES = ("常规产品图", "平铺图")
_DATA_DIR_NAME = "ProductDetailsDesktop"
_JOB_LABELS = {"search": "选品搜索", "images": "图片采集", "generate": "详情生成", "login": "平台登录"}


def default_image_settings(mode=IMAGE_MODES[0]):
    return {"input_file": "", "output_dir": "", "mode": mode, "min_clear": 2 if mode == IMAGE_MODES[1] else 4,
            "min_side": 600, "resume": True, "show_browser": True, "save_snapshot": True}


def default_search_settings():
    return {"search": "婴儿奶瓶消毒器", "pages": 2, "limit": 60, "required": "", "excluded": "",
            "category": "", "prefix": "", "show_browser": True}


def default_settings():
    return {"provider": "deepseek", "model": "deepseek-chat", "template": "template2",
            "input_file": "", "output_file": "", "resume": True, "show_browser": True,
            "price_multiplier": DEFAULT_PRICE_MULTIPLIER,
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
    return _configured_user_data_dir() is None


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
    return user_data_dir() / filename


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


@contextmanager
def workbook_write_lock(path):
    with path.with_suffix(".lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OSError("另一窗口正在写入此选品表，请稍后重试。") from error
        yield


def save_workbook_atomic(workbook, path):
    temporary = path.with_suffix(".tmp")
    try:
        workbook.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def parse_price_multiplier(value) -> Decimal:
    text = str(value).strip() or DEFAULT_PRICE_MULTIPLIER
    if re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text):
        multiplier = Decimal(text)
        if multiplier > 0:
            return multiplier
    raise ValueError("上架价格倍率必须是大于 0 的数字，例如 1.25。")


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
    normalized["price_multiplier"] = format(parse_price_multiplier(normalized["price_multiplier"]), "f")
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
    for name in ("search", "required", "excluded", "category", "prefix"):
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
    normalized["show_browser"] = bool(normalized["show_browser"])
    return normalized


def validate_image_settings(settings):
    normalized = {**default_image_settings(settings.get("mode", IMAGE_MODES[0])), **settings}
    if normalized["mode"] not in IMAGE_MODES:
        raise ValueError("请选择有效的图片模式。")
    for key, label, high in (("min_clear", "清晰图张数", 8 if normalized["mode"] == IMAGE_MODES[1] else 5),
                             ("min_side", "图片最短边", 10000)):
        try:
            value = int(str(normalized[key]).strip())
        except ValueError:
            value = 0
        if not 1 <= value <= high:
            raise ValueError(f"{label}须为 1 到 {high} 的整数。")
        normalized[key] = value
    if not str(normalized["input_file"]).strip() or not str(normalized["output_dir"]).strip():
        raise ValueError("请选择选品结果表和图片保存目录。")
    source = Path(normalized["input_file"]).expanduser().resolve()
    output = Path(normalized["output_dir"]).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        raise ValueError("选品结果表不存在，或不是 .xlsx 文件。")
    if output.exists() and not output.is_dir():
        raise ValueError("图片保存位置须为文件夹。")
    normalized.update(input_file=str(source), output_dir=str(output))
    return {key: normalized[key] for key in default_image_settings()}


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
        updates = [{"type": "log", "message": text}
                   for text in self._read_lines("worker.log", "_offset", "_pending") if text]
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
    if kind not in _JOB_LABELS:
        raise ValueError("未知任务类型")
    if kind == "login" and site not in ("alibaba", "taobao"):
        raise ValueError("未知登录站点")
    if kind == "generate":
        settings = validate_generation(settings, api_key)
        public_settings = {key: settings[key] for key in _PUBLIC_SETTINGS}
    elif kind == "search":
        public_settings = validate_candidate_search(settings)
    elif kind == "images":
        public_settings = validate_image_settings(settings)
    else:
        public_settings = {}
    data_dir = user_data_dir()
    jobs = data_dir / "jobs"
    jobs.mkdir(exist_ok=True)
    stem = f"{datetime.now():%Y%m%d_%H%M%S}_{_JOB_LABELS[kind]}"
    for sequence in count(1):
        folder = jobs / (stem if sequence == 1 else f"{stem}_{sequence:02d}")
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        break
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
    if kind == "generate":
        env[f"{settings['provider'].upper()}_API_KEY"] = str(api_key).strip()
    process = subprocess.Popen(command, cwd=str(data_dir), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return RunningTask(folder, process)
