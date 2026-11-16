# -*- coding: utf-8 -*-
"""LLM 调用层：DeepSeek / Gemini。Key 只从环境变量或同目录 .env 读取，代码里不放密钥。"""
import os
import re
import threading
import time

import requests


def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()

# rpm：每分钟最多请求数（0 = 不限）。免费额度因账号/模型而异，请按实际调整，或用环境变量 LLM_RPM 覆盖。
_PROVIDERS = {
    "deepseek": {"key_env": "DEEPSEEK_API_KEY", "model_env": "DEEPSEEK_MODEL", "model": "deepseek-chat", "rpm": 0},
    "gemini":   {"key_env": "GEMINI_API_KEY",   "model_env": "GEMINI_MODEL",   "model": "gemini-2.5-flash", "rpm": 10},
}
_active = "deepseek"
_tls = threading.local()
_slot_lock = threading.Lock()
_next_slot = 0.0


def set_provider(name: str) -> None:
    global _active
    name = name.strip().lower()
    if name not in _PROVIDERS:
        raise ValueError(f"未知 provider: {name!r}，可选：{list(_PROVIDERS)}")
    _active = name
    print(f"[llm] provider = {name}")


def _throttle(rpm: int) -> None:
    """线程安全的最小间隔限速：先在锁内预约时间片，再在锁外睡眠。"""
    global _next_slot
    if rpm <= 0:
        return
    with _slot_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + 60.0 / rpm
    time.sleep(max(0.0, slot - now))


def _post(url: str, headers: dict, body: dict) -> dict:
    """POST + 重试：429/5xx/网络错误指数退避，其余 HTTP 错误直接抛出。"""
    sess = getattr(_tls, "s", None) or requests.Session()
    _tls.s = sess
    last = None
    for attempt in range(4):
        wait = 5.0 * 2 ** attempt
        try:
            r = sess.post(url, headers=headers, json=body, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("Retry-After") or wait)
                except ValueError:
                    pass
            else:
                r.raise_for_status()
                return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            detail = str(e)
            # Retain transport diagnostics without exposing API or proxy credentials.
            for name, value in headers.items():
                if name.lower() in ("authorization", "x-goog-api-key"):
                    detail = detail.replace(value, "[redacted]")
                    if value.startswith("Bearer "):
                        detail = detail.replace(value[7:], "[redacted]")
            detail = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/?#]*@", r"\1[redacted]@", detail, flags=re.I)
            last = f"{type(e).__name__}: {detail[:800]}"
        if attempt == 3:
            break
        print(f"[llm] {last}，{wait:.0f}s 后重试（{attempt + 1}/4）")
        time.sleep(wait)
    raise RuntimeError(f"[llm] 重试 4 次仍失败：{last}")


def _deepseek(prompt: str, temperature: float, key: str, model: str) -> str:
    data = _post(
        "https://api.deepseek.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature},
    )
    return data["choices"][0]["message"]["content"].strip()


def _gemini(prompt: str, temperature: float, key: str, model: str) -> str:
    cfg = {"temperature": temperature, "maxOutputTokens": 4096}
    if "2.5-flash" in model:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}  # 关闭“思考”，更快更省，也避免思考占用输出额度
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},  # 放请求头，Key 不会出现在 URL / 日志里
        {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
    )
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except (KeyError, IndexError) as e:
        raise ValueError(f"[llm] Gemini 无有效输出：{str(data)[:300]}") from e


def call_llm(prompt: str, temperature: float = 0.2) -> str:
    cfg = _PROVIDERS[_active]
    key = os.getenv(cfg["key_env"])
    if not key:
        raise RuntimeError(f"未设置 {cfg['key_env']}：请写入环境变量，或复制 .env.example 为 .env 后填写。")
    _throttle(int(os.getenv("LLM_RPM", cfg["rpm"])))
    fn = _deepseek if _active == "deepseek" else _gemini
    try:
        return fn(prompt, temperature, key, os.getenv(cfg["model_env"]) or cfg["model"])
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code in (401, 403):
            raise RuntimeError(f"{_active} 拒绝了 {cfg['key_env']}（HTTP {code}）：请确认 Key 填在了对应的那一行、没过期；"
                               "系统环境变量里的同名变量会优先于 .env") from None
        raise
