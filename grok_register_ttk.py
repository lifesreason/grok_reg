#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import threading
import datetime
import time
import os
import sys
import queue
import secrets
import struct
import random
import re
import string
import json
import uuid
import subprocess

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:
    tk = None
    ttk = None
    messagebox = None
    scrolledtext = None

try:
    from DrissionPage import Chromium, ChromiumOptions
    from DrissionPage.errors import PageDisconnectedError
except ModuleNotFoundError:
    Chromium = None
    ChromiumOptions = None

    class PageDisconnectedError(Exception):
        pass

try:
    from curl_cffi import requests
except ModuleNotFoundError:
    requests = None


APP_DIR = os.path.dirname(os.path.abspath(__file__))


def get_data_dir():
    data_dir = os.environ.get("GROK_REG_DATA_DIR", APP_DIR)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_config_file():
    return os.path.join(get_data_dir(), "config.json")


CONFIG_FILE = get_config_file()

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "bearer",
    "cloudflare_path_domains": "/domains",
    "cloudflare_path_accounts": "/accounts",
    "cloudflare_path_token": "/token",
    "cloudflare_path_messages": "/messages",
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "show_tutorial_on_start": True,
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
# CloudMail 公开 token 单例（多线程共享，避免并发覆盖）
_cloudmail_public_token = None
_cloudmail_public_token_lock = threading.Lock()


class RegistrationCancelled(Exception):
    pass


