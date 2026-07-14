#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import threading
import datetime
import time
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import queue
import secrets
import struct
import random
import re
import string
import tempfile
import json
import uuid
import subprocess
import hashlib
import base64
import urllib.parse

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
    from curl_cffi import CurlMime, requests
except ModuleNotFoundError:
    CurlMime = None
    requests = None


APP_DIR = os.path.dirname(os.path.abspath(__file__))


def get_data_dir():
    data_dir = os.environ.get("GROK_REG_DATA_DIR", APP_DIR)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_config_file():
    return os.path.join(get_data_dir(), "config.json")


def get_account_status_file():
    return os.path.join(get_data_dir(), "account_status.json")


def get_rejected_email_domains_file():
    return os.path.join(get_data_dir(), "rejected_email_domains.json")


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
    # 注册成功后立刻改 NSFW/生日等特征，容易被当成机器号；默认关闭，需要时再开。
    "enable_nsfw": False,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    "sub2api_auto_import_remote": False,
    "sub2api_base": "",
    "sub2api_auth_mode": "x-api-key",
    "sub2api_admin_token": "",
    "sub2api_account_name": "Grok Auto",
    "sub2api_group_ids": "",
    "sub2api_concurrency": 3,
    "sub2api_priority": 50,
    "cpa_auth_dir": "cpa_auths",
    "cpa_auto_push_remote": False,
    "cpa_management_base": "",
    "cpa_management_key": "",
    "cpa_push_workers": 3,
    "register_threads": 1,
    # 线程启动错开，避免同一秒内多开浏览器打到同一出口。
    "thread_start_interval": 2.0,
    # 同线程相邻账号间隔 + 抖动，降低“工厂流水线”节奏。
    "account_interval_seconds": 12,
    "account_interval_jitter_seconds": 8,
    # 连续检测到账号封禁时熔断，避免同 IP/代理继续批量送死。
    "stop_on_consecutive_blocks": 3,
    # Docker 下默认不要补丁 window.turnstile：手动能过时，API 补丁反而会干扰 flexible 模式。
    "turnstile_patch_api": False,
    # 默认不强制 execute；先完全交给 Cloudflare 被动评分。
    "turnstile_force_execute": False,
    # 资料页等 token 的最长时间（秒）
    "turnstile_wait_seconds": 120,
    "show_tutorial_on_start": True,
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
}

XAI_GROK_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_GROK_OAUTH_AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
XAI_GROK_OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_GROK_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_GROK_OAUTH_REDIRECT_URI = "http://127.0.0.1:56121/callback"
XAI_GROK_API_BASE_URL = "https://api.x.ai/v1"
CPA_DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_CLIENT_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_yyds_domain_index = 0
_rejected_email_domains = set()
_rejected_email_domains_lock = threading.Lock()
_registered_accounts_lock = threading.Lock()
# CloudMail 公开 token 单例（多线程共享，避免并发覆盖）
_cloudmail_public_token = None
_cloudmail_public_token_lock = threading.Lock()


class RegistrationCancelled(Exception):
    pass


class EmailDomainRejected(Exception):
    def __init__(self, domain):
        self.domain = str(domain or "").strip().lower()
        super().__init__(f"邮箱域名被 x.ai 拒收: {self.domain or 'unknown'}")


class EmailProviderUnavailable(Exception):
    pass


class ProfileSessionLost(Exception):
    pass


