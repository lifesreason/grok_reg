import time
from pathlib import Path

import pytest

import grok_register_ttk as reg


def wait_for_job(job, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = job.status()
        if status["status"] in {"completed", "failed", "stopped"}:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job did not finish in time: {job.status()}")


def test_validate_registration_config_requires_cloudflare_base():
    with pytest.raises(ValueError, match="Cloudflare API Base"):
        reg.validate_registration_config(
            {"email_provider": "cloudflare", "register_count": 1, "register_threads": 1}
        )


def test_validate_registration_config_normalizes_counts_and_paths():
    settings = reg.validate_registration_config(
        {
            "email_provider": "duckmail",
            "register_count": "3",
            "register_threads": "20",
            "cloudflare_paths": "api/domains,api/new_address,api/token,api/mails",
        }
    )

    assert settings["register_count"] == 3
    assert settings["register_threads"] == 10
    assert settings["cloudflare_path_domains"] == "/api/domains"
    assert settings["cloudflare_path_accounts"] == "/api/new_address"
    assert settings["cloudflare_path_token"] == "/api/token"
    assert settings["cloudflare_path_messages"] == "/api/mails"


def test_registration_job_runs_successfully(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(
        reg,
        "fill_email_and_submit",
        lambda log_callback=None, cancel_callback=None: ("user@example.com", "mail-token"),
    )
    monkeypatch.setattr(
        reg,
        "fill_code_and_submit",
        lambda email, token, log_callback=None, cancel_callback=None: "123456",
    )
    monkeypatch.setattr(
        reg,
        "fill_profile_and_submit",
        lambda log_callback=None, cancel_callback=None: {
            "given_name": "Ada",
            "family_name": "Lovelace",
            "password": "secret",
        },
    )
    monkeypatch.setattr(
        reg,
        "wait_for_sso_cookie",
        lambda log_callback=None, cancel_callback=None: "sso-token",
    )
    monkeypatch.setattr(
        reg,
        "add_token_to_grok2api_pools",
        lambda raw_token, email="", log_callback=None: None,
    )

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert status["success_count"] == 1
    assert status["fail_count"] == 0
    assert status["output_file"].endswith(".txt")
    assert "user@example.com----secret----sso-token" in tmp_path.joinpath(status["output_file"]).read_text(
        encoding="utf-8"
    )
    assert any("注册成功" in line for line in job.logs())


def test_registration_job_stop_request_sets_stopped_status(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)

    def slow_signup(log_callback=None, cancel_callback=None):
        while not cancel_callback():
            time.sleep(0.01)
        raise reg.RegistrationCancelled("stopped")

    monkeypatch.setattr(reg, "open_signup_page", slow_signup)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    time.sleep(0.05)
    job.stop()
    status = wait_for_job(job)

    assert status["status"] == "stopped"
    assert status["stop_requested"] is True


def test_optional_tkinter_import_handles_missing_shared_library():
    source = Path(reg.__file__).read_text(encoding="utf-8")

    assert "except ImportError:" in source


def test_cloudflare_block_page_is_detected():
    html = """
    <html>
      <head><title>Attention Required! | Cloudflare</title></head>
      <body>Sorry, you have been blocked</body>
    </html>
    """

    assert reg.detect_cloudflare_block_page(html) is True


def test_email_form_script_supports_identifier_input_and_continue_button():
    script = reg.build_email_form_script("fill")

    assert 'input[name="identifier"]' in reg.EMAIL_INPUT_SELECTOR
    assert 'input[placeholder*="email" i]' in reg.EMAIL_INPUT_SELECTOR
    assert 'action = "fill"' in script

    submit_script = reg.build_email_form_script("submit")

    assert "continue" in reg.EMAIL_SUBMIT_KEYWORDS
    assert "next" in reg.EMAIL_SUBMIT_KEYWORDS
    assert 'action = "submit"' in submit_script


def test_wait_for_email_verification_step_waits_until_otp(monkeypatch):
    class FakePage:
        def __init__(self):
            self.states = ['{"step":"email","errorText":"","url":"https://accounts.x.ai/sign-up"}', '{"step":"otp","errorText":"","url":"https://accounts.x.ai/sign-up"}']

        def run_js(self, script, *args):
            return self.states.pop(0)

    sleeps = []
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: sleeps.append(seconds))

    assert reg.wait_for_email_verification_step(FakePage(), "user@example.com", timeout=5) == "otp"
    assert sleeps == [0.8]


def test_wait_for_email_verification_step_detects_rejected_domain(monkeypatch):
    class FakePage:
        def run_js(self, script, *args):
            return (
                '{"step":"email","errorText":"",'
                '"bodySnippet":"Your email domain duckmail.sbs has been rejected. Please use a different email address."}'
            )

    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    with pytest.raises(reg.EmailDomainRejected) as exc:
        reg.wait_for_email_verification_step(FakePage(), "user@duckmail.sbs", timeout=0.1)

    assert exc.value.domain == "duckmail.sbs"


def test_registration_job_retries_when_email_domain_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    reg._rejected_email_domains.clear()
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)

    attempts = []

    def fake_fill_email(log_callback=None, cancel_callback=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise reg.EmailDomainRejected("duckmail.sbs")
        return "user@example.com", "mail-token"

    monkeypatch.setattr(reg, "fill_email_and_submit", fake_fill_email)
    monkeypatch.setattr(
        reg,
        "fill_code_and_submit",
        lambda email, token, log_callback=None, cancel_callback=None: "123456",
    )
    monkeypatch.setattr(
        reg,
        "fill_profile_and_submit",
        lambda log_callback=None, cancel_callback=None: {
            "given_name": "Ada",
            "family_name": "Lovelace",
            "password": "secret",
        },
    )
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert len(attempts) == 2
    assert any("邮箱域名被 x.ai 拒收" in line for line in job.logs())


def test_registration_job_stops_queue_when_email_provider_is_unusable(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)

    attempts = []

    def fake_fill_email(log_callback=None, cancel_callback=None):
        attempts.append(1)
        raise reg.EmailProviderUnavailable("DuckMail 可用域名已被 x.ai 拒收: duckmail.sbs")

    monkeypatch.setattr(reg, "fill_email_and_submit", fake_fill_email)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 6, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "failed"
    assert status["fail_count"] == 1
    assert len(attempts) == 1
    assert job.stop_requested is True
    assert any("邮箱服务商不可用" in line for line in job.logs())


def test_yyds_pick_domain_skips_rejected_domains_and_rotates(monkeypatch):
    reg._rejected_email_domains.clear()
    reg.remember_rejected_email_domain("first.example")
    monkeypatch.setattr(
        reg,
        "yyds_get_domains",
        lambda api_key=None, jwt=None: [
            {"domain": "first.example", "isVerified": True, "isPublic": True},
            {"domain": "second.example", "isVerified": True, "isPublic": True},
            {"domain": "third.example", "isVerified": True, "isPublic": True},
        ],
    )
    monkeypatch.setattr(reg, "_yyds_domain_index", 0, raising=False)

    assert reg.yyds_pick_domain() == "second.example"
    assert reg.yyds_pick_domain() == "third.example"


def test_list_registered_accounts_reads_accounts_files(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user1@example.com----Pass-1----sso-token-1\n"
        "bad line\n"
        "user2@example.com----Pass-2----sso=sso-token-2----refresh-token-2\n",
        encoding="utf-8",
    )

    accounts = reg.list_registered_accounts()

    assert [item["email"] for item in accounts] == ["user1@example.com", "user2@example.com"]
    assert accounts[0]["password"] == "Pass-1"
    assert accounts[0]["sso_preview"] == "sso-to...oken-1"
    assert accounts[0]["sso"] == "sso-token-1"
    assert accounts[0]["has_refresh_token"] is False
    assert accounts[1]["sso"] == "sso-token-2"
    assert accounts[1]["has_refresh_token"] is True
    assert accounts[1]["refresh_token"] == "refresh-token-2"
    assert accounts[1]["refresh_token_preview"] == "refres...oken-2"
    assert accounts[0]["line_no"] == 1
    assert accounts[1]["line_no"] == 3


def test_import_accounts_to_sub2api_requires_refresh_token(monkeypatch):
    calls = []
    monkeypatch.setattr(reg, "http_post", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(ValueError, match="缺少 refresh_token"):
        reg.import_accounts_to_sub2api(
            [{"email": "user1@example.com", "sso": "sso-token-1"}],
            {
                "sub2api_base": "https://sub2api.example/api/v1",
                "sub2api_admin_token": "admin-key",
            },
        )

    assert calls == []


def test_import_accounts_to_sub2api_posts_grok_refresh_token(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
            self.text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/admin/grok/oauth/refresh-token"):
            refresh_token = kwargs["json"]["refresh_token"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "access_token": f"access-for-{refresh_token}",
                        "refresh_token": f"rotated-{refresh_token}",
                        "token_type": "Bearer",
                        "expires_at": 1790000000,
                        "email": kwargs["json"].get("email", ""),
                    },
                }
            )
        if url.endswith("/admin/accounts"):
            return FakeResponse({"code": 0, "data": {"id": 100 + len(calls)}})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(reg, "http_post", fake_post)

    result = reg.import_accounts_to_sub2api(
        [
            {"email": "user1@example.com", "sso": "sso-token-1", "refresh_token": "refresh-token-1"},
            {"email": "user2@example.com", "sso": "sso=sso-token-2", "refresh_token": "refresh-token-2"},
        ],
        {
            "sub2api_base": "https://sub2api.example/api/v1",
            "sub2api_auth_mode": "x-api-key",
            "sub2api_admin_token": "admin-key",
            "sub2api_account_name": "Grok Auto",
            "sub2api_group_ids": "1, 2",
            "sub2api_concurrency": 5,
            "sub2api_priority": 40,
        },
    )

    assert result["imported"] is True
    assert result["total"] == 2
    assert calls[0][0] == "https://sub2api.example/api/v1/admin/grok/oauth/refresh-token"
    assert calls[0][1]["headers"]["x-api-key"] == "admin-key"
    assert calls[0][1]["json"]["refresh_token"] == "refresh-token-1"
    assert calls[1][0] == "https://sub2api.example/api/v1/admin/accounts"
    assert calls[1][1]["json"]["name"] == "Grok Auto - user1@example.com"
    assert calls[1][1]["json"]["platform"] == "grok"
    assert calls[1][1]["json"]["type"] == "oauth"
    assert calls[1][1]["json"]["credentials"]["access_token"] == "access-for-refresh-token-1"
    assert calls[1][1]["json"]["credentials"]["refresh_token"] == "rotated-refresh-token-1"
    assert calls[1][1]["json"]["credentials"]["token_type"] == "Bearer"
    assert calls[1][1]["json"]["credentials"]["expires_at"] == 1790000000
    assert calls[1][1]["json"]["credentials"]["client_id"]
    assert calls[1][1]["json"]["credentials"]["base_url"] == "https://api.x.ai/v1"
    assert calls[1][1]["json"]["group_ids"] == [1, 2]
    assert calls[1][1]["json"]["concurrency"] == 5
    assert calls[1][1]["json"]["priority"] == 40
    assert "sso-token-1" not in str(calls[1][1]["json"])


def test_should_log_cloudflare_wait_throttles_repeated_same_length(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    state = {}

    assert reg.should_log_cloudflare_wait(state, "submit", "0") is True
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is False
    now[0] += 4.9
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is False
    now[0] += 0.2
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is True
    assert reg.should_log_cloudflare_wait(state, "submit", "794") is True


def test_profile_submit_script_supports_role_button_and_aria_labels():
    script = reg.build_profile_submit_script("submit")

    assert '[role="button"]' in script
    assert "aria-label" in script
    assert "continue" in reg.PROFILE_SUBMIT_KEYWORDS
    assert "create" in reg.PROFILE_SUBMIT_KEYWORDS

    diagnose_script = reg.build_profile_submit_script("diagnose")

    assert 'action = "diagnose"' in diagnose_script


def test_yyds_code_polling_triggers_resend_callback(monkeypatch):
    now = [0.0]
    resend_calls = []

    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(reg, "yyds_get_messages", lambda address, token=None, jwt=None: [])

    def fake_sleep(seconds, cancel_callback=None):
        now[0] += 61

    monkeypatch.setattr(reg, "sleep_with_cancel", fake_sleep)

    with pytest.raises(Exception, match="未收到验证码"):
        reg.yyds_get_oai_code(
            "token",
            "user@example.com",
            timeout=120,
            resend_callback=lambda: resend_calls.append("resent"),
        )

    assert resend_calls


def test_docker_runs_visible_chromium_under_xvfb_by_default():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert 'GROK_REG_HEADLESS=0' in dockerfile
    assert 'xvfb-run' in dockerfile
    assert 'GROK_REG_HEADLESS: "0"' in compose


def test_loopback_proxy_is_rewritten_inside_docker(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")

    assert (
        reg.normalize_proxy_for_runtime("http://127.0.0.1:7890")
        == "http://host.docker.internal:7890"
    )
    assert (
        reg.normalize_proxy_for_runtime("socks5://localhost:1080")
        == "socks5://host.docker.internal:1080"
    )


def test_browser_options_apply_configured_proxy(monkeypatch):
    class FakeOptions:
        def __init__(self):
            self.arguments = []

        def auto_port(self):
            return self

        def set_timeouts(self, base=1):
            self.base_timeout = base
            return self

        def set_argument(self, key, value=None):
            self.arguments.append((key, value))
            return self

        def add_extension(self, path):
            self.extension = path
            return self

    monkeypatch.setattr(reg, "ChromiumOptions", FakeOptions)
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setitem(reg.config, "proxy", "http://127.0.0.1:7890")

    options = reg.create_browser_options()

    assert ("--proxy-server", "http://host.docker.internal:7890") in options.arguments


def test_docker_visible_browser_keeps_linux_startup_flags(monkeypatch):
    class FakeOptions:
        def __init__(self):
            self.arguments = []

        def auto_port(self):
            return self

        def set_timeouts(self, base=1):
            return self

        def set_argument(self, key, value=None):
            self.arguments.append((key, value))
            return self

        def add_extension(self, path):
            return self

    monkeypatch.setattr(reg, "ChromiumOptions", FakeOptions)
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "0")

    options = reg.create_browser_options()

    assert ("--no-sandbox", None) in options.arguments
    assert ("--disable-dev-shm-usage", None) in options.arguments
    assert ("--disable-gpu", None) in options.arguments
    assert ("--window-size", "1365,900") in options.arguments


def test_docker_forces_visible_mode_even_if_legacy_headless_env_is_set(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "1")
    monkeypatch.delenv("GROK_REG_ALLOW_HEADLESS", raising=False)

    assert reg.should_run_headless() is False


def test_headless_can_only_be_forced_with_explicit_override(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "1")
    monkeypatch.setenv("GROK_REG_ALLOW_HEADLESS", "1")

    assert reg.should_run_headless() is True


def test_visible_docker_starts_xvfb_when_display_is_missing(monkeypatch):
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(args, stdout=None, stderr=None):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(reg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(reg, "_xvfb_process", None)

    started = reg.ensure_virtual_display()

    assert started is True
    assert calls == [["Xvfb", ":99", "-screen", "0", "1365x900x24", "-nolisten", "tcp"]]
    assert reg.os.environ["DISPLAY"] == ":99"


def test_web_form_exposes_yyds_credentials():
    html = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="yyds_api_key"' in html
    assert 'name="yyds_jwt"' in html