def load_config():
    global config
    config_file = get_config_file()
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(get_config_file(), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    proxy = config.get("proxy", "")
    if proxy:
        normalized = normalize_proxy_for_runtime(proxy)
        return {"http": normalized, "https": normalized}
    return {}


def normalize_proxy_for_runtime(proxy):
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    in_docker = str(os.environ.get("GROK_REG_IN_DOCKER", "0")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not in_docker:
        return raw
    return re.sub(r"(?<=://)(127\.0\.0\.1|localhost)(?=[:/]|$)", "host.docker.internal", raw)


def _env_truthy(name, default="0"):
    return str(os.environ.get(name, default)).lower() in {"1", "true", "yes", "on"}


def should_run_headless():
    if _env_truthy("GROK_REG_IN_DOCKER") and not _env_truthy("GROK_REG_ALLOW_HEADLESS"):
        return False
    return _env_truthy("GROK_REG_HEADLESS")


def should_apply_container_chrome_flags():
    return _env_truthy("GROK_REG_IN_DOCKER") or sys.platform.startswith("linux")


def ensure_virtual_display(log_callback=None):
    global _xvfb_process
    if should_run_headless():
        return False
    if not should_apply_container_chrome_flags():
        return False
    if os.environ.get("DISPLAY"):
        return False

    with _xvfb_lock:
        if _xvfb_process is not None and _xvfb_process.poll() is None:
            os.environ["DISPLAY"] = os.environ.get("GROK_REG_DISPLAY", ":99")
            return False

        display = os.environ.get("GROK_REG_DISPLAY", ":99")
        cmd = ["Xvfb", display, "-screen", "0", "1365x900x24", "-nolisten", "tcp"]
        _xvfb_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = display
        if log_callback:
            log_callback(f"[Debug] 已自动启动 Xvfb: DISPLAY={display}")
        time.sleep(0.5)
        return True


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "bearer") or "bearer").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email v1.8.x: POST /api/new_address -> {address,jwt}"""
    global _cf_domain_index
    url = f"{api_base}/api/new_address"
    payload = {}
    try:
        # 在多个域名之间轮换，降低单域偶发不收件导致的失败率
        domains = [x.strip() for x in re.split(r"[,，\s]+", str(config.get("defaultDomains", "") or "")) if x.strip()]
        if domains:
            payload["domain"] = domains[_cf_domain_index % len(domains)]
            _cf_domain_index += 1
    except Exception:
        pass
    resp = http_post(url, json=payload, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare /api/new_address 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare /api/new_address 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return r"D:\注册机\3255d5ee6e702db9220a897df64635a1ec9df644\vendor\grok2api\data\token.json"


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    # 优先使用 add 接口，避免全量覆盖远端池
    try:
        add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
        resp_add = http_post(
            f"{base}/tokens/add",
            headers=headers,
            params=query,
            json=add_payload,
            timeout=30,
            proxies={},
        )
        resp_add.raise_for_status()
        if log_callback:
            log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens/add)")
        return True
    except Exception as add_exc:
        if log_callback:
            log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {add_exc}")

    # 兜底：旧版全量保存接口
    current = {}
    try:
        resp = http_get(f"{base}/tokens", headers=headers, params=query, timeout=20, proxies={})
        if resp.status_code == 200:
            payload = resp.json()
            current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
    except Exception:
        current = {}
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    resp2 = http_post(f"{base}/tokens", headers=headers, params=query, json=current, timeout=30, proxies={})
    resp2.raise_for_status()
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens)")
    return True


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def create_browser_options():
    if ChromiumOptions is None:
        raise RuntimeError("DrissionPage 未安装，无法启动浏览器自动化")
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    browser_path = os.environ.get("CHROME_BIN", "").strip()
    if browser_path:
        options.set_browser_path(browser_path)
    proxy = normalize_proxy_for_runtime(config.get("proxy", ""))
    if proxy:
        options.set_argument("--proxy-server", proxy)
    if should_apply_container_chrome_flags():
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        options.set_argument("--disable-gpu")
        options.set_argument("--window-size", "1365,900")
    if should_run_headless():
        options.headless(True)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    if requests is None:
        raise RuntimeError("curl_cffi 未安装，无法发起 HTTP 请求")
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    if requests is None:
        raise RuntimeError("curl_cffi 未安装，无法发起 HTTP 请求")
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def detect_cloudflare_block_page(page_html):
    html = str(page_html or "").lower()
    return (
        "attention required! | cloudflare" in html
        or "sorry, you have been blocked" in html
        or "cf-error-code" in html
    )


def _parse_positive_int(value, default, minimum=1, maximum=None):
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _normalize_path(value):
    raw = str(value or "").strip()
    if not raw:
        return raw
    return raw if raw.startswith("/") else f"/{raw}"


def validate_registration_config(settings):
    normalized = {**DEFAULT_CONFIG, **dict(settings or {})}
    provider = str(normalized.get("email_provider") or "duckmail").strip() or "duckmail"
    normalized["email_provider"] = provider
    normalized["register_count"] = _parse_positive_int(
        normalized.get("register_count"), 1, minimum=1, maximum=100
    )
    normalized["register_threads"] = _parse_positive_int(
        normalized.get("register_threads"), 1, minimum=1, maximum=10
    )

    raw_paths = normalized.pop("cloudflare_paths", "")
    if raw_paths:
        parts = [x.strip() for x in str(raw_paths).split(",") if x.strip()]
        if len(parts) >= 4:
            normalized["cloudflare_path_domains"] = _normalize_path(parts[0])
            normalized["cloudflare_path_accounts"] = _normalize_path(parts[1])
            normalized["cloudflare_path_token"] = _normalize_path(parts[2])
            normalized["cloudflare_path_messages"] = _normalize_path(parts[3])

    for key in (
        "cloudflare_path_domains",
        "cloudflare_path_accounts",
        "cloudflare_path_token",
        "cloudflare_path_messages",
    ):
        normalized[key] = _normalize_path(normalized.get(key))

    if provider == "cloudflare" and not str(normalized.get("cloudflare_api_base") or "").strip():
        raise ValueError("Cloudflare 模式需要先填写 Cloudflare API Base")
    if provider == "cloudmail":
        if not str(normalized.get("cloudmail_url") or "").strip():
            raise ValueError("CloudMail 模式需要先填写 CloudMail URL")
        if not str(normalized.get("cloudmail_admin_email") or "").strip():
            raise ValueError("CloudMail 模式需要先填写 CloudMail 管理员邮箱")
        if not str(normalized.get("cloudmail_password") or "").strip():
            raise ValueError("CloudMail 模式需要先填写 CloudMail 管理员密码")
    return normalized


class RegistrationJob:
    def __init__(self, settings=None, log_sink=None):
        self.id = uuid.uuid4().hex
        self.settings = validate_registration_config(settings or load_config())
        self.log_sink = log_sink
        self.status_value = "pending"
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.created_at = datetime.datetime.now().isoformat(timespec="seconds")
        self.started_at = None
        self.finished_at = None
        self.output_file = ""
        self.thread = None
        self.stats_lock = threading.Lock()
        self._logs = []
        self._log_lock = threading.Lock()

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._log_lock:
            self._logs.append(line)
        if self.log_sink:
            self.log_sink(message)

    def logs(self, offset=0):
        with self._log_lock:
            if offset < 0:
                offset = 0
            return list(self._logs[offset:])

    def should_stop(self):
        return self.stop_requested or self.status_value not in {"pending", "running"}

    def start(self):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("job is already running")
        self.status_value = "running"
        self.started_at = datetime.datetime.now().isoformat(timespec="seconds")
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_file = f"accounts_{now}_{self.id[:8]}.txt"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def status(self):
        with self.stats_lock:
            success_count = self.success_count
            fail_count = self.fail_count
        return {
            "id": self.id,
            "status": self.status_value,
            "success_count": success_count,
            "fail_count": fail_count,
            "register_count": self.settings.get("register_count", 1),
            "register_threads": self.settings.get("register_threads", 1),
            "stop_requested": self.stop_requested,
            "output_file": self.output_file,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 3
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            email, dev_token = fill_email_and_submit(
                log_callback=logf, cancel_callback=self.should_stop
            )
            logf(f"[*] 邮箱: {email}")
            try:
                with open(
                    os.path.join(get_data_dir(), "mail_credentials.txt"),
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(f"{email}\t{dev_token}\n")
            except Exception:
                pass
            logf("[*] 3. 拉取验证码")
            try:
                code = fill_code_and_submit(
                    email, dev_token, log_callback=logf, cancel_callback=self.should_stop
                )
                mail_ok = True
                break
            except Exception as mail_exc:
                msg = str(mail_exc)
                if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                    logf(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
        if not mail_ok:
            raise Exception("验证码阶段失败，已达到最大重试次数")
        logf(f"[*] 验证码: {code}")
        logf("[*] 4. 填写资料")
        profile = fill_profile_and_submit(
            log_callback=logf, cancel_callback=self.should_stop
        )
        logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
        logf("[*] 5. 等待 sso cookie")
        sso = wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
        with self.stats_lock:
            self.results.append({"email": email, "sso": sso, "profile": profile})
            self.success_count += 1
            line = f"{email}----{profile.get('password','')}----{sso}\n"
            try:
                with open(
                    os.path.join(get_data_dir(), self.output_file),
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        logf(f"[+] 注册成功: {email}")

    def _worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"
        logf = lambda m: self.log(f"{prefix} {m}")
        try:
            start_browser(log_callback=logf)
            logf("[*] 浏览器已启动")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    should_stop_after_task = self.should_stop()
                    if not should_stop_after_task:
                        restart_browser(log_callback=logf)
                        sleep_with_cancel(1, self.should_stop)
                if should_stop_after_task:
                    break
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            stop_browser()

    def _run(self):
        global config
        config = {**DEFAULT_CONFIG, **self.settings}
        count = self.settings["register_count"]
        worker_count = max(1, min(self.settings["register_threads"], count))
        task_queue = queue.Queue()
        for i in range(1, count + 1):
            task_queue.put(i)
        workers = []
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(f"[*] 成功账号将实时保存到: {os.path.join(get_data_dir(), self.output_file)}")
        try:
            start_interval = float(self.settings.get("thread_start_interval", 0.8))
        except Exception:
            start_interval = 0.8
        start_interval = max(0.0, start_interval)

        try:
            for wid in range(1, worker_count + 1):
                if self.stop_requested:
                    break
                worker = threading.Thread(
                    target=self._worker_loop,
                    args=(wid, count, task_queue),
                    daemon=True,
                )
                workers.append(worker)
                worker.start()
                if wid < worker_count and start_interval > 0:
                    sleep_with_cancel(start_interval, self.should_stop)
            for worker in workers:
                worker.join()
            if self.stop_requested:
                self.status_value = "stopped"
            elif self.fail_count and not self.success_count:
                self.status_value = "failed"
            else:
                self.status_value = "completed"
        except RegistrationCancelled:
            self.status_value = "stopped"
        except Exception as exc:
            self.status_value = "failed"
            self.log(f"[!] 任务异常: {exc}")
        finally:
            self.finished_at = datetime.datetime.now().isoformat(timespec="seconds")
            self.log("[*] 任务结束")


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


# ──────────────────────── CloudMail (maillab/cloud-mail) ────────────────────────
# API 前缀: /api/（所有接口均挂载在 /api/ 下）
# 认证格式: Authorization: <token>（不带 Bearer 前缀）
# 公开 token 通过 /api/public/genToken 获取（需管理员账号）

def get_cloudmail_url():
    return str(config.get("cloudmail_url", "") or "").rstrip("/")


def get_cloudmail_password():
    return config.get("cloudmail_password", "")


def get_cloudmail_admin_email():
    return str(config.get("cloudmail_admin_email", "") or "").strip()


def cloudmail_login(url, email, password):
    """POST /api/login -> JWT string"""
    resp = http_post(
        f"{url}/api/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            jwt = token_data.get("token")
            if jwt:
                return jwt
    raise Exception(f"CloudMail 登录失败: {str(data)[:200]}")


def cloudmail_register(url, email, password, turnstile_token=""):
    """POST /api/register -> 注册用户+账号"""
    payload = {"email": email, "password": password}
    if turnstile_token:
        payload["token"] = turnstile_token
    resp = http_post(
        f"{url}/api/register",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") != 200:
        raise Exception(f"CloudMail 注册失败: {data.get('message', str(data))}")
    return data


def cloudmail_gen_public_token(url, admin_email, admin_password):
    """POST /api/public/genToken -> 公开 API token (UUID)"""
    resp = http_post(
        f"{url}/api/public/genToken",
        json={"email": admin_email, "password": admin_password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            return token_data.get("token")
    raise Exception(f"CloudMail 获取公开 token 失败: {str(data)[:200]}")


def cloudmail_public_email_list(url, public_token, to_email="", size=20):
    """POST /api/public/emailList -> 公开邮件查询（需公开 token，Authorization: <token>）"""
    payload = {"size": size}
    if to_email:
        payload["toEmail"] = to_email
    resp = http_post(
        f"{url}/api/public/emailList",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": public_token,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("code") == 200:
            return data.get("data", [])
        raise Exception(f"CloudMail 邮件查询失败: {data.get('message', str(data))}")
    return []


def _cloudmail_get_shared_token(force_refresh=False):
    """获取或刷新共享的公开 token（线程安全单例）"""
    global _cloudmail_public_token
    with _cloudmail_public_token_lock:
        if _cloudmail_public_token and not force_refresh:
            return _cloudmail_public_token
        url = get_cloudmail_url()
        admin_email = get_cloudmail_admin_email()
        admin_password = get_cloudmail_password()
        if not url or not admin_email or not admin_password:
            raise Exception("CloudMail 配置不完整")
        token = cloudmail_gen_public_token(url, admin_email, admin_password)
        if not token:
            raise Exception("CloudMail 公开 token 为空")
        _cloudmail_public_token = token
        return token


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=300,
    poll_interval=5,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    url = get_cloudmail_url()
    if not url:
        raise Exception("CloudMail URL 未配置")
    # 获取共享公开 token（所有线程共用同一个，避免并发覆盖）
    try:
        public_token = _cloudmail_get_shared_token()
    except Exception as exc:
        raise Exception(f"CloudMail 获取公开 token 失败: {exc}")
    if log_callback:
        log_callback("[Debug] CloudMail 公开 token 获取成功")
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 60
    start_time = time.time()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        # 动态轮询间隔：前 30 秒用 2 秒，之后用 5 秒
        elapsed = time.time() - start_time
        current_interval = 2 if elapsed < 30 else poll_interval
        # 用完整邮箱地址查询（公开 API 的 toEmail 需要完整地址）
        try:
            messages = cloudmail_public_email_list(url, public_token, to_email=email, size=20)
        except Exception as exc:
            err_msg = str(exc)
            if log_callback:
                log_callback(f"[Debug] CloudMail 邮件查询失败: {err_msg}")
            # token 失效时，刷新共享 token（加锁，多线程只刷新一次）
            if "token" in err_msg.lower() or "401" in err_msg:
                try:
                    public_token = _cloudmail_get_shared_token(force_refresh=True)
                    if log_callback:
                        log_callback("[Debug] CloudMail 公开 token 已刷新")
                except Exception:
                    pass
            sleep_with_cancel(current_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] CloudMail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = msg.get("emailId") or msg.get("id") or msg.get("messageId")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            # 提取邮件内容（公开接口返回 content 字段，为完整 HTML）
            parts = []
            for field in ("content", "text", "textContent", "text_content", "body", "snippet", "intro"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_val = msg.get("html") or msg.get("htmlContent") or msg.get("html_content")
            if isinstance(html_val, str):
                parts.append(re.sub(r"<[^>]+>", " ", html_val))
            elif isinstance(html_val, list):
                for h in html_val:
                    if isinstance(h, str):
                        parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            if log_callback:
                log_callback(f"[Debug] CloudMail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] CloudMail 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(current_interval, cancel_callback)
    raise Exception(f"CloudMail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── 公共邮箱工具 ────────────────────────

def get_email_provider():
    return config.get("email_provider", "duckmail")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        # CloudMail catch-all 模式：直接生成随机邮箱，无需注册
        # Cloudflare Email Routing 会自动将所有该域名的邮件路由到 Worker
        # 支持英文逗号、中文逗号、空格分隔
        raw = str(config.get("defaultDomains", "") or "")
        domains = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        if not domains:
            raise Exception("CloudMail 需要在 defaultDomains 中配置可用域名")
        global _cf_domain_index
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
        username = generate_username(10)
        address = f"{username}@{domain}"
        # 返回占位 token（实际不用于邮件查询，邮件查询走公开 API）
        return address, "cloudmail_catch_all"
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("鑾峰彇 DuckMail token 澶辫触")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {res.text[:200]}"
            )
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 寮傚父: {e}")
        return False


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 寮傚父: {e}")
        return False


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 寮傚父: {e}")
        return False


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": f"sso={token}; sso-rw={token}; cf_clearance={cf_clearance}",
                }
            )
            if not set_tos_accepted(session, log_callback):
                return False, "set_tos_accepted 澶辫触!"
            if not set_birth_date(session, log_callback):
                return False, "set_birth_date 澶辫触!"
            if not update_nsfw_settings(session, log_callback):
                return False, "update_nsfw_settings 澶辫触!"
            return True, "鎴愬姛寮€鍚疦SFW"
    except Exception as e:
        return False, f"寮傚父: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_thread_ctx = threading.local()
_browser_launch_semaphore = threading.Semaphore(2)
_xvfb_process = None
_xvfb_lock = threading.Lock()


def _get_browser():
    return getattr(_thread_ctx, "browser", None)


def _set_browser(value):
    _thread_ctx.browser = value


def _get_page():
    return getattr(_thread_ctx, "page", None)


def _set_page(value):
    _thread_ctx.page = value


def start_browser(log_callback=None):
    last_exc = None
    for attempt in range(1, 5):
        try:
            # 高并发下限制同时启动浏览器数量，降低 auto_port/user_data 竞争
            with _browser_launch_semaphore:
                ensure_virtual_display(log_callback=log_callback)
                browser = Chromium(create_browser_options())
                tabs = browser.get_tabs()
                page = tabs[-1] if tabs else browser.new_tab()
            _set_browser(browser)
            _set_page(page)
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback:
                proxy = normalize_proxy_for_runtime(config.get("proxy", ""))
                mode = "headless" if should_run_headless() else "visible"
                log_callback(f"[Debug] 浏览器模式: {mode}，代理: {proxy or '直连'}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
                log_callback(
                    "[Debug] 浏览器启动环境: "
                    f"DISPLAY={os.environ.get('DISPLAY', '') or '(empty)'}，"
                    f"CHROME_BIN={os.environ.get('CHROME_BIN', '') or '(empty)'}，"
                    f"模式={'headless' if should_run_headless() else 'visible'}，"
                    f"代理={normalize_proxy_for_runtime(config.get('proxy', '')) or '直连'}"
                )
            try:
                current = _get_browser()
                if current is not None:
                    current.quit(del_data=True)
            except Exception:
                pass
            _set_browser(None)
            _set_page(None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    browser = _get_browser()
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    _set_browser(None)
    _set_page(None)


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def refresh_active_page():
    browser = _get_browser()
    if browser is None:
        browser, _ = restart_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
        _set_page(page)
    except Exception:
        _, page = restart_browser()
    return _get_page()


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    page = _get_page()
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        lower.includes('email')
    );
});
if (!target) {
    return false;
}
target.click();
return true;
        """)

        if clicked:
            if log_callback:
                log_callback("[*] 已点击「使用邮箱注册」按钮")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    page_html = page.html[:500] if page else "no page"
    if log_callback:
        log_callback(f"[Debug] 页面内容片段: {page_html}")
    if detect_cloudflare_block_page(page_html):
        raise Exception("Cloudflare 已拦截当前浏览器环境，请使用 Xvfb 非 headless 模式或更换出口 IP")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = browser.get_tab(0)
        _set_page(page)
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            page = browser.new_tab(SIGNUP_URL)
            _set_page(page)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            browser, _ = restart_browser()
            page = browser.new_tab(SIGNUP_URL)
            _set_page(page)
    page.wait.doc_loaded()
    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    page = refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=15, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("鑾峰彇閭澶辫触")
    if log_callback:
        log_callback(f"[*] 宸插垱寤洪偖绠? {email}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'not-ready';
input.focus(); input.click();
// 清空并设置值
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
// 完整事件序列，确保 React 受控组件同步
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
// 验证：值已写入即可（不依赖 checkValidity，部分站点自定义校验会导致误判）
const current = (input.value || '').trim();
if (current === email) return 'filled';
// 兜底：尝试逐字符输入
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
if ((input.value || '').trim() === email) return 'filled';
return input.value;
            """,
            email,
        )
        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if filled != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !input.checkValidity() || !(input.value || '').trim()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next')
    );
});
if (!submitButton || submitButton.disabled) return false;
submitButton.click();
return true;
            """
        )
        if clicked:
            if log_callback:
                log_callback(f"[*] 已填写邮箱并点击注册: {email}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = _get_page()
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    page = _get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        sleep_with_cancel(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    page = _get_page()
    given_name, family_name, password = build_profile()
    # 预热 Turnstile：等 2 秒让 iframe 初始化，插件会自动点击 checkbox
    # 填资料期间 Turnstile 在后台求解，填完时大概率已通过
    if log_callback:
        log_callback("[*] 预热 Turnstile...")
    sleep_with_cancel(2, cancel_callback)
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});
if (!submitBtn) return 'no-submit-button';
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if submit_state == "no-submit-button" and log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            page = _get_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    return t.includes('完成注册');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});
if (!submitBtn) return 'final-page-no-submit';
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and retried in ("final-page-no-submit", "final-page-clicked-submit"):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