def load_config():
    global config
    config = DEFAULT_CONFIG.copy()
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
TURNSTILE_PAGE_HOOK_PATH = os.path.join(EXTENSION_PATH, "pageHook.js")
_turnstile_page_hook_source_cache = None


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
        # 与浏览器窗口一致，避免虚拟屏过小触发异常布局/指纹。
        cmd = ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"]
        _xvfb_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = display
        if log_callback:
            log_callback(f"[Debug] 已自动启动 Xvfb: DISPLAY={display} (1920x1080)")
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
    if os.name != "nt":
        return ""
    return r"D:\注册机\3255d5ee6e702db9220a897df64635a1ec9df644\vendor\grok2api\data\token.json"


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def _mask_token(token, head=6, tail=6):
    value = str(token or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def _account_id(source, line_no, email, sso):
    seed = f"{source}:{line_no}:{email}:{_normalize_sso_token(sso)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def parse_registered_account_line(line, source="", line_no=0, include_sso=True):
    parts = str(line or "").rstrip("\n").split("----", 3)
    if len(parts) not in {3, 4}:
        return None
    email, password, sso = [part.strip() for part in parts[:3]]
    refresh_token = parts[3].strip() if len(parts) == 4 else ""
    sso = _normalize_sso_token(sso)
    if not email or not sso:
        return None
    account = {
        "id": _account_id(source, line_no, email, sso),
        "email": email,
        "password": password,
        "sso_preview": _mask_token(sso),
        "refresh_token_preview": _mask_token(refresh_token) if refresh_token else "",
        "has_refresh_token": bool(refresh_token),
        "source_file": source,
        "line_no": line_no,
    }
    if include_sso:
        account["sso"] = sso
        if refresh_token:
            account["refresh_token"] = refresh_token
    return account


def load_account_statuses():
    path = get_account_status_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else data
    return accounts if isinstance(accounts, dict) else {}


def save_account_statuses(statuses):
    path = get_account_status_file()
    payload = {
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "accounts": statuses if isinstance(statuses, dict) else {},
    }
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def account_status_text(status):
    value = str(status or "").strip().lower()
    if value == "pushed":
        return "已推送"
    if value == "failed":
        return "推送失败"
    if value == "pushing":
        return "推送中"
    return "未推送"


def account_health_status_text(status):
    value = str(status or "").strip().lower()
    if value == "healthy":
        return "可用"
    if value == "unhealthy":
        return "失效"
    if value == "incomplete":
        return "资料不完整"
    if value == "checking":
        return "检查中"
    return "未检查"


def _sub2api_error_text(exc, step=""):
    response = getattr(exc, "response", None)
    status_code = (
        getattr(response, "status_code", None)
        or getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
    )
    text = getattr(response, "text", "") if response is not None else ""
    if not text:
        reader = getattr(exc, "read", None)
        if callable(reader):
            try:
                body = reader()
                text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
            except Exception:
                text = ""
    message = str(exc)
    if status_code:
        message = f"{step + ' ' if step else ''}HTTP {status_code}: {text or message}"
    elif step:
        message = f"{step}: {message}"
    return message[:1000]


def is_refresh_token_revoked_error(error_text):
    text = str(error_text or "").lower()
    return "invalid_grant" in text or "revoked" in text or "refresh token has been revoked" in text


def is_account_blocked_error(error_text):
    text = str(error_text or "").lower()
    return (
        "user account is blocked" in text
        or "account is blocked" in text
        or "account has been blocked" in text
        or "账号已封禁" in text
        or "账号被封" in text
    )


def is_xai_refresh_token_client_error(exc):
    response = getattr(exc, "response", None)
    status_code = (
        getattr(response, "status_code", None)
        or getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
    )
    text = _sub2api_error_text(exc).lower()
    return str(status_code) == "400" or "http 400" in text or "http error 400" in text or is_refresh_token_revoked_error(text)


def attach_account_status(account, statuses=None):
    if not isinstance(account, dict):
        return account
    statuses = load_account_statuses() if statuses is None else statuses
    record = statuses.get(str(account.get("id") or ""), {})
    if not isinstance(record, dict):
        record = {}
    status = str(record.get("sub2api_status") or record.get("status") or "not_pushed").strip() or "not_pushed"
    account["sub2api_status"] = status
    account["sub2api_status_text"] = str(record.get("sub2api_status_text") or account_status_text(status))
    if record.get("sub2api_pushed_at"):
        account["sub2api_pushed_at"] = record.get("sub2api_pushed_at")
    if "sub2api_response" in record:
        account["sub2api_response"] = record.get("sub2api_response")
    if record.get("sub2api_error"):
        account["sub2api_error"] = record.get("sub2api_error")
    grok2api_status = str(record.get("grok2api_status") or "not_pushed").strip() or "not_pushed"
    account["grok2api_status"] = grok2api_status
    account["grok2api_status_text"] = str(record.get("grok2api_status_text") or account_status_text(grok2api_status))
    if record.get("grok2api_pushed_at"):
        account["grok2api_pushed_at"] = record.get("grok2api_pushed_at")
    if "grok2api_response" in record:
        account["grok2api_response"] = record.get("grok2api_response")
    if record.get("grok2api_error"):
        account["grok2api_error"] = record.get("grok2api_error")
    cpa_status = str(record.get("cpa_status") or "not_pushed").strip() or "not_pushed"
    account["cpa_status"] = cpa_status
    account["cpa_status_text"] = str(record.get("cpa_status_text") or account_status_text(cpa_status))
    if record.get("cpa_pushed_at"):
        account["cpa_pushed_at"] = record.get("cpa_pushed_at")
    if "cpa_response" in record:
        account["cpa_response"] = record.get("cpa_response")
    if record.get("cpa_error"):
        account["cpa_error"] = record.get("cpa_error")
    health_status = str(record.get("health_status") or "unknown").strip() or "unknown"
    account["health_status"] = health_status
    account["health_status_text"] = str(record.get("health_status_text") or account_health_status_text(health_status))
    if record.get("health_checked_at"):
        account["health_checked_at"] = record.get("health_checked_at")
    if record.get("health_error"):
        account["health_error"] = record.get("health_error")
    if "health_response" in record:
        account["health_response"] = record.get("health_response")
    return account


def list_registered_accounts(include_sso=True):
    data_dir = get_data_dir()
    statuses = load_account_statuses()
    accounts = []
    for name in sorted(os.listdir(data_dir), reverse=True):
        if not (name.startswith("accounts_") and name.endswith(".txt")):
            continue
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    account = parse_registered_account_line(
                        line, source=name, line_no=line_no, include_sso=include_sso
                    )
                    if account:
                        attach_account_status(account, statuses)
                        accounts.append(account)
        except Exception:
            continue
    return accounts


def replace_registered_account_refresh_token(account, refresh_token):
    refresh_token = str(refresh_token or "").strip()
    source = str((account or {}).get("source_file") or "").strip()
    line_no = int((account or {}).get("line_no") or 0)
    if not refresh_token or not source or line_no <= 0:
        return False
    path = os.path.join(get_data_dir(), source)
    if not os.path.isfile(path):
        return False
    try:
        with _registered_accounts_lock:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if line_no > len(lines):
                return False
            parts = lines[line_no - 1].rstrip("\n").split("----", 3)
            if len(parts) < 3:
                return False
            newline = "\n" if lines[line_no - 1].endswith("\n") else ""
            lines[line_no - 1] = f"{parts[0]}----{parts[1]}----{parts[2]}----{refresh_token}{newline}"
            _write_registered_account_lines(path, lines)
        account["refresh_token"] = refresh_token
        account["refresh_token_preview"] = _mask_token(refresh_token)
        account["has_refresh_token"] = True
        return True
    except Exception:
        return False


def persist_sub2api_push_status(accounts, result):
    statuses = load_account_statuses()
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        items = []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for index, account in enumerate(accounts or []):
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        record = statuses.get(account_id)
        if not isinstance(record, dict):
            record = {}
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        item_status = str(item.get("status") or "pushed").strip().lower()
        if item_status == "failed":
            record.update(
                {
                    "sub2api_status": "failed",
                    "sub2api_status_text": f"失败：{str(item.get('error') or '')[:220]}",
                    "sub2api_failed_at": now,
                    "sub2api_error": str(item.get("error") or ""),
                    "sub2api_step": str(item.get("step") or ""),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        else:
            record.update(
                {
                    "sub2api_status": "pushed",
                    "sub2api_status_text": "已推送",
                    "sub2api_pushed_at": now,
                    "sub2api_response": item.get("response", item),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        statuses[account_id] = record
    save_account_statuses(statuses)
    return statuses


def persist_grok2api_push_status(accounts, result):
    statuses = load_account_statuses()
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        items = []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for index, account in enumerate(accounts or []):
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        record = statuses.get(account_id)
        if not isinstance(record, dict):
            record = {}
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        item_status = str(item.get("status") or "pushed").strip().lower()
        if item_status == "failed":
            record.update(
                {
                    "grok2api_status": "failed",
                    "grok2api_status_text": f"失败：{str(item.get('error') or '')[:220]}",
                    "grok2api_failed_at": now,
                    "grok2api_error": str(item.get("error") or ""),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        else:
            record.update(
                {
                    "grok2api_status": "pushed",
                    "grok2api_status_text": "已推送",
                    "grok2api_pushed_at": now,
                    "grok2api_response": item.get("response", item),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        statuses[account_id] = record
    save_account_statuses(statuses)
    return statuses


def persist_cpa_push_status(accounts, result):
    statuses = load_account_statuses()
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        items = []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for index, account in enumerate(accounts or []):
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        record = statuses.get(account_id)
        if not isinstance(record, dict):
            record = {}
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        item_status = str(item.get("status") or "pushed").strip().lower()
        if item_status == "failed":
            record.pop("cpa_pushed_at", None)
            record.pop("cpa_response", None)
            record.update(
                {
                    "cpa_status": "failed",
                    "cpa_status_text": f"失败：{str(item.get('error') or '')[:220]}",
                    "cpa_failed_at": now,
                    "cpa_error": str(item.get("error") or ""),
                    "cpa_step": str(item.get("step") or ""),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        else:
            record.pop("cpa_failed_at", None)
            record.pop("cpa_error", None)
            record.pop("cpa_step", None)
            record.update(
                {
                    "cpa_status": "pushed",
                    "cpa_status_text": "已推送",
                    "cpa_pushed_at": now,
                    "cpa_response": item.get("response", item),
                    "email": account.get("email", ""),
                    "source_file": account.get("source_file", ""),
                    "line_no": account.get("line_no", ""),
                }
            )
        statuses[account_id] = record
    save_account_statuses(statuses)
    return statuses


def persist_account_health_status(accounts, result):
    statuses = load_account_statuses()
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        items = []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for index, account in enumerate(accounts or []):
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        record = statuses.get(account_id)
        if not isinstance(record, dict):
            record = {}
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        health_status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        record.update(
            {
                "health_status": health_status,
                "health_status_text": account_health_status_text(health_status),
                "health_checked_at": now,
                "email": account.get("email", ""),
                "source_file": account.get("source_file", ""),
                "line_no": account.get("line_no", ""),
            }
        )
        if item.get("error"):
            record["health_error"] = str(item.get("error") or "")
        else:
            record.pop("health_error", None)
        if "response" in item:
            record["health_response"] = item.get("response")
        statuses[account_id] = record
    save_account_statuses(statuses)
    return statuses


def find_registered_accounts(account_ids):
    wanted = {str(item) for item in (account_ids or []) if str(item).strip()}
    if not wanted:
        return []
    return [account for account in list_registered_accounts(include_sso=True) if account["id"] in wanted]


def _write_registered_account_lines(path, lines):
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".accounts-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.writelines(lines)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def delete_registered_accounts(account_ids):
    wanted = {str(account_id).strip() for account_id in (account_ids or []) if str(account_id).strip()}
    if not wanted:
        raise ValueError("请选择要删除的账号")

    deleted_ids = set()
    statuses = load_account_statuses()
    with _registered_accounts_lock:
        data_dir = get_data_dir()
        for name in sorted(os.listdir(data_dir), reverse=True):
            if not (name.startswith("accounts_") and name.endswith(".txt")):
                continue
            path = os.path.join(data_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as file:
                lines = file.readlines()

            retained_lines = []
            for old_line_no, line in enumerate(lines, start=1):
                account = parse_registered_account_line(
                    line, source=name, line_no=old_line_no, include_sso=True
                )
                if account and account["id"] in wanted:
                    deleted_ids.add(account["id"])
                    statuses.pop(account["id"], None)
                    continue

                retained_lines.append(line)
                if not account:
                    continue
                new_line_no = len(retained_lines)
                new_account_id = _account_id(name, new_line_no, account["email"], account["sso"])
                if new_account_id == account["id"]:
                    continue
                record = statuses.pop(account["id"], None)
                if isinstance(record, dict):
                    record = dict(record)
                    record.update(
                        {
                            "email": account["email"],
                            "source_file": name,
                            "line_no": new_line_no,
                        }
                    )
                    statuses[new_account_id] = record

            if len(retained_lines) != len(lines):
                _write_registered_account_lines(path, retained_lines)

        if deleted_ids:
            save_account_statuses(statuses)

    return {"deleted": len(deleted_ids), "missing": len(wanted - deleted_ids)}


def _parse_int_list(value):
    ids = []
    if isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = str(value or "").split(",")
    for candidate in candidates:
        try:
            parsed = int(str(candidate).strip())
        except Exception:
            continue
        if parsed > 0:
            ids.append(parsed)
    return ids


def _optional_positive_int(value, default=None):
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _sub2api_api_base(settings):
    base = str(settings.get("sub2api_base") or "").strip().rstrip("/")
    if not base:
        raise ValueError("sub2api Base 未配置")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


def _sub2api_headers(settings):
    token = str(settings.get("sub2api_admin_token") or "").strip()
    if not token:
        raise ValueError("sub2api 管理 Token 未配置")
    auth_mode = str(settings.get("sub2api_auth_mode") or "x-api-key").strip().lower()
    headers = {"Content-Type": "application/json"}
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
    return headers


def _sub2api_response_data(resp):
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception:
        return {"raw": resp.text[:1000]}
    if isinstance(payload, dict) and "code" in payload and payload.get("code") not in (0, 200, "0", "200", None):
        message = payload.get("message") or payload.get("msg") or payload.get("error") or payload
        raise Exception(f"sub2api 返回错误: {message}")
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _grok2api_admin_base(settings=None):
    settings = {**config, **dict(settings or {})}
    base = str(settings.get("grok2api_remote_base") or "").strip().rstrip("/")
    if not base:
        raise ValueError("grok2api 远端 Base 未配置")
    if base.endswith("/admin/api"):
        return base
    if base.endswith("/admin"):
        return f"{base}/api"
    return f"{base}/admin/api"


def _grok2api_pool_name(settings=None):
    settings = {**config, **dict(settings or {})}
    pool_name = str(settings.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    return pool_map.get(pool_name, pool_name)


def _grok2api_auth(settings=None):
    settings = {**config, **dict(settings or {})}
    app_key = str(settings.get("grok2api_remote_app_key") or "").strip()
    if not app_key:
        raise ValueError("grok2api 远端 app_key 未配置")
    return {"Content-Type": "application/json"}, {"app_key": app_key}


def _grok2api_response_data(resp):
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception:
        return {"raw": resp.text[:1000]}
    if isinstance(payload, dict) and "code" in payload and payload.get("code") not in (0, 200, "0", "200", None):
        message = payload.get("message") or payload.get("msg") or payload.get("error") or payload
        raise Exception(f"grok2api 返回错误: {message}")
    return payload


def import_accounts_to_grok2api(accounts, settings=None, log_callback=None):
    settings = {**config, **dict(settings or {})}
    base = _grok2api_admin_base(settings)
    headers, params = _grok2api_auth(settings)
    pool = _grok2api_pool_name(settings)
    valid_accounts = []
    missing = []
    for account in accounts or []:
        token = _normalize_sso_token(account.get("sso", ""))
        if token:
            item = dict(account)
            item["sso"] = token
            valid_accounts.append(item)
        else:
            missing.append(str(account.get("email") or account.get("id") or "").strip())
    if not valid_accounts:
        raise ValueError("没有可推送的账号：选中账号缺少 sso token")
    if missing:
        missing = [item for item in missing if item]
        raise ValueError(f"账号 {', '.join(missing)} 缺少 sso token，不能推送到 grok2api")

    tokens = [account["sso"] for account in valid_accounts]
    payload = {"tokens": tokens, "pool": pool, "tags": ["auto-register"]}
    items = []
    try:
        response = _grok2api_response_data(
            http_post(
                f"{base}/tokens/add",
                headers=headers,
                params=params,
                json=payload,
                timeout=30,
                proxies={},
            )
        )
        for account in valid_accounts:
            items.append(
                {
                    "email": account.get("email", ""),
                    "status": "pushed",
                    "response": {"pool": pool, "result": response},
                }
            )
    except Exception as exc:
        error_text = _sub2api_error_text(exc, step="grok2api")
        for account in valid_accounts:
            items.append(
                {
                    "email": account.get("email", ""),
                    "status": "failed",
                    "error": error_text,
                }
            )
        if log_callback:
            log_callback(f"[!] 推送 grok2api 失败: {error_text}")

    success_count = len([item for item in items if item.get("status") == "pushed"])
    failed_count = len(items) - success_count
    if log_callback:
        log_callback(f"[+] grok2api 推送完成: 成功 {success_count} / 失败 {failed_count}")
    return {
        "imported": failed_count == 0,
        "total": success_count,
        "failed": failed_count,
        "items": items,
        "warning": "已按 SSO token 导入 grok2api 远端池。",
    }


def _sub2api_account_name(account, settings=None, index=1):
    settings = {**config, **dict(settings or {})}
    email = str((account or {}).get("email") or "").strip()
    base_name = str(settings.get("sub2api_account_name") or "Grok Auto").strip() or "Grok Auto"
    return f"{base_name} - {email}" if email else f"{base_name} #{index}"


def build_sub2api_grok_refresh_token_check_payload(account, settings=None):
    settings = {**config, **dict(settings or {})}
    refresh_token = str((account or {}).get("refresh_token") or "").strip()
    if not refresh_token:
        raise ValueError(f"账号 {account.get('email', '') or ''} 缺少 refresh_token，不能推送到 sub2api")
    payload = {
        "refresh_token": refresh_token,
        "client_id": str(settings.get("sub2api_grok_client_id") or XAI_GROK_OAUTH_CLIENT_ID).strip(),
    }
    email = str((account or {}).get("email") or "").strip()
    if email:
        payload["email"] = email
    proxy_id = _optional_positive_int(settings.get("sub2api_proxy_id"), None)
    if proxy_id is not None:
        payload["proxy_id"] = proxy_id
    return payload


def build_sub2api_grok_refresh_token_payload(account, token_info=None, settings=None, index=1):
    settings = {**config, **dict(settings or {})}
    refresh_token = str((account or {}).get("refresh_token") or "").strip()
    if not refresh_token:
        raise ValueError(f"账号 {account.get('email', '') or index} 缺少 refresh_token，不能推送到 sub2api")
    token_info = token_info if isinstance(token_info, dict) else {}
    credentials = dict(token_info)
    credentials["refresh_token"] = str(credentials.get("refresh_token") or refresh_token).strip()
    credentials["client_id"] = str(credentials.get("client_id") or settings.get("sub2api_grok_client_id") or XAI_GROK_OAUTH_CLIENT_ID).strip()
    credentials["base_url"] = str(credentials.get("base_url") or settings.get("sub2api_grok_base_url") or XAI_GROK_API_BASE_URL).strip()
    email = str((account or {}).get("email") or "").strip()
    if email and not credentials.get("email"):
        credentials["email"] = email
    payload = {
        "name": _sub2api_account_name(account, settings, index=index),
        "platform": "grok",
        "type": "oauth",
        "credentials": credentials,
    }
    group_ids = _parse_int_list(settings.get("sub2api_group_ids", ""))
    if group_ids:
        payload["group_ids"] = group_ids
    concurrency = _optional_positive_int(settings.get("sub2api_concurrency"), None)
    if concurrency is not None:
        payload["concurrency"] = concurrency
    priority = _optional_positive_int(settings.get("sub2api_priority"), None)
    if priority is not None:
        payload["priority"] = priority
    return payload


def _push_one_account_to_sub2api(account, settings, base, headers, index):
    token_info = _sub2api_response_data(
        http_post(
            f"{base}/admin/grok/oauth/refresh-token",
            headers=headers,
            json=build_sub2api_grok_refresh_token_check_payload(account, settings),
            timeout=60,
            proxies={},
        )
    )
    token_refresh = str((token_info or {}).get("refresh_token") or "").strip()
    if token_refresh and token_refresh != str(account.get("refresh_token") or "").strip():
        replace_registered_account_refresh_token(account, token_refresh)
    payload = build_sub2api_grok_refresh_token_payload(account, token_info, settings, index=index)
    created = _sub2api_response_data(
        http_post(
            f"{base}/admin/accounts",
            headers=headers,
            json=payload,
            timeout=60,
            proxies={},
        )
    )
    return {"email": account.get("email", ""), "status": "pushed", "response": created}


def import_accounts_to_sub2api(accounts, settings=None, log_callback=None):
    settings = {**config, **dict(settings or {})}
    base = _sub2api_api_base(settings)
    headers = _sub2api_headers(settings)

    valid_accounts = [account for account in (accounts or []) if str(account.get("refresh_token") or "").strip()]
    if not valid_accounts:
        raise ValueError("没有可推送的账号：选中账号缺少 refresh_token")
    missing = [
        str(account.get("email") or account.get("id") or "").strip()
        for account in (accounts or [])
        if not str(account.get("refresh_token") or "").strip()
    ]
    missing = [item for item in missing if item]
    if missing:
        raise ValueError(f"账号 {', '.join(missing)} 缺少 refresh_token，不能推送到 sub2api")

    items = []
    for index, account in enumerate(valid_accounts, start=1):
        step = "refresh-token"
        try:
            items.append(_push_one_account_to_sub2api(account, settings, base, headers, index))
        except Exception as exc:
            error_text = _sub2api_error_text(exc, step=step)
            if step == "refresh-token" and is_refresh_token_revoked_error(error_text) and account.get("sso"):
                try:
                    if log_callback:
                        log_callback(f"[*] Refresh Token 已失效，尝试用 SSO 重新获取: {account.get('email', '')}")
                    new_refresh_token = fetch_xai_oauth_refresh_token(
                        account.get("sso"),
                        log_callback=log_callback,
                    )
                    replace_registered_account_refresh_token(account, new_refresh_token)
                    items.append(_push_one_account_to_sub2api(account, settings, base, headers, index))
                    continue
                except Exception as retry_exc:
                    error_text = f"{error_text}; retry_with_sso_failed: {_sub2api_error_text(retry_exc)}"
            items.append(
                {
                    "email": account.get("email", ""),
                    "status": "failed",
                    "step": step,
                    "error": error_text,
                }
            )
            if log_callback:
                log_callback(f"[!] 推送 sub2api 失败: {account.get('email', '')} {items[-1]['error']}")
    success_count = len([item for item in items if item.get("status") == "pushed"])
    failed_count = len(items) - success_count
    if log_callback:
        log_callback(f"[+] sub2api 推送完成: 成功 {success_count} / 失败 {failed_count}")
    return {
        "imported": failed_count == 0,
        "total": success_count,
        "failed": failed_count,
        "items": items,
        "warning": "已按 Refresh Token 直接导入 sub2api；历史仅有 sso 的账号不能推送。",
    }


def check_registered_accounts_health(accounts, settings=None, log_callback=None):
    settings = {**config, **dict(settings or {})}
    items = []
    for account in accounts or []:
        email = str(account.get("email") or "").strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        if not refresh_token:
            items.append(
                {
                    "email": email,
                    "status": "incomplete",
                    "error": "缺少 refresh_token",
                }
            )
            continue
        try:
            token_info = exchange_xai_refresh_token(refresh_token, settings=settings)
            token_refresh = str((token_info or {}).get("refresh_token") or "").strip()
            if token_refresh and token_refresh != refresh_token:
                replace_registered_account_refresh_token(account, token_refresh)
            response = {
                "token_type": token_info.get("token_type", ""),
                "expires_in": token_info.get("expires_in", ""),
                "scope": token_info.get("scope", ""),
            }
            items.append({"email": email, "status": "healthy", "response": response})
        except Exception as exc:
            items.append(
                {
                    "email": email,
                    "status": "unhealthy",
                    "error": _sub2api_error_text(exc, step="refresh-token"),
                }
            )
    healthy_count = len([item for item in items if item.get("status") == "healthy"])
    failed_count = len(items) - healthy_count
    if log_callback:
        log_callback(f"[+] 健康检查完成: 可用 {healthy_count} / 异常 {failed_count}")
    return {
        "checked": len(items),
        "healthy": healthy_count,
        "failed": failed_count,
        "items": items,
    }


def auto_push_registered_account(account, settings=None, log_callback=None):
    settings = {**config, **dict(settings or {})}
    if settings.get("grok2api_auto_add_remote"):
        try:
            result = import_accounts_to_grok2api([account], settings, log_callback=log_callback)
            persist_grok2api_push_status([account], result)
            if log_callback:
                log_callback(f"[*] 已自动推送到远程 grok2api: {account.get('email', '')}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 自动推送远程 grok2api 失败: {exc}")
    if settings.get("sub2api_auto_import_remote"):
        try:
            result = import_accounts_to_sub2api([account], settings, log_callback=log_callback)
            persist_sub2api_push_status([account], result)
            if log_callback:
                log_callback(f"[*] 已自动推送到 sub2api: {account.get('email', '')}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 自动推送 sub2api 失败: {exc}")


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    if not token_file:
        if log_callback:
            log_callback("[Debug] grok2api 本地 token.json 未配置，跳过")
        return False
    token_dir = os.path.dirname(token_file)
    if token_dir:
        os.makedirs(token_dir, exist_ok=True)
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
    try:
        base = _grok2api_admin_base(config)
        headers, query = _grok2api_auth(config)
    except ValueError:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    remote_pool = _grok2api_pool_name(config)
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

    # 关键：手动浏览器能过、脚本不过时，优先消掉明显的自动化开关。
    # 不要强行覆盖成“Chrome/138”这类可能与容器真实 Chromium 版本不一致的 UA，
    # 版本漂移本身就是 Turnstile 常见扣分项；仅在配置显式给出时才设置。
    configured_ua = str(config.get("user_agent") or "").strip()
    default_ua = str(DEFAULT_CONFIG.get("user_agent") or "").strip()
    if configured_ua and configured_ua != default_ua:
        try:
            options.set_user_agent(configured_ua)
        except Exception:
            options.set_argument(f"--user-agent={configured_ua}")

    # 隐藏 automation controlled / enable-automation 横幅类特征。
    options.set_argument("--disable-blink-features=AutomationControlled")
    options.set_argument("--disable-infobars")
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    options.set_argument("--password-store=basic")
    try:
        # DrissionPage/Chromium 参数写法兼容两种形式。
        options.set_argument("--exclude-switches", "enable-automation")
    except Exception:
        options.set_argument("--exclude-switches=enable-automation")
    try:
        options.set_pref("credentials_enable_service", False)
        options.set_pref("profile.password_manager_enabled", False)
    except Exception:
        pass

    # turnstilePatch 扩展负责 document_start 隐藏 webdriver / 自动点选。
    # Docker 中跳过扩展加载：--load-extension / --enable-unsafe-extension-debugging 是已知自动化检测向量，
    # stealth 功能已由 CDP Page.addScriptToEvaluateOnNewDocument 完全覆盖。
    in_docker_mode = _env_truthy("GROK_REG_IN_DOCKER")
    if not in_docker_mode and os.path.isdir(EXTENSION_PATH):
        ext_path = os.path.abspath(EXTENSION_PATH)
        try:
            if hasattr(options, "add_extension"):
                options.add_extension(ext_path)
        except Exception:
            pass
        try:
            options.set_argument(f"--load-extension={ext_path}")
            options.set_argument(f"--disable-extensions-except={ext_path}")
        except Exception:
            pass

    if should_apply_container_chrome_flags():
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        # 不要 --disable-gpu：flexible Turnstile 会看 WebGL/Canvas；容器用 ANGLE/SwiftShader。
        options.set_argument("--use-gl=angle")
        options.set_argument("--use-angle=swiftshader-webgl")
        options.set_argument("--enable-webgl")
        options.set_argument("--enable-webgl2-compute-context")
        options.set_argument("--ignore-gpu-blocklist")
        options.set_argument("--enable-features=NetworkService,NetworkServiceInProcess")
        # 更接近常见桌面分辨率，避免 1365x900 这种少见尺寸成为指纹。
        options.set_argument("--window-size", "1920,1080")
        options.set_argument("--window-position", "0,0")
        options.set_argument("--lang", "en-US")
        options.set_argument("--accept-lang", "en-US,en")
        options.set_argument("--disable-background-timer-throttling")
        options.set_argument("--disable-renderer-backgrounding")
        options.set_argument("--disable-backgrounding-occluded-windows")
    if should_run_headless():
        options.headless(True)
    return options


def probe_browser_stealth(page, log_callback=None):
    """启动后采样自动化指纹，方便对照“手动能过/脚本不过”。"""
    if not page:
        return {}
    try:
        detail = page.run_js(
            r"""
return {
  webdriver: navigator.webdriver,
  languages: navigator.languages,
  platform: navigator.platform,
  userAgent: navigator.userAgent,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory || null,
  chrome: !!(window.chrome && window.chrome.runtime),
  plugins: navigator.plugins ? navigator.plugins.length : 0,
  webgl: (function () {
    try {
      const c = document.createElement('canvas');
      const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!gl) return {ok:false};
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return {
        ok: true,
        vendor: gl.getParameter(ext ? ext.UNMASKED_VENDOR_WEBGL : gl.VENDOR),
        renderer: gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER),
      };
    } catch (e) {
      return {ok:false, error: String(e && e.message || e).slice(0, 120)};
    }
  })(),
};
            """
        )
    except Exception as exc:
        detail = {"error": str(exc)[:200]}
    if log_callback:
        try:
            log_callback(f"[Debug] 浏览器指纹采样: {json.dumps(detail, ensure_ascii=False)[:500]}")
        except Exception:
            log_callback(f"[Debug] 浏览器指纹采样: {detail}")
    return detail if isinstance(detail, dict) else {"raw": detail}


def humanize_page_activity(page, log_callback=None, cancel_callback=None):
    """在 Turnstile 评分窗口内模拟轻微鼠标/滚动，避免“纯脚本填表零交互”。"""
    if not page:
        return
    try:
        viewport = page.run_js(
            """
return {
  w: Math.max(320, window.innerWidth || 1365),
  h: Math.max(320, window.innerHeight || 900),
};
            """
        ) or {"w": 1365, "h": 900}
        width = int(viewport.get("w") or 1365)
        height = int(viewport.get("h") or 900)
        points = [
            (int(width * 0.22), int(height * 0.28)),
            (int(width * 0.48), int(height * 0.42)),
            (int(width * 0.63), int(height * 0.57)),
            (int(width * 0.40), int(height * 0.70)),
        ]
        for x, y in points:
            raise_if_cancelled(cancel_callback)
            try:
                page.run_cdp(
                    "Input.dispatchMouseEvent",
                    type="mouseMoved",
                    x=x,
                    y=y,
                    modifiers=0,
                )
            except Exception:
                pass
            sleep_with_cancel(random.uniform(0.05, 0.16), cancel_callback)
        try:
            page.run_js(
                """
window.scrollBy(0, Math.floor(40 + Math.random() * 120));
setTimeout(() => window.scrollBy(0, -Math.floor(20 + Math.random() * 60)), 120);
                """
            )
        except Exception:
            pass
        if log_callback:
            log_callback("[Debug] 已注入轻微鼠标/滚动交互，等待 Turnstile 被动评分")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 人机交互模拟失败: {str(exc)[:160]}")


def turnstile_page_hook_source():
    global _turnstile_page_hook_source_cache
    if _turnstile_page_hook_source_cache is not None:
        return _turnstile_page_hook_source_cache
    try:
        with open(TURNSTILE_PAGE_HOOK_PATH, "r", encoding="utf-8") as handle:
            _turnstile_page_hook_source_cache = handle.read()
    except Exception:
        _turnstile_page_hook_source_cache = ""
    return _turnstile_page_hook_source_cache


def install_light_stealth_script(page, log_callback=None):
    """增强 stealth：WebGL/Canvas/Audio 反指纹 + 插件/WebRTC 伪装，绝不补丁 window.turnstile。"""
    if not page:
        return False
    source = r"""
(() => {
  // 1. 隐藏 navigator.webdriver
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
  } catch (e) {}

  // 2. chrome.runtime —— Turnstile 检查 window.chrome.runtime 是否存在
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
  } catch (e) {}

  // 3. permissions.query —— notifications 路径异常是已知检测项
  try {
    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (originalQuery) {
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : originalQuery(parameters)
      );
    }
  } catch (e) {}

  // 4. languages
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // 5. platform + userAgent —— 根据 UA 动态推导，Linux UA 替换为 Windows 画像
  try {
    const ua = navigator.userAgent || '';
    let p = 'Linux x86_64';
    let fakeUa = ua;
    if (/Windows/.test(ua)) {
      p = 'Win32';
    } else if (/Macintosh/.test(ua)) {
      p = 'MacIntel';
    } else if (/Linux/.test(ua)) {
      // Linux UA 改为 Windows 画像（保持 Chrome 版本号一致）
      p = 'Win32';
      fakeUa = ua.replace('X11; Linux x86_64', 'Windows NT 10.0; Win64; x64');
    }
    Object.defineProperty(navigator, 'platform', { get: () => p, configurable: true });
    if (fakeUa !== ua) {
      Object.defineProperty(navigator, 'userAgent', { get: () => fakeUa, configurable: true });
    }
  } catch (e) {}

  // 6. WebGL vendor/renderer —— 始终 hook getParameter，在调用时才判断是否需要伪装
  //    关键：不能先检测再 hook，因为 document_start 阶段检测可能因时序问题失败
  try {
    const FAKE_WGL_VENDOR = 'Google Inc. (Intel)';
    const FAKE_WGL_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)';
    const SW_RE = /swiftshader|llvmpipe|softpipe|software[\s_-]*rasterizer|mesa[\s_-]*swrast/i;

    const hookGetParam = (proto) => {
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      proto.getParameter = function(param) {
        const result = orig.call(this, param);
        // UNMASKED_VENDOR_WEBGL = 37445
        if (param === 37445 && SW_RE.test(String(result))) return FAKE_WGL_VENDOR;
        // UNMASKED_RENDERER_WEBGL = 37446
        if (param === 37446 && SW_RE.test(String(result))) return FAKE_WGL_RENDERER;
        return result;
      };
    };
    try { hookGetParam(WebGLRenderingContext.prototype); } catch (e) {}
    try { hookGetParam(WebGL2RenderingContext.prototype); } catch (e) {}
  } catch (e) {}

  // 7. Canvas 指纹噪声 —— 对 toDataURL/toBlob 注入微量确定性噪声
  try {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const origToBlob = HTMLCanvasElement.prototype.toBlob;
    const _noiseOff = ((window.location.hostname || '').length * 7 + 3) % 5 + 1;

    function _canvasInjectNoise(canvas) {
      try {
        const ctx = canvas.getContext('2d');
        if (!ctx || canvas.width < 1 || canvas.height < 1) return;
        const img = ctx.getImageData(0, 0, 1, 1);
        img.data[3] = (img.data[3] + _noiseOff) & 0xFF;
        ctx.putImageData(img, 0, 0);
      } catch (e) {}
    }

    HTMLCanvasElement.prototype.toDataURL = function() {
      _canvasInjectNoise(this);
      return origToDataURL.apply(this, arguments);
    };
    if (origToBlob) {
      HTMLCanvasElement.prototype.toBlob = function() {
        _canvasInjectNoise(this);
        return origToBlob.apply(this, arguments);
      };
    }
  } catch (e) {}

  // 8. AudioContext 指纹噪声 —— Turnstile 会采样 AudioContext 输出做指纹
  try {
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
      const data = origGetChannelData.call(this, channel);
      if (data && data.length > 0) {
        // 仅对第一个采样点加极微小确定性偏移，不影响听觉，但改变指纹哈希
        const off = ((this.length || 0) * 3 + 1) % 7 / 100000;
        data[0] = data[0] + off;
      }
      return data;
    };
  } catch (e) {}

  // 9. navigator.plugins 伪装 —— headless Chrome 的 plugins 为空数组是已知检测项
  //    必须先缓存原始值，否则 getter 内访问 navigator.plugins 会无限递归
  try {
    const _origPlugins = navigator.plugins;
    if (!_origPlugins || _origPlugins.length === 0) {
      // 仅当原生 plugins 为空时才覆盖（headless 特征）
      Object.defineProperty(navigator, 'plugins', {
        get: () => _origPlugins,
        configurable: true,
      });
    }
  } catch (e) {}

  // 10. WebRTC 屏蔽 —— 容器内 WebRTC 会暴露内网 IP / 无 ICE 候选，是已知检测项
  try {
    if (window.RTCPeerConnection || window.webkitRTCPeerConnection) {
      const _RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
      const _origConnect = _RTC.prototype.connect;
      // 劫持 setConfiguration 来强制 relay-only，而非替换整个构造函数（避免破坏 instanceof）
      const _origSetConfig = _RTC.prototype.setConfiguration;
      if (_origSetConfig) {
        _RTC.prototype.setConfiguration = function(config) {
          if (config && config.iceTransportPolicy === undefined) {
            config.iceTransportPolicy = 'relay';
          }
          return _origSetConfig.call(this, config);
        };
      }
    }
    // 屏蔽 getUserMedia / enumerateDevices 中的 videoinput（容器无摄像头）
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      const _origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      navigator.mediaDevices.enumerateDevices = function() {
        return _origEnum().then(d => d.filter(x => x.kind !== 'videoinput'));
      };
    }
  } catch (e) {}

  // 11. iframe contentWindow 检测 —— Turnstile 可能通过 iframe.contentWindow.chrome 判断
  try {
    const origCreateElement = document.createElement.bind(document);
    // 不修改 createElement，但确保 shadow DOM 下的 chrome 对象完整
    if (!window.chrome.csi) window.chrome.csi = function() { return {}; };
    if (!window.chrome.loadTimes) window.chrome.loadTimes = function() { return { commitLoadTime: Date.now()/1000, startLoadTime: Date.now()/1000 }; };
  } catch (e) {}
})();
"""
    ok = False
    try:
        page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
        ok = True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 增强 stealth 预注入失败: {str(exc)[:160]}")
    try:
        page.run_cdp("Runtime.evaluate", expression=source)
        ok = True
    except Exception:
        pass
    if ok and log_callback:
        log_callback("[Debug] 已安装增强 stealth（WebGL/Canvas/Audio/Plugins/WebRTC，不补丁 turnstile API）")
    return ok


def read_turnstile_token_len(page):
    if not page:
        return 0
    try:
        value = page.run_js(
            r"""
const input = document.querySelector('input[name="cf-turnstile-response"]');
const direct = String((input && input.value) || '').trim();
if (direct) return direct.length;
try {
  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
    const resp = String(window.turnstile.getResponse() || '').trim();
    if (resp) return resp.length;
  }
} catch (e) {}
return 0;
            """
        )
        return int(value or 0)
    except Exception:
        return 0


def install_turnstile_page_hook(page, log_callback=None):
    source = turnstile_page_hook_source()
    if not page or not source:
        return False
    installed = False
    try:
        page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
        installed = True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile CDP 预注入失败: {str(exc)[:180]}")
    try:
        page.run_cdp("Runtime.evaluate", expression=source)
        installed = True
    except Exception:
        pass
    return installed


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


def _base64_urlsafe_no_padding(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def build_xai_oauth_authorize_url(state, code_challenge, nonce, redirect_uri=None):
    params = {
        "response_type": "code",
        "client_id": XAI_GROK_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri or XAI_GROK_OAUTH_REDIRECT_URI,
        "scope": XAI_GROK_OAUTH_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "plan": "generic",
        "referrer": "sub2api",
    }
    return f"{XAI_GROK_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_xai_oauth_callback_url(url):
    parsed = urllib.parse.urlparse(str(url or ""))
    values = urllib.parse.parse_qs(parsed.query)
    code = (values.get("code") or [""])[0].strip()
    state = (values.get("state") or [""])[0].strip()
    error = (values.get("error") or [""])[0].strip()
    if not code and parsed.fragment:
        fragment_values = urllib.parse.parse_qs(parsed.fragment)
        code = (fragment_values.get("code") or [""])[0].strip()
        state = state or (fragment_values.get("state") or [""])[0].strip()
        error = error or (fragment_values.get("error") or [""])[0].strip()
    return {"code": code, "state": state, "error": error, "url": str(url or "")}


def build_xai_oauth_consent_click_script():
    return r"""
const isConsentPage = String(location.href || '').includes('oauth2/consent');
if (!isConsentPage) {
  return {
    clicked: false,
    skipped: true,
    isConsentPage,
    url: String(location.href || ''),
    text: document.body ? String(document.body.innerText || '').slice(0, 300) : ''
  };
}
const denyWords = ['cancel', 'deny', 'decline', 'reject', '拒绝', '取消'];
const allowWords = [
  'allow', 'authorize', 'authorise', 'continue', 'approve', 'accept',
  'agree', 'yes', 'confirm', 'submit', '同意', '授权', '继续', '允许', '确认'
];
const textOf = (node) => String(
  node.innerText || node.textContent || node.value ||
  node.getAttribute?.('aria-label') || node.getAttribute?.('title') || ''
).replace(/\s+/g, ' ').trim().toLowerCase();
const visible = (node) => {
  try {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  } catch (e) {
    return true;
  }
};
const disabled = (node) => !!(node.disabled || node.getAttribute?.('disabled') !== null || node.getAttribute?.('aria-disabled') === 'true');
const allNodes = [];
const visit = (root) => {
  if (!root) return;
  try {
    const nodes = Array.from(root.querySelectorAll('*'));
    for (const node of nodes) {
      allNodes.push(node);
      if (node.shadowRoot) visit(node.shadowRoot);
    }
  } catch (e) {}
};
visit(document);
const clickables = allNodes.filter((node) => {
  const tag = String(node.tagName || '').toLowerCase();
  const role = String(node.getAttribute?.('role') || '').toLowerCase();
  const type = String(node.getAttribute?.('type') || '').toLowerCase();
  return tag === 'button' || tag === 'a' || role === 'button' || type === 'submit' || node.onclick;
}).filter((node) => visible(node) && !disabled(node));
const buttons = clickables;
const score = (node) => {
  const text = textOf(node);
  if (denyWords.some((word) => text.includes(word))) return -100;
  let value = 0;
  if (allowWords.some((word) => text.includes(word))) value += 100;
  const cls = String(node.className || '').toLowerCase();
  if (cls.includes('primary') || cls.includes('submit') || cls.includes('continue')) value += 10;
  const rect = node.getBoundingClientRect?.();
  if (rect) value += Math.min(20, Math.max(0, rect.left / 100));
  return value;
};
const ranked = clickables.map((node) => ({ node, score: score(node), text: textOf(node) }))
  .filter((item) => item.score >= 0)
  .sort((a, b) => b.score - a.score);
const buttonDiagnostics = ranked.slice(0, 8).map((item) => ({
  text: item.text,
  score: item.score,
  tag: String(item.node.tagName || '').toLowerCase(),
  type: String(item.node.getAttribute?.('type') || '').toLowerCase(),
  role: String(item.node.getAttribute?.('role') || '').toLowerCase()
}));
const target = ranked.find((item) => item.score >= 100)?.node;
if (target) {
  target.scrollIntoView?.({ block: 'center', inline: 'center' });
  const rect = target.getBoundingClientRect();
  const centerX = Math.round(rect.left + rect.width / 2);
  const centerY = Math.round(rect.top + rect.height / 2);
  target.click();
  const form = target.closest?.('form');
  if (form) {
    try {
      form.requestSubmit ? form.requestSubmit(target) : form.submit();
    } catch (e) {
      try { form.submit(); } catch (ignored) {}
    }
  }
  return {
    clicked: true,
    text: textOf(target),
    count: clickables.length,
    isConsentPage,
    centerX,
    centerY,
    submitted: !!form,
    buttonDiagnostics
  };
}
return {
  clicked: false,
  count: clickables.length,
  isConsentPage,
  buttonDiagnostics,
  text: document.body ? String(document.body.innerText || '').slice(0, 300) : ''
};
"""


def _dispatch_cdp_click(page, x, y, include_keyboard=True):
    page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    page.run_cdp(
        "Input.dispatchMouseEvent",
        type="mousePressed",
        x=x,
        y=y,
        button="left",
        clickCount=1,
    )
    page.run_cdp(
        "Input.dispatchMouseEvent",
        type="mouseReleased",
        x=x,
        y=y,
        button="left",
        clickCount=1,
    )
    if include_keyboard:
        try:
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key=" ", code="Space", windowsVirtualKeyCode=32)
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key=" ", code="Space", windowsVirtualKeyCode=32)
        except Exception:
            pass


def _dispatch_cdp_text(page, text):
    page.run_cdp("Input.insertText", text=str(text or ""))


def _click_point_on_page(page, x, y):
    x, y = int(x), int(y)
    try:
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, modifiers=0)
    except Exception:
        pass
    _dispatch_cdp_click(page, x, y, include_keyboard=False)
    return {"x": x, "y": y, "nativeClicked": True}


def _locate_turnstile_target_via_js(page):
    """主文档 JS 定位（含 open shadow）。跨域 iframe 内文案看不到，但 iframe 元素本身应能看到。"""
    try:
        return page.run_js(
            r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
}
function centerLeft(node) {
  const rect = node.getBoundingClientRect();
  return {
    x: Math.round(rect.left + Math.min(Math.max(16, rect.width * 0.1), 34)),
    y: Math.round(rect.top + rect.height / 2),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}
const frames = [];
const widgets = [];
const allIframes = [];
function visit(root, depth) {
  if (!root || !root.querySelectorAll || depth > 8) return;
  let nodes;
  try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { return; }
  for (const node of nodes) {
    try { if (node.shadowRoot) visit(node.shadowRoot, depth + 1); } catch (e) {}
    const tag = String(node.tagName || '').toLowerCase();
    if (tag === 'iframe' && visible(node)) {
      const src = String(node.getAttribute('src') || '');
      const title = String(node.getAttribute('title') || '');
      const name = String(node.getAttribute('name') || '');
      const marker = (src + ' ' + title + ' ' + name + ' ' + (node.className || '') + ' ' + (node.id || '')).toLowerCase();
      allIframes.push({src: src.slice(0, 120), title: title.slice(0, 60), w: Math.round(node.getBoundingClientRect().width), h: Math.round(node.getBoundingClientRect().height)});
      if (
        marker.includes('challenge') || marker.includes('turnstile') ||
        marker.includes('cloudflare') || marker.includes('cf-chl') ||
        title.includes('小部件') || title.toLowerCase().includes('widget') ||
        // 无 src 标记时，尺寸像 turnstile checkbox 条（常见 ~300x65）
        (node.getBoundingClientRect().width >= 200 && node.getBoundingClientRect().width <= 420 &&
         node.getBoundingClientRect().height >= 40 && node.getBoundingClientRect().height <= 100)
      ) {
        frames.push(node);
      }
    }
    const sitekey = node.getAttribute && node.getAttribute('data-sitekey');
    const cls = String(node.className || '').toLowerCase();
    if (visible(node) && (sitekey || cls.includes('cf-turnstile') || cls.includes('cf-chl'))) {
      widgets.push(node);
    }
  }
}
visit(document, 0);
if (frames.length) {
  return { state: 'turnstile-challenge-target', via: 'js-iframe', count: frames.length, allIframes, ...centerLeft(frames[0]), src: String(frames[0].getAttribute('src')||'').slice(0,160) };
}
if (widgets.length) {
  return { state: 'turnstile-challenge-target', via: 'js-widget', count: widgets.length, allIframes, ...centerLeft(widgets[0]) };
}
// 兜底：任意“像 checkbox 条”的可见 iframe
const sized = allIframes.filter((f) => f.w >= 200 && f.w <= 420 && f.h >= 40 && f.h <= 100);
return { state: 'turnstile-challenge-not-found', via: 'js', frameCount: frames.length, widgetCount: widgets.length, allIframes: allIframes.slice(0, 8), sizedCount: sized.length };
            """
        )
    except Exception as exc:
        return {"state": "turnstile-js-error", "error": str(exc)[:200]}


def _locate_turnstile_target_via_cdp(page):
    """用 CDP DOM/FrameTree/AX 找跨域 turnstile iframe 并算页面坐标。"""
    diagnostics = {"via": "cdp"}
    # 1) FrameTree：子 frame URL 含 cloudflare/turnstile
    try:
        tree = page.run_cdp("Page.getFrameTree") or {}
        frames = []

        def walk(node, depth=0):
            if not isinstance(node, dict) or depth > 10:
                return
            frame = node.get("frame") or {}
            url = str(frame.get("url") or "")
            frames.append({"id": frame.get("id"), "url": url[:180], "name": frame.get("name")})
            for child in node.get("childFrames") or []:
                walk(child, depth + 1)

        root_frame_id = None
        frame_tree = tree.get("frameTree") or tree
        if isinstance(frame_tree, dict) and isinstance(frame_tree.get("frame"), dict):
            root_frame_id = frame_tree.get("frame", {}).get("id")
        walk(frame_tree)
        diagnostics["frames"] = frames[:12]
        cloud_frames = [
            f for f in frames
            if any(k in str(f.get("url") or "").lower() for k in ("cloudflare", "turnstile", "cf-chl", "challenges."))
        ]
        # Turnstile 在本地经常是 about:blank 子 frame（srcdoc/blob），URL 不含 cloudflare。
        blank_frames = [
            f for f in frames
            if f.get("id") and f.get("id") != root_frame_id
            and (
                not str(f.get("url") or "").strip()
                or str(f.get("url") or "").startswith("about:blank")
                or str(f.get("url") or "").startswith("blob:")
            )
        ]
        diagnostics["cloudFrameCount"] = len(cloud_frames)
        diagnostics["blankFrameCount"] = len(blank_frames)
        candidate_frames = cloud_frames + blank_frames
    except Exception as exc:
        diagnostics["frameTreeError"] = str(exc)[:160]
        cloud_frames = []
        blank_frames = []
        candidate_frames = []
        root_frame_id = None

    # 2) pierce DOM 找 iframe 节点 box
    try:
        doc = page.run_cdp("DOM.getDocument", depth=-1, pierce=True) or {}
        root_id = (doc.get("root") or {}).get("nodeId")
        if root_id:
            search = page.run_cdp("DOM.querySelectorAll", nodeId=root_id, selector="iframe") or {}
            node_ids = search.get("nodeIds") or []
            diagnostics["iframeNodeCount"] = len(node_ids)
            for node_id in node_ids[:20]:
                try:
                    attrs_resp = page.run_cdp("DOM.getAttributes", nodeId=node_id) or {}
                    attrs_list = attrs_resp.get("attributes") or []
                    attrs = {}
                    for i in range(0, len(attrs_list) - 1, 2):
                        attrs[str(attrs_list[i]).lower()] = str(attrs_list[i + 1])
                    src = attrs.get("src", "")
                    title = attrs.get("title", "")
                    marker = f"{src} {title}".lower()
                    box = page.run_cdp("DOM.getBoxModel", nodeId=node_id) or {}
                    model = box.get("model") or {}
                    content = model.get("content") or model.get("border") or []
                    if len(content) < 8:
                        continue
                    xs = content[0::2]
                    ys = content[1::2]
                    left, right = min(xs), max(xs)
                    top, bottom = min(ys), max(ys)
                    width, height = right - left, bottom - top
                    if width <= 1 or height <= 1:
                        continue
                    looks_like = (
                        any(k in marker for k in ("cloudflare", "turnstile", "challenge", "cf-chl"))
                        or (200 <= width <= 420 and 40 <= height <= 100)
                    )
                    if not looks_like:
                        continue
                    return {
                        "state": "turnstile-challenge-target",
                        "via": "cdp-iframe",
                        "x": int(left + min(max(16, width * 0.1), 34)),
                        "y": int(top + height / 2),
                        "width": int(width),
                        "height": int(height),
                        "src": src[:160],
                        "title": title[:80],
                        "diagnostics": diagnostics,
                    }
                except Exception:
                    continue
    except Exception as exc:
        diagnostics["domError"] = str(exc)[:160]

    # 3) Accessibility 树：可见 checkbox / 请验证您是真人
    try:
        page.run_cdp("Accessibility.enable")
        ax = page.run_cdp("Accessibility.getFullAXTree") or {}
        nodes = ax.get("nodes") or []
        diagnostics["axNodeCount"] = len(nodes)
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name", {}).get("value") if isinstance(node.get("name"), dict) else node.get("name") or "")
            role = str(node.get("role", {}).get("value") if isinstance(node.get("role"), dict) else node.get("role") or "")
            compact = name.replace(" ", "")
            interesting = (
                "请验证您是真人" in name
                or "确认您是真人" in name
                or "Verify you are human" in name
                or "verify you are human" in name.lower()
                or (role.lower() == "checkbox" and ("真人" in name or "human" in name.lower() or "cloudflare" in name.lower()))
            )
            if not interesting:
                continue
            backend_id = node.get("backendDOMNodeId")
            if not backend_id:
                continue
            try:
                resolved = page.run_cdp("DOM.resolveNode", backendNodeId=backend_id) or {}
                obj_id = (resolved.get("object") or {}).get("objectId")
                if not obj_id:
                    continue
                desc = page.run_cdp("DOM.describeNode", objectId=obj_id, pierce=True, depth=1) or {}
                node_id = (desc.get("node") or {}).get("nodeId")
                if not node_id:
                    # 尝试 backendDOMNodeId 直接 box
                    box = page.run_cdp("DOM.getBoxModel", backendNodeId=backend_id) or {}
                else:
                    box = page.run_cdp("DOM.getBoxModel", nodeId=node_id) or {}
                model = box.get("model") or {}
                content = model.get("content") or model.get("border") or []
                if len(content) < 8:
                    continue
                xs = content[0::2]
                ys = content[1::2]
                left, right = min(xs), max(xs)
                top, bottom = min(ys), max(ys)
                width, height = right - left, bottom - top
                return {
                    "state": "turnstile-challenge-target",
                    "via": "cdp-ax",
                    "x": int(left + min(max(16, width * 0.1), 34)),
                    "y": int(top + height / 2),
                    "width": int(width),
                    "height": int(height),
                    "name": name[:80],
                    "role": role[:40],
                    "diagnostics": diagnostics,
                }
            except Exception:
                continue
    except Exception as exc:
        diagnostics["axError"] = str(exc)[:160]

    # 4) 对 cloudflare / about:blank 子 frame，用 DOM.getFrameOwner 拿宿主 iframe 的 box 并点击左侧
    for frame in (candidate_frames or cloud_frames)[:12]:
        frame_id = frame.get("id")
        if not frame_id or frame_id == root_frame_id:
            continue
        try:
            owner = page.run_cdp("DOM.getFrameOwner", frameId=frame_id) or {}
            backend_id = owner.get("backendNodeId")
            node_id = owner.get("nodeId")
            if node_id:
                box = page.run_cdp("DOM.getBoxModel", nodeId=node_id) or {}
            elif backend_id:
                box = page.run_cdp("DOM.getBoxModel", backendNodeId=backend_id) or {}
            else:
                continue
            model = box.get("model") or {}
            content = model.get("content") or model.get("border") or []
            if len(content) < 8:
                continue
            xs = content[0::2]
            ys = content[1::2]
            left, right = min(xs), max(xs)
            top, bottom = min(ys), max(ys)
            width, height = right - left, bottom - top
            if width <= 1 or height <= 1:
                continue
            url = str(frame.get("url") or "")
            is_cf_url = any(k in url.lower() for k in ("cloudflare", "turnstile", "cf-chl", "challenges."))
            # about:blank 宿主框常见尺寸：宽 240~520，高 50~90
            checkbox_like = (180 <= width <= 560 and 36 <= height <= 120)
            if not is_cf_url and not checkbox_like:
                diagnostics.setdefault("skippedBlankOwners", []).append(
                    {"url": url[:80], "w": int(width), "h": int(height)}
                )
                continue
            return {
                "state": "turnstile-challenge-target",
                "via": "cdp-frame-owner",
                "x": int(left + min(max(16, width * 0.1), 34)),
                "y": int(top + height / 2),
                "width": int(width),
                "height": int(height),
                "src": url[:160],
                "diagnostics": diagnostics,
            }
        except Exception:
            continue

    return {"state": "turnstile-challenge-not-found", "diagnostics": diagnostics}


def _click_turnstile_challenge_if_visible(page):
    """点击可见的 Cloudflare 复选框/挑战框。

    本地桌面 Chrome 常渲染成可见的「请验证您是真人」复选框（在跨域 iframe 内）。
    仅靠主文档 querySelector 经常找不到，需要 CDP FrameTree/AX 兜底。
    """
    attempts = []
    target = _locate_turnstile_target_via_js(page)
    attempts.append({"method": "js", "state": (target or {}).get("state") if isinstance(target, dict) else type(target).__name__})
    if not (isinstance(target, dict) and target.get("state") == "turnstile-challenge-target"):
        cdp_target = _locate_turnstile_target_via_cdp(page)
        attempts.append({"method": "cdp", "state": (cdp_target or {}).get("state") if isinstance(cdp_target, dict) else type(cdp_target).__name__})
        if isinstance(cdp_target, dict) and cdp_target.get("state") == "turnstile-challenge-target":
            target = cdp_target
        elif isinstance(target, dict):
            # 合并诊断信息
            diag = {}
            if isinstance(target.get("allIframes"), list):
                diag["jsIframes"] = target.get("allIframes")
            if isinstance(cdp_target, dict):
                diag.update(cdp_target.get("diagnostics") or {})
                diag["cdpState"] = cdp_target.get("state")
            target = {
                "state": "turnstile-challenge-not-found",
                "attempts": attempts,
                "diagnostics": diag,
            }
        else:
            target = {"state": "turnstile-challenge-not-found", "attempts": attempts}

    if not isinstance(target, dict) or target.get("state") != "turnstile-challenge-target":
        return target
    if target.get("x") is None or target.get("y") is None:
        return {"state": "turnstile-challenge-missing-center", **target}

    click_meta = _click_point_on_page(page, target.get("x"), target.get("y"))
    # 有些实现要点两次才勾选
    sleep_with_cancel(0.15)
    try:
        _click_point_on_page(page, int(target.get("x")), int(target.get("y")))
    except Exception:
        pass
    return {**target, **click_meta, "attempts": attempts}


def _dispatch_cdp_keypress(page, ch):
    """派发真实按键事件（keyDown 带 text + keyUp），驱动 input-otp 等依赖
    keydown/onChange 的受控组件；insertText 会绕过这些处理器导致值不同步。"""
    ch = str(ch or "")
    if not ch:
        return
    vk = ord(ch.upper())
    page.run_cdp(
        "Input.dispatchKeyEvent",
        type="keyDown",
        text=ch,
        key=ch,
        windowsVirtualKeyCode=vk,
        nativeVirtualKeyCode=vk,
    )
    page.run_cdp(
        "Input.dispatchKeyEvent",
        type="keyUp",
        key=ch,
        windowsVirtualKeyCode=vk,
        nativeVirtualKeyCode=vk,
    )


def _fill_otp_code_native(page, clean_code, cancel_callback=None):
    target = page.run_js(build_otp_native_target_script(), len(clean_code))
    if not isinstance(target, dict) or target.get("state") != "otp-target":
        return target
    if target.get("centerX") is None or target.get("centerY") is None:
        return {"state": "otp-target-missing-center", **target}
    _dispatch_cdp_click(
        page,
        int(target.get("centerX")),
        int(target.get("centerY")),
        include_keyboard=False,
    )
    inserted = 0
    for ch in str(clean_code or ""):
        _dispatch_cdp_keypress(page, ch)
        inserted += 1
        sleep_with_cancel(0.08, cancel_callback)
    # 填后回读实际值长度，确认按键事件真的驱动了受控组件
    filled_len = None
    try:
        filled_len = page.run_js(
            r"""
try {
  const el = document.activeElement;
  const v = (el && typeof el.value === 'string') ? el.value : '';
  const otp = document.querySelector('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"]');
  const ov = otp ? String(otp.value || '') : '';
  return Math.max(String(v).replace(/\s+/g, '').length, ov.replace(/\s+/g, '').length);
} catch (e) { return -1; }
            """
        )
    except Exception:
        filled_len = None
    return {**target, "nativeInput": True, "insertedChars": inserted, "filledLen": filled_len}


def _click_otp_submit_native(page):
    target = page.run_js(build_otp_submit_target_script())
    if not isinstance(target, dict) or target.get("state") != "otp-submit-target":
        return target
    if target.get("centerX") is None or target.get("centerY") is None:
        return {"state": "otp-submit-missing-center", **target}
    _dispatch_cdp_click(
        page,
        int(target.get("centerX")),
        int(target.get("centerY")),
        include_keyboard=False,
    )
    return {**target, "nativeClicked": True}


def _click_xai_oauth_consent_if_present(page):
    try:
        result = page.run_js(build_xai_oauth_consent_click_script())
        if isinstance(result, dict) and result.get("centerX") is not None and result.get("centerY") is not None:
            x = int(result.get("centerX"))
            y = int(result.get("centerY"))
            try:
                _dispatch_cdp_click(page, x, y)
                result["nativeClicked"] = True
            except Exception as exc:
                result["nativeClickError"] = str(exc)[:160]
        return result
    except Exception:
        return False


def save_xai_oauth_debug_snapshot(page, log_callback=None):
    if not page:
        return []
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = os.path.join(get_data_dir(), f"oauth_debug_{stamp}")
    saved = []
    try:
        html = str(getattr(page, "html", "") or "")
        html_path = f"{base}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        saved.append(html_path)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] OAuth HTML 快照保存失败: {str(exc)[:160]}")
    png_path = f"{base}.png"
    screenshot_methods = [
        lambda: page.get_screenshot(path=png_path),
        lambda: page.get_screenshot(png_path),
        lambda: page.save_screenshot(png_path),
        lambda: page.screenshot(path=png_path),
    ]
    for method in screenshot_methods:
        try:
            method()
            if os.path.exists(png_path):
                saved.append(png_path)
                break
        except Exception:
            continue
    if log_callback and saved:
        log_callback(f"[Debug] OAuth 调试快照已保存: {', '.join(saved)}")
    return saved


def set_xai_sso_cookies_for_oauth(page, sso):
    token = _normalize_sso_token(sso)
    if not page or not token:
        return False
    cookies = [
        {"name": "sso", "value": token, "domain": ".x.ai", "path": "/", "secure": True, "httpOnly": True},
        {"name": "sso-rw", "value": token, "domain": ".x.ai", "path": "/", "secure": True, "httpOnly": True},
    ]
    ok = False
    for cookie in cookies:
        try:
            page.run_cdp("Network.setCookie", **cookie)
            ok = True
        except Exception:
            pass
    try:
        setter = getattr(getattr(page, "set", None), "cookies", None)
        if setter:
            setter(cookies)
            ok = True
    except Exception:
        pass
    return ok


def exchange_xai_oauth_code_for_token(code, code_verifier, redirect_uri=None):
    payload = {
        "grant_type": "authorization_code",
        "client_id": XAI_GROK_OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri or XAI_GROK_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    resp = http_post(
        XAI_GROK_OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "sub2api-grok-oauth/1.0",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not str(data.get("refresh_token") or "").strip():
        raise Exception(f"xAI OAuth token 返回缺少 refresh_token: {str(data)[:300]}")
    return data


def exchange_xai_refresh_token(refresh_token, settings=None):
    settings = {**config, **dict(settings or {})}
    token = str(refresh_token or "").strip()
    if not token:
        raise ValueError("缺少 refresh_token")
    payload = {
        "grant_type": "refresh_token",
        "client_id": str(settings.get("sub2api_grok_client_id") or XAI_GROK_OAUTH_CLIENT_ID).strip(),
        "refresh_token": token,
    }
    resp = http_post(
        XAI_GROK_OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "grok-register-health/1.0",
        },
        timeout=60,
    )
    status_code = getattr(resp, "status_code", None)
    if status_code is not None and int(status_code) >= 400:
        detail = str(getattr(resp, "text", "") or "").strip()
        if detail:
            raise ValueError(f"xAI OAuth refresh HTTP {status_code}: {detail[:1000]}")
        raise ValueError(f"xAI OAuth refresh HTTP {status_code}")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise Exception(f"xAI OAuth refresh 返回缺少 access_token: {str(data)[:300]}")
    return data


def normalize_cpa_management_auth_files_url(base):
    value = str(base or "").strip().rstrip("/")
    if not value:
        raise ValueError("CPA 管理地址不能为空")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = f"http://{value}"
    value = re.sub(r"/v0/management(?:/auth-files)?$", "", value, flags=re.IGNORECASE)
    return f"{value}/v0/management/auth-files"


def _cpa_credential_file_name(email):
    safe_email = re.sub(r"[^A-Za-z0-9@._-]", "-", str(email or "").strip()).strip("-")
    if not safe_email:
        raise ValueError("CPA 凭证缺少邮箱")
    return f"xai-{safe_email}.json"


def _cpa_access_token_metadata(access_token):
    try:
        parts = str(access_token or "").split(".")
        if len(parts) < 2:
            raise ValueError("not a JWT")
        payload_segment = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_segment))
        expires_at = int(claims["exp"])
        issued_at = int(claims.get("iat") or expires_at - 21600)
        expired = datetime.datetime.fromtimestamp(
            expires_at, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return expired, max(expires_at - issued_at, 0), str(
            claims.get("sub") or claims.get("principal_id") or ""
        ).strip()
    except Exception:
        return "", 21600, ""


def _write_cpa_credential(auth_dir, filename, payload):
    os.makedirs(auth_dir, exist_ok=True)
    path = os.path.join(auth_dir, filename)
    fd, temp_path = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=auth_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return path


def export_and_push_cpa_credential(email, refresh_token, settings=None, log_callback=None):
    resolved_settings = {**config, **dict(settings or {})}
    log = log_callback or (lambda message: None)
    token_data = exchange_xai_refresh_token(refresh_token, settings=resolved_settings)
    access_token = str(token_data.get("access_token") or "").strip()
    resolved_refresh_token = str(token_data.get("refresh_token") or refresh_token or "").strip()
    if not access_token or not resolved_refresh_token:
        raise ValueError("xAI OAuth 返回缺少 CPA 凭证所需 token")

    expired, token_expires_in, subject = _cpa_access_token_metadata(access_token)
    auth_dir = str(resolved_settings.get("cpa_auth_dir") or "cpa_auths").strip()
    if not os.path.isabs(auth_dir):
        auth_dir = os.path.join(get_data_dir(), auth_dir)
    filename = _cpa_credential_file_name(email)
    payload = {
        "type": "xai",
        "access_token": access_token,
        "refresh_token": resolved_refresh_token,
        "token_type": str(token_data.get("token_type") or "Bearer"),
        "expires_in": int(token_data.get("expires_in") or token_expires_in),
        "expired": expired,
        "last_refresh": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": str(email or "").strip(),
        "sub": subject,
        "base_url": str(resolved_settings.get("cpa_base_url") or CPA_DEFAULT_BASE_URL).rstrip("/"),
        "redirect_uri": XAI_GROK_OAUTH_REDIRECT_URI,
        "token_endpoint": XAI_GROK_OAUTH_TOKEN_URL,
        "auth_kind": "oauth",
        "headers": dict(CPA_CLIENT_HEADERS),
    }
    if token_data.get("id_token"):
        payload["id_token"] = str(token_data["id_token"])
    local_path = _write_cpa_credential(auth_dir, filename, payload)
    result = {
        "ok": True,
        "path": local_path,
        "filename": filename,
        "refresh_token": resolved_refresh_token,
    }

    if not resolved_settings.get("cpa_auto_push_remote"):
        return result

    management_base = str(resolved_settings.get("cpa_management_base") or "").strip()
    management_key = str(resolved_settings.get("cpa_management_key") or "").strip()
    if not management_base or not management_key:
        result["upload_error"] = "CPA 自动推送缺少管理地址或管理密钥"
        return result

    multipart = None
    try:
        if CurlMime is None:
            raise RuntimeError("curl_cffi 未安装，无法上传 CPA 凭证")
        multipart = CurlMime()
        multipart.addpart(
            name="file",
            content_type="application/json",
            filename=filename,
            local_path=local_path,
        )
        response = http_post(
            normalize_cpa_management_auth_files_url(management_base),
            headers={"Authorization": f"Bearer {management_key}"},
            multipart=multipart,
            timeout=30,
            proxies={},
        )
        response.raise_for_status()
        result["uploaded"] = True
        result["upload_status"] = getattr(response, "status_code", None)
        log(f"[cpa] 已推送凭证到 CPA: {filename}")
    except Exception as exc:
        result["upload_error"] = str(exc)[:500]
        log(f"[cpa] 推送 CPA 凭证失败: {result['upload_error']}")
    finally:
        if multipart is not None:
            multipart.close()
    return result


def import_accounts_to_cpa(accounts, settings=None, log_callback=None):
    resolved_settings = {**config, **dict(settings or {})}
    management_base = str(resolved_settings.get("cpa_management_base") or "").strip()
    management_key = str(resolved_settings.get("cpa_management_key") or "").strip()
    if not management_base or not management_key:
        raise ValueError("推送到 CPA 需要先填写 CPA 管理地址和管理密钥")
    resolved_settings["cpa_auto_push_remote"] = True

    accounts = list(accounts or [])
    items = [None] * len(accounts)
    pending = []
    for index, account in enumerate(accounts):
        email = str(account.get("email") or account.get("id") or "").strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        if not refresh_token:
            items[index] = {
                "email": email,
                "status": "failed",
                "step": "credential",
                "error": "缺少 refresh_token",
            }
            continue

        pending.append((index, account, email, refresh_token))

    worker_count = _parse_positive_int(
        resolved_settings.get("cpa_push_workers"), 3, minimum=1, maximum=10
    )
    log_lock = threading.Lock()

    def safe_log(message):
        if log_callback:
            with log_lock:
                log_callback(message)

    def push_once(task):
        index, account, email, refresh_token = task
        try:
            result = export_and_push_cpa_credential(
                email,
                refresh_token,
                resolved_settings,
                log_callback=safe_log,
            )
            return index, account, email, refresh_token, result, None
        except Exception as exc:
            return index, account, email, refresh_token, None, exc

    def result_item(account, email, refresh_token, result):
        rotated_refresh_token = str(result.get("refresh_token") or "").strip()
        if rotated_refresh_token and rotated_refresh_token != refresh_token:
            replace_registered_account_refresh_token(account, rotated_refresh_token)
        upload_error = str(result.get("upload_error") or "").strip()
        if upload_error or not result.get("uploaded"):
            return {
                "email": email,
                "status": "failed",
                "step": "upload",
                "error": upload_error or "CPA 未确认上传成功",
            }
        return {
            "email": email,
            "status": "pushed",
            "response": {
                "filename": result.get("filename", ""),
                "uploaded": True,
                "upload_status": result.get("upload_status"),
            },
        }

    outcomes = []
    if pending:
        actual_workers = min(worker_count, len(pending))
        safe_log(f"[*] CPA 批量推送: {len(pending)} 个账号，{actual_workers} 路并发")
        with ThreadPoolExecutor(max_workers=actual_workers, thread_name_prefix="cpa-push") as executor:
            outcomes = list(executor.map(push_once, pending))

    retry_with_sso = []
    for index, account, email, refresh_token, result, error in outcomes:
        if error is None:
            items[index] = result_item(account, email, refresh_token, result)
            continue

        error_text = _sub2api_error_text(error)
        if account.get("sso") and is_xai_refresh_token_client_error(error):
            retry_with_sso.append((index, account, email, refresh_token, error_text))
            continue
        items[index] = {
            "email": email,
            "status": "failed",
            "step": "credential",
            "error": error_text,
        }

    # SSO recovery opens browser tabs and mutates source account files, so keep it serialized.
    for index, account, email, refresh_token, error_text in retry_with_sso:
        try:
            safe_log(f"[*] CPA Refresh Token 不可用，尝试用 SSO 重新获取: {email}")
            refresh_token = fetch_xai_oauth_refresh_token(
                account.get("sso"),
                log_callback=safe_log,
            )
            replace_registered_account_refresh_token(account, refresh_token)
            result = export_and_push_cpa_credential(
                email,
                refresh_token,
                resolved_settings,
                log_callback=safe_log,
            )
            items[index] = result_item(account, email, refresh_token, result)
        except Exception as retry_exc:
            items[index] = {
                "email": email,
                "status": "failed",
                "step": "credential",
                "error": f"{error_text}; retry_with_sso_failed: {_sub2api_error_text(retry_exc)}"[:1000],
            }

    success_count = len([item for item in items if item.get("status") == "pushed"])
    failed_count = len(items) - success_count
    if log_callback:
        log_callback(f"[+] CPA 推送完成: 成功 {success_count} / 失败 {failed_count}")
    return {
        "imported": failed_count == 0,
        "total": success_count,
        "failed": failed_count,
        "items": items,
        "warning": "CPA 推送使用账号保存的第四段 Refresh Token。",
    }


def fetch_xai_oauth_refresh_token(sso, timeout=90, log_callback=None, cancel_callback=None):
    token = _normalize_sso_token(sso)
    if not token:
        raise ValueError("账号缺少 sso cookie，无法获取 Refresh Token")
    browser = _get_browser()
    page = _get_page()
    if browser is None or page is None:
        browser, page = start_browser(log_callback=log_callback)
    try:
        page = browser.new_tab("https://auth.x.ai")
        _set_page(page)
    except Exception:
        page = refresh_active_page()

    code_verifier = _base64_urlsafe_no_padding(secrets.token_bytes(32))
    challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = _base64_urlsafe_no_padding(challenge)
    state = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    auth_url = build_xai_oauth_authorize_url(state, code_challenge, nonce)
    if log_callback:
        log_callback("[*] 获取 xAI OAuth Refresh Token...")
    set_xai_sso_cookies_for_oauth(page, token)
    page.get(auth_url)

    deadline = time.time() + timeout
    last_url = ""
    next_diag_at = 0
    consent_submitted_at = 0
    consent_submitted_url = ""
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        current_url = str(getattr(page, "url", "") or "")
        last_url = current_url or last_url
        parsed = parse_xai_oauth_callback_url(current_url)
        if parsed.get("error"):
            raise Exception(f"xAI OAuth 返回错误: {parsed['error']}")
        if parsed.get("code"):
            if parsed.get("state") and parsed.get("state") != state:
                raise Exception("xAI OAuth state 不匹配")
            token_data = exchange_xai_oauth_code_for_token(parsed["code"], code_verifier)
            refresh_token = str(token_data.get("refresh_token") or "").strip()
            if log_callback:
                log_callback(f"[*] 已获取 xAI OAuth Refresh Token，长度={len(refresh_token)}")
            return refresh_token
        click_result = {"skipped": "waiting_after_submit"}
        waiting_after_submit = (
            consent_submitted_at
            and "oauth2/consent" in current_url
            and current_url == consent_submitted_url
        )
        if not waiting_after_submit:
            click_result = _click_xai_oauth_consent_if_present(page)
            if isinstance(click_result, dict) and (click_result.get("clicked") or click_result.get("submitted")):
                consent_submitted_at = time.time()
                consent_submitted_url = current_url
        if log_callback and time.time() >= next_diag_at:
            log_callback(f"[Debug] xAI OAuth consent 点击结果: {click_result}")
            next_diag_at = time.time() + 5
        sleep_with_cancel(1.2 if waiting_after_submit else 0.8, cancel_callback)
    snapshot_paths = save_xai_oauth_debug_snapshot(page, log_callback=log_callback)
    snapshot_text = f"，调试快照: {', '.join(snapshot_paths)}" if snapshot_paths else ""
    raise Exception(f"xAI OAuth 未在 {timeout}s 内返回 code，最后URL: {last_url}{snapshot_text}")


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def should_log_cloudflare_wait(state, scope, token_len, interval=5.0):
    now = time.time()
    key = str(scope or "default")
    token_len = str(token_len)
    last = state.get(key, {}) if isinstance(state, dict) else {}
    if last.get("token_len") != token_len or now - float(last.get("time", 0.0)) >= interval:
        state[key] = {"token_len": token_len, "time": now}
        return True
    return False


def detect_cloudflare_block_page(page_html):
    html = str(page_html or "").lower()
    return (
        "attention required! | cloudflare" in html
        or "sorry, you have been blocked" in html
        or "cf-error-code" in html
    )


EMAIL_INPUT_SELECTOR = ", ".join(
    [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[name="identifier"]',
        'input[id*="email" i]',
        'input[id*="identifier" i]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱"]',
        'input[aria-label*="email" i]',
        'input[aria-label*="邮箱"]',
        'input[type="text"]',
        "input:not([type])",
    ]
)

EMAIL_SUBMIT_KEYWORDS = (
    "注册",
    "继续",
    "下一步",
    "sign up",
    "signup",
    "continue",
    "next",
    "submit",
)


PROFILE_SUBMIT_KEYWORDS = (
    "完成注册",
    "创建账户",
    "创建账号",
    "注册",
    "继续",
    "下一步",
    "sign up",
    "signup",
    "create account",
    "createaccount",
    "create",
    "continue",
    "next",
    "submit",
)


def build_email_form_script(action):
    if action not in {"fill", "submit", "diagnose"}:
        raise ValueError(f"Unsupported email form action: {action}")
    selector = json.dumps(EMAIL_INPUT_SELECTOR, ensure_ascii=False)
    keywords = json.dumps(list(EMAIL_SUBMIT_KEYWORDS), ensure_ascii=False)
    action_json = json.dumps(action)
    return f"""
const action = {action_json};
const email = arguments[0] || '';
const emailSelector = {selector};
const submitKeywords = {keywords};
function isVisible(node) {{
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}}
function nodeText(node) {{
    return String(
        node.innerText ||
        node.textContent ||
        node.value ||
        node.getAttribute('aria-label') ||
        node.getAttribute('title') ||
        ''
    ).replace(/\\s+/g, ' ').trim();
}}
function inputScore(node) {{
    const attrs = [
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('type'),
        node.getAttribute('autocomplete'),
        node.getAttribute('placeholder'),
        node.getAttribute('aria-label'),
    ].join(' ').toLowerCase();
    if (attrs.includes('email') || attrs.includes('邮箱')) return 100;
    if (attrs.includes('identifier')) return 95;
    if (attrs.includes('login') || attrs.includes('account')) return 70;
    if ((node.getAttribute('type') || '').toLowerCase() === 'text') return 25;
    return 10;
}}
function pickEmailInput() {{
    const inputs = Array.from(document.querySelectorAll(emailSelector)).filter((node) => {{
        const type = (node.getAttribute('type') || 'text').toLowerCase();
        return isVisible(node) && !node.disabled && !node.readOnly && !['hidden', 'password', 'checkbox', 'radio', 'submit', 'button'].includes(type);
    }});
    return inputs.sort((a, b) => inputScore(b) - inputScore(a))[0] || null;
}}
function setInputValue(input, value) {{
    input.focus();
    input.click();
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (valueSetter) valueSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('focus', {{ bubbles: true }}));
    input.dispatchEvent(new InputEvent('beforeinput', {{ bubbles: true, data: value, inputType: 'insertText' }}));
    input.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: value, inputType: 'insertText' }}));
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    input.dispatchEvent(new KeyboardEvent('keyup', {{ key: '@', bubbles: true }}));
    input.dispatchEvent(new Event('blur', {{ bubbles: true }}));
    return String(input.value || '').trim() === String(value || '').trim();
}}
function pickSubmitButton() {{
    const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {{
        return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
    }});
    return buttons.find((node) => {{
        const text = nodeText(node).toLowerCase().replace(/\\s+/g, '');
        return submitKeywords.some((keyword) => text.includes(String(keyword).toLowerCase().replace(/\\s+/g, '')));
    }}) || buttons.find((node) => {{
        const type = String(node.getAttribute('type') || '').toLowerCase();
        return type === 'submit';
    }}) || buttons[0] || null;
}}
if (action === 'diagnose') {{
    const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).slice(0, 8).map((node) => ({{
        type: node.getAttribute('type') || '',
        name: node.getAttribute('name') || '',
        id: node.getAttribute('id') || '',
        autocomplete: node.getAttribute('autocomplete') || '',
        placeholder: node.getAttribute('placeholder') || '',
        aria: node.getAttribute('aria-label') || '',
    }}));
    const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).filter(isVisible).slice(0, 8).map(nodeText);
    return JSON.stringify({{
        url: location.href,
        title: document.title,
        hasEmailInput: !!pickEmailInput(),
        hasSubmitButton: !!pickSubmitButton(),
        inputs,
        buttons,
    }});
}}
const input = pickEmailInput();
if (!input) return 'not-ready';
if (action === 'fill') {{
    if (setInputValue(input, email)) return 'filled';
    input.value = '';
    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    for (const ch of email) {{
        input.dispatchEvent(new KeyboardEvent('keydown', {{ key: ch, bubbles: true }}));
        input.value += ch;
        input.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: ch, inputType: 'insertText' }}));
        input.dispatchEvent(new KeyboardEvent('keyup', {{ key: ch, bubbles: true }}));
    }}
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    if (String(input.value || '').trim() === email) return 'filled';
    return input.value || 'empty-after-fill';
}}
if (!(input.value || '').trim()) return 'input-empty';
const submitButton = pickSubmitButton();
if (!submitButton) return 'no-submit-button';
submitButton.focus();
submitButton.click();
return true;
"""


def build_email_submission_state_script():
    return r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return String(node?.innerText || node?.textContent || node?.value || '').replace(/\s+/g, ' ').trim();
}
const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible);
const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).filter(isVisible);
const bodyText = textOf(document.body).slice(0, 1000);
const otpInput = inputs.find((node) => {
    const attrs = [
        node.getAttribute('name'),
        node.getAttribute('autocomplete'),
        node.getAttribute('inputmode'),
        node.getAttribute('aria-label'),
        node.getAttribute('data-input-otp'),
        node.getAttribute('placeholder'),
    ].join(' ').toLowerCase();
    return attrs.includes('one-time-code') ||
        attrs.includes('otp') ||
        attrs.includes('code') ||
        attrs.includes('verification') ||
        attrs.includes('验证码') ||
        attrs.includes('numeric') ||
        node.getAttribute('data-input-otp') === 'true';
});
const resendButton = buttons.find((node) => {
    const text = textOf(node).toLowerCase();
    return text.includes('resend') || text.includes('重新发送') || text.includes('再次发送');
});
const errorNode = Array.from(document.querySelectorAll('[role="alert"], [aria-live], .error, [data-testid*="error" i]'))
    .filter(isVisible)
    .map(textOf)
    .find(Boolean) || '';
let step = 'unknown';
if (otpInput || resendButton || /verification code|enter code|验证码|確認コード/i.test(bodyText)) {
    step = 'otp';
} else if (inputs.some((node) => {
    const attrs = [
        node.getAttribute('name'),
        node.getAttribute('type'),
        node.getAttribute('autocomplete'),
        node.getAttribute('placeholder'),
        node.getAttribute('aria-label'),
    ].join(' ').toLowerCase();
    return attrs.includes('email') || attrs.includes('identifier') || attrs.includes('邮箱');
})) {
    step = 'email';
}
return JSON.stringify({
    step,
    url: location.href,
    title: document.title,
    errorText: errorNode,
    bodySnippet: bodyText.slice(0, 240),
    inputs: inputs.slice(0, 6).map((node) => ({
        type: node.getAttribute('type') || '',
        name: node.getAttribute('name') || '',
        autocomplete: node.getAttribute('autocomplete') || '',
        placeholder: node.getAttribute('placeholder') || '',
        aria: node.getAttribute('aria-label') || '',
    })),
    buttons: buttons.slice(0, 6).map(textOf),
});
"""


