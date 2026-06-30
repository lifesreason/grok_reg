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