class GrokRegisterGUI:
    def __init__(self, root):
        if tk is None:
            raise RuntimeError("当前 Python 未安装 Tkinter，无法启动桌面 GUI；请使用 web_app.py")
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("980x860")
        self.root.minsize(900, 760)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.stats_lock = threading.Lock()
        self._tutorial_window = None
        self.current_job = None
        self.setup_ui()
        self.root.after(200, self._maybe_show_tutorial_on_start)

    def setup_ui(self):
        load_config()
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_frame = ttk.LabelFrame(main_frame, text="配置", padding=10)
        config_frame.pack(fill=tk.X, pady=5)
        ttk.Label(config_frame, text="邮箱服务商:").grid(row=0, column=0, sticky=tk.W)
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = ttk.Combobox(config_frame, textvariable=self.email_provider_var, values=["duckmail", "yyds", "cloudflare", "cloudmail"], width=12, state="readonly")
        self.email_provider_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="注册数量:").grid(row=0, column=2, sticky=tk.W, padx=10)
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = ttk.Spinbox(config_frame, from_=1, to=100, width=8, textvariable=self.count_var)
        self.count_spinbox.grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="并发线程:").grid(row=1, column=2, sticky=tk.W, padx=10)
        self.thread_var = tk.StringVar(value=str(config.get("register_threads", 1)))
        self.thread_spinbox = ttk.Spinbox(config_frame, from_=1, to=10, width=8, textvariable=self.thread_var)
        self.thread_spinbox.grid(row=1, column=3, sticky=tk.W, padx=5)
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = ttk.Checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        self.nsfw_check.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(config_frame, text="代理（可选）:").grid(row=2, column=0, sticky=tk.W)
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = ttk.Entry(config_frame, textvariable=self.proxy_var, width=30)
        self.proxy_entry.grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="DuckMail API Key:").grid(row=3, column=0, sticky=tk.W)
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=30)
        self.api_key_entry.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Base:").grid(row=4, column=0, sticky=tk.W)
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_base_var, width=30)
        self.cloudflare_api_base_entry.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Key:").grid(row=5, column=0, sticky=tk.W)
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_key_var, width=30)
        self.cloudflare_api_key_entry.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare 鉴权模式:").grid(row=6, column=0, sticky=tk.W)
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "bearer"))
        self.cloudflare_auth_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cloudflare_auth_mode_var,
            values=["query-key", "bearer", "x-api-key", "none"],
            width=12,
            state="readonly",
        )
        self.cloudflare_auth_mode_combo.grid(row=6, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CF 路径(domains/accounts/token/messages):").grid(row=7, column=0, sticky=tk.W)
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/domains"),
                    config.get("cloudflare_path_accounts", "/accounts"),
                    config.get("cloudflare_path_token", "/token"),
                    config.get("cloudflare_path_messages", "/messages"),
                ]
            )
        )
        self.cloudflare_paths_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_paths_var, width=30)
        self.cloudflare_paths_entry.grid(row=7, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail URL:").grid(row=8, column=0, sticky=tk.W)
        self.cloudmail_url_var = tk.StringVar(value=str(config.get("cloudmail_url", "")))
        self.cloudmail_url_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_url_var, width=30)
        self.cloudmail_url_entry.grid(row=8, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员邮箱:").grid(row=9, column=0, sticky=tk.W)
        self.cloudmail_admin_email_var = tk.StringVar(value=str(config.get("cloudmail_admin_email", "")))
        self.cloudmail_admin_email_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_admin_email_var, width=30)
        self.cloudmail_admin_email_entry.grid(row=9, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员密码:").grid(row=10, column=0, sticky=tk.W)
        self.cloudmail_password_var = tk.StringVar(value=str(config.get("cloudmail_password", "")))
        self.cloudmail_password_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_password_var, width=30, show="*")
        self.cloudmail_password_entry.grid(row=10, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地自动入池:").grid(row=11, column=0, sticky=tk.W)
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        self.grok2api_local_auto_check.grid(row=11, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地 token.json:").grid(row=12, column=0, sticky=tk.W)
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = ttk.Entry(config_frame, textvariable=self.grok2api_local_file_var, width=30)
        self.grok2api_local_file_entry.grid(row=12, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 池名:").grid(row=13, column=0, sticky=tk.W)
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = ttk.Combobox(
            config_frame,
            textvariable=self.grok2api_pool_name_var,
            values=["ssoBasic", "ssoSuper"],
            width=12,
            state="readonly",
        )
        self.grok2api_pool_name_combo.grid(row=13, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端自动入池:").grid(row=14, column=0, sticky=tk.W)
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        self.grok2api_remote_auto_check.grid(row=14, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 Base:").grid(row=15, column=0, sticky=tk.W)
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_base_var, width=30)
        self.grok2api_remote_base_entry.grid(row=15, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 app_key:").grid(row=16, column=0, sticky=tk.W)
        self.grok2api_remote_key_var = tk.StringVar(value=str(config.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_key_var, width=30)
        self.grok2api_remote_key_entry.grid(row=16, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="默认域名(defaultDomains):").grid(row=17, column=0, sticky=tk.W)
        self.default_domains_var = tk.StringVar(value=str(config.get("defaultDomains", "")))
        self.default_domains_entry = ttk.Entry(config_frame, textvariable=self.default_domains_var, width=30)
        self.default_domains_entry.grid(row=17, column=1, columnspan=3, sticky=tk.W, padx=5)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        self.help_btn = ttk.Button(btn_frame, text="教程", command=self.show_tutorial)
        self.help_btn.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=60)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        # 仅当用户当前就在底部时自动跟随，避免手动上滑后被强制拉回底部
        yview = self.log_text.yview()
        at_bottom = bool(yview) and yview[1] >= 0.999
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        if at_bottom:
            self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def _maybe_show_tutorial_on_start(self):
        if bool(config.get("show_tutorial_on_start", True)):
            self.show_tutorial()

    def _tutorial_text(self):
        return """欢迎使用 Grok 注册机。建议按下面顺序填写（从最关键到可选）：

【第一步：先确定邮箱后端信息从哪里来】
如果你使用 cloudflare 模式（你当前主要是这套），先去你的临时邮箱服务配置接口查信息：
- 常见接口: /open_api/settings、/api/settings、/health_check
- 重点字段:
  - api_base（对应本工具的 Cloudflare API Base）
  - domains / defaultDomains（可用域名）
  - needAuth（是否需要鉴权）
  - admin_password 或 api_key（需要鉴权时使用）
  - provider.type（应为 cloudflare_temp_email）

【第二步：先填最小可运行配置】
1) 邮箱服务商
- duckmail: 需要 DuckMail API Key
- yyds: 需要 YYDS API Key 或 JWT
- cloudflare: 需要 Cloudflare API Base（cloudflare_temp_email 临时邮箱）
- cloudmail: 需要 CloudMail URL + 密码 + defaultDomains（maillab/cloud-mail 完整邮箱）

2) Cloudflare API Base（cloudflare 模式必填）
- 示例: https://xxxx.pages.dev
- 填写规则: 与 settings 接口中的 api_base 保持一致

3) 默认域名(defaultDomains)
- 填写你要优先使用的域名
- 支持单域名或逗号分隔多域名轮换
- 示例: a.com,b.com

4) CF 路径(domains/accounts/token/messages)
- 必须与后端真实路由一致
- 常见新路径:
  - /api/domains,/api/new_address,/api/token,/api/mails
- 常见旧路径:
  - /domains,/accounts,/token,/messages

5) Cloudflare API Key / 鉴权模式
- needAuth=false: 通常鉴权模式选 none，key 可留空
- needAuth=true: 按后端要求填 key，并选择 bearer/x-api-key/query-key

6) CloudMail 模式配置（maillab/cloud-mail 部署）
- CloudMail URL: 你的 Worker 地址，如 https://mail.xxx.workers.dev
- CloudMail 管理员邮箱: 管理员账号，如 admin@yourdomain.com
- CloudMail 管理员密码: 管理员密码（用于获取公开 API token 查询邮件）
- defaultDomains: 必须填写可用域名，如 yourdomain.com
- 前提: CloudMail 管理面板需关闭注册验证码（Turnstile），或确保注册接口可用
- 邮件获取: 通过 /api/public/emailList 公开接口查询，自动刷新 token

【第三步：并发与稳定性】
6) 注册数量
- 本次要注册的总账号数

7) 并发线程
- 建议先 3-6 稳定后再升到 10

8) 代理（可选）
- 不填=直连
- 示例: http://127.0.0.1:7890
- 代理不稳会影响验证码和注册稳定性

9) 注册后开启 NSFW
- 勾选后成功账号会自动调用接口开启对应设置

【第四步：grok2api 入池（可选）】
10) grok2api 本地自动入池
- 开启后把成功 sso 自动写入本地池
- 本地 token.json 填 grok2api 的 token.json 路径

11) grok2api 池名
- ssoBasic 或 ssoSuper

12) grok2api 远端自动入池
- 开启后调用远端管理接口自动加 token
- 远端 Base 示例: https://xxx/admin/api
- app_key 按远端服务配置填写

【最后：快速自检】
1) 先设置: 注册数量=1，并发线程=1
2) 点开始后看日志是否出现：
- 已创建邮箱: xxx@你的域名
- Cloudflare/CloudMail 本轮邮件数量: ...
- 从邮件中提取到验证码: ...
3) 若第一步就失败：
- cloudflare 模式: 检查 API Base / CF 路径 / 鉴权模式
- cloudmail 模式: 检查 URL / 密码 / defaultDomains / 注册接口是否可用

提示:
- 点“开始注册”会自动保存当前配置到 config.json。
- 如果关闭了启动教程，可随时点主界面的“教程”按钮重新打开。"""

    def show_tutorial(self):
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._tutorial_window = win
        win.title("使用教程")
        win.geometry("760x620")
        win.minsize(680, 520)
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=26)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", self._tutorial_text())
        txt.config(state=tk.DISABLED)

        footer = ttk.Frame(frame)
        footer.pack(fill=tk.X, pady=(8, 0))

        dont_show_var = tk.BooleanVar(value=not bool(config.get("show_tutorial_on_start", True)))
        chk = ttk.Checkbutton(
            footer,
            text="以后不再自动显示本教程",
            variable=dont_show_var,
        )
        chk.pack(side=tk.LEFT)

        def on_close():
            config["show_tutorial_on_start"] = not bool(dont_show_var.get())
            save_config()
            try:
                win.destroy()
            except Exception:
                pass

        close_btn = ttk.Button(footer, text="关闭", command=on_close)
        close_btn.pack(side=tk.RIGHT, padx=5)
        win.protocol("WM_DELETE_WINDOW", on_close)

    def should_stop(self):
        if self.current_job is not None:
            return self.current_job.should_stop()
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        settings = {
            "email_provider": self.email_provider_var.get().strip() or "duckmail",
            "proxy": self.proxy_var.get().strip(),
            "duckmail_api_key": self.api_key_var.get().strip(),
            "cloudflare_api_base": self.cloudflare_api_base_var.get().strip(),
            "cloudflare_api_key": self.cloudflare_api_key_var.get().strip(),
            "cloudflare_auth_mode": self.cloudflare_auth_mode_var.get().strip() or "bearer",
            "cloudmail_url": self.cloudmail_url_var.get().strip(),
            "cloudmail_admin_email": self.cloudmail_admin_email_var.get().strip(),
            "cloudmail_password": self.cloudmail_password_var.get().strip(),
            "grok2api_auto_add_local": bool(self.grok2api_local_auto_var.get()),
            "grok2api_local_token_file": self.grok2api_local_file_var.get().strip(),
            "grok2api_pool_name": self.grok2api_pool_name_var.get().strip() or "ssoBasic",
            "grok2api_auto_add_remote": bool(self.grok2api_remote_auto_var.get()),
            "grok2api_remote_base": self.grok2api_remote_base_var.get().strip(),
            "grok2api_remote_app_key": self.grok2api_remote_key_var.get().strip(),
            "defaultDomains": self.default_domains_var.get().strip(),
            "register_count": self.count_var.get(),
            "register_threads": self.thread_var.get(),
            "cloudflare_paths": self.cloudflare_paths_var.get(),
            "enable_nsfw": bool(self.nsfw_var.get()),
        }
        try:
            validated = validate_registration_config(settings)
        except ValueError as exc:
            self.log(f"[!] {exc}")
            return
        config.update(validated)
        save_config()
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.update_stats()
        self._set_running_ui(True)
        self.current_job = RegistrationJob(validated, log_sink=self.log)
        self.accounts_output_file = self.current_job.output_file
        self.current_job.start()
        threading.Thread(
            target=self._watch_job,
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        if self.current_job is not None:
            self.current_job.stop()
        self.log("[!] 用户停止注册")

    def _watch_job(self):
        if self.current_job is not None and self.current_job.thread is not None:
            self.current_job.thread.join()
            status = self.current_job.status()
            self.success_count = status["success_count"]
            self.fail_count = status["fail_count"]
            self.results = list(self.current_job.results)
            self.accounts_output_file = status["output_file"]
            self.update_stats()
        self._set_running_ui(False)

def main():
    root = tk.Tk()
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