def build_otp_native_target_script():
    return r"""
// otp-native-target
const codeLen = Number(arguments[0] || 6);
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function inputAttrs(node) {
    return [
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('type'),
        node.getAttribute('autocomplete'),
        node.getAttribute('inputmode'),
        node.getAttribute('placeholder'),
        node.getAttribute('aria-label'),
        node.getAttribute('data-input-otp'),
    ].join(' ').toLowerCase();
}
function centerOf(node) {
    const rect = node.getBoundingClientRect();
    return {
        centerX: Math.round(rect.left + rect.width / 2),
        centerY: Math.round(rect.top + rect.height / 2),
    };
}
const inputs = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const type = String(node.getAttribute('type') || 'text').toLowerCase();
    return !['hidden', 'password', 'checkbox', 'radio', 'submit', 'button'].includes(type);
});
const scored = inputs.map((node) => {
    const attrs = inputAttrs(node);
    let score = 0;
    if (node.getAttribute('data-input-otp') === 'true') score += 120;
    if (attrs.includes('one-time-code')) score += 110;
    if (attrs.includes('otp')) score += 100;
    if (attrs.includes('verification')) score += 90;
    if (attrs.includes('code')) score += 80;
    if (attrs.includes('验证码')) score += 80;
    if (attrs.includes('numeric')) score += 40;
    if (Number(node.maxLength || 0) >= codeLen) score += 25;
    if (Number(node.maxLength || 0) === 1) score -= 15;
    return { node, score };
}).filter((item) => item.score > 0).sort((a, b) => b.score - a.score);
if (scored.length) {
    const target = scored[0].node;
    target.focus();
    const point = centerOf(target);
    return {
        state: 'otp-target',
        mode: Number(target.maxLength || 0) === 1 ? 'split-first' : 'aggregate',
        valueLen: String(target.value || '').length,
        maxLength: Number(target.maxLength || 0),
        ...point,
    };
}
return {
    state: 'otp-not-ready',
    inputs: inputs.slice(0, 6).map((node) => ({
        name: node.getAttribute('name') || '',
        type: node.getAttribute('type') || '',
        autocomplete: node.getAttribute('autocomplete') || '',
        inputmode: node.getAttribute('inputmode') || '',
        maxLength: Number(node.maxLength || 0),
        aria: node.getAttribute('aria-label') || '',
        dataInputOtp: node.getAttribute('data-input-otp') || '',
    })),
};
"""


def build_otp_submit_target_script():
    return r"""
// otp-submit-target
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return String(
        node?.innerText ||
        node?.textContent ||
        node?.value ||
        node?.getAttribute?.('aria-label') ||
        ''
    ).replace(/\s+/g, ' ').trim();
}
function centerOf(node) {
    const rect = node.getBoundingClientRect();
    return {
        centerX: Math.round(rect.left + rect.width / 2),
        centerY: Math.round(rect.top + rect.height / 2),
    };
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const target = buttons.find((node) => {
    const text = textOf(node).toLowerCase().replace(/\s+/g, '');
    return (
        text.includes('确认邮箱') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('confirm') ||
        text.includes('continue') ||
        text.includes('next')
    );
}) || buttons.find((node) => String(node.getAttribute('type') || '').toLowerCase() === 'submit');
if (!target) return { state: 'otp-submit-not-ready', count: buttons.length };
target.focus();
return {
    state: 'otp-submit-target',
    text: textOf(target),
    count: buttons.length,
    ...centerOf(target),
};
"""


def build_profile_submit_script(action):
    if action not in {"check", "submit", "trigger", "diagnose", "retry_error", "recover_entry"}:
        raise ValueError(f"Unsupported profile submit action: {action}")
    keywords = json.dumps(list(PROFILE_SUBMIT_KEYWORDS), ensure_ascii=False)
    action_json = json.dumps(action)
    return f"""
const action = {action_json};
const submitKeywords = {keywords};
function isVisible(node) {{
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}}
function nodeText(node) {{
    return String(
        node.innerText ||
        node.textContent ||
        node.value ||
        node.getAttribute('aria-label') ||
        node.getAttribute('title') ||
        ''
    ).replace(/\\s+/g, ' ').trim();
}}
function normalizedText(node) {{
    return nodeText(node).toLowerCase().replace(/\\s+/g, '');
}}
function pickSubmitButton() {{
    const buttons = Array.from(document.querySelectorAll(
        'button[type="submit"], button, [role="button"], input[type="submit"], a[href]'
    )).filter((node) => {{
        return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
    }});
    return buttons.find((node) => {{
        const text = normalizedText(node);
        return submitKeywords.some((keyword) => text.includes(String(keyword).toLowerCase().replace(/\\s+/g, '')));
    }}) || buttons.find((node) => {{
        return String(node.getAttribute('type') || '').toLowerCase() === 'submit';
    }}) || null;
}}
function submitProfileForm(submitBtn) {{
    if (!submitBtn) return false;
    submitBtn.focus();
    const form = submitBtn.form || submitBtn.closest('form');
    if (form && typeof form.requestSubmit === 'function') {{
        try {{
            form.requestSubmit(submitBtn);
            return true;
        }} catch (e) {{}}
    }}
    try {{
        submitBtn.click();
        return true;
    }} catch (e) {{}}
    return false;
}}
function cloudflareState() {{
    const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
    const cfPresent = !!cfInput
      || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
    if (!cfPresent) return 'none';
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length >= 80) return 'solved';
    return 'wait-cloudflare:' + token.length;
}}
function hasResource(fragment) {{
    try {{
        return performance.getEntriesByType('resource').some((entry) => {{
            return String(entry && entry.name || '').includes(fragment);
        }});
    }} catch (e) {{
        return false;
    }}
}}
function triggerPasswordValidation() {{
    const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
    if (!passwordInput) return false;
    try {{
        passwordInput.focus();
        passwordInput.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: '', inputType: 'insertText' }}));
        passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        passwordInput.blur();
        return true;
    }} catch (e) {{
        return false;
    }}
}}
function turnstileDetail() {{
    const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
    const captured = (() => {{
        try {{
            const raw = window.__grokTurnstile || {{}};
            return {{
                hookInstalled: !!window.__grokTurnstileHookInstalled,
                renderCount: raw.renderCount || 0,
                executeCount: raw.executeCount || 0,
                callbackCount: raw.callbackCount || 0,
                lastTokenLen: String(raw.lastToken || '').trim().length,
                lastExecuteArgs: raw.lastExecuteArgs || [],
                widgets: Array.isArray(raw.widgets) ? raw.widgets.slice(-5) : [],
                errors: Array.isArray(raw.errors) ? raw.errors.slice(-5) : [],
            }};
        }} catch (e) {{
            return {{ error: String(e && e.message || e).slice(0, 160) }};
        }}
    }})();
    const widgets = Array.from(document.querySelectorAll('div.cf-turnstile, [data-sitekey]')).map((n) => ({{
        sitekey: n.getAttribute('data-sitekey') || '',
        theme: n.getAttribute('data-theme') || '',
        size: n.getAttribute('data-size') || '',
        action: n.getAttribute('data-action') || '',
        class: n.className || '',
    }}));
    const iframes = Array.from(document.querySelectorAll('iframe')).filter((f) => {{
        const s = f.getAttribute('src') || '';
        return s.includes('turnstile') || s.includes('challenges.cloudflare.com');
    }}).map((f) => ({{
        src: (f.getAttribute('src') || '').slice(0, 160),
        w: f.getBoundingClientRect().width,
        h: f.getBoundingClientRect().height,
        visible: isVisible(f),
    }}));
    return {{
        hasInput: !!cfInput,
        inputLen: String((cfInput && cfInput.value) || '').trim().length,
        turnstileApi: (typeof window.turnstile !== 'undefined'),
        captured,
        widgets,
        iframes,
        webdriver: navigator.webdriver,
    }};
}}
function networkDetail() {{
    const resources = (() => {{
        try {{
            return performance.getEntriesByType('resource').map((entry) => String(entry && entry.name || ''));
        }} catch (e) {{
            return [];
        }}
    }})();
    return {{
        validatePasswordSeen: resources.some((name) => name.includes('ValidatePassword')),
        signUpSeen: resources.some((name) => name.includes('/sign-up')),
    }};
}}
if (action === 'diagnose') {{
    const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"], a[href]'))
        .filter(isVisible)
        .slice(0, 10)
        .map((node) => ({{
            text: nodeText(node),
            tag: node.tagName,
            role: node.getAttribute('role') || '',
            type: node.getAttribute('type') || '',
            aria: node.getAttribute('aria-label') || '',
            disabled: !!node.disabled,
            ariaDisabled: node.getAttribute('aria-disabled') || '',
        }}));
    const inputs = Array.from(document.querySelectorAll('input'))
        .filter(isVisible)
        .slice(0, 10)
        .map((node) => ({{
            type: node.getAttribute('type') || '',
            name: node.getAttribute('name') || '',
            autocomplete: node.getAttribute('autocomplete') || '',
            aria: node.getAttribute('aria-label') || '',
        }}));
    return JSON.stringify({{
        url: location.href,
        title: document.title,
        cf: cloudflareState(),
        turnstile: turnstileDetail(),
        network: networkDetail(),
        hasSubmitButton: !!pickSubmitButton(),
        buttons,
        inputs,
        bodySnippet: nodeText(document.body).slice(0, 300),
    }});
}}
if (action === 'retry_error') {{
    const bodyText = nodeText(document.body);
    const compactBody = bodyText.toLowerCase().replace(/\\s+/g, '');
    const errorHints = [
        'An error occurred',
        'There was an error loading this page',
        '请验证你使用的网址是否正确',
    ];
    const isErrorPage = compactBody.includes('anerroroccurred')
        || compactBody.includes('therewasanerrorloadingthispage')
        || compactBody.includes('errorloadingthispage');
    if (!isErrorPage) return 'profile-error-page-not-detected';
    const retryBtn = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], a[href]'))
        .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
        .find((node) => {{
            const text = normalizedText(node);
            return text.includes('retry') || text.includes('重试') || text.includes('reload');
        }});
    if (!retryBtn) {{
        return {{
            state: 'profile-error-page-no-retry',
            title: document.title,
            hints: errorHints,
            bodySnippet: bodyText.slice(0, 240),
        }};
    }}
    retryBtn.focus();
    const rect = retryBtn.getBoundingClientRect();
    try {{ retryBtn.click(); }} catch (e) {{}}
    return {{
        state: 'profile-error-retry-target',
        centerX: Math.round(rect.left + rect.width / 2),
        centerY: Math.round(rect.top + rect.height / 2),
        text: nodeText(retryBtn).slice(0, 80),
        title: document.title,
        bodySnippet: bodyText.slice(0, 240),
    }};
}}
if (action === 'recover_entry') {{
    const bodyText = nodeText(document.body);
    const compactBody = bodyText.toLowerCase().replace(/\\s+/g, '');
    const hasProfileInputs = !!document.querySelector('input[name="givenName"], input[autocomplete="given-name"]')
        && !!document.querySelector('input[name="password"], input[type="password"]');
    if (hasProfileInputs) return 'profile-entry-has-profile-form';
    const isSignupEntry = compactBody.includes('createyouraccount')
        || compactBody.includes('signupwithemail')
        || compactBody.includes('youaresigninginto');
    if (!isSignupEntry) return 'profile-entry-page-not-detected';
    const emailBtn = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], a[href]'))
        .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
        .find((node) => {{
            const text = normalizedText(node);
            return text.includes('signupwithemail')
                || text.includes('continuewithemail')
                || text.includes('使用邮箱注册')
                || text.includes('email');
        }});
    if (!emailBtn) {{
        return {{
            state: 'profile-entry-page-no-email',
            title: document.title,
            hints: ['Create your account', 'Sign up with email'],
            bodySnippet: bodyText.slice(0, 240),
        }};
    }}
    emailBtn.focus();
    const rect = emailBtn.getBoundingClientRect();
    try {{ emailBtn.click(); }} catch (e) {{}}
    return {{
        state: 'profile-entry-email-target',
        centerX: Math.round(rect.left + rect.width / 2),
        centerY: Math.round(rect.top + rect.height / 2),
        text: nodeText(emailBtn).slice(0, 80),
        title: document.title,
        bodySnippet: bodyText.slice(0, 240),
    }};
}}
const cf = cloudflareState();
if (action === 'trigger') {{
    // xAI 已改为“提交时才触发”的隐形 Turnstile：脚本已加载但不预渲染组件，
    // 需主动执行并放行点击，让网站前端自行驱动 challenge 生成 token。
    let executed = false;
    const executedWidgets = [];
    try {{
        if (window.turnstile && typeof window.turnstile.execute === 'function') {{
            const capturedWidgets = Array.isArray(window.__grokTurnstile && window.__grokTurnstile.widgets)
                ? window.__grokTurnstile.widgets
                : [];
            for (const widget of capturedWidgets) {{
                const id = widget && widget.id;
                if (id !== undefined && id !== null && id !== '') {{
                    try {{
                        window.turnstile.execute(id);
                        executed = true;
                        executedWidgets.push(String(id));
                    }} catch (e) {{}}
                }}
            }}
            if (!executed) {{
                try {{ window.turnstile.execute(); executed = true; }} catch (e) {{}}
            }}
        }}
    }} catch (e) {{}}
    const submitBtn = pickSubmitButton();
    if (!submitBtn) return 'trigger-no-submit';
    // 仅在已有 token 时才点提交；token 仍为空时只触发 challenge，避免空 token 提交后卡死。
    if (cf === 'solved' || cf === 'none') {{
        submitProfileForm(submitBtn);
        return 'trigger-clicked:' + (executed ? '1' : '0') + ':' + executedWidgets.join(',');
    }}
    return 'trigger-wait-cf:' + (executed ? '1' : '0') + ':' + executedWidgets.join(',') + ':' + cf;
}}
const submitBtn = pickSubmitButton();
if (!submitBtn) return 'no-submit-button';
if (!hasResource('ValidatePassword')) {{
    triggerPasswordValidation();
    return 'wait-password-validation';
}}
if (cf.startsWith('wait-cloudflare')) {{
    // 只要存在 cf-turnstile-response 且 token 为空，就必须等待。
    // managed/flexible 模式经常没有可见 iframe，不能据此当成“无挑战”提前提交。
    return cf;
}}
if (action === 'check') return 'ready-to-submit';
submitProfileForm(submitBtn);
return 'submitted';
"""


def extract_rejected_email_domain(text, email=""):
    """从页面文案中提取被 x.ai 拒收的邮箱域名（中英文都支持）。"""
    combined = str(text or "")
    patterns = [
        # EN: email domain example.com has been rejected
        r"email\s*domain\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s+has\s+been\s+rejected",
        # EN variants
        r"domain\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s+(?:is|has been)\s+rejected",
        r"([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s+has\s+been\s+rejected",
        # ZH: 邮箱域名 xxx 已被拒绝 / 您的邮箱域名 xxx 已被拒绝
        r"邮箱域名\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*已被拒绝",
        r"域名\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*已被拒绝",
        r"([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*已被拒绝",
        # 宽松：出现“已被拒绝/拒绝”且附近有域名
        r"([A-Za-z0-9.-]+\.[A-Za-z]{2,}).{0,12}(?:已被拒绝|被拒绝|拒绝)",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return match.group(1).strip(".").lower()
    lowered = combined.lower()
    rejected_markers = (
        "has been rejected",
        "is rejected",
        "已被拒绝",
        "被拒绝",
        "请使用其他邮箱",
        "use a different email",
        "use another email",
    )
    if any(marker in combined or marker in lowered for marker in rejected_markers):
        # 优先从当前邮箱地址取后缀
        email_match = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(email or ""))
        if email_match:
            return email_match.group(1).lower()
        # 再从正文里找域名
        body_match = re.search(r"\b([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b", combined)
        if body_match:
            return body_match.group(1).lower()
    return ""


def wait_for_email_verification_step(
    page, email, timeout=20, log_callback=None, cancel_callback=None
):
    def _raise_if_domain_rejected(state):
        combined = " ".join(
            str(state.get(key) or "")
            for key in ("errorText", "bodySnippet", "raw", "title")
        )
        domain = extract_rejected_email_domain(combined, email=email)
        if domain:
            raise EmailDomainRejected(domain)

    deadline = time.time() + timeout
    last_state = {}
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        raw = page.run_js(build_email_submission_state_script(), email)
        try:
            state = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:
            state = {"step": "unknown", "raw": str(raw)}
        last_state = state
        _raise_if_domain_rejected(state)
        if state.get("step") == "otp":
            return "otp"
        error_text = str(state.get("errorText") or "").strip()
        if error_text:
            # 错误文案也可能是中文域名拒收
            domain = extract_rejected_email_domain(error_text, email=email)
            if domain:
                raise EmailDomainRejected(domain)
            raise Exception(f"x.ai 未接受该邮箱: {error_text}")
        sleep_with_cancel(0.8, cancel_callback)
    if log_callback:
        log_callback(
            "[Debug] 邮箱提交后页面状态: "
            + json.dumps(last_state, ensure_ascii=False)[:1200]
        )
    _raise_if_domain_rejected(last_state)
    # 超时兜底：正文里若有拒收语义，仍写入黑名单
    fallback = extract_rejected_email_domain(
        json.dumps(last_state, ensure_ascii=False), email=email
    )
    if fallback:
        raise EmailDomainRejected(fallback)
    raise Exception("邮箱已提交，但未进入验证码页面，x.ai 可能未发送验证码")


def wait_for_post_code_transition(
    page, email, timeout=60, log_callback=None, cancel_callback=None
):
    deadline = time.time() + timeout
    last_state = "not-started"
    # 验证成功后可能闪现错误页/入口页等瞬时过渡态；容忍连续 N 次再判失败，
    # 期间只要出现 profile-form 即成功。max_adverse_streak*~1.2s 为容忍窗口。
    adverse_streak = 0
    max_adverse_streak = 12
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        state = page.run_js(
            r"""
function visible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function resourceSummary() {
    try {
        const resources = performance.getEntriesByType('resource');
        const names = resources.map((entry) => String(entry && entry.name || ''));
        const interesting = resources.filter((entry) => {
            const name = String(entry && entry.name || '');
            return name.includes('VerifyEmailValidationCode') ||
                name.includes('ValidatePassword') ||
                /\/sign-up(?:\?|$)/.test(name) ||
                name.includes('/auth_mgmt.');
        }).slice(-8).map((entry) => {
            const name = String(entry && entry.name || '');
            let kind = 'other';
            if (name.includes('VerifyEmailValidationCode')) kind = 'verify-email';
            else if (name.includes('ValidatePassword')) kind = 'validate-password';
            else if (/\/sign-up(?:\?|$)/.test(name)) kind = 'sign-up';
            else if (name.includes('/auth_mgmt.')) kind = 'auth-mgmt';
            return {
                kind,
                responseStatus: Number(entry.responseStatus || 0),
                transferSize: Number(entry.transferSize || 0),
                encodedBodySize: Number(entry.encodedBodySize || 0),
                duration: Math.round(Number(entry.duration || 0)),
            };
        });
        return {
            verifyEmailSeen: names.some((name) => name.includes('VerifyEmailValidationCode')),
            validatePasswordSeen: names.some((name) => name.includes('ValidatePassword')),
            signupSeen: names.some((name) => /\/sign-up(?:\?|$)/.test(name)),
            authMgmtCount: names.filter((name) => name.includes('/auth_mgmt.')).length,
            matches: interesting,
            verifyEmailNet: (window.__grokNet && window.__grokNet.verifyEmail) || [],
        };
    } catch (e) {
        return {error: String(e && e.message || e).slice(0, 120)};
    }
}
function retryTarget() {
    const clickables = Array.from(document.querySelectorAll('button, [role="button"], a[href]')).filter(visible);
    const target = clickables.find((node) => {
        const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '')
            .replace(/\s+/g, '').toLowerCase();
        return text.includes('retry') || text.includes('重试') || text.includes('再试');
    });
    if (!target) return null;
    const rect = target.getBoundingClientRect();
    return {
        centerX: Math.round(rect.left + rect.width / 2),
        centerY: Math.round(rect.top + rect.height / 2),
        text: String(target.innerText || target.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    };
}
const bodyText = String(document.body?.innerText || document.body?.textContent || '').replace(/\s+/g, ' ').trim();
const compact = bodyText.toLowerCase().replace(/\s+/g, '');
const hasProfile = !!document.querySelector('input[name="givenName"], input[autocomplete="given-name"]')
    && !!document.querySelector('input[name="familyName"], input[autocomplete="family-name"]')
    && !!document.querySelector('input[name="password"], input[type="password"]');
if (hasProfile) return 'profile-form';
if (compact.includes('anerroroccurred') || compact.includes('therewasanerrorloadingthispage')) {
    return {state: 'post-code-error-page', bodySnippet: bodyText.slice(0, 240), resourceSummary: resourceSummary(), retryTarget: retryTarget()};
}
const hasEmailInput = !!Array.from(document.querySelectorAll('input[type="email"], input[name="email"], input[autocomplete="email"]')).find(visible);
if (hasEmailInput) return {state: 'post-code-email-step', bodySnippet: bodyText.slice(0, 240)};
const hasEntry = compact.includes('createyouraccount') || compact.includes('signupwithemail');
if (hasEntry) return {state: 'post-code-entry-page', bodySnippet: bodyText.slice(0, 240)};
return 'post-code-waiting';
// post-code-profile-form
            """
        )
        last_state = state
        if state == "profile-form":
            return "profile-form"
        if isinstance(state, dict):
            name = str(state.get("state") or "")
            snippet = str(state.get("bodySnippet") or "")
            if name == "post-code-error-page":
                resource_summary = state.get("resourceSummary")
                # 验证码已被服务端接受(grpc-status:0)。这个 "an error occurred"
                # 页面是成功后的瞬时过渡态，任何主动干预都会毁掉会话：
                #   - 点 "Retry" 会重启注册流程回到入口
                #   - refresh() 会丢掉存于前端内存的流程状态、回到入口
                # 对齐上游(38dc6eb 时可用)：什么都不做，容忍瞬时并继续轮询等资料页。
                adverse_streak += 1
                if adverse_streak <= max_adverse_streak:
                    if log_callback and adverse_streak == 1:
                        log_callback(
                            f"[Debug] 验证码校验后瞬时过渡页，静默等待资料页 snippet={snippet[:120]}"
                        )
                    sleep_with_cancel(1.2, cancel_callback)
                    continue
                detail = ""
                if resource_summary:
                    detail = "；资源摘要: " + json.dumps(resource_summary, ensure_ascii=False)[:500]
                raise ProfileSessionLost(f"验证码提交后 xAI 持续停在错误页: {snippet}{detail}")
            if name == "post-code-email-step":
                # 容忍过渡中的瞬时快照；仅在持续退回邮箱页时才判失败
                adverse_streak += 1
                if adverse_streak <= max_adverse_streak:
                    sleep_with_cancel(1.2, cancel_callback)
                    continue
                raise ProfileSessionLost(f"验证码提交后退回邮箱输入页，验证码会话已失效: {snippet}")
            if name == "post-code-entry-page":
                # 容忍过渡中的瞬时快照；仅在持续退回入口时才判失败
                adverse_streak += 1
                if adverse_streak <= max_adverse_streak:
                    if log_callback and adverse_streak == 1:
                        log_callback("[Debug] 验证码校验后短暂闪现入口页，继续等待资料页...")
                    sleep_with_cancel(1.2, cancel_callback)
                    continue
                raise ProfileSessionLost(f"验证码提交后退回注册入口，验证码会话已失效: {snippet}")
        else:
            adverse_streak = 0
        sleep_with_cancel(0.8, cancel_callback)
    if log_callback:
        log_callback(f"[Debug] 验证码提交后未进入资料页，最后状态: {last_state}")
    raise ProfileSessionLost("验证码提交后未进入资料页，验证码会话可能已失效")


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
    try:
        start_interval = float(normalized.get("thread_start_interval", 2.0))
    except Exception:
        start_interval = 2.0
    normalized["thread_start_interval"] = max(0.0, min(start_interval, 60.0))
    try:
        account_interval = float(normalized.get("account_interval_seconds", 12))
    except Exception:
        account_interval = 12.0
    normalized["account_interval_seconds"] = max(0.0, min(account_interval, 600.0))
    try:
        account_jitter = float(normalized.get("account_interval_jitter_seconds", 8))
    except Exception:
        account_jitter = 8.0
    normalized["account_interval_jitter_seconds"] = max(0.0, min(account_jitter, 300.0))
    normalized["stop_on_consecutive_blocks"] = _parse_positive_int(
        normalized.get("stop_on_consecutive_blocks"), 3, minimum=0, maximum=50
    )
    for bool_key in ("turnstile_patch_api", "turnstile_force_execute"):
        raw_bool = normalized.get(bool_key)
        if isinstance(raw_bool, str):
            normalized[bool_key] = raw_bool.strip().lower() in {"1", "true", "yes", "on"}
        else:
            normalized[bool_key] = bool(raw_bool)
    try:
        wait_seconds = float(normalized.get("turnstile_wait_seconds", 120) or 120)
    except Exception:
        wait_seconds = 120.0
    normalized["turnstile_wait_seconds"] = max(45.0, min(wait_seconds, 300.0))
    normalized["sub2api_concurrency"] = _parse_positive_int(
        normalized.get("sub2api_concurrency"), 3, minimum=0, maximum=1000
    )
    normalized["cpa_push_workers"] = _parse_positive_int(
        normalized.get("cpa_push_workers"), 3, minimum=1, maximum=10
    )
    normalized["sub2api_priority"] = _parse_positive_int(
        normalized.get("sub2api_priority"), 50, minimum=0, maximum=1000
    )
    auth_mode = str(normalized.get("sub2api_auth_mode") or "x-api-key").strip().lower()
    normalized["sub2api_auth_mode"] = "bearer" if auth_mode == "bearer" else "x-api-key"
    if isinstance(normalized.get("enable_nsfw"), str):
        normalized["enable_nsfw"] = normalized["enable_nsfw"].strip().lower() in {"1", "true", "yes", "on"}
    else:
        normalized["enable_nsfw"] = bool(normalized.get("enable_nsfw"))
    normalized["grok2api_auto_add_remote"] = bool(normalized.get("grok2api_auto_add_remote"))
    normalized["sub2api_auto_import_remote"] = bool(normalized.get("sub2api_auto_import_remote"))
    if isinstance(normalized.get("cpa_auto_push_remote"), str):
        normalized["cpa_auto_push_remote"] = normalized["cpa_auto_push_remote"].strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        normalized["cpa_auto_push_remote"] = bool(normalized.get("cpa_auto_push_remote"))

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
    if normalized["cpa_auto_push_remote"]:
        if not str(normalized.get("cpa_management_base") or "").strip():
            raise ValueError("CPA 自动推送需要先填写 CPA 管理地址")
        if not str(normalized.get("cpa_management_key") or "").strip():
            raise ValueError("CPA 自动推送需要先填写 CPA 管理密钥")
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
        self.fatal_error = False
        self.consecutive_blocks = 0
        self.block_stop_triggered = False
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
            consecutive_blocks = self.consecutive_blocks
            block_stop_triggered = self.block_stop_triggered
        return {
            "id": self.id,
            "status": self.status_value,
            "success_count": success_count,
            "fail_count": fail_count,
            "register_count": self.settings.get("register_count", 1),
            "register_threads": self.settings.get("register_threads", 1),
            "consecutive_blocks": consecutive_blocks,
            "block_stop_triggered": block_stop_triggered,
            "stop_requested": self.stop_requested,
            "output_file": self.output_file,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def _note_registration_outcome(self, success, error_text="", logf=None):
        threshold = int(self.settings.get("stop_on_consecutive_blocks", 3) or 0)
        with self.stats_lock:
            if success:
                self.consecutive_blocks = 0
                return False
            blocked = is_account_blocked_error(error_text)
            if blocked:
                self.consecutive_blocks += 1
            else:
                self.consecutive_blocks = 0
            hit_threshold = bool(threshold and blocked and self.consecutive_blocks >= threshold)
            if hit_threshold:
                self.block_stop_triggered = True
                self.fatal_error = True
                self.stop_requested = True
                consecutive = self.consecutive_blocks
            else:
                consecutive = self.consecutive_blocks
        if hit_threshold and logf:
            logf(
                f"[!] 连续 {consecutive} 个账号出现封禁信号，触发熔断停止剩余任务。"
                "请更换代理/出口 IP 或降低并发后重试。"
            )
        elif blocked and logf and threshold:
            logf(f"[!] 检测到账号封禁信号（连续 {consecutive}/{threshold}）")
        return hit_threshold

    def _account_pause_seconds(self):
        base = float(self.settings.get("account_interval_seconds", 12) or 0)
        jitter = float(self.settings.get("account_interval_jitter_seconds", 8) or 0)
        base = max(0.0, base)
        jitter = max(0.0, jitter)
        if base <= 0 and jitter <= 0:
            return 0.0
        delay = base
        if jitter > 0:
            delay += random.uniform(0.0, jitter)
        return max(0.0, delay)

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        profile = None
        mail_ok = False
        max_mail_retry = 3
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            try:
                email, dev_token = fill_email_and_submit(
                    log_callback=logf, cancel_callback=self.should_stop
                )
            except EmailDomainRejected as rejected:
                remembered = remember_rejected_email_domain(rejected.domain)
                if mail_try < max_mail_retry:
                    logf(
                        f"[!] 邮箱域名被 x.ai 拒收，已加入黑名单并换后缀重试: {rejected.domain}"
                        f"（当前共 {len(remembered)} 个拒收域名）"
                    )
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
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
                logf(f"[*] 验证码: {code}")
                logf("[*] 4. 填写资料")
                profile = fill_profile_and_submit(
                    log_callback=logf, cancel_callback=self.should_stop
                )
                mail_ok = True
                break
            except ProfileSessionLost as profile_exc:
                if mail_try < max_mail_retry:
                    logf(f"[!] 注册会话丢失，自动换邮箱重试: {profile_exc}")
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
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
        logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
        logf("[*] 5. 等待 sso cookie")
        sso = wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
        if self.settings.get("enable_nsfw"):
            logf("[*] 6. 开启 NSFW")
            nsfw_ok, nsfw_message = enable_nsfw_for_token(sso, log_callback=logf)
            if nsfw_ok:
                logf("[*] NSFW 已开启")
            else:
                logf(f"[!] NSFW 开启失败，继续注册流程: {nsfw_message}")
        logf("[*] 7. 获取 Refresh Token")
        refresh_token = fetch_xai_oauth_refresh_token(
            sso, log_callback=logf, cancel_callback=self.should_stop
        )
        if self.settings.get("cpa_auto_push_remote"):
            logf("[*] 8. 推送 CPA 凭证")
            try:
                cpa_result = export_and_push_cpa_credential(
                    email, refresh_token, self.settings, log_callback=logf
                )
                rotated_refresh_token = str(cpa_result.get("refresh_token") or "").strip()
                if rotated_refresh_token:
                    refresh_token = rotated_refresh_token
                if cpa_result.get("upload_error"):
                    logf(f"[!] CPA 凭证推送失败，已保留本地文件: {cpa_result['upload_error']}")
                elif cpa_result.get("uploaded"):
                    logf(f"[+] CPA 凭证已推送: {cpa_result.get('filename', '')}")
            except Exception as cpa_exc:
                logf(f"[!] CPA 凭证生成或推送失败，继续注册流程: {cpa_exc}")
        with self.stats_lock:
            source_line_no = self.success_count + 1
            self.results.append(
                {"email": email, "sso": sso, "refresh_token": refresh_token, "profile": profile}
            )
            self.success_count += 1
            line = f"{email}----{profile.get('password','')}----{sso}----{refresh_token}\n"
            try:
                with _registered_accounts_lock:
                    with open(
                        os.path.join(get_data_dir(), self.output_file),
                        "a",
                        encoding="utf-8",
                    ) as f:
                        f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        account = parse_registered_account_line(
            line,
            source=self.output_file,
            line_no=source_line_no,
            include_sso=True,
        ) or {
            "email": email,
            "sso": sso,
            "refresh_token": refresh_token,
            "has_refresh_token": bool(refresh_token),
        }
        add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        auto_push_registered_account(account, self.settings, log_callback=logf)
        self._note_registration_outcome(True, logf=logf)
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
                except EmailProviderUnavailable as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    self.fatal_error = True
                    self.stop_requested = True
                    logf(f"[!] 邮箱服务商不可用，停止剩余任务: {exc}")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    self._note_registration_outcome(False, str(exc), logf=logf)
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    should_stop_after_task = self.should_stop()
                    has_more_tasks = (not task_queue.empty()) and (not should_stop_after_task)
                    if has_more_tasks:
                        pause_seconds = self._account_pause_seconds()
                        if pause_seconds > 0:
                            logf(f"[*] 账号间隔等待 {pause_seconds:.1f}s，降低批量节奏")
                            sleep_with_cancel(pause_seconds, self.should_stop)
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
        account_interval = float(self.settings.get("account_interval_seconds", 12) or 0)
        account_jitter = float(self.settings.get("account_interval_jitter_seconds", 8) or 0)
        block_threshold = int(self.settings.get("stop_on_consecutive_blocks", 3) or 0)
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(
            f"[*] 风控节奏: 账号间隔 {account_interval:.1f}s ±{account_jitter:.1f}s，"
            f"连续封禁熔断 {block_threshold or '关闭'}，"
            f"注册后 NSFW={'开' if self.settings.get('enable_nsfw') else '关'}"
        )
        rejected_domains = list_rejected_email_domains()
        if rejected_domains:
            preview = ", ".join(rejected_domains[:12])
            extra = f" 等{len(rejected_domains)}个" if len(rejected_domains) > 12 else f" 共{len(rejected_domains)}个"
            self.log(f"[*] 已加载历史拒收邮箱后缀，将自动跳过: {preview}{extra}")
        else:
            self.log("[*] 历史拒收邮箱后缀: 无")
        self.log(f"[*] 成功账号将实时保存到: {os.path.join(get_data_dir(), self.output_file)}")
        try:
            start_interval = float(self.settings.get("thread_start_interval", 2.0))
        except Exception:
            start_interval = 2.0
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
            if self.fatal_error:
                self.status_value = "failed"
            elif self.stop_requested:
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
    raise Exception(f"YYDS 创建邮箱失败: {data}")


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
    raise Exception(f"YYDS 获取 token 失败: {data}")


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
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    candidates = [
        d for d in domains
        if not is_email_domain_rejected(d.get("domain"))
    ]
    if not candidates:
        rejected = sorted(
            {
                str(d.get("domain") or "").strip().lower()
                for d in domains
                if d.get("domain")
            }
        )
        raise EmailProviderUnavailable(f"YYDS 可用域名已被 x.ai 拒收: {', '.join(rejected)}")
    private = [d for d in candidates if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return pick_rotating_domain(private, "_yyds_domain_index")
    public = [d for d in candidates if d.get("isVerified") and d.get("isPublic")]
    if public:
        return pick_rotating_domain(public, "_yyds_domain_index")
    verified = [d for d in candidates if d.get("isVerified")]
    if verified:
        return pick_rotating_domain(verified, "_yyds_domain_index")
    raise Exception("YYDS 无已验证域名可用")


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
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
    resend_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    next_resend_at = time.time() + 60
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
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
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
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
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
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def normalize_rejected_email_domain(domain):
    """只规范化为精确邮箱后缀，不自动提升父域。

    例：user@07210d00.dpdns.org -> 07210d00.dpdns.org
    不会额外写入 dpdns.org / eu.org。
    """
    normalized = str(domain or "").strip().lower().lstrip("@.")
    if not normalized:
        return ""
    if "@" in normalized:
        normalized = normalized.split("@", 1)[1].strip().lower().lstrip(".")
    # 去掉尾部点
    normalized = normalized.strip(".")
    if not normalized or "." not in normalized:
        return ""
    # 过滤明显不是域名的内容
    if any(ch.isspace() for ch in normalized):
        return ""
    return normalized


def rejected_email_domain_variants(domain):
    """兼容旧调用：现在只返回精确域名集合。"""
    exact = normalize_rejected_email_domain(domain)
    return {exact} if exact else set()


def load_rejected_email_domains(force=False):
    """从 data 目录加载历史拒收域名，进程内缓存。"""
    global _rejected_email_domains
    path = get_rejected_email_domains_file()
    with _rejected_email_domains_lock:
        loaded = set()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if isinstance(raw, list):
                    items = raw
                elif isinstance(raw, dict):
                    items = raw.get("domains") or raw.get("rejected") or []
                else:
                    items = []
                for item in items:
                    exact = normalize_rejected_email_domain(item)
                    if exact:
                        loaded.add(exact)
            except Exception:
                loaded = set()
        if force:
            _rejected_email_domains = loaded
        else:
            _rejected_email_domains |= loaded
        return set(_rejected_email_domains)


def save_rejected_email_domains():
    path = get_rejected_email_domains_file()
    with _rejected_email_domains_lock:
        domains = sorted(_rejected_email_domains)
    payload = {
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "domains": domains,
    }
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".rejected-domains-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    return path


def remember_rejected_email_domain(domain):
    """只记录被拒的精确后缀，不自动记父域。"""
    load_rejected_email_domains()
    exact = normalize_rejected_email_domain(domain)
    if not exact:
        return set()
    with _rejected_email_domains_lock:
        before = set(_rejected_email_domains)
        _rejected_email_domains.add(exact)
        changed = _rejected_email_domains != before
        snapshot = set(_rejected_email_domains)
    if changed:
        try:
            save_rejected_email_domains()
        except Exception:
            pass
    return snapshot


def is_email_domain_rejected(domain):
    """精确匹配黑名单。

    - 存的是 07210d00.dpdns.org 时，只跳过这个后缀
    - 若将来主域 dpdns.org 自己也被拒并写入，则 foo.dpdns.org 会因后缀命中而跳过
    """
    load_rejected_email_domains()
    normalized = normalize_rejected_email_domain(domain)
    if not normalized:
        return False
    with _rejected_email_domains_lock:
        if normalized in _rejected_email_domains:
            return True
        # 仅当黑名单里显式存在父域时，才拦截其子域
        for rejected in _rejected_email_domains:
            if normalized.endswith("." + rejected):
                return True
    return False


def list_rejected_email_domains():
    load_rejected_email_domains()
    with _rejected_email_domains_lock:
        return sorted(_rejected_email_domains)


def pick_rotating_domain(candidates, index_name):
    if not candidates:
        return None
    current = int(globals().get(index_name, 0) or 0)
    domain = candidates[current % len(candidates)].get("domain")
    globals()[index_name] = current + 1
    return domain


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    candidates = [
        d for d in domains
        if not is_email_domain_rejected(d.get("domain"))
    ]
    if not candidates:
        rejected = sorted(
            {
                str(d.get("domain") or "").strip().lower()
                for d in domains
                if d.get("domain")
            }
        )
        raise EmailProviderUnavailable(f"DuckMail 可用域名已被 x.ai 拒收: {', '.join(rejected)}")
    private = [d for d in candidates if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return pick_rotating_domain(verified_private, "_cf_domain_index")
    public = [d for d in candidates if d.get("isVerified")]
    if public:
        return pick_rotating_domain(public, "_cf_domain_index")
    raise Exception("DuckMail 无已验证域名可用")


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
        available = [d for d in domains if not is_email_domain_rejected(d)]
        if not available:
            raise EmailProviderUnavailable(
                "CloudMail defaultDomains 中的域名均已被 x.ai 拒收: " + ", ".join(domains)
            )
        global _cf_domain_index
        domain = available[_cf_domain_index % len(available)]
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
            address, token = cloudflare_create_temp_address(api_base)
            # 若历史黑名单命中，直接换兜底域名创建，避免重复撞同一拒收后缀
            if address and is_email_domain_rejected(address):
                raise Exception(f"临时邮箱域名已在拒收黑名单: {address}")
            return address, token
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [
                d for d in domains
                if d.get("isVerified") and not is_email_domain_rejected(d.get("domain"))
            ]
            candidates = verified or [
                d for d in domains if not is_email_domain_rejected(d.get("domain"))
            ]
            if not candidates:
                raise EmailProviderUnavailable(
                    f"Cloudflare 可用域名均已被 x.ai 拒收（含历史黑名单）: {primary_exc}"
                )
            target = candidates[0]
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
            resend_callback=resend_callback,
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


def override_user_agent_for_docker(page, log_callback=None):
    """Docker 中将 Linux UA 覆盖为 Windows UA（保持 Chrome 版本一致），同时覆盖 HTTP 头和 JS 层。"""
    if not page or not _env_truthy("GROK_REG_IN_DOCKER"):
        return
    try:
        actual_ua = page.run_js("return navigator.userAgent;") or ""
        if not actual_ua:
            return
        # 仅替换平台部分，保持 Chrome 版本号一致
        windows_ua = actual_ua.replace("X11; Linux x86_64", "Windows NT 10.0; Win64; x64")
        if windows_ua == actual_ua:
            return  # 不是 Linux UA，无需修改
        # CDP 覆盖 HTTP 头中的 UA
        page.run_cdp("Network.setUserAgentOverride", userAgent=windows_ua)
        if log_callback:
            log_callback(f"[Debug] UA 已覆盖为 Windows: {windows_ua}")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] UA 覆盖失败: {str(exc)[:160]}")


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
            # Docker 中先覆盖 UA（HTTP 头 + JS 层），再装 stealth
            try:
                override_user_agent_for_docker(page, log_callback=log_callback)
            except Exception:
                pass
            # 启动时只装轻量 stealth，绝不补丁 turnstile API。
            # pageHook（补丁 window.turnstile）默认关闭，手动能过时补丁反而容易干扰 flexible 模式。
            try:
                install_light_stealth_script(page, log_callback=log_callback)
            except Exception:
                pass
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback:
                proxy = normalize_proxy_for_runtime(config.get("proxy", ""))
                mode = "headless" if should_run_headless() else "visible"
                extension_loaded = os.path.isdir(EXTENSION_PATH)
                log_callback(
                    f"[Debug] 浏览器模式: {mode}，代理: {proxy or '直连'}，"
                    f"Turnstile扩展路径: {'存在' if extension_loaded else '未找到'}，"
                    f"API补丁: {'开' if config.get('turnstile_patch_api') else '关'}，"
                    f"强制execute: {'开' if config.get('turnstile_force_execute') else '关'}"
                )
            probe_browser_stealth(page, log_callback=log_callback)
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
            page = browser.new_tab()
            _set_page(page)
            page.get(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            browser, _ = restart_browser()
            page = browser.new_tab()
            _set_page(page)
            page.get(SIGNUP_URL)
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


def fill_email_and_submit(timeout=30, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_state = "not-started"
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            build_email_form_script("fill"),
            email,
        )
        last_state = str(filled)
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
            build_email_form_script("submit"),
            email,
        )
        last_state = str(clicked)
        if clicked is True:
            wait_for_email_verification_step(
                page,
                email,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
            if log_callback:
                log_callback(f"[*] 已填写邮箱并点击注册: {email}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if log_callback:
        try:
            diag = page.run_js(build_email_form_script("diagnose"), email)
        except Exception as diag_exc:
            diag = f"诊断失败: {diag_exc}"
        log_callback(f"[Debug] 邮箱表单诊断: last_state={last_state}; {diag}")
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

    # 不要在 OTP 页安装 Turnstile pageHook。
    # 验证码接口 200 后 xAI 前端路由对脚本注入很敏感，过早 hook 会导致
    # 持续停在 "An error occurred" 过渡/错误页，无法进入资料页。
    # Turnstile 仅存在于资料页，延后到 fill_profile_and_submit 再注入。

    # 在填充/提交前启动 CDP 网络监听，无论请求经由 fetch/XHR/worker 发出，
    # 都能截获 VerifyEmailValidationCode 的响应体，定位服务端拒绝原因。
    listen_started = False
    try:
        page.listen.start("VerifyEmailValidationCode", method="POST")
        listen_started = True
    except Exception as listen_exc:
        if log_callback:
            log_callback(f"[Debug] 启动 verify-email 网络监听失败: {str(listen_exc)[:160]}")

    def _log_verify_packet():
        if not listen_started or not log_callback:
            return
        try:
            packet = page.listen.wait(count=1, timeout=1.5, fit_count=False)
        except Exception:
            packet = None
        if not packet:
            return
        packets = packet if isinstance(packet, (list, tuple)) else [packet]
        for pkt in packets:
            try:
                resp = getattr(pkt, "response", None)
                req = getattr(pkt, "request", None)
                body = getattr(resp, "body", "") if resp else ""
                status = getattr(resp, "status", "") if resp else ""
                post = getattr(req, "postData", "") if req else ""
                log_callback(
                    "[Debug] verify-email CDP 抓包: "
                    + json.dumps(
                        {
                            "status": status,
                            "reqBody": str(post)[:400],
                            "respBody": str(body)[:600],
                        },
                        ensure_ascii=False,
                    )[:1200]
                )
            except Exception as pkt_exc:
                log_callback(f"[Debug] verify-email 抓包解析失败: {str(pkt_exc)[:160]}")

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        # 上游验证码填充（JS + _valueTracker 同步）为主路径：已验证能正确驱动
        # input-otp 受控组件的 onComplete；CDP 原生输入仅作兜底，避免其绕过
        # React 状态导致自动提交携带无效值被 verify-email 拒绝。
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

        # JS 主路径未成功时，回退到 CDP 原生按键输入
        if filled == "not-ready" or "failed" in str(filled):
            native_state = _fill_otp_code_native(page, clean_code, cancel_callback=cancel_callback)
            if isinstance(native_state, dict) and native_state.get("nativeInput"):
                filled = "filled-native"
                if log_callback:
                    log_callback(
                        "[Debug] 验证码 JS 填充未就绪，回退 CDP 原生输入: "
                        + json.dumps(native_state, ensure_ascii=False)[:500]
                    )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if log_callback:
            log_callback("[Debug] 验证码已填入，等待前端状态同步...")
        sleep_with_cancel(0.6, cancel_callback)

        native_submit = _click_otp_submit_native(page)
        if isinstance(native_submit, dict) and native_submit.get("nativeClicked"):
            clicked = "clicked"
            if log_callback:
                log_callback(
                    "[Debug] 验证码提交按钮已通过 CDP 原生点击: "
                    + json.dumps(native_submit, ensure_ascii=False)[:500]
                )
        else:
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

        if clicked == "clicked":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            _log_verify_packet()
            if listen_started:
                try: page.listen.stop()
                except Exception: pass
            wait_for_post_code_transition(
                page,
                email,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
            return code
        if clicked == "no-button":
            if log_callback:
                log_callback("[Debug] 验证码提交按钮未出现，等待前端自动提交结果...")
            _log_verify_packet()
            if listen_started:
                try: page.listen.stop()
                except Exception: pass
            wait_for_post_code_transition(
                page,
                email,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
            return code

        sleep_with_cancel(0.5, cancel_callback)

    if listen_started:
        try: page.listen.stop()
        except Exception: pass
    raise Exception("验证码已获取，但自动填写/提交失败")


def _read_turnstile_token_from_page(page):
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
        return str(token or "").strip()
    except Exception:
        return ""


def _parse_element_rect(box):
    """兼容 DrissionPage rect 的多种返回形态：对象 / dict / (x,y,w,h) tuple。"""
    if box is None:
        return None
    try:
        # 对象形态：.location / .size 或 .x/.y/.width/.height
        loc = getattr(box, "location", None)
        size = getattr(box, "size", None)
        if isinstance(loc, dict) or isinstance(size, dict):
            x = float((loc or {}).get("x") or getattr(box, "x", 0) or 0)
            y = float((loc or {}).get("y") or getattr(box, "y", 0) or 0)
            w = float((size or {}).get("width") or getattr(box, "width", 0) or 0)
            h = float((size or {}).get("height") or getattr(box, "height", 0) or 0)
            return {"x": x, "y": y, "width": w, "height": h}
        if all(hasattr(box, attr) for attr in ("x", "y", "width", "height")):
            return {
                "x": float(box.x or 0),
                "y": float(box.y or 0),
                "width": float(box.width or 0),
                "height": float(box.height or 0),
            }
    except Exception:
        pass
    # tuple/list: (x, y, w, h) 或 (x1,y1,x2,y2)
    if isinstance(box, (tuple, list)):
        nums = [float(v) for v in box[:4]]
        if len(nums) >= 4:
            # JS getBoundingClientRect 导出为 [left, top, width, height]
            return {"x": nums[0], "y": nums[1], "width": nums[2], "height": nums[3]}
    if isinstance(box, dict):
        if "location" in box or "size" in box:
            loc = box.get("location") or {}
            size = box.get("size") or {}
            return {
                "x": float(loc.get("x") or box.get("x") or 0),
                "y": float(loc.get("y") or box.get("y") or 0),
                "width": float(size.get("width") or box.get("width") or 0),
                "height": float(size.get("height") or box.get("height") or 0),
            }
        return {
            "x": float(box.get("x") or 0),
            "y": float(box.get("y") or 0),
            "width": float(box.get("width") or box.get("w") or 0),
            "height": float(box.get("height") or box.get("h") or 0),
        }
    return None


def _safe_element_click(el, actions=None, label="element"):
    """兼容 ChromiumElement / ChromiumFrame 的点击。"""
    actions = actions if actions is not None else []
    if el is None:
        return False
    # 1) 标准 click
    for kwargs in ({"by_js": False}, {}, {"by_js": True}):
        try:
            if kwargs:
                el.click(**kwargs)
            else:
                el.click()
            actions.append(f"clicked-{label}")
            return True
        except TypeError:
            try:
                el.click()
                actions.append(f"clicked-{label}")
                return True
            except Exception:
                continue
        except Exception as exc:
            actions.append(f"click-{label}-fail:{type(el).__name__}:{exc}")
            break
    # 2) 某些 Frame 只有 .click.left
    try:
        click_obj = getattr(el, "click", None)
        if click_obj is not None and hasattr(click_obj, "left"):
            click_obj.left()
            actions.append(f"clicked-{label}-left")
            return True
    except Exception as exc:
        actions.append(f"click-{label}-left-fail:{exc}")
    return False


def _is_page_absolute_turnstile_rect(rect):
    """过滤 iframe 内部相对坐标（Docker 里常见 x=25,y=32 这种误点）。"""
    if not rect:
        return False
    x = float(rect.get("x") or 0)
    y = float(rect.get("y") or 0)
    w = float(rect.get("width") or 0)
    h = float(rect.get("height") or 0)
    if w < 80 or h < 20:
        return False
    # 页面中部/下方的 checkbox 条，不太可能贴在 (0,0) 附近
    if x < 40 and y < 40 and w < 120:
        return False
    # 明显在可视区域内
    if x > 2500 or y > 2500:
        return False
    return True


def _locate_turnstile_box_on_page(page):
    """在主文档坐标系下找 Turnstile 可见区域（页面绝对坐标）。"""
    try:
        raw = page.run_js(
            r"""
function boxOf(node) {
  if (!node || !node.getBoundingClientRect) return null;
  const r = node.getBoundingClientRect();
  if (r.width < 40 || r.height < 16) return null;
  return [r.left, r.top, r.width, r.height];
}
// 1) 从 hidden input 向上找合适容器
const input = document.querySelector('input[name="cf-turnstile-response"]');
if (input) {
  let n = input;
  for (let i = 0; i < 10 && n; i++) {
    const b = boxOf(n);
    if (b && b[2] >= 180 && b[2] <= 560 && b[3] >= 36 && b[3] <= 120) return {via:'input-parent', box:b};
    n = n.parentElement;
  }
}
// 2) 标准 host
const hosts = Array.from(document.querySelectorAll('div.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], iframe[title*="Cloudflare"], iframe[title*="小部件"]'));
for (const h of hosts) {
  const b = boxOf(h);
  if (b) return {via:'host', box:b, tag:h.tagName};
}
// 3) 开放 shadow 里的 iframe
function walk(root, depth) {
  if (!root || depth > 6) return null;
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return null; }
  for (const node of nodes) {
    try {
      if (node.shadowRoot) {
        const hit = walk(node.shadowRoot, depth + 1);
        if (hit) return hit;
      }
    } catch (e) {}
    if (String(node.tagName||'').toLowerCase() === 'iframe') {
      const b = boxOf(node);
      if (b && b[2] >= 180 && b[2] <= 560 && b[3] >= 36 && b[3] <= 120) {
        return {via:'open-shadow-iframe', box:b};
      }
    }
  }
  return null;
}
const shadowHit = walk(document, 0);
if (shadowHit) return shadowHit;
// 4) 任意“checkbox 条”尺寸元素（靠近密码框下方优先）
const password = document.querySelector('input[type="password"], input[name="password"]');
const py = password ? password.getBoundingClientRect().bottom : 0;
let best = null;
for (const node of Array.from(document.querySelectorAll('div,iframe,section'))) {
  const b = boxOf(node);
  if (!b || b[2] < 200 || b[2] > 520 || b[3] < 40 || b[3] > 90) continue;
  const score = Math.abs((b[1] + b[3]/2) - (py || b[1]));
  if (!best || score < best.score) best = {via:'sized', box:b, score};
}
return best;
            """
        )
    except Exception as exc:
        return {"error": str(exc)[:160]}
    if not isinstance(raw, dict):
        return None
    box = _parse_element_rect(raw.get("box"))
    if not _is_page_absolute_turnstile_rect(box):
        return {"raw": raw, "rejected_box": box}
    return {"via": raw.get("via"), "box": box}


def _cdp_click_page_box_left(page, box, actions=None, label="page-box"):
    actions = actions if actions is not None else []
    if not _is_page_absolute_turnstile_rect(box):
        actions.append(f"skip-bad-box-{label}:{box}")
        return False
    x = int(box["x"] + min(max(16, box["width"] * 0.12), 36))
    y = int(box["y"] + max(box["height"] / 2, 12))
    # 再保险：拒绝贴边误点
    if x < 30 and y < 30:
        actions.append(f"skip-corner-{label}:{x},{y}")
        return False
    try:
        _click_point_on_page(page, x, y)
        actions.append(f"cdp-click-{label}:{x},{y}")
        sleep_with_cancel(0.15)
        _click_point_on_page(page, x + 2, y)
        actions.append(f"cdp-click2-{label}:{x+2},{y}")
        return True
    except Exception as exc:
        actions.append(f"cdp-click-{label}-fail:{exc}")
        return False


def _cdp_click_element_left(page, el, actions=None, label="element"):
    """仅在能拿到页面绝对坐标时 CDP 点击；拒绝 iframe 内相对坐标。"""
    actions = actions if actions is not None else []
    if el is None:
        return False
    rect = None
    for getter in (
        lambda: getattr(el, "rect", None),
        lambda: el.run_js(
            "const r=this.getBoundingClientRect();"
            "return [r.left + (window.scrollX||0), r.top + (window.scrollY||0), r.width, r.height];"
        )
        if hasattr(el, "run_js")
        else None,
    ):
        try:
            raw = getter()
            rect = _parse_element_rect(raw)
            if rect and rect.get("width", 0) > 1:
                break
        except Exception as exc:
            actions.append(f"rect-{label}-fail:{exc}")
            rect = None
    if not _is_page_absolute_turnstile_rect(rect):
        # 元素坐标不可信（常见于 iframe 内 shadow input），改用主文档定位
        actions.append(f"untrusted-rect-{label}:{rect}")
        located = _locate_turnstile_box_on_page(page)
        if isinstance(located, dict) and located.get("box"):
            return _cdp_click_page_box_left(page, located["box"], actions, f"{label}-via-page")
        actions.append(f"no-page-box-{label}:{located}")
        return False
    return _cdp_click_page_box_left(page, rect, actions, label)


def _click_turnstile_via_shadow_dom(page, log_callback=None):
    """参考 grok-auto-register：通过 DrissionPage shadow_root 点 Cloudflare 复选框。

    Docker 中 element.click() 常“假成功”不出 token，因此 shadow 点选后
    必须再补一次 CDP 坐标点击。
    """
    actions = []
    try:
        challenge_input = None
        for locator in (
            "@name=cf-turnstile-response",
            'css:input[name="cf-turnstile-response"]',
            "css:input[name*=turnstile]",
        ):
            try:
                challenge_input = page.ele(locator, timeout=0.3)
            except Exception:
                challenge_input = None
            if challenge_input:
                actions.append(f"found-input:{locator}")
                break

        iframe = None
        btn = None

        if challenge_input:
            wrapper = challenge_input
            for depth in range(0, 6):
                try:
                    if hasattr(wrapper, "shadow_root") and wrapper.shadow_root:
                        try:
                            iframe = wrapper.shadow_root.ele("tag:iframe", timeout=0.3)
                        except Exception:
                            iframe = None
                        if iframe:
                            actions.append(f"iframe-via-shadow-depth:{depth}")
                            break
                except Exception:
                    pass
                try:
                    parent = wrapper.parent()
                except Exception:
                    parent = None
                if not parent:
                    break
                wrapper = parent

        if not iframe:
            for locator in (
                "tag:iframe@src():turnstile",
                "tag:iframe@src():challenges.cloudflare.com",
                "css:iframe[src*='turnstile']",
                "css:iframe[src*='challenges.cloudflare.com']",
                "css:iframe[title*='Cloudflare']",
                "css:iframe[title*='小部件']",
                "css:div.cf-turnstile iframe",
                "css:[data-sitekey] iframe",
            ):
                try:
                    iframe = page.ele(locator, timeout=0.25)
                except Exception:
                    iframe = None
                if iframe:
                    actions.append(f"iframe-via:{locator}")
                    break

        if not iframe:
            try:
                frames = page.eles("tag:iframe") or []
                for fr in frames[:16]:
                    try:
                        rect = _parse_element_rect(getattr(fr, "rect", None))
                        w = float((rect or {}).get("width") or 0)
                        h = float((rect or {}).get("height") or 0)
                    except Exception:
                        w = h = 0
                    if 180 <= w <= 560 and 36 <= h <= 120:
                        iframe = fr
                        actions.append(f"iframe-via-size:{int(w)}x{int(h)}")
                        break
            except Exception as exc:
                actions.append(f"iframe-scan-fail:{exc}")

        if not iframe and not challenge_input:
            return {"ok": False, "actions": actions or ["no-cf-input"]}

        # 参考项目：伪造 screenX/screenY
        if iframe is not None:
            try:
                iframe.run_js(
                    """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
try {
  Object.defineProperty(MouseEvent.prototype, 'screenX', { get: function(){ return sx; } });
  Object.defineProperty(MouseEvent.prototype, 'screenY', { get: function(){ return sy; } });
} catch (e) {}
                    """
                )
                actions.append("patched-mouse-screen")
            except Exception as exc:
                actions.append(f"patch-mouse-fail:{exc}")

        clicked = False
        # 1) iframe body shadow_root 内 checkbox（本地桌面最有效）
        if iframe is not None:
            try:
                body = iframe.ele("tag:body", timeout=0.8)
                body_sr = getattr(body, "shadow_root", None) if body else None
                if body_sr:
                    for sel in (
                        "tag:input",
                        "css:input[type=checkbox]",
                        "css:input",
                        "css:.cb-lb",
                        "css:label",
                        "css:[role=checkbox]",
                    ):
                        try:
                            btn = body_sr.ele(sel, timeout=0.25)
                        except Exception:
                            btn = None
                        if btn:
                            actions.append(f"btn:{sel}")
                            break
                if btn is not None:
                    if _safe_element_click(btn, actions, "shadow-input"):
                        clicked = True
            except Exception as exc:
                actions.append(f"shadow-click-fail:{exc}")

        # 2) 主文档坐标系下定位 widget 后 CDP 点左侧
        # 关键：禁止使用 iframe 内相对坐标（Docker 曾误点 25,32 把页面点飞）
        located = _locate_turnstile_box_on_page(page)
        if isinstance(located, dict) and located.get("box"):
            actions.append(f"page-box:{located.get('via')}:{located.get('box')}")
            if _cdp_click_page_box_left(page, located["box"], actions, "page-widget"):
                clicked = True
        else:
            actions.append(f"page-box-miss:{located}")

        # 3) 仅当元素 rect 是页面绝对坐标时，才对 host 再点
        if challenge_input is not None:
            try:
                host = challenge_input.parent()
            except Exception:
                host = challenge_input
            if _cdp_click_element_left(page, host, actions, "host"):
                clicked = True

        # 4) 不要对 ChromiumFrame 调 .click()；坐标不可信时跳过
        # 5) 全局 CDP frame-owner 兜底（about:blank）
        if not clicked:
            try:
                cdp_info = _click_turnstile_challenge_if_visible(page)
                actions.append(
                    "cdp-global:"
                    + json.dumps(cdp_info if isinstance(cdp_info, dict) else {"raw": str(cdp_info)}, ensure_ascii=False)[:180]
                )
                if isinstance(cdp_info, dict) and cdp_info.get("nativeClicked"):
                    # 若坐标看起来像左上角误点，视为失败
                    cx = int(cdp_info.get("x") or 0)
                    cy = int(cdp_info.get("y") or 0)
                    if cx < 40 and cy < 40:
                        actions.append(f"cdp-global-rejected-corner:{cx},{cy}")
                    else:
                        clicked = True
            except Exception as exc:
                actions.append(f"cdp-global-fail:{exc}")

        return {"ok": clicked, "actions": actions}
    except Exception as exc:
        return {"ok": False, "actions": actions + [f"fatal:{exc}"]}


def getTurnstileToken(log_callback=None, cancel_callback=None, attempts=15):
    """参考 grok-auto-register 的 Turnstile 复用：shadow_root 点选 + 读 token。"""
    page = _get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    for i in range(max(1, int(attempts))):
        raise_if_cancelled(cancel_callback)
        token = _read_turnstile_token_from_page(page)
        if len(token) >= 80:
            if log_callback:
                log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
            return token

        click_info = _click_turnstile_via_shadow_dom(page, log_callback=log_callback)
        if log_callback:
            log_callback(
                f"[*] Turnstile shadow 点击尝试 #{i+1}: "
                + json.dumps(click_info, ensure_ascii=False)[:500]
            )

        # 点完多等一会：Docker 里 CF 出 token 更慢
        sleep_with_cancel(1.2 if i == 0 else 1.0, cancel_callback)
        token = _read_turnstile_token_from_page(page)
        if len(token) >= 80:
            if log_callback:
                log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
            return token

        # 若“点成功”但 token 仍空，再强制 CDP 全局点一次
        if isinstance(click_info, dict) and click_info.get("ok") and not token:
            try:
                cdp_info = _click_turnstile_challenge_if_visible(page)
                if log_callback:
                    log_callback(
                        "[*] 点击后 token 仍为空，CDP 再点: "
                        + json.dumps(cdp_info if isinstance(cdp_info, dict) else {"raw": str(cdp_info)}, ensure_ascii=False)[:300]
                    )
                sleep_with_cancel(1.0, cancel_callback)
                token = _read_turnstile_token_from_page(page)
                if len(token) >= 80:
                    if log_callback:
                        log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                    return token
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] CDP 再点失败: {exc}")

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
    patch_api = bool(config.get("turnstile_patch_api"))
    force_execute = bool(config.get("turnstile_force_execute"))
    try:
        wait_limit = float(config.get("turnstile_wait_seconds", 120) or 120)
    except Exception:
        wait_limit = 120.0
    wait_limit = max(45.0, min(wait_limit, 300.0))
    # 默认不补丁 turnstile API。手动浏览器能过时，API hook 往往是负优化。
    if patch_api:
        install_turnstile_page_hook(page, log_callback=log_callback)
        if log_callback:
            log_callback("[Debug] 已按配置安装 turnstile API pageHook")
    else:
        install_light_stealth_script(page, log_callback=log_callback)
    # 预热 Turnstile：完全交给 Cloudflare 被动评分，不要一上来 execute。
    if log_callback:
        log_callback(
            f"[*] 预热 Turnstile（被动评分，API补丁={'开' if patch_api else '关'}，"
            f"强制execute={'开' if force_execute else '关'}，最长等待 {wait_limit:.0f}s）..."
        )
    humanize_page_activity(page, log_callback=log_callback, cancel_callback=cancel_callback)
    sleep_with_cancel(5, cancel_callback)
    # 预热后先采集一次 Turnstile 结构，便于判断是 IP 信誉还是自动化指纹问题
    if log_callback:
        try:
            warm_diag = page.run_js(build_profile_submit_script("diagnose"))
            warm_obj = json.loads(warm_diag) if isinstance(warm_diag, str) else warm_diag
            token_now = read_turnstile_token_len(page)
            log_callback(
                f"[Debug] 预热后 Turnstile 状态: {json.dumps(warm_obj.get('turnstile', {}), ensure_ascii=False)} "
                f"tokenLen={token_now}"
            )
        except Exception as warm_exc:
            log_callback(f"[Debug] 预热诊断失败: {warm_exc}")
    deadline = time.time() + max(timeout, wait_limit + 30)
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0
    last_humanize_at = 0.0
    cf_wait_log_state = {}
    error_page_retries = 0
    max_error_page_retries = 4
    last_error_page_retry_at = 0.0
    entry_page_retries = 0
    max_entry_page_retries = 2
    last_entry_page_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        now = time.time()
        if now - last_error_page_retry_at >= 2.0:
            try:
                error_state = page.run_js(build_profile_submit_script("retry_error"))
            except Exception as error_retry_exc:
                error_state = f"profile-error-check-failed:{error_retry_exc}"

            if isinstance(error_state, dict) and str(error_state.get("state") or "") in {
                "profile-error-retry-target",
                "profile-error-page-no-retry",
            }:
                if error_state.get("state") == "profile-error-retry-target":
                    error_page_retries += 1
                    if error_page_retries > max_error_page_retries:
                        raise Exception(
                            f"xAI 最终注册页连续返回错误页，已重试 {max_error_page_retries} 次仍未恢复"
                        )
                    if error_state.get("centerX") is not None and error_state.get("centerY") is not None:
                        try:
                            _dispatch_cdp_click(
                                page,
                                int(error_state.get("centerX")),
                                int(error_state.get("centerY")),
                                include_keyboard=False,
                            )
                            error_state["nativeClicked"] = True
                        except Exception as native_exc:
                            error_state["nativeClickError"] = str(native_exc)[:160]
                    if log_callback:
                        log_callback(
                            f"[*] 最终注册页错误页，点击 Retry 重试 ({error_page_retries}/{max_error_page_retries})"
                        )
                        log_callback(f"[Debug] 最终注册页错误页状态: {json.dumps(error_state, ensure_ascii=False)}")
                    last_error_page_retry_at = now
                    sleep_with_cancel(2, cancel_callback)
                    try:
                        refresh_active_page()
                        page = _get_page()
                    except Exception:
                        pass
                    continue
                if error_state.get("state") == "profile-error-page-no-retry":
                    raise Exception(f"xAI 最终注册页错误页且未找到 Retry 按钮: {error_state.get('bodySnippet', '')}")

        now = time.time()
        if now - last_entry_page_retry_at >= 2.0:
            try:
                entry_state = page.run_js(build_profile_submit_script("recover_entry"))
            except Exception as entry_retry_exc:
                entry_state = f"profile-entry-check-failed:{entry_retry_exc}"

            if isinstance(entry_state, dict) and str(entry_state.get("state") or "") in {
                "profile-entry-email-target",
                "profile-entry-page-no-email",
            }:
                if entry_state.get("state") == "profile-entry-email-target":
                    entry_page_retries += 1
                    if entry_page_retries > max_entry_page_retries:
                        raise ProfileSessionLost(
                            f"xAI 最终注册页反复退回注册入口，已尝试恢复 {max_entry_page_retries} 次仍未进入资料页"
                        )
                    if entry_state.get("centerX") is not None and entry_state.get("centerY") is not None:
                        try:
                            _dispatch_cdp_click(
                                page,
                                int(entry_state.get("centerX")),
                                int(entry_state.get("centerY")),
                                include_keyboard=False,
                            )
                            entry_state["nativeClicked"] = True
                        except Exception as native_exc:
                            entry_state["nativeClickError"] = str(native_exc)[:160]
                    if log_callback:
                        log_callback(
                            f"[*] 最终注册页退回注册入口，点击邮箱注册恢复 ({entry_page_retries}/{max_entry_page_retries})"
                        )
                        log_callback(f"[Debug] 最终注册页入口恢复状态: {json.dumps(entry_state, ensure_ascii=False)}")
                    last_entry_page_retry_at = now
                    sleep_with_cancel(2, cancel_callback)
                    try:
                        refresh_active_page()
                        page = _get_page()
                    except Exception:
                        pass
                    continue
                if entry_state.get("state") == "profile-entry-page-no-email":
                    raise ProfileSessionLost(f"xAI 最终注册页退回注册入口且未找到邮箱注册按钮: {entry_state.get('bodySnippet', '')}")

        # 资料已填过，且表单已从页面消失 => 提交已被隐形 Turnstile 驱动成功，页面已推进
        if form_filled_once:
            try:
                progressed = page.run_js(
                    """
try {
  const pwd = document.querySelector('input[name="password"], input[type="password"]');
  const given = document.querySelector('input[name="givenName"], input[autocomplete="given-name"]');
  return (!pwd && !given) ? 'gone' : 'present';
} catch (e) { return 'present'; }
                    """
                )
            except Exception:
                progressed = "present"
            if progressed == "gone":
                if log_callback:
                    log_callback(f"[*] 注册资料已提交，页面已跳转: {given_name} {family_name}")
                return {"given_name": given_name, "family_name": family_name, "password": password}
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

if (!givenInput || !familyInput || !passwordInput) {
    const emailInput = pickInput('input[type="email"], input[name="email"], input[autocomplete="email"]');
    if (emailInput) return 'email-step';
    return 'not-ready';
}

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

return 'profile-filled';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                token_len = str(read_turnstile_token_len(page) or (filled.split(":", 1)[1] if ":" in filled else "0"))
                if log_callback and should_log_cloudflare_wait(cf_wait_log_state, "profile-fill", token_len):
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                if int(float(token_len or 0)) >= 80:
                    # token 已到，下一轮走提交
                    sleep_with_cancel(0.3, cancel_callback)
                    continue
                if now - wait_cf_since >= wait_limit and str(token_len) in {"0", ""}:
                    raise Exception(
                        f"Cloudflare Turnstile {wait_limit:.0f}s 内未签发 token（token长度=0）。"
                        "已尝试 shadow_root 点选（参考 grok-auto-register）；若仍失败请人工确认页面复选框是否可点"
                    )
                # 参考 grok-auto-register：token 为空时短暂停顿 + shadow DOM 点选复用
                if str(token_len) in {"0", ""}:
                    pause_seconds = random.uniform(1.0, 2.5)
                    if log_callback and should_log_cloudflare_wait(cf_wait_log_state, "profile-pause", "0"):
                        log_callback(f"[*] Cloudflare token 为空，暂停 {pause_seconds:.1f}s 后 shadow 点选")
                    sleep_with_cancel(pause_seconds, cancel_callback)
                if now - last_humanize_at >= 5:
                    humanize_page_activity(page, log_callback=None, cancel_callback=cancel_callback)
                    last_humanize_at = now
                if now - last_cf_retry_at >= 2.0:
                    try:
                        # 关键：用参考项目同款 shadow_root 点击，而不是无效的主文档 CDP 搜索
                        token = getTurnstileToken(
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                            attempts=4,
                        )
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return 0;
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
                                log_callback(f"[*] Turnstile 复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile shadow 复用未完成: {cf_exc}")
                    if force_execute:
                        try:
                            trig = page.run_js(build_profile_submit_script("trigger"))
                            if log_callback:
                                log_callback(f"[Debug] Turnstile 主动触发结果: {trig}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] Turnstile 主动触发失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("profile-filled", "ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "email-step":
                raise ProfileSessionLost("xAI 最终注册页退回邮箱输入页，验证码会话已失效")

        submit_state = page.run_js(build_profile_submit_script("submit"))

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            token_len = str(read_turnstile_token_len(page) or (submit_state.split(":", 1)[1] if ":" in submit_state else "0"))
            if log_callback and should_log_cloudflare_wait(cf_wait_log_state, "profile-submit", token_len):
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if int(float(token_len or 0)) >= 80:
                sleep_with_cancel(0.3, cancel_callback)
                continue
            if now - wait_cf_since >= wait_limit and str(token_len) in {"0", ""}:
                raise Exception(
                    f"Cloudflare Turnstile {wait_limit:.0f}s 内未签发 token（token长度=0）。"
                    "已尝试 shadow_root 点选（参考 grok-auto-register）"
                )
            if now - last_humanize_at >= 5:
                humanize_page_activity(page, log_callback=None, cancel_callback=cancel_callback)
                last_humanize_at = now
            if now - last_cf_retry_at >= 2.0:
                try:
                    token = getTurnstileToken(
                        log_callback=log_callback,
                        cancel_callback=cancel_callback,
                        attempts=4,
                    )
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return 0;
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
                            log_callback(f"[*] 提交前 Turnstile 复用完成，回填长度={synced}")
                except Exception as click_exc:
                    if log_callback:
                        log_callback(f"[Debug] 提交前 Turnstile shadow 复用未完成: {click_exc}")
                if force_execute:
                    try:
                        trig = page.run_js(build_profile_submit_script("trigger"))
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 主动触发结果: {trig}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 主动触发失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        if submit_state == "submitted-no-challenge":
            # 兼容旧脚本返回值：无 CF 痕迹才允许；有空 token 时继续等。
            if log_callback:
                log_callback("[Debug] 收到 submitted-no-challenge，复查 Cloudflare 状态...")
            try:
                recheck = page.run_js(build_profile_submit_script("check"))
            except Exception:
                recheck = "unknown"
            if isinstance(recheck, str) and recheck.startswith("wait-cloudflare"):
                sleep_with_cancel(0.8, cancel_callback)
                continue
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        if submit_state == "wait-password-validation":
            if log_callback and should_log_cloudflare_wait(cf_wait_log_state, "password-validation", "0"):
                log_callback("[*] 等待 xAI 密码校验完成后再提交...")
            sleep_with_cancel(0.8, cancel_callback)
            continue
        wait_cf_since = None
        if submit_state == "no-submit-button" and log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")

        sleep_with_cancel(0.5, cancel_callback)

    if log_callback:
        try:
            diag = page.run_js(build_profile_submit_script("diagnose"))
        except Exception as diag_exc:
            diag = f"诊断失败: {diag_exc}"
        log_callback(f"[Debug] 最终注册页诊断: {diag}")
    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    last_final_retry_state = ""
    wait_cf_zero_since = None

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            page = _get_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在最终注册页时，若 Cloudflare 已通过，周期性重试点击提交。
            # xAI 页面会按区域显示中文或英文，不能只用中文标题判断。
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
function compactText(node) {
    return String(node?.innerText || node?.textContent || node?.value || node?.getAttribute?.('aria-label') || '')
        .replace(/\s+/g, '')
        .toLowerCase();
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = compactText(el);
    return t.includes('完成注册') || t.includes('completeyoursignup') || t.includes('completesignup') || t.includes('createyourgrokaccount');
});
const formHit = !!document.querySelector('input[name="givenName"], input[autocomplete="given-name"]')
    && !!document.querySelector('input[name="password"], input[type="password"]');
const urlHit = location.href.includes('/sign-up');
if (!titleHit && !(formHit && urlHit)) return 'not-final-page:' + compactText(document.body).slice(0, 80);

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
const rawHook = window.__grokTurnstile || {};
const capturedWidgets = Array.isArray(rawHook.widgets) ? rawHook.widgets : [];
const executedWidgetIds = Array.isArray(rawHook.executedWidgetIds) ? rawHook.executedWidgetIds : [];
if (!Array.isArray(rawHook.executedWidgetIds)) rawHook.executedWidgetIds = executedWidgetIds;
const executedWidgets = [];
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    // managed/flexible 模式经常没有可见 iframe，只要 token 未签发就必须等待，禁止空 token 提交。
    if (!solved) {
        try {
            if (window.turnstile && typeof window.turnstile.execute === 'function') {
                // token 仍为空时允许重复 execute，避免“只执行一次后永久卡住”。
                if (token.length === 0 && executedWidgetIds.length > 8) {
                    rawHook.executedWidgetIds = [];
                    executedWidgetIds.length = 0;
                }
                for (const widget of capturedWidgets) {
                    const id = widget && widget.id;
                    const idText = String(id || '');
                    if (!idText) continue;
                    if (token.length > 0 && executedWidgetIds.includes(idText)) continue;
                    try {
                        window.turnstile.execute(id);
                        if (!executedWidgetIds.includes(idText)) executedWidgetIds.push(idText);
                        executedWidgets.push(idText);
                    } catch (e) {}
                }
                if (!executedWidgets.length) {
                    try { window.turnstile.execute(); executedWidgets.push('anonymous'); } catch (e) {}
                }
            }
        } catch (e) {}
        return {
            state: 'final-page-wait-cf',
            tokenLen: token.length,
            executedWidgets,
            captured: {
                executeCount: rawHook.executeCount || 0,
                callbackCount: rawHook.callbackCount || 0,
                executedWidgetIds: executedWidgetIds.slice(-5),
                errors: Array.isArray(rawHook.errors) ? rawHook.errors.slice(-5) : [],
            },
        };
    }
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = compactText(node);
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('completesignup') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) return 'final-page-no-submit';
submitBtn.focus();
const rect = submitBtn.getBoundingClientRect();
try { submitBtn.click(); } catch (e) {}
return {
    state: 'final-page-submit-target',
    centerX: Math.round(rect.left + rect.width / 2),
    centerY: Math.round(rect.top + rect.height / 2),
    text: compactText(submitBtn).slice(0, 80),
    tokenLen: String((cfInput && cfInput.value) || '').trim().length,
    captured: (() => {
        try {
            const raw = window.__grokTurnstile || {};
            return {
                hookInstalled: !!window.__grokTurnstileHookInstalled,
                renderCount: raw.renderCount || 0,
                executeCount: raw.executeCount || 0,
                callbackCount: raw.callbackCount || 0,
                lastTokenLen: String(raw.lastToken || '').trim().length,
                executedWidgets,
                widgets: Array.isArray(raw.widgets) ? raw.widgets.slice(-5) : [],
                errors: Array.isArray(raw.errors) ? raw.errors.slice(-5) : [],
            };
        } catch (e) {
            return { error: String(e && e.message || e).slice(0, 160), executedWidgets };
        }
    })(),
};
                    """
                )
                last_submit_retry = now
                token_len_now = None
                if isinstance(retried, str):
                    last_final_retry_state = retried
                    if retried.startswith("final-page-wait-cf"):
                        token_len_now = retried.split(":", 1)[1] if ":" in retried else "0"
                if isinstance(retried, dict):
                    last_final_retry_state = str(retried.get("state") or "final-page-dict")
                    if last_final_retry_state == "final-page-wait-cf":
                        token_len_now = str(retried.get("tokenLen", "0"))
                        if retried.get("executedWidgets"):
                            try:
                                retried["challengeClick"] = _click_turnstile_challenge_if_visible(page)
                            except Exception as challenge_exc:
                                retried["challengeClickError"] = str(challenge_exc)[:160]
                    # 只有 CF 已通过/无 CF 时才原生点击提交，避免空 token 连点。
                    if (
                        last_final_retry_state == "final-page-submit-target"
                        and retried.get("centerX") is not None
                        and retried.get("centerY") is not None
                        and int(retried.get("tokenLen") or 0) >= 80
                    ):
                        try:
                            x = int(retried.get("centerX"))
                            y = int(retried.get("centerY"))
                            _dispatch_cdp_click(page, x, y, include_keyboard=False)
                            retried["nativeClicked"] = True
                            last_final_retry_state = f"{last_final_retry_state}:native-click:{x},{y}"
                        except Exception as native_exc:
                            retried["nativeClickError"] = str(native_exc)[:160]
                            last_final_retry_state = f"{last_final_retry_state}:native-failed"
                    if log_callback:
                        log_callback(f"[Debug] 最终页状态: {json.dumps(retried, ensure_ascii=False)}")
                if token_len_now is not None:
                    if str(token_len_now) in {"0", ""}:
                        if wait_cf_zero_since is None:
                            wait_cf_zero_since = now
                        elif now - wait_cf_zero_since >= 45:
                            raise Exception(
                                "最终页 Cloudflare Turnstile 45s 内未签发 token（token长度=0）。"
                                "常见原因是代理/出口 IP 信誉差或被 Cloudflare 静默拒绝，请更换代理后重试"
                            )
                    else:
                        wait_cf_zero_since = None
                    if log_callback:
                        log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len_now}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，主动触发 Turnstile（暂不空 token 提交）...")
                        try:
                            trig = page.run_js(
                                r"""
let executed = false;
try {
    if (window.turnstile && typeof window.turnstile.execute === 'function') {
        try { window.turnstile.execute(); executed = true; } catch (e) {}
    }
} catch (e) {}
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const tokenLen = String((cfInput && cfInput.value) || '').trim().length;
return 'final-trigger:' + (executed ? '1' : '0') + ':token=' + tokenLen;
                                """
                            )
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 主动触发结果: {trig}")
                            _click_turnstile_challenge_if_visible(page)
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 主动触发失败: {cf_exc}")
                        last_cf_retry_at = now
                if log_callback and retried in ("final-page-no-submit", "final-page-clicked-submit"):
                    log_callback(f"[Debug] 最终页状态: {retried}")

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
        f"等待超时：未获取到 sso cookie。最后最终页状态: {last_final_retry_state or 'unknown'}。已看到 cookies: {sorted(last_seen_names)}"
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
